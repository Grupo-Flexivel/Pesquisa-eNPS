import django.db.models.deletion
from django.db import migrations, models


def converter_gestor_usuario_para_colaborador(apps, schema_editor):
    """O gestor deixa de ser um usuário do painel e passa a ser um colaborador marcado como gestor.

    Vínculos existentes são preservados pelo colaborador ligado ao mesmo login (Colaborador.usuario),
    que é marcado como gestor. Usuários sem colaborador vinculado não têm equivalente: o vínculo é
    limpo e o aviso de avaliação passa a ir para o RH até um gestor ser escolhido.
    """
    Colaborador = apps.get_model('pesquisas', 'Colaborador')
    colaborador_por_usuario = dict(
        Colaborador.objects.filter(usuario__isnull=False).values_list('usuario_id', 'pk')
    )
    for colaborador in Colaborador.objects.filter(gestor_usuario__isnull=False):
        gestor_pk = colaborador_por_usuario.get(colaborador.gestor_usuario_id)
        if gestor_pk and gestor_pk != colaborador.pk:
            Colaborador.objects.filter(pk=colaborador.pk).update(gestor_id=gestor_pk)
            Colaborador.objects.filter(pk=gestor_pk).update(eh_gestor=True)


class Migration(migrations.Migration):

    dependencies = [
        ('pesquisas', '0013_colaborador_gestor'),
    ]

    operations = [
        migrations.RenameField(model_name='colaborador', old_name='gestor', new_name='gestor_usuario'),
        migrations.AddField(
            model_name='colaborador',
            name='eh_gestor',
            field=models.BooleanField(
                default=False, verbose_name='É gestor',
                help_text='Marque para que este colaborador possa ser escolhido como gestor responsável de outros.',
            ),
        ),
        migrations.AddField(
            model_name='colaborador',
            name='gestor',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='liderados', to='pesquisas.colaborador', verbose_name='Gestor responsável',
            ),
        ),
        migrations.RunPython(converter_gestor_usuario_para_colaborador, migrations.RunPython.noop),
        migrations.RemoveField(model_name='colaborador', name='gestor_usuario'),
    ]
