from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from pesquisas.permissoes import modulo_required

from .models import Notificacao


def _voltar(request, destino=''):
    if destino and url_has_allowed_host_and_scheme(destino, allowed_hosts={request.get_host()}):
        return redirect(destino)
    anterior = request.META.get('HTTP_REFERER', '')
    if url_has_allowed_host_and_scheme(anterior, allowed_hosts={request.get_host()}):
        return redirect(anterior)
    return redirect('pesquisas:dashboard')


@modulo_required()
@require_POST
def abrir(request, pk):
    """Marca a notificação como lida e leva o usuário à tela relacionada."""
    notificacao = get_object_or_404(Notificacao, pk=pk, destinatario=request.user)
    if not notificacao.lida:
        notificacao.lida = True
        notificacao.lida_em = timezone.now()
        notificacao.save(update_fields=['lida', 'lida_em'])
    return _voltar(request, notificacao.link)


@modulo_required()
@require_POST
def marcar_todas_lidas(request):
    Notificacao.objects.filter(destinatario=request.user, lida=False).update(lida=True, lida_em=timezone.now())
    return _voltar(request)
