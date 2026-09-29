from django.urls import path

from . import views


app_name = 'pesquisas'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('colaboradores/', views.colaborador_lista, name='colaborador_lista'),
    path('colaboradores/novo/', views.colaborador_criar, name='colaborador_criar'),
    path('colaboradores/<int:pk>/editar/', views.colaborador_editar, name='colaborador_editar'),
    path('colaboradores/<int:pk>/status/', views.colaborador_alterar_status, name='colaborador_status'),
    path('perfis/', views.perfil_lista, name='perfil_lista'),
    path('perfis/novo/', views.perfil_criar, name='perfil_criar'),
    path('perfis/<int:pk>/editar/', views.perfil_editar, name='perfil_editar'),
    path('perfis/<int:pk>/status/', views.perfil_alterar_status, name='perfil_status'),
    path('perfis/<int:pk>/excluir/', views.perfil_excluir, name='perfil_excluir'),
    path('gerenciar-pesquisas/', views.pesquisa_lista, name='pesquisa_lista'),
    path('gerenciar-pesquisas/nova/', views.pesquisa_criar, name='pesquisa_criar'),
    path('gerenciar-pesquisas/<int:pk>/excluir/', views.pesquisa_excluir, name='pesquisa_excluir'),
    path('gerenciar-pesquisas/<int:pk>/editar/', views.pesquisa_editar, name='pesquisa_editar'),
    path('relatorios/<int:pk>/', views.resultados, name='resultados'),
    path('pesquisa/<uuid:link_uuid>/', views.validar_participante, name='responder'),
    path('pesquisa/<uuid:link_uuid>/pergunta/<int:etapa>/', views.responder_etapa, name='responder_etapa'),
    path('pesquisa/<uuid:link_uuid>/obrigado/', views.agradecimento, name='agradecimento'),
]
