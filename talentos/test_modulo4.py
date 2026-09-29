"""Testes do Módulo 4: rastreabilidade (HistoricoCandidatura)."""
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from organizacao.models import Cargo, Departamento, Secao
from pesquisas.models import User
from pesquisas.test_utils import criar_usuario

from .erros import ErroProcessamento
from .fluxo import mover_candidato
from .models import Candidato, HistoricoCandidatura
from .tests import CONFIG, DIMENSOES, PERFIL, TEXTO_CV, llm_falso, mock_triagem_automatica, pdf_com_texto

Evento = HistoricoCandidatura.Evento


def _candidato(**campos):
    campos.setdefault('processamento', Candidato.Processamento.CONCLUIDO)
    return Candidato.objects.create(
        curriculo=SimpleUploadedFile('cv.pdf', pdf_com_texto(TEXTO_CV)), nome_arquivo='cv.pdf',
        hash_arquivo='h' * 64, **campos,
    )


@override_settings(**CONFIG)
class HistoricoTests(TestCase):
    def setUp(self):
        self.analista = criar_usuario(
            'ana.rh', first_name='Ana', last_name='RH', administrador=True,
        )
        self.client.force_login(self.analista)
        dep = Departamento.objects.create(nome='Tecnologia')
        secao = Secao.objects.create(departamento=dep, nome='Dev')
        self.front = Cargo.objects.create(secao=secao, nome='Front-end')
        self.back = Cargo.objects.create(secao=secao, nome='Back-end')

    def eventos(self, candidato):
        return list(candidato.historico.order_by('pk').values_list('evento', flat=True))

    def test_upload_registra_envio_e_processamento_com_analista(self):
        with mock_triagem_automatica(direcionado=True, cargo_id=self.front.pk, similaridade=0.9), \
                llm_falso(), mock.patch('talentos.pipeline.gerar_embedding', return_value=[0.5] * DIMENSOES):
            self.client.post(reverse('talentos:upload'), {
                'curriculos': [SimpleUploadedFile('cv.pdf', pdf_com_texto(TEXTO_CV), 'application/pdf')],
            })
        candidato = Candidato.objects.get()
        self.assertEqual(self.eventos(candidato), [Evento.CURRICULO_ENVIADO, Evento.PROCESSAMENTO_CONCLUIDO])
        concluido = candidato.historico.get(evento=Evento.PROCESSAMENTO_CONCLUIDO)
        self.assertEqual((concluido.usuario, concluido.usuario_nome), (self.analista, 'Ana RH'))
        self.assertIn('Front-end', concluido.descricao)
        self.assertIsNotNone(concluido.data_hora)

    def test_falha_de_processamento_e_reprocessamento(self):
        with mock.patch('talentos.pipeline.extrair_perfil', side_effect=ErroProcessamento('IA fora')):
            self.client.post(reverse('talentos:upload'), {
                'curriculos': [SimpleUploadedFile('cv.pdf', pdf_com_texto(TEXTO_CV), 'application/pdf')],
            })
        candidato = Candidato.objects.get()
        self.assertEqual(self.eventos(candidato), [Evento.CURRICULO_ENVIADO, Evento.PROCESSAMENTO_ERRO])
        self.assertEqual(candidato.historico.get(evento=Evento.PROCESSAMENTO_ERRO).descricao, 'IA fora')

        with mock_triagem_automatica(direcionado=True, cargo_id=self.front.pk), \
                llm_falso(), mock.patch('talentos.pipeline.gerar_embedding', return_value=[0.5] * DIMENSOES):
            self.client.post(reverse('talentos:reprocessar', args=[candidato.pk]))
        self.assertEqual(self.eventos(candidato)[-2:], [Evento.REPROCESSAMENTO, Evento.PROCESSAMENTO_CONCLUIDO])
        self.assertEqual(candidato.historico.get(evento=Evento.REPROCESSAMENTO).usuario, self.analista)

    def test_mover_registra_etapas_e_quem_moveu(self):
        candidato = _candidato()
        self.assertTrue(mover_candidato(candidato, 'ENTREVISTA_AGENDADA', self.analista))
        registro = candidato.historico.get()
        self.assertEqual(registro.evento, Evento.ETAPA_ALTERADA)
        self.assertEqual((registro.status_anterior, registro.status_novo), ('TRIAGEM', 'ENTREVISTA_AGENDADA'))
        self.assertEqual(registro.usuario, self.analista)
        self.assertIn('Triagem', registro.descricao)
        self.assertIn('Entrevista agendada', registro.descricao)

    def test_mover_para_mesma_etapa_ou_invalida_nao_registra(self):
        candidato = _candidato()
        self.assertFalse(mover_candidato(candidato, 'TRIAGEM', self.analista))
        with self.assertRaises(ValueError):
            mover_candidato(candidato, 'XYZ', self.analista)
        self.assertFalse(candidato.historico.exists())

    def test_anterior_vem_do_banco_e_nao_da_instancia_desatualizada(self):
        candidato = _candidato()
        copia_antiga = Candidato.objects.get(pk=candidato.pk)  # outro analista tem a tela aberta
        mover_candidato(candidato, 'TESTE_TECNICO', self.analista)
        mover_candidato(copia_antiga, 'REPROVADO', self.analista)
        ultimo = candidato.historico.order_by('-pk').first()
        self.assertEqual((ultimo.status_anterior, ultimo.status_novo), ('TESTE_TECNICO', 'REPROVADO'))

    def test_kanban_ajax_registra_no_historico(self):
        candidato = _candidato()
        self.client.post(reverse('talentos:mover', args=[candidato.pk]), {'status': 'CONTRATADO'})
        self.assertEqual(candidato.historico.get().usuario, self.analista)

    def test_edicao_registra_cargo_e_campos_sem_expor_valores(self):
        candidato = _candidato(nome='Maria', telefone='11999999999', cargo=self.front)
        self.client.post(reverse('talentos:detalhe', args=[candidato.pk]), {
            'nome': 'Maria', 'email': '', 'telefone': '11888888888', 'cidade': 'Campinas', 'bairro': '',
            'cargo': self.back.pk,
        })
        eventos = {h.evento: h for h in candidato.historico.all()}
        self.assertIn('Front-end', eventos[Evento.CARGO_DEFINIDO].descricao)
        self.assertIn('Back-end', eventos[Evento.CARGO_DEFINIDO].descricao)
        dados = eventos[Evento.DADOS_EDITADOS].descricao
        self.assertIn('telefone', dados)
        self.assertIn('cidade', dados)
        self.assertNotIn('11888888888', dados)  # valores não vão para a trilha
        self.assertNotIn('Maria', dados)

    def test_salvar_sem_mudancas_nao_registra(self):
        candidato = _candidato(nome='Maria')
        self.client.post(reverse('talentos:detalhe', args=[candidato.pk]), {
            'nome': 'Maria', 'email': '', 'telefone': '', 'cidade': '', 'bairro': '', 'cargo': '',
        })
        self.assertFalse(candidato.historico.exists())

    def test_linha_do_tempo_na_tela_do_candidato(self):
        candidato = _candidato()
        mover_candidato(candidato, 'TESTE_TECNICO', self.analista)
        resposta = self.client.get(reverse('talentos:detalhe', args=[candidato.pk]))
        self.assertContains(resposta, 'Linha do tempo')
        self.assertContains(resposta, 'Etapa alterada')
        self.assertContains(resposta, 'Ana RH')

    def test_painel_mostra_atividade_recente(self):
        candidato = _candidato(nome='Maria')
        mover_candidato(candidato, 'REPROVADO', self.analista)
        self.assertContains(self.client.get(reverse('talentos:painel')), 'Atividade recente')

    def test_acao_do_sistema_fica_sem_analista(self):
        from .historico import registrar
        registro = registrar(_candidato(), Evento.PROCESSAMENTO_CONCLUIDO, None, 'x')
        self.assertEqual((registro.usuario, registro.usuario_nome), (None, ''))

    def test_registro_sobrevive_a_remocao_do_usuario(self):
        candidato = _candidato()
        mover_candidato(candidato, 'REPROVADO', self.analista)
        self.client.logout()
        self.analista.delete()
        registro = candidato.historico.get()
        self.assertIsNone(registro.usuario)
        self.assertEqual(registro.usuario_nome, 'Ana RH')

    def test_historico_e_imutavel(self):
        candidato = _candidato()
        mover_candidato(candidato, 'REPROVADO', self.analista)
        registro = candidato.historico.get()
        registro.descricao = 'adulterado'
        with self.assertRaises(PermissionError):
            registro.save()
        with self.assertRaises(PermissionError):
            registro.delete()
        registro.refresh_from_db()
        self.assertNotEqual(registro.descricao, 'adulterado')

    def test_excluir_candidato_apaga_o_historico_junto(self):
        candidato = _candidato()
        mover_candidato(candidato, 'REPROVADO', self.analista)
        self.client.post(reverse('talentos:excluir', args=[candidato.pk]))
        self.assertFalse(HistoricoCandidatura.objects.exists())

    def test_admin_e_somente_leitura(self):
        admin = criar_usuario('adm', administrador=True)
        self.client.force_login(admin)
        candidato = _candidato()
        mover_candidato(candidato, 'REPROVADO', admin)
        registro = candidato.historico.get()
        self.assertEqual(self.client.get('/admin/talentos/historicocandidatura/').status_code, 200)
        self.assertEqual(self.client.get('/admin/talentos/historicocandidatura/add/').status_code, 403)
        self.assertEqual(
            self.client.post(f'/admin/talentos/historicocandidatura/{registro.pk}/delete/', {'post': 'yes'}).status_code, 403,
        )
        self.assertTrue(HistoricoCandidatura.objects.filter(pk=registro.pk).exists())
