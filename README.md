# karthus-wpa

**Win Probability Added (WPA) analyzer for Karthus** — collects ranked solo/duo games from the Riot API, stores per-minute timeline data in SQLite, trains a per-role PyTorch model to estimate win probability, and computes WPA for Karthus-specific in-game events (R kills, deaths, item purchases, objectives).

Inspired by [Coachless.gg](https://coachless.gg). Supports all five Karthus roles (MID, JUNGLE, BOTTOM, SUPPORT, TOP) with separate data and models per role.

---

## Requirements

- Python 3.9+
- A [Riot Games API key](https://developer.riotgames.com/) (dev key works; rate-limited to 20 req/s)

---

## Installation

```bash
git clone git@github.com:mma2027/karthus-wpa.git
cd karthus-wpa
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Open `.env` and fill in your key:

```
RIOT_API_KEY=RGAPI-your-key-here
RIOT_PLATFORM=na1
```

> **CUDA note:** `requirements.txt` points to the PyTorch CUDA 12.1 index, compatible with NVIDIA drivers supporting CUDA 12.1–12.2. If your driver is newer or you want CPU-only, replace the `--extra-index-url` line in `requirements.txt` with `https://download.pytorch.org/whl/cpu` before installing.

---

Each session, activate the environment first:
```bash
source .venv/bin/activate
```

## Usage

### 1. Collect games

```bash
# BFS from one player (good for testing)
python main.py collect --seed "SqfeWalk#NA1"
python main.py collect --seed "SqfeWalk#NA1" --max-players 50

# Verbose mode: print each player and match as they are fetched
python main.py collect --seed "SqfeWalk#NA1" --max-players 50 -v

# Seed from NA master/GM/challenger ladder (broad coverage)
python main.py collect
```

#### collect flags

| Flag | Description |
|---|---|
| `--seed NAME#TAG` | Start BFS from a specific player |
| `--max-players N` | Stop after N players (useful for testing) |
| `--patch-window N` | Only store games from the N most recent patches (default: 5). Patch list fetched automatically from Data Dragon. |
| `--refresh-days N` | Re-scan players last collected more than N days ago — fetches new games they've played and updates their rank. Safe to run alongside normal collection. |
| `--verbose` / `-v` | Print each player name, rank, and match as they are fetched |

### 2. Train a model

Once you have enough games collected (100+ per role recommended, 500+ for best results):

```bash
python main.py train --role MID
python main.py train --role JUNGLE
python main.py train --role JUNGLE --patch-window 3   # restrict to 3 most recent patches
```

Valid roles: `MID`, `JUNGLE`, `BOTTOM`, `SUPPORT`, `TOP`

Trains a feedforward neural network (PyTorch) on per-minute timeline frames. Saves the model and scaler to `models/wp_{role}.pt` and `models/scaler_{role}.pkl`. One model per role. The model is loaded automatically by `analyze` and `tierlist`.

### 3. Analyze a player

```bash
python main.py analyze "SqfeWalk#NA1"
python main.py analyze "SqfeWalk#NA1" --role JUNGLE
python main.py analyze "SqfeWalk#NA1" --role MID --games 30
```

Valid roles: `MID`, `JUNGLE`, `BOTTOM`, `SUPPORT`, `TOP`

Prints a WPA breakdown table — how much each event type (R kills, deaths, dragons, etc.) shifted win probability on average across the player's recent games. Requires a trained model for the relevant role. If `--role` is omitted, uses the role with the most stored games for that player.

### 4. Item WPA tier list

```bash
# All roles and all purchase slots (1–6)
python main.py tierlist

# One role, all purchase slots
python main.py tierlist --role MID

# One role, specific purchase slot
python main.py tierlist --role MID --purchase-rank 1   # first item bought
python main.py tierlist --role JUNGLE --purchase-rank 2   # second item bought
```

Valid roles: `MID`, `JUNGLE`, `BOTTOM`, `SUPPORT`, `TOP`

`--purchase-rank` is the order in which the item was bought that game: `1` = first item purchased, `2` = second, etc. Ranks items by their average WPA at purchase time. Requires a trained model. Tables with insufficient data (< 3 games) are skipped automatically.

### 5. Keystone rune win rates

```bash
python main.py rune --role MID
python main.py rune --role JUNGLE
```

Valid roles: `MID`, `JUNGLE`, `BOTTOM`, `SUPPORT`, `TOP`

Shows win rate by keystone rune across all stored games for a role. Does **not** require a trained model — uses raw match outcomes directly.

### 6. Database utilities

```bash
# Database overview
python main.py stats

# List collected players (optionally filter by tier)
python main.py players
python main.py players --tier CHALLENGER

# Show all Karthus games stored for a player
python main.py games "SqfeWalk#NA1"

# Wipe the database and start fresh (asks for confirmation)
python main.py reset
```

Valid `--tier` values: `IRON`, `BRONZE`, `SILVER`, `GOLD`, `PLATINUM`, `EMERALD`, `DIAMOND`, `MASTER`, `GRANDMASTER`, `CHALLENGER`

> **Note:** rank data only appears for players the crawler has fully scanned. Players discovered as match participants show `—` for rank/W/L until the BFS reaches and processes them.

---

## Data collection notes

- **Rate limit**: dev key allows 20 req/s. The async client uses a semaphore of 18 concurrent requests. On 429, it backs off using the `Retry-After` header.
- **BFS crawl**: the seed player's last 50 ranked games are scanned to discover opponents/teammates. For each discovered player, only their Karthus games are fetched (no wasted calls).
- **Resume-safe**: all inserts use `INSERT OR IGNORE` — stopping and restarting the collector will never duplicate data.
- **Storage**: each game stores the full per-minute timeline (economy, combat stats, positions for all 10 players) plus extracted Karthus events (R kills, deaths, item purchases, objectives).

---

## Project structure

```
karthus-wpa/
├── .env.example        # Copy to .env and add your Riot API key
├── requirements.txt
├── db.py               # SQLite schema + query helpers (WAL mode)
├── riot_client.py      # Async Riot API client (aiohttp, semaphore-based rate limiting)
├── collector.py        # BFS data collection pipeline
├── features.py         # Feature extraction from timeline frames (384-dim vectors)
├── model.py            # PyTorch WinProbNet (feedforward) + training loop
├── wpa.py              # WPA calculator + rune/item tier list aggregators
├── analyzer.py         # Rich terminal output (player analysis, tier lists, rune analysis)
└── main.py             # CLI entry point
```

---

## Roadmap

- [x] Async data collection pipeline (BFS crawl, timeline storage)
- [x] Feature extraction (`features.py`)
- [x] Win probability model training (`model.py`)
- [x] WPA calculator (`wpa.py`)
- [x] Per-player WPA breakdown CLI (`analyzer.py`)
- [x] Item WPA tier list (all roles and purchase slots)
- [x] Keystone rune win rate tier list
- [ ] Challenger benchmark comparison column in analyze output
- [ ] LSTM sequence model (v2) for better temporal modeling

---

*Not affiliated with Riot Games. Uses the [Riot Games API](https://developer.riotgames.com/) under their terms of service.*
