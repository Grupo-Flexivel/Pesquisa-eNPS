from .permissoes import contexto_acesso


def acesso_modulos(request):
    """Expõe `acesso` aos templates (sidebar dinâmica)."""
    return {'acesso': contexto_acesso(request.user)}
