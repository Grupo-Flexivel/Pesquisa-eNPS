from django import forms
from django.db.models import Q

from pesquisas.forms import BootstrapMixin

from .models import Cargo, Departamento, Secao


class CadastroForm(BootstrapMixin, forms.ModelForm):
    """Base dos formulários: estilo Bootstrap e pais restritos a registros ativos."""

    campo_pai = None  # nome do FK pai, quando houver

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.campo_pai:
            campo = self.fields[self.campo_pai]
            # Só permite vincular a pais ativos (mantendo o pai atual ao editar).
            filtro = Q(ativo=True)
            if self.instance.pk:
                filtro |= Q(pk=getattr(self.instance, f'{self.campo_pai}_id'))
            campo.queryset = campo.queryset.filter(filtro)
        self.aplicar_bootstrap()


class DepartamentoForm(CadastroForm):
    class Meta:
        model = Departamento
        fields = ('nome', 'ativo')


class SecaoForm(CadastroForm):
    campo_pai = 'departamento'

    class Meta:
        model = Secao
        fields = ('departamento', 'nome', 'ativo')


class CargoForm(CadastroForm):
    campo_pai = 'secao'

    class Meta:
        model = Cargo
        fields = ('secao', 'nome', 'ativo')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['secao'].queryset = self.fields['secao'].queryset.select_related('departamento')
