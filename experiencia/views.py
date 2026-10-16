from functools import wraps

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from pesquisas.models import Colaborador
from pesquisas.permissoes import EXPERIENCIA, modulo_required

from . import regras
from .forms import AvaliacaoForm, FormularioAvaliacaoForm, PerguntaAvaliacaoFormSet
from .models import FormularioAvaliacao, PeriodoExperiencia, RespostaAvaliacao
from .services import (
    TransicaoInvalida, arquivar_periodo, atualizar_periodos, eh_gestor, finalizar_beneficios, formulario_da_avaliacao,
    formulario_padrao, pode_avaliar, pode_gerir, registrar_avaliacao,
)


DESCRICOES_COLUNAS = {
    PeriodoExperiencia.Status.DENTRO_PRAZO: 'Mais de 15 dias para o fim da experiência.',
    PeriodoExperiencia.Status.VENCENDO: '15 dias ou menos: o gestor responde o formulário de avaliação.',
    PeriodoExperiencia.Status.EFETIVADO: 'Aguardando inclusão de benefícios.',
    PeriodoExperiencia.Status.DESLIGADO: f'Não aprovados. Saem do quadro {regras.DIAS_DESLIGADO_NO_QUADRO} dias após o desligamento.',
}


@modulo_required()
def kanban(request):
    gerir = pode_gerir(request.user)
    # Gestores (com ou sem o módulo no perfil) enxergam apenas os próprios liderados.
    if not gerir and not (eh_gestor(request.user) or Colaborador.objects.filter(gestor__usuario=request.user).exists()):
        raise PermissionDenied

    atualizar_periodos()

    periodos = PeriodoExperiencia.objects.em_acompanhamento().select_related(
        'colaborador', 'colaborador__gestor__usuario', 'resposta_avaliacao',
    ).prefetch_related('colaborador__empresas')
    if not gerir:
        periodos = periodos.filter(colaborador__gestor__usuario=request.user)
    periodos = list(periodos)

    colunas = [
        {
            'valor': valor, 'rotulo': rotulo, 'descricao': DESCRICOES_COLUNAS[valor],
            'cards': [p for p in periodos if p.status == valor],
        }
        for valor, rotulo in PeriodoExperiencia.Status.choices
    ]
    return render(request, 'experiencia/kanban.html', {
        'colunas': colunas, 'pode_gerir': gerir, 'sem_formulario_padrao': formulario_padrao() is None,
    })


def _erro(mensagem, status=400):
    return JsonResponse({'ok': False, 'erro': mensagem}, status=status)


def rh_required(view):
    """Ações exclusivas do RH: finalizar benefícios e arquivar (respostas JSON)."""
    @modulo_required(EXPERIENCIA)
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not pode_gerir(request.user):
            return _erro('Apenas o RH pode realizar esta ação.', status=403)
        return view(request, *args, **kwargs)
    return wrapper


def rh_pagina_required(view):
    """Telas exclusivas do RH ("Gerenciar avaliações")."""
    @modulo_required(EXPERIENCIA)
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not pode_gerir(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper


@rh_required
@require_POST
def beneficios(request, pk):
    periodo = get_object_or_404(PeriodoExperiencia, pk=pk)
    try:
        finalizar_beneficios(periodo, request.user)
    except TransicaoInvalida as erro:
        return _erro(str(erro))
    return JsonResponse({'ok': True})


@rh_required
@require_POST
def arquivar(request, pk):
    periodo = get_object_or_404(PeriodoExperiencia, pk=pk)
    try:
        arquivar_periodo(periodo, request.user)
    except TransicaoInvalida as erro:
        return _erro(str(erro))
    return JsonResponse({'ok': True})


# --------------------------------------------------------------------------
# Preenchimento da avaliação pelo gestor (link único do card, exige login)
# --------------------------------------------------------------------------

def _motivo_indisponivel(periodo):
    if periodo.status == PeriodoExperiencia.Status.DENTRO_PRAZO:
        if not periodo.data_contratacao:
            return 'O colaborador está sem data de contratação. Avise o RH.'
        liberacao = regras.inicio_alerta(periodo.data_contratacao)
        return (
            f'A avaliação fica disponível a partir de {liberacao:%d/%m/%Y}, quando faltarem '
            f'{regras.ALERTA_DIAS} dias para o fim do período de experiência.'
        )
    if periodo.status != PeriodoExperiencia.Status.VENCENDO:
        return f'O período de experiência deste colaborador já foi encerrado ({periodo.get_status_display()}).'
    return None


@modulo_required()
def avaliar(request, token):
    periodo = get_object_or_404(
        PeriodoExperiencia.objects.select_related('colaborador', 'colaborador__gestor'),
        token_avaliacao=token,
    )
    if not pode_avaliar(request.user, periodo):
        if pode_gerir(request.user):
            return redirect('experiencia:avaliacao_respostas', pk=periodo.pk)
        raise PermissionDenied

    contexto = {'periodo': periodo, 'colaborador': periodo.colaborador}
    if periodo.avaliacao_realizada:
        contexto['resposta'] = RespostaAvaliacao.objects.filter(periodo=periodo).first()
        return render(request, 'experiencia/avaliar.html', contexto)

    # Garante que o status do card está em dia antes de liberar (ou não) o formulário.
    atualizar_periodos()
    periodo.refresh_from_db()
    indisponivel = _motivo_indisponivel(periodo)
    formulario = None if indisponivel else formulario_da_avaliacao(periodo, fixar=True)
    if not indisponivel and formulario is None:
        indisponivel = 'O RH ainda não cadastrou o formulário de avaliação de experiência. Avise o RH.'
    if indisponivel:
        return render(request, 'experiencia/avaliar.html', {**contexto, 'indisponivel': indisponivel})

    form = AvaliacaoForm(formulario, request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            resposta = registrar_avaliacao(
                periodo, formulario, form.respostas(), form.cleaned_data['decisao'], request.user,
                observacoes=form.cleaned_data['observacoes'],
            )
        except TransicaoInvalida as erro:
            messages.error(request, str(erro))
            return redirect('experiencia:avaliar', token=token)
        messages.success(
            request,
            f'Avaliação de {periodo.colaborador.nome} enviada. Decisão registrada: {resposta.get_decisao_display()}.',
        )
        return redirect('experiencia:kanban')

    return render(request, 'experiencia/avaliar.html', {**contexto, 'formulario': formulario, 'form': form})


# --------------------------------------------------------------------------
# Gerenciar avaliações (RH): formulários e acompanhamento das respostas
# --------------------------------------------------------------------------

FILTROS_SITUACAO = {
    'pendentes': ('Pendentes', {'status_avaliacao': PeriodoExperiencia.StatusAvaliacao.PENDENTE,
                                'status__in': PeriodoExperiencia.STATUS_AUTOMATICOS}),
    'respondidas': ('Respondidas', {'status_avaliacao': PeriodoExperiencia.StatusAvaliacao.RESPONDIDO}),
    'todas': ('Todas', {}),
}


@rh_pagina_required
def avaliacoes_painel(request):
    atualizar_periodos()
    situacao = request.GET.get('situacao', 'pendentes')
    if situacao not in FILTROS_SITUACAO:
        situacao = 'pendentes'
    periodos = PeriodoExperiencia.objects.filter(**FILTROS_SITUACAO[situacao][1]).select_related(
        'colaborador', 'colaborador__gestor', 'resposta_avaliacao',
    ).order_by('-status_alterado_em')
    formularios = FormularioAvaliacao.objects.prefetch_related('perguntas')
    return render(request, 'experiencia/avaliacoes.html', {
        'formularios': formularios,
        'periodos': periodos,
        'situacao': situacao,
        'filtros': [(chave, rotulo) for chave, (rotulo, _) in FILTROS_SITUACAO.items()],
        'total_pendentes': PeriodoExperiencia.objects.filter(**FILTROS_SITUACAO['pendentes'][1]).count(),
    })


def _salvar_formulario(request, instance=None):
    form = FormularioAvaliacaoForm(request.POST or None, instance=instance)
    formset = PerguntaAvaliacaoFormSet(request.POST or None, instance=instance, prefix='perguntas')

    if request.method == 'POST':
        form_valido = form.is_valid()
        formset_valido = formset.is_valid()
        if form_valido and formset_valido:
            with transaction.atomic():
                formulario = form.save(commit=False)
                formulario.padrao = form.cleaned_data['padrao']
                if not formulario.pk:
                    formulario.criado_por = request.user
                # O primeiro formulário cadastrado já nasce como padrão; marcar outro como padrão
                # desmarca o anterior (só um pode ser o padrão).
                outros_padrao = FormularioAvaliacao.objects.filter(padrao=True).exclude(pk=formulario.pk)
                if formulario.padrao:
                    outros_padrao.update(padrao=False)
                elif not outros_padrao.exists():
                    formulario.padrao = True
                formulario.save()
                formset.instance = formulario
                formset.save()
            messages.success(request, 'Formulário de avaliação salvo com sucesso.')
            return redirect('experiencia:avaliacoes_painel')
        messages.error(request, 'Corrija os erros indicados no formulário.')

    return render(request, 'experiencia/formulario_form.html', {
        'form': form, 'formset': formset, 'formulario': instance,
    })


@rh_pagina_required
def formulario_criar(request):
    return _salvar_formulario(request)


@rh_pagina_required
def formulario_editar(request, pk):
    return _salvar_formulario(request, get_object_or_404(FormularioAvaliacao, pk=pk))


@rh_pagina_required
@require_POST
def formulario_definir_padrao(request, pk):
    formulario = get_object_or_404(FormularioAvaliacao, pk=pk)
    with transaction.atomic():
        FormularioAvaliacao.objects.filter(padrao=True).exclude(pk=pk).update(padrao=False)
        FormularioAvaliacao.objects.filter(pk=pk).update(padrao=True)
    messages.success(request, f'"{formulario.titulo}" agora é o formulário padrão das avaliações.')
    return redirect('experiencia:avaliacoes_painel')


@rh_pagina_required
@require_POST
def formulario_excluir(request, pk):
    formulario = get_object_or_404(FormularioAvaliacao, pk=pk)
    if formulario.padrao:
        messages.error(request, 'Defina outro formulário como padrão antes de excluir este.')
    else:
        # As respostas já enviadas guardam uma cópia das perguntas e opções: o histórico é preservado.
        formulario.delete()
        messages.success(request, 'Formulário excluído com sucesso.')
    return redirect('experiencia:avaliacoes_painel')


@rh_pagina_required
def avaliacao_respostas(request, pk):
    periodo = get_object_or_404(
        PeriodoExperiencia.objects.select_related('colaborador', 'colaborador__gestor'), pk=pk,
    )
    resposta = RespostaAvaliacao.objects.filter(periodo=periodo).select_related('respondido_por').first()
    return render(request, 'experiencia/avaliacao_respostas.html', {
        'periodo': periodo, 'colaborador': periodo.colaborador, 'resposta': resposta,
    })
