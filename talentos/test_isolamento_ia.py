"""Auditoria arquitetural (requisito 4): a integração com a OpenAI só pode existir no
módulo `talentos`, restrita à análise de currículos (`llm.py`) e à busca semântica
(`embeddings.py`). Este teste falha se alguém introduzir uma nova dependência da OpenAI
em outro lugar do sistema, evitando a "IA solta" fora do Banco de Talentos.
"""
from pathlib import Path

from django.test import SimpleTestCase

BASE_DIR = Path(__file__).resolve().parent.parent
IGNORAR_DIRETORIOS = {'.git', 'node_modules', 'venv', '.venv', '__pycache__', 'staticfiles', 'media'}
MODULOS_COM_PACOTE_OPENAI = {'llm.py'}  # únicos arquivos que podem `import openai`


def _arquivos_python():
    for caminho in BASE_DIR.rglob('*.py'):
        relativo = caminho.relative_to(BASE_DIR)
        if IGNORAR_DIRETORIOS & set(relativo.parts):
            continue
        yield relativo, caminho


class IsolamentoIATests(SimpleTestCase):
    def test_openai_so_e_referenciada_dentro_do_modulo_talentos(self):
        """Nenhum outro app (nem settings de outro projeto) deve mencionar a OpenAI."""
        ofensores = []
        for relativo, caminho in _arquivos_python():
            if relativo.parts[0] == 'talentos':
                continue
            if relativo == Path('enps/settings.py'):
                continue  # única exceção: aqui só se carrega a configuração (OPENAI_API_KEY etc.)
            texto = caminho.read_text(encoding='utf-8', errors='ignore')
            if 'openai' in texto.lower():
                ofensores.append(str(relativo))
        self.assertEqual(ofensores, [], f'Referência à OpenAI fora do módulo talentos: {ofensores}')

    def test_pacote_openai_e_usado_somente_em_llm_py(self):
        """Dentro de `talentos`, só `llm.py` (análise de currículo) importa o SDK da OpenAI.

        A busca semântica (`embeddings.py`) fala com o serviço de embeddings por HTTP puro,
        sem o SDK — mantendo os dois únicos pontos de uso (análise e busca) claramente
        isolados do resto do pipeline.
        """
        ofensores = []
        for relativo, caminho in _arquivos_python():
            if relativo.parts[0] != 'talentos':
                continue
            if relativo.name in MODULOS_COM_PACOTE_OPENAI or relativo.name.startswith('test'):
                continue
            texto = caminho.read_text(encoding='utf-8', errors='ignore')
            if 'import openai' in texto or 'from openai' in texto:
                ofensores.append(str(relativo))
        self.assertEqual(ofensores, [], f'Uso do pacote openai fora de llm.py: {ofensores}')
