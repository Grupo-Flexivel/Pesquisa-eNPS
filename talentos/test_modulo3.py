"""Testes do Módulo 3: filtros/busca, dashboard e Kanban."""
import unittest
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from organizacao.models import Cargo, Departamento, Secao
from pesquisas.models import User
from pesquisas.test_utils import criar_usuario

from .erros import ErroProcessamento
from .models import Candidato
from .tests import CONFIG, DIMENSOES, TEXTO_CV, pdf_com_texto


def vetor(*inicio):
    """Vetor com as primeiras componentes dadas e o restante zerado (dimensão real da coluna)."""
    return list(inicio) + [0] * (DIMENSOES - len(inicio))


def _candidato(hash_arquivo, **campos):
    campos.setdefault('processamento', Candidato.Processamento.CONCLUIDO)
    # `cargo`/`cargo_sugerido` são @property somente-leitura (resolvem no banco 'default');
    # o campo de verdade, gravável, é o `_id` cru — ver Candidato.cargo/cargo_sugerido.
    if 'cargo' in campos:
        campos['cargo_id'] = campos.pop('cargo').pk
    if 'cargo_sugerido' in campos:
        campos['cargo_sugerido_id'] = campos.pop('cargo_sugerido').pk
    return Candidato.objects.create(
        curriculo=SimpleUploadedFile('cv.pdf', pdf_com_texto(TEXTO_CV)), nome_arquivo='cv.pdf',
        hash_arquivo=hash_arquivo.ljust(64, '0'), **campos,
    )


@override_settings(**CONFIG)
class FiltroBuscaTests(TestCase):
    def setUp(self):
        self.dep_ti = Departamento.objects.create(nome='Tecnologia')
        self.dep_rh = Departamento.objects.create(nome='RH')
        self.dev = Secao.objects.create(departamento=self.dep_ti, nome='Dev')
        recrut = Secao.objects.create(departamento=self.dep_rh, nome='Recrutamento')
        self.front = Cargo.objects.create(secao=self.dev, nome='Front-end')
        self.analista = Cargo.objects.create(secao=recrut, nome='Analista')

        _candidato('a', nome='Ana', cidade='São Paulo', bairro='Mooca', cargo_sugerido=self.front)
        _candidato('b', nome='Bia', cidade='Campinas', cargo_sugerido=self.front, cargo=self.analista)
        _candidato('c', nome='Caio', cidade='São Paulo', cargo_sugerido=self.analista, status=Candidato.Status.REPROVADO)
        _candidato('d', nome='Erro', cidade='São Paulo', processamento=Candidato.Processamento.ERRO)

    def nomes(self, **filtros):
        from .busca import filtrar
        return sorted(c.nome for c in filtrar(**filtros))

    def test_sem_filtros_ignora_curriculos_com_erro(self):
        self.assertEqual(self.nomes(), ['Ana', 'Bia', 'Caio'])

    def test_localidade(self):
        self.assertEqual(self.nomes(cidade='são paulo'), ['Ana', 'Caio'])
        self.assertEqual(self.nomes(cidade='paulo', bairro='moo'), ['Ana'])

    def test_estrutura_usa_cargo_efetivo(self):
        # Bia foi sugerida para Front-end, mas o analista definiu Analista: vale o definido.
        self.assertEqual(self.nomes(cargo=self.front), ['Ana'])
        self.assertEqual(self.nomes(cargo=self.analista), ['Bia', 'Caio'])
        self.assertEqual(self.nomes(secao=self.dev), ['Ana'])
        self.assertEqual(self.nomes(departamento=self.dep_rh), ['Bia', 'Caio'])
        self.assertEqual(self.nomes(departamento=self.dep_ti), ['Ana'])

    def test_status_e_combinacao(self):
        self.assertEqual(self.nomes(status='REPROVADO'), ['Caio'])
        self.assertEqual(self.nomes(cidade='paulo', departamento=self.dep_rh), ['Caio'])

    def test_sem_texto_lista_pelos_filtros_sem_chamar_embedding(self):
        from .busca import buscar
        with mock.patch('talentos.busca.gerar_embedding') as gerar:
            resultado = buscar(texto='  ', cidade='campinas')
        gerar.assert_not_called()
        self.assertEqual([c.nome for c in resultado], ['Bia'])


@override_settings(**CONFIG)
class PaineisViewsTests(TestCase):
    def setUp(self):
        self.user = criar_usuario('rh', administrador=True)
        self.client.force_login(self.user)
        dep = Departamento.objects.create(nome='Tecnologia')
        self.cargo = Cargo.objects.create(secao=Secao.objects.create(departamento=dep, nome='Dev'), nome='Front-end')
        self.ana = _candidato('a', nome='Ana', cidade='São Paulo', cargo_sugerido=self.cargo)
        self.bia = _candidato('b', nome='Bia', cidade='São Paulo', status=Candidato.Status.CONTRATADO)

    def test_painel(self):
        resposta = self.client.get(reverse('talentos:painel'))
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.context['total'], 2)
        self.assertEqual(resposta.context['em_andamento'], 1)
        self.assertEqual(resposta.context['contratados'], 1)
        self.assertEqual(resposta.context['por_cidade'], [{'cidade': 'São Paulo', 'total': 2}])
        departamentos = {d['departamento']: d['total'] for d in resposta.context['por_departamento']}
        self.assertEqual(departamentos, {'Tecnologia': 1, None: 1})

    def test_painel_vazio(self):
        Candidato.objects.all().delete()
        self.assertEqual(self.client.get(reverse('talentos:painel')).status_code, 200)

    def test_quadro_tem_uma_coluna_por_etapa(self):
        resposta = self.client.get(reverse('talentos:quadro'))
        colunas = {c['valor']: c for c in resposta.context['colunas']}
        self.assertEqual(list(colunas), Candidato.Status.values)
        self.assertEqual([c.pk for c in colunas['TRIAGEM']['cartoes']], [self.ana.pk])
        self.assertEqual(colunas['CONTRATADO']['total'], 1)
        self.assertContains(resposta, 'data-id="%d"' % self.ana.pk)
        self.assertContains(resposta, reverse('talentos:pdf', args=[self.ana.pk]))  # link rápido para o PDF

    def test_quadro_filtra_por_termo(self):
        resposta = self.client.get(reverse('talentos:quadro'), {'q': 'bia'})
        self.assertEqual(sum(c['total'] for c in resposta.context['colunas']), 1)

    def test_quadro_mostra_pendencia_mesmo_inativa(self):
        # Pendência é sempre inativa (a IA não conseguiu direcionar um cargo com confiança);
        # se essa coluna só mostrasse ativos, o RH nunca veria o que precisa revisar.
        pendente = _candidato('p', nome='Pendente', status=Candidato.Status.PENDENCIA, ativo=False)
        resposta = self.client.get(reverse('talentos:quadro'))
        colunas = {c['valor']: c for c in resposta.context['colunas']}
        self.assertEqual([c.pk for c in colunas['PENDENCIA']['cartoes']], [pendente.pk])
        self.assertContains(resposta, 'Inativo')  # selo indicando que aguarda aprovação

    def test_mover_via_ajax(self):
        url = reverse('talentos:mover', args=[self.ana.pk])
        resposta = self.client.post(url, {'status': 'TESTE_TECNICO'}, headers={'X-Requested-With': 'XMLHttpRequest'})
        self.assertEqual(resposta.status_code, 200)
        dados = resposta.json()
        self.assertTrue(dados['ok'] and dados['mudou'])
        self.assertEqual(dados['contagens'], {'TESTE_TECNICO': 1, 'CONTRATADO': 1})
        self.ana.refresh_from_db()
        self.assertEqual(self.ana.status, 'TESTE_TECNICO')
        # mesma etapa: sem mudança
        self.assertFalse(self.client.post(url, {'status': 'TESTE_TECNICO'}).json()['mudou'])

    def test_mover_rejeita_etapa_invalida_e_get(self):
        url = reverse('talentos:mover', args=[self.ana.pk])
        self.assertEqual(self.client.post(url, {'status': 'INEXISTENTE'}).status_code, 400)
        self.assertEqual(self.client.post(url, {}).status_code, 400)
        self.assertEqual(self.client.get(url).status_code, 405)
        self.ana.refresh_from_db()
        self.assertEqual(self.ana.status, 'TRIAGEM')

    def test_mover_bloqueia_candidato_pendente_inativo(self):
        pendente = _candidato('q', nome='Pendente', status=Candidato.Status.PENDENCIA, ativo=False)
        url = reverse('talentos:mover', args=[pendente.pk])
        resposta = self.client.post(url, {'status': 'TRIAGEM'})
        self.assertEqual(resposta.status_code, 400)
        self.assertFalse(resposta.json()['ok'])
        pendente.refresh_from_db()
        self.assertEqual(pendente.status, Candidato.Status.PENDENCIA)  # não deixa "pular" a aprovação

    def test_mover_exige_permissao_e_csrf(self):
        url = reverse('talentos:mover', args=[self.ana.pk])
        so_busca = criar_usuario('b', modulos=['talentos.busca'])
        self.client.force_login(so_busca)
        self.assertEqual(self.client.post(url, {'status': 'REPROVADO'}).status_code, 403)
        self.assertEqual(self.client.get(reverse('talentos:quadro')).status_code, 403)

        seguro = Client(enforce_csrf_checks=True)
        seguro.force_login(self.user)
        self.assertEqual(seguro.post(url, {'status': 'REPROVADO'}).status_code, 403)
        self.ana.refresh_from_db()
        self.assertEqual(self.ana.status, 'TRIAGEM')

    def test_busca_view(self):
        self.ana.resumo = 'Dev front-end'
        self.ana.similaridade = 87
        with mock.patch('talentos.views.buscar', return_value=[self.ana]) as buscar_falso:
            resposta = self.client.get(reverse('talentos:busca'), {'q': 'programador react', 'cidade': 'São Paulo'})
        self.assertContains(resposta, 'Ana')
        self.assertContains(resposta, '87%')
        kwargs = buscar_falso.call_args.kwargs
        self.assertEqual((kwargs['texto'], kwargs['cidade']), ('programador react', 'São Paulo'))

    def test_busca_trata_falha_do_embedding(self):
        with mock.patch('talentos.views.buscar', side_effect=ErroProcessamento('embeddings fora do ar')):
            resposta = self.client.get(reverse('talentos:busca'), {'q': 'react'})
        self.assertContains(resposta, 'embeddings fora do ar')

    def test_busca_sem_parametros_mostra_so_o_formulario(self):
        resposta = self.client.get(reverse('talentos:busca'))
        self.assertEqual(resposta.status_code, 200)
        self.assertIsNone(resposta.context['resultados'])

    def test_sidebar_mostra_itens_do_modulo_conforme_permissao(self):
        html = self.client.get(reverse('talentos:painel')).content.decode()
        for rotulo in ('Dashboard de talentos', 'Kanban de candidatos', 'Busca semântica', 'Enviar currículo'):
            self.assertIn(rotulo, html)
        restrito = criar_usuario('k', modulos=['talentos.kanban'])
        self.client.force_login(restrito)
        html = self.client.get(reverse('talentos:quadro')).content.decode()
        self.assertIn('Kanban de candidatos', html)
        self.assertNotIn('Busca semântica', html)
        self.assertNotIn('Dashboard de talentos', html)
        # quem só tem o Kanban é encaminhado a ele a partir do dashboard geral
        self.assertRedirects(self.client.get(reverse('pesquisas:dashboard')), reverse('talentos:quadro'))


@unittest.skipUnless(connection.vendor == 'postgresql', 'Requer PostgreSQL com pgvector')
@override_settings(**CONFIG)
class BuscaVetorialPostgresTests(TestCase):
    """Roda apenas em PostgreSQL: valida o ranking real por CosineDistance."""

    def test_ranking_por_cosseno(self):
        _candidato('p', nome='Perto', embedding=vetor(1))
        _candidato('l', nome='Longe', embedding=vetor(0, 1))
        _candidato('s', nome='SemVetor')
        from .busca import buscar
        with mock.patch('talentos.busca.gerar_embedding', return_value=vetor(1, 0.1)):
            resultado = buscar(texto='qualquer')
        self.assertEqual([c.nome for c in resultado], ['Perto', 'Longe'])
        self.assertGreater(resultado[0].similaridade, resultado[1].similaridade)
