"""
wpa.py — Win Probability Added (WPA) calculator.

WPA for an event = change in win probability across the nearest frame boundary:
    wpa = (p_win[frame_after_event] - p_win[frame_before_event]) × 100  (pp)

Positive WPA means the event helped win; negative means it hurt.
"""

from __future__ import annotations

import json
from typing import Optional

import numpy as np

import db
import features as feat
import model as mdl

# Module-level model cache: role.upper() → (WinProbNet, StandardScaler)
_model_cache: dict[str, tuple] = {}


def _get_model(role: str):
    role = role.upper()
    if role not in _model_cache:
        _model_cache[role] = mdl.load_model(role)
    return _model_cache[role]


# ---------------------------------------------------------------------------
# Per-game WPA
# ---------------------------------------------------------------------------

def compute_game_wpa(match_id: str) -> list[dict]:
    """
    Compute WPA for every stored event in a game.

    Returns a list of plain dicts (one per event) with two extra keys:
        'wpa'          — float, percentage points
        'p_win_before' — float [0,1], model win probability just before event

    Raises FileNotFoundError if no model is trained for this game's role.
    """
    match_row = db.get_match(match_id)
    if match_row is None:
        return []

    role         = match_row["karthus_role"]
    karthus_pid  = match_row["karthus_participant"]
    karthus_side = match_row["karthus_side"]
    patch        = match_row["patch"] or "0.0"

    net, scaler = _get_model(role)  # raises FileNotFoundError if missing

    frames = db.get_frames_for_match(match_id)
    if not frames:
        return []

    # Build (n_frames, 384) feature matrix and predict
    X = np.vstack([
        feat.build_feature_vector(f, karthus_pid, karthus_side, patch)
        for f in frames
    ])
    p_win = mdl.predict_proba(net, scaler, X)  # (n_frames,)

    frame_ts = [f["timestamp_ms"] for f in frames]
    events   = db.get_events_for_match(match_id)
    result   = []

    for ev_row in events:
        ev = dict(ev_row)
        t  = ev["timestamp_ms"]

        before_idx = _last_frame_at_or_before(frame_ts, t)
        after_idx  = min(before_idx + 1, len(frames) - 1)

        ev["wpa"]          = round(float(p_win[after_idx] - p_win[before_idx]) * 100, 2)
        ev["p_win_before"] = round(float(p_win[before_idx]), 4)
        result.append(ev)

    return result


def _last_frame_at_or_before(frame_ts: list[int], event_ts: int) -> int:
    """Binary search: index of the last frame whose timestamp ≤ event_ts."""
    lo, hi = 0, len(frame_ts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if frame_ts[mid] <= event_ts:
            lo = mid
        else:
            hi = mid - 1
    return lo


# ---------------------------------------------------------------------------
# Aggregate WPA over multiple games
# ---------------------------------------------------------------------------

def aggregate_player_wpa(
    puuid: str,
    role: Optional[str] = None,
    n_games: int = 20,
) -> dict:
    """
    Aggregate WPA statistics for a player across their most recent n_games.

    Returns:
    {
        'game_count': int,
        'win_rate':   float,
        'by_event': {
            'R_KILL': {'count': int, 'avg_wpa': float, 'game_count': int},
            ...
        }
    }

    Raises FileNotFoundError if no model is trained for the player's role.
    """
    matches = db.get_matches_for_player(puuid)
    if role:
        matches = [m for m in matches if m["karthus_role"] == role.upper()]
    matches = matches[:n_games]

    if not matches:
        return {"game_count": 0, "win_rate": 0.0, "by_event": {}}

    win_count = sum(1 for m in matches if m["win"])
    # accum[etype] = {"count": int, "wpa_sum": float, "game_set": set[str]}
    accum: dict[str, dict] = {}

    for m in matches:
        try:
            events = compute_game_wpa(m["match_id"])
        except FileNotFoundError:
            raise
        except Exception:
            continue  # skip games with transient errors (missing frames, etc.)

        for ev in events:
            etype = ev["event_type"]
            if etype not in accum:
                accum[etype] = {"count": 0, "wpa_sum": 0.0, "game_set": set()}
            accum[etype]["count"]   += 1
            accum[etype]["wpa_sum"] += ev["wpa"]
            accum[etype]["game_set"].add(m["match_id"])

    by_event = {
        etype: {
            "count":      v["count"],
            "avg_wpa":    round(v["wpa_sum"] / v["count"], 2) if v["count"] else 0.0,
            "game_count": len(v["game_set"]),
        }
        for etype, v in accum.items()
    }

    return {
        "game_count": len(matches),
        "win_rate":   round(win_count / len(matches), 3),
        "by_event":   by_event,
    }


# ---------------------------------------------------------------------------
# Item WPA tier list
# ---------------------------------------------------------------------------

def item_wpa_tierlist(
    role: str,
    purchase_rank: int = 1,
    min_games: int = 3,
) -> list[dict]:
    """
    Average WPA for the Nth item purchase (by timestamp) across all games.

    purchase_rank=1: first completed item per game
    purchase_rank=2: second completed item

    Returns [{item_id, avg_wpa, game_count}] sorted by avg_wpa descending.
    Raises FileNotFoundError if no model is trained for this role.
    """
    with db.get_connection() as conn:
        match_rows = conn.execute(
            "SELECT match_id FROM matches WHERE karthus_role = ?",
            (role.upper(),),
        ).fetchall()

    if not match_rows:
        return []

    accum: dict[int, dict] = {}  # item_id → {"wpa_sum": float, "count": int}

    for m in match_rows:
        mid = m["match_id"]
        try:
            events = compute_game_wpa(mid)
        except FileNotFoundError:
            raise
        except Exception:
            continue

        item_events = sorted(
            [e for e in events if e["event_type"] == "ITEM_PURCHASE"],
            key=lambda e: e["timestamp_ms"],
        )

        if len(item_events) < purchase_rank:
            continue

        target = item_events[purchase_rank - 1]
        detail  = json.loads(target.get("detail_json") or "{}")
        item_id = detail.get("item_id")
        if not item_id:
            continue

        if item_id not in accum:
            accum[item_id] = {"wpa_sum": 0.0, "count": 0}
        accum[item_id]["wpa_sum"] += target["wpa"]
        accum[item_id]["count"]   += 1

    result = [
        {
            "item_id":    item_id,
            "avg_wpa":    round(v["wpa_sum"] / v["count"], 2),
            "game_count": v["count"],
        }
        for item_id, v in accum.items()
        if v["count"] >= min_games
    ]
    result.sort(key=lambda x: x["avg_wpa"], reverse=True)
    return result


# ---------------------------------------------------------------------------
# Rune win rate tier list
# ---------------------------------------------------------------------------

def rune_tierlist(
    role: str,
    min_games: int = 3,
) -> list[dict]:
    """
    Win rate by keystone rune across all stored games for a role.

    Returns [{keystone_id, win_rate, game_count}] sorted by win_rate descending.
    No model required — uses match outcomes directly.
    """
    with db.get_connection() as conn:
        rows = conn.execute(
            """
            SELECT ms.perks_json, m.win
            FROM match_stats ms
            JOIN matches m ON ms.match_id = m.match_id
            WHERE m.karthus_role = ?
            """,
            (role.upper(),),
        ).fetchall()

    if not rows:
        return []

    accum: dict[int, dict] = {}  # keystone_id → {"wins": int, "count": int}

    for row in rows:
        try:
            perks = json.loads(row["perks_json"] or "{}")
            keystone_id = perks["styles"][0]["selections"][0]["perk"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            continue

        if keystone_id not in accum:
            accum[keystone_id] = {"wins": 0, "count": 0}
        accum[keystone_id]["count"] += 1
        if row["win"]:
            accum[keystone_id]["wins"] += 1

    result = [
        {
            "keystone_id": keystone_id,
            "win_rate":    round(v["wins"] / v["count"], 3),
            "game_count":  v["count"],
        }
        for keystone_id, v in accum.items()
        if v["count"] >= min_games
    ]
    result.sort(key=lambda x: x["win_rate"], reverse=True)
    return result
