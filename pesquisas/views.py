import csv
import json
from datetime import timedelta

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.db.models import Count, ProtectedError, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST
from django.core.exceptions import PermissionDenied, ValidationError

from desligamentos.models import FormularioDesligamento, RespostaDesligamento

from .forms import ColaboradorForm, ImportarColaboradoresForm, PerfilAcessoForm, PerguntaFormSet, PesquisaForm, ResponderPerguntaForm, ValidarCPFForm
from .models import Colaborador, DetalheResposta, Empresa, ImportacaoColaboradores, PerfilAcesso, Pergunta, Pesquisa, RespostaPesquisa
from .permissoes import DESLIGAMENTOS, ENPS, GERAL, administrador_required, modulo_required, pagina_inicial, usuario_tem_acesso
from .services import gerar_hash_documento


def _formularios_desligamento_por_iniciativa():
    from desligamentos.models import FormularioDesligamento
    formularios = FormularioDesligamento.objects.all()
    return {
        'formularios_empresa': [f for f in formularios if f.iniciativa == FormularioDesligamento.Iniciativa.EMPRESA],
        'formularios_colaborador': [f for f in formularios if f.iniciativa == FormularioDesligamento.Iniciativa.COLABORADOR],
    }


def _validar_formulario_desligamento(request):
    from desligamentos.models import FormularioDesligamento

    iniciativa = request.POST.get('iniciativa', '').strip()
    if iniciativa not in FormularioDesligamento.Iniciativa.values:
        messages.error(request, 'Selecione o motivo do desligamento (iniciativa da empresa ou do colaborador).')
        return None, None
    formulario = FormularioDesligamento.objects.filter(
        pk=request.POST.get('formulario_id', '').strip() or None, iniciativa=iniciativa,
    ).first()
    if not formulario:
        messages.error(request, 'Selecione um formulário de desligamento válido para a iniciativa informada.')
        return None, None
    return iniciativa, formulario


def _status_pesquisa(pesquisa):
    agora = timezone.now()
    if agora < pesquisa.data_inicio:
        return 'Agendada'
    if agora > pesquisa.data_final:
        return 'Encerrada'
    return 'Em andamento'


@modulo_required()
def dashboard(request):
    # O dashboard reúne métricas de eNPS e desligamento; quem não tem esses módulos
    # é encaminhado à primeira página a que tem direito.
    if not usuario_tem_acesso(request.user, GERAL, ENPS, DESLIGAMENTOS):
        destino = pagina_inicial(request.user)
        if destino is None:
            raise PermissionDenied
        return redirect(destino)
    contexto = {
        'total_colaboradores': Colaborador.objects.filter(ativo=True).count(),
        'total_colaboradores_inativos': Colaborador.objects.filter(ativo=False).count(),
        'total_pesquisas': Pesquisa.objects.count(),
        'total_respostas': RespostaPesquisa.objects.count(),
        'total_formularios_desligamento': FormularioDesligamento.objects.count(),
        'total_respostas_desligamento': RespostaDesligamento.objects.count(),
        'pesquisas': Pesquisa.objects.annotate(total_respostas=Count('respostas'))[:8],
        'desligamentos_recentes': FormularioDesligamento.objects.all()[:5],
    }
    return render(request, 'pesquisas/dashboard.html', contexto)


def _aplicar_desligamento(colaborador, iniciativa, formulario, usuario):
    """Depois de gravada a data de demissão: agenda (data futura) ou inativa já (até hoje).

    Sem `iniciativa`/`formulario`, mantém o motivo do desligamento já agendado.
    Devolve a mensagem para o usuário.
    """
    from desligamentos.services import (
        agendar_desligamento, desligamento_agendado, efetivar_desligamento, registrar_desligamento_pendente,
    )

    if colaborador.data_demissao > timezone.localdate():
        agendar_desligamento(colaborador, iniciativa, formulario, usuario)
        return (
            f'Desligamento agendado: {colaborador.nome} continua ativo e será inativado em '
            f'{colaborador.data_demissao:%d/%m/%Y}, quando o card irá para "Pendentes" no Kanban de desligamentos.'
        )

    colaborador.ativo = False
    colaborador.data_inativacao = timezone.now()
    colaborador.save()
    agendado = desligamento_agendado(colaborador)
    if agendado:
        efetivar_desligamento(agendado, iniciativa, formulario)
    else:
        registrar_desligamento_pendente(colaborador, iniciativa, formulario, usuario)
    return f'{colaborador.nome} agora está inativo. Um card foi criado na coluna "Pendentes" do Kanban de desligamentos.'


def _filtrar_colaboradores(request):
    """Aplica os filtros da listagem (busca, empresa e status); usados também na exportação."""
    termo = request.GET.get('q', '').strip()
    empresa_id = request.GET.get('empresa', '').strip()
    status = request.GET.get('status', '').strip()

    colaboradores = Colaborador.objects.select_related('usuario').prefetch_related('empresas')

    if termo:
        colaboradores = colaboradores.filter(
            Q(nome__icontains=termo) | Q(documento__icontains=termo) | Q(empresas__nome__icontains=termo)
        ).distinct()

    if empresa_id.isdigit():
        colaboradores = colaboradores.filter(empresas=empresa_id).distinct()

    if status == 'ativo':
        colaboradores = colaboradores.filter(ativo=True)
    elif status == 'inativo':
        colaboradores = colaboradores.filter(ativo=False)

    return colaboradores, termo, empresa_id, status


@modulo_required(GERAL)
def colaborador_lista(request):
    from desligamentos.services import processar_desligamentos_agendados

    # Além da rotina de hora em hora: quem chegou à data de demissão já aparece inativo.
    processar_desligamentos_agendados()
    colaboradores, termo, empresa_id, status = _filtrar_colaboradores(request)

    return render(request, 'pesquisas/colaborador_lista.html', {
        'colaboradores': colaboradores,
        'termo': termo,
        'empresas': Empresa.objects.all(),
        'empresa_selecionada': empresa_id,
        'status_selecionado': status,
        **_formularios_desligamento_por_iniciativa(),
    })


COLUNAS_EXPORTACAO = (
    'Nome', 'CPF / CNPJ', 'Empresas', 'Status', 'Celulares', 'E-mail', 'Data de nascimento',
    'Data de contratação', 'Data de demissão', 'É gestor', 'Gestor responsável',
)


@modulo_required(GERAL)
def colaborador_exportar(request):
    """Exporta os colaboradores da listagem, respeitando os filtros aplicados, em XLSX (padrão) ou CSV."""
    from desligamentos.services import processar_desligamentos_agendados

    processar_desligamentos_agendados()
    colaboradores, *_ = _filtrar_colaboradores(request)
    colaboradores = colaboradores.select_related('gestor')
    linhas = [
        [
            c.nome, c.documento, c.nomes_empresas, 'Ativo' if c.ativo else 'Inativo',
            ' / '.join(c.celulares or []), c.email, c.data_nascimento, c.data_contratacao, c.data_demissao,
            'Sim' if c.eh_gestor else 'Não', c.gestor.nome if c.gestor else '',
        ]
        for c in colaboradores
    ]
    nome_arquivo = f'colaboradores_{timezone.localdate():%Y-%m-%d}'

    if request.GET.get('formato') == 'csv':
        resposta = HttpResponse(content_type='text/csv; charset=utf-8')
        resposta['Content-Disposition'] = f'attachment; filename="{nome_arquivo}.csv"'
        resposta.write('﻿')  # BOM: o Excel abre com acentos corretos
        writer = csv.writer(resposta, delimiter=';')
        writer.writerow(COLUNAS_EXPORTACAO)
        for linha in linhas:
            writer.writerow([valor.strftime('%d/%m/%Y') if hasattr(valor, 'strftime') else valor for valor in linha])
        return resposta

    from openpyxl import Workbook
    from openpyxl.styles import Font

    pasta = Workbook()
    planilha = pasta.active
    planilha.title = 'Colaboradores'
    planilha.append(COLUNAS_EXPORTACAO)
    for celula in planilha[1]:
        celula.font = Font(bold=True)
    for linha in linhas:
        planilha.append(linha)
    for coluna in ('G', 'H', 'I'):
        for celula in planilha[coluna][1:]:
            celula.number_format = 'DD/MM/YYYY'
    # CPF/CNPJ como texto, para o Excel não remover zeros à esquerda.
    for celula in planilha['B'][1:]:
        celula.number_format = '@'
    for indice, largura in enumerate((35, 18, 30, 10, 30, 30, 18, 18, 18, 10, 30), start=1):
        planilha.column_dimensions[planilha.cell(row=1, column=indice).column_letter].width = largura
    planilha.freeze_panes = 'A2'
    planilha.auto_filter.ref = planilha.dimensions

    resposta = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resposta['Content-Disposition'] = f'attachment; filename="{nome_arquivo}.xlsx"'
    pasta.save(resposta)
    return resposta


@modulo_required(GERAL, ENPS, DESLIGAMENTOS)
def colaborador_criar(request):
    form = ColaboradorForm(request.POST or None, usuario_logado=request.user)
    if request.method == 'POST' and form.is_valid():
        colaborador = form.save()
        colaborador.data_ativacao = timezone.now()
        # Cadastrado já com data de demissão: entra inativo.
        colaborador.data_inativacao = None if colaborador.ativo else colaborador.data_ativacao
        colaborador.save(update_fields=['data_ativacao', 'data_inativacao'])
        messages.success(request, 'Colaborador cadastrado com sucesso.')
        return redirect('pesquisas:colaborador_lista')
    return render(request, 'pesquisas/colaborador_form.html', {'form': form})


@modulo_required(GERAL, ENPS, DESLIGAMENTOS)
def colaborador_importar(request):
    """Passo 1 da importação: lê a planilha e guarda as linhas para revisão, sem gravar no cadastro."""
    from .importacao import ErroImportacao, preparar_linhas

    form = ImportarColaboradoresForm(request.POST or None, request.FILES or None)
    erros = []
    if request.method == 'POST' and form.is_valid():
        arquivo = form.cleaned_data['arquivo']
        try:
            linhas, colunas_ignoradas = preparar_linhas(arquivo.read(), arquivo.name)
        except ErroImportacao as exc:
            erros = exc.erros
        else:
            # Revisões abandonadas não precisam ficar guardadas.
            ImportacaoColaboradores.objects.filter(criada_em__lt=timezone.now() - timedelta(days=2)).delete()
            importacao = ImportacaoColaboradores.objects.create(
                usuario=request.user, nome_arquivo=arquivo.name[:255], linhas=linhas, colunas_ignoradas=colunas_ignoradas,
            )
            return redirect('pesquisas:colaborador_importar_revisao', pk=importacao.pk)
    return render(request, 'pesquisas/colaborador_importar.html', {'form': form, 'erros': erros})


def _importacao_do_usuario(request, pk):
    # Cada revisão só é visível para quem enviou a planilha.
    return get_object_or_404(ImportacaoColaboradores, pk=pk, usuario=request.user)


def _estado_importacao(importacao):
    from .importacao import validar_linhas

    erros = validar_linhas(importacao.linhas)
    return {'erros': erros, 'total': len(importacao.linhas), 'com_erro': len(erros)}


@modulo_required(GERAL, ENPS, DESLIGAMENTOS)
def colaborador_importar_revisao(request, pk):
    """Passo 2: mostra as linhas válidas e as com erro; as com erro são corrigidas direto na tabela."""
    from .importacao import COLUNAS, ROTULOS

    importacao = _importacao_do_usuario(request, pk)
    estado = _estado_importacao(importacao)
    return render(request, 'pesquisas/colaborador_importar_revisao.html', {
        'importacao': importacao,
        'colunas': [{'chave': coluna, 'rotulo': ROTULOS[coluna]} for coluna in COLUNAS],
        'estado': estado,
        'validos': estado['total'] - estado['com_erro'],
    })


@modulo_required(GERAL, ENPS, DESLIGAMENTOS)
@require_POST
def colaborador_importar_celula(request, pk):
    """Altera uma célula da revisão e devolve a validação de todas as linhas (uma correção pode afetar outras)."""
    from .importacao import COLUNAS

    importacao = _importacao_do_usuario(request, pk)
    try:
        dados = json.loads(request.body)
        numero, coluna = int(dados['numero']), dados['coluna']
        valor = ' '.join(str(dados.get('valor') or '').split())[:1000]
    except (ValueError, KeyError, TypeError):
        return JsonResponse({'erro': 'Requisição inválida.'}, status=400)
    if coluna not in COLUNAS:
        return JsonResponse({'erro': 'Coluna inválida.'}, status=400)
    linha = next((linha for linha in importacao.linhas if linha['numero'] == numero), None)
    if linha is None:
        return JsonResponse({'erro': 'Esta linha não está mais na importação.'}, status=404)

    linha['valores'][coluna] = valor
    importacao.save(update_fields=['linhas'])
    return JsonResponse({**_estado_importacao(importacao), 'valor': valor})


@modulo_required(GERAL, ENPS, DESLIGAMENTOS)
@require_POST
def colaborador_importar_descartar(request, pk):
    """Remove linhas da importação: as indicadas em `numeros` ou, com `com_erro`, todas as que têm erro."""
    from .importacao import validar_linhas

    importacao = _importacao_do_usuario(request, pk)
    try:
        dados = json.loads(request.body)
        if dados.get('com_erro'):
            numeros = set(validar_linhas(importacao.linhas))
        else:
            numeros = {int(numero) for numero in dados.get('numeros', [])}
    except (ValueError, TypeError, AttributeError):
        return JsonResponse({'erro': 'Requisição inválida.'}, status=400)

    importacao.linhas = [linha for linha in importacao.linhas if linha['numero'] not in numeros]
    importacao.save(update_fields=['linhas'])
    return JsonResponse({**_estado_importacao(importacao), 'descartadas': sorted(numeros)})


@modulo_required(GERAL, ENPS, DESLIGAMENTOS)
@require_POST
def colaborador_importar_confirmar(request, pk):
    """Passo 3: grava no cadastro só as linhas válidas; as com erro ficam de fora."""
    from .importacao import ErroImportacao, gravar_linhas, validar_linhas

    importacao = _importacao_do_usuario(request, pk)
    erros = validar_linhas(importacao.linhas)
    if len(erros) == len(importacao.linhas):
        messages.error(request, 'Não há linhas válidas para importar.')
        return redirect('pesquisas:colaborador_importar_revisao', pk=pk)
    try:
        with transaction.atomic():
            resultado = gravar_linhas(importacao.linhas, erros)
            importacao.delete()
    except ErroImportacao as exc:
        messages.error(request, 'Nenhum colaborador foi importado: ' + ' '.join(exc.erros))
        return redirect('pesquisas:colaborador_importar_revisao', pk=pk)

    mensagem = f'Importação concluída: {resultado.criados} criado(s) e {resultado.atualizados} atualizado(s).'
    if resultado.ignoradas:
        mensagem += f' {resultado.ignoradas} linha(s) com erro não foram importadas.'
    if importacao.colunas_ignoradas:
        mensagem += f' Colunas ignoradas: {", ".join(importacao.colunas_ignoradas)}.'
    messages.success(request, mensagem)
    return redirect('pesquisas:colaborador_lista')


@modulo_required(GERAL, ENPS, DESLIGAMENTOS)
@require_POST
def colaborador_importar_cancelar(request, pk):
    _importacao_do_usuario(request, pk).delete()
    messages.info(request, 'Importação cancelada. Nenhum colaborador foi alterado.')
    return redirect('pesquisas:colaborador_importar')


@modulo_required(GERAL, ENPS, DESLIGAMENTOS)
def colaborador_modelo_importacao(request):
    from .importacao import COLUNAS_OBRIGATORIAS, COLUNAS_OPCIONAIS

    resposta = HttpResponse(content_type='text/csv; charset=utf-8')
    resposta['Content-Disposition'] = 'attachment; filename="modelo_importacao_colaboradores.csv"'
    resposta.write('﻿')  # BOM: o Excel abre com acentos corretos
    writer = csv.writer(resposta, delimiter=';')
    writer.writerow(COLUNAS_OBRIGATORIAS + COLUNAS_OPCIONAIS)
    writer.writerow(['Maria Gestora', '52998224725', 'Matriz', '(47) 99999-8888 / (47) 98888-7777', 'maria@exemplo.com.br',
                     '20/05/1985', '01/03/2024', '', 'Sim', ''])
    writer.writerow(['João Silva', '11144477735', 'Matriz', '', '', '', '15/09/2026', '', 'Não', 'Maria Gestora'])
    return resposta


@modulo_required(GERAL, ENPS, DESLIGAMENTOS)
def colaborador_editar(request, pk):
    from desligamentos.services import cancelar_desligamento_agendado, cancelar_desligamentos_abertos, desligamento_agendado

    colaborador = get_object_or_404(Colaborador, pk=pk)
    estava_ativo = colaborador.ativo
    agendado = desligamento_agendado(colaborador) if estava_ativo else None
    form = ColaboradorForm(request.POST or None, instance=colaborador, usuario_logado=request.user)
    contexto = {
        'form': form, 'colaborador': colaborador, 'desligamento_agendado': agendado,
        **_formularios_desligamento_por_iniciativa(),
    }
    if request.method == 'POST' and form.is_valid():
        # Ativo com data de demissão (hoje, passada ou futura) = desligamento. O motivo é pedido uma vez:
        # se já houver desligamento agendado, só é trocado quando um novo motivo for enviado.
        desligando = estava_ativo and form.cleaned_data['data_demissao'] is not None
        iniciativa = formulario = None
        if desligando and (agendado is None or request.POST.get('iniciativa')):
            iniciativa, formulario = _validar_formulario_desligamento(request)
            if formulario is None:
                return render(request, 'pesquisas/colaborador_form.html', contexto)

        mensagem = 'Colaborador atualizado com sucesso.'
        with transaction.atomic():
            colaborador = form.save()
            if desligando:
                mensagem = _aplicar_desligamento(colaborador, iniciativa, formulario, request.user)
            elif agendado:
                # A data de demissão futura foi apagada.
                cancelar_desligamento_agendado(colaborador)
                mensagem = 'Colaborador atualizado. O desligamento agendado foi cancelado.'
            elif not estava_ativo and colaborador.ativo:
                colaborador.data_inativacao = None
                colaborador.data_ativacao = timezone.now()
                colaborador.save(update_fields=['data_inativacao', 'data_ativacao'])
                cancelar_desligamentos_abertos(colaborador)

        messages.success(request, mensagem)
        return redirect('pesquisas:colaborador_lista')
    return render(request, 'pesquisas/colaborador_form.html', contexto)


@modulo_required(GERAL, ENPS, DESLIGAMENTOS)
@require_POST
def colaborador_alterar_status(request, pk):
    from desligamentos.services import cancelar_desligamentos_abertos

    colaborador = get_object_or_404(Colaborador, pk=pk)
    agora = timezone.now()

    if colaborador.ativo:
        iniciativa, formulario = _validar_formulario_desligamento(request)
        if formulario is None:
            return redirect('pesquisas:colaborador_lista')
        try:
            data_demissao = parse_date(request.POST.get('data_demissao', '').strip())
        except ValueError:
            data_demissao = None
        if data_demissao is None:
            messages.error(request, f'Informe a data de demissão para inativar {colaborador.nome}.')
            return redirect('pesquisas:colaborador_lista')
        try:
            with transaction.atomic():
                colaborador.data_demissao = data_demissao
                colaborador.save()
                mensagem = _aplicar_desligamento(colaborador, iniciativa, formulario, request.user)
        except ValidationError as exc:
            messages.error(request, ' '.join(exc.messages))
            return redirect('pesquisas:colaborador_lista')
        messages.success(request, mensagem)
    else:
        colaborador.ativo = True
        colaborador.data_demissao = None
        colaborador.data_inativacao = None
        colaborador.data_ativacao = agora
        colaborador.save()
        cancelar_desligamentos_abertos(colaborador)
        messages.success(request, f'{colaborador.nome} agora está ativo.')

    return redirect('pesquisas:colaborador_lista')


@modulo_required(GERAL)
def perfil_lista(request):
    perfis = PerfilAcesso.objects.annotate(total_usuarios=Count('usuarios'))
    return render(request, 'pesquisas/perfil_lista.html', {'perfis': perfis})


@modulo_required(GERAL)
def perfil_criar(request):
    form = PerfilAcessoForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Perfil de acesso cadastrado com sucesso.')
        return redirect('pesquisas:perfil_lista')
    return render(request, 'pesquisas/perfil_form.html', {'form': form})


@modulo_required(GERAL)
def perfil_editar(request, pk):
    perfil = get_object_or_404(PerfilAcesso, pk=pk)
    form = PerfilAcessoForm(request.POST or None, instance=perfil)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Perfil de acesso atualizado com sucesso.')
        return redirect('pesquisas:perfil_lista')
    return render(request, 'pesquisas/perfil_form.html', {'form': form, 'perfil': perfil})


@modulo_required(GERAL)
@require_POST
def perfil_alterar_status(request, pk):
    perfil = get_object_or_404(PerfilAcesso, pk=pk)
    perfil.ativo = not perfil.ativo
    perfil.save(update_fields=['ativo'])
    situacao = 'ativado' if perfil.ativo else 'inativado'
    messages.success(request, f'Perfil "{perfil.nome}" {situacao} com sucesso.')
    return redirect('pesquisas:perfil_lista')


@modulo_required(GERAL)
@require_POST
def perfil_excluir(request, pk):
    perfil = get_object_or_404(PerfilAcesso, pk=pk)
    try:
        perfil.delete()
        messages.success(request, 'Perfil de acesso excluído com sucesso.')
    except ProtectedError:
        messages.error(
            request,
            f'"{perfil.nome}" está vinculado a colaboradores e não pode ser excluído. Inative-o em vez disso.',
        )
    return redirect('pesquisas:perfil_lista')


@modulo_required(ENPS)
def pesquisa_lista(request):
    pesquisas = list(Pesquisa.objects.select_related('criado_por'))
    for pesquisa in pesquisas:
        pesquisa.status_exibicao = _status_pesquisa(pesquisa)
    return render(request, 'pesquisas/pesquisa_lista.html', {'pesquisas': pesquisas})


def _salvar_pesquisa(request, instance=None):
    bloquear_perguntas = instance.edicao_bloqueada if instance else False

    form = PesquisaForm(request.POST or None, instance=instance)
    
    if bloquear_perguntas:
        formset = PerguntaFormSet(None, instance=instance, prefix='perguntas')
        for q_form in formset.forms:
            for field in q_form.fields.values():
                field.widget.attrs['disabled'] = 'disabled'
    else:
        formset = PerguntaFormSet(request.POST or None, instance=instance, prefix='perguntas')

    if request.method == 'POST':
        form_valido = form.is_valid()
        formset_valido = True if bloquear_perguntas else formset.is_valid()

        if form_valido and formset_valido:
            with transaction.atomic():
                pesquisa = form.save(commit=False)
                if not pesquisa.pk:
                    pesquisa.criado_por = request.user
                pesquisa.save()
                
                if not bloquear_perguntas:
                    formset.instance = pesquisa
                    formset.save()

            messages.success(request, 'Pesquisa salva com sucesso.')
            return redirect('pesquisas:pesquisa_lista')
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"Erro no campo {field}: {error}")
            if not bloquear_perguntas:
                for q_form in formset:
                    for field, errors in q_form.errors.items():
                        for error in errors:
                            messages.error(request, f"Erro na pergunta ({field}): {error}")

    return render(request, 'pesquisas/pesquisa_form.html', {
        'form': form, 
        'formset': formset, 
        'pesquisa': instance,
        'bloquear_perguntas': bloquear_perguntas
    })


@modulo_required(ENPS)
def pesquisa_criar(request):
    return _salvar_pesquisa(request)


@modulo_required(ENPS)
def pesquisa_editar(request, pk):
    pesquisa = get_object_or_404(Pesquisa, pk=pk)
    
    if pesquisa.edicao_bloqueada and request.method == 'GET':
        messages.warning(request, 'A edição das perguntas está bloqueada porque a pesquisa já começou. Você pode alterar apenas as informações gerais.')

    return _salvar_pesquisa(request, pesquisa)


@modulo_required(ENPS)
@require_POST
def pesquisa_excluir(request, pk):
    pesquisa = get_object_or_404(Pesquisa, pk=pk)
    try:
        pesquisa.delete()
        messages.success(request, 'Pesquisa excluída com sucesso.')
    except Exception as e:
        messages.error(request, f'Erro ao excluir pesquisa: {str(e)}')
    return redirect('pesquisas:pesquisa_lista')


@modulo_required(ENPS)
def resultados(request, pk):
    pesquisa = get_object_or_404(Pesquisa, pk=pk)
    empresas = Empresa.objects.all()
    empresa_selecionada = None
    empresa_id = request.GET.get('empresa', '').strip()
    if empresa_id:
        try:
            empresa_selecionada = get_object_or_404(Empresa, pk=int(empresa_id))
        except (TypeError, ValueError):
            raise Http404('Empresa inválida.')

    pergunta_enps = pesquisa.perguntas.filter(tipo_resposta=Pergunta.TipoResposta.NOTA_0_10).first()
    
    # Lógica de contagem precisa considerando segundos e horário de ativação/inativação:
    colaboradores_elegiveis = Colaborador.objects.filter(
        data_criacao__lte=pesquisa.data_final
    ).exclude(
        Q(data_ativacao__gt=pesquisa.data_final)
    ).exclude(
        Q(ativo=False) & Q(data_inativacao__lt=pesquisa.data_inicio)
    )
    if empresa_selecionada:
        colaboradores_elegiveis = colaboradores_elegiveis.filter(empresas=empresa_selecionada)

    ativos = colaboradores_elegiveis.count()
    hashes_elegiveis = [
        gerar_hash_documento(documento)
        for documento in colaboradores_elegiveis.values_list('documento', flat=True).iterator()
    ]
    respostas = pesquisa.respostas.filter(hash_documento_respondente__in=hashes_elegiveis)

    respondentes = respostas.count()

    promotores = neutros = detratores = 0
    if pergunta_enps:
        notas = DetalheResposta.objects.filter(
            pergunta=pergunta_enps,
            resposta_pesquisa__in=respostas,
        )
        promotores = notas.filter(valor_inteiro__gte=9).count()
        neutros = notas.filter(valor_inteiro__range=(7, 8)).count()
        detratores = notas.filter(valor_inteiro__range=(0, 6)).count()
        
    percentual = (respondentes / ativos * 100) if ativos else 0
    enps = ((promotores - detratores) / respondentes * 100) if respondentes else 0

    perguntas_demais = []
    qs_perguntas = pesquisa.perguntas.all()
    if pergunta_enps:
        qs_perguntas = qs_perguntas.exclude(pk=pergunta_enps.pk)

    for p in qs_perguntas:
        detalhes = p.detalhes.select_related('resposta_pesquisa').filter(
            resposta_pesquisa__in=respostas,
        )
        media = None
        if p.tipo_resposta == Pergunta.TipoResposta.NOTA_0_10 and detalhes.exists():
            soma = sum(d.valor_inteiro for d in detalhes if d.valor_inteiro is not None)
            media = soma / detalhes.count()
            
        perguntas_demais.append({
            'pergunta': p,
            'detalhes': detalhes,
            'media': media,
        })

    return render(request, 'pesquisas/resultados.html', {
        'pesquisa': pesquisa, 'pergunta_enps': pergunta_enps, 'ativos': ativos,
        'respondentes': respondentes, 'percentual': percentual, 'promotores': promotores,
        'neutros': neutros, 'detratores': detratores, 'enps': enps,
        'perguntas_demais': perguntas_demais,
        'empresas': empresas, 'empresa_selecionada': empresa_selecionada,
    })


def _pesquisa_disponivel(link_uuid):
    pesquisa = get_object_or_404(Pesquisa, link_uuid=link_uuid)
    if not pesquisa.vigente:
        return pesquisa, 'Esta pesquisa ainda não começou ou já foi encerrada.'
    if pesquisa.perguntas.count() < 3:
        return pesquisa, 'Esta pesquisa não está disponível.'
    return pesquisa, None


def validar_participante(request, link_uuid):
    pesquisa, indisponivel = _pesquisa_disponivel(link_uuid)
    form = ValidarCPFForm(request.POST or None)
    erro = indisponivel
    if request.method == 'POST' and not indisponivel and form.is_valid():
        documento = form.cleaned_data['documento']
        if not Colaborador.objects.filter(documento=documento, ativo=True).exists():
            erro = 'CPF/CNPJ não encontrado ou colaborador inativo.'
        else:
            hash_documento = gerar_hash_documento(documento)
            if pesquisa.respostas.filter(hash_documento_respondente=hash_documento).exists():
                erro = 'Uma resposta já foi enviada para este documento.'
            else:
                request.session[f'pesquisa_auth_{link_uuid}'] = hash_documento
                request.session[f'pesquisa_respostas_{link_uuid}'] = {}
                return redirect('pesquisas:responder_etapa', link_uuid=link_uuid, etapa=1)
    return render(request, 'pesquisas/validar_cpf.html', {
        'pesquisa': pesquisa, 'form': form, 'erro': erro, 'hide_sidebar': True,
    })


def responder_etapa(request, link_uuid, etapa):
    pesquisa, indisponivel = _pesquisa_disponivel(link_uuid)
    hash_documento = request.session.get(f'pesquisa_auth_{link_uuid}')
    if indisponivel or not hash_documento:
        messages.error(request, indisponivel or 'Valide seu CPF ou CNPJ antes de responder.')
        return redirect('pesquisas:validar_participante', link_uuid=link_uuid)
    if pesquisa.respostas.filter(hash_documento_respondente=hash_documento).exists():
        request.session[f'pesquisa_concluida_{link_uuid}'] = True
        return redirect('pesquisas:agradecimento', link_uuid=link_uuid)
    perguntas = list(pesquisa.perguntas.all())
    if etapa < 1 or etapa > len(perguntas):
        raise Http404
    pergunta = perguntas[etapa - 1]
    respostas_key = f'pesquisa_respostas_{link_uuid}'
    respostas = request.session.get(respostas_key, {})
    for indice, anterior in enumerate(perguntas[:etapa - 1], start=1):
        if str(anterior.pk) not in respostas:
            return redirect('pesquisas:responder_etapa', link_uuid=link_uuid, etapa=indice)
    form = ResponderPerguntaForm(pergunta, request.POST or None, initial={'valor': respostas.get(str(pergunta.pk))})
    if request.method == 'POST' and form.is_valid():
        respostas[str(pergunta.pk)] = form.cleaned_data['valor']
        request.session[respostas_key] = respostas
        if etapa < len(perguntas):
            return redirect('pesquisas:responder_etapa', link_uuid=link_uuid, etapa=etapa + 1)
        try:
            with transaction.atomic():
                resposta = RespostaPesquisa.objects.create(pesquisa=pesquisa, hash_documento_respondente=hash_documento)
                for item in perguntas:
                    valor = respostas.get(str(item.pk))
                    DetalheResposta.objects.create(
                        resposta_pesquisa=resposta, pergunta=item,
                        valor_texto=valor if item.tipo_resposta == Pergunta.TipoResposta.TEXTO else None,
                        valor_inteiro=valor if item.tipo_resposta == Pergunta.TipoResposta.NOTA_0_10 else None,
                    )
        except IntegrityError:
            pass
        request.session.pop(f'pesquisa_auth_{link_uuid}', None)
        request.session.pop(respostas_key, None)
        request.session[f'pesquisa_concluida_{link_uuid}'] = True
        return redirect('pesquisas:agradecimento', link_uuid=link_uuid)
    return render(request, 'pesquisas/responder_etapa.html', {
        'pesquisa': pesquisa, 'pergunta': pergunta, 'form': form, 'etapa': etapa,
        'total': len(perguntas), 'progresso': int(etapa / len(perguntas) * 100), 'hide_sidebar': True,
    })


def agradecimento(request, link_uuid):
    pesquisa = get_object_or_404(Pesquisa, link_uuid=link_uuid)
    if not request.session.get(f'pesquisa_concluida_{link_uuid}', False):
        return redirect('pesquisas:validar_participante', link_uuid=link_uuid)
    return render(request, 'pesquisas/agradecimento.html', {'pesquisa': pesquisa, 'hide_sidebar': True})
