from django.apps import AppConfig


class ExperienciaConfig(AppConfig):
    name = 'experiencia'
    verbose_name = 'Período de experiência'

    def ready(self):
        from . import signals  # noqa: F401
