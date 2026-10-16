from django.utils import timezone

from .models import Notificacao


def notificar(usuarios, mensagem, link='', chave=''):
    """Cria a notificação para cada usuário. Com `chave`, não duplica avisos já existentes.

    Um aviso com a mesma `chave` ainda não lido tem o texto atualizado (ex.: o gestor responsável
    mudou); um aviso já lido não volta a aparecer.
    """
    criadas = []
    for usuario in usuarios:
        if chave:
            notificacao, criada = Notificacao.objects.get_or_create(
                destinatario=usuario, chave=chave, defaults={'mensagem': mensagem, 'link': link},
            )
            if criada:
                criadas.append(notificacao)
            elif not notificacao.lida and notificacao.mensagem != mensagem:
                notificacao.mensagem = mensagem
                notificacao.save(update_fields=['mensagem'])
        else:
            criadas.append(Notificacao.objects.create(destinatario=usuario, mensagem=mensagem, link=link))
    return criadas


def resolver(chave, exceto=()):
    """Marca como lidas as notificações de uma pendência concluída (ou que mudou de responsável).

    `exceto`: destinatários cujos avisos continuam pendentes.
    """
    pendentes = Notificacao.objects.filter(chave=chave, lida=False).exclude(destinatario__in=list(exceto))
    return pendentes.update(lida=True, lida_em=timezone.now())
