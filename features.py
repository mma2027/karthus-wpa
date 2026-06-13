"""
features.py — Feature extraction from timeline_frames DB rows.

Feature vector layout (FEATURE_DIM = 384):
  [0   : 360]  Per-participant block (36 features × 10 players)
               Players ordered: Karthus's team first (slots 0–4), enemies (5–9)
               Per player: 7 economy + 2 position + 19 champion stats + 8 damage stats
  [360 : 372]  Team/objective event counters (12 values)
  [372 : 382]  Champion IDs / 200.0  (10 values; 0.0 when not available)
  [382 : 384]  Patch major, patch minor (2 floats)
"""

from __future__ import annotations

import json
from typing import Optional

import numpy as np

import db

# ── Per-participant field lists ───────────────────────────────────────────────

_ECONOMY_FIELDS = [
    "totalGold", "currentGold", "goldPerSecond",
    "level", "xp",
    "minionsKilled", "jungleMinionsKilled",
]  # 7 fields

_CHAMP_STAT_FIELDS = [
    "abilityPower", "armor", "armorPenetration",
    "attackDamage", "attackSpeed",
    "bonusArmorPenetration", "bonusMagicPenetration",
    "ccReduction", "cooldownReduction",
    "health", "healthMax",
    "healthRegen", "lifeSteal",
    "magicPenetration", "magicResist",
    "movementSpeed",
    "omnivamp", "physicalVamp", "spellVamp",
]  # 19 fields

_DAMAGE_FIELDS = [
    "magicDamageDone", "magicDamageTaken",
    "physicalDamageDone", "physicalDamageTaken",
    "trueDamageDone", "trueDamageTaken",
    "totalDamageDone", "totalDamageTaken",
]  # 8 fields

# 7 economy + 2 position (x,y) + 19 champion stats + 8 damage stats = 36
_FEATURES_PER_PARTICIPANT = 36
_N_PARTICIPANTS            = 10
_PARTICIPANT_DIM           = _FEATURES_PER_PARTICIPANT * _N_PARTICIPANTS  # 360
_EVENT_DIM                 = 12
_CHAMP_ID_DIM              = 10
_PATCH_DIM                 = 2

FEATURE_DIM = _PARTICIPANT_DIM + _EVENT_DIM + _CHAMP_ID_DIM + _PATCH_DIM  # 384


# ---------------------------------------------------------------------------
# Core vector builder
# ---------------------------------------------------------------------------

def _participant_block(
    pid: int,
    economy: dict,
    positions: dict,
    champ_stats: dict,
    damage_stats: dict,
) -> list[float]:
    """36-element list for one participant. JSON blob dicts use string keys."""
    s   = str(pid)
    eco = economy.get(s, {})
    pos = positions.get(s, {})
    cs  = champ_stats.get(s, {})
    ds  = damage_stats.get(s, {})

    row: list[float] = []
    for f in _ECONOMY_FIELDS:
        row.append(float(eco.get(f, 0)))
    row.append(float(pos.get("x", 0)))
    row.append(float(pos.get("y", 0)))
    for f in _CHAMP_STAT_FIELDS:
        row.append(float(cs.get(f, 0)))
    for f in _DAMAGE_FIELDS:
        row.append(float(ds.get(f, 0)))
    return row


def build_feature_vector(
    frame_row,                                       # sqlite3.Row from timeline_frames
    karthus_pid: int,                                # 1-based participantId
    karthus_side: int,                               # 100 (blue) or 200 (red)
    patch: str,                                      # "16.10"
    champ_ids: Optional[dict[int, int]] = None,      # {pid: champion_id} or None
) -> np.ndarray:
    """
    Build a (384,) float32 feature vector from one timeline frame row.

    champ_ids is optional. Pass None to use 0.0 for all champion ID slots.
    """
    economy      = json.loads(frame_row["economy_json"]        or "{}")
    positions    = json.loads(frame_row["positions_json"]      or "{}")
    champ_stats  = json.loads(frame_row["champion_stats_json"] or "{}")
    damage_stats = json.loads(frame_row["damage_stats_json"]   or "{}")

    # Participant ordering: Karthus's team always occupies slots 0–4
    if karthus_side == 100:
        our_pids   = list(range(1, 6))
        enemy_pids = list(range(6, 11))
    else:
        our_pids   = list(range(6, 11))
        enemy_pids = list(range(1, 6))
    ordered_pids = our_pids + enemy_pids

    # A. Per-participant block (360 features)
    participant_vec: list[float] = []
    for pid in ordered_pids:
        participant_vec.extend(
            _participant_block(pid, economy, positions, champ_stats, damage_stats)
        )

    # B. Event counters (12 features)
    ts_norm = float(frame_row["timestamp_ms"]) / (50.0 * 60.0 * 1000.0)
    event_vec = [
        float(frame_row["team_kills"]),
        float(frame_row["enemy_kills"]),
        float(frame_row["team_dragons"]),
        float(frame_row["enemy_dragons"]),
        float(frame_row["team_baron"]),
        float(frame_row["enemy_baron"]),
        float(frame_row["team_turrets"]),
        float(frame_row["enemy_turrets"]),
        float(frame_row["team_inhibs"]),
        float(frame_row["enemy_inhibs"]),
        float(frame_row["dragon_soul"]),
        ts_norm,
    ]

    # C. Champion IDs (10 features)
    if champ_ids:
        champ_id_vec = [float(champ_ids.get(pid, 0)) / 200.0 for pid in ordered_pids]
    else:
        champ_id_vec = [0.0] * _CHAMP_ID_DIM

    # D. Patch encoding (2 features)
    parts = patch.split(".")
    patch_vec = [
        float(parts[0]) if len(parts) >= 1 else 0.0,
        float(parts[1]) if len(parts) >= 2 else 0.0,
    ]

    vec = participant_vec + event_vec + champ_id_vec + patch_vec
    return np.array(vec, dtype=np.float32)


# ---------------------------------------------------------------------------
# Training data loader
# ---------------------------------------------------------------------------

def load_training_data(
    role: str,
    patches: Optional[set[str]] = None,
) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """
    Load all timeline frames for a given role (optional patch filter).

    Returns:
        X          — (N, 384) float32  feature matrix
        y          — (N,)     float32  labels: 0.0 = loss, 1.0 = win
        match_ids  — length-N list of match IDs, parallel to X and y
                     Use unique(match_ids) for game-level train/val splits.
    """
    with db.get_connection() as conn:
        if patches:
            placeholders = ",".join("?" * len(patches))
            match_rows = conn.execute(
                f"SELECT match_id, win, patch, karthus_participant, karthus_side "
                f"FROM matches WHERE karthus_role = ? AND patch IN ({placeholders})",
                [role.upper()] + list(patches),
            ).fetchall()
        else:
            match_rows = conn.execute(
                "SELECT match_id, win, patch, karthus_participant, karthus_side "
                "FROM matches WHERE karthus_role = ?",
                (role.upper(),),
            ).fetchall()

    if not match_rows:
        return (
            np.empty((0, FEATURE_DIM), dtype=np.float32),
            np.empty(0, dtype=np.float32),
            [],
        )

    X_parts:  list[np.ndarray] = []
    y_parts:  list[float]      = []
    mid_list: list[str]        = []

    for m in match_rows:
        match_id     = m["match_id"]
        win          = float(m["win"])
        patch        = m["patch"] or "0.0"
        karthus_pid  = m["karthus_participant"]
        karthus_side = m["karthus_side"]

        frames = db.get_frames_for_match(match_id)
        if not frames:
            continue

        for frame in frames:
            X_parts.append(build_feature_vector(frame, karthus_pid, karthus_side, patch))
            y_parts.append(win)
            mid_list.append(match_id)

    if not X_parts:
        return (
            np.empty((0, FEATURE_DIM), dtype=np.float32),
            np.empty(0, dtype=np.float32),
            [],
        )

    return np.vstack(X_parts), np.array(y_parts, dtype=np.float32), mid_list
