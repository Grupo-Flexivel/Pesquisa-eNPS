"""Regras de negócio do Kanban de período de experiência.

Fluxo:
  Dentro do prazo ──(≤ 15 dias restantes, automático)──► Vencendo ──(gestor responde o formulário)──► Efetivado / Desligado
  (em "Vencendo", só o gestor responsável responde a avaliação, cuja decisão final move o card)
  Vencendo ──(período terminou, automático)──► Efetivado
  Efetivado ──(RH marca "benefícios finalizados")──► sai do Kanban
  Desligado ──(15 dias depois, ou RH clica em "Arquivar")──► sai do Kanban
  (quando o formulário de desligamento é respondido, o RH é avisado para arquivar o card)

`atualizar_periodos` é idempotente: roda no comando diário `atualizar_experiencia` e também
ao abrir o Kanban, para que o quadro nunca mostre um status desatualizado.
"""
from collections import Counter

from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from notificacoes.models import Notificacao
from notificacoes.services import notificar, resolver
from pesquisas.models import Colaborador
from pesquisas.permissoes import EXPERIENCIA, usuario_tem_acesso, usuarios_com_acesso

from . import regras
from .models import (
    DetalheRespostaAvaliacao, FormularioAvaliacao, PerguntaAvaliacao, PeriodoExperiencia, RespostaAvaliacao,
)


Status = PeriodoExperiencia.Status


class TransicaoInvalida(Exception):
    """Movimento do card bloqueado por uma regra de negócio (mensagem exibida ao usuário)."""


def eh_gestor(user):
    """Gestor: login vinculado a um colaborador ativo marcado como "É gestor"."""
    return bool(user.is_authenticated and Colaborador.objects.filter(usuario=user, eh_gestor=True, ativo=True).exists())


def pode_gerir(user):
    """RH: acessa o Kanban inteiro, acompanha as avaliações, finaliza benefícios e arquiva.

    É RH quem tem o módulo e não é gestor. Gestores com o módulo no perfil continuam sendo
    tratados como gestores (veem só os próprios liderados); o administrador total é sempre RH.
    """
    if not usuario_tem_acesso(user, EXPERIENCIA):
        return False
    return user.is_superuser or getattr(user, 'is_admin_total', False) or not eh_gestor(user)


def pode_avaliar(user, periodo):
    """Apenas o gestor responsável responde a avaliação (e, nela, decide entre efetivar e desligar)."""
    gestor = periodo.colaborador.gestor
    return bool(user.is_authenticated and gestor and gestor.usuario_id == user.pk)


def usuarios_rh():
    """Destinatários dos avisos do RH (benefícios, colaborador sem gestor)."""
    return [usuario for usuario in usuarios_com_acesso(EXPERIENCIA) if pode_gerir(usuario)]


def usuario_do_gestor(colaborador):
    """Login pelo qual o gestor do colaborador recebe avisos; None se não houver um utilizável."""
    gestor = colaborador.gestor
    usuario = gestor.usuario if gestor and gestor.ativo else None
    return usuario if usuario and usuario.is_active and usuario.is_staff else None


def _link(periodo):
    return f"{reverse('experiencia:kanban')}#periodo-{periodo.pk}"


def _transicionar(periodo, de, para, usuario=None, **campos):
    """Muda o status só se o card ainda estiver em `de` (evita corrida entre a rotina e o RH).

    Retorna True quando esta chamada efetivamente fez a mudança.
    """
    campos.update(status=para, status_alterado_em=timezone.now(), status_alterado_por=usuario)
    if not PeriodoExperiencia.objects.filter(pk=periodo.pk, status=de).update(**campos):
        return False
    for nome, valor in campos.items():
        setattr(periodo, nome, valor)
    return True


def _notificar_avaliacao(periodo):
    """Garante o aviso de avaliação para o responsável atual (idempotente).

    O gestor recebe o pedido de avaliação e o RH acompanha (para cobrar o gestor, se preciso).
    Chamado a cada atualização de um card em "Vencendo" com avaliação pendente: se o gestor for
    definido ou trocado depois que o card entrou na coluna, o novo responsável também é avisado,
    o texto pendente do RH passa a citar o novo gestor e o aviso do gestor anterior é encerrado.
    """
    colaborador = periodo.colaborador
    fim = f'{periodo.fim_experiencia:%d/%m/%Y}'
    link, chave = _link(periodo), periodo.chave_avaliacao
    usuario_gestor = usuario_do_gestor(colaborador)

    destinatarios = []
    if usuario_gestor:
        destinatarios.append(usuario_gestor)
        notificar(
            [usuario_gestor], f'Preencha a avaliação de experiência de {colaborador.nome} (termina em {fim}).',
            link=periodo.caminho_avaliacao, chave=chave,
        )
        mensagem_rh = (
            f'Avaliação de experiência de {colaborador.nome} pendente com o gestor '
            f'{colaborador.gestor.nome} (termina em {fim}).'
        )
    else:
        # Só o gestor pode registrar a avaliação: sem gestor com login ativo, o RH precisa definir um.
        mensagem_rh = (
            f'Defina um gestor com acesso ao painel para {colaborador.nome}: '
            f'a avaliação de experiência termina em {fim}.'
        )
    rh = [usuario for usuario in usuarios_rh() if usuario != usuario_gestor]
    destinatarios.extend(rh)
    notificar(rh, mensagem_rh, link=link, chave=chave)
    resolver(chave, exceto=destinatarios)


def _notificar_beneficios(periodo):
    notificar(
        usuarios_rh(),
        f'Incluir benefícios para o colaborador {periodo.colaborador.nome}.',
        link=_link(periodo), chave=periodo.chave_beneficios,
    )


def _notificar_desligamento(periodo):
    """Avisa o RH que o gestor desligou o colaborador, para iniciar o processo de desligamento."""
    colaborador = periodo.colaborador
    quem = colaborador.gestor.nome if colaborador.gestor else 'O gestor'
    notificar(
        usuarios_rh(),
        f'{quem} desligou {colaborador.nome} no período de experiência. '
        'Inative o colaborador para iniciar o desligamento.',
        link=reverse('pesquisas:colaborador_editar', args=[colaborador.pk]), chave=periodo.chave_desligamento,
    )


def atualizar_periodo(periodo, hoje=None):
    """Aplica as regras automáticas a um card aberto. Retorna o novo status, ou None se nada mudou."""
    hoje = hoje or timezone.localdate()
    colaborador = periodo.colaborador
    origem = periodo.status
    if origem not in PeriodoExperiencia.STATUS_AUTOMATICOS:
        return None

    if not colaborador.ativo:
        # Colaborador inativado pelo fluxo de desligamento: o card acompanha a decisão já tomada.
        return Status.DESLIGADO if _transicionar(periodo, origem, Status.DESLIGADO) else None

    if not colaborador.data_contratacao:
        return None

    if regras.periodo_terminou(colaborador.data_contratacao, hoje):
        # Contrato de experiência vencido sem decisão: o colaborador é efetivado.
        if _transicionar(periodo, origem, Status.EFETIVADO, efetivado_automaticamente=True):
            resolver(periodo.chave_avaliacao)
            _notificar_beneficios(periodo)
            return Status.EFETIVADO
        return None

    destino = Status.VENCENDO if regras.em_alerta(colaborador.data_contratacao, hoje) else Status.DENTRO_PRAZO
    mudou = destino != origem
    if mudou and not _transicionar(periodo, origem, destino):
        return None
    if periodo.status == Status.VENCENDO and not periodo.avaliacao_realizada:
        _notificar_avaliacao(periodo)
    return destino if mudou else None


def _criar_card(colaborador):
    """Cria o card do colaborador; None se outra execução o criou em paralelo."""
    try:
        with transaction.atomic():
            return PeriodoExperiencia.objects.create(colaborador=colaborador)
    except IntegrityError:
        return None


def atualizar_periodos(hoje=None):
    """Cria os cards de novos contratados e atualiza automaticamente os cards abertos.

    Só entram no Kanban colaboradores ativos ainda dentro dos 90 dias: contratações antigas
    (inclusive registros legados que receberem a data depois) não são efetivadas em massa.
    """
    hoje = hoje or timezone.localdate()
    resumo = Counter()

    novos = Colaborador.objects.filter(
        ativo=True,
        data_contratacao__gte=regras.primeira_contratacao_acompanhada(hoje),
        periodo_experiencia__isnull=True,
    )
    for colaborador in novos:
        if _criar_card(colaborador):
            resumo['criados'] += 1

    abertos = PeriodoExperiencia.objects.abertos().select_related('colaborador', 'colaborador__gestor__usuario')
    for periodo in abertos:
        novo_status = atualizar_periodo(periodo, hoje)
        if novo_status:
            resumo[novo_status] += 1
    _encerrar_avisos_de_arquivamento_vencidos()
    return resumo


def _encerrar_avisos_de_arquivamento_vencidos():
    """Encerra o aviso "arquive o card" dos desligados que já saíram do quadro sozinhos (15 dias)."""
    chaves = set(
        Notificacao.objects.filter(chave__startswith='experiencia:', chave__endswith=':arquivamento', lida=False)
        .values_list('chave', flat=True)
    )
    if not chaves:
        return
    por_periodo = {int(chave.split(':')[1]): chave for chave in chaves}
    visiveis = set(
        PeriodoExperiencia.objects.em_acompanhamento().filter(pk__in=por_periodo).values_list('pk', flat=True)
    )
    for pk, chave in por_periodo.items():
        if pk not in visiveis:
            resolver(chave)


def avisar_arquivamento_apos_desligamento(colaborador):
    """O formulário de desligamento foi respondido: o RH é avisado para arquivar o card de experiência."""
    periodo = (
        PeriodoExperiencia.objects.em_acompanhamento()
        .filter(colaborador=colaborador, status=Status.DESLIGADO)
        .select_related('colaborador')
        .first()
    )
    if periodo is None:
        return None
    notificar(
        usuarios_rh(),
        f'O formulário de desligamento de {periodo.colaborador.nome} foi respondido. '
        'Arquive o card no Kanban de experiência.',
        link=_link(periodo), chave=periodo.chave_arquivamento,
    )
    return periodo


def sincronizar_colaborador(colaborador, hoje=None):
    """Aplica as regras a um único colaborador logo após ele ser salvo (cadastro, edição, importação).

    Assim, quem é cadastrado já a 15 dias ou menos do fim da experiência gera o aviso na hora,
    sem esperar a rotina periódica.
    """
    hoje = hoje or timezone.localdate()
    periodo = PeriodoExperiencia.objects.filter(colaborador=colaborador).first()
    if periodo is None:
        dentro_da_janela = (
            colaborador.ativo and colaborador.data_contratacao
            and colaborador.data_contratacao >= regras.primeira_contratacao_acompanhada(hoje)
        )
        if not dentro_da_janela:
            return None
        periodo = _criar_card(colaborador) or PeriodoExperiencia.objects.get(colaborador=colaborador)
    periodo.colaborador = colaborador
    if periodo.status == Status.DESLIGADO and not colaborador.ativo:
        resolver(periodo.chave_desligamento)  # RH já inativou o colaborador: pendência concluída
    return atualizar_periodo(periodo, hoje)


def formulario_padrao():
    return FormularioAvaliacao.objects.filter(padrao=True).first()


def formulario_da_avaliacao(periodo, fixar=False):
    """Formulário que o gestor responde neste card.

    O primeiro acesso do gestor (`fixar=True`) prende o card ao formulário padrão vigente, para
    que uma troca de padrão pelo RH não mude as perguntas no meio do preenchimento.
    """
    if periodo.formulario_id:
        return periodo.formulario
    formulario = formulario_padrao()
    if formulario and fixar:
        periodo.formulario = formulario
        periodo.save(update_fields=['formulario'])
    return formulario


def _detalhe_da_resposta(pergunta, valor):
    """Monta o detalhe (com cópia do texto da pergunta e das opções) validando o valor pelo tipo."""
    Tipo = PerguntaAvaliacao.TipoResposta
    detalhe = DetalheRespostaAvaliacao(
        pergunta=pergunta, ordem=pergunta.ordem, pergunta_texto=pergunta.texto_pergunta,
        tipo_resposta=pergunta.tipo_resposta,
    )
    if pergunta.tipo_resposta == Tipo.TEXTO:
        valido = isinstance(valor, str) and valor.strip()
        detalhe.valor_texto = valor.strip() if valido else None
    elif pergunta.tipo_resposta == Tipo.NOTA_0_10:
        valido = isinstance(valor, int) and 0 <= valor <= 10
        detalhe.valor_inteiro = valor
    else:
        opcoes = valor if pergunta.tipo_resposta == Tipo.CHECKBOX else [valor]
        valido = (
            isinstance(opcoes, list) and opcoes
            and all(getattr(opcao, 'pergunta_id', None) == pergunta.pk for opcao in opcoes)
        )
        detalhe.opcoes_selecionadas = [opcao.texto_opcao for opcao in opcoes] if valido else []
    if not valido:
        raise TransicaoInvalida(f'Resposta inválida para a pergunta "{pergunta.texto_pergunta}".')
    return detalhe


def registrar_avaliacao(periodo, formulario, respostas, decisao, usuario, observacoes='', hoje=None):
    """Grava a avaliação do gestor e aplica a decisão: o card vai para "Efetivado" ou "Desligado".

    `respostas`: {PerguntaAvaliacao: valor} cobrindo todas as perguntas do formulário, sendo o valor
    um texto (TEXTO), um inteiro de 0 a 10 (NOTA_0_10), uma opção (RADIO) ou uma lista de opções
    (CHECKBOX). A permissão (gestor responsável) é verificada na view.
    """
    if decisao not in RespostaAvaliacao.Decisao.values:
        raise TransicaoInvalida('Informe a decisão final: efetivar ou desligar o colaborador.')
    if set(respostas) != set(formulario.perguntas.all()):
        raise TransicaoInvalida('Responda todas as perguntas do formulário.')
    detalhes = [
        _detalhe_da_resposta(pergunta, valor)
        for pergunta, valor in sorted(respostas.items(), key=lambda item: item[0].ordem)
    ]

    # Regras automáticas primeiro (fora da transação abaixo, para não serem desfeitas se a
    # avaliação for recusada): um período já terminado vira Efetivado sem depender do gestor.
    if atualizar_periodo(periodo, hoje) == Status.EFETIVADO:
        raise TransicaoInvalida('O período de experiência terminou e o colaborador foi efetivado automaticamente.')

    with transaction.atomic():
        periodo = PeriodoExperiencia.objects.select_for_update().select_related('colaborador').get(pk=periodo.pk)
        if periodo.avaliacao_realizada:
            raise TransicaoInvalida('Esta avaliação já foi respondida.')
        if periodo.status != Status.VENCENDO:
            raise TransicaoInvalida(
                f'A avaliação só pode ser respondida com o colaborador em "Vencendo" '
                f'(este está em "{periodo.get_status_display()}").'
            )

        agora = timezone.now()
        resposta = RespostaAvaliacao.objects.create(
            periodo=periodo, formulario=formulario, formulario_titulo=formulario.titulo, decisao=decisao,
            observacoes=observacoes.strip(), respondido_por=usuario, respondido_em=agora,
        )
        for detalhe in detalhes:
            detalhe.resposta = resposta
        DetalheRespostaAvaliacao.objects.bulk_create(detalhes)
        PeriodoExperiencia.objects.filter(pk=periodo.pk).update(
            status_avaliacao=PeriodoExperiencia.StatusAvaliacao.RESPONDIDO, formulario=formulario,
            avaliacao_registrada_em=agora, avaliacao_registrada_por=usuario,
        )
        periodo.status_avaliacao = PeriodoExperiencia.StatusAvaliacao.RESPONDIDO

        _transicionar(periodo, Status.VENCENDO, decisao, usuario)
        resolver(periodo.chave_avaliacao)
        if decisao == Status.EFETIVADO:
            _notificar_beneficios(periodo)
        elif periodo.colaborador.ativo:
            _notificar_desligamento(periodo)
    return resposta


def finalizar_beneficios(periodo, usuario):
    """Encerra o acompanhamento: o card do efetivado sai do Kanban."""
    if periodo.status != Status.EFETIVADO:
        raise TransicaoInvalida('A inclusão de benefícios só se aplica a colaboradores efetivados.')
    if periodo.beneficios_finalizados:
        return periodo
    periodo.beneficios_finalizados = True
    periodo.beneficios_finalizados_em = timezone.now()
    periodo.beneficios_finalizados_por = usuario
    periodo.save(update_fields=['beneficios_finalizados', 'beneficios_finalizados_em', 'beneficios_finalizados_por'])
    resolver(periodo.chave_beneficios)
    return periodo


def arquivar_periodo(periodo, usuario):
    """Tira do Kanban, antes do prazo automático, o card de um colaborador desligado."""
    if periodo.status != Status.DESLIGADO:
        raise TransicaoInvalida('Apenas cards da coluna "Desligado" podem ser arquivados.')
    if periodo.arquivado_em:
        return periodo
    periodo.arquivado_em = timezone.now()
    periodo.arquivado_por = usuario
    periodo.save(update_fields=['arquivado_em', 'arquivado_por'])
    resolver(periodo.chave_desligamento)
    resolver(periodo.chave_arquivamento)
    return periodo
