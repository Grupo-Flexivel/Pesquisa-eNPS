"""Importação de colaboradores em lote a partir de planilhas CSV ou XLSX.

Só nome, documento (CPF/CNPJ) e empresa (unidade fabril) são obrigatórios; as demais colunas
são opcionais e, em colaboradores já cadastrados, células vazias não apagam o valor atual.
O mesmo CPF em várias linhas é um único colaborador ligado a todas as empresas dessas linhas.

O processo tem três etapas, compartilhadas pela tela e pelo comando `importar_colaboradores_completo`:
1. `preparar_linhas`: lê a planilha e devolve as linhas como texto, sem gravar nada no cadastro;
2. `validar_linhas`: aponta os erros de cada célula (a tela permite corrigi-los e revalidar);
3. `gravar_linhas`: grava só as linhas válidas. O comando continua tudo ou nada.
"""
import csv
import io
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator
from django.db import transaction
from django.utils import timezone

from .models import Colaborador, Empresa, somente_digitos, validar_celulares, validar_documento


COLUNAS_OBRIGATORIAS = ('nome', 'documento', 'empresa')
COLUNAS_OPCIONAIS = ('celular', 'email', 'data_nascimento', 'data_contratacao', 'data_demissao', 'eh_gestor', 'gestor')
COLUNAS = COLUNAS_OBRIGATORIAS + COLUNAS_OPCIONAIS
ROTULOS = {
    'nome': 'Nome', 'documento': 'CPF/CNPJ', 'empresa': 'Empresa', 'celular': 'Celular', 'email': 'E-mail',
    'data_nascimento': 'Nascimento', 'data_contratacao': 'Contratação', 'data_demissao': 'Demissão',
    'eh_gestor': 'É gestor', 'gestor': 'Gestor',
}
# Chave dos erros que valem para a linha inteira, e não para uma célula.
LINHA = ''

# Nomes alternativos aceitos no cabeçalho (já normalizados: minúsculas, sem acento, "_" no lugar de espaço/hífen).
SINONIMOS = {
    'cpf': 'documento', 'cnpj': 'documento', 'cpf_cnpj': 'documento', 'cpf_ou_cnpj': 'documento',
    'unidade': 'empresa', 'unidade_fabril': 'empresa',
    'celulares': 'celular', 'telefone': 'celular', 'telefones': 'celular', 'e_mail': 'email',
    'nascimento': 'data_nascimento', 'data_de_nascimento': 'data_nascimento', 'dt_nascimento': 'data_nascimento',
    'admissao': 'data_contratacao', 'data_admissao': 'data_contratacao', 'data_de_contratacao': 'data_contratacao',
    'demissao': 'data_demissao', 'data_de_demissao': 'data_demissao',
    'e_gestor': 'eh_gestor',
    'nome_gestor': 'gestor', 'nome_do_gestor': 'gestor', 'gestor_responsavel': 'gestor',
}

FORMATOS_DATA = ('%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y', '%d/%m/%y', '%Y-%m-%d %H:%M:%S', '%d/%m/%Y %H:%M:%S')
VALORES_SIM = {'sim', 's', 'x', '1', 'true', 'verdadeiro'}
VALORES_NAO = {'nao', 'n', '0', 'false', 'falso'}

TAMANHO_MAXIMO = 5 * 1024 * 1024
EXTENSOES = ('.csv', '.xlsx')


class ErroImportacao(Exception):
    def __init__(self, erros):
        self.erros = erros if isinstance(erros, list) else [erros]
        super().__init__('; '.join(self.erros))


@dataclass
class ResultadoImportacao:
    criados: int = 0
    atualizados: int = 0
    ignoradas: int = 0  # linhas com erro deixadas de fora
    colunas_ignoradas: list = field(default_factory=list)


def _normalizar(valor):
    texto = unicodedata.normalize('NFKD', str(valor or '')).encode('ascii', 'ignore').decode()
    return '_'.join(texto.strip().lower().replace('-', ' ').split())


def _normalizar_cabecalho(valor):
    texto = _normalizar(valor)
    return SINONIMOS.get(texto, texto)


def _texto(valor):
    if valor is None:
        return ''
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    return ' '.join(str(valor).split())


def _documento(valor):
    # Planilhas costumam perder o zero à esquerda do CPF/CNPJ ao tratá-lo como número.
    digitos = somente_digitos(_texto(valor))
    if len(digitos) in (9, 10):
        return digitos.zfill(11)
    if len(digitos) in (12, 13):
        return digitos.zfill(14)
    return digitos


def _celulares(valor):
    # Vários números na mesma célula: separados por "/", ",", ";", "|" ou quebra de linha.
    texto = str(int(valor)) if isinstance(valor, float) and valor.is_integer() else str(valor or '')
    return [parte.strip() for parte in re.split(r'[/,;|\n]+', texto) if parte.strip()]


def _data(valor):
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    texto = _texto(valor)
    if not texto:
        return None
    for formato in FORMATOS_DATA:
        try:
            return datetime.strptime(texto, formato).date()
        except ValueError:
            continue
    raise ValidationError(f'Data "{texto}" inválida. Use DD/MM/AAAA.')


def _booleano(valor):
    texto = _normalizar(_texto(valor))
    if not texto:
        return None
    if texto in VALORES_SIM:
        return True
    if texto in VALORES_NAO:
        return False
    raise ValidationError(f'Valor "{_texto(valor)}" inválido para "É gestor". Use Sim ou Não.')


def _ler_csv(conteudo):
    # CSV salvo pelo Excel em português costuma vir em Windows-1252; latin-1 nunca falha.
    for codificacao in ('utf-8-sig', 'cp1252', 'latin-1'):
        try:
            texto = conteudo.decode(codificacao)
            break
        except UnicodeDecodeError:
            continue
    try:
        delimitador = csv.Sniffer().sniff(texto[:2048], delimiters=',;\t').delimiter
    except csv.Error:
        delimitador = ','
    return list(csv.reader(io.StringIO(texto, newline=''), delimiter=delimitador))


def _ler_xlsx(conteudo):
    from openpyxl import load_workbook

    try:
        planilha = load_workbook(io.BytesIO(conteudo), read_only=True, data_only=True).worksheets[0]
    except Exception as exc:
        raise ErroImportacao(f'Não foi possível abrir a planilha XLSX: {exc}') from exc
    return [list(linha) for linha in planilha.iter_rows(values_only=True)]


def ler_planilha(conteudo, nome_arquivo):
    """Devolve (cabeçalho normalizado, [(número da linha, {coluna: valor})])."""
    nome = (nome_arquivo or '').lower()
    if not nome.endswith(EXTENSOES):
        raise ErroImportacao('Envie um arquivo .csv ou .xlsx.')
    linhas = _ler_xlsx(conteudo) if nome.endswith('.xlsx') else _ler_csv(conteudo)
    if not linhas:
        raise ErroImportacao('A planilha está vazia.')

    cabecalho = [_normalizar_cabecalho(c) for c in linhas[0]]
    faltando = [c for c in COLUNAS_OBRIGATORIAS if c not in cabecalho]
    if faltando:
        raise ErroImportacao(
            f'Coluna(s) obrigatória(s) ausente(s): {", ".join(faltando)}. '
            f'O cabeçalho deve ter ao menos: {", ".join(COLUNAS_OBRIGATORIAS)}.'
        )

    registros = []
    for numero, linha in enumerate(linhas[1:], start=2):
        if all(_texto(v) == '' for v in linha):
            continue
        registros.append((numero, {c: v for c, v in zip(cabecalho, linha) if c}))
    if not registros:
        raise ErroImportacao('A planilha não tem nenhum colaborador abaixo do cabeçalho.')
    return cabecalho, registros


def _mensagens(exc):
    if hasattr(exc, 'message_dict'):
        return [f'{campo}: {" ".join(msgs)}' if campo != '__all__' else ' '.join(msgs)
                for campo, msgs in exc.message_dict.items()]
    return exc.messages


def _empresa(nome, cache):
    chave = nome.lower()
    if chave not in cache:
        empresa = Empresa.objects.filter(nome__iexact=nome).first()
        if empresa is None:
            empresa = Empresa(nome=nome)
            empresa.full_clean()
            empresa.save()
        cache[chave] = empresa
    return cache[chave]


def _rotulo_linhas(numeros):
    if len(numeros) == 1:
        return f'Linha {numeros[0]}'
    return f'Linhas {", ".join(map(str, numeros[:-1]))} e {numeros[-1]}'


def _agrupar_por_documento(registros):
    """Junta as linhas do mesmo CPF/CNPJ: é a mesma pessoa trabalhando em mais de uma empresa.

    Devolve (grupos, erros); cada grupo é (números das linhas, dados unidos, nomes das empresas).
    Nas colunas opcionais vale o último valor preenchido.
    """
    grupos, erros = {}, []
    for numero, dados in registros:
        nome = _texto(dados.get('nome'))
        documento = _documento(dados.get('documento'))
        nome_empresa = _texto(dados.get('empresa'))
        vazios = [rotulo for rotulo, valor in (('nome', nome), ('documento', documento), ('empresa', nome_empresa)) if not valor]
        if vazios:
            erros.append(f'Linha {numero}: {"; ".join(f"{rotulo} vazio" for rotulo in vazios)}.')
            continue

        numeros, unidos, empresas = grupos.setdefault(documento, ([], {}, []))
        if unidos and _normalizar(unidos['nome']) != _normalizar(nome):
            erros.append(
                f'Linha {numero}: o documento {documento} já aparece na linha {numeros[0]} com outro nome '
                f'("{unidos["nome"]}"). Confira se o CPF/CNPJ está correto.'
            )
            continue
        numeros.append(numero)
        for coluna, valor in dados.items():
            if coluna == 'celular':
                # Celulares de todas as linhas do mesmo documento se somam.
                unidos.setdefault('celular', []).extend(_celulares(valor))
            elif _texto(valor) or isinstance(valor, (date, datetime)):
                unidos[coluna] = valor
        if nome_empresa.lower() not in {e.lower() for e in empresas}:
            empresas.append(nome_empresa)
    return list(grupos.values()), erros


def _importar_colaborador(dados, nomes_empresas, cache_empresas):
    documento = _documento(dados.get('documento'))
    colaborador = Colaborador.objects.filter(documento=documento).first()
    criado = colaborador is None
    if criado:
        colaborador = Colaborador(documento=documento, ativo=True, data_ativacao=timezone.now())
    colaborador.nome = _texto(dados.get('nome'))

    # Em cadastros existentes, célula vazia mantém o valor atual; celulares novos são somados aos atuais.
    celulares = dados.get('celular') or []
    if celulares:
        colaborador.celulares = list(colaborador.celulares) + celulares
    email = _texto(dados.get('email'))
    if email:
        colaborador.email = email
    for campo in ('data_nascimento', 'data_contratacao', 'data_demissao'):
        valor = _data(dados.get(campo))
        if valor:
            setattr(colaborador, campo, valor)
    # Mesma regra do cadastro: com data de demissão até hoje, o colaborador fica inativo; com data
    # futura continua ativo e a rotina de desligamentos o inativa no dia.
    if colaborador.data_demissao and colaborador.ativo and colaborador.data_demissao <= timezone.localdate():
        colaborador.ativo = False
        colaborador.data_inativacao = timezone.now()
    eh_gestor = _booleano(dados.get('eh_gestor'))
    if eh_gestor is not None:
        colaborador.eh_gestor = eh_gestor
    if colaborador.data_nascimento and colaborador.data_nascimento > timezone.localdate():
        raise ValidationError({'data_nascimento': ['a data de nascimento não pode ser futura']})

    colaborador.save()
    # A planilha define as empresas do colaborador: as que não estiverem nela são desvinculadas.
    colaborador.empresas.set([_empresa(nome, cache_empresas) for nome in nomes_empresas])
    return colaborador, criado


def _vincular_gestores(importados):
    """Liga cada colaborador ao gestor informado pelo nome (sem diferenciar maiúsculas, acentos e espaços).

    Mesma regra do dropdown do cadastro: só colaboradores ativos marcados como gestores.
    Célula vazia mantém o gestor atual.
    """
    gestores = {}
    for gestor in Colaborador.objects.filter(eh_gestor=True, ativo=True):
        gestores.setdefault(_normalizar(gestor.nome), []).append(gestor)

    erros = []
    for rotulo, colaborador, nome_gestor in importados:
        if not nome_gestor:
            continue
        candidatos = [g for g in gestores.get(_normalizar(nome_gestor), []) if g.pk != colaborador.pk]
        if len(candidatos) == 1:
            colaborador.gestor = candidatos[0]
            colaborador.save()
        elif candidatos:
            erros.append(f'{rotulo}: há {len(candidatos)} gestores chamados "{nome_gestor}"; '
                         'defina o gestor deste colaborador pelo cadastro.')
        elif _normalizar(nome_gestor) == _normalizar(colaborador.nome):
            erros.append(f'{rotulo}: o colaborador não pode ser gestor de si mesmo.')
        elif Colaborador.objects.filter(nome__iexact=nome_gestor).exists():
            erros.append(f'{rotulo}: "{nome_gestor}" não está marcado como gestor ativo '
                         '(marque "Sim" na coluna eh_gestor ou no cadastro dele).')
        else:
            erros.append(f'{rotulo}: nenhum gestor chamado "{nome_gestor}" foi encontrado.')

    # Feito por último para considerar as trocas de gestor da própria planilha.
    for rotulo, colaborador, _ in importados:
        if not colaborador.eh_gestor and colaborador.liderados.exists():
            erros.append(f'{rotulo}: é gestor responsável de outros colaboradores; não pode deixar de ser gestor.')
    return erros


def _valor_celula(coluna, valor):
    """Converte a célula da planilha no texto exibido (e editável) na revisão."""
    if valor is None:
        return ''
    if isinstance(valor, datetime):
        valor = valor.date()
    if isinstance(valor, date):
        return valor.strftime('%d/%m/%Y')
    if isinstance(valor, bool):
        return 'Sim' if valor else 'Não'
    if coluna == 'documento' and isinstance(valor, (int, float)):
        return _documento(valor)
    if coluna == 'celular':
        return ' / '.join(_celulares(valor))
    return _texto(valor)


def preparar_linhas(conteudo, nome_arquivo):
    """Lê a planilha (bytes) sem gravar nada e devolve (linhas, colunas ignoradas).

    Cada linha é {'numero': nº da linha na planilha, 'valores': {coluna: texto}}, sempre com todas as COLUNAS.
    """
    if len(conteudo) > TAMANHO_MAXIMO:
        raise ErroImportacao('O arquivo passa de 5 MB.')
    cabecalho, registros = ler_planilha(conteudo, nome_arquivo)
    linhas = [
        {'numero': numero, 'valores': {coluna: _valor_celula(coluna, dados.get(coluna)) for coluna in COLUNAS}}
        for numero, dados in registros
    ]
    return linhas, [c for c in cabecalho if c and c not in COLUNAS]


def _erro_da_celula(coluna, valor, hoje):
    """Valida uma célula isoladamente; devolve a mensagem de erro ou None."""
    if not valor:
        return 'Preenchimento obrigatório.' if coluna in COLUNAS_OBRIGATORIAS else None
    try:
        if coluna == 'documento':
            validar_documento(_documento(valor))
        elif coluna in ('nome', 'empresa') and len(valor) > 200:
            return 'Use no máximo 200 caracteres.'
        elif coluna == 'celular':
            validar_celulares(_celulares(valor))
        elif coluna == 'email':
            if len(valor) > 254:
                return 'Use no máximo 254 caracteres.'
            EmailValidator('Informe um e-mail válido, como nome@exemplo.com.br.')(valor.strip().lower())
        elif coluna.startswith('data_'):
            if _data(valor) > hoje and coluna == 'data_nascimento':
                return 'A data de nascimento não pode ser futura.'
        elif coluna == 'eh_gestor':
            _booleano(valor)
    except ValidationError as exc:
        return ' '.join(exc.messages)
    return None


def _unir(grupo):
    """Dados do colaborador vindos das linhas do mesmo CPF/CNPJ: vale o último valor preenchido."""
    unidos = {}
    for linha in grupo:
        unidos.update({coluna: valor for coluna, valor in linha['valores'].items() if valor})
    return unidos


def _grupo_valido(grupo, erros):
    return not any(erros.get(linha['numero']) for linha in grupo)


def _erros_entre_linhas_do_documento(documento, grupo, atual, erros):
    """Mesmo CPF/CNPJ com outro nome e datas de contratação/demissão incoerentes depois de unir as linhas."""
    referencia = next((linha for linha in grupo if linha['valores']['nome']), None)
    for linha in grupo:
        nome = linha['valores']['nome']
        if referencia and linha is not referencia and nome and _normalizar(nome) != _normalizar(referencia['valores']['nome']):
            erros[linha['numero']].setdefault('nome', (
                f'O CPF/CNPJ {documento} já aparece na linha {referencia["numero"]} com outro nome '
                f'("{referencia["valores"]["nome"]}"). Confira o nome ou o documento.'
            ))

    datas = {}
    for coluna in ('data_contratacao', 'data_demissao'):
        preenchidas = [l for l in grupo if l['valores'][coluna] and coluna not in erros[l['numero']]]
        datas[coluna] = _data(preenchidas[-1]['valores'][coluna]) if preenchidas else atual.get(coluna)
    contratacao, demissao = datas['data_contratacao'], datas['data_demissao']
    if contratacao and demissao and demissao < contratacao:
        coluna = 'data_demissao' if any(l['valores']['data_demissao'] for l in grupo) else 'data_contratacao'
        mensagem = (f'A data de demissão não pode ser anterior à contratação ({contratacao:%d/%m/%Y}).'
                    if coluna == 'data_demissao' else
                    f'A data de contratação não pode ser posterior à demissão ({demissao:%d/%m/%Y}).')
        for linha in grupo:
            if linha['valores'][coluna]:
                erros[linha['numero']].setdefault(coluna, mensagem)


def _segurar_grupos_com_erro(grupos, erros):
    """As linhas de um CPF/CNPJ são gravadas juntas (definem as empresas dele): se uma tem erro, as outras esperam."""
    mudou = False
    for grupo in grupos.values():
        com_erro = [linha['numero'] for linha in grupo if erros.get(linha['numero'])]
        if not com_erro:
            continue
        for linha in grupo:
            if not erros.get(linha['numero']):
                erros[linha['numero']][LINHA] = (
                    f'Este CPF/CNPJ tem erro em outra linha ({", ".join(map(str, com_erro))}). '
                    'Corrija ou descarte essa linha para importar este colaborador.'
                )
                mudou = True
    return mudou


def _erros_de_gestor(linhas, grupos, erros, cadastro, hoje):
    """Simula o cadastro depois de gravar as linhas válidas e confere os gestores informados pelo nome.

    Mesmas regras de `_vincular_gestores`: o gestor precisa ser um colaborador ativo marcado como gestor,
    e quem tem liderados não pode deixar de ser gestor. Devolve {número da linha: (coluna, mensagem)}.
    """
    validos = {documento: _unir(grupo) for documento, grupo in grupos.items() if _grupo_valido(grupo, erros)}
    estado = {documento: {'nome': c['nome'], 'eh_gestor': c['eh_gestor'], 'ativo': c['ativo']}
              for documento, c in cadastro.items()}
    for documento, dados in validos.items():
        info = estado.setdefault(documento, {'eh_gestor': False, 'ativo': True})
        info['nome'] = dados['nome']
        if dados.get('eh_gestor'):
            info['eh_gestor'] = _booleano(dados['eh_gestor'])
        demissao = _data(dados.get('data_demissao'))
        if demissao and demissao <= hoje:
            info['ativo'] = False

    gestores, nomes = defaultdict(list), set()
    for documento, info in estado.items():
        nomes.add(_normalizar(info['nome']))
        if info['eh_gestor'] and info['ativo']:
            gestores[_normalizar(info['nome'])].append(documento)
    pendentes = {}
    for linha in linhas:
        if erros.get(linha['numero']) and linha['valores']['nome']:
            pendentes.setdefault(_normalizar(linha['valores']['nome']), linha['numero'])

    novos, reatribuidos = {}, set()
    for documento, dados in validos.items():
        nome_gestor = dados.get('gestor')
        if not nome_gestor:
            continue
        chave = _normalizar(nome_gestor)
        candidatos = [d for d in gestores.get(chave, []) if d != documento]
        if len(candidatos) == 1:
            reatribuidos.add(documento)
            continue
        if candidatos:
            mensagem = f'Há {len(candidatos)} gestores chamados "{nome_gestor}"; defina o gestor deste colaborador pelo cadastro.'
        elif chave == _normalizar(dados['nome']):
            mensagem = 'O colaborador não pode ser gestor de si mesmo.'
        elif chave in pendentes:
            mensagem = f'O gestor "{nome_gestor}" está na linha {pendentes[chave]}, que tem erro. Corrija essa linha primeiro.'
        elif chave in nomes:
            mensagem = (f'"{nome_gestor}" não está marcado como gestor ativo '
                        '(marque "Sim" na coluna É gestor ou no cadastro dele).')
        else:
            mensagem = f'Nenhum gestor chamado "{nome_gestor}" foi encontrado.'
        for linha in grupos[documento]:
            if linha['valores']['gestor']:
                novos[linha['numero']] = ('gestor', mensagem)

    liderados = defaultdict(set)
    for documento, c in cadastro.items():
        if c['gestor__documento']:
            liderados[c['gestor__documento']].add(documento)
    for documento in validos:
        if estado[documento]['eh_gestor'] or not (liderados[documento] - reatribuidos):
            continue
        marcadas = [linha for linha in grupos[documento] if linha['valores']['eh_gestor']]
        for linha in marcadas or grupos[documento]:
            novos[linha['numero']] = ('eh_gestor' if marcadas else LINHA,
                                      'É gestor responsável de outros colaboradores; não pode deixar de ser gestor.')
    return novos


def validar_linhas(linhas):
    """Devolve {número da linha: {coluna: mensagem}} só com as linhas que têm erro.

    Além de cada célula, confere as regras entre linhas (mesmo CPF/CNPJ com outro nome, gestor informado
    pelo nome) e o que já está cadastrado. Não grava nada; é rápida o bastante para rodar a cada edição.
    """
    hoje = timezone.localdate()
    erros = defaultdict(dict)
    for linha in linhas:
        for coluna in COLUNAS:
            mensagem = _erro_da_celula(coluna, linha['valores'].get(coluna, ''), hoje)
            if mensagem:
                erros[linha['numero']][coluna] = mensagem

    grupos = defaultdict(list)
    for linha in linhas:
        if 'documento' not in erros[linha['numero']]:
            grupos[_documento(linha['valores']['documento'])].append(linha)

    cadastro = {
        c['documento']: c for c in Colaborador.objects.values(
            'documento', 'nome', 'eh_gestor', 'ativo', 'data_contratacao', 'data_demissao', 'gestor__documento',
        )
    }
    for documento, grupo in grupos.items():
        _erros_entre_linhas_do_documento(documento, grupo, cadastro.get(documento, {}), erros)

    # Cada linha que ganha erro pode invalidar outras (mesmo CPF, ou gestor dela): repete até estabilizar.
    while True:
        mudou = _segurar_grupos_com_erro(grupos, erros)
        for numero, (coluna, mensagem) in _erros_de_gestor(linhas, grupos, erros, cadastro, hoje).items():
            if coluna not in erros[numero]:
                erros[numero][coluna] = mensagem
                mudou = True
        if not mudou:
            break
    return {numero: erros_da_linha for numero, erros_da_linha in erros.items() if erros_da_linha}


def descrever_erros(numero, erros_da_linha):
    """Erros de uma linha em uma frase, ex.: "Linha 3: CPF/CNPJ: Preenchimento obrigatório."."""
    ordem = (LINHA,) + COLUNAS
    partes = [mensagem if coluna == LINHA else f'{ROTULOS[coluna]}: {mensagem}'
              for coluna, mensagem in sorted(erros_da_linha.items(), key=lambda item: ordem.index(item[0]))]
    return f'Linha {numero}: {" ".join(partes)}'


def gravar_linhas(linhas, erros=None):
    """Grava só as linhas sem erro e devolve um ResultadoImportacao.

    Revalida se `erros` não for informado. Tudo numa transação: se a gravação esbarrar em algo que a
    validação não previu, nada é gravado e ErroImportacao lista os problemas.
    """
    if erros is None:
        erros = validar_linhas(linhas)
    validas = [(linha['numero'], linha['valores']) for linha in linhas if linha['numero'] not in erros]
    resultado = ResultadoImportacao(ignoradas=len(linhas) - len(validas))

    grupos, problemas = _agrupar_por_documento(validas)
    cache_empresas, importados = {}, []
    with transaction.atomic():
        for numeros, dados, nomes_empresas in grupos:
            rotulo = _rotulo_linhas(numeros)
            try:
                with transaction.atomic():
                    colaborador, criado = _importar_colaborador(dados, nomes_empresas, cache_empresas)
            except ValidationError as exc:
                problemas.append(f'{rotulo}: {"; ".join(_mensagens(exc))}.')
                continue
            resultado.criados += int(criado)
            resultado.atualizados += int(not criado)
            importados.append((rotulo, colaborador, _texto(dados.get('gestor'))))

        # Só depois de gravar todas as linhas: o gestor pode ter sido marcado mais abaixo na planilha.
        problemas += _vincular_gestores(importados)

        if problemas:
            transaction.set_rollback(True)
    if problemas:
        # Ordena pelo número da primeira linha citada em cada erro.
        problemas.sort(key=lambda e: int(e.split(':')[0].split()[1].rstrip(',')))
        raise ErroImportacao(problemas)
    return resultado


def importar_colaboradores(conteudo, nome_arquivo):
    """Importação tudo ou nada (usada pelo comando): com qualquer linha com erro, nada é gravado."""
    linhas, colunas_ignoradas = preparar_linhas(conteudo, nome_arquivo)
    erros = validar_linhas(linhas)
    if erros:
        raise ErroImportacao([descrever_erros(numero, erros[numero]) for numero in sorted(erros)])
    resultado = gravar_linhas(linhas, erros)
    resultado.colunas_ignoradas = colunas_ignoradas
    return resultado
