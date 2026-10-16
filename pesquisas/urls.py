from django.urls import path

from . import views


app_name = 'pesquisas'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('colaboradores/', views.colaborador_lista, name='colaborador_lista'),
    path('colaboradores/exportar/', views.colaborador_exportar, name='colaborador_exportar'),
    path('colaboradores/novo/', views.colaborador_criar, name='colaborador_criar'),
    path('colaboradores/importar/', views.colaborador_importar, name='colaborador_importar'),
    path('colaboradores/importar/<int:pk>/', views.colaborador_importar_revisao, name='colaborador_importar_revisao'),
    path('colaboradores/importar/<int:pk>/celula/', views.colaborador_importar_celula, name='colaborador_importar_celula'),
    path('colaboradores/importar/<int:pk>/descartar/', views.colaborador_importar_descartar, name='colaborador_importar_descartar'),
    path('colaboradores/importar/<int:pk>/confirmar/', views.colaborador_importar_confirmar, name='colaborador_importar_confirmar'),
    path('colaboradores/importar/<int:pk>/cancelar/', views.colaborador_importar_cancelar, name='colaborador_importar_cancelar'),
    path('colaboradores/importar/modelo.csv', views.colaborador_modelo_importacao, name='colaborador_modelo_importacao'),
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
