from notion_client import Client
import os

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "seu_token_aqui")
ROOT_PAGE_ID = os.environ.get("ROOT_PAGE_ID", "id_da_pagina_raiz")

notion = Client(auth=NOTION_TOKEN)

ESCOLAS = [
    {"nome": "Juvenal",      "turnos": ["Matutino"],               "emoji": "🏫"},
    {"nome": "Arapongas",    "turnos": ["Vespertino"],             "emoji": "🏫"},
    {"nome": "Mulde",        "turnos": ["Matutino"],               "emoji": "🏫"},
    {"nome": "Anna Alves",   "turnos": ["Vespertino"],             "emoji": "🏫"},
    {"nome": "Tancredo",     "turnos": ["Matutino", "Vespertino"], "emoji": "🏫"},
    {"nome": "Maria Helena", "turnos": ["Matutino", "Vespertino"], "emoji": "🏫"},
]

TURMAS = ["6º Ano", "7º Ano", "8º Ano", "9º Ano"]
TRIMESTRES = ["1º Trimestre", "2º Trimestre", "3º Trimestre"]

def icone_turno(turno):
    return {"Matutino": "🌅", "Vespertino": "🌤️"}.get(turno, "📚")

def icone_turma(turma):
    return {"6º Ano": "6️⃣", "7º Ano": "7️⃣", "8º Ano": "8️⃣", "9º Ano": "9️⃣"}.get(turma, "📚")

def icone_trimestre(trimestre):
    return {"1º Trimestre": "1️⃣", "2º Trimestre": "2️⃣", "3º Trimestre": "3️⃣"}.get(trimestre, "📋")

def blocos_pagina_escola(nome, turnos):
    turnos_str = " | ".join([f"{icone_turno(t)} {t}" for t in turnos])
    return [
        {"object": "block", "type": "callout", "callout": {
            "rich_text": [{"type": "text", "text": {"content": f"Turnos disponíveis: {turnos_str}"}}],
            "icon": {"type": "emoji", "emoji": "🏫"},
            "color": "blue_background",
        }},
        {"object": "block", "type": "divider", "divider": {}},
        {"object": "block", "type": "heading_2", "heading_2": {
            "rich_text": [{"type": "text", "text": {"content": "📂 Turnos e Turmas"}}]
        }},
        {"object": "block", "type": "paragraph", "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": "Navegue pelos turnos abaixo para acessar as turmas, trimestres e avaliações dos alunos."}}]
        }},
    ]

def blocos_pagina_turno(turno, nome_escola):
    return [
        {"object": "block", "type": "callout", "callout": {
            "rich_text": [{"type": "text", "text": {"content": f"{nome_escola} — Turno {turno}"}}],
            "icon": {"type": "emoji", "emoji": icone_turno(turno)},
            "color": "yellow_background",
        }},
        {"object": "block", "type": "divider", "divider": {}},
        {"object": "block", "type": "heading_2", "heading_2": {
            "rich_text": [{"type": "text", "text": {"content": "🎓 Turmas"}}]
        }},
    ]

def blocos_pagina_turma(turma, turno, nome_escola):
    return [
        {"object": "block", "type": "callout", "callout": {
            "rich_text": [{"type": "text", "text": {"content": f"{nome_escola} | {turno} | {turma}"}}],
            "icon": {"type": "emoji", "emoji": icone_turma(turma)},
            "color": "green_background",
        }},
        {"object": "block", "type": "divider", "divider": {}},
        {"object": "block", "type": "heading_2", "heading_2": {
            "rich_text": [{"type": "text", "text": {"content": "📅 Trimestres"}}]
        }},
        {"object": "block", "type": "paragraph", "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": "Cada trimestre contém 3 avaliações. Clique no trimestre desejado para visualizar ou lançar notas."}}]
        }},
    ]

def blocos_pagina_trimestre(trimestre, turma, turno, nome_escola):
    return [
        {"object": "block", "type": "callout", "callout": {
            "rich_text": [{"type": "text", "text": {"content": f"{nome_escola} | {turno} | {turma} | {trimestre}"}}],
            "icon": {"type": "emoji", "emoji": icone_trimestre(trimestre)},
            "color": "purple_background",
        }},
        {"object": "block", "type": "divider", "divider": {}},
        {"object": "block", "type": "heading_2", "heading_2": {
            "rich_text": [{"type": "text", "text": {"content": "📝 Lançamento de Avaliações"}}]
        }},
        {"object": "block", "type": "paragraph", "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": "Preencha abaixo as 3 avaliações do trimestre para cada aluno."}}]
        }},
        {"object": "block", "type": "callout", "callout": {
            "rich_text": [{"type": "text", "text": {"content": "💡 Para renomear uma atividade, clique no nome da coluna no database abaixo e edite diretamente."}}],
            "icon": {"type": "emoji", "emoji": "💡"},
            "color": "gray_background",
        }},
    ]

def criar_database_alunos(parent_page_id, titulo):
    response = notion.databases.create(
        parent={"page_id": parent_page_id},
        icon={"type": "emoji", "emoji": "📊"},
        title=[{"type": "text", "text": {"content": titulo}}],
        properties={
            "Nome do Aluno": {"title": {}},
            "Número": {"number": {"format": "number"}},
            "Atividade 1": {"rich_text": {}},
            "Nota — Atividade 1": {"number": {"format": "number"}},
            "Data — Atividade 1": {"date": {}},
            "Status — Atividade 1": {"select": {"options": [
                {"name": "✅ Entregue",          "color": "green"},
                {"name": "❌ Não Entregue",      "color": "red"},
                {"name": "🚫 Faltou",            "color": "orange"},
                {"name": "📋 Falta Justificada", "color": "yellow"},
            ]}},
            "Atividade 2": {"rich_text": {}},
            "Nota — Atividade 2": {"number": {"format": "number"}},
            "Data — Atividade 2": {"date": {}},
            "Status — Atividade 2": {"select": {"options": [
                {"name": "✅ Entregue",          "color": "green"},
                {"name": "❌ Não Entregue",      "color": "red"},
                {"name": "🚫 Faltou",            "color": "orange"},
                {"name": "📋 Falta Justificada", "color": "yellow"},
            ]}},
            "Atividade 3": {"rich_text": {}},
            "Nota — Atividade 3": {"number": {"format": "number"}},
            "Data — Atividade 3": {"date": {}},
            "Status — Atividade 3": {"select": {"options": [
                {"name": "✅ Entregue",          "color": "green"},
                {"name": "❌ Não Entregue",      "color": "red"},
                {"name": "🚫 Faltou",            "color": "orange"},
                {"name": "📋 Falta Justificada", "color": "yellow"},
            ]}},
            "Média do Trimestre": {"formula": {
                "expression": "round((prop(\"Nota — Atividade 1\") + prop(\"Nota — Atividade 2\") + prop(\"Nota — Atividade 3\")) / 3 * 10) / 10"
            }},
            "Observações": {"rich_text": {}},
        },
    )
    return response["id"]

def criar_pagina(parent_id, titulo, emoji, blocos, is_database_child=False):
    parent = {"database_id": parent_id} if is_database_child else {"page_id": parent_id}
    response = notion.pages.create(
        parent=parent,
        icon={"type": "emoji", "emoji": emoji},
        properties={"title": [{"type": "text", "text": {"content": titulo}}]},
        children=blocos,
    )
    return response["id"]

def criar_estrutura_completa():
    for escola in ESCOLAS:
        nome_escola = escola["nome"]
        turnos = escola["turnos"]
        print(f"\n🏫 Criando escola: {nome_escola}")

        escola_page_id = criar_pagina(ROOT_PAGE_ID, nome_escola, "🏫", blocos_pagina_escola(nome_escola, turnos))

        for turno in turnos:
            print(f"  {icone_turno(turno)} Turno: {turno}")
            turno_page_id = criar_pagina(escola_page_id, f"{turno} — {nome_escola}", icone_turno(turno), blocos_pagina_turno(turno, nome_escola))

            for turma in TURMAS:
                print(f"    {icone_turma(turma)} Turma: {turma}")
                turma_page_id = criar_pagina(turno_page_id, turma, icone_turma(turma), blocos_pagina_turma(turma, turno, nome_escola))

                for trimestre in TRIMESTRES:
                    print(f"      {icone_trimestre(trimestre)} {trimestre}")
                    trimestre_page_id = criar_pagina(turma_page_id, trimestre, icone_trimestre(trimestre), blocos_pagina_trimestre(trimestre, turma, turno, nome_escola))
                    criar_database_alunos(trimestre_page_id, f"Alunos — {turma} | {turno} | {trimestre}")

    print("\n🎉 Estrutura completa criada com sucesso no Notion!")

if __name__ == "__main__":
    criar_estrutura_completa()
