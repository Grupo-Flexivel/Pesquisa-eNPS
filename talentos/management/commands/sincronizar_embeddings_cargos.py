from django.core.management.base import BaseCommand

from talentos.erros import ErroProcessamento
from talentos.services import CurriculumAutomationService


class Command(BaseCommand):
    help = (
        'Recalcula o embedding de todos os cargos ativos (CurriculumAutomationService.'
        'sincronizar_embeddings_cargos). Rode depois de renomear um cargo, mudar sua seção/'
        'departamento, ou depois de alterar o texto usado para representar o cargo na busca '
        'semântica (services.CurriculumAutomationService._texto_cargo) — sem isso, cargos já '
        'sincronizados continuam com o embedding do texto antigo.'
    )

    def handle(self, *args, **options):
        try:
            total = CurriculumAutomationService().sincronizar_embeddings_cargos()
        except ErroProcessamento as erro:
            raise SystemExit(f'Falha ao sincronizar: {erro}')
        self.stdout.write(self.style.SUCCESS(f'{total} cargo(s) sincronizado(s).'))
