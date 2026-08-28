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


def list_servers(conn):
    rows = conn.execute(
        "SELECT DISTINCT server FROM stats WHERE server != '' "
        "UNION SELECT DISTINCT server FROM bank WHERE server != '' "
        "ORDER BY server"
    ).fetchall()
    return [r["server"] for r in rows]


def fetch_stats_rows(conn, server=None):
    query = "SELECT message_id, timestamp, player, result, gain, strength, server, player_id FROM stats"
    params = []
    if server:
        query += " WHERE server = ?"
        params.append(server)
    query += " ORDER BY timestamp ASC"
    return conn.execute(query, params).fetchall()


def build_overview(conn, server=None):
    """Reproduit le calcul de l'onglet Overview du Google Sheet (voir
    _update_overview_impl dans golden-chicken-stats/bot.py) : regroupement
    par ID Discord quand connu, sinon par pseudo en minuscules pour les
    lignes historiques anterieures a l'ajout de la colonne ID Joueur.
    """
    rows = fetch_stats_rows(conn, server)
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

    overview = []
    for key in order:
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


def fetch_history(conn, server=None, player=None, result=None, page=1, page_size=50):
    where = []
    params = []
    if server:
        where.append("server = ?")
        params.append(server)
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
        f"SELECT message_id, timestamp, player, result, gain, strength, server "
        f"FROM stats {where_sql} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
        params + [page_size, offset],
    ).fetchall()
    return rows, total


def fetch_bank(conn, server=None, player=None, page=1, page_size=50):
    where = []
    params = []
    if server:
        where.append("server = ?")
        params.append(server)
    if player:
        where.append("player LIKE ?")
        params.append(f"%{player}%")
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    total = conn.execute(f"SELECT COUNT(*) FROM bank {where_sql}", params).fetchone()[0]
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"SELECT message_id, timestamp, player, cash, banque, total, server "
        f"FROM bank {where_sql} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
        params + [page_size, offset],
    ).fetchall()
    return rows, total


def fetch_records(conn, server=None, limit=10):
    where = "result = ? AND strength != ''"
    params = [RESULT_VICTOIRE]
    if server:
        where += " AND server = ?"
        params.append(server)
    rows = conn.execute(
        f"SELECT player, strength, server, timestamp, message_id FROM stats WHERE {where} "
        f"ORDER BY CAST(strength AS INTEGER) DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    return rows
