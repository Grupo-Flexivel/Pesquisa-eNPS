"""Cálculo do contrato de experiência (90 dias corridos a partir da data de contratação).

Convenção da CLT: o dia da contratação é o 1º dia do contrato, logo o último dia é
`data_contratacao + 89 dias`. Ex.: contratado em 01/01 → experiência termina em 31/03.
"""
from datetime import timedelta


DURACAO_DIAS = 90
ALERTA_DIAS = 15  # a partir de quantos dias restantes o card vai para "Vencendo"
DIAS_DESLIGADO_NO_QUADRO = 15  # depois disso o card da coluna "Desligado" sai do Kanban


def fim_experiencia(data_contratacao):
    """Último dia do contrato de experiência."""
    return data_contratacao + timedelta(days=DURACAO_DIAS - 1)


def dias_restantes(data_contratacao, hoje):
    """Dias até o último dia do contrato (0 = vence hoje; negativo = já terminou)."""
    return (fim_experiencia(data_contratacao) - hoje).days


def inicio_alerta(data_contratacao):
    """Primeiro dia em "Vencendo" (faltam ALERTA_DIAS dias): a avaliação do gestor é liberada."""
    return fim_experiencia(data_contratacao) - timedelta(days=ALERTA_DIAS)


def em_alerta(data_contratacao, hoje):
    """True quando faltam ALERTA_DIAS dias ou menos para o fim do contrato."""
    return dias_restantes(data_contratacao, hoje) <= ALERTA_DIAS


def periodo_terminou(data_contratacao, hoje):
    """True a partir do dia seguinte ao último dia do contrato."""
    return dias_restantes(data_contratacao, hoje) < 0


def primeira_contratacao_acompanhada(hoje):
    """Contratações anteriores a esta data já passaram da experiência e não entram no Kanban."""
    return hoje - timedelta(days=DURACAO_DIAS - 1)
