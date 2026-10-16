from .services import eh_gestor, pode_gerir


def gestor_experiencia(request):
    """Itens da sidebar do módulo: Kanban para gestores (mesmo sem o módulo no perfil) e
    "Gerenciar avaliações" apenas para o RH."""
    user = getattr(request, 'user', None)
    if not (user and user.is_authenticated and user.is_staff):
        return {}
    return {'gestor_experiencia': eh_gestor(user), 'rh_experiencia': pode_gerir(user)}
