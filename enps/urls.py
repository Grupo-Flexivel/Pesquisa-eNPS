"""
URL configuration for core project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from pesquisas.auth_views import ENPSLoginView, LocalPasswordChangeView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/login/', ENPSLoginView.as_view(), name='login'),
    path('accounts/password_change/', LocalPasswordChangeView.as_view(), name='password_change'),
    path('accounts/', include('django.contrib.auth.urls')),
    path('', include('pesquisas.urls')),
    path('', include('desligamentos.urls')),
    path('', include('vagas.urls')),
    path('', include('experiencia.urls')),
    path('', include('notificacoes.urls')),
    path('banco-de-talentos/organizacao/', include('organizacao.urls')),
    path('banco-de-talentos/curriculos/', include('talentos.urls')),
]

# Permite que o Django sirva arquivos estáticos corretamente durante o desenvolvimento (DEBUG=True)
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)