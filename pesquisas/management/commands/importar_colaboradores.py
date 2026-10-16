from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from pesquisas.importacao import COLUNAS_OBRIGATORIAS, COLUNAS_OPCIONAIS, ErroImportacao, importar_colaboradores


class Command(BaseCommand):
    help = (
        'Importa colaboradores de um CSV (UTF-8 ou Windows-1252, vírgula ou ponto e vírgula) ou XLSX. '
        f'Colunas obrigatórias: {", ".join(COLUNAS_OBRIGATORIAS)}. '
        f'Opcionais: {", ".join(COLUNAS_OPCIONAIS)}. A ordem das colunas não importa.'
    )

    def add_arguments(self, parser):
        parser.add_argument('arquivo', type=Path)

    def handle(self, *args, **options):
        caminho = options['arquivo'].resolve()
        if not caminho.is_file():
            raise CommandError(f'Arquivo não encontrado: {caminho}')

        try:
            resultado = importar_colaboradores(caminho.read_bytes(), caminho.name)
        except OSError as exc:
            raise CommandError(f'Não foi possível ler o arquivo: {exc}') from exc
        except ErroImportacao as exc:
            raise CommandError('Nada foi importado:\n' + '\n'.join(exc.erros)) from exc

        if resultado.colunas_ignoradas:
            self.stdout.write(self.style.WARNING(f'Colunas ignoradas: {", ".join(resultado.colunas_ignoradas)}'))
        self.stdout.write(self.style.SUCCESS(
            f'Importação concluída: {resultado.criados} criados e {resultado.atualizados} atualizados.'
        ))
