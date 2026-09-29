"""Telas de cadastro da estrutura organizacional (Departamento, Seção, Cargo).

As três entidades compartilham o mesmo fluxo (lista, criar, editar, ativar/inativar,
excluir); a configuração de cada uma (model, form, filtros por pai) é passada
via `as_view(...)` em urls.py.
"""
from django.contrib import messages
from django.db.models import ProtectedError
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views import View
from django.views.generic import CreateView, ListView, UpdateView

from pesquisas.permissoes import TALENTOS_ORGANIZACAO, ModuloRequiredMixin


class CadastroMixin(ModuloRequiredMixin):
    modulos_exigidos = (TALENTOS_ORGANIZACAO,)
    model = None
    prefixo = ''          
    rotulo = ''           
    rotulo_plural = ''    
    sufixo = 'o'          

    def url(self, acao):
        return reverse(f'organizacao:{self.prefixo}_{acao}')

    def get_context_data(self, **kwargs):
        contexto = super().get_context_data(**kwargs)
        contexto.update({
            'rotulo': self.rotulo, 'rotulo_plural': self.rotulo_plural, 'prefixo': self.prefixo,
            'url_lista': self.url('lista'), 'url_criar': self.url('criar'),
            # Nomes de URL das ações por registro (recebem o pk no template).
            'nome_editar': f'organizacao:{self.prefixo}_editar',
            'nome_status': f'organizacao:{self.prefixo}_status',
            'nome_excluir': f'organizacao:{self.prefixo}_excluir',
        })
        return contexto


class CadastroListView(CadastroMixin, ListView):
    template_name = 'organizacao/lista.html'
    context_object_name = 'objetos'
    paginate_by = 25
    coluna_pai = ''
    select_related = ()
    # (parâmetro GET, model do filtro, lookup no queryset)
    filtros_pai = ()

    def get_queryset(self):
        queryset = self.model.objects.select_related(*self.select_related)
        termo = self.request.GET.get('q', '').strip()
        if termo:
            queryset = queryset.filter(nome__icontains=termo)
        status = self.request.GET.get('status', '')
        if status in ('ativo', 'inativo'):
            queryset = queryset.filter(ativo=(status == 'ativo'))
        for parametro, _model, lookup in self.filtros_pai:
            valor = self.request.GET.get(parametro, '')
            if valor.isdigit():
                queryset = queryset.filter(**{lookup: int(valor)})
        return queryset

    def get_context_data(self, **kwargs):
        contexto = super().get_context_data(**kwargs)
        querystring = self.request.GET.copy()
        querystring.pop('page', None)
        contexto.update({
            'termo': self.request.GET.get('q', '').strip(),
            'status_selecionado': self.request.GET.get('status', ''),
            'filtros': [
                {'parametro': p, 'opcoes': model.objects.all(), 'selecionado': self.request.GET.get(p, '')}
                for p, model, _lookup in self.filtros_pai
            ],
            'querystring': querystring.urlencode(),
            'coluna_pai': self.coluna_pai,
        })
        return contexto


class CadastroFormMixin(CadastroMixin):
    template_name = 'organizacao/form.html'

    def get_success_url(self):
        return self.url('lista')

    def form_valid(self, form):
        resposta = super().form_valid(form)
        messages.success(self.request, f'{self.rotulo.capitalize()} salv{self.sufixo} com sucesso.')
        return resposta


class CadastroCreateView(CadastroFormMixin, CreateView):
    pass


class CadastroUpdateView(CadastroFormMixin, UpdateView):
    pass


class CadastroAcaoView(CadastroMixin, View):
    """Base das ações POST sobre um registro (status, exclusão)."""
    http_method_names = ['post']  # ações que alteram estado nunca via GET

    def post(self, request, pk):
        objeto = get_object_or_404(self.model, pk=pk)
        self.executar(request, objeto)
        return redirect(self.url('lista'))


class CadastroStatusView(CadastroAcaoView):
    def executar(self, request, objeto):
        objeto.ativo = not objeto.ativo
        objeto.save(update_fields=['ativo'])
        situacao = 'ativad' if objeto.ativo else 'inativad'
        messages.success(request, f'{self.rotulo.capitalize()} {situacao}{self.sufixo} com sucesso.')


class CadastroExcluirView(CadastroAcaoView):
    def executar(self, request, objeto):
        try:
            objeto.delete()
        except ProtectedError as erro:
            messages.error(request, erro.args[0])
        else:
            excluido = 'excluído' if self.sufixo == 'o' else 'excluída'
            messages.success(request, f'{self.rotulo.capitalize()} {excluido} com sucesso.')
