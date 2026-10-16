import uuid
from datetime import timedelta

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone

from pesquisas.models import Colaborador

from . import regras
from .sanitizacao import limpar_html


class FormularioAvaliacao(models.Model):
    """Formulário de avaliação de experiência preenchido pelo gestor (cadastrado pelo RH).

    Mesmo formato do formulário de desligamento: cada pergunta tem seu tipo de resposta
    (texto, nota de 0 a 10, múltipla escolha ou caixa de seleção). O formulário `padrao`
    é o usado nas avaliações.
    """

    titulo = models.CharField('Título', max_length=200)
    descricao = models.TextField('Descrição', blank=True, help_text='Orientações exibidas ao gestor no início do formulário.')
    padrao = models.BooleanField(
        'Formulário padrão', default=False,
        help_text='Formulário enviado aos gestores nas novas avaliações. Só um pode ser o padrão.',
    )
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    criado_em = models.DateTimeField(default=timezone.now)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-padrao', 'titulo')
        verbose_name = 'formulário de avaliação'
        verbose_name_plural = 'formulários de avaliação'
        constraints = [
            models.UniqueConstraint(
                fields=('padrao',), condition=models.Q(padrao=True), name='formulario_avaliacao_unico_padrao',
            ),
        ]

    def __str__(self):
        return self.titulo

    def save(self, *args, **kwargs):
        # Sanitiza em todo salvamento (painel, admin, shell): a descrição é exibida com |safe.
        self.descricao = limpar_html(self.descricao)
        super().save(*args, **kwargs)


class PerguntaAvaliacao(models.Model):
    class TipoResposta(models.TextChoices):
        TEXTO = 'TEXTO', 'Texto'
        NOTA_0_10 = 'NOTA_0_10', 'Nota de 0 a 10'
        RADIO = 'RADIO', 'Múltipla escolha (escolha única)'
        CHECKBOX = 'CHECKBOX', 'Caixa de seleção (múltipla escolha)'

    OPCOES_TIPOS = (TipoResposta.RADIO, TipoResposta.CHECKBOX)

    formulario = models.ForeignKey(FormularioAvaliacao, on_delete=models.CASCADE, related_name='perguntas')
    texto_pergunta = models.TextField('Texto pergunta')
    tipo_resposta = models.CharField('Tipo resposta', max_length=10, choices=TipoResposta.choices)
    ordem = models.PositiveIntegerField('Ordem', validators=[MinValueValidator(1)])

    class Meta:
        ordering = ('ordem', 'pk')
        constraints = [models.UniqueConstraint(fields=('formulario', 'ordem'), name='pergunta_avaliacao_ordem_unica')]

    def __str__(self):
        return f'{self.ordem}. {self.texto_pergunta[:80]}'


class OpcaoPerguntaAvaliacao(models.Model):
    pergunta = models.ForeignKey(PerguntaAvaliacao, on_delete=models.CASCADE, related_name='opcoes')
    texto_opcao = models.CharField(max_length=200)
    ordem = models.PositiveIntegerField(validators=[MinValueValidator(1)])

    class Meta:
        ordering = ('ordem', 'pk')
        constraints = [models.UniqueConstraint(fields=('pergunta', 'ordem'), name='opcao_pergunta_avaliacao_ordem_unica')]

    def __str__(self):
        return self.texto_opcao


class PeriodoExperienciaQuerySet(models.QuerySet):
    def em_acompanhamento(self):
        """Cards ainda visíveis no Kanban.

        Saem do quadro: efetivados com benefícios incluídos, cards arquivados pelo RH e
        desligados há mais de DIAS_DESLIGADO_NO_QUADRO dias (calculado na consulta, sem
        depender da rotina periódica).
        """
        limite = timezone.now() - timedelta(days=regras.DIAS_DESLIGADO_NO_QUADRO)
        return self.filter(beneficios_finalizados=False, arquivado_em__isnull=True).exclude(
            status=PeriodoExperiencia.Status.DESLIGADO, status_alterado_em__lt=limite,
        )

    def abertos(self):
        """Cards nas colunas automáticas, ainda sem decisão de efetivação ou desligamento."""
        return self.filter(status__in=PeriodoExperiencia.STATUS_AUTOMATICOS)


class PeriodoExperiencia(models.Model):
    """Card do colaborador no Kanban de acompanhamento do período de experiência."""

    class Status(models.TextChoices):
        DENTRO_PRAZO = 'DENTRO_PRAZO', 'Dentro do prazo'
        VENCENDO = 'VENCENDO', 'Vencendo'
        EFETIVADO = 'EFETIVADO', 'Efetivado'
        DESLIGADO = 'DESLIGADO', 'Desligado'

    # Colunas controladas pelo sistema (a partir da data de contratação) e colunas de decisão do RH.
    STATUS_AUTOMATICOS = (Status.DENTRO_PRAZO, Status.VENCENDO)
    STATUS_MANUAIS = (Status.EFETIVADO, Status.DESLIGADO)

    colaborador = models.OneToOneField(Colaborador, on_delete=models.PROTECT, related_name='periodo_experiencia')
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DENTRO_PRAZO)
    status_alterado_em = models.DateTimeField(default=timezone.now)
    status_alterado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        help_text='Vazio quando a mudança foi feita automaticamente pelo sistema.',
    )
    efetivado_automaticamente = models.BooleanField(default=False)

    class StatusAvaliacao(models.TextChoices):
        PENDENTE = 'PENDENTE', 'Pendente'
        RESPONDIDO = 'RESPONDIDO', 'Respondida'

    # Link único do gestor para o formulário de avaliação (exige login como gestor responsável).
    token_avaliacao = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    status_avaliacao = models.CharField(
        'Status da avaliação', max_length=10, choices=StatusAvaliacao.choices, default=StatusAvaliacao.PENDENTE,
    )
    # Formulário usado nesta avaliação: fixado quando o gestor abre o link, para não mudar no meio do preenchimento.
    formulario = models.ForeignKey(
        FormularioAvaliacao, on_delete=models.SET_NULL, null=True, blank=True, related_name='periodos',
    )
    avaliacao_registrada_em = models.DateTimeField(null=True, blank=True)
    avaliacao_registrada_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )

    beneficios_finalizados = models.BooleanField('Inclusão de benefícios finalizada', default=False)
    beneficios_finalizados_em = models.DateTimeField(null=True, blank=True)
    beneficios_finalizados_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )

    arquivado_em = models.DateTimeField(null=True, blank=True)
    arquivado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )

    criado_em = models.DateTimeField(default=timezone.now)

    objects = PeriodoExperienciaQuerySet.as_manager()

    class Meta:
        ordering = ('colaborador__data_contratacao', 'colaborador__nome')
        verbose_name = 'período de experiência'
        verbose_name_plural = 'períodos de experiência'

    def __str__(self):
        return f'{self.colaborador} · {self.get_status_display()}'

    @property
    def avaliacao_realizada(self):
        return self.status_avaliacao == self.StatusAvaliacao.RESPONDIDO

    @property
    def caminho_avaliacao(self):
        return reverse('experiencia:avaliar', kwargs={'token': self.token_avaliacao})

    @property
    def link_avaliacao(self):
        base_url = settings.CSRF_TRUSTED_ORIGINS[0] if settings.CSRF_TRUSTED_ORIGINS else 'http://localhost:5012'
        return f'{base_url}{self.caminho_avaliacao}'

    @property
    def data_contratacao(self):
        return self.colaborador.data_contratacao

    @property
    def fim_experiencia(self):
        return regras.fim_experiencia(self.data_contratacao) if self.data_contratacao else None

    @property
    def dias_restantes(self):
        if not self.data_contratacao:
            return None
        return regras.dias_restantes(self.data_contratacao, timezone.localdate())

    @property
    def avaliacao_liberada_em(self):
        """Data em que o card entra em "Vencendo" e o gestor pode responder a avaliação."""
        return regras.inicio_alerta(self.data_contratacao) if self.data_contratacao else None

    @property
    def sai_do_quadro_em(self):
        """Data em que um card desligado deixa o Kanban automaticamente."""
        if self.status != self.Status.DESLIGADO:
            return None
        return timezone.localtime(self.status_alterado_em + timedelta(days=regras.DIAS_DESLIGADO_NO_QUADRO))

    @property
    def chave_avaliacao(self):
        return f'experiencia:{self.pk}:avaliacao'

    @property
    def chave_beneficios(self):
        return f'experiencia:{self.pk}:beneficios'

    @property
    def chave_desligamento(self):
        return f'experiencia:{self.pk}:desligamento'

    @property
    def chave_arquivamento(self):
        return f'experiencia:{self.pk}:arquivamento'


class RespostaAvaliacao(models.Model):
    """Avaliação enviada pelo gestor, com a decisão final sobre o colaborador."""

    class Decisao(models.TextChoices):
        EFETIVADO = 'EFETIVADO', 'Efetivado'
        DESLIGADO = 'DESLIGADO', 'Desligado'

    periodo = models.OneToOneField(PeriodoExperiencia, on_delete=models.CASCADE, related_name='resposta_avaliacao')
    formulario = models.ForeignKey(
        FormularioAvaliacao, on_delete=models.SET_NULL, null=True, blank=True, related_name='respostas',
    )
    # Cópias do formulário no momento da resposta: editar o formulário depois não altera o histórico.
    formulario_titulo = models.CharField(max_length=200)
    decisao = models.CharField('Decisão', max_length=10, choices=Decisao.choices)
    observacoes = models.TextField('Observações', blank=True)
    respondido_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    respondido_em = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ('-respondido_em',)
        verbose_name = 'resposta de avaliação'
        verbose_name_plural = 'respostas de avaliação'

    def __str__(self):
        return f'{self.periodo.colaborador} · {self.get_decisao_display()}'


class DetalheRespostaAvaliacao(models.Model):
    """Resposta a uma pergunta. Guarda cópia da pergunta, do tipo e das opções escolhidas:
    editar ou excluir perguntas do formulário depois não altera o histórico."""

    resposta = models.ForeignKey(RespostaAvaliacao, on_delete=models.CASCADE, related_name='detalhes')
    pergunta = models.ForeignKey(PerguntaAvaliacao, on_delete=models.SET_NULL, null=True, related_name='+')
    ordem = models.PositiveIntegerField()
    pergunta_texto = models.TextField()
    tipo_resposta = models.CharField(max_length=10, choices=PerguntaAvaliacao.TipoResposta.choices)
    valor_texto = models.TextField(blank=True, null=True)
    valor_inteiro = models.IntegerField(blank=True, null=True, validators=[MinValueValidator(0), MaxValueValidator(10)])
    opcoes_selecionadas = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ('ordem', 'pk')
