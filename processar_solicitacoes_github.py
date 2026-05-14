import os
from typing import Dict, List, Optional

from notion_client import Client

from lancar_notas_sge import (
    LancamentoError,
    atualizar_status_execucao_notion,
    executar_lancamento,
)

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

DEFAULT_DATABASE_IDS = [
    "1bcc61e6-e3a8-493d-ad98-45ab49063103",  # Juvenal
    "76179e56-f755-42a2-b5de-e0c56025bd7b",  # Arapongas
    "fb5f3d25-4c2c-4ef9-93a4-2079b17e3bf2",  # Mulde
    "19ac0fd7-442f-4f06-a43f-12de0c1fe396",  # Anna Alves
    "c4eeb930-570e-4acc-a6e5-f6b02c1bd0fd",  # Tancredo
    "ddb3cab5-70dc-4dcb-96c2-5b8e62fb9a57",  # Maria Helena
]


def _database_ids() -> List[str]:
    raw = os.environ.get("SOLICITACOES_DATABASE_IDS", "")
    if not raw.strip():
        return DEFAULT_DATABASE_IDS
    return [x.strip() for x in raw.split(",") if x.strip()]


def _extract_data_source_id(database_obj: Dict) -> Optional[str]:
    data_sources = database_obj.get("data_sources", [])
    if not data_sources:
        return None
    first = data_sources[0]
    return first.get("id") if isinstance(first, dict) else None


def _prop_rich_text(props: Dict, name: str) -> str:
    prop = props.get(name, {})
    if prop.get("type") != "rich_text":
        return ""
    return "".join(x.get("plain_text", "") for x in prop.get("rich_text", [])).strip()


def _prop_checkbox(props: Dict, name: str) -> bool:
    prop = props.get(name, {})
    if prop.get("type") != "checkbox":
        return False
    return bool(prop.get("checkbox", False))


def _prop_select(props: Dict, name: str) -> str:
    prop = props.get(name, {})
    if prop.get("type") != "select":
        return ""
    node = prop.get("select")
    return "" if not node else str(node.get("name", "")).strip()


def _pending_requests(notion: Client, database_id: str) -> List[Dict[str, str]]:
    database_obj = notion.databases.retrieve(database_id=database_id)
    data_source_id = _extract_data_source_id(database_obj)
    if not data_source_id:
        return []

    requests: List[Dict[str, str]] = []
    cursor = None
    while True:
        if cursor:
            response = notion.data_sources.query(data_source_id=data_source_id, start_cursor=cursor, page_size=100)
        else:
            response = notion.data_sources.query(data_source_id=data_source_id, page_size=100)

        for page in response.get("results", []):
            props = page.get("properties", {})
            solicitar = _prop_checkbox(props, "Solicitar lancamento")
            status = _prop_select(props, "Status lancamento")
            escola = _prop_rich_text(props, "Escola")

            if solicitar and status in {"", "Pendente"}:
                if not escola:
                    escola = ""
                requests.append({"page_id": page["id"], "escola": escola})

        if not response.get("has_more"):
            break
        cursor = response.get("next_cursor")

    return requests


def main() -> int:
    if not NOTION_TOKEN:
        print("Erro: NOTION_TOKEN nao definido.")
        return 1

    notion = Client(auth=NOTION_TOKEN)
    db_ids = _database_ids()

    all_requests: List[Dict[str, str]] = []
    for db_id in db_ids:
        try:
            reqs = _pending_requests(notion, db_id)
            all_requests.extend(reqs)
            if reqs:
                print(f"Database {db_id}: {len(reqs)} solicitacao(oes) pendente(s)")
        except Exception as exc:  # noqa: BLE001
            print(f"Aviso: falha ao consultar database {db_id}: {exc}")

    if not all_requests:
        print("Nenhuma solicitacao pendente encontrada.")
        return 0

    for req in all_requests:
        page_id = req["page_id"]
        escola = req["escola"]

        if not escola:
            atualizar_status_execucao_notion(
                page_id=page_id,
                status="Erro",
                log_text="Campo Escola vazio na solicitacao.",
                clear_request=True,
            )
            print(f"Solicitacao {page_id}: erro por campo Escola vazio")
            continue

        atualizar_status_execucao_notion(
            page_id=page_id,
            status="Em execucao",
            log_text=f"Processando escola {escola}",
            clear_request=False,
        )

        try:
            result = executar_lancamento(filtro={"escola": escola}, logger=print, dry_run=DRY_RUN)
            resumo = (
                f"Concluido ({escola}). blocos={result['blocos']} notas={result['notas']} "
                f"preenchidas={result['notas_preenchidas']} falhas={result['falhas']}"
            )
            atualizar_status_execucao_notion(
                page_id=page_id,
                status="Concluido",
                log_text=resumo,
                clear_request=True,
            )
            print(resumo)
        except LancamentoError as exc:
            if (
                "Nenhuma nota valida foi encontrada no Notion." in str(exc)
                or "Nenhuma nota encontrada para o filtro selecionado." in str(exc)
            ):
                atualizar_status_execucao_notion(
                    page_id=page_id,
                    status="Concluido",
                    log_text=f"Sem notas para lancamento em {escola}.",
                    clear_request=True,
                )
                print(f"Sem notas para {escola}.")
                continue

            atualizar_status_execucao_notion(
                page_id=page_id,
                status="Erro",
                log_text=f"Erro: {exc}",
                clear_request=True,
            )
            print(f"Erro em {escola}: {exc}")
        except Exception as exc:  # noqa: BLE001
            atualizar_status_execucao_notion(
                page_id=page_id,
                status="Erro",
                log_text=f"Erro inesperado: {exc}",
                clear_request=True,
            )
            print(f"Erro inesperado em {escola}: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
