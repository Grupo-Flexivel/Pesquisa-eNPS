import logging
from functools import partial

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from desligamentos.models import Desligamento
from pesquisas.models import Colaborador


logger = logging.getLogger(__name__)


def _sincronizar(colaborador_pk):
    from .services import sincronizar_colaborador

    colaborador = Colaborador.objects.select_related('gestor__usuario').filter(pk=colaborador_pk).first()
    if colaborador is None:
        return
    try:
        sincronizar_colaborador(colaborador)
    except Exception:
        # O cadastro já foi salvo; a rotina periódica corrige o card na próxima execução.
        logger.exception('Falha ao atualizar o período de experiência do colaborador %s', colaborador_pk)


@receiver(post_save, sender=Colaborador, dispatch_uid='experiencia_sincronizar_colaborador')
def colaborador_salvo(sender, instance, raw=False, **kwargs):
    if raw:
        return
    # Após o commit: a view ainda ajusta o colaborador (ex.: data de ativação) na mesma transação.
    transaction.on_commit(partial(_sincronizar, instance.pk))


def _avisar_arquivamento(colaborador_pk):
    from .services import avisar_arquivamento_apos_desligamento

    colaborador = Colaborador.objects.filter(pk=colaborador_pk).first()
    if colaborador is None:
        return
    try:
        avisar_arquivamento_apos_desligamento(colaborador)
    except Exception:
        # A resposta do formulário já foi salva; o aviso é só uma conveniência para o RH.
        logger.exception('Falha ao avisar o arquivamento do período de experiência do colaborador %s', colaborador_pk)


@receiver(post_save, sender=Desligamento, dispatch_uid='experiencia_desligamento_respondido')
def desligamento_respondido(sender, instance, raw=False, update_fields=None, **kwargs):
    if raw or instance.status != Desligamento.Status.RESPONDIDO:
        return
    if update_fields is not None and 'status' not in update_fields:
        return
    transaction.on_commit(partial(_avisar_arquivamento, instance.colaborador_id))
