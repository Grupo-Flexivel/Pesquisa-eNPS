from django.core.management.base import BaseCommand

from experiencia.services import atualizar_periodos


class Command(BaseCommand):
    help = (
        'Atualiza o Kanban de período de experiência: cria cards de novos contratados, move para '
        '"Vencendo" quem está a 15 dias ou menos do fim, efetiva quem terminou o período e envia as '
        'notificações. Idempotente: agende para rodar diariamente (ou com mais frequência).'
    )

    def handle(self, *args, **options):
        resumo = atualizar_periodos()
        if not resumo:
            self.stdout.write('Nenhuma alteração no período de experiência.')
            return
        detalhes = ', '.join(f'{chave}: {total}' for chave, total in sorted(resumo.items()))
        self.stdout.write(self.style.SUCCESS(f'Período de experiência atualizado ({detalhes}).'))
