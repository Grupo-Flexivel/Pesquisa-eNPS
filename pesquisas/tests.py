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

from .forms import ColaboradorForm
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
        self.colaborador = Colaborador.objects.create(nome='Ana', documento=DOCUMENTO_VALIDO, empresas=[self.empresa])
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
            'empresas': [filial.pk],
            'ativo': 'on',
        })

        self.assertRedirects(resposta, reverse('pesquisas:colaborador_lista'))
        self.colaborador.refresh_from_db()
        self.assertEqual(list(self.colaborador.empresas.all()), [filial])

    def test_colaborador_em_varias_empresas_aparece_e_conta_em_cada_uma(self):
        filial = Empresa.objects.create(nome='Filial')
        self.colaborador.empresas.add(filial)
        self.client.force_login(self.staff)

        lista = reverse('pesquisas:colaborador_lista')
        for empresa in (self.empresa, filial):
            self.assertEqual(list(self.client.get(lista, {'empresa': empresa.pk}).context['colaboradores']), [self.colaborador])
        self.assertEqual(len(self.client.get(lista, {'q': 'i'}).context['colaboradores']), 1)  # "Matriz" e "Filial"
        self.assertContains(self.client.get(lista), 'Filial, Matriz')

        pergunta_enps = self.pesquisa.perguntas.filter(tipo_resposta=Pergunta.TipoResposta.NOTA_0_10).first()
        resposta = RespostaPesquisa.objects.create(
            pesquisa=self.pesquisa, hash_documento_respondente=gerar_hash_documento(self.colaborador.documento),
        )
        DetalheResposta.objects.create(resposta_pesquisa=resposta, pergunta=pergunta_enps, valor_inteiro=10)
        url = reverse('pesquisas:resultados', args=[self.pesquisa.pk])
        for filtro in ({}, {'empresa': self.empresa.pk}, {'empresa': filial.pk}):
            contexto = self.client.get(url, filtro).context
            self.assertEqual((contexto['ativos'], contexto['respondentes']), (1, 1), filtro)

    def test_cadastro_exige_ao_menos_uma_empresa_e_aceita_varias(self):
        filial = Empresa.objects.create(nome='Filial')
        dados = {'nome': 'Bruno', 'documento': '11144477735', 'ativo': True}
        self.assertIn('empresas', ColaboradorForm({**dados, 'empresas': []}).errors)
        form = ColaboradorForm({**dados, 'empresas': [self.empresa.pk, filial.pk]})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(set(form.save().empresas.all()), {self.empresa, filial})

    def test_relatorio_filtra_elegiveis_e_respostas_por_empresa(self):
        filial = Empresa.objects.create(nome='Filial')
        outro = Colaborador.objects.create(nome='Bruno', documento='11144477735', empresas=[filial])
        fora_do_periodo = Colaborador.objects.create(nome='Carla', documento='12345678909', empresas=[filial])
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


class ColaboradorDatasContratoTests(TestCase):
    def setUp(self):
        self.empresa = Empresa.objects.create(nome='Matriz')
        self.staff = criar_usuario('staff', administrador=True)
        self.dados = {'nome': 'Ana', 'documento': DOCUMENTO_VALIDO, 'empresas': [self.empresa.pk], 'ativo': True,
                      'celulares': ['47999998888'], 'email': 'ana@exemplo.com.br'}

    def test_novo_cadastro_nao_exige_data_de_contratacao(self):
        self.assertTrue(ColaboradorForm(self.dados).is_valid())

        form = ColaboradorForm({**self.dados, 'data_contratacao': '2024-03-01'})
        self.assertTrue(form.is_valid(), form.errors)
        colaborador = form.save()
        self.assertEqual(str(colaborador.data_contratacao), '2024-03-01')
        self.assertIsNone(colaborador.data_demissao)

    def test_colaborador_legado_sem_datas_edita_e_lista_normalmente(self):
        legado = Colaborador.objects.create(nome='Legado', documento=DOCUMENTO_VALIDO, empresas=[self.empresa])
        self.assertIsNone(legado.data_contratacao)

        form = ColaboradorForm({**self.dados, 'nome': 'Legado Atualizado'}, instance=legado)
        self.assertFalse(form.fields['data_contratacao'].required)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

        self.client.force_login(self.staff)
        self.assertContains(self.client.get(reverse('pesquisas:colaborador_lista')), 'Legado Atualizado')
        self.assertContains(self.client.get(reverse('pesquisas:colaborador_editar', args=[legado.pk])), 'type="date"')

    def test_demissao_anterior_a_contratacao_e_invalida(self):
        form = ColaboradorForm({**self.dados, 'data_contratacao': '2024-03-01', 'data_demissao': '2024-02-01'})
        self.assertFalse(form.is_valid())
        self.assertIn('data_demissao', form.errors)

    def test_data_de_nascimento_opcional_e_nao_pode_ser_futura(self):
        self.assertFalse(ColaboradorForm(self.dados).fields['data_nascimento'].required)

        form = ColaboradorForm({**self.dados, 'data_nascimento': '1990-05-20'})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(str(form.save().data_nascimento), '1990-05-20')

        amanha = timezone.localdate() + timedelta(days=1)
        form = ColaboradorForm({**self.dados, 'data_nascimento': amanha.isoformat()})
        self.assertFalse(form.is_valid())
        self.assertIn('data_nascimento', form.errors)


class ColaboradorContatoTests(TestCase):
    def setUp(self):
        self.empresa = Empresa.objects.create(nome='Matriz')
        self.dados = {
            'nome': 'Ana', 'documento': DOCUMENTO_VALIDO, 'empresas': [self.empresa.pk], 'ativo': True,
            'data_contratacao': '2024-03-01', 'celulares': ['47999998888'], 'email': ' Ana@Exemplo.com.br ',
        }

    def test_novo_cadastro_exige_apenas_nome_documento_e_empresa(self):
        form = ColaboradorForm({'nome': 'Ana', 'documento': DOCUMENTO_VALIDO, 'empresas': [self.empresa.pk], 'ativo': True})
        self.assertTrue(form.is_valid(), form.errors)

        form = ColaboradorForm({'ativo': True})
        self.assertEqual(set(form.errors) - {'__all__'}, {'nome', 'documento', 'empresas'})

    def test_celular_e_email_sao_normalizados(self):
        form = ColaboradorForm(self.dados)
        self.assertTrue(form.is_valid(), form.errors)
        colaborador = form.save()
        self.assertEqual(colaborador.celulares, ['(47) 99999-8888'])
        self.assertEqual(colaborador.email, 'ana@exemplo.com.br')

    def test_aceita_varios_celulares_ignorando_vazios_e_repetidos(self):
        form = ColaboradorForm({**self.dados, 'celulares': ['47999998888', '', '(47) 99999-8888', '11 98888 7777']})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().celulares, ['(47) 99999-8888', '(11) 98888-7777'])

    def test_rejeita_celular_e_email_fora_do_padrao(self):
        for celular in ('4733334444', '47899998888', '(07) 99999-8888', '999998888'):
            form = ColaboradorForm({**self.dados, 'celulares': ['47999998888', celular]})
            self.assertIn('celulares', form.errors, celular)
            self.assertIn(celular, str(form.errors['celulares']))
        form = ColaboradorForm({**self.dados, 'email': 'ana@'})
        self.assertIn('email', form.errors)

    def test_colaborador_legado_sem_contato_edita_normalmente(self):
        legado = Colaborador.objects.create(nome='Legado', documento=DOCUMENTO_VALIDO, empresas=[self.empresa])
        self.assertEqual((legado.celulares, legado.email), ([], ''))

        form = ColaboradorForm({**self.dados, 'nome': 'Legado Atualizado', 'celulares': [''], 'email': ''}, instance=legado)
        self.assertFalse(form.fields['celulares'].required)
        self.assertFalse(form.fields['email'].required)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

    def test_tela_de_edicao_mostra_um_campo_por_celular_e_remove_todos(self):
        colaborador = Colaborador.objects.create(
            nome='Ana', documento=DOCUMENTO_VALIDO, empresas=[self.empresa], celulares=['47999998888', '11988887777'],
        )
        self.client.force_login(criar_usuario('staff', administrador=True))
        url = reverse('pesquisas:colaborador_editar', args=[colaborador.pk])
        pagina = self.client.get(url)
        self.assertContains(pagina, 'value="(47) 99999-8888"')
        self.assertContains(pagina, 'value="(11) 98888-7777"')
        self.assertContains(pagina, 'Adicionar outro número')

        # Sem nenhum campo de celular enviado = todos os números foram removidos.
        resposta = self.client.post(url, {'nome': 'Ana', 'documento': DOCUMENTO_VALIDO, 'empresas': [self.empresa.pk], 'ativo': 'on'})
        self.assertRedirects(resposta, reverse('pesquisas:colaborador_lista'))
        colaborador.refresh_from_db()
        self.assertEqual(colaborador.celulares, [])


class DesligamentoTestBase(TestCase):
    def setUp(self):
        from desligamentos.models import FormularioDesligamento

        self.empresa = Empresa.objects.create(nome='Matriz')
        self.staff = criar_usuario('staff', administrador=True)
        self.client.force_login(self.staff)
        self.formulario = FormularioDesligamento.objects.create(
            nome='Iniciativa da empresa', iniciativa=FormularioDesligamento.Iniciativa.EMPRESA, criado_por=self.staff,
        )
        self.colaborador = Colaborador.objects.create(nome='Ana', documento=DOCUMENTO_VALIDO, empresas=[self.empresa])
        self.editar = reverse('pesquisas:colaborador_editar', args=[self.colaborador.pk])
        self.status = reverse('pesquisas:colaborador_status', args=[self.colaborador.pk])
        self.dados = {'nome': 'Ana', 'documento': DOCUMENTO_VALIDO, 'empresas': [self.empresa.pk]}
        self.motivo = {'iniciativa': 'EMPRESA', 'formulario_id': self.formulario.pk}

    def recarregar(self):
        self.colaborador.refresh_from_db()
        return self.colaborador


class DataDemissaoInativacaoTests(DesligamentoTestBase):
    """Data de demissão até hoje = inativação; inativar exige a data; reativar apaga a data."""

    def test_preencher_data_de_demissao_inativa_mesmo_com_ativo_marcado(self):
        from desligamentos.models import Desligamento

        resposta = self.client.post(self.editar, {**self.dados, 'ativo': 'on', 'data_demissao': '2026-09-30', **self.motivo})
        self.assertRedirects(resposta, reverse('pesquisas:colaborador_lista'))
        colaborador = self.recarregar()
        self.assertFalse(colaborador.ativo)
        self.assertEqual(str(colaborador.data_demissao), '2026-09-30')
        self.assertIsNotNone(colaborador.data_inativacao)
        self.assertTrue(Desligamento.objects.filter(colaborador=colaborador).exists())

    def test_data_de_demissao_sem_motivo_nao_inativa(self):
        resposta = self.client.post(self.editar, {**self.dados, 'ativo': 'on', 'data_demissao': '2026-09-30'})
        self.assertEqual(resposta.status_code, 200)
        self.assertTrue(self.recarregar().ativo)

    def test_inativar_sem_data_de_demissao_pede_a_data(self):
        resposta = self.client.post(self.editar, {**self.dados, **self.motivo})
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'Informe a data de demissão para inativar o colaborador.')
        colaborador = self.recarregar()
        self.assertTrue(colaborador.ativo)
        self.assertIsNone(colaborador.data_demissao)

    def test_reativar_apaga_a_data_de_demissao(self):
        Colaborador.objects.filter(pk=self.colaborador.pk).update(ativo=False, data_demissao='2026-09-30')
        self.client.post(self.editar, {**self.dados, 'ativo': 'on', 'data_demissao': '2026-09-30'})
        colaborador = self.recarregar()
        self.assertTrue(colaborador.ativo)
        self.assertIsNone(colaborador.data_demissao)

    def test_inativo_antigo_sem_data_de_demissao_edita_normalmente(self):
        Colaborador.objects.filter(pk=self.colaborador.pk).update(ativo=False)
        resposta = self.client.post(self.editar, {**self.dados, 'nome': 'Ana Souza'})
        self.assertRedirects(resposta, reverse('pesquisas:colaborador_lista'))
        colaborador = self.recarregar()
        self.assertEqual(colaborador.nome, 'Ana Souza')
        self.assertFalse(colaborador.ativo)

    def test_novo_cadastro_com_data_de_demissao_entra_inativo(self):
        resposta = self.client.post(reverse('pesquisas:colaborador_criar'), {
            'nome': 'Bruno', 'documento': '11144477735', 'empresas': [self.empresa.pk], 'ativo': 'on',
            'data_demissao': '2026-09-30',
        })
        self.assertRedirects(resposta, reverse('pesquisas:colaborador_lista'))
        bruno = Colaborador.objects.get(documento='11144477735')
        self.assertFalse(bruno.ativo)
        self.assertIsNotNone(bruno.data_inativacao)

    def test_botao_inativar_da_lista_exige_e_grava_a_data_de_demissao(self):
        resposta = self.client.post(self.status, self.motivo, follow=True)
        self.assertContains(resposta, 'Informe a data de demissão para inativar Ana.')
        self.assertTrue(self.recarregar().ativo)

        self.client.post(self.status, {**self.motivo, 'data_demissao': '2026-09-30'})
        colaborador = self.recarregar()
        self.assertFalse(colaborador.ativo)
        self.assertEqual(str(colaborador.data_demissao), '2026-09-30')

        # Ativar pela lista apaga a data de demissão.
        self.client.post(self.status)
        colaborador = self.recarregar()
        self.assertTrue(colaborador.ativo)
        self.assertIsNone(colaborador.data_demissao)

    def test_botao_inativar_da_lista_recusa_demissao_antes_da_contratacao(self):
        Colaborador.objects.filter(pk=self.colaborador.pk).update(data_contratacao='2026-09-01')
        resposta = self.client.post(self.status, {**self.motivo, 'data_demissao': '2026-08-01'}, follow=True)
        self.assertContains(resposta, 'não pode ser anterior à data de contratação')
        self.assertTrue(self.recarregar().ativo)

    def test_lista_mostra_campo_de_data_no_modal_de_inativar(self):
        self.assertContains(self.client.get(reverse('pesquisas:colaborador_lista')), 'name="data_demissao"')


class DesligamentoAgendadoTests(DesligamentoTestBase):
    """Data de demissão futura: segue ativo, motivo guardado; no dia, inativa e vai para Pendentes."""

    def futura(self, dias=10):
        return timezone.localdate() + timedelta(days=dias)

    def agendados(self):
        from desligamentos.models import Desligamento
        return Desligamento.objects.filter(colaborador=self.colaborador, status=Desligamento.Status.AGENDADO)

    def test_data_futura_agenda_sem_inativar_nem_criar_pendencia(self):
        from desligamentos.models import Desligamento
        from notificacoes.models import Notificacao

        resposta = self.client.post(self.editar, {**self.dados, 'ativo': 'on', 'data_demissao': self.futura(), **self.motivo}, follow=True)
        self.assertContains(resposta, 'Desligamento agendado')
        colaborador = self.recarregar()
        self.assertTrue(colaborador.ativo)
        self.assertEqual(colaborador.data_demissao, self.futura())
        self.assertEqual(self.agendados().count(), 1)
        self.assertFalse(Desligamento.objects.exclude(status=Desligamento.Status.AGENDADO).exists())
        self.assertFalse(Notificacao.objects.exists())
        self.assertEqual(list(self.client.get(reverse('desligamentos:kanban')).context['pendentes']), [])
        self.assertContains(self.client.get(reverse('pesquisas:colaborador_lista')), 'Desligamento em')

    def test_data_futura_tambem_exige_motivo(self):
        resposta = self.client.post(self.editar, {**self.dados, 'ativo': 'on', 'data_demissao': self.futura()})
        self.assertEqual(resposta.status_code, 200)
        self.assertIsNone(self.recarregar().data_demissao)
        self.assertFalse(self.agendados().exists())

    def test_no_dia_da_demissao_inativa_e_vai_para_pendentes(self):
        from desligamentos.models import Desligamento
        from desligamentos.services import processar_desligamentos_agendados
        from notificacoes.models import Notificacao

        self.client.post(self.editar, {**self.dados, 'ativo': 'on', 'data_demissao': self.futura(), **self.motivo})
        self.assertEqual(processar_desligamentos_agendados(hoje=self.futura(9)), 0)  # véspera
        self.assertTrue(self.recarregar().ativo)

        self.assertEqual(processar_desligamentos_agendados(hoje=self.futura()), 1)
        colaborador = self.recarregar()
        self.assertFalse(colaborador.ativo)
        self.assertIsNotNone(colaborador.data_inativacao)
        desligamento = Desligamento.objects.get(colaborador=colaborador)
        self.assertEqual(desligamento.status, Desligamento.Status.PENDENTE)
        self.assertEqual(desligamento.formulario, self.formulario)
        self.assertTrue(Notificacao.objects.filter(destinatario=self.staff).exists())
        self.assertEqual(processar_desligamentos_agendados(hoje=self.futura()), 0)  # idempotente

    def test_kanban_ja_mostra_quem_chegou_a_data(self):
        self.client.post(self.editar, {**self.dados, 'ativo': 'on', 'data_demissao': self.futura(), **self.motivo})
        Colaborador.objects.filter(pk=self.colaborador.pk).update(data_demissao=timezone.localdate())
        pendentes = self.client.get(reverse('desligamentos:kanban')).context['pendentes']
        self.assertEqual([d.colaborador_id for d in pendentes], [self.colaborador.pk])
        self.assertFalse(self.recarregar().ativo)

    def test_apagar_a_data_cancela_o_agendamento(self):
        self.client.post(self.editar, {**self.dados, 'ativo': 'on', 'data_demissao': self.futura(), **self.motivo})
        resposta = self.client.post(self.editar, {**self.dados, 'ativo': 'on'}, follow=True)
        self.assertContains(resposta, 'O desligamento agendado foi cancelado')
        self.assertFalse(self.agendados().exists())
        self.assertTrue(self.recarregar().ativo)

    def test_trocar_para_data_passada_inativa_com_o_motivo_ja_agendado(self):
        from desligamentos.models import Desligamento

        self.client.post(self.editar, {**self.dados, 'ativo': 'on', 'data_demissao': self.futura(), **self.motivo})
        self.assertContains(self.client.get(self.editar), 'Desligamento agendado para')
        self.client.post(self.editar, {**self.dados, 'data_demissao': '2026-09-30'})  # sem reenviar o motivo
        self.assertFalse(self.recarregar().ativo)
        self.assertEqual(Desligamento.objects.get(colaborador=self.colaborador).status, Desligamento.Status.PENDENTE)

    def test_botao_inativar_da_lista_com_data_futura_agenda(self):
        resposta = self.client.post(self.status, {**self.motivo, 'data_demissao': self.futura()}, follow=True)
        self.assertContains(resposta, 'Desligamento agendado')
        self.assertTrue(self.recarregar().ativo)
        self.assertEqual(self.agendados().count(), 1)

    def test_cadastro_e_importacao_com_data_futura_inativam_so_no_dia(self):
        from desligamentos.services import processar_desligamentos_agendados
        from .importacao import importar_colaboradores

        self.client.post(reverse('pesquisas:colaborador_criar'), {
            'nome': 'Bruno', 'documento': '11144477735', 'empresas': [self.empresa.pk], 'ativo': 'on',
            'data_demissao': self.futura(),
        })
        importar_colaboradores(
            f'nome;documento;empresa;data_demissao\nCarla;12345678909;Matriz;{self.futura():%d/%m/%Y}\n'.encode(), 'a.csv',
        )
        for documento in ('11144477735', '12345678909'):
            self.assertTrue(Colaborador.objects.get(documento=documento).ativo)

        processar_desligamentos_agendados(hoje=self.futura())
        for documento in ('11144477735', '12345678909'):
            self.assertFalse(Colaborador.objects.get(documento=documento).ativo)


class ColaboradorGestorTests(TestCase):
    def setUp(self):
        self.empresa = Empresa.objects.create(nome='Matriz')
        self.gestora = Colaborador.objects.create(nome='Gestora', documento='12345678062', empresas=[self.empresa], eh_gestor=True)
        self.comum = Colaborador.objects.create(nome='Comum', documento='98765432029', empresas=[self.empresa])
        self.admin = criar_usuario('admin.total', administrador=True)
        self.dados = {
            'nome': 'Ana', 'documento': DOCUMENTO_VALIDO, 'empresas': [self.empresa.pk], 'ativo': True,
            'data_contratacao': '2026-09-01',
            'celulares': ['47999998888'], 'email': 'ana@exemplo.com.br',
        }

    def test_dropdown_lista_apenas_colaboradores_gestores(self):
        Colaborador.objects.create(nome='Gestor inativo', documento='11122233396', empresas=[self.empresa],
                                   eh_gestor=True, ativo=False)
        opcoes = list(ColaboradorForm().fields['gestor'].queryset)
        self.assertEqual(opcoes, [self.gestora])
        self.assertNotIn(self.admin, opcoes)

    def test_gestor_nao_aparece_como_gestor_de_si_mesmo(self):
        self.assertNotIn(self.gestora, ColaboradorForm(instance=self.gestora).fields['gestor'].queryset)

    def test_nao_aceita_colaborador_que_nao_e_gestor(self):
        form = ColaboradorForm({**self.dados, 'gestor': self.comum.pk})
        self.assertFalse(form.is_valid())
        self.assertIn('gestor', form.errors)

    def test_vincula_gestor(self):
        form = ColaboradorForm({**self.dados, 'gestor': self.gestora.pk})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().gestor, self.gestora)

    def test_nao_desmarca_gestor_com_liderados(self):
        Colaborador.objects.create(nome='Liderado', documento=DOCUMENTO_VALIDO, empresas=[self.empresa], gestor=self.gestora)
        form = ColaboradorForm(
            {'nome': 'Gestora', 'documento': '12345678062', 'empresas': [self.empresa.pk], 'ativo': True},
            instance=self.gestora,
        )
        self.assertFalse(form.is_valid())
        self.assertIn('eh_gestor', form.errors)


class ModuloGeralColaboradoresTests(TestCase):
    """Quem tem o Módulo Geral gerencia colaboradores: lista, cadastra, edita e ativa/inativa."""

    def setUp(self):
        self.empresa = Empresa.objects.create(nome='Matriz')
        self.client.force_login(criar_usuario('geral', modulos=['geral']))

    def test_cadastra_e_edita_colaborador(self):
        self.assertEqual(self.client.get(reverse('pesquisas:colaborador_criar')).status_code, 200)
        resposta = self.client.post(reverse('pesquisas:colaborador_criar'), {
            'nome': 'Novo', 'documento': DOCUMENTO_VALIDO, 'empresas': [self.empresa.pk], 'ativo': 'on',
            'data_contratacao': '2026-09-01',
            'celulares': ['47999998888'], 'email': 'novo@exemplo.com.br',
        })
        self.assertRedirects(resposta, reverse('pesquisas:colaborador_lista'))
        colaborador = Colaborador.objects.get(documento=DOCUMENTO_VALIDO)
        self.assertEqual(self.client.get(reverse('pesquisas:colaborador_editar', args=[colaborador.pk])).status_code, 200)
