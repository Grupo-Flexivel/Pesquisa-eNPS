from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from pesquisas.models import Colaborador, Empresa, User
from pesquisas.test_utils import criar_usuario

from .models import Desligamento, DetalheRespostaDesligamento, FormularioDesligamento, PerguntaDesligamento


class FluxoDesligamentoTests(TestCase):
    def setUp(self):
        self.staff = criar_usuario('admin', password='senha-forte-123', administrador=True)
        self.empresa = Empresa.objects.create(nome='Empresa Teste')
        self.colaborador = Colaborador.objects.create(
            nome='Fulano de Tal', documento='52998224725', empresa=self.empresa, ativo=True,
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
            {'iniciativa': 'EMPRESA', 'formulario_id': self.formulario.pk},
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
