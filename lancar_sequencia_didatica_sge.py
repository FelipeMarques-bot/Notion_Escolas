import argparse
import logging
import os
import re
import tempfile
import urllib.request
import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from notion_client import Client
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from lancar_notas_sge import (
    ACTION_TIMEOUT_MS,
    HEADLESS,
    NAV_TIMEOUT_MS,
    LancamentoError,
    _click_any_selector_any_scope,
    _click_text_any_scope,
    _database_title,
    _discover_databases,
    _extract_first_number,
    _extract_plain_text,
    _extract_turma_number,
    _is_non_empty,
    _is_placeholder_env,
    _iter_scopes,
    _login_sge,
    _normalize,
    _normalize_cpf_for_sge,
    _normalize_notion_id,
    _query_database_rows,
    _resolve_env_credential,
    _safe_notion_call,
    _set_filters_on_portal,
    _turno_code,
    listar_contextos_disponiveis,
)

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
ROOT_PAGE_ID = os.environ.get("ROOT_PAGE_ID", "")
SGE_CPF = os.environ.get("SGE_CPF", "")
SGE_SENHA = os.environ.get("SGE_SENHA", "")
NOTION_STATUS_PUBLICACAO_PROP = os.environ.get("NOTION_STATUS_PUBLICACAO_PROP", "Status publicacao plano SGE")
NOTION_LOG_PUBLICACAO_PROP = os.environ.get("NOTION_LOG_PUBLICACAO_PROP", "Log execucao")
NOTION_LAST_RUN_PROP = os.environ.get("NOTION_LAST_RUN_PROP", "Ultima execucao")

# Evita excesso de WARNING do SDK do Notion durante retries no CI.
_notion_log_level = (os.environ.get("NOTION_CLIENT_LOG_LEVEL", "ERROR") or "ERROR").upper()
logging.getLogger("notion_client").setLevel(getattr(logging, _notion_log_level, logging.ERROR))
logging.getLogger("notion_client.client").setLevel(getattr(logging, _notion_log_level, logging.ERROR))

SEQUENCIAS_DB_TITLE = "sequencias didaticas - pdfs"


@dataclass
class SequenciaRegistro:
    page_id: str
    ano: str
    escola: str
    titulo_documento: str
    arquivo_nome: str
    arquivo_url: str
    periodo_inicio: str  # dd/mm/yyyy
    periodo_fim: str  # dd/mm/yyyy
    n_aulas: int


@dataclass
class ContextoPlano:
    escola: str
    turno: str
    turma: str
    trimestre: str


@dataclass
class ExecucaoResumo:
    contextos_total: int = 0
    planejamentos_criados: int = 0
    anexos_enviados: int = 0
    situacoes_ativadas: int = 0
    falhas: int = 0


def _log(logger, msg: str) -> None:
    if logger:
        logger(msg)


def _make_rich_text(content: str) -> List[Dict]:
    text = (content or "")[:1900]
    if not text:
        return []
    return [{"type": "text", "text": {"content": text}}]


def _atualizar_status_publicacao_notion(page_id: str, status: str, logger=None, log_text: str = "") -> None:
    pid = (page_id or "").strip()
    if not pid or not (NOTION_TOKEN or "").strip():
        return

    try:
        notion = Client(auth=NOTION_TOKEN)
        page = _safe_notion_call(lambda: notion.pages.retrieve(page_id=pid))
        props = page.get("properties", {})
        payload: Dict[str, Dict] = {}

        status_prop = props.get(NOTION_STATUS_PUBLICACAO_PROP, {})
        if status_prop.get("type") == "select":
            payload[NOTION_STATUS_PUBLICACAO_PROP] = {"select": {"name": status}}

        run_prop = props.get(NOTION_LAST_RUN_PROP, {})
        if run_prop.get("type") == "date":
            payload[NOTION_LAST_RUN_PROP] = {"date": {"start": datetime.utcnow().strftime("%Y-%m-%d")}}

        if log_text:
            log_prop = props.get(NOTION_LOG_PUBLICACAO_PROP, {})
            if log_prop.get("type") == "rich_text":
                payload[NOTION_LOG_PUBLICACAO_PROP] = {"rich_text": _make_rich_text(log_text)}

        if payload:
            _safe_notion_call(lambda: notion.pages.update(page_id=pid, properties=payload))
    except Exception as exc:  # noqa: BLE001
        _log(logger, f"Aviso: falha ao atualizar status de publicacao no Notion ({pid}): {exc}")


def _status_publicacao_select_def() -> Dict:
    return {
        "select": {
            "options": [
                {"name": "Pendente", "color": "yellow"},
                {"name": "Em execucao", "color": "blue"},
                {"name": "Publicado no SGE", "color": "green"},
                {"name": "Simulado (dry run)", "color": "gray"},
                {"name": "Erro na publicacao", "color": "red"},
            ]
        }
    }


def _extract_data_source_id_from_db(database_obj: Optional[Dict]) -> str:
    if not database_obj:
        return ""
    data_sources = database_obj.get("data_sources", [])
    if not data_sources:
        return ""
    first = data_sources[0]
    if isinstance(first, dict):
        return (first.get("id") or "").strip()
    return ""


def _database_has_property(database_obj: Optional[Dict], prop_name: str) -> bool:
    props = (database_obj or {}).get("properties", {}) or {}
    return prop_name in props


def _data_source_has_property(data_source_obj: Optional[Dict], prop_name: str) -> bool:
    props = (data_source_obj or {}).get("properties", {}) or {}
    return prop_name in props


def _ensure_status_publicacao_property(
    notion: Client,
    database_id: str,
    database_obj: Optional[Dict],
    logger=None,
) -> None:
    prop_name = (NOTION_STATUS_PUBLICACAO_PROP or "").strip()
    if not prop_name:
        return

    if _database_has_property(database_obj, prop_name):
        return

    prop_def = _status_publicacao_select_def()

    db_update_ok = False

    # Notion classico: tenta atualizar direto na database.
    try:
        _safe_notion_call(
            lambda: notion.databases.update(
                database_id=database_id,
                properties={prop_name: prop_def},
            )
        )
        db_after = _safe_notion_call(lambda: notion.databases.retrieve(database_id=database_id))
        db_update_ok = _database_has_property(db_after, prop_name)
        if db_update_ok:
            _log(logger, f"Coluna '{prop_name}' criada/garantida na database de sequencias.")
    except Exception as exc_db:  # noqa: BLE001
        _log(logger, f"Aviso: nao foi possivel criar coluna via databases.update: {exc_db}")

    if db_update_ok:
        return

    # Notion data_sources: atualiza no data source vinculado.
    if not (hasattr(notion, "data_sources") and hasattr(notion.data_sources, "update")):
        return

    ds_id = _extract_data_source_id_from_db(database_obj)
    if not ds_id:
        try:
            db_obj = _safe_notion_call(lambda: notion.databases.retrieve(database_id=database_id))
            ds_id = _extract_data_source_id_from_db(db_obj)
        except Exception:  # noqa: BLE001
            ds_id = ""

    if not ds_id:
        return

    try:
        ds_obj = _safe_notion_call(lambda: notion.data_sources.retrieve(data_source_id=ds_id))
        ds_props = ds_obj.get("properties", {}) or {}
        if prop_name in ds_props:
            return

        _safe_notion_call(
            lambda: notion.data_sources.update(
                data_source_id=ds_id,
                properties={prop_name: prop_def},
            )
        )
        ds_after = _safe_notion_call(lambda: notion.data_sources.retrieve(data_source_id=ds_id))
        if _data_source_has_property(ds_after, prop_name):
            _log(logger, f"Coluna '{prop_name}' criada/garantida no data source da database de sequencias.")
            return
    except Exception as exc_ds:  # noqa: BLE001
        _log(logger, f"Aviso: falha ao garantir coluna '{prop_name}' no data source: {exc_ds}")

    _log(
        logger,
        (
            f"Aviso: nao foi possivel confirmar a criacao da coluna '{prop_name}' no Notion. "
            "Verifique se a integracao tem permissao de edicao na database e se a coluna nao esta oculta na view."
        ),
    )


def _ano_from_turma(turma: str) -> str:
    m = re.search(r"([6-9])\s*[oº]?\s*ano", turma or "", flags=re.IGNORECASE)
    return f"{m.group(1)}º Ano" if m else ""


def _normalize_ano_label(value: str) -> str:
    text = _normalize(value or "")
    m = re.search(r"\b([6-9])\s*o?\s*ano\b", text)
    if m:
        return f"{m.group(1)}º Ano"

    m = re.search(r"\b([6-9])\b", text)
    if m:
        return f"{m.group(1)}º Ano"

    return ""


def _infer_ano_from_texts(*texts: str) -> str:
    for text in texts:
        ano = _normalize_ano_label(text)
        if ano:
            return ano
    return ""


def _norm_file_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _fmt_date_ddmmyyyy(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""

    # ja em dd/mm/yyyy
    if re.fullmatch(r"\d{2}/\d{2}/\d{4}", raw):
        return raw

    # dd/mm/yy
    if re.fullmatch(r"\d{2}/\d{2}/\d{2}", raw):
        dt = datetime.strptime(raw, "%d/%m/%y")
        return dt.strftime("%d/%m/%Y")

    # yyyy-mm-dd
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        dt = datetime.strptime(raw, "%Y-%m-%d")
        return dt.strftime("%d/%m/%Y")

    return raw


def _parse_periodo_text(periodo_texto: str) -> Tuple[str, str]:
    text = _normalize(periodo_texto or "")
    if not text:
        return "", ""

    matches = re.findall(r"(\d{1,2}/\d{1,2}(?:/\d{2,4})?)", text)
    if len(matches) < 2:
        return "", ""

    i = _fmt_date_ddmmyyyy(matches[0])
    f = _fmt_date_ddmmyyyy(matches[1])
    return i, f


def _calc_n_aulas_from_periodo(inicio: str, fim: str) -> int:
    try:
        dt_i = datetime.strptime(inicio, "%d/%m/%Y")
        dt_f = datetime.strptime(fim, "%d/%m/%Y")
    except Exception:  # noqa: BLE001
        return 0

    if dt_f < dt_i:
        dt_i, dt_f = dt_f, dt_i

    days = (dt_f - dt_i).days + 1
    if days <= 0:
        return 0
    return max(1, int(math.ceil(days / 7.0)))


def _canon_file_name(name: str) -> str:
    text = _normalize(name or "")
    text = re.sub(r"\.pdf$", "", text).strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _file_name_matches(wanted: str, actual: str) -> bool:
    w = _canon_file_name(wanted)
    a = _canon_file_name(actual)
    if not w or not a:
        return False
    return w == a or w in a or a in w


def _first_file_from_prop(prop: Dict) -> Tuple[str, str]:
    if prop.get("type") != "files":
        return "", ""

    files = prop.get("files", [])
    if not files:
        return "", ""

    item = files[0]
    name = (item.get("name") or "").strip()
    if item.get("type") == "file":
        url = ((item.get("file") or {}).get("url") or "").strip()
    elif item.get("type") == "external":
        url = ((item.get("external") or {}).get("url") or "").strip()
    else:
        url = ""

    return name, url


def _resolve_notion_file_upload_url(file_upload_id: str) -> str:
    fid = (file_upload_id or "").strip()
    if not fid:
        return ""

    token = (NOTION_TOKEN or "").strip()
    if not token:
        return ""

    req = urllib.request.Request(
        f"https://api.notion.com/v1/file_uploads/{fid}",
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception:  # noqa: BLE001
        return ""

    # Tenta diferentes formatos de resposta.
    direct = (payload.get("url") or "").strip()
    if direct:
        return direct

    file_node = payload.get("file") or {}
    signed = (file_node.get("url") or "").strip()
    if signed:
        return signed

    return ""


def _first_file_from_prop_any(props: Dict) -> Tuple[str, str]:
    # 1) Prioridade: propriedade de arquivos.
    file_name, file_url = _first_file_from_prop(props.get("Arquivo PDF", {}))
    if file_url:
        return file_name, file_url

    # 2) Compatibilidade com novo tipo file_upload no Notion.
    prop = props.get("Arquivo PDF", {})
    if prop.get("type") == "files":
        for item in prop.get("files", []) or []:
            if item.get("type") != "file_upload":
                continue
            name = (item.get("name") or "").strip()
            upload_id = ((item.get("file_upload") or {}).get("id") or "").strip()
            upload_url = _resolve_notion_file_upload_url(upload_id)
            if upload_url:
                return name, upload_url

    # 3) Fallback: URL em texto (rich_text/title) no campo Arquivo PDF.
    text_url = _extract_plain_text(props.get("Arquivo PDF", {})).strip()
    if text_url.startswith("http://") or text_url.startswith("https://"):
        guessed_name = os.path.basename(text_url.split("?", 1)[0]).strip()
        return guessed_name, text_url

    # 4) Schema alternativo: primeira propriedade do tipo files com URL acessivel.
    for prop in props.values():
        if prop.get("type") != "files":
            continue

        alt_name, alt_url = _first_file_from_prop(prop)
        if alt_url:
            return alt_name, alt_url

        for item in prop.get("files", []) or []:
            if item.get("type") != "file_upload":
                continue
            name = (item.get("name") or "").strip()
            upload_id = ((item.get("file_upload") or {}).get("id") or "").strip()
            upload_url = _resolve_notion_file_upload_url(upload_id)
            if upload_url:
                return name, upload_url

    return file_name, file_url


def _extract_date_property(props: Dict, names: List[str]) -> str:
    for name in names:
        prop = props.get(name, {})
        if prop.get("type") != "date":
            continue
        node = prop.get("date") or {}
        start = (node.get("start") or "").strip()
        if start:
            return _fmt_date_ddmmyyyy(start)
    return ""


def _extract_number_property(props: Dict, names: List[str]) -> int:
    for name in names:
        prop = props.get(name, {})
        if prop.get("type") == "number" and prop.get("number") is not None:
            try:
                return int(prop.get("number"))
            except Exception:  # noqa: BLE001
                continue

        text = _extract_plain_text(prop)
        if not text:
            continue
        m = re.search(r"\d+", text)
        if m:
            return int(m.group(0))
    return 0


def _extract_select_or_text(props: Dict, names: List[str]) -> str:
    for name in names:
        prop = props.get(name, {})
        if prop.get("type") == "select":
            node = prop.get("select")
            val = "" if not node else str(node.get("name", "")).strip()
            if val:
                return val

        text = _extract_plain_text(prop).strip()
        if text:
            return text
    return ""


def _is_active_row(props: Dict) -> bool:
    prop = props.get("Ativo", {})
    if prop.get("type") == "checkbox":
        return bool(prop.get("checkbox", False))
    return True


def _load_sequencias_from_notion(logger=None, ensure_status_property: bool = True) -> List[SequenciaRegistro]:
    root_page_id = _normalize_notion_id(ROOT_PAGE_ID)

    if not NOTION_TOKEN or not root_page_id:
        raise LancamentoError("Defina NOTION_TOKEN e ROOT_PAGE_ID nas variaveis de ambiente.")
    if _is_placeholder_env(NOTION_TOKEN) or _is_placeholder_env(ROOT_PAGE_ID):
        raise LancamentoError("NOTION_TOKEN/ROOT_PAGE_ID estao com placeholders. Atualize com valores reais.")

    notion = Client(auth=NOTION_TOKEN)

    def _find_db_by_search() -> str:
        # Busca direta evita varrer toda a hierarquia (mais rapido e menos sujeito a blocos inacessiveis).
        cursor = None
        while True:
            response = _safe_notion_call(
                lambda: notion.search(
                    query="Sequências Didáticas - PDFs",
                    filter={"property": "object", "value": "database"},
                    start_cursor=cursor,
                    page_size=100,
                )
            )
            for db in response.get("results", []):
                if db.get("archived"):
                    continue
                if _normalize(_database_title(db)) == _normalize("Sequências Didáticas - PDFs"):
                    return (db.get("id") or "").strip()

            if not response.get("has_more"):
                break
            cursor = response.get("next_cursor")
        return ""

    alvo_id = _find_db_by_search()

    # Fallback para descoberta por arvore quando a busca direta nao localizar.
    databases = []
    if not alvo_id:
        databases = _discover_databases(notion, root_page_id, logger=logger)

        for db_id, _, db_title in databases:
            title_norm = _normalize(db_title)
            if title_norm == SEQUENCIAS_DB_TITLE or SEQUENCIAS_DB_TITLE in title_norm:
                alvo_id = db_id
                break

    if not alvo_id:
        raise LancamentoError("Database 'Sequencias Didaticas - PDFs' nao encontrada no Notion.")

    db_obj = _safe_notion_call(lambda: notion.databases.retrieve(database_id=alvo_id))
    title = _database_title(db_obj)
    _log(logger, f"Database de sequencias identificada: {title}")

    if ensure_status_property:
        _ensure_status_publicacao_property(notion, alvo_id, db_obj, logger=logger)

    rows = _query_database_rows(notion, alvo_id, database_obj=db_obj)
    result: List[SequenciaRegistro] = []
    skipped_inactive = 0
    skipped_sem_ano = 0
    skipped_sem_arquivo = 0

    for row in rows:
        props = row.get("properties", {})
        if not _is_active_row(props):
            skipped_inactive += 1
            continue

        ano_bruto = _extract_select_or_text(
            props,
            ["Ano", "Ano/Série", "Ano/Serie", "Série", "Serie", "Turma", "Ano Escolar"],
        )

        escola = _extract_select_or_text(props, ["Escola"])
        titulo_documento = _extract_select_or_text(props, ["Titulo Documento", "Título Documento"])

        titulo_linha = ""
        for prop in props.values():
            if prop.get("type") == "title":
                titulo_linha = _extract_plain_text(prop).strip()
                if titulo_linha:
                    break
        if not titulo_documento:
            titulo_documento = titulo_linha

        arquivo_nome, arquivo_url = _first_file_from_prop_any(props)

        ano = _infer_ano_from_texts(ano_bruto, titulo_documento, titulo_linha, arquivo_nome)
        if not ano:
            skipped_sem_ano += 1
            continue

        periodo_inicio = _extract_date_property(props, ["Periodo inicio", "Período início", "Periodo", "Período"])
        periodo_fim = _extract_date_property(props, ["Periodo fim", "Período fim", "Periodo", "Período"])

        # Se a propriedade Periodo for unica (date com start/end), reusa end.
        if not periodo_fim:
            periodo_unico = props.get("Periodo", {}) or props.get("Período", {})
            if periodo_unico.get("type") == "date":
                node = periodo_unico.get("date") or {}
                end = (node.get("end") or "").strip()
                if end:
                    periodo_fim = _fmt_date_ddmmyyyy(end)

        # Compatibilidade: quando 'Período' for texto (ex.: 25/05 a 19/06).
        if not periodo_inicio or not periodo_fim:
            periodo_txt = _extract_select_or_text(props, ["Período", "Periodo"])
            pi_txt, pf_txt = _parse_periodo_text(periodo_txt)
            periodo_inicio = periodo_inicio or pi_txt
            periodo_fim = periodo_fim or pf_txt

        n_aulas = _extract_number_property(props, ["N aulas", "Nº aulas", "Numero de aulas"])
        if n_aulas <= 0 and periodo_inicio and periodo_fim:
            n_aulas = _calc_n_aulas_from_periodo(periodo_inicio, periodo_fim)

        if not titulo_documento:
            titulo_documento = arquivo_nome or titulo_linha

        # Mantem linha elegivel com o minimo necessario. Campos faltantes
        # podem ser completados por argumentos do workflow/CLI.
        if not arquivo_url:
            skipped_sem_arquivo += 1
            _log(logger, f"Aviso: linha '{titulo_linha or '(sem titulo)'}' ignorada: Arquivo PDF sem URL acessivel.")
            continue

        result.append(
            SequenciaRegistro(
                page_id=row.get("id", ""),
                ano=ano,
                escola=escola,
                titulo_documento=titulo_documento,
                arquivo_nome=arquivo_nome,
                arquivo_url=arquivo_url,
                periodo_inicio=periodo_inicio,
                periodo_fim=periodo_fim,
                n_aulas=n_aulas,
            )
        )

    if not result and len(rows) > 0 and skipped_inactive == len(rows):
        raise LancamentoError(
            "Nenhum registro elegivel: todos os registros estao com 'Ativo' desmarcado na database de Sequencias Didaticas. "
            "Marque ao menos um registro como Ativo para executar o lancamento."
        )

    if not result:
        raise LancamentoError(
            "Nenhum registro ativo/valido encontrado na database de Sequencias Didaticas "
            f"(total={len(rows)}, inativos={skipped_inactive}, sem_ano={skipped_sem_ano}, sem_arquivo={skipped_sem_arquivo})."
        )

    _log(logger, f"Registros de sequencia carregados do Notion: {len(result)}")
    return result


def _filter_contexts(contextos_raw: List[Dict[str, str]], escola: str, trimestre: str, turma: str = "") -> List[ContextoPlano]:
    filtered: List[ContextoPlano] = []
    for item in contextos_raw:
        ctx = ContextoPlano(
            escola=item.get("escola", ""),
            turno=item.get("turno", ""),
            turma=item.get("turma", ""),
            trimestre=item.get("trimestre", ""),
        )
        if escola and _normalize(ctx.escola) != _normalize(escola):
            continue
        if trimestre and _normalize(ctx.trimestre) != _normalize(trimestre):
            continue
        if turma and _normalize(ctx.turma) != _normalize(turma):
            continue
        filtered.append(ctx)
    return filtered


def _pick_template_for_context(
    registros: List[SequenciaRegistro],
    contexto: ContextoPlano,
    filename_by_ano: Dict[str, str],
    override_inicio: str,
    override_fim: str,
) -> Optional[SequenciaRegistro]:
    ano = _ano_from_turma(contexto.turma)
    if not ano:
        return None

    candidates = [r for r in registros if _normalize(r.ano) == _normalize(ano)]
    if not candidates:
        return None

    # Prioriza linha da escola quando preenchida na database.
    with_school = [r for r in candidates if r.escola and _normalize(r.escola) == _normalize(contexto.escola)]
    if with_school:
        candidates = with_school
    else:
        without_school = [r for r in candidates if not r.escola]
        if without_school:
            candidates = without_school

    wanted_file = (filename_by_ano.get(ano, "") or "").strip()
    if wanted_file:
        exact_file = [r for r in candidates if _file_name_matches(wanted_file, r.arquivo_nome)]
        if exact_file:
            candidates = exact_file
        else:
            return None

    chosen = candidates[0]
    final_inicio = _fmt_date_ddmmyyyy(override_inicio) or chosen.periodo_inicio
    final_fim = _fmt_date_ddmmyyyy(override_fim) or chosen.periodo_fim
    final_n_aulas = chosen.n_aulas
    if final_n_aulas <= 0:
        final_n_aulas = _calc_n_aulas_from_periodo(final_inicio, final_fim)

    return SequenciaRegistro(
        page_id=chosen.page_id,
        ano=chosen.ano,
        escola=chosen.escola,
        titulo_documento=chosen.titulo_documento or chosen.arquivo_nome,
        arquivo_nome=chosen.arquivo_nome,
        arquivo_url=chosen.arquivo_url,
        periodo_inicio=final_inicio,
        periodo_fim=final_fim,
        n_aulas=final_n_aulas,
    )


def _download_pdf(url: str, name_hint: str) -> str:
    base_name = (name_hint or "sequencia_didatica.pdf").strip()
    if not base_name.lower().endswith(".pdf"):
        base_name = f"{base_name}.pdf"

    safe_name = re.sub(r"[^a-zA-Z0-9._-]", "_", base_name)
    tmp_dir = tempfile.mkdtemp(prefix="seq_didatica_")
    target = os.path.join(tmp_dir, safe_name)

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=45) as resp:  # noqa: S310
        data = resp.read()

    with open(target, "wb") as f:
        f.write(data)

    return target


def _open_plano_aulas_for_context(page, contexto: ContextoPlano, logger=None) -> bool:
    _set_filters_on_portal(page, contexto, logger=logger)

    turma_num = _extract_turma_number(contexto.turma)
    trimestre_num = _extract_first_number(contexto.trimestre)
    turno_norm = _normalize(contexto.turno).upper()

    for scope in _iter_scopes(page):
        hidden_rows = scope.locator("input[name^='W0019W0075_TURNUMSTR_']")
        total = hidden_rows.count()
        for idx in range(total):
            cell = hidden_rows.nth(idx)
            try:
                label = (cell.input_value(timeout=400) or "").strip()
            except Exception:  # noqa: BLE001
                continue

            norm = _normalize(label)
            ok_turno = bool(turno_norm and _normalize(turno_norm) in norm)
            ok_turma = True if not turma_num else bool(re.search(rf"\bturma\s*{re.escape(turma_num)}\b", norm))
            ok_trim = bool(trimestre_num and f"{trimestre_num}o trimestre" in norm)
            if not (ok_turno and ok_turma and ok_trim):
                continue

            name = (cell.get_attribute("name") or "")
            suffix = name.rsplit("_", 1)[-1]
            selectors = [
                f"#W0019W0075_PLANOAULA_{suffix}",
                f"img[name='W0019W0075_PLANOAULA_{suffix}']",
                f"#W0019W0075_PLANODEAULA_{suffix}",
                f"img[name='W0019W0075_PLANODEAULA_{suffix}']",
                f"#W0019W0075_PLANOAULAS_{suffix}",
                f"img[name='W0019W0075_PLANOAULAS_{suffix}']",
            ]
            for sel in selectors:
                try:
                    icon = scope.locator(sel)
                    if icon.count() == 0:
                        continue
                    icon.first.click(timeout=ACTION_TIMEOUT_MS)
                    page.wait_for_timeout(800)
                    return True
                except Exception:  # noqa: BLE001
                    continue

    # Fallback do print: abrir via menu de rodape.
    if _click_text_any_scope(page, "Plano de Aulas"):
        page.wait_for_timeout(700)
        return True

    return False


def _set_periodo_and_aulas(page, data_inicio: str, data_fim: str, n_aulas: int) -> bool:
    js = """
    ({ inicio, fim, aulas }) => {
      const visible = (el) => {
        const st = window.getComputedStyle(el);
        return st.visibility !== 'hidden' && st.display !== 'none';
      };

      const allText = Array.from(document.querySelectorAll('input[type="text"], input[type="number"]'))
        .filter((el) => !el.disabled && !el.readOnly && visible(el));

      const isDateLike = (el) => {
        const key = `${el.name || ''} ${el.id || ''}`.toLowerCase();
        return key.includes('period') || key.includes('data') || key.includes('dt');
      };

      let dateInputs = allText.filter(isDateLike);
      if (dateInputs.length < 2) {
        dateInputs = allText.filter((el) => (el.value || '').trim() === '').slice(0, 2);
      }
      if (dateInputs.length >= 2) {
        dateInputs[0].value = inicio;
        dateInputs[0].dispatchEvent(new Event('input', { bubbles: true }));
        dateInputs[0].dispatchEvent(new Event('change', { bubbles: true }));

        dateInputs[1].value = fim;
        dateInputs[1].dispatchEvent(new Event('input', { bubbles: true }));
        dateInputs[1].dispatchEvent(new Event('change', { bubbles: true }));
      }

      let aulasInput = allText.find((el) => {
        const key = `${el.name || ''} ${el.id || ''}`.toLowerCase();
        return key.includes('aula') || key.includes('aul');
      });

            if (!aulasInput) {
                aulasInput = allText.find((el) => /^\\d*$/.test((el.value || '').trim())) || null;
            }

      if (!aulasInput) return false;

      aulasInput.value = String(aulas);
      aulasInput.dispatchEvent(new Event('input', { bubbles: true }));
      aulasInput.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    }
    """
    try:
        return bool(page.evaluate(js, {"inicio": data_inicio, "fim": data_fim, "aulas": int(n_aulas)}))
    except Exception:  # noqa: BLE001
        return False


def _click_confirmar(page) -> bool:
    selectors = [
        "button:has-text('Confirmar')",
        "input[type='submit'][value*='Confirmar' i]",
        "input[type='button'][value*='Confirmar' i]",
    ]
    return _click_any_selector_any_scope(page, selectors)


def _click_plus_planejamento(page) -> bool:
        js = """
        () => {
            const txt = Array.from(document.querySelectorAll('body *')).find((el) => {
                const t = (el.textContent || '').toLowerCase();
                return t.includes('planejamentos:');
            });
            if (!txt) return false;
            const root = txt.closest('table, div, tr, td') || txt.parentElement || document.body;
            const candidate = root.querySelector('img[alt="+"], input[type="image"][alt="+"], a img[alt="+"], img[src*="plus" i], img[src*="mais" i]');
            if (!candidate) return false;
            const clickable = candidate.closest('a, button, input[type="image"]') || candidate;
            clickable.click();
            return true;
        }
        """
        try:
            if page.evaluate(js):
                return True
        except Exception:  # noqa: BLE001
            pass

        selectors = [
            "img[alt='+']",
            "input[type='image'][alt='+']",
            "a:has(img[alt='+'])",
            "img[src*='plus' i]",
            "img[src*='mais' i]",
        ]
        return _click_any_selector_any_scope(page, selectors)


def _click_cell_action_by_header(row, header_key: str, prefer_arrow: bool = False) -> bool:
        js = """
        ({ key, preferArrow }) => {
            const tr = rowEl;
            const table = tr.closest('table');
            if (!table) return false;

            const normalize = (s) => (s || '').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '').trim();
            const keyNorm = normalize(key);

            const headerRow = Array.from(table.querySelectorAll('tr')).find((r) => {
                const text = normalize(r.textContent || '');
                return text.includes('periodo') && text.includes('situacao');
            });
            if (!headerRow) return false;

            const heads = Array.from(headerRow.querySelectorAll('th, td'));
            let colIdx = -1;
            for (let i = 0; i < heads.length; i++) {
                const h = normalize(heads[i].textContent || '');
                if (h.includes(keyNorm)) {
                    colIdx = i;
                    break;
                }
            }
            if (colIdx < 0) return false;

            const cells = Array.from(tr.querySelectorAll('td, th'));
            if (colIdx >= cells.length) return false;
            const cell = cells[colIdx];

            const clickables = Array.from(cell.querySelectorAll('a, input[type="image"], button, img'));
            if (!clickables.length) return false;

            const meta = (el) => normalize([
                el.getAttribute?.('title') || '',
                el.getAttribute?.('alt') || '',
                el.getAttribute?.('name') || '',
                el.getAttribute?.('id') || '',
                el.getAttribute?.('src') || '',
            ].join(' '));

            let target = null;
            if (preferArrow) {
                target = clickables.find((el) => {
                    const m = meta(el);
                    return m.includes('seta') || m.includes('arrow') || m.includes('direita') || m.includes('status') || m.includes('situ');
                });
            }

            if (!target) {
                target = clickables.find((el) => meta(el).includes(keyNorm));
            }

            if (!target) {
                target = clickables[0];
            }

            const clickable = target.closest?.('a, button, input[type="image"]') || target;
            clickable.click();
            return true;
        }
        """
        try:
            return bool(row.evaluate(
                js.replace("rowEl", "el"),
                {"key": header_key, "preferArrow": prefer_arrow},
            ))
        except Exception:  # noqa: BLE001
            return False


def _row_for_periodo(page, data_inicio: str, data_fim: str):
    dd_i = data_inicio[:5]
    dd_f = data_fim[:5]

    for scope in _iter_scopes(page):
        try:
            rows = scope.locator("tr")
            total = rows.count()
        except Exception:  # noqa: BLE001
            continue

        for idx in range(total):
            row = rows.nth(idx)
            try:
                text = _normalize(row.inner_text(timeout=300))
            except Exception:  # noqa: BLE001
                continue
            if dd_i in text and dd_f in text:
                return row

    return None


def _click_anexo_icon_on_row(row) -> bool:
    if _click_cell_action_by_header(row, "anex", prefer_arrow=False):
        return True

    try:
        icons = row.locator("a, img, input[type='image']")
        total = icons.count()
    except Exception:  # noqa: BLE001
        return False

    for idx in range(total):
        node = icons.nth(idx)
        try:
            meta = " ".join(
                [
                    (node.get_attribute("title") or ""),
                    (node.get_attribute("alt") or ""),
                    (node.get_attribute("src") or ""),
                    (node.get_attribute("name") or ""),
                ]
            )
            if "anex" not in _normalize(meta):
                continue
            node.click(timeout=ACTION_TIMEOUT_MS)
            return True
        except Exception:  # noqa: BLE001
            continue

    return False


def _click_plus_anexo_section(page) -> bool:
    js = """
    () => {
      const txt = Array.from(document.querySelectorAll('body *')).find((el) => {
        const t = (el.textContent || '').toLowerCase();
        return t.includes('anexos do planej') || t.includes('anexos do planeja');
      });
      if (!txt) return false;
      const root = txt.closest('table, div, tr, td') || txt.parentElement || document.body;
      const candidate = root.querySelector('img[alt="+"], input[type="image"][alt="+"], a img[alt="+"]');
      if (!candidate) return false;
      const clickable = candidate.closest('a, button, input[type="image"]') || candidate;
      clickable.click();
      return true;
    }
    """
    try:
        if page.evaluate(js):
            return True
    except Exception:  # noqa: BLE001
        pass

    return _click_plus_planejamento(page)


def _fill_anexo_form(page, titulo_documento: str, arquivo_path: str) -> bool:
    # Documento
    ok_doc = False
    for scope in _iter_scopes(page):
        for sel in [
            "input[name*='DOCUMENT' i]",
            "input[id*='DOCUMENT' i]",
            "input[type='text']",
        ]:
            try:
                loc = scope.locator(sel)
                if loc.count() == 0:
                    continue
                loc.first.fill(titulo_documento, timeout=ACTION_TIMEOUT_MS)
                ok_doc = True
                break
            except Exception:  # noqa: BLE001
                continue
        if ok_doc:
            break

    # Tipo
    tipo_set = False
    for scope in _iter_scopes(page):
        try:
            selects = scope.locator("select")
            total = selects.count()
        except Exception:  # noqa: BLE001
            continue

        for i in range(total):
            sel = selects.nth(i)
            try:
                options = sel.locator("option")
                ocount = options.count()
            except Exception:  # noqa: BLE001
                continue

            target_value = None
            for j in range(ocount):
                try:
                    label = (options.nth(j).inner_text(timeout=200) or "").strip()
                except Exception:  # noqa: BLE001
                    continue
                if "detal" in _normalize(label):
                    target_value = options.nth(j).get_attribute("value")
                    break

            if target_value is not None:
                try:
                    sel.select_option(value=target_value)
                    tipo_set = True
                    break
                except Exception:  # noqa: BLE001
                    continue
        if tipo_set:
            break

    # Arquivo
    file_set = False
    for scope in _iter_scopes(page):
        try:
            file_loc = scope.locator("input[type='file']")
            if file_loc.count() > 0:
                file_loc.first.set_input_files(arquivo_path)
                file_set = True
                break
        except Exception:  # noqa: BLE001
            continue

    return ok_doc and tipo_set and file_set


def _click_inicio(page) -> None:
    _click_text_any_scope(page, "Inicio")


def _ativar_situacao_da_linha(row) -> bool:
    if _click_cell_action_by_header(row, "situ", prefer_arrow=True):
        return True

    try:
        icons = row.locator("a, img, input[type='image']")
        total = icons.count()
    except Exception:  # noqa: BLE001
        return False

    for idx in range(total):
        node = icons.nth(idx)
        try:
            meta = " ".join(
                [
                    (node.get_attribute("title") or ""),
                    (node.get_attribute("alt") or ""),
                    (node.get_attribute("src") or ""),
                    (node.get_attribute("name") or ""),
                ]
            )
            norm = _normalize(meta)
            if "situ" in norm or "seta" in norm or "status" in norm:
                node.click(timeout=ACTION_TIMEOUT_MS)
                return True
        except Exception:  # noqa: BLE001
            continue

    # fallback pragmatico: ultimo icone clicavel da linha.
    if total > 0:
        try:
            icons.nth(total - 1).click(timeout=ACTION_TIMEOUT_MS)
            return True
        except Exception:  # noqa: BLE001
            return False
    return False


def _executar_fluxo_plano_aulas(page, contexto: ContextoPlano, registro: SequenciaRegistro, dry_run: bool, logger=None) -> Tuple[bool, bool, bool]:
    ok_planejamento = False
    ok_anexo = False
    ok_situacao = False

    if not _open_plano_aulas_for_context(page, contexto, logger=logger):
        raise LancamentoError(f"Nao foi possivel abrir Plano de Aulas para {contexto.escola} | {contexto.turno} | {contexto.turma}.")

    if dry_run:
        return True, False, False

    if not _click_plus_planejamento(page):
        raise LancamentoError("Nao foi possivel clicar no '+' de Planejamentos.")

    if not _set_periodo_and_aulas(page, registro.periodo_inicio, registro.periodo_fim, registro.n_aulas):
        raise LancamentoError("Nao foi possivel preencher Periodo/N aulas na tela de Planejamentos.")

    if not _click_confirmar(page):
        raise LancamentoError("Nao foi possivel confirmar criacao do planejamento.")
    ok_planejamento = True

    try:
        page.wait_for_timeout(1200)
    except Exception:  # noqa: BLE001
        pass

    row = _row_for_periodo(page, registro.periodo_inicio, registro.periodo_fim)
    if row is None:
        raise LancamentoError("Planejamento criado, mas linha por periodo nao foi localizada para anexar arquivo.")

    if not _click_anexo_icon_on_row(row):
        raise LancamentoError("Nao foi possivel abrir coluna Anexos da linha criada.")

    try:
        page.wait_for_timeout(700)
    except Exception:  # noqa: BLE001
        pass

    if not _click_plus_anexo_section(page):
        raise LancamentoError("Nao foi possivel clicar no '+' da secao ANEXOS DO PLANEJAMENTO.")

    try:
        page.wait_for_timeout(700)
    except Exception:  # noqa: BLE001
        pass

    arquivo_local = _download_pdf(registro.arquivo_url, registro.arquivo_nome)
    if not _fill_anexo_form(page, registro.titulo_documento, arquivo_local):
        raise LancamentoError("Nao foi possivel preencher formulario de anexo (Documento/Tipo/Arquivo).")

    if not _click_confirmar(page):
        raise LancamentoError("Nao foi possivel confirmar envio do anexo.")
    ok_anexo = True

    # Volta para tela principal do planejamento.
    _click_text_any_scope(page, "Voltar")
    try:
        page.wait_for_timeout(900)
    except Exception:  # noqa: BLE001
        pass

    row = _row_for_periodo(page, registro.periodo_inicio, registro.periodo_fim)
    if row is not None:
        ok_situacao = _ativar_situacao_da_linha(row)

    _click_inicio(page)
    return ok_planejamento, ok_anexo, ok_situacao


def executar_lancamento_sequencia(
    escola: str = "",
    turma: str = "",
    trimestre: str = "2º Trimestre",
    modo_execucao: str = "por_escola",
    dry_run: bool = False,
    modo_rapido: bool = False,
    data_inicio: str = "",
    data_fim: str = "",
    arquivo_por_ano: Optional[Dict[str, str]] = None,
    logger=print,
) -> ExecucaoResumo:
    cpf = _resolve_env_credential(SGE_CPF, "SGE_CPF", logger=logger, digits_only=True)
    cpf = _normalize_cpf_for_sge(cpf, logger=logger)
    senha = _resolve_env_credential(SGE_SENHA, "SGE_SENHA", logger=logger, digits_only=False)

    if not cpf or not senha:
        raise LancamentoError("Defina SGE_CPF e SGE_SENHA nas variaveis de ambiente.")
    if _is_placeholder_env(cpf) or _is_placeholder_env(senha):
        raise LancamentoError("SGE_CPF/SGE_SENHA estao com placeholders. Atualize com valores reais.")

    registros = _load_sequencias_from_notion(logger=logger, ensure_status_property=(not modo_rapido))
    filtro_contexto: Dict[str, str] = {}
    if escola:
        filtro_contexto["escola"] = escola
    if trimestre:
        filtro_contexto["trimestre"] = trimestre
    if turma:
        filtro_contexto["turma"] = turma

    contextos = _filter_contexts(
        listar_contextos_disponiveis(logger=logger, filtro=filtro_contexto),
        escola=escola,
        trimestre=trimestre,
        turma=turma,
    )

    if not contextos:
        raise LancamentoError("Nenhum contexto de turma encontrado para executar Plano de Aulas.")

    if modo_execucao == "por_turma_em_todas_as_escolas":
        contextos = sorted(contextos, key=lambda c: (_ano_from_turma(c.turma), _normalize(c.turma), _normalize(c.escola), _normalize(c.turno)))
    else:
        contextos = sorted(contextos, key=lambda c: (_normalize(c.escola), _normalize(c.turno), _ano_from_turma(c.turma), _normalize(c.turma)))

    resumo = ExecucaoResumo(contextos_total=len(contextos))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(ACTION_TIMEOUT_MS)

        _login_sge(page, cpf=cpf, senha=senha, logger=logger)

        for idx, ctx in enumerate(contextos, start=1):
            _log(logger, f"[{idx}/{len(contextos)}] Processando {ctx.escola} | {ctx.turno} | {ctx.turma} | {ctx.trimestre}")

            registro = _pick_template_for_context(
                registros,
                contexto=ctx,
                filename_by_ano=arquivo_por_ano or {},
                override_inicio=_fmt_date_ddmmyyyy(data_inicio),
                override_fim=_fmt_date_ddmmyyyy(data_fim),
            )

            if not registro:
                _log(logger, f"Aviso: nenhum template de sequencia encontrado para {ctx.turma} ({ctx.escola}).")
                resumo.falhas += 1
                continue

            if not registro.periodo_inicio or not registro.periodo_fim:
                _log(
                    logger,
                    f"Falha em {ctx.escola} | {ctx.turma}: periodo ausente (preencha no Notion ou envie --data-inicio/--data-fim).",
                )
                resumo.falhas += 1
                continue

            if registro.n_aulas <= 0:
                _log(
                    logger,
                    f"Falha em {ctx.escola} | {ctx.turma}: N aulas invalido (preencha no Notion).",
                )
                resumo.falhas += 1
                continue

            try:
                if not modo_rapido:
                    _atualizar_status_publicacao_notion(
                        registro.page_id,
                        "Em execucao",
                        logger=logger,
                        log_text=f"Iniciado para {ctx.escola} | {ctx.turno} | {ctx.turma} | {ctx.trimestre}",
                    )

                ok_plan, ok_anexo, ok_sit = _executar_fluxo_plano_aulas(
                    page,
                    contexto=ctx,
                    registro=registro,
                    dry_run=dry_run,
                    logger=logger,
                )

                if not modo_rapido:
                    status_final = "Simulado (dry run)" if dry_run else "Publicado no SGE"
                    _atualizar_status_publicacao_notion(
                        registro.page_id,
                        status_final,
                        logger=logger,
                        log_text=(
                            f"Concluido para {ctx.escola} | {ctx.turno} | {ctx.turma} | {ctx.trimestre}. "
                            f"planejamento={ok_plan}, anexo={ok_anexo}, situacao={ok_sit}"
                        ),
                    )

                if ok_plan:
                    resumo.planejamentos_criados += 1
                if ok_anexo:
                    resumo.anexos_enviados += 1
                if ok_sit:
                    resumo.situacoes_ativadas += 1
            except PlaywrightTimeoutError as exc:
                resumo.falhas += 1
                _log(logger, f"Falha por timeout em {ctx.escola} | {ctx.turma}: {exc}")
                if not modo_rapido:
                    _atualizar_status_publicacao_notion(
                        registro.page_id,
                        "Erro na publicacao",
                        logger=logger,
                        log_text=f"Timeout em {ctx.escola} | {ctx.turno} | {ctx.turma} | {ctx.trimestre}: {exc}",
                    )
                _click_inicio(page)
            except Exception as exc:  # noqa: BLE001
                resumo.falhas += 1
                _log(logger, f"Falha em {ctx.escola} | {ctx.turma}: {exc}")
                if not modo_rapido:
                    _atualizar_status_publicacao_notion(
                        registro.page_id,
                        "Erro na publicacao",
                        logger=logger,
                        log_text=f"Erro em {ctx.escola} | {ctx.turno} | {ctx.turma} | {ctx.trimestre}: {exc}",
                    )
                _click_inicio(page)

        context.close()
        browser.close()

    return resumo


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lanca sequencia didatica (Plano de Aulas) no SGE")
    parser.add_argument("--escola", default="")
    parser.add_argument("--turma", default="")
    parser.add_argument("--trimestre", default="2º Trimestre")
    parser.add_argument("--modo-execucao", default="por_escola", choices=["por_escola", "por_turma_em_todas_as_escolas"])
    parser.add_argument("--modo-rapido", action="store_true")
    parser.add_argument("--data-inicio", default="")
    parser.add_argument("--data-fim", default="")
    parser.add_argument("--arquivo-6-ano", default="")
    parser.add_argument("--arquivo-7-ano", default="")
    parser.add_argument("--arquivo-8-ano", default="")
    parser.add_argument("--arquivo-9-ano", default="")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    arquivo_por_ano = {
        "6º Ano": args.arquivo_6_ano,
        "7º Ano": args.arquivo_7_ano,
        "8º Ano": args.arquivo_8_ano,
        "9º Ano": args.arquivo_9_ano,
    }

    try:
        resumo = executar_lancamento_sequencia(
            escola=args.escola if args.escola and _normalize(args.escola) != "todas" else "",
            turma=args.turma,
            trimestre=args.trimestre,
            modo_execucao=args.modo_execucao,
            dry_run=args.dry_run,
            modo_rapido=args.modo_rapido,
            data_inicio=args.data_inicio,
            data_fim=args.data_fim,
            arquivo_por_ano=arquivo_por_ano,
            logger=print,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Erro: {exc}")
        return 1

    print("Resumo sequencia didatica:")
    print(f"- contextos_total: {resumo.contextos_total}")
    print(f"- planejamentos_criados: {resumo.planejamentos_criados}")
    print(f"- anexos_enviados: {resumo.anexos_enviados}")
    print(f"- situacoes_ativadas: {resumo.situacoes_ativadas}")
    print(f"- falhas: {resumo.falhas}")
    return 0 if resumo.falhas == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
