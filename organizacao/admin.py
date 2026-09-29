from django.contrib import admin

from .models import Cargo, Departamento, Secao


@admin.register(Departamento)
class DepartamentoAdmin(admin.ModelAdmin):
    list_display = ('nome', 'ativo')
    list_filter = ('ativo',)
    search_fields = ('nome',)


@admin.register(Secao)
class SecaoAdmin(admin.ModelAdmin):
    list_display = ('nome', 'departamento', 'ativo')
    list_filter = ('ativo', 'departamento')
    search_fields = ('nome',)


@admin.register(Cargo)
class CargoAdmin(admin.ModelAdmin):
    list_display = ('nome', 'secao', 'ativo')
    list_filter = ('ativo', 'secao__departamento')
    search_fields = ('nome',)
