"""
db.py — SQLite schema, connection helper, and query helpers for karthus-wpa.

Tables
------
matches          One row per Karthus ranked solo/duo game stored
timeline_frames  One row per game-minute per match (all 10 player blobs)
match_stats      End-of-game summary stats for the Karthus participant
events           Karthus-specific events (R kills, deaths, items, objectives)
players          Player registry: PUUID, name, rank snapshot
scanned_players  BFS tracking: which PUUIDs have been processed
"""

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional


def patch_sort_key(patch: str) -> tuple[int, int]:
    """Numeric sort key for patch strings like '16.9', '16.10'.

    Prevents lexicographic mis-ordering where '16.9' > '16.10'.
    """
    try:
        major, minor = patch.split(".", 1)
        return (int(major), int(minor))
    except (ValueError, AttributeError):
        return (0, 0)

DB_PATH = Path(__file__).parent / "karthus.db"

# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

@contextmanager
def get_connection(db_path: Path = DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous  = NORMAL;
        PRAGMA foreign_keys = ON;
        PRAGMA cache_size   = -64000;
        PRAGMA temp_store   = MEMORY;
    """)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema init
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    match_id            TEXT PRIMARY KEY,
    game_start          INTEGER,
    game_duration_s     INTEGER,
    patch               TEXT,
    win                 INTEGER,
    karthus_puuid       TEXT,
    karthus_participant INTEGER,
    karthus_role        TEXT,
    karthus_side        INTEGER
);

CREATE TABLE IF NOT EXISTS timeline_frames (
    match_id              TEXT,
    frame_index           INTEGER,
    timestamp_ms          INTEGER,
    team_kills            INTEGER,
    enemy_kills           INTEGER,
    team_dragons          INTEGER,
    enemy_dragons         INTEGER,
    team_baron            INTEGER,
    enemy_baron           INTEGER,
    team_turrets          INTEGER,
    enemy_turrets         INTEGER,
    team_inhibs           INTEGER,
    enemy_inhibs          INTEGER,
    dragon_soul           INTEGER,
    economy_json          TEXT,
    positions_json        TEXT,
    champion_stats_json   TEXT,
    damage_stats_json     TEXT,
    items_json            TEXT,
    PRIMARY KEY (match_id, frame_index),
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

CREATE TABLE IF NOT EXISTS match_stats (
    match_id                      TEXT PRIMARY KEY,
    kills                         INTEGER,
    deaths                        INTEGER,
    assists                       INTEGER,
    total_damage_to_champs        INTEGER,
    magic_damage_to_champs        INTEGER,
    total_damage_taken            INTEGER,
    damage_self_mitigated         INTEGER,
    gold_earned                   INTEGER,
    gold_spent                    INTEGER,
    total_minions_killed          INTEGER,
    neutral_minions_killed        INTEGER,
    vision_score                  INTEGER,
    wards_placed                  INTEGER,
    wards_killed                  INTEGER,
    vision_wards_bought           INTEGER,
    champ_level                   INTEGER,
    time_ccing_others             INTEGER,
    item0 INTEGER, item1 INTEGER, item2 INTEGER,
    item3 INTEGER, item4 INTEGER, item5 INTEGER, item6 INTEGER,
    summoner1_id INTEGER, summoner2_id INTEGER,
    summoner1_casts INTEGER, summoner2_casts INTEGER,
    perks_json                    TEXT,
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

CREATE TABLE IF NOT EXISTS events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id            TEXT,
    timestamp_ms        INTEGER,
    event_type          TEXT,
    detail_json         TEXT,
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

CREATE TABLE IF NOT EXISTS players (
    puuid               TEXT PRIMARY KEY,
    game_name           TEXT NOT NULL,
    tag_line            TEXT NOT NULL,
    region              TEXT NOT NULL,
    summoner_id         TEXT,
    tier                TEXT,
    division            TEXT,
    lp                  INTEGER,
    wins                INTEGER,
    losses              INTEGER,
    rank_fetched_at     INTEGER,
    first_seen_at       INTEGER
);

CREATE TABLE IF NOT EXISTS scanned_players (
    puuid               TEXT PRIMARY KEY,
    scanned_at          INTEGER
);

CREATE INDEX IF NOT EXISTS idx_matches_puuid       ON matches(karthus_puuid);
CREATE INDEX IF NOT EXISTS idx_matches_patch       ON matches(patch);
CREATE INDEX IF NOT EXISTS idx_matches_role        ON matches(karthus_role);
CREATE INDEX IF NOT EXISTS idx_matches_start       ON matches(game_start);
CREATE INDEX IF NOT EXISTS idx_matches_patch_role  ON matches(patch, karthus_role);
CREATE INDEX IF NOT EXISTS idx_matches_puuid_role  ON matches(karthus_puuid, karthus_role);

CREATE INDEX IF NOT EXISTS idx_frames_match        ON timeline_frames(match_id);

CREATE INDEX IF NOT EXISTS idx_events_match        ON events(match_id);
CREATE INDEX IF NOT EXISTS idx_events_type         ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_match_type   ON events(match_id, event_type);

CREATE INDEX IF NOT EXISTS idx_players_name        ON players(game_name, tag_line);
CREATE INDEX IF NOT EXISTS idx_players_tier        ON players(tier);
"""


def init_db(db_path: Path = DB_PATH) -> None:
    """Create all tables and indexes if they do not already exist."""
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def upsert_player(
    puuid: str,
    game_name: str,
    tag_line: str,
    region: str,
    summoner_id: Optional[str] = None,
    tier: Optional[str] = None,
    division: Optional[str] = None,
    lp: Optional[int] = None,
    wins: Optional[int] = None,
    losses: Optional[int] = None,
    db_path: Path = DB_PATH,
) -> None:
    now = int(time.time() * 1000)
    with get_connection(db_path) as conn:
        existing = conn.execute(
            "SELECT first_seen_at FROM players WHERE puuid = ?", (puuid,)
        ).fetchone()
        first_seen = existing["first_seen_at"] if existing else now
        rank_fetched = now if tier is not None else None

        conn.execute(
            """
            INSERT INTO players
                (puuid, game_name, tag_line, region, summoner_id,
                 tier, division, lp, wins, losses, rank_fetched_at, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(puuid) DO UPDATE SET
                game_name       = CASE WHEN excluded.game_name != '' THEN excluded.game_name ELSE game_name END,
                tag_line        = CASE WHEN excluded.tag_line  != '' THEN excluded.tag_line  ELSE tag_line  END,
                summoner_id     = COALESCE(excluded.summoner_id, summoner_id),
                tier            = COALESCE(excluded.tier,        tier),
                division        = COALESCE(excluded.division,    division),
                lp              = COALESCE(excluded.lp,          lp),
                wins            = COALESCE(excluded.wins,        wins),
                losses          = COALESCE(excluded.losses,      losses),
                rank_fetched_at = COALESCE(excluded.rank_fetched_at, rank_fetched_at)
            """,
            (puuid, game_name, tag_line, region, summoner_id,
             tier, division, lp, wins, losses, rank_fetched, first_seen),
        )


def insert_match(
    match_id: str,
    game_start: int,
    game_duration_s: int,
    patch: str,
    win: int,
    karthus_puuid: str,
    karthus_participant: int,
    karthus_role: str,
    karthus_side: int,
    db_path: Path = DB_PATH,
) -> bool:
    """Insert a match row. Returns True if inserted, False if already existed."""
    with get_connection(db_path) as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO matches
                (match_id, game_start, game_duration_s, patch, win,
                 karthus_puuid, karthus_participant, karthus_role, karthus_side)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (match_id, game_start, game_duration_s, patch, win,
             karthus_puuid, karthus_participant, karthus_role, karthus_side),
        )
        return cur.rowcount > 0


def insert_timeline_frame(
    match_id: str,
    frame_index: int,
    timestamp_ms: int,
    team_kills: int,
    enemy_kills: int,
    team_dragons: int,
    enemy_dragons: int,
    team_baron: int,
    enemy_baron: int,
    team_turrets: int,
    enemy_turrets: int,
    team_inhibs: int,
    enemy_inhibs: int,
    dragon_soul: int,
    economy: dict,
    positions: dict,
    champion_stats: dict,
    damage_stats: dict,
    items: dict,
    db_path: Path = DB_PATH,
) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO timeline_frames
                (match_id, frame_index, timestamp_ms,
                 team_kills, enemy_kills, team_dragons, enemy_dragons,
                 team_baron, enemy_baron, team_turrets, enemy_turrets,
                 team_inhibs, enemy_inhibs, dragon_soul,
                 economy_json, positions_json, champion_stats_json,
                 damage_stats_json, items_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?)
            """,
            (match_id, frame_index, timestamp_ms,
             team_kills, enemy_kills, team_dragons, enemy_dragons,
             team_baron, enemy_baron, team_turrets, enemy_turrets,
             team_inhibs, enemy_inhibs, dragon_soul,
             json.dumps(economy), json.dumps(positions),
             json.dumps(champion_stats), json.dumps(damage_stats),
             json.dumps(items)),
        )


def insert_match_stats(match_id: str, stats: dict, db_path: Path = DB_PATH) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO match_stats
                (match_id, kills, deaths, assists,
                 total_damage_to_champs, magic_damage_to_champs,
                 total_damage_taken, damage_self_mitigated,
                 gold_earned, gold_spent,
                 total_minions_killed, neutral_minions_killed,
                 vision_score, wards_placed, wards_killed, vision_wards_bought,
                 champ_level, time_ccing_others,
                 item0, item1, item2, item3, item4, item5, item6,
                 summoner1_id, summoner2_id, summoner1_casts, summoner2_casts,
                 perks_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                match_id,
                stats.get("kills"), stats.get("deaths"), stats.get("assists"),
                stats.get("totalDamageDealtToChampions"),
                stats.get("magicDamageDealtToChampions"),
                stats.get("totalDamageTaken"),
                stats.get("damageSelfMitigated"),
                stats.get("goldEarned"), stats.get("goldSpent"),
                stats.get("totalMinionsKilled"), stats.get("neutralMinionsKilled"),
                stats.get("visionScore"),
                stats.get("wardsPlaced"), stats.get("wardsKilled"),
                stats.get("visionWardsBoughtInGame"),
                stats.get("champLevel"), stats.get("timeCCingOthers"),
                stats.get("item0"), stats.get("item1"), stats.get("item2"),
                stats.get("item3"), stats.get("item4"), stats.get("item5"),
                stats.get("item6"),
                stats.get("summoner1Id"), stats.get("summoner2Id"),
                stats.get("summoner1Casts"), stats.get("summoner2Casts"),
                json.dumps(stats.get("perks", {})),
            ),
        )


def insert_event(
    match_id: str,
    timestamp_ms: int,
    event_type: str,
    detail: dict,
    db_path: Path = DB_PATH,
) -> None:
    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO events (match_id, timestamp_ms, event_type, detail_json)
            VALUES (?, ?, ?, ?)
            """,
            (match_id, timestamp_ms, event_type, json.dumps(detail)),
        )


def mark_scanned(puuid: str, db_path: Path = DB_PATH) -> None:
    now = int(time.time() * 1000)
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO scanned_players (puuid, scanned_at) VALUES (?, ?)",
            (puuid, now),
        )


def queue_puuid(puuid: str, db_path: Path = DB_PATH) -> None:
    """Add a PUUID to the scan queue (scanned_at = NULL = not yet processed)."""
    with get_connection(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO scanned_players (puuid, scanned_at) VALUES (?, NULL)",
            (puuid,),
        )


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def match_exists(match_id: str, db_path: Path = DB_PATH) -> bool:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM matches WHERE match_id = ?", (match_id,)
        ).fetchone()
    return row is not None


def is_scanned(puuid: str, db_path: Path = DB_PATH) -> bool:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT scanned_at FROM scanned_players WHERE puuid = ?", (puuid,)
        ).fetchone()
    return row is not None and row["scanned_at"] is not None


def get_match(match_id: str, db_path: Path = DB_PATH) -> Optional[sqlite3.Row]:
    with get_connection(db_path) as conn:
        return conn.execute(
            "SELECT * FROM matches WHERE match_id = ?", (match_id,)
        ).fetchone()


def get_matches_for_player(puuid: str, db_path: Path = DB_PATH) -> list:
    with get_connection(db_path) as conn:
        return conn.execute(
            "SELECT * FROM matches WHERE karthus_puuid = ? ORDER BY game_start DESC",
            (puuid,),
        ).fetchall()


def get_matches_by_patch_role(
    patch: str, role: str, db_path: Path = DB_PATH
) -> list:
    with get_connection(db_path) as conn:
        return conn.execute(
            "SELECT * FROM matches WHERE patch = ? AND karthus_role = ? ORDER BY game_start DESC",
            (patch, role),
        ).fetchall()


def get_frames_for_match(match_id: str, db_path: Path = DB_PATH) -> list:
    with get_connection(db_path) as conn:
        return conn.execute(
            "SELECT * FROM timeline_frames WHERE match_id = ? ORDER BY frame_index",
            (match_id,),
        ).fetchall()


def get_frames_by_patch_role(
    patch: str, role: str, db_path: Path = DB_PATH
) -> Iterator[sqlite3.Row]:
    """Stream frames for a patch+role combination without loading all into RAM."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        PRAGMA journal_mode = WAL;
        PRAGMA foreign_keys = ON;
        PRAGMA cache_size   = -64000;
    """)
    try:
        cur = conn.execute(
            """
            SELECT tf.*
            FROM timeline_frames tf
            JOIN matches m ON tf.match_id = m.match_id
            WHERE m.patch = ? AND m.karthus_role = ?
            ORDER BY tf.match_id, tf.frame_index
            """,
            (patch, role),
        )
        yield from cur
    finally:
        conn.close()


def get_events_for_match(
    match_id: str,
    event_type: Optional[str] = None,
    db_path: Path = DB_PATH,
) -> list:
    with get_connection(db_path) as conn:
        if event_type:
            return conn.execute(
                "SELECT * FROM events WHERE match_id = ? AND event_type = ? ORDER BY timestamp_ms",
                (match_id, event_type),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM events WHERE match_id = ? ORDER BY timestamp_ms",
            (match_id,),
        ).fetchall()


def get_player(puuid: str, db_path: Path = DB_PATH) -> Optional[sqlite3.Row]:
    with get_connection(db_path) as conn:
        return conn.execute(
            "SELECT * FROM players WHERE puuid = ?", (puuid,)
        ).fetchone()


def find_player_by_name(
    game_name: str, tag_line: str, db_path: Path = DB_PATH
) -> Optional[sqlite3.Row]:
    with get_connection(db_path) as conn:
        return conn.execute(
            "SELECT * FROM players WHERE game_name = ? AND tag_line = ?",
            (game_name, tag_line),
        ).fetchone()


def get_players_by_tier(tier: str, db_path: Path = DB_PATH) -> list:
    with get_connection(db_path) as conn:
        return conn.execute(
            "SELECT * FROM players WHERE tier = ? ORDER BY lp DESC",
            (tier.upper(),),
        ).fetchall()


def get_collection_stats(db_path: Path = DB_PATH) -> dict:
    with get_connection(db_path) as conn:
        total_games = conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
        total_players = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        scanned = conn.execute(
            "SELECT COUNT(*) FROM scanned_players WHERE scanned_at IS NOT NULL"
        ).fetchone()[0]
        queued = conn.execute(
            "SELECT COUNT(*) FROM scanned_players WHERE scanned_at IS NULL"
        ).fetchone()[0]

        games_by_role = {
            row["karthus_role"]: row["cnt"]
            for row in conn.execute(
                "SELECT karthus_role, COUNT(*) AS cnt FROM matches GROUP BY karthus_role"
            ).fetchall()
        }
        patch_rows = conn.execute(
            "SELECT patch, COUNT(*) AS cnt FROM matches GROUP BY patch"
        ).fetchall()
        games_by_patch = {
            row["patch"]: row["cnt"]
            for row in sorted(patch_rows, key=lambda r: patch_sort_key(r["patch"]), reverse=True)
        }

    return {
        "total_games":   total_games,
        "total_players": total_players,
        "scanned":       scanned,
        "queued":        queued,
        "games_by_role":  games_by_role,
        "games_by_patch": games_by_patch,
    }
