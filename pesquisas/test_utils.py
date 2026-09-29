"""Helpers compartilhados pelos testes (não é um módulo de testes em si)."""
from .models import PerfilAcesso, User


def criar_usuario(username, password='x', administrador=False, modulos=None, perfil_personalizado=None, **extra):
    """Cria um usuário de teste com o novo esquema de acesso (is_admin_total / perfil_personalizado).

    `modulos`, se informado, cria (ou reaproveita) um PerfilAcesso com exatamente essas chaves e
    vincula o usuário a ele — evita repetir a criação do PerfilAcesso em cada teste. Para testar um
    perfil já existente (ex.: sem módulos), passe-o diretamente em `perfil_personalizado`.
    """
    if modulos:
        perfil_personalizado, _ = PerfilAcesso.objects.get_or_create(
            nome=f'Teste: {"+".join(sorted(modulos))}', defaults={'modulos_permitidos': list(modulos)},
        )
    return User.objects.create_user(
        username, password=password, is_admin_total=administrador,
        perfil_personalizado=perfil_personalizado, **extra,
    )
