import argparse
import os
import re

import tempfile
import unicodedata
import urllib.request
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
    _infer_context,
    _is_non_empty,
    _is_notas_database,
    _is_placeholder_env,
    _iter_scopes,
    _list_children,
    _login_sge,
    _normalize,
    _normalize_cpf_for_sge,
    _normalize_notion_id,
    _query_database_rows,
    _resolve_env_credential,
    _safe_notion_call,
    _select_context,
    _set_filters_on_portal,
    _turno_code,
)

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
ROOT_PAGE_ID = os.environ.get("ROOT_PAGE_ID", "")
SGE_CPF = os.environ.get("SGE_CPF", "")
SGE_SENHA = os.environ.get("SGE_SENHA", "")
# ID opcional da database "Sequencias Didaticas - PDFs". Quando definido,
# o script le direto por ID (sem depender de permissao na raiz do Notion).
SEQUENCIAS_DATABASE_ID = os.environ.get("SEQUENCIAS_DATABASE_ID", "").strip()
# IDs das databases de SOLICITACOES das 6 escolas (mesmo formato do
# processar_solicitacoes_github.py). Quando preenchido, o script le
# apenas as databases de NOTAS filhas dessas escolas (sem varrer a raiz
# do Notion inteira). Vazio = fallback para descoberta (mais lento).
ESCOLAS_DATABASE_IDS_RAW = os.environ.get("ESCOLAS_DATABASE_IDS", "").strip()
DEFAULT_ESCOLAS_DATABASE_IDS = [
    "1bcc61e6-e3a8-493d-ad98-45ab49063103",  # Juvenal
    "76179e56-f755-42a2-b5de-e0c56025bd7b",  # Arapongas
    "fb5f3d25-4c2c-4ef9-93a4-2079b17e3bf2",  # Mulde
    "19ac0fd7-442f-4f06-a43f-12de0c1fe396",  # Anna Alves
    "c4eeb930-570e-4acc-a6e5-f6b02c1bd0fd",  # Tancredo
    "ddb3cab5-70dc-4dcb-96c2-5b8e62fb9a57",  # Maria Helena
]  # fmt: skip

SEQUENCIAS_DB_TITLE = "sequencias didaticas - pdfs"

# Mapeamento fixo de turmas por ano. Cada chave eh um valor possivel da
# coluna "Ano" na database de Sequencias. Cada valor eh a lista de
# (escola, turno, turma) que devem receber o lancamento.
# Atualize aqui quando a estrutura de escolas mudar.
TURMAS_POR_ANO: Dict[str, List[Tuple[str, str, str]]] = {
    "6º Ano": [
        ("Juvenal", "Matutino", "6º Ano"),
        ("Arapongas", "Vespertino", "6º Ano"),
        ("Mulde", "Matutino", "6º Ano"),
        ("Anna Alves", "Vespertino", "6º Ano"),
        ("Tancredo", "Matutino", "6º Ano"),
        ("Tancredo", "Vespertino", "6º Ano"),
        ("Maria Helena", "Matutino", "6º Ano"),
        ("Maria Helena", "Vespertino", "6º Ano"),
    ],
    "7º Ano": [
        ("Juvenal", "Matutino", "7º Ano"),
        ("Arapongas", "Vespertino", "7º Ano"),
        ("Mulde", "Matutino", "7º Ano"),
        ("Anna Alves", "Vespertino", "7º Ano"),
        ("Tancredo", "Matutino", "7º Ano"),
        ("Tancredo", "Vespertino", "7º Ano"),
        ("Maria Helena", "Matutino", "7º Ano"),
        ("Maria Helena", "Vespertino", "7º Ano"),
    ],
    "8º Ano": [
        ("Juvenal", "Matutino", "8º Ano"),
        ("Arapongas", "Vespertino", "8º Ano"),
        ("Mulde", "Matutino", "8º Ano"),
        ("Anna Alves", "Vespertino", "8º Ano"),
        ("Tancredo", "Matutino", "8º Ano"),
        ("Tancredo", "Vespertino", "8º Ano"),
        ("Maria Helena", "Vespertino", "8º Ano"),
    ],
    "9º Ano": [
        ("Juvenal", "Matutino", "9º Ano"),
        ("Arapongas", "Vespertino", "9º Ano"),
        ("Mulde", "Matutino", "9º Ano"),
        ("Anna Alves", "Vespertino", "9º Ano"),
        ("Tancredo", "Matutino", "9º Ano"),
        ("Tancredo", "Vespertino", "9º Ano"),
        ("Maria Helena", "Vespertino", "9º Ano"),
    ],
}


@dataclass
class SequenciaRegistro:
    page_id: str
    ano: str
    escola: str
    turno: str
    turma: str
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
    falhas_detalhes: List[str] = None


def _log(logger, msg: str) -> None:
    if logger:
        logger(msg)


def _ano_from_turma(turma: str) -> str:
    m = re.search(r"([6-9])\s*[oº]?\s*ano", turma or "", flags=re.IGNORECASE)
    return f"{m.group(1)}º Ano" if m else ""


def _norm_file_name(name: str) -> str:
    text = (name or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    # Remove acentos para matching tolerante
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text


def _normalize_match(s: str) -> str:
    text = (s or "").strip().lower()
    text = text.replace("º", "o").replace("°", "o").replace("ª", "a")
    text = text.replace("/", " ").replace("-", " ").replace("_", " ")
    text = re.sub(r"\s+", " ", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.strip()


def _fmt_date_ddmmyyyy(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""

    # ja em dd/mm/yyyy
    if re.fullmatch(r"\d{2}/\d{2}/\d{4}", raw):
        return raw

    # yyyy-mm-dd
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        dt = datetime.strptime(raw, "%Y-%m-%d")
        return dt.strftime("%d/%m/%Y")

    return raw


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


def _extract_date_property(props: Dict, names: List[str]) -> str:
    """Extrai a PRIMEIRA data de uma propriedade.

    Aceita tipo 'date' (ISO) ou 'rich_text'/'title' com texto livre no
    formato 'dd/mm a dd/mm' (devolve o inicio) ou 'dd/mm/yyyy' unico.
    """
    for name in names:
        prop = props.get(name, {})
        if not prop:
            continue

        if prop.get("type") == "date":
            node = prop.get("date") or {}
            start = (node.get("start") or "").strip()
            if start:
                return _fmt_date_ddmmyyyy(start)

        # Fallback: rich_text / title / select com texto livre.
        text = _extract_plain_text(prop).strip()
        if text:
            # Padrao "dd/mm(/yyyy)? (a|ate|to|-|/) dd/mm(/yyyy)?" -> inicio
            m = re.match(
                r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\s*(?:a|ate|to|-|/)\s*(\d{1,2})/(\d{1,2})(?:/(\d{4}))?",
                text,
            )
            if m:
                d1, mo1, y1, _d2, _mo2, _y2 = m.groups()
                year = int(y1) if y1 else datetime.now().year
                try:
                    dt = datetime.strptime(f"{int(d1):02d}/{int(mo1):02d}/{year}", "%d/%m/%Y")
                    return dt.strftime("%d/%m/%Y")
                except ValueError:
                    pass

            # Padrao "dd/mm/yyyy" unico.
            m2 = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
            if m2:
                d, mo, y = m2.groups()
                try:
                    dt = datetime.strptime(f"{int(d):02d}/{int(mo):02d}/{y}", "%d/%m/%Y")
                    return dt.strftime("%d/%m/%Y")
                except ValueError:
                    pass
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


def _load_sequencias_from_notion(logger=None) -> List[SequenciaRegistro]:
    root_page_id = _normalize_notion_id(ROOT_PAGE_ID)

    if not NOTION_TOKEN or not root_page_id:
        raise LancamentoError("Defina NOTION_TOKEN e ROOT_PAGE_ID nas variaveis de ambiente.")
    if _is_placeholder_env(NOTION_TOKEN) or _is_placeholder_env(root_page_id):
        raise LancamentoError("NOTION_TOKEN/ROOT_PAGE_ID estao com placeholders. Atualize com valores reais.")

    notion = Client(auth=NOTION_TOKEN)

    # Caminho preferencial: ler a database direto por ID (independe de
    # permissao em paginas ancestrais).
    alvo_id = _normalize_notion_id(SEQUENCIAS_DATABASE_ID) if SEQUENCIAS_DATABASE_ID else ""

    if alvo_id:
        _log(logger, f"Usando SEQUENCIAS_DATABASE_ID direto: {alvo_id}")
        try:
            db_obj = _safe_notion_call(lambda: notion.databases.retrieve(database_id=alvo_id))
        except Exception as exc:  # noqa: BLE001
            raise LancamentoError(
                f"Falha ao acessar database de Sequencias Didaticas por ID direto ({alvo_id}): {exc}"
            )
    else:
        # Fallback: descoberta recursiva a partir da raiz.
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

    rows = _query_database_rows(notion, alvo_id, database_obj=db_obj)
    result: List[SequenciaRegistro] = []

    for row in rows:
        props = row.get("properties", {})
        if not _is_active_row(props):
            continue

        ano = _extract_select_or_text(props, ["Ano"])
        if not ano:
            continue

        # Escola, Turno e Turma NAO sao mais obrigatorios: o mapeamento
        # fixo TURMAS_POR_ANO expande cada linha nos contextos corretos.
        escola = _extract_select_or_text(props, ["Escola"])
        turno = _extract_select_or_text(props, ["Turno"])
        turma = _extract_select_or_text(props, ["Turma"])
        titulo_documento = _extract_select_or_text(props, ["Titulo Documento", "Título Documento"])

        titulo_linha = ""
        for prop in props.values():
            if prop.get("type") == "title":
                titulo_linha = _extract_plain_text(prop).strip()
                if titulo_linha:
                    break
        if not titulo_documento:
            titulo_documento = titulo_linha

        arquivo_nome, arquivo_url = _first_file_from_prop(props.get("Arquivo PDF", {}))

        # Em vez de chamar _extract_date_property duas vezes (que retorna
        # o mesmo inicio), le a coluna 'Periodo' (unica) uma vez e extrai
        # inicio + fim do texto livre "dd/mm a dd/mm".
        texto_periodo = ""
        for nome in ["Periodo", "Período"]:
            prop = props.get(nome, {})
            if prop.get("type") in ("rich_text", "title"):
                t = _extract_plain_text(prop).strip()
                if t:
                    texto_periodo = t
                    break
            elif prop.get("type") == "date":
                node = prop.get("date") or {}
                start = (node.get("start") or "").strip()
                end = (node.get("end") or "").strip()
                if start:
                    periodo_inicio = _fmt_date_ddmmyyyy(start)
                    if end:
                        periodo_fim = _fmt_date_ddmmyyyy(end)

        if texto_periodo:
            datas = []
            pattern = re.compile(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?")
            for m in pattern.finditer(texto_periodo):
                if datas:
                    gap = texto_periodo[datas[-1].end():m.start()]
                    if not re.search(r"\s|[-/]|ate|to|a ", gap, re.IGNORECASE):
                        continue
                datas.append(m)
            if datas:
                first = datas[0]
                d, mo, y = first.group(1), first.group(2), first.group(3)
                year = int(y) if y else datetime.now().year
                try:
                    dt = datetime.strptime(f"{int(d):02d}/{int(mo):02d}/{year}", "%d/%m/%Y")
                    periodo_inicio = dt.strftime("%d/%m/%Y")
                except ValueError:
                    pass
            if len(datas) >= 2:
                last = datas[-1]
                d, mo, y = last.group(1), last.group(2), last.group(3)
                year = int(y) if y else datetime.now().year
                try:
                    dt = datetime.strptime(f"{int(d):02d}/{int(mo):02d}/{year}", "%d/%m/%Y")
                    periodo_fim = dt.strftime("%d/%m/%Y")
                except ValueError:
                    pass

        # Se nao pegou Periodo unico mas tem colunas separadas, usa elas.
        if not periodo_inicio:
            periodo_inicio = _extract_date_property(props, ["Periodo inicio", "Período início"])
        if not periodo_fim:
            periodo_fim = _extract_date_property(props, ["Periodo fim", "Período fim"])

        n_aulas = _extract_number_property(props, ["N aulas", "Nº aulas", "Numero de aulas"])

        # Fallback: se N aulas nao estiver preenchido, calcular dias corridos
        # entre inicio e fim (formula conservadora: 1 aula por dia).
        if n_aulas <= 0 and periodo_inicio and periodo_fim:
            try:
                di = datetime.strptime(periodo_inicio, "%d/%m/%Y")
                df = datetime.strptime(periodo_fim, "%d/%m/%Y")
                diff = (df - di).days + 1
                if diff > 0:
                    n_aulas = diff
                    _log(logger, f"Linha '{titulo_linha or '(sem titulo)'}': N aulas ausente, usando {n_aulas} (dias corridos).")
            except ValueError:
                pass

        # Log detalhado do que faltou (ajuda a diagnosticar schema do Notion).
        missing = []
        if not titulo_documento: missing.append("Titulo Documento")
        if not arquivo_url: missing.append("Arquivo PDF (sem URL)")
        if not periodo_inicio: missing.append("Periodo inicio")
        if not periodo_fim: missing.append("Periodo fim")
        if n_aulas <= 0: missing.append("N aulas")
        if missing:
            _log(logger, f"Linha '{titulo_linha or '(sem titulo)'}' ignorada. Faltando: {', '.join(missing)}.")
            continue

        result.append(
            SequenciaRegistro(
                page_id=row.get("id", ""),
                ano=ano,
                escola=escola,
                turno=turno,
                turma=turma,
                titulo_documento=titulo_documento,
                arquivo_nome=arquivo_nome,
                arquivo_url=arquivo_url,
                periodo_inicio=periodo_inicio,
                periodo_fim=periodo_fim,
                n_aulas=n_aulas,
            )
        )

    if not result:
        raise LancamentoError("Nenhum registro ativo/valido encontrado na database de Sequencias Didaticas.")

    _log(logger, f"Registros de sequencia carregados do Notion: {len(result)}")
    return result


def _escolas_database_ids() -> List[str]:
    if not ESCOLAS_DATABASE_IDS_RAW:
        return list(DEFAULT_ESCOLAS_DATABASE_IDS)
    return [x.strip() for x in ESCOLAS_DATABASE_IDS_RAW.split(",") if x.strip()]


def _gerar_contextos_de_sequencias(
    registros: List["SequenciaRegistro"],
    logger=None,
) -> List["ContextoPlano"]:
    """Constroi contextos a partir do mapeamento fixo TURMAS_POR_ANO.

    Cada linha de registro (vinda da database de Sequencias) tem um
    'ano' (ex.: '6o Ano'). Para cada linha, expande em N contextos
    (escola, turno, turma) usando a tabela hardcoded TURMAS_POR_ANO.

    Isso NAO percorre o Notion - tudo vem do codigo. Sem permissao
    em paginas de escola ou databases de notas.

    Se TURMAS_POR_ANO nao tiver chave para o ano da linha, a linha
    eh pulada com aviso.
    """
    contextos: List[ContextoPlano] = []
    for r in registros:
        # Tenta match exato e normalizado.
        chave = None
        for k in TURMAS_POR_ANO.keys():
            if _normalize(k) == _normalize(r.ano):
                chave = k
                break
        if not chave:
            _log(logger, f"[contexto] Linha '{r.titulo_documento}' (ano='{r.ano}'): ano sem mapeamento em TURMAS_POR_ANO. Pulada.")
            continue
        turmas = TURMAS_POR_ANO[chave]
        _log(logger, f"[contexto] Linha '{r.titulo_documento}' (ano={r.ano}): expandindo em {len(turmas)} turma(s).")
        for escola, turno, turma in turmas:
            contextos.append(
                ContextoPlano(
                    escola=escola,
                    turno=turno,
                    turma=turma,
                    trimestre="",  # preenchido depois a partir do CLI
                )
            )
    _log(logger, f"[contexto] Total de contextos gerados: {len(contextos)}.")
    return contextos


def _filter_contexts(contextos_raw: List[Dict[str, str]], escola: str, trimestre: str) -> List[ContextoPlano]:
    """Filtra contextos por escola e trimestre vindos do CLI.

    Quando `trimestre` do CLI nao bater com o do contexto (vazio),
    considera como match (ja que trimestres vazios serao preenchidos
    depois pelo CLI em executar_lancamento_sequencia).
    """
    filtered: List[ContextoPlano] = []
    for item in contextos_raw:
        ctx = ContextoPlano(
            escola=item.get("escola", ""),
            turno=item.get("turno", ""),
            turma=item.get("turma", ""),
            trimestre=item.get("trimestre", ""),
        )
        if escola and _normalize(escola) not in {"", "todas"} and _normalize(ctx.escola) != _normalize(escola):
            continue
        if trimestre and ctx.trimestre and _normalize(ctx.trimestre) != _normalize(trimestre):
            continue
        filtered.append(ctx)
    return filtered


def _pick_template_for_context(
    registros: List[SequenciaRegistro],
    contexto: ContextoPlano,
    filename_by_ano: Dict[str, str],
    override_inicio: str,
    override_fim: str,
    logger=None,
) -> Optional[SequenciaRegistro]:
    ano = _ano_from_turma(contexto.turma)
    if not ano:
        return None

    candidates = [r for r in registros if _normalize(r.ano) == _normalize(ano)]
    _log(logger, f"[diag] Apos filtro por ano ('{ano}'): {len(candidates)} candidato(s).")
    if not candidates:
        _log(logger, f"[diag] Nenhum registro com ano '{ano}'. Anos disponiveis: {sorted({_normalize(r.ano) for r in registros if r.ano})}")
        return None

    # Prioriza linha da escola quando preenchida na database.
    with_school = [r for r in candidates if r.escola and _normalize(r.escola) == _normalize(contexto.escola)]
    if with_school:
        candidates = with_school
        _log(logger, f"[diag] Apos filtro por escola ('{contexto.escola}'): {len(candidates)} candidato(s).")
    elif not contexto.escola:
        _log(logger, "[diag] Contexto sem escola definida; mantendo todos os candidatos do ano.")
    else:
        without_school = [r for r in candidates if not r.escola]
        if without_school:
            candidates = without_school
            _log(logger, f"[diag] Sem match por escola; usando {len(candidates)} candidato(s) sem escola preenchida.")
        else:
            _log(logger, f"[diag] AVISO: escola preenchida no Notion mas nao bate com contexto ('{contexto.escola}').")
            _log(logger, f"[diag] Escolas disponiveis nos candidatos: {sorted({r.escola for r in candidates if r.escola})}")
            return None

    wanted_file = _norm_file_name(filename_by_ano.get(ano, ""))
    if wanted_file:
        _log(logger, f"[diag] Buscando arquivo '{wanted_file}' entre {len(candidates)} candidato(s).")

        def _file_matches(wanted: str, candidate: str) -> bool:
            cand = _norm_file_name(candidate)
            if not cand:
                return False
            cand_stripped = re.sub(r"\s+ano\.pdf$", "", cand).rstrip()
            cand_stripped = re.sub(r"\.pdf$", "", cand_stripped).rstrip()
            wanted_stripped = re.sub(r"\s+ano\.pdf$", "", wanted).rstrip()
            wanted_stripped = re.sub(r"\.pdf$", "", wanted_stripped).rstrip()
            return (
                cand == wanted
                or cand_stripped == wanted
                or cand == wanted_stripped
                or cand_stripped == wanted_stripped
                or cand.startswith(wanted)
                or wanted.startswith(cand)
            )

        matched = [r for r in candidates if _file_matches(wanted_file, r.arquivo_nome)]
        if matched:
            candidates = matched
            _log(logger, f"[diag] Match por nome de arquivo: {len(candidates)} candidato(s).")
        else:
            # Fallback: tentar match por titulo do documento.
            _log(logger, f"[diag] Nenhum match de arquivo. Nomes disponiveis: {[r.arquivo_nome for r in candidates]}")
            wanted_title = _normalize_match(filename_by_ano.get(ano, ""))
            title_matched = [
                r for r in candidates
                if wanted_title and _normalize_match(r.titulo_documento) == wanted_title
            ]
            if title_matched:
                candidates = title_matched
                _log(logger, f"[diag] Fallback match por titulo: {len(candidates)} candidato(s).")
            else:
                _log(logger, f"[diag] Titulos disponiveis: {[r.titulo_documento for r in candidates]}")
                _log(logger, "[diag] Sem match por arquivo ou titulo; retornando primeiro candidato disponivel.")
                # Retorna o primeiro candidato do ano+escola sem filtro de arquivo.

    chosen = candidates[0]
    _log(logger, f"[diag] Registro escolhido: ano='{chosen.ano}' escola='{chosen.escola}' "
                 f"arquivo='{chosen.arquivo_nome}' titulo='{chosen.titulo_documento}'")
    return SequenciaRegistro(
        page_id=chosen.page_id,
        ano=chosen.ano,
        escola=chosen.escola,
        turno=chosen.turno or contexto.turno,
        turma=chosen.turma or contexto.turma,
        titulo_documento=chosen.titulo_documento,
        arquivo_nome=chosen.arquivo_nome,
        arquivo_url=chosen.arquivo_url,
        periodo_inicio=override_inicio or chosen.periodo_inicio,
        periodo_fim=override_fim or chosen.periodo_fim,
        n_aulas=chosen.n_aulas,
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
    _select_context(page, contexto, logger=logger)

    # Navega para a pagina de Plano de Aulas
    if not _click_any_selector_any_scope(page, [
        "a[onclick*='PLANOAULA' i]",
        "img[alt*='Plano de Aulas' i]",
        "a:has(img[alt*='Plano de Aulas' i])",
        "input[type='image'][alt*='Plano de Aulas' i]",
        "*:has-text('Plano de Aulas')",
    ]):
        page.goto("https://www.sge8147.com.br/hportalplanejamentoaula.aspx", wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

    page.wait_for_timeout(1000)
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

    return True


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
    trimestre: str = "2º Trimestre",
    modo_execucao: str = "por_escola",
    dry_run: bool = False,
    data_inicio: str = "",
    data_fim: str = "",
    arquivo_por_ano: Optional[Dict[str, str]] = None,
    ano: str = "",
    logger=print,
) -> ExecucaoResumo:
    cpf = _resolve_env_credential(SGE_CPF, "SGE_CPF", logger=logger, digits_only=True)
    cpf = _normalize_cpf_for_sge(cpf, logger=logger)
    senha = _resolve_env_credential(SGE_SENHA, "SGE_SENHA", logger=logger, digits_only=False)

    if not cpf or not senha:
        raise LancamentoError("Defina SGE_CPF e SGE_SENHA nas variaveis de ambiente.")
    if _is_placeholder_env(cpf) or _is_placeholder_env(senha):
        raise LancamentoError("SGE_CPF/SGE_SENHA estao com placeholders. Atualize com valores reais.")

    registros = _load_sequencias_from_notion(logger=logger)

    if not registros:
        msg = "Nenhum registro ativo/valido encontrado na database de Sequencias."
        if dry_run:
            _log(logger, f"DRY-RUN: {msg} Nada a fazer.")
            return ExecucaoResumo(contextos_total=0)
        raise LancamentoError(msg)

    # Filtro explicito por ano (CLI --ano).
    if ano and _normalize(ano) not in {"", "todos"}:
        antes = len(registros)
        registros = [r for r in registros if _normalize(r.ano) == _normalize(ano)]
        _log(logger, f"Filtro --ano={ano}: {len(registros)}/{antes} registros.")
    else:
        _log(logger, f"Anos com template de sequencia: {sorted({_normalize(r.ano) for r in registros if r.ano})}")

    # Constroi contextos direto da database (cada linha = 1 contexto).
    contextos_raw = _gerar_contextos_de_sequencias(registros, logger=logger)

    # Aplica filtro de escola/trimestre vindos do CLI.
    contextos = _filter_contexts(
        [
            {"escola": c.escola, "turno": c.turno, "turma": c.turma, "trimestre": c.trimestre}
            for c in contextos_raw
        ],
        escola=escola,
        trimestre=trimestre,
    )

    # Preenche trimestre do CLI nos contextos (ja que vem da database, nao do titulo).
    for c in contextos:
        if not c.trimestre:
            c.trimestre = trimestre

    if not contextos:
        msg = (
            f"Nenhum contexto valido. Registros lidos: {len(registros)}. "
            "Verifique se cada linha da database tem Escola, Turno e Turma preenchidos."
        )
        if dry_run:
            _log(logger, f"DRY-RUN: {msg} Nada a fazer.")
            return ExecucaoResumo(contextos_total=0)
        raise LancamentoError(msg)

    _log(logger, f"Total de contextos a processar: {len(contextos)}.")
    _log(
        logger,
        f"[diag] Registros disponiveis para match: {len(registros)}. "
        f"Primeiro registro: ano='{registros[0].ano}' escola='{registros[0].escola}' "
        f"arquivo_nome='{registros[0].arquivo_nome}' "
        f"titulo='{registros[0].titulo_documento}'."
    )
    _log(
        logger,
        f"[diag] filename_by_ano passado ao pick: " + ", ".join(
            f"{k}='{v}'" for k, v in (arquivo_por_ano or {}).items()
        ) + "."
    )

    if modo_execucao == "por_turma_em_todas_as_escolas":
        contextos = sorted(contextos, key=lambda c: (_ano_from_turma(c.turma), _normalize(c.turma), _normalize(c.escola), _normalize(c.turno)))
    else:
        contextos = sorted(contextos, key=lambda c: (_normalize(c.escola), _normalize(c.turno), _ano_from_turma(c.turma), _normalize(c.turma)))

    resumo = ExecucaoResumo(contextos_total=len(contextos), falhas_detalhes=[])

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(ACTION_TIMEOUT_MS)

        _login_sge(page, cpf=cpf, senha=senha, logger=logger)

        for idx, ctx in enumerate(contextos, start=1):
            turma_label = f"{ctx.escola} | {ctx.turno} | {ctx.turma} | {ctx.trimestre}"
            _log(logger, f"[{idx}/{len(contextos)}] Processando {turma_label}")

            try:
                registro = _pick_template_for_context(
                    registros,
                    contexto=ctx,
                    filename_by_ano=arquivo_por_ano or {},
                    override_inicio=_fmt_date_ddmmyyyy(data_inicio),
                    override_fim=_fmt_date_ddmmyyyy(data_fim),
                    logger=logger,
                )

                if not registro:
                    msg = f"Nenhum template de sequencia encontrado para {turma_label}"
                    _log(logger, f"Aviso: {msg}.")
                    resumo.falhas += 1
                    resumo.falhas_detalhes.append(msg)
                    continue

                ok_plan, ok_anexo, ok_sit = _executar_fluxo_plano_aulas(
                    page,
                    contexto=ctx,
                    registro=registro,
                    dry_run=dry_run,
                    logger=logger,
                )
                if ok_plan:
                    resumo.planejamentos_criados += 1
                if ok_anexo:
                    resumo.anexos_enviados += 1
                if ok_sit:
                    resumo.situacoes_ativadas += 1
            except PlaywrightTimeoutError as exc:
                msg = f"Timeout em {turma_label}: {exc}"
                resumo.falhas += 1
                resumo.falhas_detalhes.append(msg)
                _log(logger, msg)
                _click_inicio(page)
            except Exception as exc:  # noqa: BLE001
                msg = f"Falha em {turma_label}: {exc}"
                resumo.falhas += 1
                resumo.falhas_detalhes.append(msg)
                _log(logger, msg)
                _click_inicio(page)

        context.close()
        browser.close()

    _log(logger, "--- Resumo da execucao ---")
    _log(logger, f"Contextos processados: {resumo.contextos_total}")
    _log(logger, f"Planejamentos criados: {resumo.planejamentos_criados}")
    _log(logger, f"Anexos enviados: {resumo.anexos_enviados}")
    _log(logger, f"Situacoes ativadas: {resumo.situacoes_ativadas}")
    _log(logger, f"Falhas: {resumo.falhas}")
    if resumo.falhas_detalhes:
        _log(logger, "Detalhes das falhas:")
        for detail in resumo.falhas_detalhes:
            _log(logger, f"  - {detail}")

    return resumo


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lanca sequencia didatica (Plano de Aulas) no SGE")
    parser.add_argument("--escola", default="")
    parser.add_argument("--trimestre", default="2º Trimestre")
    parser.add_argument("--ano", default="", help="Filtra por ano (ex.: '6º Ano'). Vazio = todos com template.")
    parser.add_argument("--modo-execucao", default="por_escola", choices=["por_escola", "por_turma_em_todas_as_escolas"])
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
            trimestre=args.trimestre,
            modo_execucao=args.modo_execucao,
            dry_run=args.dry_run,
            data_inicio=args.data_inicio,
            data_fim=args.data_fim,
            arquivo_por_ano=arquivo_por_ano,
            ano=args.ano,
            logger=print,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Erro: {exc}")
        return 1

    print("--- Resumo sequencia didatica ---")
    print(f"Contextos processados: {resumo.contextos_total}")
    print(f"Planejamentos criados: {resumo.planejamentos_criados}")
    print(f"Anexos enviados: {resumo.anexos_enviados}")
    print(f"Situacoes ativadas: {resumo.situacoes_ativadas}")
    print(f"Falhas: {resumo.falhas}")
    if resumo.falhas_detalhes:
        print("Detalhes das falhas:")
        for detail in resumo.falhas_detalhes:
            print(f"  - {detail}")
    # dry-run: exit 0 mesmo com falhas (relatorio completo)
    if args.dry_run:
        return 0
    return 0 if resumo.falhas == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
