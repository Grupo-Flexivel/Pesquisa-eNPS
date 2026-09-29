from datetime import timedelta
from io import StringIO
from pathlib import Path
from tempfile import NamedTemporaryFile

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Colaborador, DetalheResposta, Empresa, Pergunta, Pesquisa, RespostaPesquisa, User, validar_documento
from .services import gerar_hash_documento
from .test_utils import criar_usuario


DOCUMENTO_VALIDO = '52998224725'  # Pode ser CPF ou CNPJ válido


@override_settings(SECRET_SALT='segredo-de-teste')
class DominioTests(TestCase):
    def setUp(self):
        self.staff = criar_usuario('gestor', password='senha', administrador=True)
        self.empresa = Empresa.objects.create(nome='Matriz')

    def pesquisa(self, inicio=None):
        return Pesquisa.objects.create(
            titulo='Clima',
            data_inicio=inicio or timezone.now() + timedelta(hours=1),
            data_final=timezone.now() + timedelta(days=2),
            criado_por=self.staff,
        )

    def test_valida_documento_e_hash_nao_expoe_documento(self):
        validar_documento(DOCUMENTO_VALIDO)
        hash_documento = gerar_hash_documento(DOCUMENTO_VALIDO)
        self.assertEqual(len(hash_documento), 64)
        self.assertNotIn(DOCUMENTO_VALIDO, hash_documento)
        with self.assertRaises(ValidationError):
            validar_documento('11111111111')

    def test_pesquisa_iniciada_nao_pode_ser_editada(self):
        pesquisa = self.pesquisa()
        Pesquisa.objects.filter(pk=pesquisa.pk).update(data_inicio=timezone.now() - timedelta(minutes=1))
        pesquisa.refresh_from_db()
        pesquisa.titulo = 'Alterada'
        pesquisa.save()  
        pesquisa.refresh_from_db()
        self.assertEqual(pesquisa.titulo, 'Alterada')

    def test_constraint_impede_resposta_duplicada(self):
        pesquisa = self.pesquisa()
        RespostaPesquisa.objects.create(pesquisa=pesquisa, hash_documento_respondente='a' * 64)
        with self.assertRaises(IntegrityError), transaction.atomic():
            RespostaPesquisa.objects.create(pesquisa=pesquisa, hash_documento_respondente='a' * 64)


@override_settings(SECRET_SALT='segredo-de-teste')
class FluxoExternoTests(TestCase):
    def setUp(self):
        self.staff = criar_usuario('gestor', password='senha', administrador=True)
        self.empresa = Empresa.objects.create(nome='Matriz')
        self.colaborador = Colaborador.objects.create(nome='Ana', documento=DOCUMENTO_VALIDO, empresa=self.empresa)
        agora = timezone.now()
        self.pesquisa = Pesquisa.objects.create(
            titulo='eNPS trimestral', data_inicio=agora + timedelta(hours=1),
            data_final=agora + timedelta(days=1), criado_por=self.staff,
        )
        for ordem, tipo in enumerate(('NOTA_0_10', 'TEXTO', 'TEXTO'), 1):
            Pergunta.objects.create(pesquisa=self.pesquisa, texto_pergunta=f'Pergunta {ordem}', tipo_resposta=tipo, ordem=ordem)
        Pesquisa.objects.filter(pk=self.pesquisa.pk).update(data_inicio=agora - timedelta(minutes=1))
        self.pesquisa.refresh_from_db()
        self.client = Client()

    def test_fluxo_completo_e_bloqueio_de_duplicidade(self):
        entrada = reverse('pesquisas:responder', args=[self.pesquisa.link_uuid])
        self.assertContains(self.client.get(entrada), self.pesquisa.titulo)
        resposta = self.client.post(entrada, {'documento': DOCUMENTO_VALIDO})
        self.assertRedirects(resposta, reverse('pesquisas:responder_etapa', args=[self.pesquisa.link_uuid, 1]))
        for etapa, valor in ((1, 10), (2, 'Muito bom'), (3, 'Continuem')):
            resposta = self.client.post(reverse('pesquisas:responder_etapa', args=[self.pesquisa.link_uuid, etapa]), {'valor': valor})
        self.assertRedirects(resposta, reverse('pesquisas:agradecimento', args=[self.pesquisa.link_uuid]))
        self.assertContains(self.client.get(resposta.url), 'Obrigado por participar')
        registro = RespostaPesquisa.objects.get()
        self.assertEqual(registro.hash_documento_respondente, gerar_hash_documento(DOCUMENTO_VALIDO))
        self.assertEqual(registro.detalhes.count(), 3)
        self.assertFalse(RespostaPesquisa.objects.filter(hash_documento_respondente=DOCUMENTO_VALIDO).exists())
        resposta = self.client.post(entrada, {'documento': DOCUMENTO_VALIDO})
        self.assertContains(resposta, 'já foi enviada')

    def test_usuario_nao_staff_nao_acessa_dashboard(self):
        comum = User.objects.create_user('comum', password='senha')
        self.client.force_login(comum)
        resposta = self.client.get(reverse('pesquisas:dashboard'))
        self.assertEqual(resposta.status_code, 302)

    def test_staff_renderiza_dashboard_e_relatorio(self):
        self.client.force_login(self.staff)
        self.assertContains(self.client.get(reverse('pesquisas:dashboard')), 'Dashboard')
        self.assertContains(self.client.get(reverse('pesquisas:resultados', args=[self.pesquisa.pk])), 'Resultados consolidados')

    def test_edicao_manual_permite_trocar_empresa(self):
        filial = Empresa.objects.create(nome='Filial')
        self.client.force_login(self.staff)
        url = reverse('pesquisas:colaborador_editar', args=[self.colaborador.pk])

        self.assertContains(self.client.get(url), filial.nome)
        resposta = self.client.post(url, {
            'nome': self.colaborador.nome,
            'documento': self.colaborador.documento,
            'empresa': filial.pk,
            'ativo': 'on',
        })

        self.assertRedirects(resposta, reverse('pesquisas:colaborador_lista'))
        self.colaborador.refresh_from_db()
        self.assertEqual(self.colaborador.empresa, filial)

    def test_relatorio_filtra_elegiveis_e_respostas_por_empresa(self):
        filial = Empresa.objects.create(nome='Filial')
        outro = Colaborador.objects.create(nome='Bruno', documento='11144477735', empresa=filial)
        fora_do_periodo = Colaborador.objects.create(nome='Carla', documento='12345678909', empresa=filial)
        Colaborador.objects.filter(pk=fora_do_periodo.pk).update(
            data_criacao=self.pesquisa.data_final + timedelta(seconds=1),
        )

        pergunta_enps = self.pesquisa.perguntas.filter(tipo_resposta=Pergunta.TipoResposta.NOTA_0_10).first()
        for colaborador, nota in (
            (self.colaborador, 10),
            (outro, 5),
            (fora_do_periodo, 0),
        ):
            resposta = RespostaPesquisa.objects.create(
                pesquisa=self.pesquisa,
                hash_documento_respondente=gerar_hash_documento(colaborador.documento),
            )
            DetalheResposta.objects.create(
                resposta_pesquisa=resposta,
                pergunta=pergunta_enps,
                valor_inteiro=nota,
            )

        self.client.force_login(self.staff)
        url = reverse('pesquisas:resultados', args=[self.pesquisa.pk])

        geral = self.client.get(url)
        self.assertIsNone(geral.context['empresa_selecionada'])
        self.assertEqual(geral.context['ativos'], 2)
        self.assertEqual(geral.context['respondentes'], 2)
        self.assertEqual(geral.context['promotores'], 1)
        self.assertEqual(geral.context['detratores'], 1)

        resultado_filial = self.client.get(url, {'empresa': filial.pk})
        self.assertEqual(resultado_filial.context['empresa_selecionada'], filial)
        self.assertEqual(resultado_filial.context['ativos'], 1)
        self.assertEqual(resultado_filial.context['respondentes'], 1)
        self.assertEqual(resultado_filial.context['promotores'], 0)
        self.assertEqual(resultado_filial.context['detratores'], 1)


@override_settings(SECRET_SALT='segredo-de-teste')
class ImportacaoColaboradoresTests(TestCase):
    def test_importa_empresa_sem_duplicar_por_diferenca_de_caixa(self):
        with NamedTemporaryFile('w', encoding='utf-8', suffix='.csv', delete=False, newline='') as arquivo:
            arquivo.write(
                'nome,documento,empresa\n'
                'Ana Silva,52998224725,Matriz\n'
                'Bruno Souza,11144477735,matriz\n'
            )
            caminho = Path(arquivo.name)

        try:
            saida = StringIO()
            call_command('importar_colaboradores', caminho, stdout=saida)
        finally:
            caminho.unlink(missing_ok=True)

        self.assertEqual(Empresa.objects.filter(nome__iexact='Matriz').count(), 1)
        empresa = Empresa.objects.get(nome__iexact='Matriz')
        self.assertEqual(empresa.colaboradores.count(), 2)
        self.assertIn('2 criados', saida.getvalue())
