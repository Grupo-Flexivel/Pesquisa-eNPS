from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = (
        'Habilita a extensão pgvector no banco atual (CREATE EXTENSION IF NOT EXISTS vector) '
        'e confirma que ela está disponível. A migration inicial de "talentos" já faz isso; '
        'use este comando para diagnosticar ou preparar o banco antes do migrate.'
    )

    def handle(self, *args, **options):
        if connection.vendor != 'postgresql':
            raise CommandError('O pgvector exige PostgreSQL.')
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
            if not cursor.fetchone():
                raise CommandError(
                    'A extensão "vector" não está instalada no servidor PostgreSQL. '
                    'Use a imagem pgvector/pgvector:pg16 ou instale o pacote postgresql-16-pgvector.'
                )
            try:
                cursor.execute('CREATE EXTENSION IF NOT EXISTS vector')
            except Exception as erro:
                raise CommandError(
                    f'Sem permissão para criar a extensão ({erro}). '
                    'Peça a um superusuário do banco: CREATE EXTENSION vector;'
                )
            cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            self.stdout.write(self.style.SUCCESS(f'pgvector {cursor.fetchone()[0]} habilitado.'))
