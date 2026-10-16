import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone

from pesquisas.models import Colaborador


class FormularioDesligamento(models.Model):
    class Iniciativa(models.TextChoices):
        EMPRESA = 'EMPRESA', 'Iniciativa da empresa'
        COLABORADOR = 'COLABORADOR', 'Iniciativa do colaborador'

    nome = models.CharField(max_length=200)
    iniciativa = models.CharField(max_length=12, choices=Iniciativa.choices)
    link_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    criado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='formularios_desligamento_criados',
    )
    data_criacao = models.DateTimeField(default=timezone.now)

    @property
    def link_completo(self):
        caminho = reverse('desligamentos:responder', kwargs={'link_uuid': self.link_uuid})
        base_url = settings.CSRF_TRUSTED_ORIGINS[0] if settings.CSRF_TRUSTED_ORIGINS else 'http://localhost:5012'
        return f"{base_url}{caminho}"

    class Meta:
        ordering = ('-data_criacao',)
        verbose_name = 'formulário de desligamento'
        verbose_name_plural = 'formulários de desligamento'

    def __str__(self):
        return self.nome


class PerguntaDesligamento(models.Model):
    class TipoResposta(models.TextChoices):
        TEXTO = 'TEXTO', 'Texto'
        NOTA_0_10 = 'NOTA_0_10', 'Nota de 0 a 10'
        RADIO = 'RADIO', 'Múltipla escolha (escolha única)'
        CHECKBOX = 'CHECKBOX', 'Caixa de seleção (múltipla escolha)'

    OPCOES_TIPOS = (TipoResposta.RADIO, TipoResposta.CHECKBOX)

    formulario = models.ForeignKey(FormularioDesligamento, on_delete=models.CASCADE, related_name='perguntas')
    texto_pergunta = models.TextField()
    tipo_resposta = models.CharField(max_length=10, choices=TipoResposta.choices)
    ordem = models.PositiveIntegerField(validators=[MinValueValidator(1)])

    class Meta:
        ordering = ('ordem', 'pk')
        constraints = [models.UniqueConstraint(fields=('formulario', 'ordem'), name='pergunta_desligamento_ordem_unica')]

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.ordem}. {self.texto_pergunta[:80]}'


class OpcaoPergunta(models.Model):
    pergunta = models.ForeignKey(PerguntaDesligamento, on_delete=models.CASCADE, related_name='opcoes')
    texto_opcao = models.CharField(max_length=200)
    ordem = models.PositiveIntegerField(validators=[MinValueValidator(1)])

    class Meta:
        ordering = ('ordem', 'pk')
        constraints = [models.UniqueConstraint(fields=('pergunta', 'ordem'), name='opcao_pergunta_ordem_unica')]

    def __str__(self):
        return self.texto_opcao


class Desligamento(models.Model):
    """Representa o card de desligamento de um colaborador no Kanban."""

    class Iniciativa(models.TextChoices):
        EMPRESA = 'EMPRESA', 'Iniciativa da empresa'
        COLABORADOR = 'COLABORADOR', 'Iniciativa do colaborador'

    class Status(models.TextChoices):
        # Agendado: data de demissão futura. Fica fora do Kanban até a data, quando vira Pendente.
        AGENDADO = 'AGENDADO', 'Agendado'
        PENDENTE = 'PENDENTE', 'Pendente'
        AGUARDANDO_RESPOSTA = 'AGUARDANDO_RESPOSTA', 'Aguardando resposta'
        RESPONDIDO = 'RESPONDIDO', 'Respondido'

    colaborador = models.ForeignKey(Colaborador, on_delete=models.PROTECT, related_name='desligamentos')
    formulario = models.ForeignKey(FormularioDesligamento, on_delete=models.CASCADE, related_name='desligamentos')
    iniciativa = models.CharField(max_length=12, choices=Iniciativa.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDENTE)
    criado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='desligamentos_criados',
    )
    data_criacao = models.DateTimeField(default=timezone.now)
    data_envio = models.DateTimeField(null=True, blank=True)
    data_resposta = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-data_criacao',)
        constraints = [
            models.UniqueConstraint(
                fields=('colaborador',),
                condition=models.Q(status__in=('AGENDADO', 'PENDENTE', 'AGUARDANDO_RESPOSTA')),
                name='desligamento_unico_em_aberto_por_colaborador',
            ),
        ]

    def clean(self):
        super().clean()
        if self.formulario_id and self.iniciativa and self.formulario.iniciativa != self.iniciativa:
            raise ValidationError('O formulário selecionado não corresponde à iniciativa informada.')

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'Desligamento de {self.colaborador.nome} ({self.get_status_display()})'


class RespostaDesligamento(models.Model):
    desligamento = models.OneToOneField(Desligamento, on_delete=models.CASCADE, related_name='resposta')
    data_resposta = models.DateTimeField(default=timezone.now, editable=False)
    hash_documento_respondente = models.CharField(max_length=64, editable=False)

    def __str__(self):
        return f'Resposta de desligamento #{self.pk}'


class DetalheRespostaDesligamento(models.Model):
    resposta = models.ForeignKey(RespostaDesligamento, on_delete=models.CASCADE, related_name='detalhes')
    pergunta = models.ForeignKey(PerguntaDesligamento, on_delete=models.CASCADE, related_name='detalhes')
    valor_texto = models.TextField(blank=True, null=True)
    valor_inteiro = models.IntegerField(blank=True, null=True, validators=[MinValueValidator(0), MaxValueValidator(10)])
    opcoes = models.ManyToManyField(OpcaoPergunta, blank=True, related_name='detalhes')

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('resposta', 'pergunta'), name='uma_resposta_desligamento_por_pergunta'),
        ]

    def clean(self):
        super().clean()
        if self.resposta_id and self.pergunta_id and self.resposta.desligamento.formulario_id != self.pergunta.formulario_id:
            raise ValidationError('A pergunta não pertence ao formulário respondido.')

    def __str__(self):
        return f'Detalhe #{self.pk}'
