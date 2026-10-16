from .models import Notificacao


LIMITE_SININHO = 10


def notificacoes(request):
    """Expõe ao topo da tela as notificações não lidas do usuário logado (sininho)."""
    user = getattr(request, 'user', None)
    if not (user and user.is_authenticated and user.is_staff):
        return {}
    nao_lidas = Notificacao.objects.filter(destinatario=user, lida=False)
    return {
        'notificacoes_nao_lidas': list(nao_lidas[:LIMITE_SININHO]),
        'total_notificacoes_nao_lidas': nao_lidas.count(),
    }
