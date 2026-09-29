"""Árvore organizacional: Departamento > Seção > Cargo (ex.: Tecnologia > Dev > Front-end).

Regra crítica: nenhum nó pode ser excluído enquanto tiver dependências (filhos ou,
nos módulos seguintes, currículos vinculados). Use `ativo=False` para aposentar um nó.
"""
from django.db import models, transaction
from django.db.models import ProtectedError
from django.db.models.functions import Lower


class ProtegidoQuerySet(models.QuerySet):
    def delete(self):
        # QuerySet.delete() não chama Model.delete(); fazemos aqui para que a
        # exclusão em massa (ex.: ação do admin) respeite a mesma proteção.
        with transaction.atomic():
            total = 0
            for objeto in self:
                objeto.delete()
                total += 1
        return total, {self.model._meta.label: total}

    delete.alters_data = True


class NoOrganizacional(models.Model):
    """Base abstrata: campo `ativo` e bloqueio de exclusão com dependências."""

    ativo = models.BooleanField(default=True)

    objects = ProtegidoQuerySet.as_manager()

    class Meta:
        abstract = True

    def _dependentes(self):
        """Querysets dos registros que apontam para este nó (filhos, currículos vinculados etc.).

        Percorre as relações reversas do model; novas FKs (ex.: Curriculo -> Cargo)
        passam a ser protegidas automaticamente, sem alterar esta classe.
        """
        for relacao in self._meta.related_objects:
            yield relacao.related_model._base_manager.filter(**{relacao.field.name: self})

    @property
    def possui_dependencias(self):
        return any(queryset.exists() for queryset in self._dependentes())

    def delete(self, *args, **kwargs):
        dependentes = {obj for queryset in self._dependentes() for obj in queryset[:5]}
        if dependentes:
            raise ProtectedError(
                f'“{self}” possui registros vinculados e não pode ser excluído. Inative-o em vez disso.',
                dependentes,
            )
        return super().delete(*args, **kwargs)

    delete.alters_data = True


class Departamento(NoOrganizacional):
    nome = models.CharField(max_length=120)

    class Meta:
        ordering = ('nome',)
        verbose_name = 'departamento'
        verbose_name_plural = 'departamentos'
        constraints = [
            models.UniqueConstraint(
                Lower('nome'), name='departamento_nome_unico',
                violation_error_message='Já existe um departamento com este nome.',
            ),
        ]

    pai = None

    def __str__(self):
        return self.nome


class Secao(NoOrganizacional):
    departamento = models.ForeignKey(Departamento, on_delete=models.PROTECT, related_name='secoes')
    nome = models.CharField(max_length=120)

    class Meta:
        ordering = ('departamento__nome', 'nome')
        verbose_name = 'seção'
        verbose_name_plural = 'seções'
        constraints = [
            models.UniqueConstraint(
                Lower('nome'), 'departamento', name='secao_nome_unico_por_departamento',
                violation_error_message='Já existe uma seção com este nome neste departamento.',
            ),
        ]

    @property
    def pai(self):
        return self.departamento

    @property
    def nome_completo(self):
        return f'{self.departamento} › {self.nome}'

    def __str__(self):
        # ALTERADO: Retorna apenas o nome isolado da seção
        return self.nome


class Cargo(NoOrganizacional):
    secao = models.ForeignKey(Secao, on_delete=models.PROTECT, related_name='cargos', verbose_name='seção')
    nome = models.CharField(max_length=120)

    class Meta:
        ordering = ('secao__departamento__nome', 'secao__nome', 'nome')
        verbose_name = 'cargo'
        verbose_name_plural = 'cargos'
        constraints = [
            models.UniqueConstraint(
                Lower('nome'), 'secao', name='cargo_nome_unico_por_secao',
                violation_error_message='Já existe um cargo com este nome nesta seção.',
            ),
        ]

    @property
    def pai(self):
        return self.secao

    @property
    def departamento(self):
        return self.secao.departamento

    @property
    def nome_completo(self):
        return f'{self.secao} › {self.nome}'

    def __str__(self):
        return self.nome