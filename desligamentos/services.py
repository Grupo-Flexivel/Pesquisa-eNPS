from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from notificacoes.services import notificar, resolver
from pesquisas.models import Colaborador
from pesquisas.permissoes import DESLIGAMENTOS, usuarios_com_acesso

from .models import Desligamento


def chave_envio(desligamento_pk):
    """Identifica o aviso "enviar o formulário de desligamento" no sininho."""
    return f'desligamento:{desligamento_pk}:envio'


def _avisar_envio(desligamento):
    notificar(
        usuarios_com_acesso(DESLIGAMENTOS),
        f'Enviar o formulário de desligamento "{desligamento.formulario.nome}" para {desligamento.colaborador.nome}.',
        link=f"{reverse('desligamentos:kanban')}#desligamento-{desligamento.pk}",
        chave=chave_envio(desligamento.pk),
    )


def registrar_desligamento_pendente(colaborador, iniciativa, formulario, usuario):
    """Cria o card de desligamento na coluna Pendentes para o colaborador inativado.

    O RH (quem acessa o módulo de desligamento) é avisado no sininho para enviar o formulário.
    """
    desligamento = Desligamento.objects.create(
        colaborador=colaborador,
        formulario=formulario,
        iniciativa=iniciativa,
        status=Desligamento.Status.PENDENTE,
        criado_por=usuario,
    )
    _avisar_envio(desligamento)
    return desligamento


def desligamento_agendado(colaborador):
    return Desligamento.objects.filter(colaborador=colaborador, status=Desligamento.Status.AGENDADO).first()


def agendar_desligamento(colaborador, iniciativa, formulario, usuario):
    """Guarda o motivo de um desligamento com data de demissão futura (ou atualiza o já agendado).

    O colaborador continua ativo; na data de demissão `efetivar_desligamento` o inativa.
    """
    agendado = desligamento_agendado(colaborador)
    if agendado is None:
        return Desligamento.objects.create(
            colaborador=colaborador, formulario=formulario, iniciativa=iniciativa,
            status=Desligamento.Status.AGENDADO, criado_por=usuario,
        )
    if iniciativa and formulario:
        agendado.iniciativa, agendado.formulario = iniciativa, formulario
        agendado.save()
    return agendado


def efetivar_desligamento(desligamento, iniciativa=None, formulario=None):
    """Chegou a data de demissão: inativa o colaborador e leva o card agendado para Pendentes."""
    agora = timezone.now()
    colaborador = desligamento.colaborador
    if colaborador.ativo:
        colaborador.ativo = False
        colaborador.data_inativacao = agora
        colaborador.save()
    if iniciativa and formulario:
        desligamento.iniciativa, desligamento.formulario = iniciativa, formulario
    desligamento.status = Desligamento.Status.PENDENTE
    desligamento.data_criacao = agora  # entra no topo da coluna Pendentes
    desligamento.save()
    _avisar_envio(desligamento)
    return desligamento


def processar_desligamentos_agendados(hoje=None):
    """Inativa quem chegou à data de demissão. Idempotente: pode rodar a qualquer momento.

    Com desligamento agendado, o card vai para Pendentes. Quem tem data de demissão sem motivo
    (cadastro novo ou importação de planilha) só é inativado.
    """
    hoje = hoje or timezone.localdate()
    total = 0
    agendados = Desligamento.objects.select_related('colaborador', 'formulario').filter(
        status=Desligamento.Status.AGENDADO, colaborador__data_demissao__lte=hoje,
    )
    for desligamento in agendados:
        with transaction.atomic():
            efetivar_desligamento(desligamento)
        total += 1

    sem_motivo = Colaborador.objects.filter(ativo=True, data_demissao__lte=hoje).exclude(
        desligamentos__status=Desligamento.Status.AGENDADO,
    )
    for colaborador in sem_motivo:
        colaborador.ativo = False
        colaborador.data_inativacao = timezone.now()
        colaborador.save()
        total += 1
    return total


def resolver_aviso_envio(desligamento):
    """Encerra o aviso de envio para todos (link enviado ou desligamento cancelado)."""
    resolver(chave_envio(desligamento.pk))


def cancelar_desligamento_agendado(colaborador):
    """A data de demissão futura foi apagada: o desligamento agendado deixa de existir."""
    Desligamento.objects.filter(colaborador=colaborador, status=Desligamento.Status.AGENDADO).delete()


def cancelar_desligamentos_abertos(colaborador):
    """Remove pendências de desligamento em aberto quando o colaborador é reativado."""
    abertos = Desligamento.objects.filter(
        colaborador=colaborador,
        status__in=(Desligamento.Status.AGENDADO, Desligamento.Status.PENDENTE, Desligamento.Status.AGUARDANDO_RESPOSTA),
    )
    for desligamento in abertos:
        resolver_aviso_envio(desligamento)
    abertos.delete()
