"""Telas do módulo de currículos: upload, lista, detalhe, PDF protegido e reprocessamento."""
import hashlib
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import FileResponse, Http404, JsonResponse
from django.utils import timezone
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from organizacao.models import Cargo
from pesquisas.permissoes import (
    TALENTOS_BUSCA, TALENTOS_CURRICULOS, TALENTOS_DASHBOARD, TALENTOS_KANBAN, modulo_required,
)

from .erros import ErroProcessamento
from .extracao import validar_upload
from .busca import buscar
from .forms import BuscaForm, CandidatoForm
from .fluxo import inativar_candidato, mover_candidato, reativar_candidato
from .historico import Evento, registrar, registrar_edicao
from .models import Candidato, HistoricoCandidatura
from .pipeline import processar_candidato

logger = logging.getLogger('talentos')

acesso_curriculos = modulo_required(TALENTOS_CURRICULOS)
LIMITE_HISTORICO = 100


def _sha256(arquivo):
    resumo = hashlib.sha256()
    for bloco in arquivo.chunks():
        resumo.update(bloco)
    arquivo.seek(0)
    return resumo.hexdigest()


@acesso_curriculos
def upload(request):
    if request.method == 'POST':
        arquivos = request.FILES.getlist('curriculos')
        if not arquivos:
            messages.error(request, 'Selecione ao menos um arquivo PDF.')
        elif len(arquivos) > settings.CURRICULO_MAX_ARQUIVOS:
            messages.error(request, f'Envie no máximo {settings.CURRICULO_MAX_ARQUIVOS} arquivos por vez.')
        else:
            ultimo = None
            for arquivo in arquivos:
                ultimo = _processar_arquivo(request, arquivo) or ultimo
            # if len(arquivos) == 1 and ultimo:
            #    return redirect('talentos:detalhe', pk=ultimo.pk)
            return redirect('talentos:lista')
    return render(request, 'talentos/upload.html', {
        'max_mb': settings.CURRICULO_MAX_MB, 'max_arquivos': settings.CURRICULO_MAX_ARQUIVOS,
    })


def _processar_arquivo(request, arquivo):
    """Valida, grava e processa um PDF. Retorna o Candidato final (ou None se rejeitado
    ou se o processamento falhou). Se o currículo pertencer a alguém já cadastrado
    (mesmo e-mail, telefone ou nome), o Candidato devolvido é o cadastro já existente,
    atualizado — não um novo registro (upsert, ver `duplicidade.fundir_duplicata`)."""
    nome = arquivo.name[:255]
    try:
        validar_upload(arquivo)
    except ErroProcessamento as erro:
        messages.error(request, f'{nome}: {erro}')
        return None

    hash_arquivo = _sha256(arquivo)
    existente = Candidato.all_objects.filter(hash_arquivo=hash_arquivo).first()
    if existente:
        messages.warning(request, f'{nome}: este currículo já foi enviado (candidato #{existente.pk}).')
        return None

    candidato = Candidato.objects.create(
        curriculo=arquivo, nome_arquivo=nome, hash_arquivo=hash_arquivo, enviado_por_id=request.user.pk if request.user.is_authenticated else None,
    )
    registrar(candidato, Evento.CURRICULO_ENVIADO, request.user, f'Arquivo "{nome}" enviado.')
    resultado = processar_candidato(candidato, request.user)
    if resultado:
        messages.success(request, f'{nome}: currículo processado.')
        return resultado
    messages.error(request, f'{nome}: {candidato.erro_processamento}')
    return None


@acesso_curriculos
def lista(request):
    # Usa all_objects para permitir visualizar tanto ativos quanto inativos
    candidatos = Candidato.all_objects.all()
    
    termo = request.GET.get('q', '').strip()
    if termo:
        candidatos = candidatos.filter(
            Q(nome__icontains=termo) | Q(email__icontains=termo) | Q(cidade__icontains=termo)
            | Q(nome_arquivo__icontains=termo)
        )
        
    processamento = request.GET.get('processamento', '')
    if processamento in Candidato.Processamento.values:
        candidatos = candidatos.filter(processamento=processamento)

    # Filtro de situação na listagem (ativos, inativos, todos)
    situacao = request.GET.get('situacao', 'todos')
    if situacao == 'ativos':
        candidatos = candidatos.filter(ativo=True)
    elif situacao == 'inativos':
        candidatos = candidatos.filter(ativo=False)
    # Se for 'todos', não aplica filtro restritivo de ativo/inativo

    pagina = Paginator(candidatos, 25).get_page(request.GET.get('page'))
    querystring = request.GET.copy()
    querystring.pop('page', None)
    
    return render(request, 'talentos/lista.html', {
        'pagina': pagina, 
        'termo': termo, 
        'processamento_selecionado': processamento,
        'situacao_selecionada': situacao,
        'processamentos': Candidato.Processamento.choices, 
        'querystring': querystring.urlencode(),
    })

@acesso_curriculos
def detalhe(request, pk):
    # all_objects: a tela de detalhe precisa continuar acessível para candidatos
    # inativos (para consulta e para a ação de reativar).
    candidato = get_object_or_404(Candidato.all_objects, pk=pk)
    cargo_anterior = candidato.cargo  # Usa a @property que busca do banco default
    form = CandidatoForm(request.POST or None, instance=candidato)
    if request.method == 'POST' and form.is_valid():
        # Aprovação da Pendência: currículo inativo que a IA não conseguiu direcionar. Se o
        # analista agora definiu um cargo, ele "aprova" o candidato — sai da Pendência,
        # entra no funil normal (Triagem) e volta a ficar ativo/visível nas buscas.
        aguardando_aprovacao = candidato.status == Candidato.Status.PENDENCIA and not candidato.ativo
        candidato = form.save()
        registrar_edicao(candidato, request.user, form.changed_data, cargo_anterior)
        if aguardando_aprovacao and candidato.cargo_id:
            mover_candidato(candidato, Candidato.Status.TRIAGEM, request.user)
            candidato = reativar_candidato(candidato, request.user)
            messages.success(request, 'Cargo definido: candidato reativado e movido para Triagem.')
        else:
            messages.success(request, 'Dados do candidato atualizados.')
        return redirect('talentos:detalhe', pk=pk)
    return render(request, 'talentos/detalhe.html', {
        'candidato': candidato, 'form': form,
        'historico': candidato.historico.all()[:LIMITE_HISTORICO],
        'total_historico': candidato.historico.count(),
        'limite_historico': LIMITE_HISTORICO,
    })


@acesso_curriculos
@require_POST
def inativar(request, pk):
    candidato = get_object_or_404(Candidato.all_objects, pk=pk)
    try:
        inativar_candidato(candidato, request.POST.get('motivo_inativacao', ''), request.user)
    except ValidationError as erro:
        messages.error(request, ' '.join(erro.messages))
    else:
        messages.success(request, 'Candidato inativado.')
    return redirect('talentos:detalhe', pk=pk)


@acesso_curriculos
@require_POST
def reativar(request, pk):
    candidato = get_object_or_404(Candidato.all_objects, pk=pk)
    reativar_candidato(candidato, request.user)
    messages.success(request, 'Candidato reativado.')
    return redirect('talentos:detalhe', pk=pk)


@acesso_curriculos
def pdf(request, pk):
    """Entrega o PDF somente a usuários autorizados (o arquivo não está em /media/)."""
    candidato = get_object_or_404(Candidato.all_objects, pk=pk)
    try:
        arquivo = candidato.curriculo.open('rb')
    except FileNotFoundError:
        raise Http404('Arquivo não encontrado.')
    resposta = FileResponse(arquivo, content_type='application/pdf')
    resposta['Content-Disposition'] = f'inline; filename="curriculo-{candidato.pk}.pdf"'
    resposta['Cache-Control'] = 'private, no-store'
    return resposta


@acesso_curriculos
@require_POST
def reprocessar(request, pk):
    candidato = get_object_or_404(Candidato.all_objects, pk=pk)
    registrar(candidato, Evento.REPROCESSAMENTO, request.user, 'Reprocessamento do currículo solicitado.')
    resultado = processar_candidato(candidato, request.user)
    if resultado:
        messages.success(request, 'Currículo reprocessado com sucesso.')
        return redirect('talentos:detalhe', pk=resultado.pk)
    messages.error(request, candidato.erro_processamento)
    return redirect('talentos:detalhe', pk=pk)


@acesso_curriculos
@require_POST
def excluir(request, pk):
    """Exclusão definitiva (direito de eliminação da LGPD): remove o registro e o PDF."""
    candidato = get_object_or_404(Candidato.all_objects, pk=pk)
    logger.info('Candidato id=%s excluído por %s', candidato.pk, request.user.username)
    candidato.curriculo.delete(save=False)
    candidato.delete()
    messages.success(request, 'Candidato e currículo excluídos.')
    return redirect('talentos:lista')


# --- Dashboard ---------------------------------------------------------------
@modulo_required(TALENTOS_DASHBOARD)
def painel(request):
    candidatos = Candidato.objects.all()
    por_status = dict(candidatos.values_list('status').annotate(total=Count('pk')).order_by())
    total = sum(por_status.values())
    funil = [
        {'valor': valor, 'rotulo': rotulo, 'total': por_status.get(valor, 0),
         'percentual': round(por_status.get(valor, 0) / total * 100) if total else 0}
        for valor, rotulo in Candidato.Status.choices
    ]
    
    # Mapeia departamentos do banco 'default' para compatibilidade multi-database
    cargos_map = {
        c.id: c.secao.departamento.nome 
        for c in Cargo.objects.using('default').select_related('secao__departamento').all()
    }
    
    dept_counts = {}
    for c in candidatos.filter(processamento=Candidato.Processamento.CONCLUIDO):
        cargo_id_efetivo = c.cargo_id or c.cargo_sugerido_id
        dept = cargos_map.get(cargo_id_efetivo, 'Outros') if cargo_id_efetivo else 'Não definido'
        dept_counts[dept] = dept_counts.get(dept, 0) + 1
        
    por_departamento = [
        {'departamento': k, 'total': v} 
        for k, v in sorted(dept_counts.items(), key=lambda x: x[1], reverse=True)[:6]
    ]

    por_cidade = list(
        candidatos.exclude(cidade='').values('cidade').annotate(total=Count('pk')).order_by('-total', 'cidade')[:6]
    )
    ativos = total - por_status.get(Candidato.Status.REPROVADO, 0) - por_status.get(Candidato.Status.CONTRATADO, 0)
    
    # Quantidade de currículos inativos (usando all_objects para pegar os marcados como inativos=False)
    inativos = Candidato.all_objects.filter(ativo=False).count()

    return render(request, 'talentos/painel.html', {
        'total': total,
        'em_andamento': ativos,
        'contratados': por_status.get(Candidato.Status.CONTRATADO, 0),
        'inativos': inativos,  # <-- Nova métrica repassada ao template
        'recebidos_30_dias': candidatos.filter(criado_em__gte=timezone.now() - timedelta(days=30)).count(),
        'com_erro': candidatos.filter(processamento=Candidato.Processamento.ERRO).count(),
        'funil': funil,
        'por_departamento': por_departamento,
        'maximo_departamento': max((d['total'] for d in por_departamento), default=1),
        'por_cidade': por_cidade,
        'maximo_cidade': max((c['total'] for c in por_cidade), default=1),
        'recentes': list(candidatos[:6]),
        'atividade': HistoricoCandidatura.objects.select_related('candidato')[:8],
    })

# --- Busca semântica ---------------------------------------------------------
@modulo_required(TALENTOS_BUSCA)
def busca(request):
    form = BuscaForm(request.GET or None)
    resultados = erro = None
    
    if request.GET and form.is_valid():
        try:
            # Copia os dados limpos do formulário para passar à função de busca
            dados = form.cleaned_data.copy()
            texto_busca = dados.pop('q', '')
            
            # A função 'buscar' (em talentos/busca.py) já faz todo o trabalho pesado:
            # 1. Se 'texto_busca' estiver vazio, ela retorna direto do banco (CUSTO ZERO DE IA).
            # 2. Se houver texto, ela aciona a IA ranqueando por similaridade vetorial.
            # 3. Ela respeita o parâmetro 'situacao' (ativos, inativos, todos) usando o gerenciador correto.
            resultados = buscar(texto=texto_busca, **dados)
            
        except ErroProcessamento as falha:
            erro = str(falha)
            
    return render(request, 'talentos/busca.html', {
        'form': form, 'resultados': resultados, 'erro': erro,
        'com_texto': bool(request.GET.get('q', '').strip()),
    })

# --- Kanban ------------------------------------------------------------------
LIMITE_CARTOES_POR_COLUNA = 50


@modulo_required(TALENTOS_KANBAN)
def quadro(request):
    termo = request.GET.get('q', '').strip()

    def _filtrar(base):
        if termo:
            return base.filter(Q(nome__icontains=termo) | Q(cidade__icontains=termo) | Q(nome_arquivo__icontains=termo))
        return base

    base_ativos = _filtrar(Candidato.objects.all())
    # A Pendência é, por definição, sempre inativa (a IA não conseguiu direcionar um cargo com
    # confiança e o candidato aguarda o RH aprovar manualmente) — se essa coluna usasse o
    # manager padrão (só ativos), ficaria sempre vazia e o RH nunca veria o que precisa revisar.
    base_pendencia = _filtrar(Candidato.all_objects.filter(status=Candidato.Status.PENDENCIA))

    colunas = []
    for valor, rotulo in Candidato.Status.choices:
        base = base_pendencia if valor == Candidato.Status.PENDENCIA else base_ativos
        da_coluna = base.filter(status=valor).order_by('-atualizado_em')
        colunas.append({
            'valor': valor, 'rotulo': rotulo, 'total': da_coluna.count(),
            'cartoes': list(da_coluna[:LIMITE_CARTOES_POR_COLUNA]),
        })
    return render(request, 'talentos/quadro.html', {
        'colunas': colunas, 'termo': termo, 'etapas': Candidato.Status.choices,
        'limite': LIMITE_CARTOES_POR_COLUNA,
    })


@modulo_required(TALENTOS_KANBAN)
@require_POST
def mover(request, pk):
    """Endpoint AJAX do drag-and-drop: move o candidato para a etapa informada em `status`."""
    candidato = get_object_or_404(Candidato.all_objects, pk=pk)
    if not candidato.ativo:
        # Candidato em Pendência (sempre inativo): não deixa "pular" a etapa arrastando o
        # card, sem antes o RH aprovar de verdade definindo um cargo na tela de detalhe.
        return JsonResponse(
            {'ok': False, 'erro': 'Candidato inativo: defina um cargo na tela de detalhe para aprová-lo e movê-lo.'},
            status=400,
        )
    try:
        mudou = mover_candidato(candidato, request.POST.get('status', ''), request.user)
    except ValueError:
        return JsonResponse({'ok': False, 'erro': 'Etapa inválida.'}, status=400)
    contagens = dict(Candidato.objects.values_list('status').annotate(total=Count('pk')).order_by())
    return JsonResponse({'ok': True, 'mudou': mudou, 'status': candidato.status, 'contagens': contagens})