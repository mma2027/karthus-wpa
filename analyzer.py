"""
analyzer.py — Rich terminal output for WPA analysis.

Entry points:
    print_player_analysis(name_tag, role, n_games)
    print_item_tierlist(role, purchase_rank)
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

# ── Item name cache (fetched from Data Dragon on first use) ──────────────────

_item_names: dict[int, str] = {}
_item_names_loaded           = False


def _get_item_names() -> dict[int, str]:
    global _item_names, _item_names_loaded
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
        _item_names = {int(k): v["name"] for k, v in data["data"].items()}
    except Exception:
        pass
    return _item_names


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

def print_item_tierlist(
    role: str,
    purchase_rank: int = 1,
) -> None:
    """Print an item WPA tier list for a role."""
    console.print(
        f"\n[cyan]Computing item WPA — {role.upper()} (purchase #{purchase_rank})…[/cyan]"
    )

    try:
        items = _wpa.item_wpa_tierlist(role, purchase_rank=purchase_rank)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        return

    if not items:
        console.print("[yellow]No item data found. Need more games or a trained model.[/yellow]")
        return

    names = _get_item_names()

    t = Table(
        title=f"Item WPA — {role.upper()}  ·  Purchase #{purchase_rank}",
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
