from django.urls import path

from . import views


app_name = 'vagas'

urlpatterns = [
    # Painel interno (login corporativo obrigatório)
    path('vagas/', views.vaga_lista, name='vaga_lista'),
    path('vagas/nova/', views.vaga_criar, name='vaga_criar'),
    path('vagas/<int:pk>/', views.vaga_detalhe, name='vaga_detalhe'),
    path('vagas/<int:pk>/editar/', views.vaga_editar, name='vaga_editar'),
    path('vagas/<int:pk>/excluir/', views.vaga_excluir, name='vaga_excluir'),
    path('vagas/<int:pk>/qrcode/', views.vaga_qrcode, name='vaga_qrcode'),
    path('vagas/candidatura/<int:pk>/respostas/', views.candidatura_respostas, name='candidatura_respostas'),

    # Fluxo público por UUID (sem login, validado por CPF de colaborador ativo)
    path('talentos/vaga/<uuid:vaga_uuid>/', views.validar_cpf_candidatura_view, name='candidatar'),
    path('talentos/vaga/<uuid:vaga_uuid>/formulario/', views.submeter_candidatura_view, name='submeter_candidatura'),
    path('talentos/vaga/<uuid:vaga_uuid>/obrigado/', views.agradecimento, name='agradecimento'),
]
