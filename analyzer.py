"""
analyzer.py — Rich terminal output for WPA analysis.

Entry points:
    print_player_analysis(name_tag, role, n_games)
    print_item_tierlist(role, purchase_rank)
    print_rune_analysis(role)
"""

from __future__ import annotations

import json
import urllib.request
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table, box

import db
import wpa as _wpa

console = Console()

# ── Item cache (fetched from Data Dragon on first use) ───────────────────────

_item_names: dict[int, str]  = {}
_item_data:  dict[int, dict] = {}   # full item data for filtering
_item_names_loaded            = False
_rune_names: dict[int, str]  = {}
_rune_names_loaded            = False


def _get_item_names() -> dict[int, str]:
    global _item_names, _item_data, _item_names_loaded
    if _item_names_loaded:
        return _item_names
    _item_names_loaded = True
    try:
        with urllib.request.urlopen(
            "https://ddragon.leagueoflegends.com/api/versions.json", timeout=5
        ) as r:
            versions = json.loads(r.read())
        version = versions[0]
        url = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/item.json"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        raw         = data["data"]
        _item_data  = {int(k): v for k, v in raw.items()}
        _item_names = {int(k): v["name"] for k, v in raw.items()}
    except Exception:
        pass
    return _item_names


# Starter items are a fixed, intentional set — heuristics are too broad
# because basic components (Amplifying Tome, Sapphire Crystal, etc.) also
# have no sub-ingredients and low cost.
_STARTER_ITEM_IDS: frozenset[int] = frozenset({
    1054,  # Doran's Shield
    1055,  # Doran's Blade
    1056,  # Doran's Ring
    1082,  # Dark Seal
    1086,  # Cull
})


def _is_valid_tierlist_item(item_id: int) -> bool:
    """
    Return True only for starter items and completed legendary items.

    Starters   : hardcoded set (Doran's items, Dark Seal, Cull).
    Legendaries: has recipe components, doesn't upgrade into another
                 *purchasable* item (Ornn upgrade entries are non-purchasable
                 and ignored), total cost ≥ 2 500 g.
    Excluded   : everything else — components, boots, trinkets, consumables,
                 wards, intermediate items, anything not in the shop.
    """
    if item_id in _STARTER_ITEM_IDS:
        return True

    d = _item_data.get(item_id)
    if d is None:
        return False  # unknown item: don't show garbage IDs

    tags = d.get("tags", [])
    gold = d.get("gold", {})

    if not gold.get("purchasable", False) or not d.get("inStore", True):
        return False
    if "Trinket" in tags or d.get("consumed") or "Boots" in tags:
        return False

    has_components = bool(d.get("from", []))
    total_cost     = gold.get("total", 0)

    # Legendary: built from components, doesn't build into another purchasable item
    into           = d.get("into", [])
    builds_further = any(
        _item_data.get(int(iid), {}).get("gold", {}).get("purchasable", False)
        for iid in into
    )
    if has_components and not builds_further and total_cost >= 2500:
        return True

    return False


def _get_rune_names() -> dict[int, str]:
    global _rune_names, _rune_names_loaded
    if _rune_names_loaded:
        return _rune_names
    _rune_names_loaded = True
    try:
        with urllib.request.urlopen(
            "https://ddragon.leagueoflegends.com/api/versions.json", timeout=5
        ) as r:
            versions = json.loads(r.read())
        version = versions[0]
        url = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/runesReforged.json"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        for tree in data:
            for slot in tree.get("slots", []):
                for rune in slot.get("runes", []):
                    _rune_names[rune["id"]] = rune["name"]
    except Exception:
        pass
    return _rune_names


def _fmt_wpa(wpa: float) -> str:
    s = f"{wpa:+.2f}pp"
    if wpa > 0:
        return f"[green]{s}[/green]"
    if wpa < 0:
        return f"[red]{s}[/red]"
    return f"[dim]{s}[/dim]"


# ── Event display order ──────────────────────────────────────────────────────

_EVENT_ORDER = [
    "R_KILL", "DEATH", "DRAGON", "BARON",
    "DRAGON_SOUL", "ITEM_PURCHASE",
]


# ---------------------------------------------------------------------------
# Player WPA analysis
# ---------------------------------------------------------------------------

def print_player_analysis(
    name_tag: str,
    role: Optional[str] = None,
    n_games: int = 20,
) -> None:
    """Print a WPA breakdown panel for a player."""
    if "#" in name_tag:
        game_name, tag_line = name_tag.rsplit("#", 1)
    else:
        game_name, tag_line = name_tag, ""

    player = db.find_player_by_name(game_name, tag_line)
    if not player:
        with db.get_connection() as conn:
            player = conn.execute(
                "SELECT * FROM players WHERE LOWER(game_name) = LOWER(?)",
                (game_name,),
            ).fetchone()
    if not player:
        console.print(f"[red]Player '{name_tag}' not found in database.[/red]")
        return

    puuid        = player["puuid"]
    display_name = f"{player['game_name']}#{player['tag_line']}"

    try:
        data = _wpa.aggregate_player_wpa(puuid, role=role, n_games=n_games)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        return

    if data["game_count"] == 0:
        suffix = f" as {role.upper()}" if role else ""
        console.print(f"[yellow]No games found for {display_name}{suffix}.[/yellow]")
        return

    wr_pct   = round(data["win_rate"] * 100, 1)
    wr_color = "green" if wr_pct >= 50 else "red"
    role_str = role.upper() if role else "ALL ROLES"

    console.print()
    console.print(Panel(
        f"[bold]{display_name}[/bold]  ·  {role_str}  ·  "
        f"Last {data['game_count']} games  ·  "
        f"[{wr_color}]{wr_pct}% WR[/{wr_color}]",
        title="[bold cyan]Karthus WPA Analysis[/bold cyan]",
        border_style="cyan",
        expand=False,
    ))

    if not data["by_event"]:
        console.print("[yellow]No events found in these games.[/yellow]")
        return

    t = Table(title="Event WPA", box=box.SIMPLE_HEAVY, show_header=True)
    t.add_column("Event",   style="bold", min_width=18)
    t.add_column("Count",   justify="right")
    t.add_column("Avg WPA", justify="right")
    t.add_column("Games",   justify="right")

    seen = set()
    for etype in _EVENT_ORDER + sorted(data["by_event"].keys()):
        if etype in seen or etype not in data["by_event"]:
            continue
        seen.add(etype)
        ev = data["by_event"][etype]
        t.add_row(
            etype.replace("_", " ").title(),
            str(ev["count"]),
            _fmt_wpa(ev["avg_wpa"]),
            str(ev["game_count"]),
        )

    console.print(t)


# ---------------------------------------------------------------------------
# Item WPA tier list
# ---------------------------------------------------------------------------

_ALL_ROLES  = ["MID", "JUNGLE", "BOTTOM", "SUPPORT", "TOP"]
_MAX_RANK   = 6   # maximum purchase slot to show when iterating all ranks


def print_item_tierlist(
    role: Optional[str] = None,
    purchase_rank: Optional[int] = None,
) -> None:
    """
    Print item WPA tier list(s).

    If role is None, iterates over all roles that have data.
    If purchase_rank is None, iterates over purchase slots 1–6 (skips empty ones).
    """
    roles = [role.upper()] if role else _ALL_ROLES
    ranks = [purchase_rank] if purchase_rank else list(range(1, _MAX_RANK + 1))
    explicit = bool(role and purchase_rank)  # True when user asked for something specific

    names = _get_item_names()

    # Build the valid item set used to filter purchase-rank counting in wpa.py.
    # If Data Dragon didn't load we pass None (no pre-filtering; post-filter is
    # still applied for the hardcoded starters).
    if _item_data:
        valid_ids: Optional[frozenset] = frozenset(
            iid for iid in _item_data if _is_valid_tierlist_item(iid)
        ) | _STARTER_ITEM_IDS
    else:
        valid_ids = None
        console.print(
            "[yellow]Note: Data Dragon item data unavailable — purchase rank includes "
            "all items (components, wards, etc.). Results may be noisy.[/yellow]"
        )

    any_shown = False

    for r in roles:
        for rk in ranks:
            try:
                items = _wpa.item_wpa_tierlist(r, purchase_rank=rk, valid_item_ids=valid_ids)
            except FileNotFoundError as e:
                if explicit:
                    console.print(f"[red]{e}[/red]")
                continue

            # Keep only starters and completed legendaries
            items = [it for it in items if _is_valid_tierlist_item(it["item_id"])]

            if not items:
                if explicit:
                    console.print("[yellow]No item data found. Need more games or a trained model.[/yellow]")
                continue

            any_shown = True
            t = Table(
                title=f"Item WPA — {r}  ·  Purchase #{rk}",
                box=box.SIMPLE_HEAVY,
            )
            t.add_column("Rank",    justify="right", style="dim")
            t.add_column("Item",    style="bold", min_width=24)
            t.add_column("Avg WPA", justify="right")
            t.add_column("Games",   justify="right")

            for i, item in enumerate(items[:20], 1):
                name = names.get(item["item_id"], f"Item {item['item_id']}")
                t.add_row(str(i), name, _fmt_wpa(item["avg_wpa"]), str(item["game_count"]))

            console.print(t)

    if not any_shown and not explicit:
        console.print("[yellow]No item data found. Collect games and train a model first.[/yellow]")


# ---------------------------------------------------------------------------
# Rune win rate analysis
# ---------------------------------------------------------------------------

def print_rune_analysis(role: str) -> None:
    """Print a win rate breakdown by keystone rune for a role."""
    console.print(
        f"\n[cyan]Computing rune win rates — {role.upper()}…[/cyan]"
    )

    runes = _wpa.rune_tierlist(role)

    if not runes:
        console.print("[yellow]No rune data found. Collect more games for this role.[/yellow]")
        return

    names = _get_rune_names()

    t = Table(
        title=f"Keystone Win Rates — {role.upper()}",
        box=box.SIMPLE_HEAVY,
    )
    t.add_column("Rank",     justify="right", style="dim")
    t.add_column("Keystone", style="bold", min_width=20)
    t.add_column("Win Rate", justify="right")
    t.add_column("Games",    justify="right")

    for i, rune in enumerate(runes, 1):
        name     = names.get(rune["keystone_id"], f"Rune {rune['keystone_id']}")
        wr_pct   = round(rune["win_rate"] * 100, 1)
        wr_color = "green" if wr_pct >= 50 else "red"
        t.add_row(
            str(i),
            name,
            f"[{wr_color}]{wr_pct}%[/{wr_color}]",
            str(rune["game_count"]),
        )

    console.print(t)
