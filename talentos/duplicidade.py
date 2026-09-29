"""Upsert inteligente de candidatos: evita duplicar o cadastro de quem já mandou currículo.

Critérios de unicidade flexíveis (basta um bater): e-mail, telefone (comparado só pelos
dígitos) ou nome completo (case-insensitive). São aplicados depois da extração pelo LLM,
que é o único momento em que esses dados existem — no upload só temos o arquivo.
"""
import re

from django.db import transaction
from django.db.models import Q

from .historico import Evento, registrar
from .models import Candidato, HistoricoCandidatura

# Campos copiados do "stub" recém-enviado para o cadastro já existente quando há fusão.
CAMPOS_COPIA = (
    'curriculo', 'nome_arquivo', 'hash_arquivo', 'nome', 'email', 'telefone', 'cidade', 'bairro',
    'resumo', 'experiencias', 'qualificacoes', 'texto_extraido', 'cargo_sugerido_id', 'justificativa_cargo',
)


def _somente_digitos(valor):
    return re.sub(r'\D', '', valor or '')


def localizar_duplicata(candidato, using='vector_db'):
    """Candidato existente (ativo ou inativo) com o mesmo e-mail, telefone ou nome. None se nenhum.

    Considera ativos e inativos de propósito: reenviar o currículo de alguém já inativado
    deve atualizar o cadastro dele, não criar um segundo.
    """
    email = (candidato.email or '').strip()
    nome = (candidato.nome or '').strip()
    telefone = _somente_digitos(candidato.telefone)

    candidatos = Candidato.all_objects.using(using)
    if candidato.pk:
        candidatos = candidatos.exclude(pk=candidato.pk)

    if telefone:
        # Comparação por dígitos (o mesmo telefone pode vir formatado de jeitos diferentes).
        for outro in candidatos.exclude(telefone='').iterator():
            if _somente_digitos(outro.telefone) == telefone:
                return outro

    filtro = Q()
    if email:
        filtro |= Q(email__iexact=email)
    if nome:
        filtro |= Q(nome__iexact=nome)
    if not filtro:
        return None
    return candidatos.filter(filtro).order_by('criado_em').first()


def fundir_duplicata(candidato, usuario=None):
    """Se `candidato` (recém-processado) duplica um cadastro existente, funde os dois.

    O cadastro já existente é atualizado com o novo arquivo/dados e o "stub" criado no
    upload é descartado — preservando o histórico dele, que é reatribuído ao cadastro
    definitivo antes da exclusão. Retorna o Candidato canônico: o existente (se houve
    fusão) ou o próprio `candidato` (caso contrário), para o chamador continuar o pipeline.
    """
    if not candidato.pk:
        return candidato

    with transaction.atomic(using='vector_db'):
        encontrado = localizar_duplicata(candidato)
        if encontrado is None:
            return candidato

        existente = Candidato.all_objects.using('vector_db').select_for_update().get(pk=encontrado.pk)
        arquivo_antigo = existente.curriculo.name
        novos_valores = {campo: getattr(candidato, campo) for campo in CAMPOS_COPIA}

        # Preserva a trilha de auditoria do upload que originou a fusão.
        HistoricoCandidatura.objects.using('vector_db').filter(candidato=candidato).update(candidato=existente)
        candidato.delete(using='vector_db')  # libera o hash_arquivo (único) antes de reaproveitá-lo

        for campo, valor in novos_valores.items():
            setattr(existente, campo, valor)
        existente.save(using='vector_db')

        if arquivo_antigo and arquivo_antigo != existente.curriculo.name:
            existente.curriculo.storage.delete(arquivo_antigo)

        registrar(
            existente, Evento.CURRICULO_ATUALIZADO, usuario,
            'Currículo atualizado: já existia um cadastro com o mesmo e-mail, telefone ou nome; '
            'os dados e o arquivo foram substituídos pela nova versão enviada.',
        )

    return existente
