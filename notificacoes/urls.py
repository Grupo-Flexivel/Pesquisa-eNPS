from django.urls import path

from . import views


app_name = 'notificacoes'

urlpatterns = [
    path('notificacoes/<int:pk>/abrir/', views.abrir, name='abrir'),
    path('notificacoes/marcar-todas-lidas/', views.marcar_todas_lidas, name='marcar_todas_lidas'),
]
