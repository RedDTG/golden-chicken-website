# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A read-only FastAPI website that reproduces the Google Sheet dashboard used
by the sibling repo `golden-chicken-stats`: a farming dashboard (Pouleto
Farmingo tab, the home page), a player leaderboard (Overview tab, at
`/classement`), a paginated fight log (Cockfights tab), and a
balance-tracking view (Bank tab). Instead of talking to the Google Sheets
API, it reads directly from `golden-chicken-stats`'s SQLite file (`bot.db`),
which that bot's own CLAUDE.md documents as the actual source of truth
(Sheets is just a mirror kept in sync from it). This site never writes to
that database — every connection is opened `mode=ro` (see `db.py`).

Related repos (siblings, not submodules):
- `golden-chicken-stats` — the Discord bot that logs `+cf` cockfight results
  and `+bal` balances into `bot.db` and syncs them to the Google Sheet. Owns
  the schema this site reads (`stats`/`bank` tables) and the semantics of
  every column (see its CLAUDE.md, especially "SQLite as source of truth" and
  the Overview-sheet grouping logic this site's `build_overview()` mirrors).
- `golden-chicken-finder` — the self-bot that actually farms `+cf`. Unrelated
  to this site beyond farming the data `golden-chicken-stats` logs.

## Setup & running

```bash
python -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env   # then set DB_PATH
venv/bin/python app.py
```

Deployed via Nixpacks (`nixpacks.toml`: `uvicorn app:app` as the start
command, bound to Nixpacks' `$PORT`).

Environment variables (see `.env.example`, loaded via `python-dotenv`):
- `DB_PATH` (default `bot.db`) — path to `golden-chicken-stats`'s SQLite
  file. **Must be the same file or a volume shared with that bot's own
  `DB_PATH`** for the site to show current data; a stale/local copy will
  silently show stale data since nothing here ever fetches from Sheets or
  Discord directly.
- `PORT` (default `8000`) — only used by the `python app.py` entry point;
  Nixpacks/uvicorn's own `$PORT` takes precedence in that deployment path.

There is no test suite, linter, or build step in this repo.

## Architecture

Everything is small and un-abstracted on purpose — three routes, one query
module:

1. **`db.py`**: all SQLite access. `get_connection()` opens a fresh
   `mode=ro` URI connection per request (cheap for SQLite, and safe against
   the bot's concurrent WAL writes — readers get a consistent snapshot
   without blocking the writer). `build_overview()` re-implements, in
   Python, the exact grouping rule `golden-chicken-stats`'s
   `_update_overview_impl()` uses for its Overview sheet: group by
   `id:{player_id}` when a row has one, else fall back to
   `name:{lower(player)}` for rows logged before the `player_id` column
   existed. This has to stay in sync by hand with that function if its
   grouping logic ever changes — there's no shared code between the two
   repos, just the shared database. Aggregates (wins/losses/winrate/best
   win %/profit) are computed by scanning every matching `stats` row in
   Python per request rather than in SQL, matching the sibling bot's own
   approach and keeping the fallback-grouping logic in one readable place;
   revisit only if the row count ever makes this slow.
2. **`app.py`**: four FastAPI routes rendering Jinja2 templates —
   `/` (the farming dashboard, see below), `/classement` (leaderboard +
   top-5 win-probability records — the original home page, moved here),
   `/historique` (paginated, filterable fight log), `/solde` (paginated,
   filterable `+bal` log). All filtering/sorting/pagination is server-side
   via query params (no JS framework, no client state) — a page reload is
   the whole interaction model, deliberately, to match the rest of this
   project's preference for simple single-purpose scripts over a build
   pipeline.
3. **Templates/static**: plain Jinja2 + hand-written CSS (`static/style.css`,
   dark theme), no bundler, no client-side JS at all — filter forms submit
   as a plain GET.

No `Serveur` dimension anywhere (filter or column): every row in this
deployment's `bot.db` is from the single Discord server this is run for, so
a per-row/per-query server field would just repeat the same value on every
line. If this ever needs to serve more than one server's data again, the
`server` column is still on `stats`/`bank` in the shared database — only the
view layer here dropped it, so reintroducing the filter is a `db.py`/routes
change, not a schema migration.

Because the schema and the `Victoire`/`Defaite`/gain-sign/strength-as-percent
conventions all come from `golden-chicken-stats`, any change to that bot's
`stats`/`bank` table schema or to its result/gain encoding needs a matching
update here (`db.py`'s queries and `build_overview()`) or this site will
silently misread the data (wrong aggregates, not a crash).

## Farming dashboard (home page, `/`)

Reproduces the Google Sheet's "Pouleto Farmingo" tab, reverse-engineered from
its live formulas (not just its displayed values) so the math here matches
exactly rather than approximately:

- `FARMING_PLAYERS` (`db.py`) is a hardcoded list of the four tracked
  accounts (`Cheval_2_3`, `Gol__mon`, `Ittap_el`, `Tarte2foutre`), matched
  case-insensitively against the `player` column — same convention and same
  trust level as `TRACKED_MEMBER_IDS` in `golden-chicken-stats`. No
  auto-detection; add a name here by hand if a new account joins the
  farming rotation. The "All" row aggregates every row in `stats`, not just
  these four (matches the Sheet's `Overview!$B$2`-sourced Total row).
- **Best streak (last Xh)**: highest `strength` among that player's
  `Victoire` rows with `timestamp >=` a cutoff computed via `TIMEZONE`
  (defaults `Europe/Paris`, same env var as the bot) — mirrors the Sheet's
  `QUERY(... A >= NOW()-X/24 ...)` formulas.
- **Objectifs (goals) table**: the game's mechanic is a fresh chicken starts
  at 50% and each consecutive win adds 1 point (see `_kelly_bet` in
  `golden-chicken-stats`), so the probability a *single* streak-attempt
  reaches `n` consecutive wins is `∏_{k=0}^{n-1}(0.50 + 0.01k)`
  (`_prob_reach_n_wins`) — computed analytically here rather than read from
  the Sheet's `Probas 2` tab, which hardcodes the same formula as a
  100-row lookup table (and has a copy-paste row-offset bug on two of its
  four goal rows that this reimplementation doesn't reproduce). From that:
  `_avg_attempts_for_streak(n) = 1/p` (expected number of independent
  chicken-attempts to see one reach `n`, geometric distribution mean), and
  `_rarity(total_fights, n)` estimates how surprising it would be to see a
  streak that long *at least twice* by pure chance across `total_fights`
  total fights recorded — `1 - P(X<=1)` for `X ~ Binomial(total_fights, p)`,
  computed directly from the two lowest terms (`p` is tiny and
  `total_fights` large — `math.comb`-based approaches risk overflow/underflow
  that this avoids). `FARMING_GOALS` hardcodes the four horizon/goal pairs
  from the Sheet (5h→86%, 12h→87%, 24h→90%, all-time→95%).
- **Record actuel**: the highest `strength` ever logged on a `Victoire`,
  plus its rarity computed against `COUNT(*) FROM stats WHERE timestamp <=`
  that record's own timestamp (fights played *up to that point*, not the
  live total) — matches the Sheet's `COUNTIF(Stats!A:A;"<="&L9)`. See the
  2026-09-01 incident (fixed by deleting 26 corrupted rows directly from the
  Sheet — a `golden-chicken-finder` bug produced a `gol__mon` streak with
  impossible non-monotonic jumps up to a repeated 100%) for why this number
  should always be sanity-checked against the fight-by-fight history before
  trusting it after an anomaly.
