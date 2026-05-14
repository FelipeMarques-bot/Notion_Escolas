# Notion_Escolas

Automacao para criar estrutura no Notion e lancar notas no SGE via GitHub Actions, sem dependencia de N8N.

## Arquivos principais

- notion_lancamento.py: cria paginas e databases no Notion para escolas, turnos, turmas e trimestres.
- lancar_notas_sge.py: le notas do Notion e tenta lancar no SGE via Playwright.
- painel.py: interface Streamlit para executar o lancamento localmente.
- .github/workflows/lancar-notas-sge.yml: workflow principal para lancamento manual no GitHub Actions.
- .github/workflows/processar-solicitacoes-sge.yml: processa automaticamente solicitacoes marcadas no Notion.
- .github/workflows/solicitar-lancamento-issue.yml: workflow de solicitacao via Issue (segundo botao).
- .github/ISSUE_TEMPLATE/solicitar-lancamento-sge.yml: formulario para solicitar lancamento.

## URL do SGE

Padrao do projeto:

- https://www.sge8147.com.br/

Se precisar trocar, configure a secret/variavel SGE_LOGIN_URL.

## Secrets obrigatorios no GitHub

Em Settings > Secrets and variables > Actions:

- NOTION_TOKEN
- ROOT_PAGE_ID
- SGE_CPF
- SGE_SENHA
- SGE_LOGIN_URL (opcional)

## Instalacao local (opcional)

```bash
pip install -r requirements.txt
playwright install chromium
```

## Execucao local (opcional)

```bash
python lancar_notas_sge.py --dry-run
streamlit run painel.py
```

## Execucao remota no GitHub

Ha duas formas:

1. Manual por `Run workflow`.
2. Automatica por solicitacoes marcadas no Notion (agendamento a cada 10 minutos).

### Botao 1: Run workflow (Actions)

1. Abra o repositorio no GitHub.
2. Acesse Actions.
3. Selecione Lancar Notas SGE.
4. Clique em Run workflow.
5. Preencha filtros opcionais e execute.

### Botao 2: Solicitar por Issue

1. Abra o repositorio no GitHub.
2. Acesse Issues > New issue.
3. Escolha o formulario Solicitar lancamento SGE.
4. Preencha os campos e clique em Submit new issue.
5. A issue dispara o workflow automaticamente.

### Processamento automatico por solicitacao no Notion

1. Em cada database `Solicitacoes SGE - <Escola>`, marque `Solicitar lancamento`.
2. O workflow `Processar Solicitacoes SGE` verifica pendencias no cron.
3. Se houver item pendente, executa o lancamento e atualiza status/log na propria linha.
4. Para teste, rode manualmente com `dry_run=true`.

### Processamento manual por escola (sem checkbox)

1. Acesse Actions > `Processar Solicitacoes SGE` > Run workflow.
2. Preencha `escola` com o nome exato (ex.: `Juvenal`).
3. Use `dry_run=true` para validar sem enviar ao SGE.
4. Execute o workflow (neste modo, nao depende da tabela `Solicitacoes SGE`).

## Observacoes importantes

- Os nomes das colunas de avaliacao no Notion devem bater com os nomes das avaliacoes no SGE.
- Use dry-run antes do primeiro envio real.