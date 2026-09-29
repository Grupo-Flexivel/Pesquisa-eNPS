"""Regras de movimentação/estado do candidato: etapas do funil e ativo/inativo.

Ponto único de mudança de etapa: qualquer tela (Kanban etc.) deve passar por
`mover_candidato`, que também grava a rastreabilidade no histórico. O mesmo vale para
inativação/reativação: sempre via `inativar_candidato`/`reativar_candidato`, nunca
manipulando `Candidato.ativo` direto, para a auditoria ficar completa.
"""
from django.db import transaction

from .historico import Evento, registrar
from .models import Candidato


def mover_candidato(candidato, novo_status, usuario):
    """Move o candidato para `novo_status`. Retorna True se houve mudança.

    Levanta ValueError se a etapa não existe. A mudança e o registro no histórico são
    atômicos; a etapa "anterior" vem do banco (com lock), não da instância em memória,
    para o histórico refletir a realidade mesmo com dois analistas mexendo ao mesmo tempo.

    Quando o novo status é REPROVADO, um marco de reprovação também é gravado no
    histórico, para a auditoria do processo seletivo. Isso é independente de o currículo
    ser inativado ou não — essa é uma decisão separada do analista (`inativar_candidato`).
    """
    if novo_status not in Candidato.Status.values:
        raise ValueError(f'Etapa inválida: {novo_status!r}')

    # Força a transação atômica a rodar explicitamente no banco do vector_db
    with transaction.atomic(using='vector_db'):
        atual = Candidato.all_objects.using('vector_db').select_for_update().get(pk=candidato.pk)
        candidato.status = atual.status
        if atual.status == novo_status:
            return False
        anterior = atual.status
        candidato.status = novo_status
        candidato.save(using='vector_db', update_fields=['status', 'atualizado_em'])
        registrar(
            candidato, Evento.ETAPA_ALTERADA, usuario,
            f'Etapa alterada de {Candidato.Status(anterior).label} para {Candidato.Status(novo_status).label}.',
            status_anterior=anterior, status_novo=novo_status,
        )
        if novo_status == Candidato.Status.REPROVADO:
            registrar(
                candidato, Evento.REPROVACAO_REGISTRADA, usuario,
                'Candidato reprovado no processo seletivo.',
                status_anterior=anterior, status_novo=novo_status,
            )
    return True


def inativar_candidato(candidato, motivo, usuario):
    """Inativa o candidato com auditoria (motivo, quem e quando). Levanta ValidationError
    (via `Candidato.inativar`) se o motivo não for informado."""
    with transaction.atomic(using='vector_db'):
        atual = Candidato.all_objects.using('vector_db').select_for_update().get(pk=candidato.pk)
        atual.inativar(motivo, usuario, using='vector_db')
        registrar(atual, Evento.CANDIDATO_INATIVADO, usuario, atual.motivo_inativacao)
    return atual


def reativar_candidato(candidato, usuario):
    """Reverte a inativação e registra o evento no histórico."""
    with transaction.atomic(using='vector_db'):
        atual = Candidato.all_objects.using('vector_db').select_for_update().get(pk=candidato.pk)
        atual.reativar(usuario, using='vector_db')
        registrar(atual, Evento.CANDIDATO_REATIVADO, usuario, 'Candidato reativado.')
    return atual
