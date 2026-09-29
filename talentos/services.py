"""Triagem e direcionamento automático de currículos por busca semântica (sem aprovação manual).

Assim que um currículo termina de ser processado (texto extraído, perfil interpretado e
`Candidato.embedding` gerado — ver `pipeline.processar_candidato`), `CurriculumAutomationService`
decide sozinho o destino do candidato, cruzando o embedding dele com o embedding de cada cargo
ativo:

- Cenário A (similaridade >= limiar): o candidato é direcionado automaticamente ao cargo mais
  compatível (`Candidato.cargo_id`), vai para a etapa "Triagem" e o cadastro fica/permanece
  ativo. O RH continua livre para trocar o cargo depois, normalmente, pela tela de detalhe.
- Cenário B (nenhum cargo atinge o limiar, ou não há cargo algum para comparar): o sistema
  NÃO força um direcionamento errado. O candidato vai para "Pendência" e o cadastro fica
  inativo, até um analista definir o cargo manualmente e reativá-lo.

Arquitetura multi-banco: `Candidato`/`Candidato.embedding` vivem no banco 'vector_db' (Docker,
com a extensão pgvector); `Cargo` vive no banco 'default'. O pgvector só compara dois vetores
dentro da MESMA conexão, então este serviço mantém `CargoEmbedding` — uma cópia leve (id do
cargo + o vetor do seu texto) dentro do 'vector_db' — para a busca semântica poder rodar
inteiramente lá. É o mesmo padrão de "IDs soltos" que o restante do app já usa para referenciar
o banco 'default' (ex.: `Candidato.cargo_id`).

Toda mudança de etapa/ativação passa pelas funções de `fluxo.py` (ponto único de mudança de
estado do app, com histórico e locking), para a automação ficar auditável do mesmo jeito que
uma ação manual do RH.
"""
import logging
from dataclasses import dataclass
from typing import Optional, Tuple

from django.conf import settings
from pgvector.django import CosineDistance

from .embeddings import gerar_embedding
from .erros import ErroProcessamento
from .fluxo import inativar_candidato, mover_candidato, reativar_candidato
from .historico import Evento, registrar
from .models import CargoEmbedding, Candidato

logger = logging.getLogger('talentos')


@dataclass(frozen=True)
class ResultadoTriagem:
    """Resultado de uma triagem automática, para quem chamou o serviço decidir o que exibir."""

    candidato: Candidato
    direcionado: bool  # True = Cenário A (cargo atribuído automaticamente)
    cargo_id: Optional[int]  # cargo escolhido (Cenário A) ou o mais próximo encontrado (B)
    similaridade: Optional[float]  # 0.0 a 1.0; None se não havia nenhum cargo elegível para comparar


class CurriculumAutomationService:
    """Direciona automaticamente um `Candidato` já processado, com base em busca semântica.

    Uso típico, logo após `pipeline.processar_candidato` ter gerado o embedding:

        candidato = processar_candidato(candidato, usuario)
        if candidato:
            CurriculumAutomationService().triar(candidato, usuario)
    """

    def __init__(self, limiar_similaridade: Optional[float] = None) -> None:
        # Similaridade de cosseno mínima (0.0 a 1.0) para o direcionamento automático.
        # Sem valor explícito, usa settings.CURRICULO_MATCH_THRESHOLD (configurável por env).
        self.limiar_similaridade: float = (
            limiar_similaridade if limiar_similaridade is not None else settings.CURRICULO_MATCH_THRESHOLD
        )

    # ------------------------------------------------------------------------------------
    # Sincronização dos embeddings de cargo (pré-requisito da busca semântica)
    # ------------------------------------------------------------------------------------
    @staticmethod
    def _texto_cargo(cargo) -> str:
        """Texto que representa o cargo na busca semântica.

        Importante: `Candidato.embedding` é gerado a partir de um documento rico (resumo,
        experiências, qualificações e até 12.000 caracteres do currículo bruto — ver
        `pipeline.documento_para_embedding`). Comparar isso com um rótulo curto tipo
        "Cargo: X. Seção: Y." (poucas palavras) tende a produzir uma similaridade de
        cosseno sistematicamente mais baixa do que a intuição sugere — não é falta de
        aderência real, é a assimetria entre um texto longo e uma etiqueta curta. Por isso
        o texto do cargo é escrito como uma frase mais parecida com uma descrição de vaga
        (mesmo estilo das consultas em linguagem natural de `busca.buscar`), repetindo o
        nome do cargo em contexto, em vez de só listar rótulos separados por ponto.
        """
        return (
            f'Vaga para o cargo de {cargo.nome}, da seção {cargo.secao.nome}, no departamento '
            f'{cargo.secao.departamento.nome}. Buscamos um profissional com experiência, '
            f'formação e qualificações compatíveis com a função de {cargo.nome}.'
        )

    def _obter_ou_gerar_embedding_cargo(self, cargo) -> CargoEmbedding:
        """Devolve o `CargoEmbedding` do cargo, gerando-o (e persistindo) na hora se faltar.

        Isso evita que a triagem falhe só porque um cargo foi cadastrado depois da última
        sincronização em lote. Para ATUALIZAR um cargo que já tem embedding (nome ou seção
        alterados), use `sincronizar_embeddings_cargos`.
        """
        existente = CargoEmbedding.objects.using('vector_db').filter(cargo_id=cargo.id).first()
        if existente is not None:
            return existente
        texto = self._texto_cargo(cargo)
        vetor = gerar_embedding(texto)  # pode levantar ErroProcessamento (serviço de IA fora do ar etc.)
        return CargoEmbedding.objects.using('vector_db').create(cargo_id=cargo.id, texto_origem=texto, embedding=vetor)

    def sincronizar_embeddings_cargos(self) -> int:
        """Recalcula o embedding de todos os cargos ativos. Retorna quantos foram processados.

        Chame ao renomear um cargo ou mudar sua seção/departamento (o texto vetorizado muda),
        ou periodicamente (ex.: management command / tarefa agendada), para cargos novos
        entrarem na busca semântica sem depender da geração "sob demanda".
        """
        from .pipeline import cargos_disponiveis  # import tardio: evita import circular (pipeline usa este serviço)

        total = 0
        for cargo in cargos_disponiveis():
            texto = self._texto_cargo(cargo)
            vetor = gerar_embedding(texto)
            CargoEmbedding.objects.using('vector_db').update_or_create(
                cargo_id=cargo.id, defaults={'texto_origem': texto, 'embedding': vetor},
            )
            total += 1
        return total

    # ------------------------------------------------------------------------------------
    # Busca semântica
    # ------------------------------------------------------------------------------------
    def ranking_similaridade(self, embedding) -> list:
        """[(cargo_id, similaridade), ...] de todo cargo ativo, do mais ao menos compatível
        com `embedding`. Lista vazia se não houver nenhum cargo ativo para comparar.

        Sincroniza (sob demanda) o embedding de qualquer cargo ativo que ainda não tenha um
        `CargoEmbedding`. Usado tanto para decidir o direcionamento (`_cargo_mais_compativel`)
        quanto para diagnóstico/calibração do limiar (comando `inspecionar_similaridade_cargos`).
        """
        from .pipeline import cargos_disponiveis  # import tardio: evita import circular (pipeline usa este serviço)

        cargos_ativos = list(cargos_disponiveis())
        if not cargos_ativos:
            return []

        # Garante que todo cargo ativo tenha um vetor antes de comparar (novos cargos incluídos).
        for cargo in cargos_ativos:
            self._obter_ou_gerar_embedding_cargo(cargo)

        ids_ativos = [cargo.id for cargo in cargos_ativos]
        candidatos_embedding = (
            CargoEmbedding.objects.using('vector_db')
            .filter(cargo_id__in=ids_ativos)
            .annotate(distancia=CosineDistance('embedding', embedding))
            .order_by('distancia')
        )
        # Distância de cosseno (0 = idêntico, 2 = oposto) -> similaridade (1 = idêntico, 0 = nada a ver).
        return [(linha.cargo_id, max(0.0, min(1.0, 1 - linha.distancia))) for linha in candidatos_embedding]

    def _cargo_mais_compativel(self, candidato: Candidato) -> Tuple[Optional[int], Optional[float]]:
        """(cargo_id, similaridade) do cargo cujo embedding está mais próximo do candidato.

        Considera só cargos ativos (árvore organizacional inteira ativa, via `cargos_disponiveis`).
        Devolve (None, None) se não houver nenhum cargo ativo para comparar.
        """
        ranking = self.ranking_similaridade(candidato.embedding)
        return ranking[0] if ranking else (None, None)

    # ------------------------------------------------------------------------------------
    # Regra de negócio: aplica o Cenário A ou B diretamente, sem etapa de aprovação
    # ------------------------------------------------------------------------------------
    def triar(self, candidato: Candidato, usuario=None) -> ResultadoTriagem:
        """Direciona `candidato` automaticamente, aplicando o resultado no banco.

        Idempotente: pode ser chamada de novo (ex.: reprocessamento de um currículo) e
        simplesmente reavalia a regra com o embedding atual do candidato.

        Pré-condição: `candidato.embedding` já foi gerado por `pipeline.processar_candidato`.
        Levanta `ErroProcessamento` se faltar o embedding do candidato, ou se a geração de um
        embedding de cargo novo falhar (ex.: serviço de IA indisponível).
        """
        if not candidato.pk:
            raise ErroProcessamento('O candidato precisa estar salvo antes da triagem automática.')
        if not candidato.embedding:
            raise ErroProcessamento('O candidato não possui embedding; processe o currículo antes de triar.')

        try:
            cargo_id, similaridade = self._cargo_mais_compativel(candidato)
        except ErroProcessamento:
            raise
        except Exception as erro:  # falha inesperada na busca não deve derrubar o pipeline chamador sem contexto
            logger.exception('Triagem automática: falha na busca semântica (candidato id=%s)', candidato.pk)
            raise ErroProcessamento('Falha ao buscar cargos compatíveis por similaridade semântica.') from erro

        direcionado = cargo_id is not None and similaridade >= self.limiar_similaridade

        # Log de diagnóstico centralizado: candidato, melhor cargo encontrado (por aproximação,
        # não precisa ser idêntico) e o score — para calibrar CURRICULO_MATCH_THRESHOLD com
        # casos reais sem precisar abrir o banco. Não loga o texto do currículo em si (nome,
        # e-mail e o conteúdo do PDF são dados pessoais); o id do candidato já basta para
        # quem tiver acesso ao sistema investigar o caso pontual.
        logger.info(
            'Triagem automática: candidato id=%s -> melhor cargo=%s similaridade=%s limiar=%.0f%% decisao=%s',
            candidato.pk, cargo_id if cargo_id is not None else '(nenhum cargo elegível)',
            f'{similaridade:.1%}' if similaridade is not None else 'n/d',
            self.limiar_similaridade * 100, 'DIRECIONADO' if direcionado else 'PENDENCIA',
        )

        if direcionado:
            candidato_final = self._direcionar_para_cargo(candidato, cargo_id, similaridade, usuario)
        else:
            candidato_final = self._enviar_para_pendencia(candidato, cargo_id, similaridade, usuario)

        return ResultadoTriagem(
            candidato=candidato_final, direcionado=direcionado, cargo_id=cargo_id, similaridade=similaridade,
        )

    # Cenário A: match encontrado acima do limiar (não precisa ser idêntico, só o mais próximo
    # dentre os cadastrados e acima do piso mínimo de confiança) -----------------------------
    def _direcionar_para_cargo(self, candidato: Candidato, cargo_id: int, similaridade: float, usuario) -> Candidato:
        candidato.cargo_sugerido_id = cargo_id
        candidato.cargo_id = cargo_id  # direcionamento automático; o RH ainda pode trocar depois
        nome_cargo = candidato.cargo_sugerido.nome if candidato.cargo_sugerido else f'#{cargo_id}'
        candidato.justificativa_cargo = (
            f'Direcionamento automático por busca semântica: "{nome_cargo}" foi o cargo mais próximo, '
            f'com {similaridade:.0%} de similaridade (limiar configurado: {self.limiar_similaridade:.0%}).'
        )
        candidato.save(using='vector_db', update_fields=['cargo_id', 'cargo_sugerido_id', 'justificativa_cargo'])

        if not candidato.ativo:
            candidato = reativar_candidato(candidato, usuario)
        mover_candidato(candidato, Candidato.Status.TRIAGEM, usuario)

        registrar(
            candidato, Evento.CARGO_DEFINIDO, usuario,
            f'Cargo "{nome_cargo}" atribuído automaticamente pela IA (similaridade de {similaridade:.0%}). '
            'O RH pode alterar o cargo a qualquer momento.',
        )
        logger.info(
            'Triagem automática: candidato id=%s direcionado ao cargo id=%s (%s), similaridade=%.4f.',
            candidato.pk, cargo_id, nome_cargo, similaridade,
        )
        return candidato

    # Cenário B: nenhum cargo chega perto o suficiente (ex.: currículo de Contador vs. cargos
    # todos de TI) — falha só quando o melhor score ainda assim fica abaixo do piso mínimo -----
    def _enviar_para_pendencia(
        self, candidato: Candidato, cargo_id: Optional[int], similaridade: Optional[float], usuario,
    ) -> Candidato:
        candidato.cargo_id = None  # nunca força um direcionamento em que a IA não confia
        candidato.cargo_sugerido_id = cargo_id  # referência para o RH, mesmo abaixo do limiar
        if cargo_id is not None:
            nome_cargo = candidato.cargo_sugerido.nome if candidato.cargo_sugerido else f'#{cargo_id}'
            motivo_log = f'melhor cargo foi "{nome_cargo}" (#{cargo_id}), com {similaridade:.0%} de similaridade'
            candidato.justificativa_cargo = (
                f'Nenhum cargo atingiu o piso mínimo de similaridade ({self.limiar_similaridade:.0%}); '
                f'o mais próximo foi "{nome_cargo}", com {similaridade:.0%}.'
            )
        else:
            motivo_log = 'não há nenhum cargo ativo cadastrado para comparar'
            candidato.justificativa_cargo = 'Nenhum cargo ativo com embedding disponível para comparação.'
        candidato.save(using='vector_db', update_fields=['cargo_id', 'cargo_sugerido_id', 'justificativa_cargo'])

        mover_candidato(candidato, Candidato.Status.PENDENCIA, usuario)
        if candidato.ativo:
            candidato = inativar_candidato(
                candidato,
                'Triagem automática: nenhum cargo compatível foi encontrado pela IA. '
                'Aguardando definição manual do cargo pelo RH.',
                usuario,
            )
        logger.info('Triagem automática: candidato id=%s enviado para Pendência — %s.', candidato.pk, motivo_log)
        return candidato
