import logging

from django.core.cache import cache

from .services import atualizar_periodos


logger = logging.getLogger(__name__)

CHAVE_CACHE = 'experiencia:ultima_atualizacao'
INTERVALO_SEGUNDOS = 5 * 60


class AtualizacaoExperienciaMiddleware:
    """Roda a rotina do período de experiência durante o uso do painel, no máximo a cada 5 minutos.

    Garante que os cards avancem com a passagem dos dias (e que o sininho receba os avisos)
    mesmo que o agendamento externo (`experiencia_job` / cron) não esteja ativo.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if user and user.is_authenticated and user.is_staff and cache.add(CHAVE_CACHE, True, INTERVALO_SEGUNDOS):
            try:
                atualizar_periodos()
            except Exception:
                logger.exception('Falha ao atualizar o período de experiência')
        return self.get_response(request)
