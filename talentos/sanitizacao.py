"""Sanitização de texto extraído de PDFs, antes de persistir no PostgreSQL.

PDFs malformados ou com fontes/encoding corrompidos às vezes fazem o pypdf extrair bytes
NUL (0x00) no meio do texto (ex.: "experi\x00ência"). O PostgreSQL rejeita nativamente
qualquer string com esse byte em colunas de texto ou JSON (`DataError: PostgreSQL text
fields cannot contain NUL (0x00) bytes`), o que derruba o `candidato.save()` no pipeline.
"""


def remover_bytes_nulos(valor):
    """Remove bytes NUL (0x00) de `valor`, percorrendo recursivamente dicts e listas.

    Cobre tanto texto simples (ex.: `texto_extraido`, `resumo`) quanto os campos JSONField
    do Candidato (`experiencias`, `qualificacoes`), que são listas de dicts. Valores de
    outros tipos (int, bool, None, etc.) voltam inalterados.
    """
    if isinstance(valor, str):
        return valor.replace('\x00', '')
    if isinstance(valor, dict):
        return {chave: remover_bytes_nulos(item) for chave, item in valor.items()}
    if isinstance(valor, list):
        return [remover_bytes_nulos(item) for item in valor]
    return valor
