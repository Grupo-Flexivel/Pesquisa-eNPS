"""Orquestra o pipeline: PDF -> texto (pypdf) -> perfil (LLM) -> cargo (LLM) -> embedding."""
import logging

from organizacao.models import Cargo

from .duplicidade import fundir_duplicata
from .embeddings import gerar_embedding
from .erros import ErroProcessamento
from .extracao import extrair_texto
from .fluxo import inativar_candidato, mover_candidato
from .historico import Evento, registrar
from .llm import extrair_perfil
from .models import Candidato
from .sanitizacao import remover_bytes_nulos
from .services import CurriculumAutomationService

logger = logging.getLogger('talentos')

LIMITE_TEXTO_EMBEDDING = 12000  # caracteres do texto bruto anexados ao documento vetorizado


def cargos_disponiveis():
    """Cargos elegíveis para sugestão: toda a cadeia (departamento > seção > cargo) ativa no banco 'default'."""
    return Cargo.objects.using('default').filter(
        ativo=True, secao__ativo=True, secao__departamento__ativo=True,
    ).select_related('secao__departamento')


def documento_para_embedding(candidato):
    """Texto que representa o candidato na busca semântica (sem dados de contato)."""
    partes = [candidato.resumo]
    partes += [
        f'{e.get("cargo", "")} em {e.get("empresa", "")} ({e.get("periodo", "")}): {e.get("descricao", "")}'
        for e in candidato.experiencias
    ]
    partes += [f'{q.get("tipo", "")}: {q.get("descricao", "")}' for q in candidato.qualificacoes]
    partes.append(f'Cidade: {candidato.cidade}. Bairro: {candidato.bairro}.')
    partes.append(candidato.texto_extraido[:LIMITE_TEXTO_EMBEDDING])
    return '\n'.join(p for p in partes if p and p.strip())


def processar_candidato(candidato, usuario=None):
    """Executa o pipeline e persiste o resultado. Retorna o Candidato final em caso de
    sucesso (truthy) ou None em caso de falha (falsy) — compatível com checagens booleanas.

    Falhas esperadas (ErroProcessamento) marcam o candidato como ERRO, permitindo
    "reprocessar" depois; falhas inesperadas propagam. O resultado vai para o histórico,
    atribuído a `usuario` (o analista que disparou o processamento).

    Depois de extraídos os dados, `fundir_duplicata` verifica se já existe um cadastro com
    o mesmo e-mail, telefone ou nome (upsert): se existir, o resultado devolvido é o
    cadastro já existente (atualizado), não um novo registro — daí o retorno poder ter um
    pk diferente do `candidato` recebido.
    """
    try:
        with candidato.curriculo.open('rb') as arquivo:
            candidato.texto_extraido = extrair_texto(arquivo)

        # O texto do PDF já vem limpo (ver extracao.extrair_texto), mas o perfil que o LLM
        # devolve é sanitizado de novo por segurança: ele pode ecoar trechos do currículo
        # (ex.: no resumo ou nas descrições de experiência) e o Postgres rejeita bytes NUL.
        perfil = remover_bytes_nulos(extrair_perfil(candidato.texto_extraido))
        candidato.nome = perfil['nome']
        candidato.email = perfil['email']
        candidato.telefone = perfil['telefone']
        candidato.cidade = perfil['cidade']
        candidato.bairro = perfil['bairro']
        candidato.resumo = perfil['resumo']
        candidato.experiencias = perfil['experiencias']
        candidato.qualificacoes = perfil['qualificacoes']

        # Upsert: se já existe um cadastro com o mesmo e-mail/telefone/nome, este currículo
        # passa a atualizá-lo em vez de criar um duplicado.
        candidato = fundir_duplicata(candidato, usuario)

        candidato.embedding = gerar_embedding(documento_para_embedding(candidato))
    except ErroProcessamento as erro:
        candidato.processamento = Candidato.Processamento.ERRO
        candidato.erro_processamento = str(erro)[:300]
        candidato.save()
        registrar(candidato, Evento.PROCESSAMENTO_ERRO, usuario, candidato.erro_processamento)
        logger.warning('Candidato id=%s: processamento falhou (%s)', candidato.pk, erro)
        return None

    candidato.processamento = Candidato.Processamento.CONCLUIDO
    candidato.erro_processamento = ''
    candidato.save()

    # Triagem automática por busca semântica: cruza o embedding do candidato com o dos cargos
    # ativos e já aplica o direcionamento (cargo + etapa + ativo/inativo), sem aprovação manual.
    try:
        resultado = CurriculumAutomationService().triar(candidato, usuario)
    except ErroProcessamento as erro:
        candidato.processamento = Candidato.Processamento.ERRO
        candidato.erro_processamento = str(erro)[:300]
        candidato.save()
        # Falha na busca semântica nunca pode deixar o candidato na Triagem (etapa padrão do
        # model): sem confirmação de compatibilidade, ele vai para Pendência, igual ao caso de
        # "nenhum cargo compatível", para um analista tratar manualmente o erro ou o cargo.
        mover_candidato(candidato, Candidato.Status.PENDENCIA, usuario)
        if candidato.ativo:
            candidato = inativar_candidato(
                candidato,
                'Falha na triagem automática por busca semântica; aguardando análise manual do RH.',
                usuario,
            )
        registrar(candidato, Evento.PROCESSAMENTO_ERRO, usuario, candidato.erro_processamento)
        logger.warning('Candidato id=%s: triagem automática falhou (%s)', candidato.pk, erro)
        return None
    candidato = resultado.candidato

    if resultado.direcionado:
        nome_cargo = candidato.cargo.nome if candidato.cargo else '?'
        sugestao = f'Direcionado automaticamente ao cargo "{nome_cargo}" ({resultado.similaridade:.0%} de similaridade).'
    else:
        sugestao = 'Nenhum cargo compatível foi encontrado pela IA; aguardando triagem manual (Pendência).'
    registrar(candidato, Evento.PROCESSAMENTO_CONCLUIDO, usuario, f'Dados extraídos e indexados. {sugestao}')
    return candidato