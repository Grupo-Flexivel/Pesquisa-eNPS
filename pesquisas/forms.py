from django import forms
from django.db.models import Q
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.utils import timezone

from .models import (
    Colaborador, PerfilAcesso, Pergunta, Pesquisa, User, normalizar_celulares, somente_digitos, validar_celulares,
    validar_documento,
)
from .permissoes import MODULOS_CHOICES


class CelularesWidget(forms.Widget):
    """Vários inputs com o mesmo `name`, um por número (o template monta as linhas e o botão de adicionar)."""

    def value_from_datadict(self, data, files, name):
        if hasattr(data, 'getlist'):
            return data.getlist(name)
        valor = data.get(name, [])
        return [valor] if isinstance(valor, str) else list(valor)

    def value_omitted_from_data(self, data, files, name):
        # Sem nenhum input enviado = o usuário removeu todos os números.
        return False


class CelularesField(forms.Field):
    widget = CelularesWidget

    def to_python(self, value):
        return [str(v).strip() for v in (value or []) if str(v).strip()]

    def validate(self, value):
        super().validate(value)
        validar_celulares(value)

    def clean(self, value):
        return normalizar_celulares(super().clean(value))


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


class ImportarColaboradoresForm(BootstrapMixin, forms.Form):
    arquivo = forms.FileField(
        label='Planilha (.csv ou .xlsx)',
        widget=forms.ClearableFileInput(attrs={'accept': '.csv,.xlsx'}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.aplicar_bootstrap()


class ColaboradorForm(BootstrapMixin, forms.ModelForm):
    documento = forms.CharField(
        label='CPF ou CNPJ', max_length=18,
        widget=forms.TextInput(attrs={'inputmode': 'numeric', 'maxlength': '18', 'placeholder': 'Digite CPF ou CNPJ'}),
    )
    celulares = CelularesField(label='Celulares', required=False)

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
        fields = (
            'nome', 'documento', 'data_nascimento', 'celulares', 'email', 'empresas', 'gestor',
            'data_contratacao', 'data_demissao', 'eh_gestor', 'ativo',
        )
        widgets = {
            'empresas': forms.CheckboxSelectMultiple,
            'email': forms.EmailInput(attrs={'placeholder': 'nome@exemplo.com.br'}),
            'data_nascimento': forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}),
            'data_contratacao': forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}),
            'data_demissao': forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date'}),
        }
        help_texts = {
            'empresas': 'Marque todas as empresas/unidades em que o colaborador trabalha.',
            'data_demissao': 'Obrigatória para inativar. Data futura: o colaborador segue ativo e é inativado nesse dia.',
            'gestor': 'Apenas colaboradores marcados como gestores. O aviso de avaliação da experiência '
                      'chega ao gestor pelo login dele no painel.',
        }

    def __init__(self, *args, usuario_logado=None, **kwargs):
        super().__init__(*args, **kwargs)

        # Só colaboradores ativos marcados como gestores; o gestor já vinculado continua na lista
        # mesmo que tenha deixado de sê-lo, para a seleção atual não "sumir" ao abrir o formulário.
        gestor_atual = self.instance.gestor_id if self.instance.pk else None
        gestores = Colaborador.objects.filter(Q(eh_gestor=True, ativo=True) | Q(pk=gestor_atual))
        if self.instance.pk:
            gestores = gestores.exclude(pk=self.instance.pk)
        self.fields['gestor'].queryset = gestores.order_by('nome')
        self.fields['gestor'].empty_label = 'Sem gestor definido'

        for nome in ('data_nascimento', 'data_contratacao', 'data_demissao'):
            self.fields[nome].input_formats = ('%Y-%m-%d',)

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

    def clean_email(self):
        return (self.cleaned_data.get('email') or '').strip().lower()

    def clean_gestor(self):
        gestor = self.cleaned_data.get('gestor')
        if gestor and gestor.pk != self.instance.gestor_id and not gestor.eh_gestor:
            raise forms.ValidationError('Selecione um colaborador marcado como gestor.')
        return gestor

    def clean_eh_gestor(self):
        eh_gestor = self.cleaned_data.get('eh_gestor')
        if not eh_gestor and self.instance.pk:
            total = self.instance.liderados.count()
            if total:
                raise forms.ValidationError(
                    f'Este colaborador é o gestor responsável de {total} colaborador(es). '
                    'Troque o gestor deles antes de desmarcar esta opção.'
                )
        return eh_gestor

    def clean(self):
        cleaned_data = super().clean()
        self._aplicar_regra_de_desligamento(cleaned_data)
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

    def _aplicar_regra_de_desligamento(self, cleaned_data):
        """Data de demissão e status andam juntos.

        - Ativo (ou novo cadastro) com data de demissão até hoje: está sendo inativado.
        - Data de demissão futura: continua ativo e é inativado automaticamente no dia
          (ver desligamentos.services.processar_desligamentos_agendados).
        - Inativar exige a data de demissão.
        - Reativar apaga a data de demissão (senão o próximo salvamento o inativaria de novo).
        Inativos antigos sem data de demissão continuam editáveis sem erro.
        """
        if 'ativo' not in cleaned_data or 'data_demissao' not in cleaned_data:
            return
        era_ativo = self.instance.ativo if self.instance.pk else True
        ativo, demissao = cleaned_data['ativo'], cleaned_data['data_demissao']
        if era_ativo and demissao:
            cleaned_data['ativo'] = demissao > timezone.localdate()
        elif era_ativo and not ativo:
            self.add_error('data_demissao', 'Informe a data de demissão para inativar o colaborador.')
        elif not era_ativo and ativo:
            cleaned_data['data_demissao'] = None

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