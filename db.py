"""Acces en lecture seule a la base SQLite (bot.db) de golden-chicken-stats.

Cette base est la source de verite du bot de logging (voir son CLAUDE.md,
section "SQLite as source of truth") : le site ne fait qu'y lire, jamais
d'ecriture. En production, DB_PATH doit pointer vers le meme fichier/volume
que le bot golden-chicken-stats.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "bot.db")
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Europe/Paris"))

RESULT_VICTOIRE = "Victoire"
RESULT_DEFAITE = "Defaite"

# Comptes suivis sur la page "Pouleto Farmingo" (reprend les memes joueurs que
# l'onglet du meme nom dans le Google Sheet) - liste figee au meme titre que
# TRACKED_MEMBER_IDS cote golden-chicken-stats, pas de detection automatique.
FARMING_PLAYERS = ["Cheval_2_3", "Gol__mon", "Ittap_el", "Tarte2foutre"]


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


def since_hours(hours):
    """Horodatage ISO (meme format+fuseau que golden-chicken-stats) pour un
    filtre 'depuis les X dernieres heures', comparable par simple tri de
    chaine avec la colonne timestamp (voir last_win_strength() cote bot)."""
    return (datetime.now(TIMEZONE) - timedelta(hours=hours)).isoformat(timespec="seconds")


def player_row_stats(conn, player=None):
    """Combats/victoires/defaites/winrate pour un pseudo (ou globalement si
    player est None) - equivalent des colonnes Combats/Victoires/Defaites/
    Win rate de l'onglet Pouleto Farmingo (elles-memes tirees d'Overview)."""
    where = "WHERE lower(player) = ?" if player else ""
    params = [player.lower()] if player else []
    total = conn.execute(f"SELECT COUNT(*) FROM stats {where}", params).fetchone()[0]
    wins = conn.execute(
        f"SELECT COUNT(*) FROM stats {where}{' AND' if where else 'WHERE'} result = ?",
        params + [RESULT_VICTOIRE],
    ).fetchone()[0]
    losses = conn.execute(
        f"SELECT COUNT(*) FROM stats {where}{' AND' if where else 'WHERE'} result = ?",
        params + [RESULT_DEFAITE],
    ).fetchone()[0]
    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "winrate": (wins / total) if total else 0,
    }


def best_streak(conn, player=None, since=None):
    """Plus haute Probabilite (%) atteinte sur une Victoire, tous poulets
    confondus pour ce joueur (ou globalement) - equivalent des lignes 'Best
    streak (last Xh)' de l'onglet Pouleto Farmingo."""
    where = ["result = ?", "strength != ''"]
    params = [RESULT_VICTOIRE]
    if player:
        where.append("lower(player) = ?")
        params.append(player.lower())
    if since:
        where.append("timestamp >= ?")
        params.append(since)
    row = conn.execute(
        f"SELECT MAX(CAST(strength AS INTEGER)) FROM stats WHERE {' AND '.join(where)}", params
    ).fetchone()
    return row[0]


def _prob_reach_n_wins(n):
    """Probabilite qu'un poulet fraichement achete (50% de chance des la
    1ere victoire, +1 point par victoire consecutive - voir _kelly_bet dans
    golden-chicken-stats) enchaine au moins n victoires d'affilee avant de
    perdre : produit des chances de gagner chacun des n combats necessaires.
    Reproduit la colonne C de l'onglet 'Probas 2' du Sheet, mais calculee a
    la volee plutot que lue dans une table figee de 100 lignes."""
    p = 1.0
    for k in range(n):
        p *= 0.50 + 0.01 * k
    return p


def _avg_attempts_for_streak(n):
    """Nombre moyen de poulets (tentatives independantes) necessaires pour
    qu'un seul atteigne au moins n victoires d'affilee - colonne D de
    'Probas 2' (loi geometrique, moyenne = 1/p)."""
    return 1 / _prob_reach_n_wins(n)


def _rarity(total_fights, n):
    """Probabilite d'observer au moins 2 fois un poulet a >= n victoires
    d'affilee sur total_fights combats au total, si chaque combat etait une
    tentative independante de probabilite _prob_reach_n_wins(n) - reproduit
    1-BINOMDIST(1; total; p; TRUE) du Sheet (colonne E de 'Probas 2' et la
    case 'Chance' du record dans Pouleto Farmingo). Calculee directement (pas
    de math.comb) : avec p minuscule et total_fights ~10^5-10^6, seuls les
    termes k=0 et k=1 de la binomiale sont non negligeables, donc pas de
    risque de depassement de capacite/precision."""
    p = _prob_reach_n_wins(n)
    q = 1 - p
    cdf_le1 = q**total_fights + total_fights * p * q ** (total_fights - 1)
    return 1 - cdf_le1


def _prob_at_least_one(total_fights, n):
    """Probabilite d'avoir deja obtenu (ou d'obtenir) AU MOINS UN poulet a
    >= n victoires d'affilee sur total_fights combats au total, si chaque
    combat etait une tentative independante de probabilite
    _prob_reach_n_wins(n) - c'est 1 - P(X=0) pour X ~ Binomial(total_fights;
    p), pas 1 - P(X<=1) (qui demanderait au moins deux occurrences, utilise
    par _rarity() pour juger si le record observe est statistiquement
    surprenant, un besoin different)."""
    p = _prob_reach_n_wins(n)
    return 1 - (1 - p) ** total_fights


# Horizons de la table "Objectifs" - (libelle, heures pour Best streak, objectif de % de victoire)
FARMING_GOALS = [
    ("5h", 5, 0.86),
    ("12h", 12, 0.87),
    ("24h", 24, 0.90),
    ("Total", None, 0.95),
]


def farming_goals(conn, total_fights):
    goals = []
    for label, hours, goal_pct in FARMING_GOALS:
        n = round((goal_pct - 0.50) * 100)
        avg_needed = _avg_attempts_for_streak(n)
        goals.append(
            {
                "label": label,
                "goal_pct": goal_pct,
                "actual_chance": _prob_at_least_one(total_fights, n),
                "avg_needed": avg_needed,
                "completion": (total_fights / avg_needed) if avg_needed else 0,
                "remaining": avg_needed - total_fights,
            }
        )
    return goals


def farming_record(conn):
    """Record actuel (plus haute Probabilite jamais gagnee) + sa rarete,
    calculee avec le nombre de combats deja joues au moment ou il a ete
    obtenu (pas le total actuel) - equivalent de la ligne 'Best/User/Time/
    Chance' de Pouleto Farmingo."""
    row = conn.execute(
        "SELECT player, strength, timestamp FROM stats "
        "WHERE result = ? AND strength != '' "
        "ORDER BY CAST(strength AS INTEGER) DESC, timestamp ASC LIMIT 1",
        (RESULT_VICTOIRE,),
    ).fetchone()
    if row is None:
        return None
    strength = int(row["strength"])
    fights_at_time = conn.execute(
        "SELECT COUNT(*) FROM stats WHERE timestamp <= ?", (row["timestamp"],)
    ).fetchone()[0]
    return {
        "player": row["player"],
        "strength": strength,
        "timestamp": row["timestamp"],
        "rarity": _rarity(fights_at_time, strength - 50),
    }
