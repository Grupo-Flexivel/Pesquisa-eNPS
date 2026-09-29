"""Testes das melhorias de negócio: upsert de currículos, inativação com auditoria e
rastreabilidade de reprovação."""
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from pesquisas.test_utils import criar_usuario

from .fluxo import inativar_candidato, mover_candidato, reativar_candidato
from .models import Candidato, HistoricoCandidatura
from .pipeline import processar_candidato
from .tests import CONFIG, DIMENSOES, PERFIL, TEXTO_CV, llm_falso, mock_triagem_automatica, pdf_com_texto

Evento = HistoricoCandidatura.Evento


def _stub(hash_arquivo, texto=TEXTO_CV):
    return Candidato.objects.create(
        curriculo=SimpleUploadedFile(f'{hash_arquivo}.pdf', pdf_com_texto(texto)),
        nome_arquivo=f'{hash_arquivo}.pdf', hash_arquivo=hash_arquivo,
    )


@override_settings(**CONFIG)
class UpsertCurriculoTests(TestCase):
    """Requisito 1: não duplicar candidato já cadastrado (e-mail, telefone ou nome)."""

    def setUp(self):
        self.analista = criar_usuario('rh', modulos=['talentos.curriculos'])

    def _processar(self, candidato, perfil):
        # Sem cargo cadastrado neste teste: sempre "direcionado" no mock para o cadastro
        # permanecer ativo e o teste continuar focado em upsert, não em triagem/roteamento.
        with mock_triagem_automatica(direcionado=True), \
                llm_falso(perfil=perfil), mock.patch('talentos.pipeline.gerar_embedding', return_value=[0.5] * DIMENSOES):
            return processar_candidato(candidato, self.analista)

    def test_mesmo_email_atualiza_cadastro_existente(self):
        c1 = _stub('1' * 64)
        original = self._processar(c1, PERFIL)
        self.assertEqual(original.pk, c1.pk)

        c2 = _stub('2' * 64, TEXTO_CV + ' versão 2')
        resultado = self._processar(c2, PERFIL)  # mesmo e-mail/telefone/nome

        self.assertEqual(resultado.pk, c1.pk)  # fundiu no cadastro original, não criou outro
        self.assertEqual(Candidato.objects.count(), 1)
        self.assertFalse(Candidato.all_objects.filter(pk=c2.pk).exists())  # stub descartado
        self.assertEqual(
            list(resultado.historico.order_by('pk').values_list('evento', flat=True))[-2:],
            [Evento.CURRICULO_ATUALIZADO, Evento.PROCESSAMENTO_CONCLUIDO],
        )

    def test_telefone_com_formatacao_diferente_e_reconhecido(self):
        c1 = _stub('3' * 64)
        original = self._processar(c1, PERFIL)

        perfil2 = dict(PERFIL, nome='Outra Pessoa', email='outra@exemplo.com', telefone='(11) 99999-9999')
        c2 = _stub('4' * 64, 'outro currículo qualquer')
        resultado = self._processar(c2, perfil2)

        self.assertEqual(resultado.pk, original.pk)
        self.assertEqual(resultado.nome, 'Outra Pessoa')  # dados atualizados com a versão mais nova

    def test_mesmo_nome_sem_email_ou_telefone_e_reconhecido(self):
        perfil_sem_contato = dict(PERFIL, email='', telefone='')
        c1 = _stub('5' * 64)
        original = self._processar(c1, perfil_sem_contato)

        c2 = _stub('6' * 64, 'outro currículo qualquer')
        resultado = self._processar(c2, dict(perfil_sem_contato, resumo='Resumo atualizado.'))

        self.assertEqual(resultado.pk, original.pk)
        self.assertEqual(resultado.resumo, 'Resumo atualizado.')

    def test_candidatos_diferentes_nao_sao_fundidos(self):
        c1 = _stub('7' * 64)
        self._processar(c1, PERFIL)

        perfil_diferente = {**PERFIL, 'nome': 'João Silva', 'email': 'joao@exemplo.com', 'telefone': '11888887777'}
        c2 = _stub('8' * 64, 'currículo completamente diferente')
        resultado = self._processar(c2, perfil_diferente)

        self.assertNotEqual(resultado.pk, c1.pk)
        self.assertEqual(Candidato.objects.count(), 2)


@override_settings(**CONFIG)
class InativacaoTests(TestCase):
    """Requisito 2: inativação com auditoria e validação estrita."""

    def setUp(self):
        self.analista = criar_usuario('rh', modulos=['talentos.curriculos'])
        self.candidato = _stub('a' * 64)

    def test_inativar_sem_motivo_levanta_validation_error(self):
        with self.assertRaises(ValidationError):
            self.candidato.inativar('   ', self.analista)
        self.candidato.refresh_from_db()
        self.assertTrue(self.candidato.ativo)

    def test_inativar_com_motivo_grava_auditoria_e_historico(self):
        inativar_candidato(self.candidato, 'Desistiu do processo seletivo.', self.analista)
        candidato = Candidato.all_objects.get(pk=self.candidato.pk)

        self.assertFalse(candidato.ativo)
        self.assertEqual(candidato.motivo_inativacao, 'Desistiu do processo seletivo.')
        self.assertEqual(candidato.inativado_por_id, self.analista.pk)
        self.assertIsNotNone(candidato.inativado_em)
        registro = candidato.historico.get(evento=Evento.CANDIDATO_INATIVADO)
        self.assertEqual(registro.descricao, 'Desistiu do processo seletivo.')
        self.assertEqual(registro.usuario, self.analista)

    def test_manager_padrao_esconde_inativos_alternativo_mostra(self):
        inativar_candidato(self.candidato, 'motivo', self.analista)
        self.assertFalse(Candidato.objects.filter(pk=self.candidato.pk).exists())
        self.assertTrue(Candidato.all_objects.filter(pk=self.candidato.pk).exists())

    def test_reativar_limpa_a_auditoria_e_registra_evento(self):
        inativar_candidato(self.candidato, 'motivo', self.analista)
        candidato = Candidato.all_objects.get(pk=self.candidato.pk)
        reativar_candidato(candidato, self.analista)
        candidato.refresh_from_db()

        self.assertTrue(candidato.ativo)
        self.assertEqual(candidato.motivo_inativacao, '')
        self.assertIsNone(candidato.inativado_por_id)
        self.assertIsNone(candidato.inativado_em)
        self.assertTrue(candidato.historico.filter(evento=Evento.CANDIDATO_REATIVADO).exists())

    def test_campos_de_auditoria_sao_imutaveis_fora_de_inativar_e_reativar(self):
        inativar_candidato(self.candidato, 'motivo', self.analista)
        candidato = Candidato.all_objects.get(pk=self.candidato.pk)
        candidato.ativo = True  # tentativa de burlar a inativação sem passar por reativar()
        with self.assertRaises(ValidationError):
            candidato.save()


@override_settings(**CONFIG)
class InativacaoViewsTests(TestCase):
    def setUp(self):
        self.analista = criar_usuario('rh', modulos=['talentos.curriculos'])
        self.client.force_login(self.analista)
        self.candidato = _stub('b' * 64)

    def test_inativar_sem_motivo_mostra_erro_amigavel(self):
        resposta = self.client.post(reverse('talentos:inativar', args=[self.candidato.pk]), {'motivo_inativacao': ''})
        self.assertRedirects(resposta, reverse('talentos:detalhe', args=[self.candidato.pk]))
        pagina = self.client.get(reverse('talentos:detalhe', args=[self.candidato.pk]))
        self.assertContains(pagina, 'Informe o motivo da inativação')
        self.candidato.refresh_from_db()
        self.assertTrue(self.candidato.ativo)

    def test_inativar_e_reativar_pela_tela(self):
        self.client.post(reverse('talentos:inativar', args=[self.candidato.pk]), {'motivo_inativacao': 'Sem aderência à vaga.'})
        candidato = Candidato.all_objects.get(pk=self.candidato.pk)
        self.assertFalse(candidato.ativo)

        resposta_detalhe = self.client.get(reverse('talentos:detalhe', args=[self.candidato.pk]))
        self.assertContains(resposta_detalhe, 'Candidato inativo')
        self.assertContains(resposta_detalhe, 'Sem aderência à vaga.')

        self.client.post(reverse('talentos:reativar', args=[self.candidato.pk]))
        candidato.refresh_from_db()
        self.assertTrue(candidato.ativo)


@override_settings(**CONFIG)
class ReprovacaoTests(TestCase):
    """Requisito 3: reprovação registra marco de auditoria, independente de inativação."""

    def setUp(self):
        self.analista = criar_usuario('rh', administrador=True)
        self.candidato = _stub('c' * 64)

    def eventos(self):
        return list(self.candidato.historico.order_by('pk').values_list('evento', flat=True))

    def test_mover_para_reprovado_registra_etapa_e_marco_de_reprovacao(self):
        mover_candidato(self.candidato, 'REPROVADO', self.analista)
        self.assertEqual(self.eventos(), [Evento.ETAPA_ALTERADA, Evento.REPROVACAO_REGISTRADA])

    def test_reprovacao_nao_inativa_automaticamente(self):
        mover_candidato(self.candidato, 'REPROVADO', self.analista)
        candidato = Candidato.all_objects.get(pk=self.candidato.pk)
        self.assertTrue(candidato.ativo)  # inativar é decisão separada do analista

    def test_mover_para_outra_etapa_nao_gera_marco_de_reprovacao(self):
        mover_candidato(self.candidato, 'ENTREVISTA_AGENDADA', self.analista)
        self.assertEqual(self.eventos(), [Evento.ETAPA_ALTERADA])


@override_settings(**CONFIG)
class BuscaSituacaoTests(TestCase):
    """Requisito 2 (tela de busca): filtro de status ativo/inativo/todos."""

    def setUp(self):
        self.analista = criar_usuario('rh', modulos=['talentos.busca'])
        self.client.force_login(self.analista)
        self.ativo = _stub('d' * 64)
        self.ativo.processamento = Candidato.Processamento.CONCLUIDO
        self.ativo.save()
        self.inativo = _stub('e' * 64)
        self.inativo.processamento = Candidato.Processamento.CONCLUIDO
        self.inativo.save()
        inativar_candidato(self.inativo, 'motivo', self.analista)

    def test_padrao_mostra_so_ativos(self):
        from .busca import filtrar
        resultado = list(filtrar())
        self.assertIn(self.ativo, resultado)
        self.assertNotIn(self.inativo, resultado)

    def test_situacao_inativos(self):
        from .busca import filtrar
        resultado = list(filtrar(situacao='inativos'))
        self.assertEqual(resultado, [self.inativo])

    def test_situacao_todos(self):
        from .busca import filtrar
        resultado = list(filtrar(situacao='todos'))
        self.assertEqual(set(resultado), {self.ativo, self.inativo})
