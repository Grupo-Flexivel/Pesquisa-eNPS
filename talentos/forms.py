from django import forms

from pesquisas.forms import BootstrapMixin

from organizacao.models import Cargo, Departamento, Secao

from .models import Candidato
from .pipeline import cargos_disponiveis


class CandidatoForm(BootstrapMixin, forms.ModelForm):
    """Correção dos dados extraídos e definição do cargo pelo analista."""

    # Declaramos explicitamente o campo como um ModelChoiceField apontando para o banco 'default'
    cargo = forms.ModelChoiceField(
        queryset=Cargo.objects.using('default').none(),  # Inicialmente vazio, ajustado no __init__
        required=False,
        label="Cargo definido pelo analista",
        empty_label='Não definido'
    )

    class Meta:
        model = Candidato
        fields = ('nome', 'email', 'telefone', 'cidade', 'bairro', 'cargo')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Obtém os cargos disponíveis usando o banco 'default'
        cargos = cargos_disponiveis()
        if hasattr(cargos, 'using'):
            cargos = cargos.using('default')
        
        if self.instance and self.instance.cargo_id:  # mantém o cargo atual mesmo que tenha sido inativado depois
            cargos = cargos | Cargo.objects.using('default').filter(pk=self.instance.cargo_id)
            
        self.fields['cargo'].queryset = cargos.distinct()

        # `cargo` não é um campo real do model (só `cargo_id` — ver Candidato.cargo), então o
        # ModelForm padrão não consegue preencher o valor inicial dele sozinho (`model_to_dict`
        # só enxerga campos de verdade). Sem isto, o <select> sempre abre em "Não definido" —
        # mesmo quando a IA (ou um analista antes) já definiu um cargo — e um simples "Salvar"
        # sem mexer no campo apagaria o cargo já atribuído.
        if self.instance and self.instance.pk:
            self.initial['cargo'] = self.instance.cargo_id

        self.aplicar_bootstrap()

    def save(self, commit=True):
        # `cargo` não é um campo real do model (só `cargo_id`, um IntegerField cru para
        # suportar o banco cruzado — ver Candidato.cargo). O ModelForm padrão só popula
        # campos que existem de verdade em `instance._meta.fields`, então preencher
        # `cargo_id` manualmente aqui é necessário: sem isso, o cargo escolhido na tela é
        # validado mas nunca chega a ser salvo.
        candidato = super().save(commit=False)
        cargo = self.cleaned_data.get('cargo')
        candidato.cargo_id = cargo.id if cargo else None
        if candidato.cargo_id and candidato.processamento == Candidato.Processamento.ERRO:
            # O analista assumiu o caso manualmente ao definir o cargo: a mensagem de erro do
            # processamento automático (ex.: falha na busca semântica) não deve mais poluir a
            # tela — as telas de lista/detalhe/Kanban só mostram a tag de erro enquanto
            # `processamento == ERRO`.
            candidato.processamento = Candidato.Processamento.CONCLUIDO
            candidato.erro_processamento = ''
        if commit:
            candidato.save()
        return candidato

class BuscaForm(BootstrapMixin, forms.Form):
    """Busca em linguagem natural + filtros por localidade e estrutura organizacional."""

    q = forms.CharField(
        label='O que você procura?', required=False, max_length=500,
        widget=forms.TextInput(attrs={'placeholder': 'Ex.: desenvolvedor com experiência em React e liderança de equipe'}),
    )
    cidade = forms.CharField(label='Cidade', required=False, max_length=120)
    bairro = forms.CharField(label='Bairro', required=False, max_length=120)
    departamento = forms.ModelChoiceField(
        label='Departamento', required=False, queryset=Departamento.objects.all(), empty_label='Todos',
    )
    secao = forms.ModelChoiceField(
        label='Seção', required=False, queryset=Secao.objects.select_related('departamento'), empty_label='Todas',
    )
    cargo = forms.ModelChoiceField(
        label='Cargo', required=False, queryset=Cargo.objects.select_related('secao__departamento'), empty_label='Todos',
    )
    status = forms.ChoiceField(
        label='Etapa', required=False, choices=[('', 'Todas')] + Candidato.Status.choices,
    )
    situacao = forms.ChoiceField(
        label='Situação', required=False, initial='ativos',
        choices=[('ativos', 'Ativos'), ('inativos', 'Inativos'), ('todos', 'Todos')],
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.aplicar_bootstrap()
