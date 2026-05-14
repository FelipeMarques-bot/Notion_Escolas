import argparse
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from notion_client import Client
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

LogFn = Callable[[str], None]

load_dotenv(override=True)

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
ROOT_PAGE_ID = os.environ.get("ROOT_PAGE_ID", "")
SGE_CPF = os.environ.get("SGE_CPF", "")
SGE_SENHA = os.environ.get("SGE_SENHA", "")
SGE_LOGIN_URL = os.environ.get("SGE_LOGIN_URL", "https://www.sge8147.com.br/")
HEADLESS = os.environ.get("HEADLESS", "1") == "1"
NAV_TIMEOUT_MS = int(os.environ.get("NAV_TIMEOUT_MS", "35000"))
ACTION_TIMEOUT_MS = int(os.environ.get("ACTION_TIMEOUT_MS", "9000"))
NOTION_STATUS_PROP = os.environ.get("NOTION_STATUS_PROP", "Status lancamento")
NOTION_LAST_RUN_PROP = os.environ.get("NOTION_LAST_RUN_PROP", "Ultima execucao")
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
    return re.sub(r"\s+", " ", s.strip().lower())


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


def _safe_notion_call(fn):
    retry = 4
    wait = 1.2
    last = None
    for idx in range(1, retry + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
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


def _discover_databases(notion: Client, root_page_id: str) -> List[Tuple[str, List[str], str]]:
    queue: List[Tuple[str, List[str]]] = [(root_page_id, ["ROOT"])]
    visited_pages = set()
    databases: List[Tuple[str, List[str], str]] = []

    while queue:
        page_id, breadcrumb = queue.pop(0)
        if page_id in visited_pages:
            continue
        visited_pages.add(page_id)

        children = _list_children(notion, page_id)
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
    if not NOTION_TOKEN or not ROOT_PAGE_ID:
        raise LancamentoError("Defina NOTION_TOKEN e ROOT_PAGE_ID nas variaveis de ambiente.")
    if _is_placeholder_env(NOTION_TOKEN) or _is_placeholder_env(ROOT_PAGE_ID):
        raise LancamentoError("NOTION_TOKEN/ROOT_PAGE_ID estao com placeholders. Atualize com valores reais.")

    notion = Client(auth=NOTION_TOKEN)
    _log(logger, "Conectando ao Notion e descobrindo databases...")
    databases = _discover_databases(notion, ROOT_PAGE_ID)

    if not databases:
        raise LancamentoError("Nenhuma database foi encontrada a partir de ROOT_PAGE_ID.")

    registros: List[RegistroNota] = []

    for db_id, breadcrumb, db_title in databases:
        try:
            db_obj = _safe_notion_call(lambda: notion.databases.retrieve(database_id=db_id))
        except Exception as exc:  # noqa: BLE001
            _log(logger, f"Aviso: falha ao ler metadata da database {db_id}: {exc}")
            continue

        title = _database_title(db_obj) or db_title
        if not _is_notas_database(title):
            continue
        context = _infer_context([*breadcrumb, title])
        try:
            rows = _query_database_rows(notion, db_id, database_obj=db_obj)
        except Exception as exc:  # noqa: BLE001
            _log(logger, f"Aviso: pulando database inacessivel {title or db_id}: {exc}")
            continue

        if not rows:
            continue

        _log(logger, f"Database {title or db_id}: {len(rows)} alunos encontrados")

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

    if not NOTION_TOKEN or not ROOT_PAGE_ID:
        raise LancamentoError("Defina NOTION_TOKEN e ROOT_PAGE_ID nas variaveis de ambiente.")

    notion = Client(auth=NOTION_TOKEN)
    _log(logger, "Nenhuma nota valida encontrada. Listando contextos pela estrutura das databases...")
    databases = _discover_databases(notion, ROOT_PAGE_ID)

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
    _log(logger, "Abrindo pagina de login do SGE...")
    page.goto(SGE_LOGIN_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

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

            for reg in itens:
                ok = _fill_grade_for_student(page, reg.aluno, reg.nota, logger=logger)
                if ok:
                    notas_ok += 1
                else:
                    falhas += 1

            _confirm_save(page, logger=logger)

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
    logs_execucao: List[str] = []

    def logger(msg: str) -> None:
        print(msg)
        logs_execucao.append(msg)

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
        if "Nenhuma nota valida foi encontrada no Notion." in str(exc):
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
