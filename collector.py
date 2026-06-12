"""
collector.py — Karthus game collection pipeline.

Two modes
---------
Seed mode   (--seed "SqfeWalk#NA1" [--max-players N])
    BFS starting from one player. For the seed player, fetches ALL recent
    ranked games to discover a wide pool of opponents/teammates. For every
    discovered player, fetches only their Karthus games.

Ladder mode (default, no --seed)
    Seeds from the NA master/GM/challenger ladder (~2,500 players) then
    runs the same BFS expansion.

Both modes are resumable: already-scanned PUUIDs and already-stored matches
are skipped via INSERT OR IGNORE / is_scanned() checks.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Optional

import aiohttp
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)

import db
from riot_client import KARTHUS_ID, RiotAPIError, RiotClient, make_session

console = Console()

# ---------------------------------------------------------------------------
# Patch helper
# ---------------------------------------------------------------------------

def _parse_patch(game_version: str) -> str:
    """'15.10.675.4890'  →  '15.10'"""
    parts = game_version.split(".")
    if len(parts) >= 2:
        return f"{parts[0]}.{parts[1]}"
    return game_version


# ---------------------------------------------------------------------------
# Game storage helpers
# ---------------------------------------------------------------------------

def _find_karthus_participant(info: dict) -> Optional[dict]:
    """Return the participant dict for Karthus (champion ID 30), or None."""
    for p in info.get("participants", []):
        if p.get("championId") == KARTHUS_ID:
            return p
    return None


def _normalize_role(participant: dict) -> str:
    pos = participant.get("teamPosition") or participant.get("individualPosition") or ""
    mapping = {
        "TOP":     "TOP",
        "JUNGLE":  "JUNGLE",
        "MIDDLE":  "MID",
        "BOTTOM":  "BOTTOM",
        "UTILITY": "SUPPORT",
    }
    return mapping.get(pos.upper(), pos.upper() or "UNKNOWN")


def _store_game(match_id: str, match_data: dict, timeline_data: dict) -> bool:
    """
    Parse and persist a full Karthus game.
    Returns True if stored, False if Karthus not found or already stored.
    """
    info = match_data.get("info", {})

    kp = _find_karthus_participant(info)
    if kp is None:
        return False  # no Karthus in this game (shouldn't happen, but guard)

    patch           = _parse_patch(info.get("gameVersion", "0.0"))
    game_start      = info.get("gameStartTimestamp", 0)
    game_duration_s = info.get("gameDuration", 0)
    win             = 1 if kp.get("win") else 0
    karthus_puuid   = kp.get("puuid", "")
    karthus_pid     = kp.get("participantId", 0)  # 1-based
    karthus_role    = _normalize_role(kp)
    karthus_side    = kp.get("teamId", 100)  # 100=blue, 200=red

    inserted = db.insert_match(
        match_id, game_start, game_duration_s, patch, win,
        karthus_puuid, karthus_pid, karthus_role, karthus_side,
    )
    if not inserted:
        return False  # already in DB

    # --- End-of-game stats ---
    db.insert_match_stats(match_id, kp)

    # --- Timeline frames + events ---
    _store_timeline(match_id, timeline_data, karthus_pid, karthus_side)

    return True


def _store_timeline(
    match_id: str, timeline_data: dict, karthus_pid: int, karthus_side: int
) -> None:
    info   = timeline_data.get("info", {})
    frames = info.get("frames", [])

    # Running event counters accumulated frame-by-frame
    team_kills = enemy_kills = 0
    team_dragons = enemy_dragons = 0
    team_baron = enemy_baron = 0
    team_turrets = enemy_turrets = 0
    team_inhibs = enemy_inhibs = 0
    dragon_soul = 0
    baron_timer: dict[int, int] = {}  # teamId → timestamp when buff expires (~180s)

    # Determine which participant IDs are on Karthus's team
    karthus_team = karthus_side  # 100 or 200

    for frame_idx, frame in enumerate(frames):
        ts = frame.get("timestamp", 0)

        # Process events in this frame to update counters
        for ev in frame.get("events", []):
            etype = ev.get("type", "")

            if etype == "CHAMPION_KILL":
                killer_team = _pid_to_team(ev.get("killerId", 0), info)
                if killer_team == karthus_team:
                    team_kills += 1
                elif killer_team is not None:
                    enemy_kills += 1

            elif etype == "ELITE_MONSTER_KILL":
                monster     = ev.get("monsterType", "")
                killer_team = ev.get("killerTeamId", ev.get("teamId", 0))
                if monster == "DRAGON":
                    if killer_team == karthus_team:
                        team_dragons += 1
                    else:
                        enemy_dragons += 1
                elif monster == "BARON_NASHOR":
                    if killer_team == karthus_team:
                        team_baron  = 1
                        baron_timer[karthus_team] = ts + 180_000
                    else:
                        enemy_baron = 1
                        baron_timer[200 if karthus_team == 100 else 100] = ts + 180_000

            elif etype == "BUILDING_KILL":
                building    = ev.get("buildingType", "")
                victim_team = ev.get("teamId", 0)  # team that LOST the building
                if building == "TOWER_BUILDING":
                    if victim_team != karthus_team:
                        team_turrets += 1
                    else:
                        enemy_turrets += 1
                elif building == "INHIBITOR_BUILDING":
                    if victim_team != karthus_team:
                        team_inhibs += 1
                    else:
                        enemy_inhibs += 1

            elif etype == "DRAGON_SOUL_GIVEN":
                if ev.get("teamId") == karthus_team:
                    dragon_soul = 1

        # Check baron buff expiry
        if karthus_team in baron_timer and ts > baron_timer[karthus_team]:
            team_baron = 0
        enemy_team = 200 if karthus_team == 100 else 100
        if enemy_team in baron_timer and ts > baron_timer[enemy_team]:
            enemy_baron = 0

        # Build per-participant blobs
        pf = frame.get("participantFrames", {})

        economy_blob       = {}
        positions_blob     = {}
        champ_stats_blob   = {}
        damage_stats_blob  = {}
        items_blob         = {}

        for pid_str, pdata in pf.items():
            pid = int(pid_str)
            economy_blob[pid] = {
                "totalGold":          pdata.get("totalGold", 0),
                "currentGold":        pdata.get("currentGold", 0),
                "goldPerSecond":      pdata.get("goldPerSecond", 0),
                "level":              pdata.get("level", 1),
                "xp":                 pdata.get("xp", 0),
                "minionsKilled":      pdata.get("minionsKilled", 0),
                "jungleMinionsKilled":pdata.get("jungleMinionsKilled", 0),
            }
            pos = pdata.get("position", {})
            positions_blob[pid] = {"x": pos.get("x", 0), "y": pos.get("y", 0)}

            cs = pdata.get("championStats", {})
            champ_stats_blob[pid] = {
                "abilityPower":         cs.get("abilityPower", 0),
                "armor":                cs.get("armor", 0),
                "armorPenetration":     cs.get("armorPenetration", 0),
                "attackDamage":         cs.get("attackDamage", 0),
                "attackSpeed":          cs.get("attackSpeed", 0),
                "bonusArmorPenetration":cs.get("bonusArmorPenetration", 0),
                "bonusMagicPenetration":cs.get("bonusMagicPenetration", 0),
                "ccReduction":          cs.get("ccReduction", 0),
                "cooldownReduction":    cs.get("cooldownReduction", 0),
                "health":               cs.get("health", 0),
                "healthMax":            cs.get("healthMax", 1),
                "healthRegen":          cs.get("healthRegen", 0),
                "lifeSteal":            cs.get("lifeSteal", 0),
                "magicPenetration":     cs.get("magicPenetration", 0),
                "magicResist":          cs.get("magicResist", 0),
                "movementSpeed":        cs.get("movementSpeed", 0),
                "omnivamp":             cs.get("omnivamp", 0),
                "physicalVamp":         cs.get("physicalVamp", 0),
                "spellVamp":            cs.get("spellVamp", 0),
            }

            ds = pdata.get("damageStats", {})
            damage_stats_blob[pid] = {
                "magicDamageDone":      ds.get("magicDamageDone", 0),
                "magicDamageTaken":     ds.get("magicDamageTaken", 0),
                "physicalDamageDone":   ds.get("physicalDamageDone", 0),
                "physicalDamageTaken":  ds.get("physicalDamageTaken", 0),
                "trueDamageDone":       ds.get("trueDamageDone", 0),
                "trueDamageTaken":      ds.get("trueDamageTaken", 0),
                "totalDamageDone":      ds.get("totalDamageDone", 0),
                "totalDamageTaken":     ds.get("totalDamageTaken", 0),
            }

            items_blob[pid] = pdata.get("inventory", pdata.get("items", []))

        db.insert_timeline_frame(
            match_id, frame_idx, ts,
            team_kills, enemy_kills,
            team_dragons, enemy_dragons,
            team_baron, enemy_baron,
            team_turrets, enemy_turrets,
            team_inhibs, enemy_inhibs,
            dragon_soul,
            economy_blob, positions_blob,
            champ_stats_blob, damage_stats_blob, items_blob,
        )

    # --- Karthus-specific events ---
    _store_karthus_events(match_id, frames, karthus_pid, karthus_side)


def _pid_to_team(pid: int, info: dict) -> Optional[int]:
    """Map a 1-based participantId → teamId (100 or 200)."""
    if pid == 0:
        return None
    for p in info.get("participants", []):
        if p.get("participantId") == pid:
            return p.get("teamId")
    return None


def _store_karthus_events(
    match_id: str, frames: list, karthus_pid: int, karthus_side: int
) -> None:
    prev_damage = 0  # track frame-level totalDamageDone for passive detection

    for frame in frames:
        ts  = frame.get("timestamp", 0)
        pf  = frame.get("participantFrames", {})
        kpf = pf.get(str(karthus_pid), {})

        # Current Karthus total damage (for passive heuristic)
        curr_damage = kpf.get("damageStats", {}).get("totalDamageDone", 0)

        for ev in frame.get("events", []):
            etype = ev.get("type", "")

            # ── R Kill ──────────────────────────────────────────────────
            if etype == "CHAMPION_KILL" and ev.get("killerId") == karthus_pid:
                is_r_kill = any(
                    d.get("spellName", "") == "KarthusRequiem"
                    for d in ev.get("victimDamageReceived", [])
                )
                if is_r_kill:
                    db.insert_event(match_id, ts, "R_KILL", {
                        "victim_id": ev.get("victimId"),
                        "position":  ev.get("position", {}),
                        "bounty":    ev.get("bounty", 0),
                    })

            # ── Death ────────────────────────────────────────────────────
            elif etype == "CHAMPION_KILL" and ev.get("victimId") == karthus_pid:
                # Passive used heuristic: damage increased in this frame after death
                passive_used = curr_damage > prev_damage
                db.insert_event(match_id, ts, "DEATH", {
                    "killer_id":    ev.get("killerId"),
                    "position":     ev.get("position", {}),
                    "passive_used": passive_used,
                })

            # ── Item Purchase ─────────────────────────────────────────────
            elif etype == "ITEM_PURCHASED" and ev.get("participantId") == karthus_pid:
                db.insert_event(match_id, ts, "ITEM_PURCHASE", {
                    "item_id": ev.get("itemId"),
                })

            # ── Dragon ───────────────────────────────────────────────────
            elif etype == "ELITE_MONSTER_KILL" and ev.get("monsterType") == "DRAGON":
                killer_team = ev.get("killerTeamId", ev.get("teamId", 0))
                if killer_team == karthus_side:
                    assists = ev.get("assistingParticipantIds", [])
                    participated = (
                        ev.get("killerId") == karthus_pid or karthus_pid in assists
                    )
                    if participated:
                        db.insert_event(match_id, ts, "DRAGON", {
                            "sub_type": ev.get("monsterSubType", ""),
                            "position": ev.get("position", {}),
                        })

            # ── Baron ────────────────────────────────────────────────────
            elif etype == "ELITE_MONSTER_KILL" and ev.get("monsterType") == "BARON_NASHOR":
                killer_team = ev.get("killerTeamId", ev.get("teamId", 0))
                if killer_team == karthus_side:
                    assists = ev.get("assistingParticipantIds", [])
                    participated = (
                        ev.get("killerId") == karthus_pid or karthus_pid in assists
                    )
                    if participated:
                        db.insert_event(match_id, ts, "BARON", {
                            "position": ev.get("position", {}),
                        })

            # ── Dragon Soul ──────────────────────────────────────────────
            elif etype == "DRAGON_SOUL_GIVEN" and ev.get("teamId") == karthus_side:
                db.insert_event(match_id, ts, "DRAGON_SOUL", {
                    "soul": ev.get("name", ev.get("soul", "")),
                })

        prev_damage = curr_damage


# ---------------------------------------------------------------------------
# Player upsert from match participant data
# ---------------------------------------------------------------------------

def _upsert_players_from_match(match_data: dict, region: str) -> list[str]:
    """
    Extract all 10 participant PUUIDs + names from match data and upsert
    into the players table. Returns list of PUUIDs.
    """
    puuids = []
    for p in match_data.get("info", {}).get("participants", []):
        puuid     = p.get("puuid", "")
        game_name = p.get("riotIdGameName", "")
        tag_line  = p.get("riotIdTagline", "")
        if puuid and game_name:
            db.upsert_player(puuid, game_name, tag_line, region)
            puuids.append(puuid)
    return puuids


# ---------------------------------------------------------------------------
# Core BFS collector
# ---------------------------------------------------------------------------

async def _process_player(
    client: RiotClient,
    puuid: str,
    seed_puuid: str,
    region: str,
    progress: Progress,
    games_task,
    players_task,
    queue: deque,
    visited: set,
    all_match_cache: dict,  # match_id → metadata (participants list)
    verbose: bool = False,
) -> None:
    """Fetch and store all Karthus games for one player, then expand queue."""
    is_seed = puuid == seed_puuid

    try:
        # 1. Resolve summoner details + rank (non-fatal if it fails)
        summoner    = await client.get_summoner_by_puuid(puuid)
        summoner_id = summoner.get("id", "")
        rank_info   = await client.get_rank(summoner_id) if summoner_id else None

        # Upsert with empty name strings — db preserves existing non-empty values
        db.upsert_player(
            puuid       = puuid,
            game_name   = "",
            tag_line    = "",
            region      = region,
            summoner_id = summoner_id,
            tier        = rank_info.get("tier")         if rank_info else None,
            division    = rank_info.get("rank")         if rank_info else None,
            lp          = rank_info.get("leaguePoints") if rank_info else None,
            wins        = rank_info.get("wins")         if rank_info else None,
            losses      = rank_info.get("losses")       if rank_info else None,
        )

    except RiotAPIError:
        pass  # non-fatal; player row already upserted from match data

    if verbose:
        player_row = db.get_player(puuid)
        if player_row and player_row["game_name"]:
            name_str = f"[bold]{player_row['game_name']}#{player_row['tag_line']}[/bold]"
            rank_str = ""
            if player_row["tier"]:
                rank_str = f"  [dim]{player_row['tier']} {player_row['division'] or ''} {player_row['lp'] or 0} LP[/dim]"
        else:
            name_str = f"[dim]{puuid[:16]}…[/dim]"
            rank_str = ""
        console.print(f"[cyan]→[/cyan] {name_str}{rank_str}")

    try:
        # 2. For seed player: fetch all recent games (to widen BFS net)
        #    For everyone else: only Karthus games
        if is_seed:
            all_ids = await client.get_match_ids(puuid, champion=None, count=50)
        else:
            all_ids = []

        karthus_ids = await client.get_match_ids(puuid, champion=KARTHUS_ID, count=100)

    except RiotAPIError:
        db.mark_scanned(puuid)
        return

    # 3. Fetch + store each new Karthus game
    new_ids = [mid for mid in karthus_ids if not db.match_exists(mid)]
    for mid in new_ids:
        try:
            if verbose:
                console.print(f"  [dim]fetching {mid}…[/dim]")
            match_data    = await client.get_match(mid)
            timeline_data = await client.get_match_timeline(mid)
            stored = _store_game(mid, match_data, timeline_data)
            if stored:
                progress.advance(games_task)
                if verbose:
                    info = match_data.get("info", {})
                    kp   = _find_karthus_participant(info)
                    role = _normalize_role(kp) if kp else "?"
                    patch = _parse_patch(info.get("gameVersion", "0.0"))
                    result = "[green]WIN[/green]" if kp and kp.get("win") else "[red]LOSS[/red]"
                    console.print(f"  [green]✓[/green] stored  {result}  {role}  patch {patch}")
            # Cache participant list for BFS expansion
            all_match_cache[mid] = match_data.get("metadata", {}).get("participants", [])
            # Upsert names of all players in this game
            _upsert_players_from_match(match_data, region)
        except RiotAPIError:
            continue

    # 4. Queue participants from all fetched match IDs (seed: all, others: Karthus only)
    expansion_ids = set(all_ids) | set(karthus_ids)
    for mid in expansion_ids:
        if mid not in all_match_cache:
            # We need participant list but don't have match data yet
            # Only load if not already stored (avoid redundant fetch for seed all-games)
            try:
                meta = await client.get_match(mid)
                all_match_cache[mid] = meta.get("metadata", {}).get("participants", [])
                _upsert_players_from_match(meta, region)
            except RiotAPIError:
                continue

        for participant_puuid in all_match_cache.get(mid, []):
            if participant_puuid not in visited:
                queue.append(participant_puuid)
                db.queue_puuid(participant_puuid)

    db.mark_scanned(puuid)
    progress.advance(players_task)


async def run_collection(
    seed: Optional[str] = None,
    max_players: Optional[int] = None,
    api_key: Optional[str] = None,
    platform: str = "na1",
    verbose: bool = False,
) -> None:
    """
    Main entry point for the collection pipeline.

    seed        : "GameName#TAG" to start BFS from one player
                  None  → seed from NA master/GM/challenger ladder
    max_players : stop after processing this many unique players (testing)
    """
    db.init_db()

    region   = platform  # store region same as platform string
    visited: set[str] = set()
    queue: deque[str] = deque()
    all_match_cache: dict[str, list] = {}

    async with make_session() as session:
        client = RiotClient(api_key=api_key, platform=platform, session=session)

        # ── Determine seed PUUIDs ──────────────────────────────────────
        seed_puuid: str = ""

        if seed:
            # Parse "GameName#TAG"
            if "#" in seed:
                game_name, tag_line = seed.rsplit("#", 1)
            else:
                game_name, tag_line = seed, platform.upper()

            try:
                acct = await client.get_account_by_riot_id(game_name, tag_line)
                seed_puuid = acct["puuid"]
                db.upsert_player(seed_puuid, acct["gameName"], acct["tagLine"], region)
                queue.append(seed_puuid)
                console.print(
                    f"[cyan]Seed:[/cyan] [bold]{acct['gameName']}#{acct['tagLine']}[/bold]  "
                    f"PUUID {seed_puuid[:16]}…"
                )
            except (RiotAPIError, KeyError) as e:
                console.print(f"[red]Could not resolve seed player: {e}[/red]")
                return
        else:
            # Ladder mode — resolve summonerId → PUUID for each ladder entry
            console.print("[cyan]Fetching NA master/GM/challenger ladder…[/cyan]")
            for tier in ("challenger", "grandmaster", "master"):
                try:
                    entries = await client.get_ladder(tier)
                    # Resolve summoner IDs to PUUIDs concurrently (batched)
                    batch_size = 18
                    resolved = 0
                    for i in range(0, len(entries), batch_size):
                        batch = entries[i : i + batch_size]
                        tasks = [
                            client.get_summoner_by_id(e["summonerId"])
                            for e in batch
                            if e.get("summonerId")
                        ]
                        results = await asyncio.gather(*tasks, return_exceptions=True)
                        for summ in results:
                            if isinstance(summ, Exception):
                                continue
                            puuid = summ.get("puuid", "")
                            if puuid and puuid not in visited:
                                queue.append(puuid)
                                db.queue_puuid(puuid)
                                resolved += 1
                    console.print(f"  {tier}: {len(entries)} entries → {resolved} PUUIDs queued")
                except RiotAPIError as e:
                    console.print(f"  [yellow]Ladder {tier} failed: {e}[/yellow]")

        if not queue:
            console.print("[red]Queue is empty — nothing to collect.[/red]")
            return

        # ── BFS collection ─────────────────────────────────────────────
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            players_task = progress.add_task(
                "[cyan]Players scanned[/cyan]", total=max_players
            )
            games_task = progress.add_task(
                "[green]Karthus games stored[/green]", total=None
            )

            while queue:
                if max_players is not None and len(visited) >= max_players:
                    break

                puuid = queue.popleft()
                if puuid in visited or db.is_scanned(puuid):
                    continue
                visited.add(puuid)

                await _process_player(
                    client, puuid, seed_puuid, region,
                    progress, games_task, players_task,
                    queue, visited, all_match_cache,
                    verbose=verbose,
                )

    stats = db.get_collection_stats()
    console.print()
    console.print(
        f"[bold green]Collection complete.[/bold green]  "
        f"Games: [bold]{stats['total_games']}[/bold]  "
        f"Players: [bold]{stats['total_players']}[/bold]"
    )
    if stats["games_by_role"]:
        console.print("Games by role: " + "  ".join(
            f"{role}={cnt}" for role, cnt in sorted(stats["games_by_role"].items())
        ))
    if stats["games_by_patch"]:
        top_patches = list(stats["games_by_patch"].items())[:5]
        console.print("Games by patch: " + "  ".join(
            f"{p}={cnt}" for p, cnt in top_patches
        ))
