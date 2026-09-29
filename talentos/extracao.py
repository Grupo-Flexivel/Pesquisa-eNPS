"""Etapa 1 do pipeline: validar o PDF e extrair o texto com pypdf."""
import logging
import re

from django.conf import settings
from pypdf import PdfReader

from .erros import ErroProcessamento
from .sanitizacao import remover_bytes_nulos

logger = logging.getLogger('talentos')

MINIMO_CARACTERES = 50  # abaixo disso o PDF provavelmente é imagem digitalizada


def validar_upload(arquivo):
    """Valida nome, tamanho e assinatura do arquivo enviado. Levanta ErroProcessamento."""
    if not (arquivo.name or '').lower().endswith('.pdf'):
        raise ErroProcessamento('Envie o currículo em formato PDF.')
    limite = settings.CURRICULO_MAX_MB * 1024 * 1024
    if arquivo.size > limite:
        raise ErroProcessamento(f'O arquivo excede o limite de {settings.CURRICULO_MAX_MB} MB.')
    cabecalho = arquivo.read(5)
    arquivo.seek(0)
    if cabecalho != b'%PDF-':  # não confia apenas na extensão/Content-Type
        raise ErroProcessamento('O arquivo não é um PDF válido.')


def extrair_texto(arquivo):
    """Retorna o texto do PDF (arquivo aberto em modo binário)."""
    try:
        leitor = PdfReader(arquivo)
        if leitor.is_encrypted and not leitor.decrypt(''):
            raise ErroProcessamento('O PDF está protegido por senha.')
        if len(leitor.pages) > settings.CURRICULO_MAX_PAGINAS:
            raise ErroProcessamento(
                f'O PDF tem mais de {settings.CURRICULO_MAX_PAGINAS} páginas; não parece um currículo.'
            )
        texto = '\n'.join((pagina.extract_text() or '') for pagina in leitor.pages)
    except ErroProcessamento:
        raise
    except Exception:  # PDF malformado pode gerar vários tipos de erro no pypdf
        logger.exception('Falha ao ler PDF')
        raise ErroProcessamento('Não foi possível ler o PDF (arquivo corrompido ou em formato incomum).')

    texto = remover_bytes_nulos(texto)  # pypdf pode extrair \x00 de PDFs com fonte/encoding corrompidos
    texto = re.sub(r'[ \t]+', ' ', texto)
    texto = re.sub(r'\n{3,}', '\n\n', texto).strip()
    if len(texto) < MINIMO_CARACTERES:
        raise ErroProcessamento(
            'O PDF não contém texto extraível (provavelmente é uma imagem digitalizada; OCR não é suportado).'
        )
    if len(texto) > settings.CURRICULO_MAX_CARACTERES:
        raise ErroProcessamento('O currículo é extenso demais para ser processado.')
    return texto
