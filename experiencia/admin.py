from django.contrib import admin

from .models import (
    DetalheRespostaAvaliacao, FormularioAvaliacao, OpcaoPerguntaAvaliacao, PerguntaAvaliacao, PeriodoExperiencia,
    RespostaAvaliacao,
)


@admin.register(PeriodoExperiencia)
class PeriodoExperienciaAdmin(admin.ModelAdmin):
    list_display = ('colaborador', 'status', 'status_avaliacao', 'beneficios_finalizados', 'status_alterado_em')
    list_filter = ('status', 'status_avaliacao', 'beneficios_finalizados', 'efetivado_automaticamente')
    search_fields = ('colaborador__nome', 'colaborador__documento')
    # As transições têm regras (avaliação obrigatória, notificações): devem passar pelo Kanban.
    readonly_fields = [field.name for field in PeriodoExperiencia._meta.fields]

    def has_add_permission(self, request):
        return False


class PerguntaAvaliacaoInline(admin.TabularInline):
    model = PerguntaAvaliacao
    extra = 0
    show_change_link = True


class OpcaoPerguntaAvaliacaoInline(admin.TabularInline):
    model = OpcaoPerguntaAvaliacao
    extra = 0


@admin.register(PerguntaAvaliacao)
class PerguntaAvaliacaoAdmin(admin.ModelAdmin):
    list_display = ('texto_pergunta', 'formulario', 'tipo_resposta', 'ordem')
    list_filter = ('formulario', 'tipo_resposta')
    inlines = (OpcaoPerguntaAvaliacaoInline,)


@admin.register(FormularioAvaliacao)
class FormularioAvaliacaoAdmin(admin.ModelAdmin):
    list_display = ('titulo', 'padrao', 'criado_por', 'atualizado_em')
    readonly_fields = ('criado_por',)
    inlines = (PerguntaAvaliacaoInline,)

    def save_model(self, request, obj, form, change):
        if not obj.pk:
            obj.criado_por = request.user
        super().save_model(request, obj, form, change)


class DetalheRespostaAvaliacaoInline(admin.TabularInline):
    model = DetalheRespostaAvaliacao
    extra = 0
    can_delete = False
    readonly_fields = ('ordem', 'pergunta_texto', 'tipo_resposta', 'valor_texto', 'valor_inteiro', 'opcoes_selecionadas')
    fields = readonly_fields


@admin.register(RespostaAvaliacao)
class RespostaAvaliacaoAdmin(admin.ModelAdmin):
    list_display = ('periodo', 'decisao', 'respondido_por', 'respondido_em')
    list_filter = ('decisao',)
    search_fields = ('periodo__colaborador__nome',)
    readonly_fields = [field.name for field in RespostaAvaliacao._meta.fields]
    inlines = (DetalheRespostaAvaliacaoInline,)

    def has_add_permission(self, request):
        return False
