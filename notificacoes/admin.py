from django.contrib import admin

from .models import Notificacao


@admin.register(Notificacao)
class NotificacaoAdmin(admin.ModelAdmin):
    list_display = ('mensagem', 'destinatario', 'lida', 'criada_em')
    list_filter = ('lida',)
    search_fields = ('mensagem', 'destinatario__username', 'chave')
    readonly_fields = ('criada_em', 'lida_em')
