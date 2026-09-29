"""Registro de eventos na linha do tempo do candidato (Módulo 4 - rastreabilidade)."""
from .models import HistoricoCandidatura

Evento = HistoricoCandidatura.Evento

ROTULOS_CAMPOS = {
    'nome': 'nome', 'email': 'e-mail', 'telefone': 'telefone', 'cidade': 'cidade', 'bairro': 'bairro',
}


def registrar(candidato, evento, usuario=None, descricao='', status_anterior='', status_novo=''):
    """Grava um evento com data/hora (agora) e o analista responsável (None = ação do sistema)."""
    autenticado = usuario is not None and usuario.is_authenticated
    return HistoricoCandidatura.objects.create(
        candidato=candidato, evento=evento, descricao=descricao[:500],
        status_anterior=status_anterior, status_novo=status_novo,
        usuario_id=usuario.pk if autenticado else None, # <-- Ajustado para usuario_id
        usuario_nome=(usuario.get_full_name() or usuario.get_username()) if autenticado else '',
    )


def registrar_edicao(candidato, usuario, campos_alterados, cargo_anterior):
    """Registra a edição manual feita na tela de detalhe.

    Só os NOMES dos campos alterados vão para o histórico (não os valores), para não
    duplicar dados pessoais na trilha de auditoria.
    """
    if 'cargo' in campos_alterados:
        de = str(cargo_anterior) if cargo_anterior else 'nenhum'
        para = str(candidato.cargo) if candidato.cargo else 'nenhum'
        registrar(candidato, Evento.CARGO_DEFINIDO, usuario, f'Cargo alterado de {de} para {para}.')
    dados = [ROTULOS_CAMPOS[c] for c in campos_alterados if c in ROTULOS_CAMPOS]
    if dados:
        registrar(candidato, Evento.DADOS_EDITADOS, usuario, f'Campos alterados: {", ".join(dados)}.')
