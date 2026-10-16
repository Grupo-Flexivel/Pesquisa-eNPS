from datetime import date, timedelta
from io import StringIO
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from notificacoes.models import Notificacao
from pesquisas.models import Colaborador, Empresa
from pesquisas.test_utils import criar_usuario

from . import regras
from .middleware import CHAVE_CACHE
from .models import FormularioAvaliacao, OpcaoPerguntaAvaliacao, PerguntaAvaliacao, PeriodoExperiencia, RespostaAvaliacao
from .sanitizacao import limpar_html
from .services import TransicaoInvalida, arquivar_periodo, atualizar_periodos, finalizar_beneficios, registrar_avaliacao


Status = PeriodoExperiencia.Status
Tipo = PerguntaAvaliacao.TipoResposta
HOJE = date(2026, 10, 1)


def contratado_ha(dias):
    """Data de contratação tal que hoje seja o (dias + 1)º dia do contrato."""
    return HOJE - timedelta(days=dias)


class RegrasTests(TestCase):
    def test_noventa_dias_corridos_contando_o_dia_da_contratacao(self):
        self.assertEqual(regras.fim_experiencia(date(2026, 1, 1)), date(2026, 3, 31))

    def test_limite_dos_15_dias(self):
        # 16 dias restantes → ainda dentro do prazo; 15 → vencendo.
        self.assertFalse(regras.em_alerta(HOJE + timedelta(days=16) - timedelta(days=89), HOJE))
        self.assertTrue(regras.em_alerta(HOJE + timedelta(days=15) - timedelta(days=89), HOJE))

    def test_periodo_termina_no_dia_seguinte_ao_ultimo_dia(self):
        self.assertFalse(regras.periodo_terminou(contratado_ha(89), HOJE))  # último dia
        self.assertTrue(regras.periodo_terminou(contratado_ha(90), HOJE))


class BaseExperienciaTests(TestCase):
    def setUp(self):
        self.empresa = Empresa.objects.create(nome='Matriz')
        self.rh = criar_usuario('rh', modulos=['experiencia'])
        self.gestor = criar_usuario('gestor', modulos=['enps'])  # login do gestor no painel
        self.documentos = iter([
            '52998224725', '11144477735', '12345678909', '39053344705', '12345678062', '98765432029',
        ])
        self.gestor_colaborador = Colaborador.objects.create(
            nome='Gestora', documento=next(self.documentos), empresas=[self.empresa],
            eh_gestor=True, usuario=self.gestor,
        )
        self.formulario = self.criar_formulario()

    def criar_formulario(self, titulo='Avaliação de experiência', padrao=True):
        formulario = FormularioAvaliacao.objects.create(
            titulo=titulo, descricao='<p>Avalie o <strong>colaborador</strong>.</p>', padrao=padrao, criado_por=self.rh,
        )
        perguntas = (
            ('Pontualidade', Tipo.NOTA_0_10, ()),
            ('Comportamento', Tipo.RADIO, ('Ótimo', 'Bom', 'Ruim')),
            ('Competências demonstradas', Tipo.CHECKBOX, ('Comunicação', 'Liderança', 'Organização')),
            ('Comentários do gestor', Tipo.TEXTO, ()),
        )
        for ordem, (texto, tipo, opcoes) in enumerate(perguntas, start=1):
            pergunta = PerguntaAvaliacao.objects.create(
                formulario=formulario, texto_pergunta=texto, tipo_resposta=tipo, ordem=ordem,
            )
            for ordem_opcao, texto_opcao in enumerate(opcoes, start=1):
                OpcaoPerguntaAvaliacao.objects.create(pergunta=pergunta, texto_opcao=texto_opcao, ordem=ordem_opcao)
        return formulario

    def respostas_validas(self, formulario=None):
        """{pergunta: valor} conforme o tipo de cada pergunta."""
        respostas = {}
        for pergunta in (formulario or self.formulario).perguntas.all():
            opcoes = list(pergunta.opcoes.all())
            respostas[pergunta] = {
                Tipo.TEXTO: 'Muito dedicado.',
                Tipo.NOTA_0_10: 9,
                Tipo.RADIO: opcoes[0] if opcoes else None,
                Tipo.CHECKBOX: opcoes[:2],
            }[pergunta.tipo_resposta]
        return respostas

    def criar_colaborador(self, data_contratacao, **extra):
        extra.setdefault('gestor', self.gestor_colaborador)
        return Colaborador.objects.create(
            nome=extra.pop('nome', 'Ana'), documento=next(self.documentos), empresas=[self.empresa],
            data_contratacao=data_contratacao, **extra,
        )

    def periodo(self, colaborador):
        return PeriodoExperiencia.objects.get(colaborador=colaborador)

    def notificacoes(self, usuario):
        return list(Notificacao.objects.filter(destinatario=usuario, lida=False).values_list('mensagem', flat=True))

    def avaliar(self, periodo, decisao, hoje=HOJE):
        """Gestor responde o formulário padrão com respostas válidas para cada tipo de pergunta."""
        return registrar_avaliacao(periodo, self.formulario, self.respostas_validas(), decisao, self.gestor, hoje=hoje)

    def dados_avaliacao(self, decisao):
        """POST do formulário do gestor (mesmas respostas de respostas_validas)."""
        dados = {}
        for pergunta, valor in self.respostas_validas().items():
            if pergunta.tipo_resposta == Tipo.CHECKBOX:
                valor = [opcao.pk for opcao in valor]
            elif pergunta.tipo_resposta == Tipo.RADIO:
                valor = valor.pk
            dados[f'pergunta_{pergunta.pk}'] = valor
        return {**dados, 'decisao': decisao, 'observacoes': 'Ótimo começo.'}


class RotinaAutomaticaTests(BaseExperienciaTests):
    def test_cria_card_dentro_do_prazo_sem_notificar(self):
        colaborador = self.criar_colaborador(contratado_ha(10))
        atualizar_periodos(HOJE)
        periodo = self.periodo(colaborador)
        self.assertEqual(periodo.status, Status.DENTRO_PRAZO)
        self.assertEqual(periodo.status_avaliacao, PeriodoExperiencia.StatusAvaliacao.PENDENTE)
        self.assertIsNotNone(periodo.token_avaliacao)  # link único gerado junto com o card
        self.assertFalse(Notificacao.objects.exists())

    def test_move_para_vencendo_e_notifica_o_gestor_uma_unica_vez(self):
        colaborador = self.criar_colaborador(contratado_ha(10))
        atualizar_periodos(HOJE)
        atualizar_periodos(HOJE + timedelta(days=64))  # faltam 15 dias
        atualizar_periodos(HOJE + timedelta(days=65))  # rotina roda de novo: não duplica

        periodo = self.periodo(colaborador)
        self.assertEqual(periodo.status, Status.VENCENDO)
        avisos = Notificacao.objects.filter(destinatario=self.gestor, lida=False)
        self.assertEqual(len(avisos), 1)
        self.assertIn('avaliação de experiência de Ana', avisos[0].mensagem)
        self.assertEqual(avisos[0].link, periodo.caminho_avaliacao)  # leva direto ao formulário
        # O RH acompanha a pendência, citando o gestor a ser cobrado.
        self.assertEqual(
            self.notificacoes(self.rh),
            ['Avaliação de experiência de Ana pendente com o gestor Gestora (termina em 19/12/2026).'],
        )

    def test_sem_gestor_o_rh_recebe_o_aviso_de_avaliacao(self):
        self.criar_colaborador(contratado_ha(80), gestor=None)
        atualizar_periodos(HOJE)
        self.assertEqual(len(self.notificacoes(self.rh)), 1)

    def test_periodo_terminado_sem_avaliacao_vai_direto_para_efetivado(self):
        colaborador = self.criar_colaborador(contratado_ha(80))
        atualizar_periodos(HOJE)
        atualizar_periodos(HOJE + timedelta(days=10))  # dia seguinte ao último dia

        periodo = self.periodo(colaborador)
        self.assertEqual(periodo.status, Status.EFETIVADO)
        self.assertTrue(periodo.efetivado_automaticamente)
        self.assertFalse(periodo.avaliacao_realizada)
        self.assertEqual(self.notificacoes(self.rh), ['Incluir benefícios para o colaborador Ana.'])
        self.assertEqual(self.notificacoes(self.gestor), [])  # aviso de avaliação resolvido

    def test_contratacoes_antigas_e_legados_nao_entram_no_kanban(self):
        self.criar_colaborador(contratado_ha(90))
        self.criar_colaborador(None)
        self.criar_colaborador(contratado_ha(5), ativo=False)
        atualizar_periodos(HOJE)
        self.assertFalse(PeriodoExperiencia.objects.exists())

    def test_colaborador_inativado_vai_para_desligado(self):
        colaborador = self.criar_colaborador(contratado_ha(10))
        atualizar_periodos(HOJE)
        colaborador.ativo = False
        colaborador.save()
        atualizar_periodos(HOJE)
        self.assertEqual(self.periodo(colaborador).status, Status.DESLIGADO)

    def test_comando_diario(self):
        self.criar_colaborador(contratado_ha(80))
        saida = StringIO()
        with mock.patch('django.utils.timezone.localdate', return_value=HOJE):
            call_command('atualizar_experiencia', stdout=saida)
        self.assertIn('criados: 1', saida.getvalue())
        self.assertIn('VENCENDO: 1', saida.getvalue())


class RegistroAvaliacaoTests(BaseExperienciaTests):
    def setUp(self):
        super().setUp()
        self.colaborador = self.criar_colaborador(contratado_ha(80))
        atualizar_periodos(HOJE)
        self.periodo_vencendo = self.periodo(self.colaborador)

    def test_efetivado_pela_avaliacao_notifica_beneficios_e_encerra_ao_finalizar(self):
        resposta = self.avaliar(self.periodo_vencendo, RespostaAvaliacao.Decisao.EFETIVADO)

        periodo = self.periodo(self.colaborador)
        self.assertEqual(periodo.status, Status.EFETIVADO)
        self.assertEqual(periodo.status_avaliacao, PeriodoExperiencia.StatusAvaliacao.RESPONDIDO)
        self.assertFalse(periodo.efetivado_automaticamente)
        self.assertEqual(resposta.detalhes.count(), 4)
        self.assertEqual(self.notificacoes(self.gestor), [])  # avaliação feita: aviso resolvido
        self.assertEqual(self.notificacoes(self.rh), ['Incluir benefícios para o colaborador Ana.'])

        finalizar_beneficios(periodo, self.rh)
        self.assertFalse(PeriodoExperiencia.objects.em_acompanhamento().exists())
        self.assertEqual(self.notificacoes(self.rh), [])

    def test_desligado_pela_avaliacao_avisa_o_rh_ate_a_inativacao(self):
        self.avaliar(self.periodo_vencendo, RespostaAvaliacao.Decisao.DESLIGADO)
        self.assertEqual(self.periodo(self.colaborador).status, Status.DESLIGADO)
        aviso = 'Gestora desligou Ana no período de experiência. Inative o colaborador para iniciar o desligamento.'
        self.assertEqual(self.notificacoes(self.rh), [aviso])

        with self.captureOnCommitCallbacks(execute=True):
            self.colaborador.ativo = False
            self.colaborador.save()
        self.assertEqual(self.notificacoes(self.rh), [])

    def test_decisao_obrigatoria_e_valida(self):
        with self.assertRaises(TransicaoInvalida):
            self.avaliar(self.periodo_vencendo, Status.DENTRO_PRAZO)
        self.assertFalse(RespostaAvaliacao.objects.exists())

    def test_todas_as_perguntas_precisam_de_resposta(self):
        respostas = self.respostas_validas()
        respostas.pop(next(iter(respostas)))
        with self.assertRaises(TransicaoInvalida):
            registrar_avaliacao(
                self.periodo_vencendo, self.formulario, respostas, RespostaAvaliacao.Decisao.EFETIVADO, self.gestor,
                hoje=HOJE,
            )

    def test_valor_incompativel_com_o_tipo_e_recusado(self):
        for tipo, invalido in ((Tipo.NOTA_0_10, 11), (Tipo.TEXTO, '   '), (Tipo.CHECKBOX, [])):
            respostas = self.respostas_validas()
            pergunta = next(p for p in respostas if p.tipo_resposta == tipo)
            respostas[pergunta] = invalido
            with self.assertRaises(TransicaoInvalida, msg=tipo):
                registrar_avaliacao(
                    self.periodo_vencendo, self.formulario, respostas, RespostaAvaliacao.Decisao.EFETIVADO,
                    self.gestor, hoje=HOJE,
                )

    def test_opcao_de_outra_pergunta_e_recusada(self):
        respostas = self.respostas_validas()
        radio = next(p for p in respostas if p.tipo_resposta == Tipo.RADIO)
        checkbox = next(p for p in respostas if p.tipo_resposta == Tipo.CHECKBOX)
        respostas[radio] = checkbox.opcoes.first()
        with self.assertRaises(TransicaoInvalida):
            registrar_avaliacao(
                self.periodo_vencendo, self.formulario, respostas, RespostaAvaliacao.Decisao.EFETIVADO, self.gestor,
                hoje=HOJE,
            )

    def test_cada_tipo_de_resposta_e_gravado(self):
        resposta = self.avaliar(self.periodo_vencendo, RespostaAvaliacao.Decisao.EFETIVADO)
        detalhes = {d.tipo_resposta: d for d in resposta.detalhes.all()}
        self.assertEqual(detalhes[Tipo.NOTA_0_10].valor_inteiro, 9)
        self.assertEqual(detalhes[Tipo.RADIO].opcoes_selecionadas, ['Ótimo'])
        self.assertEqual(detalhes[Tipo.CHECKBOX].opcoes_selecionadas, ['Comunicação', 'Liderança'])
        self.assertEqual(detalhes[Tipo.TEXTO].valor_texto, 'Muito dedicado.')

    def test_nao_responde_duas_vezes(self):
        self.avaliar(self.periodo_vencendo, RespostaAvaliacao.Decisao.EFETIVADO)
        with self.assertRaisesMessage(TransicaoInvalida, 'já foi respondida'):
            self.avaliar(self.periodo(self.colaborador), RespostaAvaliacao.Decisao.DESLIGADO)

    def test_so_responde_em_vencendo(self):
        colaborador = self.criar_colaborador(contratado_ha(10), nome='Bruno')
        atualizar_periodos(HOJE)
        with self.assertRaisesMessage(TransicaoInvalida, 'Vencendo'):
            self.avaliar(self.periodo(colaborador), RespostaAvaliacao.Decisao.EFETIVADO)

    def test_periodo_vencido_e_efetivado_mesmo_que_o_gestor_tente_desligar(self):
        with self.assertRaisesMessage(TransicaoInvalida, 'efetivado automaticamente'):
            self.avaliar(self.periodo_vencendo, RespostaAvaliacao.Decisao.DESLIGADO, hoje=HOJE + timedelta(days=10))
        self.assertEqual(self.periodo(self.colaborador).status, Status.EFETIVADO)

    def test_respostas_preservadas_ao_editar_o_formulario(self):
        resposta = self.avaliar(self.periodo_vencendo, RespostaAvaliacao.Decisao.EFETIVADO)
        self.formulario.perguntas.update(texto_pergunta='Pergunta alterada')
        OpcaoPerguntaAvaliacao.objects.filter(pergunta__formulario=self.formulario).delete()
        textos = list(resposta.detalhes.values_list('pergunta_texto', 'opcoes_selecionadas'))
        self.assertEqual(textos[1], ('Comportamento', ['Ótimo']))
        self.assertEqual(textos[2], ('Competências demonstradas', ['Comunicação', 'Liderança']))

    def test_beneficios_so_para_efetivados(self):
        with self.assertRaises(TransicaoInvalida):
            finalizar_beneficios(self.periodo_vencendo, self.rh)


@mock.patch('django.utils.timezone.localdate', return_value=HOJE)
class PreenchimentoPeloGestorTests(BaseExperienciaTests):
    def setUp(self):
        super().setUp()
        self.colaborador = self.criar_colaborador(contratado_ha(80))
        self.outro = self.criar_colaborador(contratado_ha(80), nome='Bruno', gestor=None)
        atualizar_periodos(HOJE)
        self.url = self.periodo(self.colaborador).caminho_avaliacao

    def test_gestor_abre_o_link_e_ve_titulo_descricao_perguntas_e_decisao(self, _):
        self.client.force_login(self.gestor)
        resposta = self.client.get(self.url)
        self.assertContains(resposta, 'Avaliação de experiência')
        self.assertContains(resposta, '<strong>colaborador</strong>', html=False)  # HTML do editor renderizado
        for texto in ('Pontualidade', 'Comportamento', 'Ótimo', 'Competências demonstradas', 'Liderança',
                      'Comentários do gestor', 'Efetivar o colaborador'):
            self.assertContains(resposta, texto)
        self.assertContains(resposta, 'type="checkbox"')  # caixa de seleção
        self.assertContains(resposta, '<textarea', html=False)  # pergunta de texto
        self.assertContains(resposta, 'Desligar o colaborador')
        self.assertEqual(self.periodo(self.colaborador).formulario, self.formulario)  # formulário fixado no card

    def test_envio_salva_respostas_atualiza_status_e_move_o_card(self, _):
        self.client.force_login(self.gestor)
        resposta = self.client.post(self.url, self.dados_avaliacao('DESLIGADO'))
        self.assertRedirects(resposta, reverse('experiencia:kanban'), fetch_redirect_response=False)

        periodo = self.periodo(self.colaborador)
        self.assertEqual(periodo.status_avaliacao, PeriodoExperiencia.StatusAvaliacao.RESPONDIDO)
        self.assertEqual(periodo.status, Status.DESLIGADO)
        self.assertEqual(periodo.resposta_avaliacao.decisao, 'DESLIGADO')
        self.assertEqual(periodo.resposta_avaliacao.observacoes, 'Ótimo começo.')
        self.assertEqual(periodo.avaliacao_registrada_por, self.gestor)

        # Reabrir o link mostra a avaliação enviada, sem permitir reenviar.
        self.assertContains(self.client.get(self.url), 'Esta avaliação já foi respondida.')

    def test_envio_incompleto_mostra_erros_sem_alterar_o_card(self, _):
        self.client.force_login(self.gestor)
        dados = self.dados_avaliacao('EFETIVADO')
        dados.pop('decisao')
        dados.pop(f'pergunta_{self.formulario.perguntas.first().pk}')
        resposta = self.client.post(self.url, dados)
        self.assertEqual(resposta.status_code, 200)
        self.assertContains(resposta, 'Informe a decisão final')
        self.assertContains(resposta, 'Responda esta pergunta.')
        self.assertEqual(self.periodo(self.colaborador).status, Status.VENCENDO)

    def test_rh_nao_responde_e_e_levado_aos_detalhes(self, _):
        self.client.force_login(self.rh)
        periodo = self.periodo(self.colaborador)
        resposta = self.client.post(self.url, self.dados_avaliacao('EFETIVADO'))
        self.assertRedirects(
            resposta, reverse('experiencia:avaliacao_respostas', args=[periodo.pk]), fetch_redirect_response=False,
        )
        self.assertFalse(self.periodo(self.colaborador).avaliacao_realizada)

    def test_outro_gestor_nao_acessa_o_link(self, _):
        outro_login = criar_usuario('outro.gestor', modulos=['enps'])
        Colaborador.objects.create(
            nome='Outro gestor', documento=next(self.documentos), empresas=[self.empresa], eh_gestor=True,
            usuario=outro_login,
        )
        self.client.force_login(outro_login)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_token_invalido(self, _):
        self.client.force_login(self.gestor)
        resposta = self.client.get(reverse('experiencia:avaliar', args=['00000000-0000-0000-0000-000000000000']))
        self.assertEqual(resposta.status_code, 404)

    def test_link_exige_login(self, _):
        resposta = self.client.get(self.url)
        self.assertEqual(resposta.status_code, 302)
        self.assertIn(reverse('login'), resposta['Location'])

    def test_antes_de_vencendo_informa_quando_libera(self, _):
        colaborador = self.criar_colaborador(contratado_ha(10), nome='Carla')
        atualizar_periodos(HOJE)
        self.client.force_login(self.gestor)
        resposta = self.client.get(self.periodo(colaborador).caminho_avaliacao)
        self.assertContains(resposta, 'A avaliação fica disponível a partir de 04/12/2026')

    def test_sem_formulario_cadastrado(self, _):
        FormularioAvaliacao.objects.all().delete()
        self.client.force_login(self.gestor)
        self.assertContains(self.client.get(self.url), 'O RH ainda não cadastrou o formulário')

    def test_kanban_mostra_o_link_no_card(self, _):
        periodo = self.periodo(self.colaborador)
        self.client.force_login(self.gestor)
        resposta = self.client.get(reverse('experiencia:kanban'))
        self.assertContains(resposta, 'Preencher avaliação')
        self.assertContains(resposta, periodo.link_avaliacao)
        self.assertNotContains(resposta, 'Bruno')  # só os próprios liderados

        self.client.force_login(self.rh)
        resposta = self.client.get(reverse('experiencia:kanban'))
        self.assertNotContains(resposta, 'Preencher avaliação')
        self.assertContains(resposta, periodo.link_avaliacao)
        self.assertContains(resposta, 'Aguardando o gestor responder o formulário.')


@mock.patch('django.utils.timezone.localdate', return_value=HOJE)
class GerenciarAvaliacoesTests(BaseExperienciaTests):
    def dados_formulario(self, **extra):
        dados = {
            'titulo': 'Avaliação 2026',
            'descricao': '<p onclick="x()">Leia com <em>atenção</em></p><script>alert(1)</script>',
            'perguntas-TOTAL_FORMS': '2', 'perguntas-INITIAL_FORMS': '0',
            'perguntas-MIN_NUM_FORMS': '1', 'perguntas-MAX_NUM_FORMS': '1000',
            'perguntas-0-texto_pergunta': 'Qualidade do trabalho', 'perguntas-0-tipo_resposta': 'NOTA_0_10',
            'perguntas-0-ordem': '1',
            'perguntas-1-texto_pergunta': 'Comunicação', 'perguntas-1-tipo_resposta': 'RADIO',
            'perguntas-1-ordem': '2', 'perguntas-1-opcoes_texto': 'Supera\nAtende\nNão atende',
        }
        dados.update(extra)
        return dados

    def test_rh_cadastra_formulario_com_descricao_sanitizada(self, _):
        self.client.force_login(self.rh)
        resposta = self.client.post(reverse('experiencia:formulario_criar'), self.dados_formulario())
        self.assertRedirects(resposta, reverse('experiencia:avaliacoes_painel'), fetch_redirect_response=False)

        formulario = FormularioAvaliacao.objects.get(titulo='Avaliação 2026')
        self.assertEqual(formulario.descricao, '<p>Leia com <em>atenção</em></p>')
        self.assertEqual(
            list(formulario.perguntas.values_list('texto_pergunta', 'tipo_resposta')),
            [('Qualidade do trabalho', 'NOTA_0_10'), ('Comunicação', 'RADIO')],
        )
        comunicacao = formulario.perguntas.get(tipo_resposta='RADIO')
        self.assertEqual(list(comunicacao.opcoes.values_list('texto_opcao', flat=True)), ['Supera', 'Atende', 'Não atende'])
        self.assertFalse(formulario.perguntas.get(tipo_resposta='NOTA_0_10').opcoes.exists())
        self.assertFalse(formulario.padrao)  # já existe um padrão

    def test_marcar_como_padrao_desmarca_o_anterior(self, _):
        self.client.force_login(self.rh)
        self.client.post(reverse('experiencia:formulario_criar'), self.dados_formulario(padrao='on'))
        novo = FormularioAvaliacao.objects.get(titulo='Avaliação 2026')
        self.formulario.refresh_from_db()
        self.assertTrue(novo.padrao)
        self.assertFalse(self.formulario.padrao)

    def test_primeiro_formulario_vira_padrao(self, _):
        FormularioAvaliacao.objects.all().delete()
        self.client.force_login(self.rh)
        self.client.post(reverse('experiencia:formulario_criar'), self.dados_formulario())
        self.assertTrue(FormularioAvaliacao.objects.get().padrao)

    def test_escolha_exige_duas_opcoes(self, _):
        self.client.force_login(self.rh)
        dados = self.dados_formulario(**{'perguntas-1-opcoes_texto': 'Única'})
        resposta = self.client.post(reverse('experiencia:formulario_criar'), dados)
        self.assertContains(resposta, 'Informe ao menos duas opções de resposta.')
        self.assertFalse(FormularioAvaliacao.objects.filter(titulo='Avaliação 2026').exists())

    def test_exige_ao_menos_uma_pergunta(self, _):
        self.client.force_login(self.rh)
        resposta = self.client.post(reverse('experiencia:formulario_criar'), self.dados_formulario(**{'perguntas-TOTAL_FORMS': '0'}))
        self.assertEqual(resposta.status_code, 200)
        self.assertFalse(FormularioAvaliacao.objects.filter(titulo='Avaliação 2026').exists())

    def test_editar_pergunta_troca_tipo_e_opcoes(self, _):
        self.client.force_login(self.rh)
        perguntas = list(self.formulario.perguntas.all())
        dados = {
            'titulo': self.formulario.titulo, 'descricao': '', 'padrao': 'on',
            'perguntas-TOTAL_FORMS': str(len(perguntas)), 'perguntas-INITIAL_FORMS': str(len(perguntas)),
            'perguntas-MIN_NUM_FORMS': '1', 'perguntas-MAX_NUM_FORMS': '1000',
        }
        for indice, pergunta in enumerate(perguntas):
            dados.update({
                f'perguntas-{indice}-id': pergunta.pk, f'perguntas-{indice}-texto_pergunta': pergunta.texto_pergunta,
                f'perguntas-{indice}-tipo_resposta': pergunta.tipo_resposta, f'perguntas-{indice}-ordem': pergunta.ordem,
                f'perguntas-{indice}-opcoes_texto': '\n'.join(pergunta.opcoes.values_list('texto_opcao', flat=True)),
            })
        # Pergunta de texto passa a ser múltipla escolha; a de escolha única vira nota.
        dados['perguntas-3-tipo_resposta'] = 'RADIO'
        dados['perguntas-3-opcoes_texto'] = 'Sim\nNão'
        dados['perguntas-1-tipo_resposta'] = 'NOTA_0_10'
        resposta = self.client.post(reverse('experiencia:formulario_editar', args=[self.formulario.pk]), dados)
        self.assertRedirects(resposta, reverse('experiencia:avaliacoes_painel'), fetch_redirect_response=False)

        texto, radio = perguntas[3], perguntas[1]
        texto.refresh_from_db()
        radio.refresh_from_db()
        self.assertEqual(texto.tipo_resposta, 'RADIO')
        self.assertEqual(list(texto.opcoes.values_list('texto_opcao', flat=True)), ['Sim', 'Não'])
        self.assertEqual(radio.tipo_resposta, 'NOTA_0_10')
        self.assertFalse(radio.opcoes.exists())

    def test_nao_exclui_o_formulario_padrao(self, _):
        self.client.force_login(self.rh)
        self.client.post(reverse('experiencia:formulario_excluir', args=[self.formulario.pk]))
        self.assertTrue(FormularioAvaliacao.objects.filter(pk=self.formulario.pk).exists())

    def test_painel_mostra_pendentes_e_respondidas_com_detalhes(self, _):
        pendente = self.criar_colaborador(contratado_ha(80), nome='Pendente Silva')
        respondida = self.criar_colaborador(contratado_ha(80), nome='Respondida Souza')
        atualizar_periodos(HOJE)
        self.avaliar(self.periodo(respondida), RespostaAvaliacao.Decisao.EFETIVADO)

        self.client.force_login(self.rh)
        painel = reverse('experiencia:avaliacoes_painel')
        linha = '<td class="fw-semibold">{}</td>'.format  # linha da tabela (o sininho também cita nomes)
        pendentes = self.client.get(painel)
        self.assertContains(pendentes, linha('Pendente Silva'))
        self.assertNotContains(pendentes, linha('Respondida Souza'))
        respondidas = self.client.get(painel, {'situacao': 'respondidas'})
        self.assertContains(respondidas, linha('Respondida Souza'))
        self.assertNotContains(respondidas, linha('Pendente Silva'))

        detalhe = self.client.get(reverse('experiencia:avaliacao_respostas', args=[self.periodo(respondida).pk]))
        self.assertContains(detalhe, 'Decisão: Efetivado')
        for texto in ('Pontualidade', '9 / 10', 'Ótimo', 'Comunicação', 'Liderança', 'Muito dedicado.'):
            self.assertContains(detalhe, texto)

        sem_resposta = self.client.get(reverse('experiencia:avaliacao_respostas', args=[self.periodo(pendente).pk]))
        self.assertContains(sem_resposta, 'Avaliação ainda não respondida')

    def test_gestor_nao_acessa_o_painel_nem_os_formularios(self, _):
        gestor_com_modulo = criar_usuario('gestor.rh', modulos=['experiencia'])
        Colaborador.objects.create(
            nome='Gestor com módulo', documento=next(self.documentos), empresas=[self.empresa], eh_gestor=True,
            usuario=gestor_com_modulo,
        )
        for usuario in (self.gestor, gestor_com_modulo):
            self.client.force_login(usuario)
            self.assertEqual(self.client.get(reverse('experiencia:avaliacoes_painel')).status_code, 403)
            self.assertEqual(self.client.get(reverse('experiencia:formulario_criar')).status_code, 403)

    def test_sanitizacao_remove_conteudo_perigoso(self, _):
        html = limpar_html('<a href="javascript:alert(1)">x</a><img src=x onerror=alert(1)><p style="color:red;position:fixed">ok</p>')
        self.assertNotIn('javascript', html)
        self.assertNotIn('onerror', html)
        self.assertNotIn('position', html)
        self.assertIn('<p style="color:red">ok</p>', html)


@mock.patch('django.utils.timezone.localdate', return_value=HOJE)
class KanbanViewsTests(BaseExperienciaTests):
    def setUp(self):
        super().setUp()
        self.colaborador = self.criar_colaborador(contratado_ha(80))
        self.outro = self.criar_colaborador(contratado_ha(80), nome='Bruno', gestor=None)

    def test_rh_ve_o_quadro_e_o_sininho(self, _):
        self.client.force_login(self.rh)
        resposta = self.client.get(reverse('experiencia:kanban'))
        self.assertContains(resposta, 'Ana')
        self.assertContains(resposta, 'Bruno')
        self.assertContains(resposta, 'Gerenciar avaliações')
        self.assertContains(resposta, 'Defina um gestor com acesso ao painel para Bruno')  # sininho (Bruno sem gestor)

    def test_usuario_sem_modulo_e_sem_liderados_nao_acessa(self, _):
        self.client.force_login(criar_usuario('outro', modulos=['enps']))
        self.assertEqual(self.client.get(reverse('experiencia:kanban')).status_code, 403)

    def test_rh_e_avisado_quando_nao_ha_formulario(self, _):
        FormularioAvaliacao.objects.all().delete()
        self.client.force_login(self.rh)
        self.assertContains(self.client.get(reverse('experiencia:kanban')), 'Nenhum formulário de avaliação cadastrado')

    def test_abrir_notificacao_marca_como_lida_e_redireciona(self, _):
        atualizar_periodos(HOJE)
        notificacao = Notificacao.objects.get(destinatario=self.gestor)
        self.client.force_login(self.gestor)
        resposta = self.client.post(reverse('notificacoes:abrir', args=[notificacao.pk]))
        self.assertRedirects(resposta, notificacao.link, fetch_redirect_response=False)
        notificacao.refresh_from_db()
        self.assertTrue(notificacao.lida)

    def test_nao_abre_notificacao_de_outro_usuario(self, _):
        atualizar_periodos(HOJE)
        notificacao = Notificacao.objects.get(destinatario=self.gestor)
        self.client.force_login(self.rh)
        self.assertEqual(self.client.post(reverse('notificacoes:abrir', args=[notificacao.pk])).status_code, 404)


@mock.patch('django.utils.timezone.localdate', return_value=HOJE)
class AtualizacaoSemAbrirKanbanTests(BaseExperienciaTests):
    """Os avisos não podem depender de alguém abrir o Kanban nem do job externo estar ativo."""

    def setUp(self):
        super().setUp()
        cache.delete(CHAVE_CACHE)

    def test_cadastro_ja_a_menos_de_15_dias_notifica_ao_salvar(self, _):
        with self.captureOnCommitCallbacks(execute=True):
            colaborador = self.criar_colaborador(contratado_ha(76), gestor=None)  # faltam 13 dias

        self.assertEqual(self.periodo(colaborador).status, Status.VENCENDO)
        self.assertEqual(len(self.notificacoes(self.rh)), 1)

    def test_informar_data_de_contratacao_depois_cria_o_card(self, _):
        colaborador = self.criar_colaborador(None)
        with self.captureOnCommitCallbacks(execute=True):
            colaborador.data_contratacao = contratado_ha(76)
            colaborador.save()
        self.assertEqual(len(self.notificacoes(self.gestor)), 1)

    def test_uso_do_painel_atualiza_no_maximo_a_cada_intervalo(self, _):
        colaborador = self.criar_colaborador(contratado_ha(10))  # sem on_commit: card ainda não existe
        self.client.force_login(self.rh)

        with mock.patch('experiencia.middleware.atualizar_periodos', wraps=atualizar_periodos) as rotina:
            self.client.get(reverse('pesquisas:dashboard'))
            self.client.get(reverse('pesquisas:dashboard'))
        self.assertEqual(rotina.call_count, 1)
        self.assertEqual(self.periodo(colaborador).status, Status.DENTRO_PRAZO)


class SaidaDosDesligadosTests(BaseExperienciaTests):
    def setUp(self):
        super().setUp()
        self.colaborador = self.criar_colaborador(contratado_ha(80))
        atualizar_periodos(HOJE)
        self.avaliar(self.periodo(self.colaborador), RespostaAvaliacao.Decisao.DESLIGADO)
        self.desligado = self.periodo(self.colaborador)

    def visiveis(self):
        return list(PeriodoExperiencia.objects.em_acompanhamento())

    def test_desligado_sai_do_quadro_apos_15_dias(self):
        desligado_em = self.desligado.status_alterado_em
        with mock.patch('django.utils.timezone.now', return_value=desligado_em + timedelta(days=15) - timedelta(minutes=1)):
            self.assertEqual(self.visiveis(), [self.desligado])
        with mock.patch('django.utils.timezone.now', return_value=desligado_em + timedelta(days=15, minutes=1)):
            self.assertEqual(self.visiveis(), [])
        self.assertTrue(PeriodoExperiencia.objects.filter(pk=self.desligado.pk).exists())  # histórico preservado

    def test_rh_arquiva_antes_do_prazo(self):
        arquivar_periodo(self.desligado, self.rh)
        self.assertEqual(self.visiveis(), [])
        self.assertEqual(self.periodo(self.colaborador).arquivado_por, self.rh)

    def test_so_desligados_podem_ser_arquivados(self):
        colaborador = self.criar_colaborador(contratado_ha(10), nome='Bruno')
        atualizar_periodos(HOJE)
        with self.assertRaises(TransicaoInvalida):
            arquivar_periodo(self.periodo(colaborador), self.rh)

    def test_botao_arquivar_pela_tela(self):
        self.client.force_login(self.rh)
        self.assertContains(self.client.get(reverse('experiencia:kanban')), 'Arquivar')
        resposta = self.client.post(reverse('experiencia:arquivar', args=[self.desligado.pk]))
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(self.visiveis(), [])

    def test_gestor_sem_modulo_nao_arquiva(self):
        self.client.force_login(self.gestor)
        resposta = self.client.post(reverse('experiencia:arquivar', args=[self.desligado.pk]))
        self.assertEqual(resposta.status_code, 403)


class GestorSemLoginTests(BaseExperienciaTests):
    def test_gestor_sem_login_no_painel_faz_o_aviso_ir_para_o_rh(self):
        sem_login = Colaborador.objects.create(
            nome='Gestor sem login', documento=next(self.documentos), empresas=[self.empresa], eh_gestor=True,
        )
        self.criar_colaborador(contratado_ha(80), gestor=sem_login)
        atualizar_periodos(HOJE)
        self.assertEqual(len(self.notificacoes(self.rh)), 1)
        self.assertEqual(self.notificacoes(self.gestor), [])


@mock.patch('django.utils.timezone.localdate', return_value=HOJE)
class PapeisGestorRhTests(BaseExperienciaTests):
    """Gestor: só os próprios liderados e só ele avalia. RH: vê tudo, benefícios, arquiva e gerencia formulários."""

    def setUp(self):
        super().setUp()
        # Gestora com o módulo no perfil (como no perfil "Gestores"): continua sendo tratada como gestora.
        self.gestor.perfil_personalizado = criar_usuario('aux', modulos=['experiencia']).perfil_personalizado
        self.gestor.save()
        self.liderado = self.criar_colaborador(contratado_ha(80))
        self.outro = self.criar_colaborador(contratado_ha(80), nome='Bruno', gestor=None)
        atualizar_periodos(HOJE)

    def test_gestor_com_modulo_ve_apenas_os_proprios_liderados(self, _):
        self.client.force_login(self.gestor)
        resposta = self.client.get(reverse('experiencia:kanban'))
        self.assertContains(resposta, 'Ana')
        self.assertNotContains(resposta, 'Bruno')
        self.assertNotContains(resposta, 'Gerenciar avaliações')

    def test_gestor_nao_finaliza_beneficios_nem_arquiva(self, _):
        periodo = self.periodo(self.liderado)
        self.client.force_login(self.gestor)
        for nome in ('beneficios', 'arquivar'):
            resposta = self.client.post(reverse(f'experiencia:{nome}', args=[periodo.pk]))
            self.assertEqual(resposta.status_code, 403, nome)

    def test_beneficios_so_para_o_rh(self, _):
        self.avaliar(self.periodo(self.liderado), RespostaAvaliacao.Decisao.EFETIVADO)
        self.assertIn('Incluir benefícios para o colaborador Ana.', self.notificacoes(self.rh))
        self.assertEqual(self.notificacoes(self.gestor), [])

    def test_gestor_definido_depois_de_vencendo_recebe_o_aviso(self, _):
        self.assertIn('Defina um gestor com acesso ao painel para Bruno', ' '.join(self.notificacoes(self.rh)))
        with self.captureOnCommitCallbacks(execute=True):
            self.outro.gestor = self.gestor_colaborador
            self.outro.save()
        self.assertIn('Preencha a avaliação de experiência de Bruno', ' '.join(self.notificacoes(self.gestor)))
        # O aviso pendente do RH deixa de pedir um gestor e passa a citar quem cobrar.
        avisos_rh = ' '.join(self.notificacoes(self.rh))
        self.assertIn('Bruno pendente com o gestor Gestora', avisos_rh)
        self.assertNotIn('Defina um gestor', avisos_rh)

    def test_avaliacao_respondida_encerra_o_aviso_do_gestor_e_do_rh(self, _):
        self.avaliar(self.periodo(self.liderado), RespostaAvaliacao.Decisao.EFETIVADO)
        self.assertEqual(self.notificacoes(self.gestor), [])
        self.assertNotIn('pendente com o gestor', ' '.join(self.notificacoes(self.rh)))


class AvisoArquivamentoAposDesligamentoTests(BaseExperienciaTests):
    """Formulário de desligamento respondido → RH é avisado para arquivar o card de experiência."""

    AVISO = 'O formulário de desligamento de Ana foi respondido. Arquive o card no Kanban de experiência.'

    def setUp(self):
        from desligamentos.models import Desligamento, FormularioDesligamento

        super().setUp()
        self.colaborador = self.criar_colaborador(contratado_ha(80))
        atualizar_periodos(HOJE)
        self.avaliar(self.periodo(self.colaborador), RespostaAvaliacao.Decisao.DESLIGADO)
        formulario = FormularioDesligamento.objects.create(
            nome='Desligamento', iniciativa=FormularioDesligamento.Iniciativa.EMPRESA, criado_por=self.rh,
        )
        self.desligamento = Desligamento.objects.create(
            colaborador=self.colaborador, formulario=formulario, iniciativa='EMPRESA', criado_por=self.rh,
        )

    def responder_formulario_de_desligamento(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.desligamento.status = 'RESPONDIDO'
            self.desligamento.save(update_fields=['status'])

    def test_resposta_do_formulario_avisa_o_rh_para_arquivar(self):
        self.assertNotIn(self.AVISO, self.notificacoes(self.rh))
        self.responder_formulario_de_desligamento()
        self.assertIn(self.AVISO, self.notificacoes(self.rh))
        self.assertNotIn(self.AVISO, self.notificacoes(self.gestor))

    def test_arquivar_encerra_o_aviso(self):
        self.responder_formulario_de_desligamento()
        arquivar_periodo(self.periodo(self.colaborador), self.rh)
        self.assertNotIn(self.AVISO, self.notificacoes(self.rh))

    def test_card_que_saiu_sozinho_do_quadro_encerra_o_aviso(self):
        self.responder_formulario_de_desligamento()
        depois_do_prazo = self.periodo(self.colaborador).status_alterado_em + timedelta(days=16)
        with mock.patch('django.utils.timezone.now', return_value=depois_do_prazo):
            atualizar_periodos(HOJE + timedelta(days=16))
        self.assertNotIn(self.AVISO, self.notificacoes(self.rh))

    def test_sem_card_de_experiencia_nao_avisa(self):
        arquivar_periodo(self.periodo(self.colaborador), self.rh)
        self.responder_formulario_de_desligamento()
        self.assertNotIn(self.AVISO, self.notificacoes(self.rh))
