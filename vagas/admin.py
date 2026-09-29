from django.contrib import admin

from .models import Candidatura, DetalheRespostaCandidatura, OpcaoPerguntaVaga, PerguntaVaga, Vaga


class OpcaoPerguntaVagaInline(admin.TabularInline):
    model = OpcaoPerguntaVaga
    extra = 2


class PerguntaVagaInline(admin.TabularInline):
    model = PerguntaVaga
    extra = 1
    min_num = 1
    validate_min = True


@admin.register(Vaga)
class VagaAdmin(admin.ModelAdmin):
    list_display = ('titulo', 'data_abertura', 'data_encerramento', 'criado_por', 'data_criacao')
    list_filter = ('data_abertura', 'data_encerramento')
    readonly_fields = ('link_uuid', 'criado_por')
    inlines = (PerguntaVagaInline,)

    def save_model(self, request, obj, form, change):
        if not obj.pk:
            obj.criado_por = request.user
        super().save_model(request, obj, form, change)


@admin.register(PerguntaVaga)
class PerguntaVagaAdmin(admin.ModelAdmin):
    list_display = ('vaga', 'ordem', 'texto_pergunta', 'tipo_resposta')
    list_filter = ('vaga', 'tipo_resposta')
    inlines = (OpcaoPerguntaVagaInline,)


@admin.register(Candidatura)
class CandidaturaAdmin(admin.ModelAdmin):
    list_display = ('colaborador', 'vaga', 'data_candidatura')
    list_filter = ('vaga',)
    search_fields = ('colaborador__nome', 'colaborador__documento')
    readonly_fields = ('hash_documento_candidato', 'data_candidatura')

    def has_add_permission(self, request):
        return False


@admin.register(DetalheRespostaCandidatura)
class DetalheRespostaCandidaturaAdmin(admin.ModelAdmin):
    list_display = ('id', 'pergunta', 'candidatura')
    readonly_fields = ('candidatura', 'pergunta', 'valor_texto', 'valor_inteiro', 'opcoes')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
