from django.urls import path

from . import views


app_name = 'talentos'

urlpatterns = [
    path('painel/', views.painel, name='painel'),
    path('busca/', views.busca, name='busca'),
    path('quadro/', views.quadro, name='quadro'),
    path('<int:pk>/mover/', views.mover, name='mover'),
    path('', views.lista, name='lista'),
    path('enviar/', views.upload, name='upload'),
    path('<int:pk>/', views.detalhe, name='detalhe'),
    path('<int:pk>/pdf/', views.pdf, name='pdf'),
    path('<int:pk>/reprocessar/', views.reprocessar, name='reprocessar'),
    path('<int:pk>/excluir/', views.excluir, name='excluir'),
    path('<int:pk>/inativar/', views.inativar, name='inativar'),
    path('<int:pk>/reativar/', views.reativar, name='reativar'),
]
