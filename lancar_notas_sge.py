import argparse
import os
import re
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from notion_client import Client
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*args, **kwargs):
        return False

LogFn = Callable[[str], None]

load_dotenv(override=True)

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
ROOT_PAGE_ID = os.environ.get("ROOT_PAGE_ID", "")
SGE_CPF = os.environ.get("SGE_CPF", "")
SGE_SENHA = os.environ.get("SGE_SENHA", "")
DEFAULT_SGE_LOGIN_URL = "https://www.sge8147.com.br/"
SGE_LOGIN_URL = os.environ.get("SGE_LOGIN_URL", DEFAULT_SGE_LOGIN_URL)
HEADLESS = os.environ.get("HEADLESS", "1") == "1"
NAV_TIMEOUT_MS = int(os.environ.get("NAV_TIMEOUT_MS", "35000"))
ACTION_TIMEOUT_MS = int(os.environ.get("ACTION_TIMEOUT_MS", "9000"))
NOTION_STATUS_PROP = os.environ.get("NOTION_STATUS_PROP", "Status lancamento")
NOTION_LAST_RUN_PROP = os.environ.get("NOTION_LAST_RUN_PROP", "Ultima execucao")
NOTION_LAUNCH_DATE_PROP = os.environ.get("NOTION_LAUNCH_DATE_PROP", "Data lancamento")
NOTION_LOG_PROP = os.environ.get("NOTION_LOG_PROP", "Log execucao")
NOTION_REQUEST_PROP = os.environ.get("NOTION_REQUEST_PROP", "Solicitar lancamento")

TURNOS_KNOWN = ["Matutino", "Vespertino", "Noturno", "Integral"]
TRIMESTRES_KNOWN = [
    "1o Trimestre",
    "2o Trimestre",
    "3o Trimestre",
    "1º Trimestre",
    "2º Trimestre",
    "3º Trimestre",
]

IGNORE_COLS = {
    "Nome",
    "Status",
    "Status Fluxo",
    "Media",
    "Media Final",
    "Observacoes",
    "Observacoes Pedagogicas",
}


@dataclass
class RegistroNota:
    escola: str
    turno: str
    turma: str
    trimestre: str
    aluno: str
    atividade: str
    nota: float
    notion_page_id: str = ""
    notion_status_prop: str = ""


@dataclass
class ContextoTurma:
    escola: str
    turno: str
    turma: str
    trimestre: str


class LancamentoError(RuntimeError):
    pass


def _log(logger: Optional[LogFn], msg: str) -> None:
    if logger:
        logger(msg)


def _is_non_empty(value: Optional[str]) -> bool:
    return bool(value and value.strip())


def _is_placeholder_env(value: str) -> bool:
    return value.strip().lower() in {
        "your_token_here",
        "your_root_page_id_here",
        "seu_token",
        "id_da_pagina_raiz",
        "seu_cpf",
        "sua_senha",
    }


def _normalize(s: str) -> str:
    text = (s or "").strip().lower()
    # Uniformiza ordinais usados em serie/trimestre: 6º == 6o, 2° == 2o.
    text = text.replace("º", "o").replace("°", "o").replace("ª", "a")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text)


def _normalize_notion_id(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""

    # Aceita UUID com ou sem hifens, ou URL da pagina do Notion.
    match = re.search(
        r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32})",
        raw,
    )
    if not match:
        return raw

    token = match.group(1).replace("-", "").lower()
    if len(token) != 32:
        return raw
    return f"{token[:8]}-{token[8:12]}-{token[12:16]}-{token[16:20]}-{token[20:32]}"


def _resolve_sge_login_url(logger: Optional[LogFn] = None) -> str:
    raw = (SGE_LOGIN_URL or "").strip().strip('"').strip("'")
    if not raw:
        _log(logger, f"Aviso: SGE_LOGIN_URL vazia; usando padrao {DEFAULT_SGE_LOGIN_URL}")
        return DEFAULT_SGE_LOGIN_URL

    if not re.match(r"^https?://", raw, flags=re.IGNORECASE):
        raw = f"https://{raw}"

    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        _log(logger, f"Aviso: SGE_LOGIN_URL invalida; usando padrao {DEFAULT_SGE_LOGIN_URL}")
        return DEFAULT_SGE_LOGIN_URL

    return raw


def _to_float(value: object) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(" ", "")
    if not text:
        return None
    text = text.replace(",", ".")
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _status_prop_for_activity(atividade: str) -> str:
    texto = (atividade or "").strip().lower()
    match = re.search(r"(\d+)\s*$", texto)
    if match and match.group(1) in {"1", "2", "3"}:
        return f"Status lancamento {match.group(1)}"
    return "Status lancamento"


def _safe_notion_call(fn):
    retry = 4
    wait = 1.2
    last = None
    for idx in range(1, retry + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if "api token is invalid" in msg or "unauthorized" in msg:
                raise LancamentoError(
                    "NOTION_TOKEN invalido no ambiente de execucao. Atualize o secret NOTION_TOKEN no GitHub Actions."
                ) from exc
            last = exc
            if idx == retry:
                break
            time.sleep(wait * idx)
    raise last


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _make_rich_text(content: str) -> List[Dict]:
    text = (content or "")[:1900]
    if not text:
        return []
    return [{"type": "text", "text": {"content": text}}]


def atualizar_status_execucao_notion(
    page_id: str,
    status: str,
    logger: Optional[LogFn] = None,
    log_text: str = "",
    clear_request: bool = False,
) -> None:
    if not page_id:
        return
    if not NOTION_TOKEN:
        _log(logger, "Aviso: NOTION_TOKEN nao definido; nao foi possivel atualizar status no Notion.")
        return

    notion = Client(auth=NOTION_TOKEN)

    try:
        page = _safe_notion_call(lambda: notion.pages.retrieve(page_id=page_id))
    except Exception as exc:  # noqa: BLE001
        _log(logger, f"Aviso: falha ao ler pagina de execucao no Notion: {exc}")
        return

    props = page.get("properties", {})
    payload: Dict[str, Dict] = {}

    if NOTION_STATUS_PROP in props and props[NOTION_STATUS_PROP].get("type") == "select":
        payload[NOTION_STATUS_PROP] = {"select": {"name": status}}
    else:
        _log(logger, f"Aviso: propriedade de status nao encontrada/compativel: {NOTION_STATUS_PROP}")

    if NOTION_LAST_RUN_PROP in props and props[NOTION_LAST_RUN_PROP].get("type") == "date":
        payload[NOTION_LAST_RUN_PROP] = {"date": {"start": _utc_now_iso()}}
    else:
        _log(logger, f"Aviso: propriedade de data nao encontrada/compativel: {NOTION_LAST_RUN_PROP}")

    if NOTION_LAUNCH_DATE_PROP in props and props[NOTION_LAUNCH_DATE_PROP].get("type") == "date":
        payload[NOTION_LAUNCH_DATE_PROP] = {"date": {"start": _utc_now_iso()}}

    if log_text and NOTION_LOG_PROP in props and props[NOTION_LOG_PROP].get("type") == "rich_text":
        payload[NOTION_LOG_PROP] = {"rich_text": _make_rich_text(log_text)}
    elif log_text:
        _log(logger, f"Aviso: propriedade de log nao encontrada/compativel: {NOTION_LOG_PROP}")

    if clear_request and NOTION_REQUEST_PROP in props and props[NOTION_REQUEST_PROP].get("type") == "checkbox":
        payload[NOTION_REQUEST_PROP] = {"checkbox": False}
    elif clear_request:
        _log(logger, f"Aviso: propriedade de solicitacao nao encontrada/compativel: {NOTION_REQUEST_PROP}")

    if not payload:
        return

    try:
        _safe_notion_call(lambda: notion.pages.update(page_id=page_id, properties=payload))
    except Exception as exc:  # noqa: BLE001
        _log(logger, f"Aviso: falha ao atualizar status no Notion: {exc}")


def _find_pending_request_page_id(escola: str, logger: Optional[LogFn] = None) -> str:
    escola = (escola or "").strip()
    if not escola or not NOTION_TOKEN:
        return ""

    notion = Client(auth=NOTION_TOKEN)
    target_title = f"Solicitacoes SGE - {escola}"

    data_source_ids: List[str] = []
    cursor = None
    while True:
        response = _safe_notion_call(
            lambda: notion.search(
                query=target_title,
                filter={"property": "object", "value": "data_source"},
                start_cursor=cursor,
                page_size=100,
            )
        )

        for ds in response.get("results", []):
            title = "".join(x.get("plain_text", "") for x in ds.get("title", [])).strip()
            if _normalize(title) != _normalize(target_title):
                continue
            ds_id = ds.get("id", "")
            if ds_id:
                data_source_ids.append(ds_id)

        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")

    if not data_source_ids:
        _log(logger, f"Aviso: nenhuma data source de solicitacao encontrada para {escola}.")
        return ""

    for ds_id in data_source_ids:
        cursor = None
        while True:
            query_resp = _safe_notion_call(
                lambda: notion.data_sources.query(
                    data_source_id=ds_id,
                    start_cursor=cursor,
                    page_size=100,
                )
            )

            for page in query_resp.get("results", []):
                props = page.get("properties", {})

                req_prop = props.get(NOTION_REQUEST_PROP, {})
                solicitar = req_prop.get("type") == "checkbox" and bool(req_prop.get("checkbox", False))

                status_prop = props.get(NOTION_STATUS_PROP, {})
                status_name = ""
                if status_prop.get("type") == "select":
                    status_name = ((status_prop.get("select") or {}).get("name") or "").strip()

                escola_prop = _extract_plain_text(props.get("Escola", {})).strip()

                if not solicitar:
                    continue
                if status_name not in {"", "Pendente"}:
                    continue
                if escola_prop and _normalize(escola_prop) != _normalize(escola):
                    continue

                page_id = page.get("id", "")
                if page_id:
                    return page_id

            if not query_resp.get("has_more"):
                break
            cursor = query_resp.get("next_cursor")

    _log(logger, f"Aviso: nenhuma solicitacao pendente encontrada para {escola}.")
    return ""


def _extract_plain_text(prop: Dict) -> str:
    ptype = prop.get("type")
    if ptype == "title":
        return "".join(x.get("plain_text", "") for x in prop.get("title", []))
    if ptype == "rich_text":
        return "".join(x.get("plain_text", "") for x in prop.get("rich_text", []))
    if ptype == "number":
        value = prop.get("number")
        return "" if value is None else str(value)
    if ptype == "select":
        node = prop.get("select")
        return "" if not node else node.get("name", "")
    if ptype == "formula":
        formula = prop.get("formula", {})
        ftype = formula.get("type")
        return "" if not ftype else str(formula.get(ftype, ""))
    if ptype == "rollup":
        data = prop.get("rollup", {})
        if data.get("type") == "number":
            return "" if data.get("number") is None else str(data.get("number"))
        if data.get("type") == "array":
            arr = data.get("array", [])
            parts = []
            for item in arr:
                item_type = item.get("type")
                if item_type == "title":
                    parts.append("".join(x.get("plain_text", "") for x in item.get("title", [])))
                elif item_type == "rich_text":
                    parts.append("".join(x.get("plain_text", "") for x in item.get("rich_text", [])))
            return " ".join(x for x in parts if x)
    return ""


def _database_title(database: Dict) -> str:
    titles = database.get("title", [])
    if not titles:
        return ""
    return "".join(x.get("plain_text", "") for x in titles).strip()


def _is_notas_database(title: str) -> bool:
    return _normalize(title).startswith("notas escolas -")


def _page_title(page: Dict) -> str:
    props = page.get("properties", {})
    for prop in props.values():
        if prop.get("type") == "title":
            return _extract_plain_text(prop).strip()
    return ""


def _list_children(notion: Client, block_id: str) -> List[Dict]:
    items = []
    cursor = None
    while True:
        response = _safe_notion_call(
            lambda: notion.blocks.children.list(block_id=block_id, start_cursor=cursor, page_size=100)
        )
        items.extend(response.get("results", []))
        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")
    return items


def _discover_databases(
    notion: Client,
    root_page_id: str,
    logger: Optional[LogFn] = None,
) -> List[Tuple[str, List[str], str]]:
    queue: List[Tuple[str, List[str]]] = [(root_page_id, ["ROOT"])]
    visited_pages = set()
    databases: List[Tuple[str, List[str], str]] = []

    while queue:
        page_id, breadcrumb = queue.pop(0)
        if page_id in visited_pages:
            continue
        visited_pages.add(page_id)

        try:
            children = _list_children(notion, page_id)
        except Exception as exc:  # noqa: BLE001
            _log(
                logger,
                f"Aviso: pagina/bloco inacessivel no Notion durante descoberta ({page_id}): {exc}",
            )
            continue
        for block in children:
            btype = block.get("type")

            if btype == "child_page":
                title = block.get("child_page", {}).get("title", "")
                queue.append((block["id"], breadcrumb + [title]))
                continue

            if btype == "link_to_page":
                link_data = block.get("link_to_page", {})
                if link_data.get("type") == "page_id":
                    queue.append((link_data.get("page_id", ""), breadcrumb + ["linked-page"]))
                continue

            if btype == "child_database":
                db_title = block.get("child_database", {}).get("title", "")
                databases.append((block["id"], breadcrumb.copy(), db_title))
                continue

    return databases


def _extract_data_source_id(database_obj: Optional[Dict]) -> Optional[str]:
    if not database_obj:
        return None
    data_sources = database_obj.get("data_sources", [])
    if not data_sources:
        return None
    first = data_sources[0]
    if isinstance(first, dict):
        return first.get("id")
    return None


def _query_database_rows(notion: Client, database_id: str, database_obj: Optional[Dict] = None) -> List[Dict]:
    rows = []
    cursor = None

    query_databases = hasattr(notion, "databases") and hasattr(notion.databases, "query")
    query_data_sources = hasattr(notion, "data_sources") and hasattr(notion.data_sources, "query")

    data_source_id = _extract_data_source_id(database_obj)
    if not query_databases and query_data_sources and not data_source_id:
        db_obj = _safe_notion_call(lambda: notion.databases.retrieve(database_id=database_id))
        data_source_id = _extract_data_source_id(db_obj)

    if not query_databases and query_data_sources and not data_source_id:
        raise LancamentoError(
            "Nao foi possivel localizar data_source para consultar as linhas da database no Notion."
        )

    while True:
        if query_databases:
            response = _safe_notion_call(
                lambda: notion.databases.query(database_id=database_id, start_cursor=cursor, page_size=100)
            )
        elif query_data_sources:
            response = _safe_notion_call(
                lambda: notion.data_sources.query(data_source_id=data_source_id, start_cursor=cursor, page_size=100)
            )
        else:
            raise LancamentoError("Versao da biblioteca do Notion sem suporte para query de databases.")

        rows.extend(response.get("results", []))
        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")
    return rows


def _infer_context(parts: Iterable[str]) -> ContextoTurma:
    parts_clean = [p for p in (x.strip() for x in parts) if p]
    all_text = " | ".join(parts_clean)

    escola = ""
    turno = ""
    turma = ""
    trimestre = ""

    for p in parts_clean:
        if not turno:
            for t in TURNOS_KNOWN:
                if _normalize(t) in _normalize(p):
                    turno = t
                    break

        if not trimestre:
            for tr in TRIMESTRES_KNOWN:
                if _normalize(tr) in _normalize(p):
                    trimestre = tr
                    break

        if not turma:
            found = re.search(r"([6-9][oº]?\s*Ano)", p, flags=re.IGNORECASE)
            if found:
                turma = found.group(1).replace("º", "o")

    if not escola:
        # Heuristica: assume o primeiro item relevante do breadcrumb
        for p in parts_clean:
            if p.lower() in {"root", "linked-page"}:
                continue
            if _normalize(p) in {"dashboard de lancamentos", "portal de gestao de avaliacoes"}:
                continue
            if any(_normalize(x) in _normalize(p) for x in TURNOS_KNOWN + TRIMESTRES_KNOWN):
                continue
            if re.search(r"[6-9][oº]?\s*ano", p, flags=re.IGNORECASE):
                continue
            escola = p
            break

    if not escola:
        escola = "Escola nao identificada"
    if not turno:
        turno = "Turno nao identificado"
    if not turma:
        turma = "Turma nao identificada"
    if not trimestre:
        trimestre = "Trimestre nao identificado"

    # Limpeza final de espacos para evitar divergencias no filtro
    escola = re.sub(r"\s+", " ", escola).strip()
    turno = re.sub(r"\s+", " ", turno).strip()
    turma = re.sub(r"\s+", " ", turma).strip()
    trimestre = re.sub(r"\s+", " ", trimestre).strip()

    _ = all_text
    return ContextoTurma(escola=escola, turno=turno, turma=turma, trimestre=trimestre)


def _is_probably_grade_column(col_name: str) -> bool:
    clean = col_name.strip()
    if not clean or clean in IGNORE_COLS:
        return False
    lowered = clean.lower()
    blacklist = ["status", "media", "obs", "coment", "nome", "id", "chamada", "frequencia"]
    return all(word not in lowered for word in blacklist)


def carregar_notas_notion(logger: Optional[LogFn] = None) -> List[RegistroNota]:
    root_page_id = _normalize_notion_id(ROOT_PAGE_ID)

    if not NOTION_TOKEN or not root_page_id:
        raise LancamentoError("Defina NOTION_TOKEN e ROOT_PAGE_ID nas variaveis de ambiente.")
    if _is_placeholder_env(NOTION_TOKEN) or _is_placeholder_env(ROOT_PAGE_ID):
        raise LancamentoError("NOTION_TOKEN/ROOT_PAGE_ID estao com placeholders. Atualize com valores reais.")

    notion = Client(auth=NOTION_TOKEN)
    _log(logger, "Conectando ao Notion e descobrindo databases...")
    databases = _discover_databases(notion, root_page_id, logger=logger)

    if not databases:
        raise LancamentoError("Nenhuma database foi encontrada a partir de ROOT_PAGE_ID.")

    registros: List[RegistroNota] = []
    candidatos: List[Dict[str, Any]] = []

    for db_id, breadcrumb, db_title in databases:
        try:
            db_obj = _safe_notion_call(lambda: notion.databases.retrieve(database_id=db_id))
        except Exception as exc:  # noqa: BLE001
            _log(logger, f"Aviso: falha ao ler metadata da database {db_id}: {exc}")
            continue

        title = _database_title(db_obj) or db_title
        if not _is_notas_database(title):
            continue
        try:
            rows = _query_database_rows(notion, db_id, database_obj=db_obj)
        except Exception as exc:  # noqa: BLE001
            _log(logger, f"Aviso: pulando database inacessivel {title or db_id}: {exc}")
            continue

        if not rows:
            continue

        context = _infer_context([*breadcrumb, title])
        candidatos.append(
            {
                "db_id": db_id,
                "title": title,
                "context": context,
                "rows": rows,
            }
        )

    if not candidatos:
        return registros

    # Em caso de bases duplicadas com o mesmo titulo, processa apenas a com mais linhas.
    deduplicadas: Dict[str, Dict[str, Any]] = {}
    duplicadas_ignoradas = 0
    for candidato in candidatos:
        key = _normalize(candidato["title"])
        atual = deduplicadas.get(key)
        if atual is None:
            deduplicadas[key] = candidato
            continue

        if len(candidato["rows"]) > len(atual["rows"]):
            deduplicadas[key] = candidato
            duplicadas_ignoradas += 1
        else:
            duplicadas_ignoradas += 1

    if duplicadas_ignoradas:
        _log(logger, f"Aviso: {duplicadas_ignoradas} database(s) duplicada(s) foram ignoradas automaticamente.")

    for item in deduplicadas.values():
        title = item["title"]
        context = item["context"]
        rows = item["rows"]
        _log(logger, f"Database {title}: {len(rows)} alunos encontrados")

        for row in rows:
            props = row.get("properties", {})

            aluno = ""
            if "Nome" in props:
                aluno = _extract_plain_text(props["Nome"]).strip()
            if not aluno:
                for prop in props.values():
                    if prop.get("type") == "title":
                        aluno = _extract_plain_text(prop).strip()
                        if aluno:
                            break

            if not _is_non_empty(aluno):
                continue

            for col_name, prop in props.items():
                if not _is_probably_grade_column(col_name):
                    continue
                nota = _to_float(_extract_plain_text(prop))
                if nota is None:
                    continue

                registros.append(
                    RegistroNota(
                        escola=context.escola,
                        turno=context.turno,
                        turma=context.turma,
                        trimestre=context.trimestre,
                        aluno=aluno,
                        atividade=col_name.strip(),
                        nota=nota,
                        notion_page_id=row.get("id", ""),
                        notion_status_prop=_status_prop_for_activity(col_name.strip()),
                    )
                )

    if not registros:
        raise LancamentoError("Nenhuma nota valida foi encontrada no Notion.")

    _log(logger, f"Total de notas carregadas do Notion: {len(registros)}")
    return registros


def listar_contextos_disponiveis(logger: Optional[LogFn] = None) -> List[Dict[str, str]]:
    try:
        registros = carregar_notas_notion(logger=logger)
        contextos = {
            (r.escola, r.turno, r.turma, r.trimestre)
            for r in registros
        }
        result = [
            {"escola": e, "turno": t, "turma": tu, "trimestre": tr}
            for e, t, tu, tr in sorted(contextos)
        ]
        return result
    except LancamentoError as exc:
        if "Nenhuma nota valida" not in str(exc):
            raise

    root_page_id = _normalize_notion_id(ROOT_PAGE_ID)

    if not NOTION_TOKEN or not root_page_id:
        raise LancamentoError("Defina NOTION_TOKEN e ROOT_PAGE_ID nas variaveis de ambiente.")

    notion = Client(auth=NOTION_TOKEN)
    _log(logger, "Nenhuma nota valida encontrada. Listando contextos pela estrutura das databases...")
    databases = _discover_databases(notion, root_page_id, logger=logger)

    contextos = set()
    for db_id, breadcrumb, db_title in databases:
        try:
            db_obj = _safe_notion_call(lambda: notion.databases.retrieve(database_id=db_id))
            title = _database_title(db_obj) or db_title
        except Exception:  # noqa: BLE001
            title = db_title

        if not _is_notas_database(title):
            continue

        ctx = _infer_context([*breadcrumb, title])
        if "nao identificado" in ctx.turno.lower() or "nao identificado" in ctx.turma.lower():
            continue
        contextos.add((ctx.escola, ctx.turno, ctx.turma, ctx.trimestre))

    return [
        {"escola": e, "turno": t, "turma": tu, "trimestre": tr}
        for e, t, tu, tr in sorted(contextos)
    ]


def _filtrar_registros(registros: List[RegistroNota], filtro: Optional[Dict[str, str]]) -> List[RegistroNota]:
    if not filtro:
        return registros

    def match(value: str, key: str) -> bool:
        expected = filtro.get(key)
        return True if not expected else _normalize(value) == _normalize(expected)

    return [
        r
        for r in registros
        if match(r.escola, "escola")
        and match(r.turno, "turno")
        and match(r.turma, "turma")
        and match(r.trimestre, "trimestre")
    ]


def _first_visible(page, selectors: List[str]):
    for selector in selectors:
        loc = page.locator(selector)
        if loc.count() > 0:
            return loc.first
    return None


def _click_text(page, text: str) -> bool:
    text = text.strip()
    if not text:
        return False

    candidates = [
        page.get_by_role("button", name=text),
        page.get_by_role("link", name=text),
        page.get_by_role("option", name=text),
        page.get_by_text(text, exact=True),
        page.get_by_text(text, exact=False),
    ]
    for loc in candidates:
        try:
            if loc.count() > 0:
                loc.first.click(timeout=2000)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _login_sge(page, logger: Optional[LogFn]) -> None:
    login_url = _resolve_sge_login_url(logger=logger)
    _log(logger, "Abrindo pagina de login do SGE...")
    page.goto(login_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

    cpf_input = _first_visible(
        page,
        [
            "input[name*='cpf' i]",
            "input[id*='cpf' i]",
            "input[placeholder*='cpf' i]",
            "input[type='text']",
        ],
    )
    senha_input = _first_visible(
        page,
        [
            "input[name*='senha' i]",
            "input[id*='senha' i]",
            "input[placeholder*='senha' i]",
            "input[type='password']",
        ],
    )

    if cpf_input is None or senha_input is None:
        raise LancamentoError("Nao foi possivel localizar os campos de login no SGE.")

    cpf_input.fill(SGE_CPF, timeout=ACTION_TIMEOUT_MS)
    senha_input.fill(SGE_SENHA, timeout=ACTION_TIMEOUT_MS)

    submit = _first_visible(
        page,
        [
            "button[type='submit']",
            "input[type='submit']",
            "button:has-text('Entrar')",
            "button:has-text('Acessar')",
            "button:has-text('Login')",
        ],
    )
    if submit is None:
        raise LancamentoError("Nao foi possivel localizar botao de login no SGE.")

    submit.click(timeout=ACTION_TIMEOUT_MS)

    try:
        page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        # Alguns portais continuam com requests de longa duracao.
        pass

    _log(logger, "Login realizado. Iniciando lancamento...")


def _select_context(page, contexto: ContextoTurma, logger: Optional[LogFn]) -> None:
    _log(logger, f"Selecionando contexto: {contexto.escola} | {contexto.turno} | {contexto.turma} | {contexto.trimestre}")

    textos = [contexto.escola, contexto.turno, contexto.turma, contexto.trimestre]
    for item in textos:
        if item.startswith("Escola nao") or item.startswith("Turno nao"):
            continue
        _click_text(page, item)


def _select_activity(page, atividade: str, logger: Optional[LogFn]) -> None:
    _log(logger, f"Selecionando avaliacao: {atividade}")
    if not _click_text(page, atividade):
        _log(logger, f"Aviso: avaliacao nao encontrada diretamente na tela: {atividade}")


def _fill_grade_for_student(page, aluno: str, nota: float, logger: Optional[LogFn]) -> bool:
    nota_texto = str(nota).replace(".", ",")
    row = page.locator("tr", has_text=aluno)
    if row.count() == 0:
        _log(logger, f"Aviso: aluno nao localizado na grade: {aluno}")
        return False

    inputs = row.first.locator("input[type='text'], input[type='number']")
    if inputs.count() == 0:
        _log(logger, f"Aviso: campo de nota nao encontrado para aluno: {aluno}")
        return False

    try:
        cell = inputs.first
        cell.click(timeout=ACTION_TIMEOUT_MS)
        cell.fill(nota_texto, timeout=ACTION_TIMEOUT_MS)
        return True
    except Exception as exc:  # noqa: BLE001
        _log(logger, f"Erro ao preencher nota de {aluno}: {exc}")
        return False


def _update_launch_status_for_notes(registros: List[RegistroNota], logger: Optional[LogFn]) -> None:
    if not registros:
        return
    if not NOTION_TOKEN:
        _log(logger, "Aviso: NOTION_TOKEN ausente; status de lancamento nao foi atualizado.")
        return

    notion = Client(auth=NOTION_TOKEN)
    atualizados = 0
    falhas = 0
    vistos = set()

    for reg in registros:
        page_id = _normalize_notion_id(reg.notion_page_id)
        status_prop = (reg.notion_status_prop or "").strip()
        if not page_id or not status_prop:
            continue

        chave = (page_id, status_prop)
        if chave in vistos:
            continue
        vistos.add(chave)

        try:
            page = _safe_notion_call(lambda page_id=page_id: notion.pages.retrieve(page_id=page_id))
            props = page.get("properties", {})
            prop_info = props.get(status_prop, {})
            ptype = prop_info.get("type")

            if ptype == "select":
                payload = {status_prop: {"select": {"name": "Lancada"}}}
            elif ptype == "checkbox":
                payload = {status_prop: {"checkbox": True}}
            elif ptype == "rich_text":
                payload = {status_prop: {"rich_text": _make_rich_text("Lancada")}}
            else:
                _log(logger, f"Aviso: propriedade de status nao encontrada/compativel para {reg.aluno}: {status_prop}")
                falhas += 1
                continue

            _safe_notion_call(
                lambda page_id=page_id, payload=payload: notion.pages.update(page_id=page_id, properties=payload)
            )
            atualizados += 1
        except Exception as exc:  # noqa: BLE001
            falhas += 1
            _log(logger, f"Aviso: falha ao atualizar status de lancamento ({reg.aluno}): {exc}")

    _log(logger, f"Status de lancamento atualizado em {atualizados} nota(s). Falhas: {falhas}")


def _confirm_save(page, logger: Optional[LogFn]) -> None:
    submit = _first_visible(
        page,
        [
            "button:has-text('Salvar')",
            "button:has-text('Confirmar')",
            "button:has-text('Lancar')",
            "button:has-text('Gravar')",
        ],
    )
    if submit is None:
        _log(logger, "Aviso: botao de confirmacao nao encontrado; seguindo para o proximo bloco.")
        return

    submit.click(timeout=ACTION_TIMEOUT_MS)
    try:
        page.wait_for_timeout(800)
    except Exception:  # noqa: BLE001
        pass


def _group_for_launch(registros: List[RegistroNota]):
    grouped: Dict[Tuple[str, str, str, str, str], List[RegistroNota]] = defaultdict(list)
    for reg in registros:
        key = (reg.escola, reg.turno, reg.turma, reg.trimestre, reg.atividade)
        grouped[key].append(reg)
    return grouped


def executar_lancamento(
    filtro: Optional[Dict[str, str]] = None,
    logger: Optional[LogFn] = print,
    dry_run: bool = False,
) -> Dict[str, int]:
    if not SGE_CPF or not SGE_SENHA:
        raise LancamentoError("Defina SGE_CPF e SGE_SENHA nas variaveis de ambiente.")
    if _is_placeholder_env(SGE_CPF) or _is_placeholder_env(SGE_SENHA):
        raise LancamentoError("SGE_CPF/SGE_SENHA estao com placeholders. Atualize com valores reais.")

    registros = carregar_notas_notion(logger=logger)
    registros = _filtrar_registros(registros, filtro)

    if not registros:
        raise LancamentoError("Nenhuma nota encontrada para o filtro selecionado.")

    grouped = _group_for_launch(registros)
    total_blocos = len(grouped)
    total_notas = len(registros)
    _log(logger, f"Blocos para lancamento: {total_blocos} | notas: {total_notas}")

    if dry_run:
        _log(logger, "Dry-run habilitado: nenhum dado sera enviado ao SGE.")
        return {
            "blocos": total_blocos,
            "notas": total_notas,
            "notas_preenchidas": 0,
            "falhas": 0,
        }

    notas_ok = 0
    falhas = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(ACTION_TIMEOUT_MS)

        _login_sge(page, logger=logger)

        for idx, (key, itens) in enumerate(grouped.items(), start=1):
            escola, turno, turma, trimestre, atividade = key
            _log(logger, f"[{idx}/{total_blocos}] {escola} | {turno} | {turma} | {trimestre} | {atividade}")

            contexto = ContextoTurma(escola=escola, turno=turno, turma=turma, trimestre=trimestre)
            _select_context(page, contexto, logger=logger)
            _select_activity(page, atividade, logger=logger)

            regs_ok_bloco: List[RegistroNota] = []
            for reg in itens:
                ok = _fill_grade_for_student(page, reg.aluno, reg.nota, logger=logger)
                if ok:
                    notas_ok += 1
                    regs_ok_bloco.append(reg)
                else:
                    falhas += 1

            _confirm_save(page, logger=logger)
            _update_launch_status_for_notes(regs_ok_bloco, logger=logger)

        context.close()
        browser.close()

    _log(logger, f"Finalizado. Notas preenchidas: {notas_ok} | Falhas: {falhas}")
    return {
        "blocos": total_blocos,
        "notas": total_notas,
        "notas_preenchidas": notas_ok,
        "falhas": falhas,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lanca notas do Notion no SGE Indaial")
    parser.add_argument("--escola", default="")
    parser.add_argument("--turno", default="")
    parser.add_argument("--turma", default="")
    parser.add_argument("--trimestre", default="")
    parser.add_argument("--notion-page-id", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--listar-contextos", action="store_true")
    return parser.parse_args()


def _build_filtro(args: argparse.Namespace) -> Dict[str, str]:
    filtro = {
        "escola": args.escola,
        "turno": args.turno,
        "turma": args.turma,
        "trimestre": args.trimestre,
    }
    return {k: v for k, v in filtro.items() if _is_non_empty(v)}


def main() -> int:
    args = _parse_args()
    args.notion_page_id = _normalize_notion_id(args.notion_page_id)
    logs_execucao: List[str] = []

    def logger(msg: str) -> None:
        print(msg)
        logs_execucao.append(msg)

    if not args.notion_page_id and _is_non_empty(args.escola) and _normalize(args.escola) not in {"todas", "todos"}:
        try:
            auto_page_id = _find_pending_request_page_id(args.escola, logger=logger)
            if auto_page_id:
                args.notion_page_id = auto_page_id
                logger("Page ID de solicitacao identificado automaticamente.")
        except Exception as exc:  # noqa: BLE001
            logger(f"Aviso: falha ao buscar Page ID automaticamente: {exc}")

    if args.listar_contextos:
        contextos = listar_contextos_disponiveis(logger=logger)
        if not contextos:
            print("Nenhum contexto encontrado.")
            return 1
        for ctx in contextos:
            print(f"- {ctx['escola']} | {ctx['turno']} | {ctx['turma']} | {ctx['trimestre']}")
        return 0

    filtro = _build_filtro(args)
    if args.notion_page_id:
        atualizar_status_execucao_notion(
            page_id=args.notion_page_id,
            status="Em execucao",
            logger=logger,
            log_text="Execucao iniciada pelo dispatcher.",
            clear_request=False,
        )

    try:
        resultado = executar_lancamento(filtro=filtro, logger=logger, dry_run=args.dry_run)
    except LancamentoError as exc:
        if (
            "Nenhuma nota valida foi encontrada no Notion." in str(exc)
            or "Nenhuma nota encontrada para o filtro selecionado." in str(exc)
        ):
            aviso = "Sem notas validas para lancar no Notion. Encerrando sem alteracoes."
            print(f"Aviso: {aviso}")
            if args.notion_page_id:
                atualizar_status_execucao_notion(
                    page_id=args.notion_page_id,
                    status="Concluido",
                    logger=logger,
                    log_text=aviso,
                    clear_request=True,
                )
            return 0

        print(f"Erro: {exc}")
        if args.notion_page_id:
            atualizar_status_execucao_notion(
                page_id=args.notion_page_id,
                status="Erro",
                logger=logger,
                log_text=f"Erro: {exc}",
                clear_request=True,
            )
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Erro inesperado: {exc}")
        if args.notion_page_id:
            atualizar_status_execucao_notion(
                page_id=args.notion_page_id,
                status="Erro",
                logger=logger,
                log_text=f"Erro inesperado: {exc}",
                clear_request=True,
            )
        return 1

    print("Resumo:")
    print(f"- blocos: {resultado['blocos']}")
    print(f"- notas: {resultado['notas']}")
    print(f"- notas_preenchidas: {resultado['notas_preenchidas']}")
    print(f"- falhas: {resultado['falhas']}")

    if args.notion_page_id:
        resumo = (
            f"Concluido. blocos={resultado['blocos']} notas={resultado['notas']} "
            f"preenchidas={resultado['notas_preenchidas']} falhas={resultado['falhas']}"
        )
        log_text = "\n".join((logs_execucao + [resumo])[-20:])
        atualizar_status_execucao_notion(
            page_id=args.notion_page_id,
            status="Concluido",
            logger=logger,
            log_text=log_text,
            clear_request=True,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
