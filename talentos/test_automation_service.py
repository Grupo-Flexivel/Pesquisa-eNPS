"""Testes do serviço de triagem automática por busca semântica (CurriculumAutomationService).

Só rodam com PostgreSQL + pgvector no banco 'vector_db' (mesma exigência de
BuscaVetorialPostgresTests em test_modulo3.py), pois exercitam CosineDistance de verdade.
"""
import unittest
from unittest import mock

from django.db import connections
from django.test import TestCase, override_settings

from organizacao.models import Cargo, Departamento, Secao
from pesquisas.test_utils import criar_usuario

from .erros import ErroProcessamento
from .historico import Evento
from .models import CargoEmbedding, Candidato
from .services import CurriculumAutomationService
from .test_modulo3 import _candidato, vetor
from .tests import CONFIG


@unittest.skipUnless(connections['vector_db'].vendor == 'postgresql', 'Requer PostgreSQL com pgvector')
@override_settings(**CONFIG, CURRICULO_MATCH_THRESHOLD=0.75)
class CurriculumAutomationServiceTests(TestCase):
    def setUp(self):
        self.analista = criar_usuario('rh', modulos=['talentos.curriculos'])
        dep = Departamento.objects.create(nome='Tecnologia')
        secao = Secao.objects.create(departamento=dep, nome='Dev')
        self.front = Cargo.objects.create(secao=secao, nome='Front-end')
        self.back = Cargo.objects.create(secao=secao, nome='Back-end')
        CargoEmbedding.objects.create(cargo_id=self.front.pk, embedding=vetor(1), texto_origem='front')
        CargoEmbedding.objects.create(cargo_id=self.back.pk, embedding=vetor(0, 1), texto_origem='back')
        self.servico = CurriculumAutomationService()

    def test_direciona_automaticamente_quando_similaridade_atinge_o_limiar(self):
        candidato = _candidato('a', embedding=vetor(1))
        resultado = self.servico.triar(candidato, self.analista)

        self.assertTrue(resultado.direcionado)
        self.assertEqual(resultado.cargo_id, self.front.pk)
        candidato.refresh_from_db()
        self.assertEqual(candidato.cargo_id, self.front.pk)
        self.assertEqual(candidato.cargo_sugerido_id, self.front.pk)
        self.assertEqual(candidato.status, Candidato.Status.TRIAGEM)
        self.assertTrue(candidato.ativo)
        self.assertTrue(candidato.historico.filter(evento=Evento.CARGO_DEFINIDO).exists())

    def test_envia_para_pendencia_quando_abaixo_do_limiar(self):
        # ~45° de ambos os cargos cadastrados: similaridade de cosseno ≈ 0.71, abaixo do limiar (0.75).
        candidato = _candidato('b', embedding=vetor(0.5, 0.5))
        resultado = self.servico.triar(candidato, self.analista)

        self.assertFalse(resultado.direcionado)
        candidato.refresh_from_db()
        self.assertIsNone(candidato.cargo_id)
        self.assertEqual(candidato.status, Candidato.Status.PENDENCIA)
        self.assertFalse(candidato.ativo)
        self.assertIn('Aguardando definição manual', candidato.motivo_inativacao)
        self.assertIn(candidato.cargo_sugerido.nome, candidato.justificativa_cargo)  # diagnóstico: qual cargo chegou mais perto
        self.assertTrue(candidato.historico.filter(evento=Evento.CANDIDATO_INATIVADO).exists())

    def test_nao_forca_cargo_incorreto_mas_guarda_sugestao_de_referencia(self):
        candidato = _candidato('g', embedding=vetor(0.5, 0.5))
        resultado = self.servico.triar(candidato, self.analista)

        candidato.refresh_from_db()
        self.assertIsNone(candidato.cargo_id)  # nunca força
        self.assertIsNotNone(candidato.cargo_sugerido_id)  # mas guarda o mais próximo, para o RH
        self.assertEqual(candidato.cargo_sugerido_id, resultado.cargo_id)

    def test_sem_cargo_ativo_para_comparar_vai_para_pendencia(self):
        self.front.ativo = False
        self.front.save()
        self.back.ativo = False
        self.back.save()
        candidato = _candidato('c', embedding=vetor(1))
        resultado = self.servico.triar(candidato, self.analista)

        self.assertFalse(resultado.direcionado)
        self.assertIsNone(resultado.cargo_id)
        self.assertIsNone(resultado.similaridade)
        candidato.refresh_from_db()
        self.assertEqual(candidato.status, Candidato.Status.PENDENCIA)
        self.assertFalse(candidato.ativo)

    def test_reprocessamento_reativa_ao_encontrar_cargo_compativel(self):
        candidato = _candidato('d', embedding=vetor(0.5, 0.5))
        self.servico.triar(candidato, self.analista)  # cai em Pendência/inativo
        candidato.refresh_from_db()
        self.assertFalse(candidato.ativo)

        candidato.embedding = vetor(1)  # currículo reprocessado com um perfil mais claro
        candidato.save(update_fields=['embedding'])
        candidato = Candidato.all_objects.get(pk=candidato.pk)
        resultado = self.servico.triar(candidato, self.analista)

        self.assertTrue(resultado.direcionado)
        candidato.refresh_from_db()
        self.assertTrue(candidato.ativo)
        self.assertEqual(candidato.cargo_id, self.front.pk)

    def test_gera_embedding_de_cargo_novo_sob_demanda(self):
        novo_cargo = Cargo.objects.create(secao=self.front.secao, nome='QA')
        self.assertFalse(CargoEmbedding.objects.filter(cargo_id=novo_cargo.pk).exists())

        with mock.patch('talentos.services.gerar_embedding', return_value=vetor(0, 0, 1)) as gerar:
            candidato = _candidato('e', embedding=vetor(0, 0, 1))
            resultado = self.servico.triar(candidato, self.analista)

        gerar.assert_called_once()  # front/back já tinham embedding; só o cargo novo precisou gerar
        self.assertEqual(resultado.cargo_id, novo_cargo.pk)
        self.assertTrue(CargoEmbedding.objects.filter(cargo_id=novo_cargo.pk).exists())

    def test_sincronizar_embeddings_cargos_atualiza_todos_os_ativos(self):
        with mock.patch('talentos.services.gerar_embedding', return_value=vetor(9)):
            total = self.servico.sincronizar_embeddings_cargos()
        self.assertEqual(total, 2)  # front + back (ativos)
        for embedding in CargoEmbedding.objects.values_list('embedding', flat=True):
            self.assertEqual(list(embedding), vetor(9))

    def test_triar_exige_candidato_salvo_e_com_embedding(self):
        candidato = _candidato('f')  # sem embedding
        with self.assertRaisesMessage(ErroProcessamento, 'embedding'):
            self.servico.triar(candidato, self.analista)

        candidato_novo = Candidato(nome_arquivo='x.pdf', hash_arquivo='z' * 64, embedding=vetor(1))
        with self.assertRaisesMessage(ErroProcessamento, 'salvo'):
            self.servico.triar(candidato_novo, self.analista)
