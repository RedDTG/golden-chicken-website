import json
import math
import os

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

import db

app = FastAPI(title="Golden Chicken")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def _tojson(value):
    # Jinja2 (contrairement a Flask) n'a pas de filtre tojson integre -
    # celui-ci echappe aussi "</" pour qu'un nom de joueur contenant
    # "</script>" ne puisse pas casser hors de la balise <script> du
    # template, et renvoie du Markup pour que l'autoescape HTML (actif par
    # defaut sur les templates .html) ne ré-echappe pas les guillemets du
    # JSON en &#34; (ce qui casserait le JS cote navigateur).
    return Markup(json.dumps(value).replace("</", "<\\/"))


templates.env.filters["tojson"] = _tojson


def static_version(filename):
    # Horodatage du fichier lu a chaque requete (pas au demarrage) pour que
    # le navigateur revoit un CSS modifie meme sans redemarrer l'app -
    # sinon le cache navigateur peut garder indefiniment un ancien style.css
    # (deja arrive : une modif de couleur invisible tant que ce param ne
    # changeait pas dans l'URL).
    try:
        return int(os.path.getmtime(os.path.join("static", filename)))
    except OSError:
        return 0


templates.env.globals["static_version"] = static_version

PAGE_SIZE = 50
FARMING_WINDOWS = [("1h", 1), ("5h", 5), ("12h", 12), ("24h", 24)]


@app.get("/")
def farming(request: Request):
    with db.get_connection() as conn:
        dashboard = db.farming_dashboard(conn, FARMING_WINDOWS)

    return templates.TemplateResponse(
        "farming.html",
        {
            "request": request,
            "active": "farming",
            "windows": FARMING_WINDOWS,
            "columns": dashboard["columns"],
            "ladder": dashboard["ladder"],
            "record": dashboard["record"],
        },
    )


@app.get("/classement")
def overview(request: Request, sort: str = "total", dir: str = "desc"):
    with db.get_connection() as conn:
        rows = db.build_overview(conn)
        rows = db.sort_overview(rows, sort, dir)
        records = db.fetch_records(conn, limit=5)

    totals = {
        "total": sum(r["total"] for r in rows),
        "wins": sum(r["wins"] for r in rows),
        "losses": sum(r["losses"] for r in rows),
        "profit": sum(r["profit"] for r in rows),
    }
    totals["winrate"] = (totals["wins"] / totals["total"]) if totals["total"] else 0

    return templates.TemplateResponse(
        "overview.html",
        {
            "request": request,
            "active": "overview",
            "rows": rows,
            "totals": totals,
            "records": records,
            "sort": sort,
            "dir": dir,
        },
    )


@app.get("/historique")
def historique(request: Request, player: str = "", result: str = "", page: int = 1):
    page = max(page, 1)
    with db.get_connection() as conn:
        rows, total = db.fetch_history(conn, player or None, result or None, page, PAGE_SIZE)

    pages = max(math.ceil(total / PAGE_SIZE), 1)
    return templates.TemplateResponse(
        "historique.html",
        {
            "request": request,
            "active": "historique",
            "player": player,
            "result": result,
            "rows": rows,
            "total": total,
            "page": page,
            "pages": pages,
        },
    )


GRANULARITIES = ("day", "week", "month")


@app.get("/graphique")
def graphique(request: Request, player: str = "", granularity: str = "day", start: str = "", end: str = ""):
    granularity = granularity if granularity in GRANULARITIES else "day"
    with db.get_connection() as conn:
        min_date, max_date = db.fetch_date_bounds(conn)
        start = start or min_date or ""
        end = end or max_date or ""
        rows = db.fetch_best_streak_series(conn, granularity, player or None, start or None, end or None)

    points = [
        {
            "label": db.format_bucket_label(row["bucket"], granularity),
            "value": row["strength"],
            "player": row["player"],
            "timestamp": row["timestamp"],
        }
        for row in rows
    ]

    return templates.TemplateResponse(
        "graphique.html",
        {
            "request": request,
            "active": "graphique",
            "player": player,
            "granularity": granularity,
            "start": start,
            "end": end,
            "min_date": min_date or "",
            "max_date": max_date or "",
            "points": points,
        },
    )


@app.get("/solde")
def solde(request: Request, player: str = "", page: int = 1):
    page = max(page, 1)
    with db.get_connection() as conn:
        rows, total = db.fetch_bank(conn, player or None, page, PAGE_SIZE)

    pages = max(math.ceil(total / PAGE_SIZE), 1)
    return templates.TemplateResponse(
        "solde.html",
        {
            "request": request,
            "active": "solde",
            "player": player,
            "rows": rows,
            "total": total,
            "page": page,
            "pages": pages,
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
