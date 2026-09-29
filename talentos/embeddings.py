"""Geração de embeddings via endpoint compatível com POST /v1/embeddings."""
import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

from .erros import ErroProcessamento

logger = logging.getLogger('talentos')

TIMEOUT_SEGUNDOS = 30


def gerar_embedding(texto):
    """Retorna o vetor (list[float]) de `texto`, com o tamanho de EMBEDDING_DIMENSIONS."""
    if not settings.EMBEDDING_API_KEY:
        raise ErroProcessamento('EMBEDDING_API_KEY não configurada; não é possível gerar o embedding.')
    if not settings.EMBEDDING_API_URL.startswith(('https://', 'http://')):
        raise ErroProcessamento('EMBEDDING_API_URL inválida.')

    corpo = json.dumps({
        'model': settings.EMBEDDING_MODEL,
        'input': texto,
        'dimensions': settings.EMBEDDING_DIMENSIONS,
    }).encode()
    requisicao = urllib.request.Request(
        settings.EMBEDDING_API_URL, data=corpo, method='POST',
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {settings.EMBEDDING_API_KEY}'},
    )
    try:
        # A URL vem da configuração do servidor, nunca de entrada do usuário.
        with urllib.request.urlopen(requisicao, timeout=TIMEOUT_SEGUNDOS) as resposta:  # noqa: S310
            dados = json.load(resposta)
        vetor = dados['data'][0]['embedding']
    except urllib.error.HTTPError as erro:
        logger.error('Embeddings: HTTP %s', erro.code)
        raise ErroProcessamento(f'O serviço de embeddings recusou a requisição (HTTP {erro.code}).')
    except (urllib.error.URLError, TimeoutError, OSError):
        logger.exception('Embeddings: falha de rede')
        raise ErroProcessamento('O serviço de embeddings está indisponível. Tente reprocessar em instantes.')
    except (ValueError, KeyError, IndexError, TypeError):
        raise ErroProcessamento('Resposta inesperada do serviço de embeddings.')

    if len(vetor) != settings.EMBEDDING_DIMENSIONS:
        raise ErroProcessamento(
            f'O modelo retornou {len(vetor)} dimensões, mas o banco espera {settings.EMBEDDING_DIMENSIONS}.'
        )
    return vetor
