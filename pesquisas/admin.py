from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import AdminUserCreationForm as DjangoAdminUserCreationForm, UserChangeForm
from django import forms
import logging

from .models import Colaborador, DetalheResposta, Empresa, PerfilAcesso, Pergunta, Pesquisa, RespostaPesquisa, User


logger = logging.getLogger('pesquisas.auth')


class AdminUserCreationForm(DjangoAdminUserCreationForm):
    auth_source = forms.ChoiceField(choices=User.AuthSource.choices, label='Origem da autenticação')
    is_admin_total = forms.BooleanField(label='Administrador total', required=False, help_text='Designa acesso total de administrador ao painel.')
    perfil_personalizado = forms.ModelChoiceField(
        queryset=PerfilAcesso.objects.all(),
        required=False,
        label='Perfil de acesso personalizado',
        help_text='Selecione um perfil customizado caso não seja administrador total.'
    )

    class Meta(DjangoAdminUserCreationForm.Meta):
        model = User
        fields = ('username', 'auth_source', 'is_admin_total', 'perfil_personalizado')

    def save(self, commit=True):
        user = super().save(commit=False)
        if self.cleaned_data['auth_source'] == User.AuthSource.DIRECTORY:
            user.set_unusable_password()
        if commit:
            user.save()
        return user


class AdminUserChangeForm(UserChangeForm):
    auth_source = forms.ChoiceField(choices=User.AuthSource.choices, label='Origem da autenticação')
    is_admin_total = forms.BooleanField(label='Administrador total', required=False, help_text='Designa acesso total de administrador ao painel.')
    perfil_personalizado = forms.ModelChoiceField(
        queryset=PerfilAcesso.objects.all(),
        required=False,
        label='Perfil de acesso personalizado',
        help_text='Selecione um perfil customizado caso não seja administrador total.'
    )
    local_password = forms.CharField(
        label='Nova senha local', required=False, strip=False,
        widget=forms.PasswordInput(render_value=False),
        help_text='Obrigatória ao trocar de Active Directory para senha local.',
    )

    class Meta:
        model = User
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['auth_source'].initial = self.instance.auth_source

    def clean(self):
        cleaned = super().clean()
        if (
            self.instance.auth_source == User.AuthSource.DIRECTORY
            and cleaned.get('auth_source') == User.AuthSource.LOCAL
            and not cleaned.get('local_password')
        ):
            self.add_error('local_password', 'Defina uma nova senha local ao desvincular do AD.')
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        source_before = self.instance.auth_source
        source_after = self.cleaned_data['auth_source']
        user.auth_source = source_after

        if source_after == User.AuthSource.DIRECTORY:
            if source_before != User.AuthSource.DIRECTORY:
                user.set_unusable_password()
        elif source_before == User.AuthSource.DIRECTORY:
            user.directory_guid = None
            user.set_password(self.cleaned_data['local_password'])
        elif self.cleaned_data.get('local_password'):
            user.set_password(self.cleaned_data['local_password'])

        if commit:
            user.save()
        return user


def _somente_administrador(request):
    return request.user.is_active and request.user.is_admin_total


# Módulo administrativo (Django admin / gestão de usuários): só o Administrador total.
admin.site.has_permission = _somente_administrador
admin.site.site_header = 'Administração eNPS'
admin.site.site_title = 'eNPS'


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    add_form = AdminUserCreationForm
    form = AdminUserChangeForm
    list_display = ('username', 'is_admin_total', 'perfil_personalizado', 'auth_source', 'directory_guid', 'is_active')
    list_filter = ('is_admin_total', 'perfil_personalizado', 'auth_source', 'is_active')
    readonly_fields = ('directory_guid',)
    
    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        ('Informações pessoais', {'fields': ('first_name', 'last_name', 'email')}),
        ('Acesso aos módulos', {'fields': ('is_admin_total', 'perfil_personalizado', 'is_active')}),
        ('Datas importantes', {'fields': ('last_login', 'date_joined')}),
        ('Autenticação corporativa', {'fields': ('auth_source', 'directory_guid', 'local_password')}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ('Acesso aos módulos', {'fields': ('is_admin_total', 'perfil_personalizado')}),
        ('Autenticação corporativa', {'fields': ('auth_source',)}),
    )
    actions = ('relink_directory_identity',)

    def save_model(self, request, obj, form, change):
        previous_source = None
        if change:
            previous_source = User.objects.only('auth_source').get(pk=obj.pk).auth_source
        super().save_model(request, obj, form, change)
        if previous_source != obj.auth_source:
            logger.info(
                'admin=%s auth_source_changed user=%s from=%s to=%s',
                request.user.username, obj.username, previous_source or '-', obj.auth_source,
            )

    @admin.action(description='Religar identidade AD (limpar objectGUID)')
    def relink_directory_identity(self, request, queryset):
        users = queryset.filter(auth_source=User.AuthSource.DIRECTORY)
        count = users.update(directory_guid=None)
        logger.info('admin=%s relink_directory_identity count=%s', request.user.username, count)
        self.message_user(request, f'{count} identidade(s) AD liberada(s) para novo vínculo.')


@admin.register(PerfilAcesso)
class PerfilAcessoAdmin(admin.ModelAdmin):
    list_display = ('nome',)
    search_fields = ('nome',)


@admin.register(Colaborador)
class ColaboradorAdmin(admin.ModelAdmin):
    list_display = ('nome', 'documento', 'nomes_empresas', 'eh_gestor', 'gestor', 'data_contratacao', 'data_demissao', 'ativo')
    list_filter = ('empresas', 'ativo', 'eh_gestor')
    search_fields = ('nome', 'documento', 'empresas__nome')
    list_select_related = ('gestor',)
    filter_horizontal = ('empresas',)

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related('empresas')

    @admin.display(description='Empresas')
    def nomes_empresas(self, obj):
        return obj.nomes_empresas

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == 'gestor':
            kwargs['queryset'] = Colaborador.objects.filter(eh_gestor=True).order_by('nome')
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(Empresa)
class EmpresaAdmin(admin.ModelAdmin):
    search_fields = ('nome',)


class PerguntaInline(admin.TabularInline):
    model = Pergunta
    extra = 3
    min_num = 3
    validate_min = True


@admin.register(Pesquisa)
class PesquisaAdmin(admin.ModelAdmin):
    list_display = ('titulo', 'data_inicio', 'data_final', 'edicao_bloqueada', 'criado_por')
    readonly_fields = ('link_uuid', 'criado_por')
    inlines = (PerguntaInline,)

    def save_model(self, request, obj, form, change):
        if not obj.pk:
            obj.criado_por = request.user
        super().save_model(request, obj, form, change)

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.edicao_bloqueada:
            return tuple(field.name for field in obj._meta.fields)
        return super().get_readonly_fields(request, obj)

    def get_inline_instances(self, request, obj=None):
        if obj and obj.edicao_bloqueada:
            return []
        return super().get_inline_instances(request, obj)

    def has_change_permission(self, request, obj=None):
        if obj and obj.edicao_bloqueada:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and obj.edicao_bloqueada:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(RespostaPesquisa)
class RespostaPesquisaAdmin(admin.ModelAdmin):
    list_display = ('id', 'pesquisa', 'data_resposta')
    list_filter = ('pesquisa',)
    readonly_fields = ('pesquisa', 'data_resposta', 'hash_documento_respondente')
    search_fields = ('pesquisa__titulo',)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DetalheResposta)
class DetalheRespostaAdmin(admin.ModelAdmin):
    list_display = ('id', 'pergunta', 'resposta_pesquisa')
    readonly_fields = ('resposta_pesquisa', 'pergunta', 'valor_texto', 'valor_inteiro')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
