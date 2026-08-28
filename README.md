# Golden Chicken Website

Site web en lecture seule qui reprend les fonctionnalites du Google Sheet
utilise par [golden-chicken-stats](../golden-chicken-stats) : classement des
joueurs (Overview), historique des combats (Cockfights) et suivi du solde
(Bank). Au lieu de lire le Google Sheet, le site lit directement le fichier
SQLite `bot.db` qui sert de source de verite a ce bot.

## Setup & lancement

```bash
python -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env   # puis renseigner DB_PATH
venv/bin/python app.py
```

Ou avec uvicorn directement (rechargement auto en dev) :

```bash
venv/bin/uvicorn app:app --reload
```

Par defaut ceci n'ecoute que sur `127.0.0.1` (localhost). Pour y acceder
depuis un autre appareil du meme reseau local (ex. `http://192.168.1.65:8000`),
ajouter `--host 0.0.0.0` :

```bash
venv/bin/uvicorn app:app --reload --host 0.0.0.0
```

`DB_PATH` doit pointer vers le meme fichier `bot.db` (ou une copie/volume
partage) que le bot `golden-chicken-stats` — voir son `.env.example`
(`DB_PATH`). Le site n'ecrit jamais dans cette base (connexion SQLite
`mode=ro`).

Deploye via Nixpacks (`nixpacks.toml`).

## Pages

- `/` — Classement des joueurs (equivalent de l'onglet Overview) + records de
  probabilite de victoire.
- `/historique` — Historique des combats (equivalent de l'onglet Cockfights),
  filtrable par serveur/joueur/resultat, pagine.
- `/solde` — Suivi des `+bal` observes pour les joueurs suivis (equivalent de
  l'onglet Bank).

Il n'y a pas de suite de tests, de linter ou d'etape de build dans ce repo.
