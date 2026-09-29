from django import forms
from django.db.models import Q
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.utils import timezone

from .models import Colaborador, PerfilAcesso, Pergunta, Pesquisa, User, somente_digitos, validar_documento
from .permissoes import MODULOS_CHOICES


class BootstrapMixin:
    def aplicar_bootstrap(self):
        for field in self.fields.values():
            css = 'form-select' if isinstance(field.widget, forms.Select) else 'form-control'
            if isinstance(field.widget, forms.CheckboxInput):
                css = 'form-check-input'
            elif isinstance(field.widget, forms.CheckboxSelectMultiple):
                css = 'form-check-input'
            field.widget.attrs['class'] = css


class PerfilAcessoForm(BootstrapMixin, forms.ModelForm):
    """Formulário para cadastrar e editar perfis de acesso personalizados e seus módulos."""
    modulos_permitidos = forms.MultipleChoiceField(
        choices=MODULOS_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label='Módulos Permitidos',
        help_text='Selecione os módulos liberados para este perfil.'
    )

    class Meta:
        model = PerfilAcesso
        fields = ('nome', 'modulos_permitidos', 'ativo')
        widgets = {
            'nome': forms.TextInput(attrs={'placeholder': 'Ex: Gestor de Talentos'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.aplicar_bootstrap()

    def clean_modulos_permitidos(self):
        modulos = self.cleaned_data['modulos_permitidos']
        if not modulos:
            raise forms.ValidationError('Selecione ao menos um módulo para este perfil.')
        return modulos


class ColaboradorForm(BootstrapMixin, forms.ModelForm):
    documento = forms.CharField(
        label='CPF ou CNPJ', max_length=18,
        widget=forms.TextInput(attrs={'inputmode': 'numeric', 'maxlength': '18', 'placeholder': 'Digite CPF ou CNPJ'}),
    )

    # Campos de acesso ao painel: preenchido dinamicamente no __init__
    perfil = forms.ChoiceField(
        label='Perfil de acesso ao painel',
        required=False,
        choices=(),
        help_text='Define o que este colaborador pode acessar no painel administrativo.',
    )
    
    username = forms.CharField(
        label='Nome de usuário (Login)',
        max_length=150,
        required=False,
        help_text='Login de rede (sAMAccountName) que este usuário usará no sistema.'
    )

    CAMPOS_ACESSO = ('perfil', 'username')

    class Meta:
        model = Colaborador
        fields = ('nome', 'documento', 'empresa', 'ativo')

    def __init__(self, *args, usuario_logado=None, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Constrói as opções de perfil dinamicamente: Opção vazia + Administrador Total + Perfis do Banco.
        # Só perfis ativos entram na lista — exceto o perfil já vinculado a este colaborador (se houver),
        # que é mantido visível mesmo inativado, para não "sumir" a seleção atual ao abrir o formulário.
        vinculado_id = None
        if self.instance and self.instance.pk and self.instance.usuario_id:
            vinculado_id = self.instance.usuario.perfil_personalizado_id

        perfis = PerfilAcesso.objects.filter(Q(ativo=True) | Q(pk=vinculado_id))
        choices = [('', 'Sem acesso ao painel'), ('ADMINISTRADOR', 'Administrador total')]
        for perfil_obj in perfis:
            rotulo = f'Perfil: {perfil_obj.nome}' + ('' if perfil_obj.ativo else ' (inativo)')
            choices.append((str(perfil_obj.pk), rotulo))

        self.fields['perfil'].choices = choices

        # Conceder acesso ao painel é atribuição exclusiva do Administrador Total
        self.pode_gerir_acesso = bool(usuario_logado and usuario_logado.is_authenticated
                                      and usuario_logado.is_admin_total)
        
        if not self.pode_gerir_acesso:
            for nome in self.CAMPOS_ACESSO:
                del self.fields[nome]
        elif self.instance and self.instance.pk and self.instance.usuario_id:
            usuario = self.instance.usuario
            if usuario.is_admin_total:
                self.fields['perfil'].initial = 'ADMINISTRADOR'
            elif usuario.perfil_personalizado_id:
                self.fields['perfil'].initial = str(usuario.perfil_personalizado_id)
            else:
                self.fields['perfil'].initial = ''
                
            self.fields['username'].initial = usuario.username

        self.aplicar_bootstrap()

    def clean_documento(self):
        documento = somente_digitos(self.cleaned_data['documento'])
        validar_documento(documento)
        return documento

    def clean(self):
        cleaned_data = super().clean()
        if not self.pode_gerir_acesso:
            return cleaned_data

        perfil = cleaned_data.get('perfil')
        username = (cleaned_data.get('username') or '').strip().lower()
        cleaned_data['username'] = username

        # Se há perfil de acesso selecionado, o login de rede torna-se obrigatório
        if perfil and not username:
            self.add_error('username', 'Informe o nome de usuário (login) para conceder acesso ao painel.')

        # Validar se o username já existe no sistema caso esteja criando/editando
        if username:
            user_qs = User.objects.filter(username__iexact=username)
            if self.instance and self.instance.pk and self.instance.usuario_id:
                user_qs = user_qs.exclude(pk=self.instance.usuario_id)
            if user_qs.exists():
                self.add_error('username', 'Este nome de usuário já está em uso.')

        return cleaned_data

    def save(self, commit=True):
        colaborador = super().save(commit=False)
        if not commit:
            return colaborador

        if self.pode_gerir_acesso:
            self._sincronizar_usuario(colaborador)
        colaborador.save()
        self.save_m2m()
        return colaborador

    def _sincronizar_usuario(self, colaborador):
        perfil = self.cleaned_data.get('perfil')
        username = self.cleaned_data.get('username')

        if not (perfil and username):
            if colaborador.usuario:
                # Remove o acesso ao painel e desvincula
                usuario = colaborador.usuario
                usuario.is_admin_total = False
                usuario.perfil_personalizado = None
                usuario.save()
                colaborador.usuario = None
            return

        if colaborador.usuario:
            user = colaborador.usuario
            user.username = username
            user.first_name = colaborador.nome
        else:
            user, _ = User.objects.get_or_create(username=username, defaults={'first_name': colaborador.nome})
            
        source_before = user.auth_source

        # Atribui o tipo de acesso correspondente
        if perfil == 'ADMINISTRADOR':
            user.is_admin_total = True
            user.perfil_personalizado = None
        else:
            user.is_admin_total = False
            user.perfil_personalizado = PerfilAcesso.objects.filter(pk=perfil).first()

        # O login informado neste cadastro é o sAMAccountName do AD.
        # Nunca validar esse usuário por senha local.
        user.auth_source = User.AuthSource.DIRECTORY
        if source_before != User.AuthSource.DIRECTORY:
            user.directory_guid = None
        if user.has_usable_password():
            user.set_unusable_password()
            
        user.save()
        colaborador.usuario = user


class PesquisaForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Pesquisa
        fields = ('titulo', 'data_inicio', 'data_final')
        widgets = {
            'data_inicio': forms.DateTimeInput(format='%Y-%m-%dT%H:%M', attrs={'type': 'datetime-local'}),
            'data_final': forms.DateTimeInput(format='%Y-%m-%dT%H:%M', attrs={'type': 'datetime-local'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['data_inicio'].input_formats = ('%Y-%m-%dT%H:%M',)
        self.fields['data_final'].input_formats = ('%Y-%m-%dT%H:%M',)
        self.aplicar_bootstrap()

    def clean_data_inicio(self):
        data_inicio = self.cleaned_data['data_inicio']
        if not self.instance.pk and data_inicio <= timezone.now():
            raise forms.ValidationError('A data inicial de uma nova pesquisa deve estar no futuro.')
        return data_inicio


class PerguntaForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Pergunta
        fields = ('texto_pergunta', 'tipo_resposta', 'ordem')
        widgets = {'texto_pergunta': forms.Textarea(attrs={'rows': 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.aplicar_bootstrap()


class BasePerguntaFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        validas = [form for form in self.forms if form.cleaned_data and not form.cleaned_data.get('DELETE', False)]
        if len(validas) < 3:
            raise forms.ValidationError('Cadastre no mínimo 3 perguntas.')
        ordens = [form.cleaned_data['ordem'] for form in validas]
        if len(ordens) != len(set(ordens)):
            raise forms.ValidationError('A ordem das perguntas não pode se repetir.')


PerguntaFormSet = inlineformset_factory(
    Pesquisa, Pergunta, form=PerguntaForm, formset=BasePerguntaFormSet,
    extra=3, min_num=3, validate_min=True, can_delete=True,
)


class ValidarCPFForm(BootstrapMixin, forms.Form):
    documento = forms.CharField(
        label='CPF ou CNPJ', max_length=18, 
        widget=forms.TextInput(attrs={'inputmode': 'numeric', 'autocomplete': 'off', 'maxlength': '18'})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.aplicar_bootstrap()

    def clean_documento(self):
        documento = somente_digitos(self.cleaned_data['documento'])
        validar_documento(documento)
        return documento


class ResponderPerguntaForm(BootstrapMixin, forms.Form):
    def __init__(self, pergunta, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pergunta = pergunta
        if pergunta.tipo_resposta == Pergunta.TipoResposta.TEXTO:
            self.fields['valor'] = forms.CharField(label='Sua resposta', widget=forms.Textarea(attrs={'rows': 5}), max_length=5000)
        else:
            self.fields['valor'] = forms.TypedChoiceField(
                label='Sua nota', choices=[(i, str(i)) for i in range(11)], coerce=int, widget=forms.RadioSelect
            )
        self.aplicar_bootstrap()