from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('talentos', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='candidato',
            name='ativo',
            field=models.BooleanField(db_index=True, default=True),
        ),
        migrations.AddField(
            model_name='candidato',
            name='motivo_inativacao',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='candidato',
            name='inativado_por_id',
            field=models.IntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='candidato',
            name='inativado_em',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='historicocandidatura',
            name='evento',
            field=models.CharField(choices=[
                ('CURRICULO_ENVIADO', 'Currículo enviado'),
                ('PROCESSAMENTO_CONCLUIDO', 'Currículo processado'),
                ('PROCESSAMENTO_ERRO', 'Falha no processamento'),
                ('REPROCESSAMENTO', 'Reprocessamento solicitado'),
                ('ETAPA_ALTERADA', 'Etapa alterada'),
                ('CARGO_DEFINIDO', 'Cargo definido'),
                ('DADOS_EDITADOS', 'Dados editados'),
                ('CURRICULO_ATUALIZADO', 'Currículo atualizado (cadastro já existente)'),
                ('REPROVACAO_REGISTRADA', 'Reprovação registrada'),
                ('CANDIDATO_INATIVADO', 'Candidato inativado'),
                ('CANDIDATO_REATIVADO', 'Candidato reativado'),
            ], max_length=30),
        ),
        migrations.AlterModelOptions(
            name='candidato',
            options={'base_manager_name': 'all_objects', 'ordering': ('-criado_em',), 'verbose_name': 'candidato', 'verbose_name_plural': 'candidatos'},
        ),
    ]
