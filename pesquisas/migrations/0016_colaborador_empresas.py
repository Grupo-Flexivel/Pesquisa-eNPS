from django.db import migrations, models


def copiar_empresa_para_empresas(apps, schema_editor):
    Colaborador = apps.get_model('pesquisas', 'Colaborador')
    Vinculo = Colaborador.empresas.through
    Vinculo.objects.bulk_create(
        [Vinculo(colaborador_id=pk, empresa_id=empresa_id)
         for pk, empresa_id in Colaborador.objects.values_list('pk', 'empresa_antiga_id')],
        ignore_conflicts=True,
    )


def copiar_empresas_para_empresa(apps, schema_editor):
    # Reversão: mantém só a primeira empresa (em ordem alfabética) de cada colaborador.
    Colaborador = apps.get_model('pesquisas', 'Colaborador')
    for colaborador in Colaborador.objects.all():
        primeira = colaborador.empresas.order_by('nome').first()
        if primeira:
            Colaborador.objects.filter(pk=colaborador.pk).update(empresa_antiga=primeira)


class Migration(migrations.Migration):

    dependencies = [
        ('pesquisas', '0015_colaborador_celular_email'),
    ]

    operations = [
        # Libera o related_name "colaboradores" para o novo campo enquanto os dois coexistem.
        migrations.RenameField(model_name='colaborador', old_name='empresa', new_name='empresa_antiga'),
        migrations.AlterField(
            model_name='colaborador',
            name='empresa_antiga',
            field=models.ForeignKey(
                null=True, on_delete=models.PROTECT, related_name='+', to='pesquisas.empresa',
            ),
        ),
        migrations.AddField(
            model_name='colaborador',
            name='empresas',
            field=models.ManyToManyField(related_name='colaboradores', to='pesquisas.empresa', verbose_name='Empresas'),
        ),
        migrations.RunPython(copiar_empresa_para_empresas, copiar_empresas_para_empresa),
        migrations.RemoveField(model_name='colaborador', name='empresa_antiga'),
    ]
