from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from notificacoes.models import Notificacao
from pesquisas.models import Colaborador, Empresa, User
from pesquisas.test_utils import criar_usuario

from .models import Desligamento, DetalheRespostaDesligamento, FormularioDesligamento, PerguntaDesligamento


class FluxoDesligamentoTests(TestCase):
    def setUp(self):
        self.staff = criar_usuario('admin', password='senha-forte-123', administrador=True)
        self.empresa = Empresa.objects.create(nome='Empresa Teste')
        self.colaborador = Colaborador.objects.create(
            nome='Fulano de Tal', documento='52998224725', empresas=[self.empresa], ativo=True,
        )
        self.formulario = FormularioDesligamento.objects.create(
            nome='Desligamento - Iniciativa da empresa',
            iniciativa=FormularioDesligamento.Iniciativa.EMPRESA,
            criado_por=self.staff,
        )
        self.pergunta_texto = PerguntaDesligamento.objects.create(
            formulario=self.formulario, texto_pergunta='Por que está saindo?',
            tipo_resposta=PerguntaDesligamento.TipoResposta.TEXTO, ordem=1,
        )
        self.pergunta_nota = PerguntaDesligamento.objects.create(
            formulario=self.formulario, texto_pergunta='Nota geral',
            tipo_resposta=PerguntaDesligamento.TipoResposta.NOTA_0_10, ordem=2,
        )
        self.pergunta_radio = PerguntaDesligamento.objects.create(
            formulario=self.formulario, texto_pergunta='Motivo principal',
            tipo_resposta=PerguntaDesligamento.TipoResposta.RADIO, ordem=3,
        )
        self.opcao_a = self.pergunta_radio.opcoes.create(texto_opcao='Salário', ordem=1)
        self.opcao_b = self.pergunta_radio.opcoes.create(texto_opcao='Clima organizacional', ordem=2)
        self.pergunta_checkbox = PerguntaDesligamento.objects.create(
            formulario=self.formulario, texto_pergunta='O que poderia melhorar?',
            tipo_resposta=PerguntaDesligamento.TipoResposta.CHECKBOX, ordem=4,
        )
        self.opcao_c = self.pergunta_checkbox.opcoes.create(texto_opcao='Liderança', ordem=1)
        self.opcao_d = self.pergunta_checkbox.opcoes.create(texto_opcao='Benefícios', ordem=2)

    def test_inativar_colaborador_cria_card_pendente(self):
        client = Client()
        client.force_login(self.staff)
        response = client.post(
            reverse('pesquisas:colaborador_status', args=[self.colaborador.pk]),
            {'iniciativa': 'EMPRESA', 'formulario_id': self.formulario.pk, 'data_demissao': '2026-10-01'},
        )
        self.assertEqual(response.status_code, 302)
        self.colaborador.refresh_from_db()
        self.assertFalse(self.colaborador.ativo)
        desligamento = Desligamento.objects.get(colaborador=self.colaborador)
        self.assertEqual(desligamento.status, Desligamento.Status.PENDENTE)
        self.assertEqual(desligamento.formulario, self.formulario)

    def test_inativar_sem_motivo_nao_altera_status(self):
        client = Client()
        client.force_login(self.staff)
        client.post(reverse('pesquisas:colaborador_status', args=[self.colaborador.pk]), {})
        self.colaborador.refresh_from_db()
        self.assertTrue(self.colaborador.ativo)
        self.assertFalse(Desligamento.objects.filter(colaborador=self.colaborador).exists())

    def test_fluxo_publico_completo(self):
        desligamento = Desligamento.objects.create(
            colaborador=self.colaborador, formulario=self.formulario, iniciativa='EMPRESA',
            status=Desligamento.Status.AGUARDANDO_RESPOSTA, criado_por=self.staff, data_envio=timezone.now(),
        )
        client = Client()

        # Enquanto o colaborador segue ativo, a validação deve recusar o CPF.
        response = client.post(
            reverse('desligamentos:responder', args=[self.formulario.link_uuid]), {'documento': '52998224725'},
        )
        self.assertContains(response, 'não encontrado', status_code=200)

        self.colaborador.ativo = False
        self.colaborador.save(update_fields=['ativo'])

        response = client.post(
            reverse('desligamentos:responder', args=[self.formulario.link_uuid]), {'documento': '52998224725'},
        )
        self.assertRedirects(response, reverse('desligamentos:responder_etapa', args=[self.formulario.link_uuid, 1]))

        respostas = {
            1: {'valor': 'Recebi outra proposta.'},
            2: {'valor': '8'},
            3: {'valor': str(self.opcao_a.pk)},
            4: {'valor': [str(self.opcao_c.pk), str(self.opcao_d.pk)]},
        }
        for etapa in range(1, 5):
            url = reverse('desligamentos:responder_etapa', args=[self.formulario.link_uuid, etapa])
            response = client.post(url, respostas[etapa])
            if etapa < 4:
                self.assertRedirects(
                    response, reverse('desligamentos:responder_etapa', args=[self.formulario.link_uuid, etapa + 1]),
                )
            else:
                self.assertRedirects(response, reverse('desligamentos:agradecimento', args=[self.formulario.link_uuid]))

        desligamento.refresh_from_db()
        self.assertEqual(desligamento.status, Desligamento.Status.RESPONDIDO)
        self.assertIsNotNone(desligamento.data_resposta)

        detalhe_checkbox = DetalheRespostaDesligamento.objects.get(
            resposta__desligamento=desligamento, pergunta=self.pergunta_checkbox,
        )
        self.assertEqual(set(detalhe_checkbox.opcoes.values_list('pk', flat=True)), {self.opcao_c.pk, self.opcao_d.pk})

        detalhe_radio = DetalheRespostaDesligamento.objects.get(
            resposta__desligamento=desligamento, pergunta=self.pergunta_radio,
        )
        self.assertEqual(list(detalhe_radio.opcoes.values_list('pk', flat=True)), [self.opcao_a.pk])

    def test_reativar_colaborador_cancela_desligamento_aberto(self):
        Desligamento.objects.create(
            colaborador=self.colaborador, formulario=self.formulario, iniciativa='EMPRESA',
            status=Desligamento.Status.PENDENTE, criado_por=self.staff,
        )
        self.colaborador.ativo = False
        self.colaborador.save(update_fields=['ativo'])

        client = Client()
        client.force_login(self.staff)
        client.post(reverse('pesquisas:colaborador_status', args=[self.colaborador.pk]), {})
        self.colaborador.refresh_from_db()
        self.assertTrue(self.colaborador.ativo)
        self.assertFalse(Desligamento.objects.filter(colaborador=self.colaborador).exists())


class AvisoEnvioFormularioTests(TestCase):
    """Ao inativar, o RH é avisado no sininho para enviar o formulário de desligamento."""

    def setUp(self):
        self.rh = criar_usuario('rh.desligamento', modulos=['desligamentos'])
        self.sem_acesso = criar_usuario('so.enps', modulos=['enps'])
        self.empresa = Empresa.objects.create(nome='Empresa Teste')
        self.colaborador = Colaborador.objects.create(nome='Fulano de Tal', documento='52998224725', empresas=[self.empresa])
        self.formulario = FormularioDesligamento.objects.create(
            nome='Desligamento - Empresa', iniciativa=FormularioDesligamento.Iniciativa.EMPRESA, criado_por=self.rh,
        )
        self.client.force_login(self.rh)

    def avisos(self, usuario):
        return list(Notificacao.objects.filter(destinatario=usuario, lida=False).values_list('mensagem', flat=True))

    def inativar(self):
        self.client.post(
            reverse('pesquisas:colaborador_status', args=[self.colaborador.pk]),
            {'iniciativa': 'EMPRESA', 'formulario_id': self.formulario.pk, 'data_demissao': '2026-10-01'},
        )
        return Desligamento.objects.get(colaborador=self.colaborador)

    def test_inativar_avisa_quem_acessa_desligamentos(self):
        desligamento = self.inativar()
        self.assertEqual(
            self.avisos(self.rh), ['Enviar o formulário de desligamento "Desligamento - Empresa" para Fulano de Tal.'],
        )
        self.assertEqual(self.avisos(self.sem_acesso), [])
        aviso = Notificacao.objects.get(destinatario=self.rh)
        self.assertEqual(aviso.link, f"{reverse('desligamentos:kanban')}#desligamento-{desligamento.pk}")

    def test_marcar_como_enviado_encerra_o_aviso(self):
        desligamento = self.inativar()
        self.client.post(reverse('desligamentos:desligamento_marcar_enviado', args=[desligamento.pk]))
        self.assertEqual(self.avisos(self.rh), [])

    def test_reativar_encerra_o_aviso(self):
        self.inativar()
        self.client.post(reverse('pesquisas:colaborador_status', args=[self.colaborador.pk]))
        self.colaborador.refresh_from_db()
        self.assertTrue(self.colaborador.ativo)
        self.assertEqual(self.avisos(self.rh), [])


class ModalInativacaoNaEdicaoTests(TestCase):
    def test_edicao_de_colaborador_ativo_traz_iniciativas_e_formularios(self):
        rh = criar_usuario('rh.modal', modulos=['geral', 'desligamentos'])
        empresa = Empresa.objects.create(nome='Empresa Teste')
        colaborador = Colaborador.objects.create(nome='Fulano', documento='52998224725', empresas=[empresa])
        FormularioDesligamento.objects.create(
            nome='Pedido de demissão', iniciativa=FormularioDesligamento.Iniciativa.COLABORADOR, criado_por=rh,
        )
        self.client.force_login(rh)
        resposta = self.client.get(reverse('pesquisas:colaborador_editar', args=[colaborador.pk]))
        self.assertContains(resposta, 'id="colaboradorForm"')
        self.assertContains(resposta, 'id="motivoModal"')
        self.assertContains(resposta, 'Iniciativa da empresa')
        self.assertContains(resposta, 'Iniciativa do colaborador')
        self.assertContains(resposta, 'Pedido de demissão')
