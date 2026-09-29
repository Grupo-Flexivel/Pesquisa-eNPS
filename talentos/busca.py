"""Busca de candidatos: filtros estruturados + ranking semântico por similaridade de cosseno."""
from django.db.models import Q
from pgvector.django import CosineDistance

from organizacao.models import Cargo, Secao
from .embeddings import gerar_embedding
from .models import Candidato

LIMITE_PADRAO = 20


def filtrar(cidade='', bairro='', departamento=None, secao=None, cargo=None, status='', situacao='ativos'):
    """Candidatos com processamento concluído que atendem aos filtros (sem ranking).

    `situacao` controla o gerenciador usado: 'ativos' (padrão, também o default do
    manager `Candidato.objects`), 'inativos' ou 'todos'.

    Como os modelos de organização estão no banco 'default' e os candidatos no 'vector_db',
    mapeamos os IDs estruturais no Python para filtrar os IDs de forma segura.
    """
    if situacao == 'inativos':
        gerenciador = Candidato.all_objects.filter(ativo=False)
    elif situacao == 'todos':
        gerenciador = Candidato.all_objects.all()
    else:
        gerenciador = Candidato.objects.all()
    candidatos = gerenciador.filter(processamento=Candidato.Processamento.CONCLUIDO)

    if cidade:
        candidatos = candidatos.filter(cidade__icontains=cidade)
    if bairro:
        candidatos = candidatos.filter(bairro__icontains=bairro)
        
    # Mapeamento e filtragem estrutural baseada em IDs (Multi-database compliant)
    cargos_ids = set()
    
    if cargo:
        # Se passou o cargo, pegamos direto o ID dele
        cargos_ids.add(cargo.id if hasattr(cargo, 'id') else cargo)
    elif secao:
        # Se passou a seção, pegamos todos os IDs de cargos dessa seção no banco 'default'
        cargos_ids.update(
            Cargo.objects.using('default').filter(secao=secao).values_list('flat_id' if hasattr(Cargo, 'flat_id') else 'id', flat=True)
            # Nota: se a PK do cargo for 'id', use 'id'. Abaixo usamos 'id' por padrão:
        )
        # Recriando a linha limpa para o ID:
        cargos_ids = set(Cargo.objects.using('default').filter(secao=secao).values_list('id', flat=True))
    elif departamento:
        # Se passou o departamento, pegamos todas as seções do depto e depois os cargos
        secoes_ids = Secao.objects.using('default').filter(departamento=departamento).values_list('id', flat=True)
        cargos_ids = set(Cargo.objects.using('default').filter(secao_id__in=secoes_ids).values_list('id', flat=True))

    if cargos_ids:
        # Filtra candidatos onde (cargo_id está nos cargos permitidos) OR 
        # (cargo_id é nulo E cargo_sugerido_id está nos cargos permitidos)
        candidatos = candidatos.filter(
            Q(cargo_id__in=cargos_ids) | Q(cargo_id__isnull=True, cargo_sugerido_id__in=cargos_ids)
        )

    if status:
        candidatos = candidatos.filter(status=status)
        
    # Removido o select_related cruzado, pois as tabelas de cargo/departamento vivem no banco 'default'
    return candidatos


def buscar(texto='', limite=LIMITE_PADRAO, **filtros):
    """Busca por intenção em linguagem natural (exige PostgreSQL + pgvector quando há `texto`).

    Sem texto, devolve os mais recentes que atendem aos filtros. Com texto, ordena pela menor
    distância de cosseno e anexa `similaridade` (0-100) a cada candidato.
    Levanta ErroProcessamento se o embedding da consulta não puder ser gerado.
    """
    candidatos = filtrar(**filtros)
    texto = (texto or '').strip()
    if not texto:
        return list(candidatos[:limite])

    vetor = gerar_embedding(texto)
    ranqueados = list(
        candidatos.exclude(embedding=None)
        .annotate(distancia=CosineDistance('embedding', vetor))
        .order_by('distancia')[:limite]
    )
    for candidato in ranqueados:
        candidato.similaridade = max(0, round((1 - candidato.distancia) * 100))
    return ranqueados