from django.db import migrations, models

import pesquisas.models


def celular_para_celulares(apps, schema_editor):
    Colaborador = apps.get_model('pesquisas', 'Colaborador')
    for pk, celular in Colaborador.objects.exclude(celular='').values_list('pk', 'celular'):
        Colaborador.objects.filter(pk=pk).update(celulares=[celular])


def celulares_para_celular(apps, schema_editor):
    # Reversão: mantém só o primeiro número de cada colaborador.
    Colaborador = apps.get_model('pesquisas', 'Colaborador')
    for pk, celulares in Colaborador.objects.values_list('pk', 'celulares'):
        if celulares:
            Colaborador.objects.filter(pk=pk).update(celular=celulares[0])


class Migration(migrations.Migration):

    dependencies = [
        ('pesquisas', '0016_colaborador_empresas'),
    ]

    operations = [
        migrations.AddField(
            model_name='colaborador',
            name='celulares',
            field=models.JSONField(
                blank=True, default=list, validators=[pesquisas.models.validar_celulares], verbose_name='Celulares',
            ),
        ),
        migrations.RunPython(celular_para_celulares, celulares_para_celular),
        migrations.RemoveField(model_name='colaborador', name='celular'),
    ]
