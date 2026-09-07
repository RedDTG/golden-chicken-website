import math
import os

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import db

app = FastAPI(title="Golden Chicken")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


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
        players = []
        for player in db.FARMING_PLAYERS:
            streaks = {label: db.best_streak(conn, player, db.since_hours(hours)) for label, hours in FARMING_WINDOWS}
            streaks["all"] = db.best_streak(conn, player)
            players.append({"name": player, "streaks": streaks, **db.player_row_stats(conn, player)})

        all_streaks = {label: db.best_streak(conn, None, db.since_hours(hours)) for label, hours in FARMING_WINDOWS}
        all_streaks["all"] = db.best_streak(conn)
        all_row = {"name": "All", "streaks": all_streaks, **db.player_row_stats(conn)}

        record = db.farming_record(conn)
        ladder = db.farming_ladder(conn, all_row["total"], start_pct=record["strength"] if record else 50)

    return templates.TemplateResponse(
        "farming.html",
        {
            "request": request,
            "active": "farming",
            "windows": FARMING_WINDOWS,
            "columns": players + [all_row],
            "ladder": ladder,
            "record": record,
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
