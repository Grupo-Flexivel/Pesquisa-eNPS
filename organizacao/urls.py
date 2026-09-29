from django.urls import path

from . import views
from .forms import CargoForm, DepartamentoForm, SecaoForm
from .models import Cargo, Departamento, Secao


app_name = 'organizacao'

CADASTROS = (
    {
        'prefixo': 'departamento', 'caminho': 'departamentos', 'model': Departamento,
        'form_class': DepartamentoForm, 'rotulo': 'departamento', 'rotulo_plural': 'Departamentos',
        'sufixo': 'o', 'lista': {},
    },
    {
        'prefixo': 'secao', 'caminho': 'secoes', 'model': Secao,
        'form_class': SecaoForm, 'rotulo': 'seção', 'rotulo_plural': 'Seções',
        'sufixo': 'a', 'lista': {
            'select_related': ('departamento',),
            'coluna_pai': 'Departamento',
            'filtros_pai': (('departamento', Departamento, 'departamento_id'),),
        },
    },
    {
        'prefixo': 'cargo', 'caminho': 'cargos', 'model': Cargo,
        'form_class': CargoForm, 'rotulo': 'cargo', 'rotulo_plural': 'Cargos',
        'sufixo': 'o', 'lista': {
            'select_related': ('secao__departamento',),
            'coluna_pai': 'Seção',
            'filtros_pai': (
                ('departamento', Departamento, 'secao__departamento_id'),
                ('secao', Secao, 'secao_id'),
            ),
        },
    },
)


def rotas(cfg):
    """Rotas padrão de um cadastro: lista, criar, editar, status e excluir."""
    base = {k: cfg[k] for k in ('model', 'prefixo', 'rotulo', 'rotulo_plural', 'sufixo')}
    com_form = {**base, 'form_class': cfg['form_class']}
    caminho, prefixo = cfg['caminho'], cfg['prefixo']
    return [
        path(f'{caminho}/', views.CadastroListView.as_view(**base, **cfg['lista']), name=f'{prefixo}_lista'),
        path(f'{caminho}/novo/', views.CadastroCreateView.as_view(**com_form), name=f'{prefixo}_criar'),
        path(f'{caminho}/<int:pk>/editar/', views.CadastroUpdateView.as_view(**com_form), name=f'{prefixo}_editar'),
        path(f'{caminho}/<int:pk>/status/', views.CadastroStatusView.as_view(**base), name=f'{prefixo}_status'),
        path(f'{caminho}/<int:pk>/excluir/', views.CadastroExcluirView.as_view(**base), name=f'{prefixo}_excluir'),
    ]


urlpatterns = [rota for cfg in CADASTROS for rota in rotas(cfg)]
