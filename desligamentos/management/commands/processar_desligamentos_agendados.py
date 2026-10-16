from django.core.management.base import BaseCommand

from desligamentos.services import processar_desligamentos_agendados


class Command(BaseCommand):
    help = (
        'Inativa os colaboradores cuja data de demissão chegou e leva os desligamentos agendados para '
        '"Pendentes" no Kanban. Idempotente: agende para rodar diariamente (ou com mais frequência).'
    )

    def handle(self, *args, **options):
        total = processar_desligamentos_agendados()
        self.stdout.write(self.style.SUCCESS(f'Desligamentos processados: {total}.'))
