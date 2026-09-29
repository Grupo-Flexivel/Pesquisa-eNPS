import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import FileSystemStorage
from django.db import models
from django.utils import timezone

from .fields import EmbeddingField


def armazenamento_curriculos():
    """Storage privado (fora de MEDIA_ROOT): o nginx nunca serve estes arquivos."""
    return FileSystemStorage(location=settings.PRIVATE_MEDIA_ROOT)


def caminho_curriculo(instance, nome_original):
    # Nome aleatório: o arquivo original costuma conter o nome do candidato.
    return f'curriculos/{uuid.uuid4().hex}.pdf'


class CandidatoAtivoManager(models.Manager):
    """Manager padrão: só os candidatos ativos. Consultas com inativos usam `Candidato.all_objects`."""

    def get_queryset(self):
        return super().get_queryset().filter(ativo=True)


class Candidato(models.Model):
    class Status(models.TextChoices):
        PENDENCIA = 'PENDENCIA', 'Pendência'  # sem cargo definido; aguarda triagem manual do RH
        TRIAGEM = 'TRIAGEM', 'Triagem'
        ENTREVISTA_AGENDADA = 'ENTREVISTA_AGENDADA', 'Entrevista agendada'
        ENTREVISTA_REALIZADA = 'ENTREVISTA_REALIZADA', 'Entrevista realizada'
        TESTE_TECNICO = 'TESTE_TECNICO', 'Teste técnico'
        BANCO_DE_TALENTOS = 'BANCO_DE_TALENTOS', 'Banco de Talentos'
        CONTRATADO = 'CONTRATADO', 'Contratado'
        REPROVADO = 'REPROVADO', 'Reprovado'

    class Processamento(models.TextChoices):
        PENDENTE = 'PENDENTE', 'Pendente'
        CONCLUIDO = 'CONCLUIDO', 'Concluído'
        ERRO = 'ERRO', 'Erro'

    # Arquivo
    curriculo = models.FileField(upload_to=caminho_curriculo, storage=armazenamento_curriculos)
    nome_arquivo = models.CharField(max_length=255)
    hash_arquivo = models.CharField(max_length=64, unique=True, editable=False)  # SHA-256: evita duplicidade

    # Dados extraídos pela IA (editáveis pelo analista)
    nome = models.CharField(max_length=200, blank=True)
    email = models.EmailField(blank=True)
    telefone = models.CharField(max_length=40, blank=True)
    cidade = models.CharField(max_length=120, blank=True, db_index=True)
    bairro = models.CharField(max_length=120, blank=True)
    resumo = models.TextField(blank=True)
    experiencias = models.JSONField(default=list, blank=True)
    qualificacoes = models.JSONField(default=list, blank=True)
    texto_extraido = models.TextField(blank=True, editable=False)

    # Busca semântica (dimensão definida em settings.EMBEDDING_DIMENSIONS)
    embedding = EmbeddingField(dimensions=settings.EMBEDDING_DIMENSIONS, null=True, blank=True, editable=False)

    # Direcionamento (Integers para suportar multi-database)
    cargo_sugerido_id = models.IntegerField(null=True, blank=True, verbose_name='cargo sugerido pela IA')
    justificativa_cargo = models.TextField(blank=True)
    cargo_id = models.IntegerField(null=True, blank=True, verbose_name='cargo definido pelo analista')

    # Propriedades auxiliares para resgatar o objeto Cargo do banco 'default'
    @property
    def cargo(self):
        if not self.cargo_id:
            return None
        from organizacao.models import Cargo
        return Cargo.objects.using('default').filter(pk=self.cargo_id).first()

    @property
    def cargo_sugerido(self):
        if not self.cargo_sugerido_id:
            return None
        from organizacao.models import Cargo
        return Cargo.objects.using('default').filter(pk=self.cargo_sugerido_id).first()

    # Fluxo
    status = models.CharField(max_length=25, choices=Status.choices, default=Status.TRIAGEM, db_index=True)
    processamento = models.CharField(max_length=10, choices=Processamento.choices, default=Processamento.PENDENTE)
    erro_processamento = models.CharField(max_length=300, blank=True)
    
    # Usuário que enviou (convertido para IntegerField para banco cruzado)
    enviado_por_id = models.IntegerField(null=True, blank=True)

    @property
    def enviado_por(self):
        if not self.enviado_por_id:
            return None
        from django.contrib.auth import get_user_model
        User = get_user_model()
        return User.objects.using('default').filter(pk=self.enviado_por_id).first()

    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    # Inativação (com auditoria imutável fora de Candidato.inativar()/reativar())
    ativo = models.BooleanField(default=True, db_index=True)
    motivo_inativacao = models.TextField(blank=True)
    inativado_por_id = models.IntegerField(null=True, blank=True)
    inativado_em = models.DateTimeField(null=True, blank=True)

    CAMPOS_AUDITORIA_INATIVACAO = ('ativo', 'motivo_inativacao', 'inativado_por_id', 'inativado_em')

    objects = CandidatoAtivoManager()  # default: só ativos
    all_objects = models.Manager()  # ativos + inativos (usar para telas de detalhe/reativação)

    @property
    def inativado_por(self):
        if not self.inativado_por_id:
            return None
        from django.contrib.auth import get_user_model
        User = get_user_model()
        return User.objects.using('default').filter(pk=self.inativado_por_id).first()

    class Meta:
        ordering = ('-criado_em',)
        verbose_name = 'candidato'
        verbose_name_plural = 'candidatos'
        base_manager_name = 'all_objects'  # relações de FK (histórico etc.) não podem ficar "cegas" a inativos

    @property
    def nome_exibicao(self):
        return self.nome or f'Currículo sem nome ({self.nome_arquivo})'

    def __str__(self):
        return self.nome_exibicao

    @classmethod
    def from_db(cls, db, field_names, values):
        instancia = super().from_db(db, field_names, values)
        # Retrato dos campos de auditoria como vieram do banco, para o save() detectar
        # alterações feitas fora de inativar()/reativar() (ex.: edição direta via admin/form).
        instancia._auditoria_original = {
            campo: getattr(instancia, campo) for campo in cls.CAMPOS_AUDITORIA_INATIVACAO if campo in field_names
        }
        return instancia

    def save(self, *args, **kwargs):
        original = getattr(self, '_auditoria_original', None)
        if original is not None and not getattr(self, '_auditoria_autorizada', False):
            if any(getattr(self, campo) != valor for campo, valor in original.items()):
                raise ValidationError(
                    'Os campos de auditoria de inativação só podem ser alterados por '
                    'Candidato.inativar() ou Candidato.reativar().'
                )
        super().save(*args, **kwargs)
        self._auditoria_autorizada = False
        self._auditoria_original = {campo: getattr(self, campo) for campo in self.CAMPOS_AUDITORIA_INATIVACAO}

    def inativar(self, motivo, usuario=None, using=None):
        """Inativa o candidato. Levanta ValidationError se `motivo` não for informado."""
        motivo = (motivo or '').strip()
        if not motivo:
            raise ValidationError({'motivo_inativacao': 'Informe o motivo da inativação.'})
        self.ativo = False
        self.motivo_inativacao = motivo
        self.inativado_por_id = usuario.pk if usuario is not None and getattr(usuario, 'is_authenticated', True) else None
        self.inativado_em = timezone.now()
        self._auditoria_autorizada = True
        self.save(using=using, update_fields=[
            'ativo', 'motivo_inativacao', 'inativado_por_id', 'inativado_em', 'atualizado_em',
        ])

    def reativar(self, usuario=None, using=None):
        """Reverte a inativação, limpando os dados de auditoria."""
        self.ativo = True
        self.motivo_inativacao = ''
        self.inativado_por_id = None
        self.inativado_em = None
        self._auditoria_autorizada = True
        self.save(using=using, update_fields=[
            'ativo', 'motivo_inativacao', 'inativado_por_id', 'inativado_em', 'atualizado_em',
        ])


class CargoEmbedding(models.Model):
    """Espelho, dentro do 'vector_db', do vetor semântico de um Cargo do banco 'default'.

    O pgvector precisa comparar dois vetores na MESMA conexão/banco. Como `Cargo` vive no
    banco 'default' (sem pgvector) e `Candidato.embedding` vive no 'vector_db', mantemos aqui
    uma cópia leve (id do cargo + embedding do seu texto) para a busca semântica poder rodar
    inteiramente dentro do 'vector_db' — o mesmo padrão de IDs "soltos" já usado em
    `Candidato.cargo_id` para referenciar o outro banco.
    """

    cargo_id = models.IntegerField(unique=True, db_index=True, verbose_name='cargo (id no banco default)')
    texto_origem = models.TextField(blank=True, editable=False)  # texto que gerou o vetor, útil para depuração
    embedding = EmbeddingField(dimensions=settings.EMBEDDING_DIMENSIONS, editable=False)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'embedding de cargo'
        verbose_name_plural = 'embeddings de cargos'

    def __str__(self):
        return f'Embedding do cargo #{self.cargo_id}'


class HistoricoCandidatura(models.Model):
    """Linha do tempo unificada do candidato: quem fez o quê e quando.

    Registros são imutáveis (só inserção). Sobrevivem à remoção do usuário (`usuario_nome`
    guarda um retrato do nome) e são apagados apenas junto com o candidato (LGPD).
    """

    class Evento(models.TextChoices):
        CURRICULO_ENVIADO = 'CURRICULO_ENVIADO', 'Currículo enviado'
        PROCESSAMENTO_CONCLUIDO = 'PROCESSAMENTO_CONCLUIDO', 'Currículo processado'
        PROCESSAMENTO_ERRO = 'PROCESSAMENTO_ERRO', 'Falha no processamento'
        REPROCESSAMENTO = 'REPROCESSAMENTO', 'Reprocessamento solicitado'
        ETAPA_ALTERADA = 'ETAPA_ALTERADA', 'Etapa alterada'
        CARGO_DEFINIDO = 'CARGO_DEFINIDO', 'Cargo definido'
        DADOS_EDITADOS = 'DADOS_EDITADOS', 'Dados editados'
        CURRICULO_ATUALIZADO = 'CURRICULO_ATUALIZADO', 'Currículo atualizado (cadastro já existente)'
        REPROVACAO_REGISTRADA = 'REPROVACAO_REGISTRADA', 'Reprovação registrada'
        CANDIDATO_INATIVADO = 'CANDIDATO_INATIVADO', 'Candidato inativado'
        CANDIDATO_REATIVADO = 'CANDIDATO_REATIVADO', 'Candidato reativado'

    ICONES = {
        'CURRICULO_ENVIADO': 'bi-cloud-upload',
        'PROCESSAMENTO_CONCLUIDO': 'bi-check-circle',
        'PROCESSAMENTO_ERRO': 'bi-exclamation-triangle',
        'REPROCESSAMENTO': 'bi-arrow-repeat',
        'ETAPA_ALTERADA': 'bi-arrow-right-circle',
        'CARGO_DEFINIDO': 'bi-person-badge',
        'DADOS_EDITADOS': 'bi-pencil',
        'CURRICULO_ATUALIZADO': 'bi-arrow-down-up',
        'REPROVACAO_REGISTRADA': 'bi-x-octagon',
        'CANDIDATO_INATIVADO': 'bi-slash-circle',
        'CANDIDATO_REATIVADO': 'bi-arrow-counterclockwise',
    }

    candidato = models.ForeignKey(Candidato, on_delete=models.CASCADE, related_name='historico')
    evento = models.CharField(max_length=30, choices=Evento.choices)
    descricao = models.CharField(max_length=500, blank=True)
    status_anterior = models.CharField(max_length=25, blank=True, choices=Candidato.Status.choices)
    status_novo = models.CharField(max_length=25, blank=True, choices=Candidato.Status.choices)
    
    # Usuário responsável no histórico (convertido para IntegerField)
    usuario_id = models.IntegerField(null=True, blank=True, verbose_name='analista id')
    
    @property
    def usuario(self):
        if not self.usuario_id:
            return None
        from django.contrib.auth import get_user_model
        User = get_user_model()
        return User.objects.using('default').filter(pk=self.usuario_id).first()

    usuario_nome = models.CharField(max_length=150, blank=True)
    data_hora = models.DateTimeField(default=timezone.now, editable=False, db_index=True)

    class Meta:
        ordering = ('-data_hora', '-pk')
        verbose_name = 'histórico da candidatura'
        verbose_name_plural = 'históricos das candidaturas'

    @property
    def icone(self):
        return self.ICONES.get(self.evento, 'bi-circle')

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise PermissionError('O histórico é imutável: registros não podem ser alterados.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError('O histórico é imutável: registros só são removidos com o candidato.')

    def __str__(self):
        return f'{self.get_evento_display()} · {self.data_hora:%d/%m/%Y %H:%M}'