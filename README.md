# karthus-wpa

**Win Probability Added (WPA) analyzer for Karthus** — collects ranked solo/duo games from the Riot API, stores per-minute timeline data in SQLite, and (once enough data is collected) trains a per-role PyTorch model to compute WPA for Karthus-specific in-game events.

Inspired by [Coachless.gg](https://coachless.gg). Supports all five Karthus roles (MID, JUNGLE, BOTTOM, SUPPORT, TOP) with separate data and models per role.

---

## Requirements

- Python 3.9+
- A [Riot Games API key](https://developer.riotgames.com/) (dev key works; personal key rate-limited to 20 req/s)

---

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/karthus-wpa
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

```bash
# Collect games — BFS from one player (good for testing)
python main.py collect --seed "SqfeWalk#NA1"
python main.py collect --seed "SqfeWalk#NA1" --max-players 50

# Collect games — seed from NA master/GM/challenger ladder
python main.py collect

# Database overview
python main.py stats

# List collected players (optionally filter by tier)
python main.py players
python main.py players --tier CHALLENGER

# Show all Karthus games stored for a player
python main.py games "SqfeWalk#NA1"
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
├── main.py             # Rich CLI entry point
├── features.py         # (planned) Feature extraction from timeline frames
├── model.py            # (planned) PyTorch WinProbNet (feedforward v1 + LSTM v2)
├── wpa.py              # (planned) WPA calculator
└── analyzer.py         # (planned) Per-player aggregate WPA analysis
```

---

## Roadmap

- [x] Async data collection pipeline (BFS crawl, timeline storage)
- [ ] Feature extraction (`features.py`)
- [ ] Win probability model training (`model.py`)
- [ ] WPA calculator (`wpa.py`)
- [ ] Per-player WPA breakdown CLI (`analyzer.py`)
- [ ] Item WPA tierlist

---

*Not affiliated with Riot Games. Uses the [Riot Games API](https://developer.riotgames.com/) under their terms of service.*
