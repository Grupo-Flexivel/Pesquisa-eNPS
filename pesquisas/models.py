import re
import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser, UserManager as DjangoUserManager
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models.functions import Lower
from django.urls import reverse
from django.utils import timezone


def somente_digitos(valor):
    return re.sub(r'\D', '', valor or '')


def validar_cpf(cpf):
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        raise ValidationError('Informe um CPF válido com 11 dígitos.')
    for tamanho in (9, 10):
        soma = sum(int(cpf[i]) * (tamanho + 1 - i) for i in range(tamanho))
        digito = (soma * 10 % 11) % 10
        if digito != int(cpf[tamanho]):
            raise ValidationError('Informe um CPF válido.')


def validar_cnpj(cnpj):
    if len(cnpj) != 14 or cnpj == cnpj[0] * 14:
        raise ValidationError('Informe um CNPJ válido com 14 dígitos.')
    
    # Validação do primeiro dígito verificador
    pesos_1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    soma = sum(int(cnpj[i]) * pesos_1[i] for i in range(12))
    digito = soma % 11
    digito = 0 if digito < 2 else 11 - digito
    if digito != int(cnpj[12]):
        raise ValidationError('Informe um CNPJ válido.')

    # Validação do segundo dígito verificador
    pesos_2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    soma = sum(int(cnpj[i]) * pesos_2[i] for i in range(13))
    digito = soma % 11
    digito = 0 if digito < 2 else 11 - digito
    if digito != int(cnpj[13]):
        raise ValidationError('Informe um CNPJ válido.')


def validar_documento(valor):
    documento = somente_digitos(valor)
    if len(documento) == 11:
        validar_cpf(documento)
    elif len(documento) == 14:
        validar_cnpj(documento)
    else:
        raise ValidationError('Informe um CPF (11 dígitos) ou CNPJ (14 dígitos) válido.')


def formatar_celular(valor):
    """Normaliza o celular para o padrão (DDD) 9XXXX-XXXX; valores fora do padrão voltam como vieram."""
    digitos = somente_digitos(valor)
    if len(digitos) == 13 and digitos.startswith('55'):
        digitos = digitos[2:]
    if len(digitos) != 11:
        return (valor or '').strip()
    return f'({digitos[:2]}) {digitos[2:7]}-{digitos[7:]}'


def validar_celular(valor):
    if not re.fullmatch(r'\([1-9]\d\) 9\d{4}-\d{4}', formatar_celular(valor)):
        raise ValidationError('Informe um celular válido no formato (DDD) 9XXXX-XXXX.')


def normalizar_celulares(valores):
    """Formata cada número, descarta os vazios e remove repetidos, mantendo a ordem."""
    if isinstance(valores, str):
        valores = [valores]
    celulares = []
    for valor in valores or []:
        celular = formatar_celular(str(valor))
        if celular and celular not in celulares:
            celulares.append(celular)
    return celulares


def validar_celulares(valores):
    if not isinstance(valores, list):
        raise ValidationError('Informe a lista de celulares.')
    invalidos = [valor for valor in valores if not re.fullmatch(r'\([1-9]\d\) 9\d{4}-\d{4}', formatar_celular(str(valor)))]
    if invalidos:
        raise ValidationError(
            f'Celular inválido: {", ".join(map(str, invalidos))}. Use o formato (DDD) 9XXXX-XXXX.'
        )


class PerfilAcesso(models.Model):
    """Perfis de acesso personalizados, cadastrados pelo administrador (ex.: "Gestor de RH")."""
    nome = models.CharField(max_length=100, unique=True)
    modulos_permitidos = models.JSONField(
        'módulos permitidos', default=list, blank=True,
        help_text='Chaves de módulo liberadas para este perfil (ver pesquisas/permissoes.py).',
    )
    ativo = models.BooleanField(default=True)

    class Meta:
        verbose_name = 'perfil de acesso'
        verbose_name_plural = 'perfis de acesso'
        ordering = ('nome',)
        constraints = [
            models.UniqueConstraint(Lower('nome'), name='perfilacesso_nome_unico_case_insensitive'),
        ]

    def __str__(self):
        return self.nome


class UserManager(DjangoUserManager):
    def create_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault('is_admin_total', True)
        return super().create_superuser(username, email, password, **extra_fields)


class User(AbstractUser):
    """Usuário administrativo; colaboradores não precisam de login."""

    class AuthSource(models.TextChoices):
        LOCAL = 'LOCAL', 'Senha local'
        DIRECTORY = 'DIRECTORY', 'Active Directory'

    objects = UserManager()

    is_admin_total = models.BooleanField(
        'administrador total', 
        default=False, 
        help_text='Indica se o usuário possui acesso total irrestrito ao sistema.'
    )
    perfil_personalizado = models.ForeignKey(
        PerfilAcesso,
        # PROTECT: um perfil em uso não pode ser excluído por engano (ver perfil_excluir), só inativado.
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        verbose_name='perfil de acesso personalizado',
        related_name='usuarios',
        help_text='Perfil customizado que define os módulos permitidos para este usuário.'
    )

    auth_source = models.CharField(
        max_length=10,
        choices=AuthSource.choices,
        default=AuthSource.LOCAL,
    )
    directory_guid = models.UUIDField(
        null=True,
        blank=True,
        unique=True,
        editable=False,
    )
    failed_login_attempts = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        self.username = (self.username or '').strip().lower()
        
        # O administrador total tem permissões máximas
        if self.is_admin_total:
            self.is_superuser = True
            self.is_staff = True
            self.perfil_personalizado = None
        else:
            self.is_superuser = False
            # É staff só se o perfil vinculado de fato libera algum módulo; um perfil vazio
            # (ainda sem módulos marcados) não deve abrir a porta do painel para ninguém.
            self.is_staff = bool(self.perfil_personalizado_id and self.perfil_personalizado.modulos_permitidos)

        super().save(*args, **kwargs)

    class Meta:
        verbose_name = 'usuário'
        verbose_name_plural = 'usuários'


class Empresa(models.Model):
    nome = models.CharField(max_length=200, unique=True)

    class Meta:
        ordering = ('nome',)
        constraints = [
            models.UniqueConstraint(Lower('nome'), name='empresa_nome_unico_case_insensitive'),
        ]

    def __str__(self):
        return self.nome


class ColaboradorManager(models.Manager):
    def create(self, empresas=(), **kwargs):
        """Aceita `empresas=[...]`, já que relações muitos-para-muitos só podem ser gravadas após o save."""
        with transaction.atomic(using=self.db):
            colaborador = super().create(**kwargs)
            if empresas:
                colaborador.empresas.set(empresas)
        return colaborador


class Colaborador(models.Model):
    nome = models.CharField(max_length=200)
    documento = models.CharField(max_length=14, unique=True, validators=[validar_documento])
    # Um mesmo CPF pode trabalhar em mais de uma empresa/unidade: continua sendo um único
    # cadastro (uma resposta por pesquisa eNPS), mas aparece em cada uma das empresas.
    empresas = models.ManyToManyField(Empresa, related_name='colaboradores', verbose_name='Empresas')
    ativo = models.BooleanField(default=True)

    usuario = models.OneToOneField(
        settings.AUTH_USER_MODEL, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True, 
        related_name='colaborador'
    )
    eh_gestor = models.BooleanField(
        'É gestor', default=False,
        help_text='Marque para que este colaborador possa ser escolhido como gestor responsável de outros.',
    )
    # Colaborador (marcado como gestor) que avalia este colaborador, ex.: no período de experiência.
    # Os avisos do sininho chegam pelo login (`usuario`) vinculado ao gestor.
    gestor = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='liderados',
        verbose_name='Gestor responsável',
    )
    
    data_criacao = models.DateTimeField(default=timezone.now)
    data_inativacao = models.DateTimeField(null=True, blank=True)
    data_ativacao = models.DateTimeField(null=True, blank=True)

    # Opcionais: só nome, documento e empresas são obrigatórios no cadastro.
    data_nascimento = models.DateField('Data de nascimento', null=True, blank=True)
    data_contratacao = models.DateField('Data de contratação', null=True, blank=True)
    data_demissao = models.DateField('Data de demissão', null=True, blank=True)

    # Lista de celulares, cada um guardado já formatado: (DDD) 9XXXX-XXXX.
    celulares = models.JSONField('Celulares', default=list, blank=True, validators=[validar_celulares])
    email = models.EmailField('E-mail', blank=True, default='')

    objects = ColaboradorManager()

    class Meta:
        ordering = ('nome',)

    @property
    def nomes_empresas(self):
        # Ordena em Python para aproveitar o prefetch_related('empresas') das listagens.
        return ', '.join(sorted(e.nome for e in self.empresas.all()))

    def clean(self):
        super().clean()
        self.documento = somente_digitos(self.documento)
        validar_documento(self.documento)
        self.celulares = normalizar_celulares(self.celulares)
        self.email = (self.email or '').strip().lower()
        if self.data_nascimento and self.data_nascimento > timezone.localdate():
            raise ValidationError({'data_nascimento': 'A data de nascimento não pode ser futura.'})
        if self.data_contratacao and self.data_demissao and self.data_demissao < self.data_contratacao:
            raise ValidationError({'data_demissao': 'A data de demissão não pode ser anterior à data de contratação.'})

    def save(self, *args, **kwargs):
        self.documento = somente_digitos(self.documento)
        self.celulares = normalizar_celulares(self.celulares)
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.nome


class Pesquisa(models.Model):
    titulo = models.CharField(max_length=200)
    data_inicio = models.DateTimeField()
    data_final = models.DateTimeField()
    link_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    criado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='pesquisas_criadas')

    @property
    def link_completo(self):
        caminho = reverse('pesquisas:responder', kwargs={'link_uuid': self.link_uuid})
        base_url = settings.CSRF_TRUSTED_ORIGINS[0] if settings.CSRF_TRUSTED_ORIGINS else 'http://localhost:5012'
        return f"{base_url}{caminho}"

    class Meta:
        ordering = ('-data_inicio',)

    @property
    def edicao_bloqueada(self):
        return timezone.now() >= self.data_inicio

    @property
    def vigente(self):
        agora = timezone.now()
        return self.data_inicio <= agora <= self.data_final

    def clean(self):
        super().clean()
        if self.data_inicio and self.data_final and self.data_final <= self.data_inicio:
            raise ValidationError({'data_final': 'A data final deve ser posterior à data inicial.'})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        return super().delete(*args, **kwargs)
    
    def __str__(self):
        return self.titulo


class Pergunta(models.Model):
    class TipoResposta(models.TextChoices):
        TEXTO = 'TEXTO', 'Texto'
        NOTA_0_10 = 'NOTA_0_10', 'Nota de 0 a 10'

    pesquisa = models.ForeignKey(Pesquisa, on_delete=models.CASCADE, related_name='perguntas')
    texto_pergunta = models.TextField()
    tipo_resposta = models.CharField(max_length=12, choices=TipoResposta.choices)
    ordem = models.PositiveIntegerField(validators=[MinValueValidator(1)])

    class Meta:
        ordering = ('ordem', 'pk')
        constraints = [models.UniqueConstraint(fields=('pesquisa', 'ordem'), name='pergunta_ordem_unica')]

    def clean(self):
        super().clean()
        if self.pesquisa_id and self.pesquisa.edicao_bloqueada:
            raise ValidationError('Perguntas de uma pesquisa iniciada não podem ser alteradas.')

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f'{self.ordem}. {self.texto_pergunta[:80]}'


class RespostaPesquisa(models.Model):
    pesquisa = models.ForeignKey(Pesquisa, on_delete=models.CASCADE, related_name='respostas')
    data_resposta = models.DateTimeField(default=timezone.now, editable=False)
    hash_documento_respondente = models.CharField(max_length=64, editable=False)

    class Meta:
        ordering = ('-data_resposta',)
        constraints = [models.UniqueConstraint(fields=('pesquisa', 'hash_documento_respondente'), name='resposta_unica_por_documento_e_pesquisa')]

    def __str__(self):
        return f'Resposta anônima #{self.pk} - {self.pesquisa}'


class DetalheResposta(models.Model):
    resposta_pesquisa = models.ForeignKey(RespostaPesquisa, on_delete=models.CASCADE, related_name='detalhes')
    pergunta = models.ForeignKey(Pergunta, on_delete=models.CASCADE, related_name='detalhes') 
    valor_texto = models.TextField(blank=True, null=True)
    valor_inteiro = models.IntegerField(blank=True, null=True, validators=[MinValueValidator(0), MaxValueValidator(10)])

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=('resposta_pesquisa', 'pergunta'), name='uma_resposta_por_pergunta'),
            models.CheckConstraint(
                condition=(models.Q(valor_texto__isnull=False, valor_inteiro__isnull=True) | models.Q(valor_texto__isnull=True, valor_inteiro__isnull=False)),
                name='detalhe_exatamente_um_valor',
            ),
        ]

    def clean(self):
        super().clean()
        if self.pergunta_id:
            if self.pergunta.tipo_resposta == Pergunta.TipoResposta.TEXTO:
                if not (self.valor_texto or '').strip() or self.valor_inteiro is not None:
                    raise ValidationError('Esta pergunta exige uma resposta em texto.')
            elif self.valor_inteiro is None or self.valor_texto is not None:
                raise ValidationError('Esta pergunta exige uma nota de 0 a 10.')
        if self.resposta_pesquisa_id and self.pergunta_id and self.resposta_pesquisa.pesquisa_id != self.pergunta.pesquisa_id:
            raise ValidationError('A pergunta não pertence à pesquisa respondida.')

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f'Detalhe #{self.pk}'

class ImportacaoColaboradores(models.Model):
    """Planilha de colaboradores em revisão: as linhas ficam aqui até a confirmação, sem tocar no cadastro.

    `linhas` guarda [{'numero': nº da linha na planilha, 'valores': {coluna: texto}}]; os erros não são
    guardados, e sim recalculados a cada edição (dependem também do que já está cadastrado).
    """
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='importacoes_colaboradores')
    nome_arquivo = models.CharField(max_length=255)
    linhas = models.JSONField(default=list)
    colunas_ignoradas = models.JSONField(default=list)
    criada_em = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = 'importação de colaboradores em revisão'
        verbose_name_plural = 'importações de colaboradores em revisão'

    def __str__(self):
        return f'{self.nome_arquivo} ({self.usuario})'
