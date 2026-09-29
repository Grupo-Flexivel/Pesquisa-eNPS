"""RBAC por módulos do painel.

Perfis e acessos:
  * Administrador Total (is_admin_total=True) – acesso irrestrito, inclusive ao módulo administrativo.
  * Perfil Personalizado (perfil_personalizado) – acesso restrito aos módulos permitidos naquele perfil.

As permissões são 100% locais (não vêm de grupos do AD).

Chaves de módulo são hierárquicas: ter "talentos" dá acesso a todas as
chaves "talentos.*"; ter apenas "talentos.organizacao" dá acesso só a ela.
"""
from functools import wraps

from django import forms
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied


ENPS = 'enps'
DESLIGAMENTOS = 'desligamentos'
GERAL = 'geral'
TALENTOS = 'talentos'
TALENTOS_DASHBOARD = 'talentos.dashboard'
TALENTOS_ORGANIZACAO = 'talentos.organizacao'
TALENTOS_CURRICULOS = 'talentos.curriculos'
TALENTOS_BUSCA = 'talentos.busca'
TALENTOS_KANBAN = 'talentos.kanban'

# (chave, rótulo) exibidos nos formulários de perfil/usuário.
MODULOS_CHOICES = [
    (GERAL, 'Módulo Geral'), # <-- 2. Adicionado nas opções do formulário
    (ENPS, 'eNPS'),
    (DESLIGAMENTOS, 'Desligamento'),
    (TALENTOS, 'Banco de Talentos (todos os submódulos)'),
    (TALENTOS_DASHBOARD, 'Banco de Talentos › Dashboard'),
    (TALENTOS_ORGANIZACAO, 'Banco de Talentos › Estrutura organizacional'),
    (TALENTOS_CURRICULOS, 'Banco de Talentos › Currículos'),
    (TALENTOS_BUSCA, 'Banco de Talentos › Busca semântica'),
    (TALENTOS_KANBAN, 'Banco de Talentos › Kanban'),
]
CHAVES_VALIDAS = {chave for chave, _ in MODULOS_CHOICES}


def _chave_coberta(permitidas, chave):
    """True se `chave` (ou um ancestral dela, ex.: 'talentos') está em `permitidas`."""
    partes = chave.split('.')
    return any('.'.join(partes[:i]) in permitidas for i in range(1, len(partes) + 1))


def usuario_tem_acesso(user, *chaves):
    """True se o usuário acessa QUALQUER uma das chaves informadas."""
    if not (user.is_authenticated and user.is_active and user.is_staff):
        return False
    
    # Libera acesso total para superusers ou se is_admin_total for True
    if user.is_superuser or getattr(user, 'is_admin_total', False):
        return True
    
    permitidas = []
    if user.perfil_personalizado and hasattr(user.perfil_personalizado, 'modulos_permitidos'):
        permitidas = user.perfil_personalizado.modulos_permitidos or []
        
    return any(_chave_coberta(set(permitidas), chave) for chave in chaves)

def contexto_acesso(user):
    """Flags usadas pela sidebar para renderizar apenas o que o usuário pode ver."""
    return {
        'geral': usuario_tem_acesso(user, GERAL),
        'enps': usuario_tem_acesso(user, ENPS),
        'desligamentos': usuario_tem_acesso(user, DESLIGAMENTOS),
        'colaboradores': usuario_tem_acesso(user, ENPS, DESLIGAMENTOS),
        'talentos_organizacao': usuario_tem_acesso(user, TALENTOS_ORGANIZACAO),
        'talentos_curriculos': usuario_tem_acesso(user, TALENTOS_CURRICULOS),
        'talentos_dashboard': usuario_tem_acesso(user, TALENTOS_DASHBOARD),
        'talentos_busca': usuario_tem_acesso(user, TALENTOS_BUSCA),
        'talentos_kanban': usuario_tem_acesso(user, TALENTOS_KANBAN),
        'talentos': usuario_tem_acesso(user, TALENTOS_DASHBOARD, TALENTOS_ORGANIZACAO,
                                    TALENTOS_CURRICULOS, TALENTOS_BUSCA, TALENTOS_KANBAN),
        'administrativo': bool(user.is_authenticated and user.is_active and user.is_staff
                                and (user.is_superuser or getattr(user, 'is_admin_total', False))),
    }


def pagina_inicial(user):
    """Nome da URL de entrada mais adequada ao usuário (usado no redirect pós-login)."""
    if usuario_tem_acesso(user, GERAL):
        return 'geral:home' 
    if usuario_tem_acesso(user, ENPS, DESLIGAMENTOS):
        return 'pesquisas:dashboard'
    if usuario_tem_acesso(user, TALENTOS_DASHBOARD):
        return 'talentos:painel'
    if usuario_tem_acesso(user, TALENTOS_KANBAN):
        return 'talentos:quadro'
    if usuario_tem_acesso(user, TALENTOS_BUSCA):
        return 'talentos:busca'
    if usuario_tem_acesso(user, TALENTOS_CURRICULOS):
        return 'talentos:lista'
    if usuario_tem_acesso(user, TALENTOS_ORGANIZACAO):
        return 'organizacao:departamento_lista'
    return None


def modulo_required(*chaves):
    """Decorator de view: exige acesso a QUALQUER uma das chaves.

    Sem chaves, exige apenas acesso ao painel. Anônimo ou sem acesso ao painel → login.
    Autenticado sem a permissão → 403.
    """
    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            user = request.user
            if not (user.is_authenticated and user.is_staff):
                return redirect_to_login(request.get_full_path())
            if chaves and not usuario_tem_acesso(user, *chaves):
                raise PermissionDenied
            return view(request, *args, **kwargs)
        return wrapper
    return decorator


def administrador_required(view):
    """Decorator de view: exige Administrador total (usado na gestão de Perfis de Acesso).

    Conceder acesso é, em si, uma operação sensível — por isso fica restrita ao mesmo
    perfil que já controla os campos de acesso no formulário de colaborador.
    Anônimo/sem acesso ao painel → login. Autenticado mas não administrador → 403.
    """
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        user = request.user
        if not (user.is_authenticated and user.is_staff):
            return redirect_to_login(request.get_full_path())
        if not (user.is_superuser or getattr(user, 'is_admin_total', False)):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapper


class ModuloRequiredMixin:
    """Equivalente de modulo_required para class-based views."""
    modulos_exigidos = ()

    def dispatch(self, request, *args, **kwargs):
        return modulo_required(*self.modulos_exigidos)(super().dispatch)(request, *args, **kwargs)


class ModulosFormField(forms.MultipleChoiceField):
    """Checkboxes de módulos para o campo de seleção de permissões."""

    def __init__(self, **kwargs):
        kwargs.setdefault('choices', MODULOS_CHOICES)
        kwargs.setdefault('widget', forms.CheckboxSelectMultiple)
        kwargs.setdefault('required', False)
        kwargs.setdefault('label', 'Módulos permitidos')
        kwargs.setdefault('help_text', 'Selecione os módulos liberados para este perfil.')
        super().__init__(**kwargs)