from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from pesquisas.models import Colaborador, Empresa
from pesquisas.test_utils import criar_usuario

from .models import Candidatura, DetalheRespostaCandidatura, PerguntaVaga, Vaga


class FluxoVagaTests(TestCase):
    def setUp(self):
        self.staff = criar_usuario('admin', password='senha-forte-123', administrador=True)
        self.empresa = Empresa.objects.create(nome='Empresa Teste')
        self.colaborador = Colaborador.objects.create(
            nome='Fulano de Tal', documento='52998224725', empresas=[self.empresa], ativo=True,
        )
        agora = timezone.now()
        self.vaga = Vaga.objects.create(
            titulo='Analista de RH',
            descricao='Vaga para atuar no time de RH.',
            data_abertura=agora - timezone.timedelta(days=1),
            data_encerramento=agora + timezone.timedelta(days=10),
            criado_por=self.staff,
        )
        self.pergunta_texto = PerguntaVaga.objects.create(
            vaga=self.vaga, texto_pergunta='Por que você quer essa vaga?',
            tipo_resposta=PerguntaVaga.TipoResposta.TEXTO, ordem=1,
        )
        self.pergunta_nota = PerguntaVaga.objects.create(
            vaga=self.vaga, texto_pergunta='Nota de autoavaliação',
            tipo_resposta=PerguntaVaga.TipoResposta.NOTA_0_10, ordem=2,
        )
        self.pergunta_radio = PerguntaVaga.objects.create(
            vaga=self.vaga, texto_pergunta='Disponibilidade',
            tipo_resposta=PerguntaVaga.TipoResposta.RADIO, ordem=3,
        )
        self.opcao_a = self.pergunta_radio.opcoes.create(texto_opcao='Imediata', ordem=1)
        self.opcao_b = self.pergunta_radio.opcoes.create(texto_opcao='30 dias', ordem=2)
        self.pergunta_checkbox = PerguntaVaga.objects.create(
            vaga=self.vaga, texto_pergunta='Competências',
            tipo_resposta=PerguntaVaga.TipoResposta.CHECKBOX, ordem=4,
        )
        self.opcao_c = self.pergunta_checkbox.opcoes.create(texto_opcao='Excel', ordem=1)
        self.opcao_d = self.pergunta_checkbox.opcoes.create(texto_opcao='Comunicação', ordem=2)

    def test_painel_interno_requer_login(self):
        client = Client()
        response = client.get(reverse('vagas:vaga_lista'))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response.url)

    def test_painel_interno_acessivel_para_staff(self):
        client = Client()
        client.force_login(self.staff)
        response = client.get(reverse('vagas:vaga_lista'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Analista de RH')

    def test_qrcode_gera_imagem_png(self):
        client = Client()
        client.force_login(self.staff)
        response = client.get(reverse('vagas:vaga_qrcode', args=[self.vaga.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')

    def test_link_publico_nao_exige_login(self):
        client = Client()
        response = client.get(reverse('vagas:candidatar', args=[self.vaga.link_uuid]))
        self.assertEqual(response.status_code, 200)

    def test_cpf_de_colaborador_inativo_e_recusado(self):
        self.colaborador.ativo = False
        self.colaborador.save(update_fields=['ativo'])
        client = Client()
        response = client.post(
            reverse('vagas:candidatar', args=[self.vaga.link_uuid]), {'documento': '52998224725'},
        )
        self.assertContains(response, 'não encontrado', status_code=200)

    def test_fluxo_publico_completo(self):
        client = Client()

        response = client.post(
            reverse('vagas:candidatar', args=[self.vaga.link_uuid]), {'documento': '52998224725'},
        )
        self.assertRedirects(response, reverse('vagas:submeter_candidatura', args=[self.vaga.link_uuid]))

        payload = {
            f'pergunta_{self.pergunta_texto.pk}': 'Quero crescer na área de RH.',
            f'pergunta_{self.pergunta_nota.pk}': '9',
            f'pergunta_{self.pergunta_radio.pk}': str(self.opcao_a.pk),
            f'pergunta_{self.pergunta_checkbox.pk}': [str(self.opcao_c.pk), str(self.opcao_d.pk)],
        }
        response = client.post(reverse('vagas:submeter_candidatura', args=[self.vaga.link_uuid]), payload)
        self.assertRedirects(response, reverse('vagas:agradecimento', args=[self.vaga.link_uuid]))

        candidatura = Candidatura.objects.get(vaga=self.vaga, colaborador=self.colaborador)

        detalhe_checkbox = DetalheRespostaCandidatura.objects.get(
            candidatura=candidatura, pergunta=self.pergunta_checkbox,
        )
        self.assertEqual(set(detalhe_checkbox.opcoes.values_list('pk', flat=True)), {self.opcao_c.pk, self.opcao_d.pk})

        detalhe_radio = DetalheRespostaCandidatura.objects.get(
            candidatura=candidatura, pergunta=self.pergunta_radio,
        )
        self.assertEqual(list(detalhe_radio.opcoes.values_list('pk', flat=True)), [self.opcao_a.pk])

        # Uma segunda tentativa de candidatura à mesma vaga deve ser recusada.
        response = client.post(
            reverse('vagas:candidatar', args=[self.vaga.link_uuid]), {'documento': '52998224725'},
        )
        self.assertContains(response, 'já se candidatou', status_code=200)

    def test_vaga_encerrada_bloqueia_acesso_publico(self):
        agora = timezone.now()
        self.vaga.data_abertura = agora - timezone.timedelta(days=10)
        self.vaga.data_encerramento = agora - timezone.timedelta(days=1)
        self.vaga.save(update_fields=['data_abertura', 'data_encerramento'])
        self.assertEqual(self.vaga.status, Vaga.Status.ENCERRADA)

        client = Client()
        response = client.post(
            reverse('vagas:candidatar', args=[self.vaga.link_uuid]), {'documento': '52998224725'},
        )
        self.assertContains(response, 'encerrado', status_code=200)
        self.assertNotContains(response, 'name="documento"')

    def test_vaga_agendada_bloqueia_acesso_publico(self):
        agora = timezone.now()
        self.vaga.data_abertura = agora + timezone.timedelta(days=1)
        self.vaga.data_encerramento = agora + timezone.timedelta(days=10)
        self.vaga.save(update_fields=['data_abertura', 'data_encerramento'])
        self.assertEqual(self.vaga.status, Vaga.Status.AGENDADA)

        client = Client()
        response = client.post(
            reverse('vagas:candidatar', args=[self.vaga.link_uuid]), {'documento': '52998224725'},
        )
        self.assertContains(response, 'ainda não começaram', status_code=200)
        self.assertNotContains(response, 'name="documento"')
