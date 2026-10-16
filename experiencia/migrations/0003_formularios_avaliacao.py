import uuid

import django.core.validators
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


def gerar_tokens(apps, schema_editor):
    """Um UUID distinto por card já existente (o default do AddField seria o mesmo para todos)."""
    PeriodoExperiencia = apps.get_model('experiencia', 'PeriodoExperiencia')
    for periodo in PeriodoExperiencia.objects.filter(token_avaliacao__isnull=True).only('pk'):
        PeriodoExperiencia.objects.filter(pk=periodo.pk).update(token_avaliacao=uuid.uuid4())


def converter_avaliacao_realizada(apps, schema_editor):
    """A antiga caixa "Avaliação realizada" vira o status da avaliação.

    Só conta como respondida a avaliação de quem já saiu de "Vencendo" (efetivado/desligado).
    Um card ainda aberto com a caixa marcada não tem decisão registrada: volta a Pendente para
    o gestor responder pelo formulário (que agora traz a decisão obrigatória).
    """
    PeriodoExperiencia = apps.get_model('experiencia', 'PeriodoExperiencia')
    PeriodoExperiencia.objects.filter(
        avaliacao_realizada=True, status__in=('EFETIVADO', 'DESLIGADO'),
    ).update(status_avaliacao='RESPONDIDO')


class Migration(migrations.Migration):

    dependencies = [
        ('experiencia', '0002_periodoexperiencia_arquivamento'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='FormularioAvaliacao',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('titulo', models.CharField(max_length=200, verbose_name='Título')),
                ('descricao', models.TextField(blank=True, help_text='Orientações exibidas ao gestor no início do formulário.', verbose_name='Descrição')),
                ('padrao', models.BooleanField(default=False, help_text='Formulário enviado aos gestores nas novas avaliações. Só um pode ser o padrão.', verbose_name='Formulário padrão')),
                ('criado_em', models.DateTimeField(default=django.utils.timezone.now)),
                ('atualizado_em', models.DateTimeField(auto_now=True)),
                ('criado_por', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'formulário de avaliação',
                'verbose_name_plural': 'formulários de avaliação',
                'ordering': ('-padrao', 'titulo'),
                'constraints': [models.UniqueConstraint(condition=models.Q(('padrao', True)), fields=('padrao',), name='formulario_avaliacao_unico_padrao')],
            },
        ),
        migrations.CreateModel(
            name='OpcaoRespostaAvaliacao',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('texto', models.CharField(max_length=100)),
                ('ordem', models.PositiveIntegerField(validators=[django.core.validators.MinValueValidator(1)])),
                ('formulario', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='opcoes', to='experiencia.formularioavaliacao')),
            ],
            options={
                'ordering': ('ordem', 'pk'),
                'constraints': [models.UniqueConstraint(fields=('formulario', 'ordem'), name='opcao_avaliacao_ordem_unica')],
            },
        ),
        migrations.CreateModel(
            name='PerguntaAvaliacao',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('texto', models.TextField(verbose_name='Pergunta')),
                ('ordem', models.PositiveIntegerField(validators=[django.core.validators.MinValueValidator(1)], verbose_name='Ordem')),
                ('formulario', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='perguntas', to='experiencia.formularioavaliacao')),
            ],
            options={
                'ordering': ('ordem', 'pk'),
                'constraints': [models.UniqueConstraint(fields=('formulario', 'ordem'), name='pergunta_avaliacao_ordem_unica')],
            },
        ),

        # Token do link de avaliação: nulo → preenchido card a card → único e obrigatório.
        migrations.AddField(
            model_name='periodoexperiencia',
            name='token_avaliacao',
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.RunPython(gerar_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='periodoexperiencia',
            name='token_avaliacao',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),

        migrations.AddField(
            model_name='periodoexperiencia',
            name='status_avaliacao',
            field=models.CharField(choices=[('PENDENTE', 'Pendente'), ('RESPONDIDO', 'Respondida')], default='PENDENTE', max_length=10, verbose_name='Status da avaliação'),
        ),
        migrations.RunPython(converter_avaliacao_realizada, migrations.RunPython.noop),
        migrations.RemoveField(model_name='periodoexperiencia', name='avaliacao_realizada'),

        migrations.AddField(
            model_name='periodoexperiencia',
            name='formulario',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='periodos', to='experiencia.formularioavaliacao'),
        ),
        migrations.CreateModel(
            name='RespostaAvaliacao',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('formulario_titulo', models.CharField(max_length=200)),
                ('decisao', models.CharField(choices=[('EFETIVADO', 'Efetivado'), ('DESLIGADO', 'Desligado')], max_length=10, verbose_name='Decisão')),
                ('observacoes', models.TextField(blank=True, verbose_name='Observações')),
                ('respondido_em', models.DateTimeField(default=django.utils.timezone.now)),
                ('formulario', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='respostas', to='experiencia.formularioavaliacao')),
                ('periodo', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='resposta_avaliacao', to='experiencia.periodoexperiencia')),
                ('respondido_por', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'resposta de avaliação',
                'verbose_name_plural': 'respostas de avaliação',
                'ordering': ('-respondido_em',),
            },
        ),
        migrations.CreateModel(
            name='DetalheRespostaAvaliacao',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('ordem', models.PositiveIntegerField()),
                ('pergunta_texto', models.TextField()),
                ('opcao_texto', models.CharField(max_length=100)),
                ('opcao', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='experiencia.opcaorespostaavaliacao')),
                ('pergunta', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='experiencia.perguntaavaliacao')),
                ('resposta', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='detalhes', to='experiencia.respostaavaliacao')),
            ],
            options={
                'ordering': ('ordem', 'pk'),
            },
        ),
    ]
