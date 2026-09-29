# Sistema de pesquisas eNPS

Aplicação Django com PostgreSQL para cadastro de colaboradores, pesquisas com formulário dinâmico, resposta externa por CPF e relatórios eNPS. O CPF é usado apenas durante a validação; a participação é persistida como HMAC-SHA-256 e existe uma restrição única no banco para impedir respostas duplicadas.

## Preparação

1. Ative a venv e instale as dependências: `pip install -r requirements.txt`.
2. Crie o banco e o usuário no PostgreSQL.
3. Preencha `.env`. Em produção, troque `SECRET_KEY` e `SECRET_SALT` por segredos longos e independentes, defina `DEBUG=False` e informe `ALLOWED_HOSTS`.
4. Não altere `SECRET_SALT` após receber respostas: a troca muda os hashes e invalida a detecção de duplicidade.
5. Execute `python manage.py migrate` e `python manage.py createsuperuser`.
6. Inicie localmente com `python manage.py runserver`.

O carregamento em `enps/settings.py` usa `config(...)` da biblioteca `python-decouple`. O banco padrão é sempre PostgreSQL; SQLite é usado apenas no arquivo isolado de configuração de testes.

## Active Directory

A autenticação é híbrida: usuários com origem `LOCAL` usam a senha da aplicação; usuários com origem `DIRECTORY` autenticam com a senha de rede via LDAP. O AD não cria usuários, não sincroniza atributos e não fornece permissões.

1. Instale as dependências com `pip install -r requirements.txt`.
2. Preencha no `.env` somente `LDAP_SERVER`, `LDAP_DOMAIN`, `LDAP_BASE_DN`, `LDAP_BIND_USER` e `LDAP_BIND_PASSWORD`. `LDAP_SERVER` pode ser `ldap://host:389` (StartTLS automático) ou `ldaps://host:636`.
3. Execute `python manage.py migrate` e, no admin, crie/altere o usuário para origem `Active Directory`. Usuários existentes permanecem `LOCAL` pela migração.

No primeiro login AD bem-sucedido, o `objectGUID` é gravado; divergências bloqueiam o login e exigem a ação administrativa “Religar identidade AD”. Desabilitar a conta no AD impede novos logins, enquanto desativar o cadastro local continua sendo uma ação manual. Usuários AD não podem trocar ou recuperar senha localmente. A sessão dura no máximo 8 horas e expira ao fechar o navegador.

Se o DC ficar indisponível, usuários AD recebem uma mensagem específica sem incremento de tentativas; usuários locais continuam entrando. Para produção, prefira `ldaps://`, pois LDAP puro transmite as credenciais sem criptografia. Mantenha uma conta local de emergência, com senha definida fora do código, para contingência operacional.

## CSV

O arquivo deve ser UTF-8 e conter exatamente:

```csv
nome,cpf,empresa
Ana Silva,52998224725,Filial São Paulo
```

Importe com `python manage.py importar_colaboradores Colaboradores.csv`. Empresas são criadas automaticamente, sem duplicidade de nomes (inclusive com diferenças apenas entre maiúsculas e minúsculas). Registros existentes são atualizados, vinculados à empresa informada e reativados; qualquer linha inválida cancela toda a importação.

## Banco de Talentos (currículos, IA e pgvector)

**Perfis de acesso.** Cada usuário tem um perfil (`Administrador total`, `Gestor / RH geral` ou `Usuário específico` com lista de módulos). O Administrador define o perfil no cadastro de colaboradores ou em `/admin/`. Ver `pesquisas/permissoes.py`.

**pgvector.** O banco precisa da extensão `vector`. No Docker, o serviço `db` usa a imagem `pgvector/pgvector:pg16` (mesma major do `postgres:16`, os dados existentes continuam válidos). A migration `talentos/0001` executa `CREATE EXTENSION IF NOT EXISTS vector`. Em banco gerenciado/sem superusuário, peça a um DBA `CREATE EXTENSION vector;` e valide com `python manage.py verificar_pgvector`.

**Pipeline** (upload síncrono, até `CURRICULO_MAX_ARQUIVOS` PDFs por envio): `pypdf` extrai o texto → OpenAI GPT (`CURRICULO_LLM_MODEL`, saída estruturada por JSON Schema) extrai nome, contato, cidade/bairro, experiências e qualificações → GPT sugere o cargo mais compatível na árvore `organizacao` → embedding gravado em `vector(EMBEDDING_DIMENSIONS)`. Falhas ficam registradas no candidato e podem ser reprocessadas.

**Variáveis** (ver `.env.example`): `OPENAI_API_KEY`; `EMBEDDING_API_URL`/`EMBEDDING_API_KEY`/`EMBEDDING_MODEL`/`EMBEDDING_DIMENSIONS` (alterar as dimensões exige nova migration e reprocessar os currículos).

**Privacidade (LGPD).** Os PDFs ficam em `PRIVATE_MEDIA_ROOT` (fora de `/media/`, que o nginx serve) e só são entregues por view autenticada. O texto do currículo é enviado aos provedores de LLM/embeddings configurados: confirme base legal/contrato com eles. A exclusão do candidato remove também o PDF.

## Regras relevantes

- Somente usuários com acesso ao painel (`is_staff`, derivado do perfil) entram na gestão; cada módulo exige a permissão correspondente. O `/admin/` é restrito ao perfil Administrador.
- Uma nova pesquisa exige pelo menos três perguntas e data inicial futura. A partir do início, o modelo, as views e o admin bloqueiam alterações.
- O relatório usa a primeira pergunta `NOTA_0_10`, ordenada por `ordem`, como pergunta eNPS.
- Como o cadastro solicitado possui apenas o estado atual `ativo`, o KPI de colaboradores ativos reflete o cadastro no momento da consulta do relatório.

## Testes

Execute `python manage.py test --settings=enps.settings_test`.
