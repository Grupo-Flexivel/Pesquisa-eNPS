import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


def copiar_opcoes_do_formulario(apps, schema_editor):
    """As opções deixam de ser do formulário e passam a ser de cada pergunta.

    Perguntas já cadastradas viram "múltipla escolha" com as mesmas opções que o formulário
    tinha, para que nada já configurado se perca.
    """
    PerguntaAvaliacao = apps.get_model('experiencia', 'PerguntaAvaliacao')
    OpcaoRespostaAvaliacao = apps.get_model('experiencia', 'OpcaoRespostaAvaliacao')
    OpcaoPerguntaAvaliacao = apps.get_model('experiencia', 'OpcaoPerguntaAvaliacao')
    for pergunta in PerguntaAvaliacao.objects.all():
        opcoes = OpcaoRespostaAvaliacao.objects.filter(formulario_id=pergunta.formulario_id).order_by('ordem', 'pk')
        OpcaoPerguntaAvaliacao.objects.bulk_create([
            OpcaoPerguntaAvaliacao(pergunta=pergunta, texto_opcao=opcao.texto, ordem=ordem)
            for ordem, opcao in enumerate(opcoes, start=1)
        ])


def converter_respostas(apps, schema_editor):
    """Respostas já enviadas eram sempre de escolha única: a opção escolhida vira a lista selecionada."""
    DetalheRespostaAvaliacao = apps.get_model('experiencia', 'DetalheRespostaAvaliacao')
    for detalhe in DetalheRespostaAvaliacao.objects.all():
        detalhe.opcoes_selecionadas = [detalhe.opcao_texto] if detalhe.opcao_texto else []
        detalhe.save(update_fields=['opcoes_selecionadas'])


class Migration(migrations.Migration):

    dependencies = [
        ('experiencia', '0003_formularios_avaliacao'),
    ]

    operations = [
        migrations.RenameField(model_name='perguntaavaliacao', old_name='texto', new_name='texto_pergunta'),
        migrations.AlterField(
            model_name='perguntaavaliacao',
            name='texto_pergunta',
            field=models.TextField(verbose_name='Texto pergunta'),
        ),
        migrations.AddField(
            model_name='perguntaavaliacao',
            name='tipo_resposta',
            field=models.CharField(
                choices=[('TEXTO', 'Texto'), ('NOTA_0_10', 'Nota de 0 a 10'), ('RADIO', 'Múltipla escolha (escolha única)'), ('CHECKBOX', 'Caixa de seleção (múltipla escolha)')],
                default='RADIO', max_length=10, verbose_name='Tipo resposta',
            ),
            preserve_default=False,
        ),
        migrations.CreateModel(
            name='OpcaoPerguntaAvaliacao',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('texto_opcao', models.CharField(max_length=200)),
                ('ordem', models.PositiveIntegerField(validators=[django.core.validators.MinValueValidator(1)])),
                ('pergunta', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='opcoes', to='experiencia.perguntaavaliacao')),
            ],
            options={
                'ordering': ('ordem', 'pk'),
                'constraints': [models.UniqueConstraint(fields=('pergunta', 'ordem'), name='opcao_pergunta_avaliacao_ordem_unica')],
            },
        ),
        migrations.RunPython(copiar_opcoes_do_formulario, migrations.RunPython.noop),

        migrations.AddField(
            model_name='detalherespostaavaliacao',
            name='tipo_resposta',
            field=models.CharField(
                choices=[('TEXTO', 'Texto'), ('NOTA_0_10', 'Nota de 0 a 10'), ('RADIO', 'Múltipla escolha (escolha única)'), ('CHECKBOX', 'Caixa de seleção (múltipla escolha)')],
                default='RADIO', max_length=10,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='detalherespostaavaliacao',
            name='valor_texto',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='detalherespostaavaliacao',
            name='valor_inteiro',
            field=models.IntegerField(blank=True, null=True, validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(10)]),
        ),
        migrations.AddField(
            model_name='detalherespostaavaliacao',
            name='opcoes_selecionadas',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.RunPython(converter_respostas, migrations.RunPython.noop),
        migrations.RemoveField(model_name='detalherespostaavaliacao', name='opcao'),
        migrations.RemoveField(model_name='detalherespostaavaliacao', name='opcao_texto'),
        migrations.DeleteModel(name='OpcaoRespostaAvaliacao'),
    ]
