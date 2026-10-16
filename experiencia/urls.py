from django.urls import path

from . import views


app_name = 'experiencia'

urlpatterns = [
    path('experiencia/', views.kanban, name='kanban'),
    path('experiencia/<int:pk>/beneficios/', views.beneficios, name='beneficios'),
    path('experiencia/<int:pk>/arquivar/', views.arquivar, name='arquivar'),
    path('experiencia/avaliacao/<uuid:token>/', views.avaliar, name='avaliar'),
    path('experiencia/avaliacoes/', views.avaliacoes_painel, name='avaliacoes_painel'),
    path('experiencia/avaliacoes/<int:pk>/respostas/', views.avaliacao_respostas, name='avaliacao_respostas'),
    path('experiencia/avaliacoes/formularios/novo/', views.formulario_criar, name='formulario_criar'),
    path('experiencia/avaliacoes/formularios/<int:pk>/editar/', views.formulario_editar, name='formulario_editar'),
    path('experiencia/avaliacoes/formularios/<int:pk>/padrao/', views.formulario_definir_padrao, name='formulario_definir_padrao'),
    path('experiencia/avaliacoes/formularios/<int:pk>/excluir/', views.formulario_excluir, name='formulario_excluir'),
]
