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

- https://www.sge8147.com.br/hportalprofessor.aspx

Se precisar trocar, configure a secret/variavel SGE_LOGIN_URL.

## Secrets obrigatorios no GitHub

Em Settings > Secrets and variables > Actions:

- NOTION_TOKEN
- ROOT_PAGE_ID
- SGE_CPF
- SGE_SENHA
- SGE_LOGIN_URL (opcional)

### Secret adicional para Sequencia Didatica

O workflow `Plano de Aula - Sequencia Didatica` aceita uma secret extra:

- SEQUENCIAS_DATABASE_ID (recomendado)

Exemplo de valor: `383db7e871644613804539dcb69f6a1a`

Quando definida, o script le a database `Sequencias Didaticas - PDFs`
diretamente por ID, sem depender de permissao na pagina raiz apontada
por ROOT_PAGE_ID. Sem essa secret, o script cai na descoberta recursiva
(mais lenta e exige acesso de leitura a paginas ancestrais).

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

### Login manual local (alternativa)

Quando o login no ambiente remoto falhar, rode localmente com navegador visivel:

```bash
HEADLESS=0 MANUAL_LOGIN=1 python lancar_notas_sge.py --escola "Tancredo" --turno "Matutino" --turma "6º Ano" --trimestre "2º Trimestre"
```

O script aguarda voce autenticar manualmente no SGE e continua automaticamente apos detectar que saiu da tela de login.

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

## Sequencia Didatica (Plano de Aula)

O workflow `Plano de Aula - Sequencia Didatica` le registros da database
`Sequencias Didaticas - PDFs` e cria planejamentos, anexa PDFs e ativa
situacoes no SGE para cada turma.

### Fluxo de execucao

1. Carrega registros da database do Notion.
2. Filtra por ano (`--ano`) e escola (`--escola`).
3. Para cada contexto (escola + turno + turma + trimestre), faz:
   - **Match de template**: encontra o registro de sequencia correspondente
     (por ano, escola e nome de arquivo/titulo).
   - **Cria planejamento**: preenche periodo e numero de aulas no SGE.
   - **Anexa PDF**: faz upload do arquivo da sequencia didatica.
   - **Ativa situacao**: marca a situacao da linha como concluida.

### Execucao local

```bash
# Validar sem enviar ao SGE
python lancar_sequencia_didatica_sge.py --dry-run --data-inicio 2025-02-01 --data-fim 2025-02-28

# Executar para todos os contextos
python lancar_sequencia_didatica_sge.py --data-inicio 2025-02-01 --data-fim 2025-02-28

# Filtrar por escola e ano
python lancar_sequencia_didatica_sge.py --escola "Tancredo" --ano "6º Ano" --data-inicio 2025-02-01 --data-fim 2025-02-28

# Especificar arquivos PDF por ano
python lancar_sequencia_didatica_sge.py --arquivo-6-ano "seq_6.pdf" --arquivo-7-ano "seq_7.pdf" --data-inicio 2025-02-01 --data-fim 2025-02-28
```

### Match de template (sequencia)

O script encontra o template correto seguindo esta ordem:

1. **Por ano**: filtra registros cujo ano corresponde ao da turma.
2. **Por escola**: prioriza registros com a mesma escola do contexto.
   Se nenhum bater, usa registros sem escola preenchida.
3. **Por arquivo**: se um nome de arquivo foi informado via CLI
   (`--arquivo-6-ano`, etc.), tenta match tolerante:
   - match exato apos normalizar (sem acentos, lowercase)
   - `startswith` em qualquer direcao
   - ignorar sufixo " Ano.pdf" / ".pdf"
4. **Fallback por titulo**: se o nome do arquivo nao casar, tenta
   match pelo titulo do documento no Notion.
5. **Ultimo recurso**: usa o primeiro registro restante.

Se nenhum template for encontrado para uma turma, ela e pulada com
falha registrada no resumo final.

### Troubleshooting de match

- **"Nenhum template de sequencia encontrado"**: verifique se a database
  `Sequencias Didaticas - PDFs` tem registros ativos para o ano da turma.
- **Logs de diagnostico**: o script imprime logs com prefixo `[diag]`
  mostrando quantos candidatos foram encontrados em cada etapa do match.
- **Anos disponiveis**: a database lista os anos encontrados no inicio
  da execucao. Use `--ano` para filtrar.

## Troubleshooting de login SGE

### Erros comuns

| Erro | Causa provavel | Acao |
|------|---------------|------|
| `Nao foi possivel localizar os campos de login` | URL errada ou formulario mudou | Verificar `SGE_LOGIN_URL`; rodar localmente com `HEADLESS=0` |
| `Falha no login do SGE: senha inval` | Credencial errada ou senha com maiuscula/minuscula | Verificar `SGE_CPF` e `SGE_SENHA` |
| `Nao foi possivel encontrar formulario de login apos N fallbacks` | Portal SGE indisponivel ou URL desatualizada | Acessar manualmente o portal; atualizar `SGE_LOGIN_URL` |
| `Timeout aguardando login manual` | Usuario nao completou login a tempo | Aumentar `MANUAL_LOGIN_TIMEOUT_SEC` |

### Debug local

```bash
# Login manual com navegador visivel
HEADLESS=0 MANUAL_LOGIN=1 python lancar_notas_sge.py --dry-run --escola "Tancredo" --turno "Matutino" --turma "6º Ano"

# Capturar screenshot/HTML do estado de login
SGE_DEBUG_LOGIN=1 SGE_DEBUG_DIR=./debug-login python lancar_sequencia_didatica_sge.py --dry-run --data-inicio 2025-02-01 --data-fim 2025-02-28
```

Os artifacts de debug sao salvos em `SGE_DEBUG_DIR` (padrao: `artifacts/sge-login/`)
quando executado no GitHub Actions, ou no diretorio especificado localmente.

### Mecanismo de retry

O login tem ate 2 tentativas automaticas:
1. Se a URL falhar (formulario nao encontrado), recarrega a pagina e tenta de novo.
2. Se a senha for rejeitada, tenta com a senha em maiusculas (o SGE normaliza para maiusculas).
3. Erros de credencial invalida nao sao retentados.

## Checklist de secrets

Secrets obrigatorias no GitHub (Settings > Secrets and variables > Actions):

| Secret | Obrigatoria | Descricao |
|--------|-------------|-----------|
| `NOTION_TOKEN` | Sim | Token de integracao do Notion |
| `ROOT_PAGE_ID` | Sim | ID da pagina raiz do Notion |
| `SGE_CPF` | Sim | CPF de acesso ao SGE |
| `SGE_SENHA` | Sim | Senha de acesso ao SGE |
| `SGE_LOGIN_URL` | Nao | URL personalizada do portal SGE |
| `SEQUENCIAS_DATABASE_ID` | Nao | ID da database de sequencias (acesso direto) |

## Fluxo recomendado

Sempre siga esta ordem antes de uma execucao real:

1. **Dry-run local**: `python lancar_sequencia_didatica_sge.py --dry-run --data-inicio ... --data-fim ...`
2. **Dry-run no Actions**: execute o workflow com `dry_run=true`.
3. **Verificar logs**: confira se todos os contextos foram processados sem erros.
4. **Executar real**: rode com `dry_run=false` (ou omita `--dry-run` localmente).

## Observacoes importantes

- Os nomes das colunas de avaliacao no Notion devem bater com os nomes das avaliacoes no SGE.
- Use dry-run antes do primeiro envio real.