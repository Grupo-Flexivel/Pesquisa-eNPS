from django import forms
from django.forms import BaseInlineFormSet, inlineformset_factory

from pesquisas.forms import BootstrapMixin

from .models import FormularioAvaliacao, OpcaoPerguntaAvaliacao, PerguntaAvaliacao, RespostaAvaliacao


class FormularioAvaliacaoForm(BootstrapMixin, forms.ModelForm):
    """Cadastro do formulário pelo RH: título, descrição (editor rico) e se é o padrão."""

    # Fora dos campos do model: a troca de padrão (desmarcar o anterior) é feita na view, e a
    # validação da restrição "um único padrão" do ModelForm recusaria a marcação antes disso.
    padrao = forms.BooleanField(
        label='Formulário padrão', required=False,
        help_text='Formulário enviado aos gestores nas novas avaliações. Só um pode ser o padrão.',
    )

    class Meta:
        model = FormularioAvaliacao
        fields = ('titulo', 'descricao')
        widgets = {'descricao': forms.Textarea(attrs={'rows': 6, 'class': 'richeditor'})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields['padrao'].initial = self.instance.padrao
        self.aplicar_bootstrap()
        self.fields['descricao'].widget.attrs['class'] = 'form-control richeditor'


class PerguntaAvaliacaoForm(BootstrapMixin, forms.ModelForm):
    opcoes_texto = forms.CharField(
        label='Opções de resposta (uma por linha)',
        required=False,
        widget=forms.Textarea(attrs={'rows': 3, 'placeholder': 'Digite uma opção por linha'}),
    )

    class Meta:
        model = PerguntaAvaliacao
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
        if tipo in PerguntaAvaliacao.OPCOES_TIPOS and len(opcoes) < 2:
            self.add_error('opcoes_texto', 'Informe ao menos duas opções de resposta.')
        cleaned_data['opcoes'] = opcoes
        return cleaned_data

    def save(self, commit=True):
        pergunta = super().save(commit=commit)
        if commit:
            self._salvar_opcoes(pergunta)
        return pergunta

    def _salvar_opcoes(self, pergunta):
        # As respostas já enviadas guardam cópia das opções escolhidas: recriar aqui não afeta o histórico.
        pergunta.opcoes.all().delete()
        if pergunta.tipo_resposta in PerguntaAvaliacao.OPCOES_TIPOS:
            OpcaoPerguntaAvaliacao.objects.bulk_create([
                OpcaoPerguntaAvaliacao(pergunta=pergunta, texto_opcao=texto, ordem=indice)
                for indice, texto in enumerate(self.cleaned_data.get('opcoes') or [], start=1)
            ])


class BasePerguntaAvaliacaoFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        validas = [form for form in self.forms if form.cleaned_data and not form.cleaned_data.get('DELETE', False)]
        if len(validas) < 1:
            raise forms.ValidationError('Cadastre ao menos uma pergunta.')
        ordens = [form.cleaned_data['ordem'] for form in validas]
        if len(ordens) != len(set(ordens)):
            raise forms.ValidationError('A ordem das perguntas não pode se repetir.')


PerguntaAvaliacaoFormSet = inlineformset_factory(
    FormularioAvaliacao, PerguntaAvaliacao, form=PerguntaAvaliacaoForm, formset=BasePerguntaAvaliacaoFormSet,
    extra=1, min_num=1, validate_min=True, can_delete=True,
)


class AvaliacaoForm(BootstrapMixin, forms.Form):
    """Preenchimento pelo gestor: um campo por pergunta (conforme o tipo) + decisão final."""

    decisao = forms.ChoiceField(
        label='Decisão final',
        choices=[
            (RespostaAvaliacao.Decisao.EFETIVADO, 'Efetivar o colaborador'),
            (RespostaAvaliacao.Decisao.DESLIGADO, 'Desligar o colaborador'),
        ],
        widget=forms.RadioSelect,
        error_messages={'required': 'Informe a decisão final: efetivar ou desligar.'},
    )
    observacoes = forms.CharField(
        label='Observações (opcional)', required=False, max_length=5000,
        widget=forms.Textarea(attrs={'rows': 3}),
    )

    def __init__(self, formulario, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.formulario = formulario
        self.perguntas = list(formulario.perguntas.prefetch_related('opcoes'))
        campos_perguntas = {}
        for pergunta in self.perguntas:
            campo = self._campo_da_pergunta(pergunta)
            campo.error_messages['required'] = 'Responda esta pergunta.'
            campos_perguntas[self.nome_campo(pergunta)] = campo
        # Perguntas primeiro, decisão e observações no final do formulário.
        self.fields = {**campos_perguntas, **self.fields}
        self.aplicar_bootstrap()
        for campo in self.fields.values():
            if isinstance(campo.widget, (forms.RadioSelect, forms.CheckboxSelectMultiple)):
                campo.widget.attrs['class'] = 'form-check-input'

    @staticmethod
    def _campo_da_pergunta(pergunta):
        tipo = pergunta.tipo_resposta
        if tipo == PerguntaAvaliacao.TipoResposta.TEXTO:
            return forms.CharField(label=pergunta.texto_pergunta, widget=forms.Textarea(attrs={'rows': 4}), max_length=5000)
        if tipo == PerguntaAvaliacao.TipoResposta.NOTA_0_10:
            return forms.TypedChoiceField(
                label=pergunta.texto_pergunta, choices=[(i, str(i)) for i in range(11)], coerce=int,
                widget=forms.RadioSelect,
            )
        opcoes = [(opcao.pk, opcao.texto_opcao) for opcao in pergunta.opcoes.all()]
        if tipo == PerguntaAvaliacao.TipoResposta.RADIO:
            return forms.TypedChoiceField(
                label=pergunta.texto_pergunta, choices=opcoes, coerce=int, widget=forms.RadioSelect,
            )
        return forms.TypedMultipleChoiceField(
            label=pergunta.texto_pergunta, choices=opcoes, coerce=int, widget=forms.CheckboxSelectMultiple,
        )

    @staticmethod
    def nome_campo(pergunta):
        return f'pergunta_{pergunta.pk}'

    def perguntas_com_campo(self):
        for pergunta in self.perguntas:
            yield pergunta, self[self.nome_campo(pergunta)]

    def respostas(self):
        """{pergunta: valor} — texto, nota (int), opção ou lista de opções. Usar após is_valid()."""
        respostas = {}
        for pergunta in self.perguntas:
            valor = self.cleaned_data[self.nome_campo(pergunta)]
            if pergunta.tipo_resposta in PerguntaAvaliacao.OPCOES_TIPOS:
                opcoes = {opcao.pk: opcao for opcao in pergunta.opcoes.all()}
                valor = [opcoes[pk] for pk in valor] if isinstance(valor, list) else opcoes[valor]
            respostas[pergunta] = valor
        return respostas
