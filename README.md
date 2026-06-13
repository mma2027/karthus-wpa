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
| `--verbose` / `-v` | Print each player name, rank, and match as they are fetched |

### 2. Train a model

Once you have enough games collected (100+ per role recommended, 500+ for best results):

```bash
python main.py train --role MID
python main.py train --role JUNGLE
python main.py train --role JUNGLE --patch-window 3   # restrict to 3 most recent patches
```

Trains a feedforward neural network (PyTorch) on per-minute timeline frames. Saves the model and scaler to `models/`. One model per role.

### 3. Analyze a player

```bash
python main.py analyze "SqfeWalk#NA1"
python main.py analyze "SqfeWalk#NA1" --role JUNGLE
python main.py analyze "SqfeWalk#NA1" --role MID --games 30
```

Prints a WPA breakdown table — how much each event type (R kills, deaths, dragons, etc.) shifted win probability on average across the player's recent games.

### 4. Item WPA tier list

```bash
python main.py tierlist --role MID
python main.py tierlist --role JUNGLE --purchase-rank 2   # second item
```

Ranks items by their average WPA at purchase time across all stored games for that role.

### 5. Database utilities

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
├── wpa.py              # WPA calculator
├── analyzer.py         # Per-player WPA analysis + item tier list (Rich output)
└── main.py             # Rich CLI entry point
```

---

## Roadmap

- [x] Async data collection pipeline (BFS crawl, timeline storage)
- [x] Feature extraction (`features.py`)
- [x] Win probability model training (`model.py`)
- [x] WPA calculator (`wpa.py`)
- [x] Per-player WPA breakdown CLI (`analyzer.py`)
- [x] Item WPA tierlist
- [ ] Challenger benchmark comparison column in analyze output
- [ ] LSTM sequence model (v2) for better temporal modeling

---

*Not affiliated with Riot Games. Uses the [Riot Games API](https://developer.riotgames.com/) under their terms of service.*
