"""Acces en lecture seule a la base SQLite (bot.db) de golden-chicken-stats.

Cette base est la source de verite du bot de logging (voir son CLAUDE.md,
section "SQLite as source of truth") : le site ne fait qu'y lire, jamais
d'ecriture. En production, DB_PATH doit pointer vers le meme fichier/volume
que le bot golden-chicken-stats.
"""

import os
import sqlite3
import time
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


OVERVIEW_CACHE_TTL = 30  # secondes
_overview_cache = {"data": None, "expires": 0.0}


def build_overview(conn):
    """Classement (voir _build_overview ci-dessous), cache en memoire
    OVERVIEW_CACHE_TTL secondes. Sur bot.db a ~870k lignes, l'agregation
    Python ligne par ligne prend plusieurs secondes (voir CLAUDE.md,
    "Farming dashboard") - un classement n'a pas besoin d'etre a la
    seconde pres, donc ce cache evite de la refaire a chaque chargement
    de /classement plutot que de reecrire l'agregation en SQL (le
    regroupement id:/name: est plus simple a garder lisible en Python)."""
    now = time.monotonic()
    if _overview_cache["data"] is not None and now < _overview_cache["expires"]:
        return _overview_cache["data"]
    data = _build_overview(conn)
    _overview_cache["data"] = data
    _overview_cache["expires"] = now + OVERVIEW_CACHE_TTL
    return data


def _build_overview(conn):
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


def farming_column(conn, windows, player=None):
    """Une colonne du tableau 'Meilleures series' (combats/victoires/defaites
    + best streak par fenetre de temps + all-time) en UNE requete SQL.

    Avant : 5 appels best_streak() + 3 requetes COUNT (player_row_stats) par
    colonne, soit 8 requetes x 5 colonnes (4 joueurs + All) = 40 requetes par
    chargement de page - chacune re-scannant (ou re-cherchant via l'index) le
    meme sous-ensemble de lignes. Sur bot.db a ~870k lignes ca faisait monter
    la page a plus de 10s meme avec les bons index en place (voir CLAUDE.md,
    "Farming dashboard"). Regrouper tous les agregats dans un seul SELECT
    avec des CASE/MAX/SUM ne scanne le sous-ensemble du joueur qu'une fois.
    """
    windows = list(windows)
    where = "WHERE lower(player) = ?" if player else ""
    since_columns = ", ".join(
        f"MAX(CASE WHEN result = ? AND strength != '' AND timestamp >= ? THEN CAST(strength AS INTEGER) END)"
        for _ in windows
    )
    query = (
        "SELECT COUNT(*), "
        "SUM(CASE WHEN result = ? THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN result = ? THEN 1 ELSE 0 END), "
        "MAX(CASE WHEN result = ? AND strength != '' THEN CAST(strength AS INTEGER) END)"
        + (", " + since_columns if windows else "")
        + f" FROM stats {where}"
    )
    params = [RESULT_VICTOIRE, RESULT_DEFAITE, RESULT_VICTOIRE]
    for _, hours in windows:
        params.extend([RESULT_VICTOIRE, since_hours(hours)])
    if player:
        params.append(player.lower())

    total, wins, losses, streak_all, *window_streaks = conn.execute(query, params).fetchone()
    total = total or 0
    wins = wins or 0
    losses = losses or 0
    streaks = {label: value for (label, _hours), value in zip(windows, window_streaks)}
    streaks["all"] = streak_all
    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "winrate": (wins / total) if total else 0,
        "streaks": streaks,
    }


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


def farming_ladder(conn, total_fights, start_pct=50):
    """Echelle complete de probabilites, du % de depart (le record actuel par
    defaut) jusqu'a 100%, une ligne par point de victoire - mêmes formules
    que l'onglet 'Probas 2' (colonnes C/D), sans notion d'horizon temporel :
    contrairement a l'ancienne table a 4 lignes fixes (5h/12h/24h/Total), ces
    probabilites portent sur le nombre total de combats joues, pas sur une
    fenetre de temps - leur accoler un libelle en heures n'avait pas de sens
    et rendait la table incoherente."""
    start_pct = max(start_pct, 51)  # n=0 (50%) n'est pas un "objectif" a atteindre
    ladder = []
    for pct in range(start_pct, 101):
        n = pct - 50
        avg_needed = _avg_attempts_for_streak(n)
        ladder.append(
            {
                "pct": pct,
                "prob_reach": _prob_reach_n_wins(n),
                "avg_needed": avg_needed,
                "actual_chance": _prob_at_least_one(total_fights, n),
                "completion": (total_fights / avg_needed) if avg_needed else 0,
                "remaining": avg_needed - total_fights,
            }
        )
    return ladder


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


FARMING_CACHE_TTL = 30  # secondes
_farming_cache = {"data": None, "expires": 0.0}


GRANULARITY_BUCKET_EXPR = {
    "day": "date(timestamp)",
    # %W = numero de semaine (lundi = premier jour), prefixe par l'annee pour
    # trier/regrouper correctement les semaines a cheval sur deux annees.
    "week": "strftime('%Y-W%W', timestamp)",
    "month": "strftime('%Y-%m', timestamp)",
}

MONTH_NAMES_FR = [
    "janv.", "fevr.", "mars", "avr.", "mai", "juin",
    "juil.", "aout", "sept.", "oct.", "nov.", "dec.",
]


def format_bucket_label(bucket, granularity):
    """Libelle affiche sur l'axe/tooltip du graphique pour un bucket (voir
    fetch_best_streak_series) - le bucket lui-meme (date ISO, 'YYYY-Wnn' ou
    'YYYY-mm') n'est pas directement lisible."""
    if granularity == "month":
        year, month = bucket.split("-")
        return f"{MONTH_NAMES_FR[int(month) - 1]} {year}"
    if granularity == "week":
        year, week = bucket.split("-W")
        return f"Semaine {int(week):02d} {year}"
    year, month, day = bucket.split("-")
    return f"{int(day):02d} {MONTH_NAMES_FR[int(month) - 1]} {year}"


def fetch_date_bounds(conn, players=None):
    """Plage de dates couverte par stats (bornes par defaut du selecteur de
    dates du graphique : de la premiere a la derniere donnee), restreinte aux
    comptes surveilles (FARMING_PLAYERS) quand fournis - sinon le datepicker
    pourrait proposer une date par defaut sans aucune donnee pour ces comptes
    (ex. un autre membre du serveur ayant joue +cf avant eux)."""
    where = ""
    params = []
    if players:
        placeholders = ", ".join("?" for _ in players)
        where = f"WHERE lower(player) IN ({placeholders})"
        params = [p.lower() for p in players]
    row = conn.execute(f"SELECT MIN(date(timestamp)), MAX(date(timestamp)) FROM stats {where}", params).fetchone()
    return row[0], row[1]


def fetch_best_streak_series(conn, granularity="day", player=None, start=None, end=None):
    """Une ligne par bucket (jour/semaine/mois) : la plus haute strength parmi
    les Victoire de ce bucket, et le joueur qui l'a obtenue (departage par le
    combat le plus tot en cas d'egalite) - alimente le graphique 'Best streak'
    de /graphique. Restreint aux comptes surveilles (FARMING_PLAYERS, meme
    liste que le tableau Farming) - les autres membres du serveur jouent aussi
    a +cf mais ne font pas partie de la rotation suivie par ce site. Le filtre
    joueur restreint en plus la recherche du max a un seul de ces comptes (une
    ligne = ce compte uniquement) plutot que de se contenter de changer
    l'etiquette affichee."""
    bucket_expr = GRANULARITY_BUCKET_EXPR.get(granularity, GRANULARITY_BUCKET_EXPR["day"])
    placeholders = ", ".join("?" for _ in FARMING_PLAYERS)
    where = ["result = ?", "strength != ''", f"lower(player) IN ({placeholders})"]
    params = [RESULT_VICTOIRE] + [p.lower() for p in FARMING_PLAYERS]
    if player:
        where.append("lower(player) = ?")
        params.append(player.lower())
    if start:
        where.append("date(timestamp) >= ?")
        params.append(start)
    if end:
        where.append("date(timestamp) <= ?")
        params.append(end)
    where_sql = " AND ".join(where)

    query = (
        "WITH wins AS ("
        f"  SELECT timestamp, player, CAST(strength AS INTEGER) AS strength, {bucket_expr} AS bucket"
        f"  FROM stats WHERE {where_sql}"
        "), ranked AS ("
        "  SELECT bucket, player, strength, timestamp,"
        "         ROW_NUMBER() OVER (PARTITION BY bucket ORDER BY strength DESC, timestamp ASC) AS rn"
        "  FROM wins"
        ") SELECT bucket, player, strength, timestamp FROM ranked WHERE rn = 1 ORDER BY bucket ASC"
    )
    return conn.execute(query, params).fetchall()


def farming_dashboard(conn, windows):
    """Colonnes + record + echelle d'objectifs de la page Farming, cache en
    memoire FARMING_CACHE_TTL secondes - meme raisonnement que le cache de
    build_overview() : sur bot.db a ~870k lignes, l'ensemble (5 colonnes +
    record) prend ~2.5s meme une fois les requetes regroupees (voir
    farming_column()), et un dashboard de farming n'a pas besoin d'etre a la
    seconde pres."""
    now = time.monotonic()
    if _farming_cache["data"] is not None and now < _farming_cache["expires"]:
        return _farming_cache["data"]

    players = [
        {"name": player, **farming_column(conn, windows, player)}
        for player in FARMING_PLAYERS
    ]
    all_row = {"name": "All", **farming_column(conn, windows)}
    record = farming_record(conn)
    ladder = farming_ladder(conn, all_row["total"], start_pct=record["strength"] if record else 50)

    data = {"columns": players + [all_row], "record": record, "ladder": ladder}
    _farming_cache["data"] = data
    _farming_cache["expires"] = now + FARMING_CACHE_TTL
    return data
