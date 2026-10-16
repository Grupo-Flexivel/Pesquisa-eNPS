import io

import qrcode
from django.contrib import messages
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from pesquisas.models import Colaborador
from pesquisas.services import gerar_hash_documento
from pesquisas.permissoes import VAGAS, modulo_required

from .forms import CandidaturaForm, PerguntaVagaFormSet, VagaForm, ValidarCPFCandidaturaForm
from .models import Candidatura, DetalheRespostaCandidatura, PerguntaVaga, Vaga


# --------------------------------------------------------------------------
# Painel interno de recrutamento (requer login corporativo e módulo VAGAS)
# --------------------------------------------------------------------------

@modulo_required(VAGAS)
def vaga_lista(request):
    vagas = Vaga.objects.select_related('criado_por').prefetch_related('candidaturas')
    return render(request, 'vagas/vaga_lista.html', {'vagas': vagas})


def _salvar_vaga(request, instance=None):
    form = VagaForm(request.POST or None, instance=instance)
    formset = PerguntaVagaFormSet(request.POST or None, instance=instance, prefix='perguntas')

    if request.method == 'POST':
        form_valido = form.is_valid()
        formset_valido = formset.is_valid()

        if form_valido and formset_valido:
            with transaction.atomic():
                vaga = form.save(commit=False)
                if not vaga.pk:
                    vaga.criado_por = request.user
                vaga.save()
                formset.instance = vaga
                formset.save()

            messages.success(request, 'Vaga salva com sucesso.')
            return redirect('vagas:vaga_detalhe', pk=vaga.pk)
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f'Erro no campo {field}: {error}')
            for pergunta_form in formset:
                for field, errors in pergunta_form.errors.items():
                    for error in errors:
                        messages.error(request, f'Erro na pergunta ({field}): {error}')

    return render(request, 'vagas/vaga_form.html', {
        'form': form, 'formset': formset, 'vaga': instance,
    })


@modulo_required(VAGAS)
def vaga_criar(request):
    return _salvar_vaga(request)


@modulo_required(VAGAS)
def vaga_editar(request, pk):
    vaga = get_object_or_404(Vaga, pk=pk)
    return _salvar_vaga(request, vaga)


@modulo_required(VAGAS)
def vaga_detalhe(request, pk):
    vaga = get_object_or_404(Vaga.objects.select_related('criado_por'), pk=pk)
    candidaturas = vaga.candidaturas.select_related('colaborador').prefetch_related('colaborador__empresas')
    return render(request, 'vagas/vaga_detalhe.html', {'vaga': vaga, 'candidaturas': candidaturas})


@modulo_required(VAGAS)
@require_POST
def vaga_excluir(request, pk):
    vaga = get_object_or_404(Vaga, pk=pk)
    try:
        vaga.delete()
        messages.success(request, 'Vaga excluída com sucesso.')
    except Exception as e:
        messages.error(request, f'Erro ao excluir vaga: {str(e)}')
    return redirect('vagas:vaga_lista')


@modulo_required(VAGAS)
def vaga_qrcode(request, pk):
    vaga = get_object_or_404(Vaga, pk=pk)
    imagem = qrcode.make(vaga.link_completo)
    buffer = io.BytesIO()
    imagem.save(buffer, format='PNG')
    resposta = HttpResponse(buffer.getvalue(), content_type='image/png')
    resposta['Content-Disposition'] = f'attachment; filename="vaga-{vaga.pk}-qrcode.png"'
    return resposta


@modulo_required(VAGAS)
def candidatura_respostas(request, pk):
    candidatura = get_object_or_404(
        Candidatura.objects.select_related('colaborador', 'vaga'), pk=pk,
    )
    detalhes = DetalheRespostaCandidatura.objects.filter(
        candidatura=candidatura,
    ).select_related('pergunta').prefetch_related('opcoes').order_by('pergunta__ordem')
    return render(request, 'vagas/candidatura_respostas.html', {
        'candidatura': candidatura, 'detalhes': detalhes,
    })


# --------------------------------------------------------------------------
# Fluxo público por link de UUID (sem @login_required) — candidato acessa
# via CPF, sem VPN. Isolado do painel interno.
# --------------------------------------------------------------------------

def _vaga_sem_perguntas(vaga):
    return vaga.perguntas.count() < 1


def _mensagem_status(vaga):
    """Aviso exibido na tela de CPF quando a vaga não está aberta a candidaturas."""
    #if _vaga_sem_perguntas(vaga):
       #return 'Esta vaga não está disponível no momento.'
    if vaga.status == Vaga.Status.AGENDADA:
        data = timezone.localtime(vaga.data_abertura).strftime('%d/%m/%Y às %H:%M')
        return f'As candidaturas para esta vaga ainda não começaram. Elas se iniciam em {data}.'
    if vaga.status == Vaga.Status.ENCERRADA:
        return 'O prazo de candidaturas para esta vaga já foi encerrado.'
    return None


def validar_cpf_candidatura_view(request, vaga_uuid):
    vaga = get_object_or_404(Vaga, link_uuid=vaga_uuid)
    form = ValidarCPFCandidaturaForm(request.POST or None)
    aviso = _mensagem_status(vaga)
    pode_candidatar = aviso is None
    erro = None

    if request.method == 'POST' and pode_candidatar and form.is_valid():
        documento = form.cleaned_data['documento']
        colaborador = Colaborador.objects.filter(documento=documento, ativo=True).first()
        if not colaborador:
            erro = 'CPF não encontrado entre os colaboradores ativos.'
        elif Candidatura.objects.filter(vaga=vaga, colaborador=colaborador).exists():
            erro = 'Você já se candidatou a esta vaga.'
        else:
            request.session[f'vaga_auth_{vaga_uuid}'] = colaborador.pk
            return redirect('vagas:submeter_candidatura', vaga_uuid=vaga_uuid)

    return render(request, 'vagas/validar_cpf.html', {
        'vaga': vaga, 'form': form, 'erro': erro, 'aviso': aviso, 'pode_candidatar': pode_candidatar,
        'hide_sidebar': True,
    })


def submeter_candidatura_view(request, vaga_uuid):
    vaga = get_object_or_404(Vaga, link_uuid=vaga_uuid)
    colaborador_pk = request.session.get(f'vaga_auth_{vaga_uuid}')
    if _mensagem_status(vaga) or not colaborador_pk:
        messages.error(request, _mensagem_status(vaga) or 'Valide seu CPF antes de se candidatar.')
        return redirect('vagas:candidatar', vaga_uuid=vaga_uuid)

    colaborador = get_object_or_404(Colaborador, pk=colaborador_pk, ativo=True)
    if Candidatura.objects.filter(vaga=vaga, colaborador=colaborador).exists():
        request.session[f'vaga_concluida_{vaga_uuid}'] = True
        return redirect('vagas:agradecimento', vaga_uuid=vaga_uuid)

    form = CandidaturaForm(vaga, request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                candidatura = Candidatura.objects.create(
                    vaga=vaga, colaborador=colaborador,
                    hash_documento_candidato=gerar_hash_documento(colaborador.documento),
                )
                for pergunta in form.perguntas:
                    valor = form.cleaned_data.get(f'pergunta_{pergunta.pk}')
                    detalhe = DetalheRespostaCandidatura.objects.create(
                        candidatura=candidatura, pergunta=pergunta,
                        valor_texto=valor if pergunta.tipo_resposta == PerguntaVaga.TipoResposta.TEXTO else None,
                        valor_inteiro=valor if pergunta.tipo_resposta == PerguntaVaga.TipoResposta.NOTA_0_10 else None,
                    )
                    if pergunta.tipo_resposta == PerguntaVaga.TipoResposta.RADIO and valor is not None:
                        detalhe.opcoes.set([valor])
                    elif pergunta.tipo_resposta == PerguntaVaga.TipoResposta.CHECKBOX and valor:
                        detalhe.opcoes.set(valor)
        except IntegrityError:
            pass
        request.session.pop(f'vaga_auth_{vaga_uuid}', None)
        request.session[f'vaga_concluida_{vaga_uuid}'] = True
        return redirect('vagas:agradecimento', vaga_uuid=vaga_uuid)

    return render(request, 'vagas/candidatura_form.html', {
        'vaga': vaga, 'form': form, 'hide_sidebar': True,
    })


def agradecimento(request, vaga_uuid):
    vaga = get_object_or_404(Vaga, link_uuid=vaga_uuid)
    if not request.session.get(f'vaga_concluida_{vaga_uuid}', False):
        return redirect('vagas:candidatar', vaga_uuid=vaga_uuid)
    return render(request, 'vagas/agradecimento.html', {'vaga': vaga, 'hide_sidebar': True})
