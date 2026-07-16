# Runbook SGE — Notion_Escolas

Playbook de incidentes e procedimentos operacionais para o ambiente SGE.

## Indice

1. [Erro: nome 'logger' is not defined](#erro-name-logger-is-not-defined)
2. [Erro: Nao foi possivel localizar campos de login](#erro-nao-foi-possivel-localizar-campos-de-login)
3. [Erro: Nao foi possivel encontrar formulario de login apos N fallbacks](#erro-nao-foi-possivel-encontrar-formulario-de-login-apos-n-fallbacks)
4. [Erro: Falha no login do SGE — senha inval](#erro-falha-no-login-do-sge--senha-inval)
5. [Erro: Nenhum template de sequencia encontrado](#erro-nenhum-template-de-sequencia-encontrado)
6. [Erro: PlaywrightTimeoutError no processamento de turma](#erro-playwrighttimeouterror-no-processamento-de-turma)
7. [Comandos de debug local](#comandos-de-debug-local)
8. [Estrutura de arquivos](#estrutura-de-arquivos)

---

## Erro: `name 'logger' is not defined`

### Sintomas

O workflow `Plano de Aula - Sequencia Didatica` falha com `NameError: name 'logger' is not defined`.

### Causa

A funcao `_pick_template_for_context` em `lancar_sequencia_didatica_sge.py` nao recebia
`logger` como parametro, mas usava `_log(logger, ...)` internamente.

### Solucao

Ja corrigido na Sprint 1. Verifique se o branch `sprint/01-hotfix-logger` foi mergeado.

### Prevencao

Sempre que adicionar `_log(logger, ...)` em uma funcao, garantir que `logger` esteja
na assinatura da funcao.

---

## Erro: Nao foi possivel localizar campos de login

### Sintomas

```
LancamentoError: Nao foi possivel localizar os campos de login no SGE. URL atual: ...
```

### Causas possiveis

1. URL de login desatualizada ou incorreta.
2. O formulario de login mudou (novo layout, iframe diferente).
3. O portal SGE esta fora do ar.

### Diagnosticos

1. Verificar o valor de `SGE_LOGIN_URL` no GitHub Secrets.
2. Rodar localmente com `HEADLESS=0` para ver o navegador:
   ```bash
   HEADLESS=0 SGE_DEBUG_LOGIN=1 python lancar_sequencia_didatica_sge.py --dry-run --data-inicio 2025-02-01 --data-fim 2025-02-28
   ```
3. Verificar os artifacts de debug salvos em `SGE_DEBUG_DIR` (screenshot, HTML, info).

### Solucao

- Se a URL estiver errada: atualizar `SGE_LOGIN_URL` no GitHub Secrets.
- Se o layout mudou: atualizar os seletores em `_pick_user_input` e `_find_login_inputs` em `lancar_notas_sge.py`.
- Se o portal estiver fora: aguardar e tentar novamente (o mecanismo de retry faz 2 tentativas automaticas).

---

## Erro: Nao foi possivel encontrar formulario de login apos N fallbacks

### Sintomas

```
LancamentoError: Nao foi possivel encontrar formulario de login apos 3 fallbacks. URL atual: ...
```

### Causas possiveis

1. O portal SGE mudou de endereco.
2. A URL base (`SGE_LOGIN_URL`) esta correta, mas as paginas de fallback estao desatualizadas.

### Solucao

1. Verificar manualmente qual URL exibe o formulario de login.
2. Atualizar `PORTAL_LOGIN_FALLBACK_URLS` em `lancar_notas_sge.py`.
3. Atualizar `DEFAULT_SGE_LOGIN_URL` se necessario.

---

## Erro: Falha no login do SGE — senha inval

### Sintomas

```
LancamentoError: Falha no login do SGE: senha invalida (ou similar)
```

### Causas

1. `SGE_CPF` ou `SGE_SENHA` incorretos.
2. A senha contem caracteres especiais que precisam de escape.
3. O CPF tem formatacao diferente da esperada (o script normaliza para 11 digitos).

### Solucao

1. Verificar `SGE_CPF` e `SGE_SENHA` no GitHub Secrets.
2. Testar as credenciais manualmente no portal SGE.
3. Rodar localmente com `HEADLESS=0 MANUAL_LOGIN=1` para fazer login manual e confirmar que o restante do fluxo funciona.

---

## Erro: Nenhum template de sequencia encontrado

### Sintomas

```
Aviso: nenhum template de sequencia encontrado para 6º Ano (Escola X)
```

### Causas

1. A database `Sequencias Didaticas - PDFs` nao tem registros para o ano da turma.
2. O filtro `--ano` esta excluindo registros.
3. O campo `Ano` no Notion nao corresponde ao formato esperado (ex.: "6º Ano").
4. A linha nao esta com checkbox `Ativo` marcado.
5. O Name informado no workflow/CLI nao corresponde ao campo Name da linha.

### Diagnosticos

Verificar os logs com prefixo `[diag]`:
- `Anos com template de sequencia: [...]` — lista os anos disponiveis.
- `Apos filtro por ano ('6º Ano'): 0 candidato(s)` — nenhum registro para o ano.
- `Escolas disponiveis nos candidatos: [...]` — mostra as escolas cadastradas.

### Solucao

1. Verificar se a database tem registros ativos (campo `Ativo` marcado).
2. Verificar se o campo `Ano` esta preenchido corretamente (ex.: "6º Ano").
3. Se necessario, usar `--ano` para filtrar explicitamente.
4. Se usar Name por ano no workflow/CLI, validar o texto exato do campo Name.

---

## Erro: PlaywrightTimeoutError no processamento de turma

### Sintomas

```
Falha por timeout em Escola X | Matutino | 6º Ano | 2o Trimestre: Timeout ...
```

### Causas

1. A pagina do SGE esta lenta (rede, servidor).
2. Um elemento esperado nao apareceu no tempo limite.
3. Sessao expirada durante o processamento.

### Solucao

1. O script ja captura a falha e continua para a proxima turma.
2. Aumentar timeouts via variaveis de ambiente:
   ```bash
   NAV_TIMEOUT_MS=60000 ACTION_TIMEOUT_MS=15000 python lancar_sequencia_didatica_sge.py ...
   ```
3. Verificar o resumo final para ver quantas turmas falharam e o motivo.
4. Rodar novamente apenas para as turmas que falharam.

---

## Comandos de debug local

### Sequencia Didatica

```bash
# Dry-run com logs diagnosticos
python lancar_sequencia_didatica_sge.py --dry-run --data-inicio 2025-02-01 --data-fim 2025-02-28

# Com filtro de escola e ano
python lancar_sequencia_didatica_sge.py --escola "Tancredo" --ano "6º Ano" --dry-run --data-inicio 2025-02-01 --data-fim 2025-02-28

# Com Name por ano (campo Name no Notion / titulo no portal)
python lancar_sequencia_didatica_sge.py --name-6-ano "Sequencia didatica 6" --name-7-ano "Sequencia didatica 7" --dry-run --data-inicio 2025-02-01 --data-fim 2025-02-28

# Com debug de login (screenshot + HTML)
SGE_DEBUG_LOGIN=1 SGE_DEBUG_DIR=./debug python lancar_sequencia_didatica_sge.py --dry-run --data-inicio 2025-02-01 --data-fim 2025-02-28

# Login manual com navegador visivel
HEADLESS=0 MANUAL_LOGIN=1 python lancar_sequencia_didatica_sge.py --dry-run --data-inicio 2025-02-01 --data-fim 2025-02-28
```

### Status de publicacao no Notion

Durante e ao final do processamento, o bot atualiza a coluna
`Status publicação plano SGE` com os estados:

- `Em execução`
- `Publicado no SGE`
- `Simulado (dry run)`
- `Erro na publicação`

### Lancamento de notas

```bash
# Dry-run
python lancar_notas_sge.py --dry-run

# Com filtros
python lancar_notas_sge.py --escola "Tancredo" --turno "Matutino" --turma "6º Ano" --trimestre "2º Trimestre"

# Login manual
HEADLESS=0 MANUAL_LOGIN=1 python lancar_notas_sge.py --dry-run --escola "Tancredo" --turno "Matutino" --turma "6º Ano" --trimestre "2º Trimestre"
```

### Variaveis de ambiente uteis

| Variavel | Default | Descricao |
|----------|---------|-----------|
| `HEADLESS` | `1` | `0` para ver o navegador |
| `NAV_TIMEOUT_MS` | `35000` | Timeout de navegacao (ms) |
| `ACTION_TIMEOUT_MS` | `9000` | Timeout de acoes (ms) |
| `MANUAL_LOGIN` | `0` | `1` para login manual |
| `MANUAL_LOGIN_TIMEOUT_SEC` | `300` | Timeout do login manual (s) |
| `SGE_DEBUG_LOGIN` | `1` no Actions, `0` local | Captura screenshot/HTML |
| `SGE_DEBUG_DIR` | `artifacts/sge-login` | Diretorio de debug |
| `SGE_LOGIN_URL` | `https://www.sge8147.com.br/hportalprofessor.aspx` | URL do portal SGE |
| `SEQUENCIAS_DATABASE_ID` | `""` | ID da database de sequencias |

---

## Estrutura de arquivos

```
Notion_Escolas/
  lancar_notas_sge.py              # Fluxo principal de notas + login SGE
  lancar_sequencia_didatica_sge.py # Fluxo de sequencia didatica
  notion_lancamento.py             # Criacao de estrutura no Notion
  painel.py                        # Interface Streamlit
  processar_solicitacoes_github.py # Processamento de solicitacoes
  docs/
    RUNBOOK_SGE.md                 # Este arquivo
  .github/
    workflows/
      lancar-notas-sge.yml
      plano-aula-sequencia-didatica.yml
      processar-solicitacoes-sge.yml
      solicitar-lancamento-issue.yml
```
