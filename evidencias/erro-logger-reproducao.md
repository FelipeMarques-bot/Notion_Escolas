# Evidência de reprodução — `name 'logger' is not defined`

## Resumo

O erro ocorre em `lancar_sequencia_didatica_sge.py` na função `_pick_template_for_context`, que
utiliza `_log(logger, ...)` sem receber `logger` como parâmetro.

## Localização do erro

### Função `_pick_template_for_context` (linhas 525–597)

**Assinatura atual** (linhas 525–531):
```python
def _pick_template_for_context(
    registros: List[SequenciaRegistro],
    contexto: ContextoPlano,
    filename_by_ano: Dict[str, str],
    override_inicio: str,
    override_fim: str,
) -> Optional[SequenciaRegistro]:
```

**Uso de `logger` sem definição:**
- Linha 537: `_log(logger, f"[diag] Apos filtro por ano ('{ano}'): {len(candidates)} candidato(s).")`
- Linha 545: `_log(logger, f"[diag] Apos filtro por escola ('{contexto.escola}'): {len(candidates)} candidato(s).")`
- Linha 550: `_log(logger, f"[diag] Sem match por escola; usando {len(candidates)} candidato(s) sem escola preenchida.")`
- Linha 552: `_log(logger, f"[diag] AVISO: escola preenchida no Notion mas nao bate com contexto ('{contexto.escola}').")`

### Chamada sem `logger` (linhas 1199–1206)

```python
registro = _pick_template_for_context(
    registros,
    contexto=ctx,
    filename_by_ano=arquivo_por_ano or {},
    override_inicio=_fmt_date_ddmmyyyy(data_inicio),
    override_fim=_fmt_date_ddmmyyyy(data_fim),
)
```

Note que `logger` **não** é passado como argumento.

## Causa raiz

`_pick_template_for_context` não declara `logger` na assinatura nem o recebe
da chamadora `executar_lancamento_sequencia`. Como não há variável global `logger`,
o Python levanta `NameError: name 'logger' is not defined` na primeira linha
que tenta usar `_log(logger, ...)`.

## Fluxo de chamada que leva ao erro

```
main()
  └─ executar_lancamento_sequencia(logger=print)
       └─ _pick_template_for_context(registros, contexto=ctx, ...)  ← logger NÃO passado
            └─ _log(logger, ...)  ← NameError: logger não definido neste escopo
```

## Impacto

O erro ocorre **antes** de qualquer processamento real (login SGE, match de
arquivo, etc.), impedindo a execução completa do workflow.

## Condições de reprodução

1. Rodar `python lancar_sequencia_didatica_sge.py` (qualquer modo)
2. O caminho `executar_lancamento_sequencia` → `_pick_template_for_context` é
   acionado sempre que há sequências carregadas do Notion.
3. O erro é determinístico: ocorre em 100% das execuções com registros válidos.

## Resolução esperada (Sprint 1)

Adicionar `logger=None` à assinatura de `_pick_template_for_context` e passar
`logger=logger` na chamada em `executar_lancamento_sequencia`.
