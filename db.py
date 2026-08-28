"""Acces en lecture seule a la base SQLite (bot.db) de golden-chicken-stats.

Cette base est la source de verite du bot de logging (voir son CLAUDE.md,
section "SQLite as source of truth") : le site ne fait qu'y lire, jamais
d'ecriture. En production, DB_PATH doit pointer vers le meme fichier/volume
que le bot golden-chicken-stats.
"""

import os
import sqlite3
from contextlib import contextmanager

from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "bot.db")

RESULT_VICTOIRE = "Victoire"
RESULT_DEFAITE = "Defaite"


@contextmanager
def get_connection():
    # mode=ro : le site ne doit jamais pouvoir modifier la base du bot.
    # Compatible avec des ecritures concurrentes cote bot grace au WAL mode
    # que golden-chicken-stats active deja sur ce fichier.
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _parse_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def fetch_stats_rows(conn):
    return conn.execute(
        "SELECT message_id, timestamp, player, result, gain, strength, player_id "
        "FROM stats ORDER BY timestamp ASC"
    ).fetchall()


def build_overview(conn):
    """Reproduit le calcul de l'onglet Overview du Google Sheet (voir
    _update_overview_impl dans golden-chicken-stats/bot.py) : regroupement
    par ID Discord quand connu, sinon par pseudo en minuscules pour les
    lignes historiques anterieures a l'ajout de la colonne ID Joueur.
    """
    rows = fetch_stats_rows(conn)
    groups = {}
    order = []
    for row in rows:
        player_id = row["player_id"] or ""
        key = f"id:{player_id}" if player_id else f"name:{row['player'].lower()}"
        if key not in groups:
            groups[key] = {
                "player_id": player_id,
                "display_name": row["player"],
                "total": 0,
                "wins": 0,
                "losses": 0,
                "best_strength": None,
                "profit": 0,
            }
            order.append(key)
        g = groups[key]
        g["display_name"] = row["player"]  # les lignes sont triees par date croissante -> garde le pseudo le plus recent
        g["total"] += 1
        if row["result"] == RESULT_VICTOIRE:
            g["wins"] += 1
            strength = _parse_int(row["strength"])
            if strength is not None and (g["best_strength"] is None or strength > g["best_strength"]):
                g["best_strength"] = strength
        elif row["result"] == RESULT_DEFAITE:
            g["losses"] += 1
        gain = _parse_int(row["gain"])
        if gain is not None:
            g["profit"] += gain

    # Reconciliation : les combats loggues avant que golden-chicken-stats ait
    # pu resoudre l'ID Discord du joueur (voir son CLAUDE.md, section
    # "Overview sheet") atterrissent dans un groupe name:{pseudo} distinct du
    # groupe id:{player_id} qui recoit ses combats une fois l'ID connu - un
    # meme joueur se retrouverait sinon affiche deux fois dans le classement.
    # On fusionne un groupe orphelin (sans ID) dans le groupe avec ID qui
    # partage le meme pseudo, uniquement quand la correspondance est unique -
    # deux joueurs distincts peuvent en theorie partager le meme pseudo, donc
    # on ne devine pas en cas d'ambiguite (meme niveau de confiance que le
    # reste de ce fichier/de golden-chicken-stats sur les pseudos).
    id_groups_by_name = {}
    for key in order:
        if key.startswith("id:"):
            id_groups_by_name.setdefault(groups[key]["display_name"].lower(), []).append(key)

    merged_away = set()
    for key in order:
        if not key.startswith("name:"):
            continue
        candidates = id_groups_by_name.get(groups[key]["display_name"].lower(), [])
        if len(candidates) != 1:
            continue
        target = groups[candidates[0]]
        orphan = groups[key]
        target["total"] += orphan["total"]
        target["wins"] += orphan["wins"]
        target["losses"] += orphan["losses"]
        target["profit"] += orphan["profit"]
        if orphan["best_strength"] is not None and (
            target["best_strength"] is None or orphan["best_strength"] > target["best_strength"]
        ):
            target["best_strength"] = orphan["best_strength"]
        merged_away.add(key)

    overview = []
    for key in order:
        if key in merged_away:
            continue
        g = groups[key]
        g["winrate"] = (g["wins"] / g["total"]) if g["total"] else 0
        overview.append(g)
    return overview


SORT_KEYS = {
    "joueur": lambda g: g["display_name"].lower(),
    "total": lambda g: g["total"],
    "victoires": lambda g: g["wins"],
    "defaites": lambda g: g["losses"],
    "winrate": lambda g: g["winrate"],
    "record": lambda g: g["best_strength"] if g["best_strength"] is not None else -1,
    "profit": lambda g: g["profit"],
}


def sort_overview(overview, sort="total", direction="desc"):
    key_fn = SORT_KEYS.get(sort, SORT_KEYS["total"])
    return sorted(overview, key=key_fn, reverse=(direction != "asc"))


def fetch_history(conn, player=None, result=None, page=1, page_size=50):
    where = []
    params = []
    if player:
        where.append("player LIKE ?")
        params.append(f"%{player}%")
    if result in (RESULT_VICTOIRE, RESULT_DEFAITE):
        where.append("result = ?")
        params.append(result)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    total = conn.execute(f"SELECT COUNT(*) FROM stats {where_sql}", params).fetchone()[0]
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"SELECT message_id, timestamp, player, result, gain, strength "
        f"FROM stats {where_sql} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
        params + [page_size, offset],
    ).fetchall()
    return rows, total


def fetch_bank(conn, player=None, page=1, page_size=50):
    where = []
    params = []
    if player:
        where.append("player LIKE ?")
        params.append(f"%{player}%")
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    total = conn.execute(f"SELECT COUNT(*) FROM bank {where_sql}", params).fetchone()[0]
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"SELECT message_id, timestamp, player, cash, banque, total "
        f"FROM bank {where_sql} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
        params + [page_size, offset],
    ).fetchall()
    return rows, total


def fetch_records(conn, limit=10):
    rows = conn.execute(
        "SELECT player, strength, timestamp, message_id FROM stats "
        "WHERE result = ? AND strength != '' "
        "ORDER BY CAST(strength AS INTEGER) DESC LIMIT ?",
        (RESULT_VICTOIRE, limit),
    ).fetchall()
    return rows
