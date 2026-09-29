from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from organizacao.models import Cargo

from talentos.embeddings import gerar_embedding
from talentos.erros import ErroProcessamento
from talentos.models import Candidato
from talentos.services import CurriculumAutomationService


class Command(BaseCommand):
    help = (
        'Mostra a similaridade de cosseno entre um currículo e cada cargo ativo, para '
        'calibrar CURRICULO_MATCH_THRESHOLD com dados reais em vez de "chutar" um número. '
        'Use --candidato <id> para um currículo já cadastrado, ou --texto "..." para '
        'testar um texto qualquer (ex.: um trecho de currículo) sem precisar de upload.'
    )

    def add_arguments(self, parser):
        grupo = parser.add_mutually_exclusive_group(required=True)
        grupo.add_argument('--candidato', type=int, help='ID do Candidato (precisa ter embedding gerado).')
        grupo.add_argument('--texto', type=str, help='Texto livre para gerar um embedding na hora.')

    def handle(self, *args, **options):
        if options['candidato']:
            try:
                candidato = Candidato.all_objects.get(pk=options['candidato'])
            except Candidato.DoesNotExist:
                raise CommandError(f'Candidato #{options["candidato"]} não encontrado.')
            if not candidato.embedding:
                raise CommandError('Este candidato ainda não tem embedding (processe o currículo primeiro).')
            embedding = candidato.embedding
        else:
            try:
                embedding = gerar_embedding(options['texto'])
            except ErroProcessamento as erro:
                raise CommandError(str(erro))

        try:
            ranking = CurriculumAutomationService().ranking_similaridade(embedding)
        except ErroProcessamento as erro:
            raise CommandError(str(erro))

        if not ranking:
            self.stdout.write(self.style.WARNING('Nenhum cargo ativo cadastrado para comparar.'))
            return

        nomes = {c.id: c.nome_completo for c in Cargo.objects.using('default').filter(
            pk__in=[cargo_id for cargo_id, _ in ranking],
        ).select_related('secao__departamento')}

        limiar = settings.CURRICULO_MATCH_THRESHOLD
        self.stdout.write(f'Limiar configurado (CURRICULO_MATCH_THRESHOLD): {limiar:.0%}\n')
        for cargo_id, similaridade in ranking:
            marca = self.style.SUCCESS('>= limiar') if similaridade >= limiar else self.style.WARNING('< limiar')
            self.stdout.write(f'{similaridade:6.1%}  {marca}  {nomes.get(cargo_id, f"cargo #{cargo_id}")}')
