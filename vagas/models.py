import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone

from pesquisas.models import Colaborador


class Vaga(models.Model):
    """Vaga de recrutamento interno, com formulário de candidatura dinâmico."""

    class Status(models.TextChoices):
        AGENDADA = 'AGENDADA', 'Agendada'
        EM_ANDAMENTO = 'EM_ANDAMENTO', 'Em andamento'
        ENCERRADA = 'ENCERRADA', 'Encerrada'

    titulo = models.CharField(max_length=200)
    descricao = models.TextField()
    data_abertura = models.DateTimeField()
    data_encerramento = models.DateTimeField()
    link_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    criado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='vagas_criadas',
    )
    data_criacao = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ('-data_criacao',)
        verbose_name = 'vaga'
        verbose_name_plural = 'vagas'

    def clean(self):
        super().clean()
        if self.data_abertura and self.data_encerramento and self.data_encerramento <= self.data_abertura:
            raise ValidationError('A data de encerramento deve ser posterior à data de abertura.')

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    @property
    def status(self):
        """Status calculado dinamicamente a partir da data/hora atual (não é persistido)."""
        agora = timezone.now()
        if agora < self.data_abertura:
            return self.Status.AGENDADA
        if agora > self.data_encerramento:
            return self.Status.ENCERRADA
        return self.Status.EM_ANDAMENTO

    @property
    def esta_aberta(self):
        return self.status == self.Status.EM_ANDAMENTO

    @property
    def link_completo(self):
        caminho = reverse('vagas:candidatar', kwargs={'vaga_uuid': self.link_uuid})
        base_url = settings.CSRF_TRUSTED_ORIGINS[0] if settings.CSRF_TRUSTED_ORIGINS else 'http://localhost:5012'
        return f"{base_url}{caminho}"

    def __str__(self):
        return self.titulo


class PerguntaVaga(models.Model):
    class TipoResposta(models.TextChoices):
        TEXTO = 'TEXTO', 'Texto'
        NOTA_0_10 = 'NOTA_0_10', 'Nota de 0 a 10'
        RADIO = 'RADIO', 'Múltipla escolha (escolha única)'
        CHECKBOX = 'CHECKBOX', 'Caixa de seleção (múltipla escolha)'

    OPCOES_TIPOS = (TipoResposta.RADIO, TipoResposta.CHECKBOX)

    vaga = models.ForeignKey(Vaga, on_delete=models.CASCADE, related_name='perguntas')
    texto_pergunta = models.TextField()
    tipo_resposta = models.CharField(max_length=10, choices=TipoResposta.choices)
    ordem = models.PositiveIntegerField(validators=[MinValueValidator(1)])

    class Meta:
        ordering = ('ordem', 'pk')
        constraints = [models.UniqueConstraint(fields=('vaga', 'ordem'), name='pergunta_vaga_ordem_unica')]

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.ordem}. {self.texto_pergunta[:80]}'


class OpcaoPerguntaVaga(models.Model):
    pergunta = models.ForeignKey(PerguntaVaga, on_delete=models.CASCADE, related_name='opcoes')
    texto_opcao = models.CharField(max_length=200)
    ordem = models.PositiveIntegerField(validators=[MinValueValidator(1)])

    class Meta:
        ordering = ('ordem', 'pk')
        constraints = [models.UniqueConstraint(fields=('pergunta', 'ordem'), name='opcao_pergunta_vaga_ordem_unica')]

    def __str__(self):
        return self.texto_opcao


class Candidatura(models.Model):
    """Candidatura de um colaborador ativo a uma vaga interna."""

    vaga = models.ForeignKey(Vaga, on_delete=models.CASCADE, related_name='candidaturas')
    colaborador = models.ForeignKey(Colaborador, on_delete=models.PROTECT, related_name='candidaturas')
    data_candidatura = models.DateTimeField(default=timezone.now, editable=False)
    hash_documento_candidato = models.CharField(max_length=64, editable=False)

    class Meta:
        ordering = ('-data_candidatura',)
        verbose_name = 'candidatura'
        verbose_name_plural = 'candidaturas'
        constraints = [
            models.UniqueConstraint(fields=('vaga', 'colaborador'), name='candidatura_unica_por_vaga_colaborador'),
        ]

    def __str__(self):
        return f'Candidatura de {self.colaborador.nome} à vaga {self.vaga.titulo}'


class DetalheRespostaCandidatura(models.Model):
    candidatura = models.ForeignKey(Candidatura, on_delete=models.CASCADE, related_name='detalhes')
    pergunta = models.ForeignKey(PerguntaVaga, on_delete=models.CASCADE, related_name='detalhes')
    valor_texto = models.TextField(blank=True, null=True)
    valor_inteiro = models.IntegerField(blank=True, null=True, validators=[MinValueValidator(0), MaxValueValidator(10)])
    opcoes = models.ManyToManyField(OpcaoPerguntaVaga, blank=True, related_name='detalhes')

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('candidatura', 'pergunta'), name='uma_resposta_candidatura_por_pergunta'),
        ]

    def clean(self):
        super().clean()
        if self.candidatura_id and self.pergunta_id and self.candidatura.vaga_id != self.pergunta.vaga_id:
            raise ValidationError('A pergunta não pertence à vaga respondida.')

    def __str__(self):
        return f'Detalhe #{self.pk}'
