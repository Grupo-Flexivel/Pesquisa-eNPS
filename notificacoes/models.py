from django.conf import settings
from django.db import models
from django.utils import timezone


class Notificacao(models.Model):
    """Aviso exibido no sininho do topo da tela para um usuário do painel.

    `chave` identifica o assunto (ex.: "experiencia:12:beneficios"): torna a criação idempotente
    (a rotina pode rodar várias vezes sem duplicar avisos) e permite resolver todos os avisos de
    um mesmo assunto de uma vez quando a pendência é concluída.
    """

    destinatario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notificacoes')
    mensagem = models.CharField(max_length=300)
    link = models.CharField(max_length=300, blank=True)
    chave = models.CharField(max_length=100, blank=True)
    lida = models.BooleanField(default=False)
    criada_em = models.DateTimeField(default=timezone.now)
    lida_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('lida', '-criada_em')
        verbose_name = 'notificação'
        verbose_name_plural = 'notificações'
        constraints = [
            models.UniqueConstraint(
                fields=('destinatario', 'chave'), condition=~models.Q(chave=''),
                name='notificacao_unica_por_chave_e_destinatario',
            ),
        ]
        indexes = [models.Index(fields=('destinatario', 'lida'))]

    def __str__(self):
        return self.mensagem
