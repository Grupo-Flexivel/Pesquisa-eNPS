from django import forms
from django.forms import BaseInlineFormSet, inlineformset_factory

from pesquisas.forms import BootstrapMixin
from pesquisas.models import somente_digitos, validar_documento

from .models import OpcaoPerguntaVaga, PerguntaVaga, Vaga


class VagaForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Vaga
        fields = ('titulo', 'descricao', 'data_abertura', 'data_encerramento')
        widgets = {
            'descricao': forms.Textarea(attrs={'rows': 5, 'class': 'richeditor'}),
            'data_abertura': forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'),
            'data_encerramento': forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['data_abertura'].input_formats = ['%Y-%m-%dT%H:%M']
        self.fields['data_encerramento'].input_formats = ['%Y-%m-%dT%H:%M']
        self.aplicar_bootstrap()


class PerguntaVagaForm(BootstrapMixin, forms.ModelForm):
    opcoes_texto = forms.CharField(
        label='Opções de resposta (uma por linha)',
        required=False,
        widget=forms.Textarea(attrs={'rows': 3, 'placeholder': 'Digite uma opção por linha'}),
    )

    class Meta:
        model = PerguntaVaga
        fields = ('texto_pergunta', 'tipo_resposta', 'ordem')
        widgets = {'texto_pergunta': forms.Textarea(attrs={'rows': 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields['opcoes_texto'].initial = '\n'.join(
                self.instance.opcoes.values_list('texto_opcao', flat=True)
            )
        self.aplicar_bootstrap()

    def clean(self):
        cleaned_data = super().clean()
        tipo = cleaned_data.get('tipo_resposta')
        opcoes = [linha.strip() for linha in (cleaned_data.get('opcoes_texto') or '').splitlines() if linha.strip()]
        if tipo in PerguntaVaga.OPCOES_TIPOS and len(opcoes) < 2:
            self.add_error('opcoes_texto', 'Informe ao menos duas opções de resposta.')
        cleaned_data['opcoes'] = opcoes
        return cleaned_data

    def save(self, commit=True):
        pergunta = super().save(commit=commit)
        if commit:
            self._salvar_opcoes(pergunta)
        return pergunta

    def _salvar_opcoes(self, pergunta):
        pergunta.opcoes.all().delete()
        if pergunta.tipo_resposta in PerguntaVaga.OPCOES_TIPOS:
            OpcaoPerguntaVaga.objects.bulk_create([
                OpcaoPerguntaVaga(pergunta=pergunta, texto_opcao=texto, ordem=indice)
                for indice, texto in enumerate(self.cleaned_data.get('opcoes') or [], start=1)
            ])


class BasePerguntaVagaFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        
        validas = [form for form in self.forms if form.cleaned_data and not form.cleaned_data.get('DELETE', False)]        
        ordens = [form.cleaned_data['ordem'] for form in validas if form.cleaned_data.get('ordem') is not None]
        if len(ordens) != len(set(ordens)):
            raise forms.ValidationError('A ordem das perguntas não pode se repetir.')


PerguntaVagaFormSet = inlineformset_factory(
    Vaga, PerguntaVaga, form=PerguntaVagaForm, formset=BasePerguntaVagaFormSet,
    extra=1, min_num=0, validate_min=False, can_delete=True,
)


class ValidarCPFCandidaturaForm(BootstrapMixin, forms.Form):
    documento = forms.CharField(
        label='CPF', max_length=14,
        widget=forms.TextInput(attrs={'inputmode': 'numeric', 'autocomplete': 'off', 'maxlength': '14'}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.aplicar_bootstrap()

    def clean_documento(self):
        documento = somente_digitos(self.cleaned_data['documento'])
        validar_documento(documento)
        return documento


class CandidaturaForm(BootstrapMixin, forms.Form):
    """Renderiza dinamicamente um campo por pergunta da vaga."""

    def __init__(self, vaga, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.vaga = vaga
        self.perguntas = list(vaga.perguntas.prefetch_related('opcoes'))
        for pergunta in self.perguntas:
            nome_campo = f'pergunta_{pergunta.pk}'
            tipo = pergunta.tipo_resposta
            if tipo == PerguntaVaga.TipoResposta.TEXTO:
                self.fields[nome_campo] = forms.CharField(
                    label=pergunta.texto_pergunta, widget=forms.Textarea(attrs={'rows': 4}), max_length=5000,
                )
            elif tipo == PerguntaVaga.TipoResposta.NOTA_0_10:
                self.fields[nome_campo] = forms.TypedChoiceField(
                    label=pergunta.texto_pergunta, choices=[(i, str(i)) for i in range(11)], coerce=int,
                    widget=forms.RadioSelect,
                )
            elif tipo == PerguntaVaga.TipoResposta.RADIO:
                opcoes = [(opcao.pk, opcao.texto_opcao) for opcao in pergunta.opcoes.all()]
                self.fields[nome_campo] = forms.TypedChoiceField(
                    label=pergunta.texto_pergunta, choices=opcoes, coerce=int, widget=forms.RadioSelect,
                )
            else:
                opcoes = [(opcao.pk, opcao.texto_opcao) for opcao in pergunta.opcoes.all()]
                self.fields[nome_campo] = forms.TypedMultipleChoiceField(
                    label=pergunta.texto_pergunta, choices=opcoes, coerce=int, widget=forms.CheckboxSelectMultiple,
                )
            self.fields[nome_campo].pergunta = pergunta
        self.aplicar_bootstrap()

    def perguntas_com_campo(self):
        for pergunta in self.perguntas:
            yield pergunta, self[f'pergunta_{pergunta.pk}']
