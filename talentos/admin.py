from django.contrib import admin

from .models import CargoEmbedding, Candidato, HistoricoCandidatura


@admin.register(Candidato)
class CandidatoAdmin(admin.ModelAdmin):
    list_display = ('nome_exibicao', 'cidade', 'status', 'processamento', 'ativo', 'cargo_sugerido', 'criado_em')
    list_filter = ('status', 'processamento', 'ativo')
    search_fields = ('nome', 'email', 'cidade')
    # Auditoria de inativação é somente leitura aqui: só muda via Candidato.inativar()/
    # reativar() (telas do módulo), nunca por edição direta no admin.
    readonly_fields = (
        'hash_arquivo', 'texto_extraido', 'criado_em', 'atualizado_em', 'enviado_por',
        'ativo', 'motivo_inativacao', 'inativado_por', 'inativado_em',
    )
    exclude = ('embedding', 'inativado_por_id')  # vetor grande e IDs cruzados sem utilidade para edição manual

    def get_queryset(self, request):
        # `all_objects`: o admin precisa continuar enxergando os inativos.
        return Candidato.all_objects.get_queryset()


@admin.register(HistoricoCandidatura)
class HistoricoCandidaturaAdmin(admin.ModelAdmin):
    """Somente leitura: a trilha de auditoria não pode ser criada nem alterada manualmente."""
    list_display = ('data_hora', 'candidato', 'evento', 'usuario_nome', 'descricao')
    list_filter = ('evento',)
    search_fields = ('descricao', 'usuario_nome')
    date_hierarchy = 'data_hora'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CargoEmbedding)
class CargoEmbeddingAdmin(admin.ModelAdmin):
    """Somente leitura: gerado por `CurriculumAutomationService`, nunca editado manualmente."""
    list_display = ('cargo_id', 'atualizado_em')
    search_fields = ('cargo_id',)
    exclude = ('embedding',)
    readonly_fields = ('cargo_id', 'texto_origem', 'atualizado_em')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
