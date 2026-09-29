from django.contrib import messages
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from pesquisas.models import Colaborador
from pesquisas.services import gerar_hash_documento
from pesquisas.permissoes import DESLIGAMENTOS, modulo_required

from .forms import (
    FormularioDesligamentoForm, PerguntaDesligamentoFormSet, ResponderPerguntaDesligamentoForm,
    ValidarDocumentoDesligamentoForm,
)
from .models import (
    DetalheRespostaDesligamento, Desligamento, FormularioDesligamento, PerguntaDesligamento, RespostaDesligamento,
)


@modulo_required(DESLIGAMENTOS)
def kanban(request):
    desligamentos = Desligamento.objects.select_related('colaborador', 'colaborador__empresa', 'formulario')
    contexto = {
        'pendentes': desligamentos.filter(status=Desligamento.Status.PENDENTE),
        'aguardando': desligamentos.filter(status=Desligamento.Status.AGUARDANDO_RESPOSTA),
        'respondidos': desligamentos.filter(status=Desligamento.Status.RESPONDIDO),
    }
    return render(request, 'desligamentos/kanban.html', contexto)


@modulo_required(DESLIGAMENTOS)
@require_POST
def desligamento_marcar_enviado(request, pk):
    desligamento = get_object_or_404(Desligamento, pk=pk, status=Desligamento.Status.PENDENTE)
    desligamento.status = Desligamento.Status.AGUARDANDO_RESPOSTA
    desligamento.data_envio = timezone.now()
    desligamento.save(update_fields=['status', 'data_envio'])
    messages.success(request, f'Link enviado para {desligamento.colaborador.nome}. O card foi movido para "Aguardando resposta".')
    return redirect('desligamentos:kanban')


@modulo_required(DESLIGAMENTOS)
def desligamento_respostas(request, pk):
    desligamento = get_object_or_404(
        Desligamento.objects.select_related('colaborador', 'colaborador__empresa', 'formulario'),
        pk=pk, status=Desligamento.Status.RESPONDIDO,
    )
    detalhes = DetalheRespostaDesligamento.objects.filter(
        resposta__desligamento=desligamento,
    ).select_related('pergunta').prefetch_related('opcoes').order_by('pergunta__ordem')
    return render(request, 'desligamentos/desligamento_respostas.html', {
        'desligamento': desligamento, 'detalhes': detalhes,
    })


@modulo_required(DESLIGAMENTOS)
def formulario_lista(request):
    formularios = FormularioDesligamento.objects.select_related('criado_por')
    return render(request, 'desligamentos/formulario_lista.html', {'formularios': formularios})


def _salvar_formulario(request, instance=None):
    form = FormularioDesligamentoForm(request.POST or None, instance=instance)
    formset = PerguntaDesligamentoFormSet(request.POST or None, instance=instance, prefix='perguntas')

    if request.method == 'POST':
        form_valido = form.is_valid()
        formset_valido = formset.is_valid()

        if form_valido and formset_valido:
            with transaction.atomic():
                formulario = form.save(commit=False)
                if not formulario.pk:
                    formulario.criado_por = request.user
                formulario.save()
                formset.instance = formulario
                formset.save()

            messages.success(request, 'Formulário de desligamento salvo com sucesso.')
            return redirect('desligamentos:formulario_lista')
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f'Erro no campo {field}: {error}')
            for pergunta_form in formset:
                for field, errors in pergunta_form.errors.items():
                    for error in errors:
                        messages.error(request, f'Erro na pergunta ({field}): {error}')

    return render(request, 'desligamentos/formulario_form.html', {
        'form': form, 'formset': formset, 'formulario': instance,
    })


@modulo_required(DESLIGAMENTOS)
def formulario_criar(request):
    return _salvar_formulario(request)


@modulo_required(DESLIGAMENTOS)
def formulario_editar(request, pk):
    formulario = get_object_or_404(FormularioDesligamento, pk=pk)
    return _salvar_formulario(request, formulario)


@modulo_required(DESLIGAMENTOS)
@require_POST
def formulario_excluir(request, pk):
    formulario = get_object_or_404(FormularioDesligamento, pk=pk)
    try:
        formulario.delete()
        messages.success(request, 'Formulário excluído com sucesso.')
    except Exception as e:
        messages.error(request, f'Erro ao excluir formulário: {str(e)}')
    return redirect('desligamentos:formulario_lista')


def _formulario_disponivel(link_uuid):
    formulario = get_object_or_404(FormularioDesligamento, link_uuid=link_uuid)
    if formulario.perguntas.count() < 1:
        return formulario, 'Este formulário não está disponível.'
    return formulario, None


def validar_participante(request, link_uuid):
    formulario, indisponivel = _formulario_disponivel(link_uuid)
    form = ValidarDocumentoDesligamentoForm(request.POST or None)
    erro = indisponivel
    if request.method == 'POST' and not indisponivel and form.is_valid():
        documento = form.cleaned_data['documento']
        colaborador = Colaborador.objects.filter(documento=documento, ativo=False).first()
        if not colaborador:
            erro = 'CPF/CNPJ não encontrado entre os colaboradores desligados pendentes.'
        else:
            desligamento = Desligamento.objects.filter(
                colaborador=colaborador, formulario=formulario, status=Desligamento.Status.AGUARDANDO_RESPOSTA,
            ).first()
            if desligamento:
                request.session[f'desligamento_auth_{link_uuid}'] = desligamento.pk
                request.session[f'desligamento_respostas_{link_uuid}'] = {}
                return redirect('desligamentos:responder_etapa', link_uuid=link_uuid, etapa=1)
            if Desligamento.objects.filter(
                colaborador=colaborador, formulario=formulario, status=Desligamento.Status.RESPONDIDO,
            ).exists():
                erro = 'Este formulário já foi respondido.'
            else:
                erro = 'Não encontramos uma pendência de resposta para este CPF/CNPJ neste formulário.'
    return render(request, 'desligamentos/validar_cpf.html', {
        'formulario': formulario, 'form': form, 'erro': erro, 'hide_sidebar': True,
    })


def responder_etapa(request, link_uuid, etapa):
    formulario, indisponivel = _formulario_disponivel(link_uuid)
    desligamento_pk = request.session.get(f'desligamento_auth_{link_uuid}')
    if indisponivel or not desligamento_pk:
        messages.error(request, indisponivel or 'Valide seu CPF ou CNPJ antes de responder.')
        return redirect('desligamentos:responder', link_uuid=link_uuid)

    desligamento = get_object_or_404(Desligamento, pk=desligamento_pk, formulario=formulario)
    if desligamento.status == Desligamento.Status.RESPONDIDO:
        request.session[f'desligamento_concluido_{link_uuid}'] = True
        return redirect('desligamentos:agradecimento', link_uuid=link_uuid)

    perguntas = list(formulario.perguntas.prefetch_related('opcoes'))
    if etapa < 1 or etapa > len(perguntas):
        raise Http404
    pergunta = perguntas[etapa - 1]
    respostas_key = f'desligamento_respostas_{link_uuid}'
    respostas = request.session.get(respostas_key, {})
    for indice, anterior in enumerate(perguntas[:etapa - 1], start=1):
        if str(anterior.pk) not in respostas:
            return redirect('desligamentos:responder_etapa', link_uuid=link_uuid, etapa=indice)

    form = ResponderPerguntaDesligamentoForm(pergunta, request.POST or None, initial={'valor': respostas.get(str(pergunta.pk))})
    if request.method == 'POST' and form.is_valid():
        respostas[str(pergunta.pk)] = form.cleaned_data['valor']
        request.session[respostas_key] = respostas
        if etapa < len(perguntas):
            return redirect('desligamentos:responder_etapa', link_uuid=link_uuid, etapa=etapa + 1)
        try:
            with transaction.atomic():
                resposta = RespostaDesligamento.objects.create(
                    desligamento=desligamento,
                    hash_documento_respondente=gerar_hash_documento(desligamento.colaborador.documento),
                )
                for item in perguntas:
                    valor = respostas.get(str(item.pk))
                    detalhe = DetalheRespostaDesligamento.objects.create(
                        resposta=resposta, pergunta=item,
                        valor_texto=valor if item.tipo_resposta == PerguntaDesligamento.TipoResposta.TEXTO else None,
                        valor_inteiro=valor if item.tipo_resposta == PerguntaDesligamento.TipoResposta.NOTA_0_10 else None,
                    )
                    if item.tipo_resposta == PerguntaDesligamento.TipoResposta.RADIO and valor is not None:
                        detalhe.opcoes.set([valor])
                    elif item.tipo_resposta == PerguntaDesligamento.TipoResposta.CHECKBOX and valor:
                        detalhe.opcoes.set(valor)
                desligamento.status = Desligamento.Status.RESPONDIDO
                desligamento.data_resposta = timezone.now()
                desligamento.save(update_fields=['status', 'data_resposta'])
        except IntegrityError:
            pass
        request.session.pop(f'desligamento_auth_{link_uuid}', None)
        request.session.pop(respostas_key, None)
        request.session[f'desligamento_concluido_{link_uuid}'] = True
        return redirect('desligamentos:agradecimento', link_uuid=link_uuid)

    return render(request, 'desligamentos/responder_etapa.html', {
        'formulario': formulario, 'pergunta': pergunta, 'form': form, 'etapa': etapa,
        'total': len(perguntas), 'progresso': int(etapa / len(perguntas) * 100), 'hide_sidebar': True,
    })


def agradecimento(request, link_uuid):
    formulario = get_object_or_404(FormularioDesligamento, link_uuid=link_uuid)
    if not request.session.get(f'desligamento_concluido_{link_uuid}', False):
        return redirect('desligamentos:responder', link_uuid=link_uuid)
    return render(request, 'desligamentos/agradecimento.html', {'formulario': formulario, 'hide_sidebar': True})
