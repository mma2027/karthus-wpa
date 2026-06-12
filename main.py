"""
main.py — Karthus WPA CLI entry point.

Usage
-----
python main.py                                     # interactive menu
python main.py collect                             # ladder-seeded collection
python main.py collect --seed "SqfeWalk#NA1"       # BFS from one player
python main.py collect --seed "SqfeWalk#NA1" --max-players 50
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table, box

import db
from collector import run_collection

load_dotenv()
console = Console()


# ---------------------------------------------------------------------------
# CLI sub-commands
# ---------------------------------------------------------------------------

def cmd_collect(args: argparse.Namespace) -> None:
    """Run the data collection pipeline."""
    api_key  = os.getenv("RIOT_API_KEY", "")
    platform = os.getenv("RIOT_PLATFORM", "na1")

    if not api_key or api_key.startswith("RGAPI-your"):
        console.print("[red]RIOT_API_KEY not set in .env — cannot collect.[/red]")
        sys.exit(1)

    seed        = args.seed or None
    max_players = args.max_players or None

    if seed:
        console.print(
            Panel(
                f"[bold]Seed mode[/bold]\n"
                f"Starting from: [cyan]{seed}[/cyan]\n"
                f"Max players:   [cyan]{max_players or 'unlimited'}[/cyan]",
                title="[bold green]Karthus WPA — Collector[/bold green]",
                border_style="green",
            )
        )
    else:
        console.print(
            Panel(
                "[bold]Ladder mode[/bold]\n"
                f"Platform: [cyan]{platform.upper()}[/cyan]  (master/GM/challenger seed)",
                title="[bold green]Karthus WPA — Collector[/bold green]",
                border_style="green",
            )
        )

    asyncio.run(
        run_collection(
            seed=seed,
            max_players=max_players,
            api_key=api_key,
            platform=platform,
            verbose=args.verbose,
        )
    )


def cmd_stats(_args: argparse.Namespace) -> None:
    """Print database collection statistics."""
    stats = db.get_collection_stats()

    console.print()
    console.print(Panel(
        f"[bold]Total games:[/bold]   {stats['total_games']}\n"
        f"[bold]Total players:[/bold] {stats['total_players']}\n"
        f"[bold]Scanned:[/bold]       {stats['scanned']}\n"
        f"[bold]Queued:[/bold]        {stats['queued']}",
        title="[bold cyan]Database Stats[/bold cyan]",
        border_style="cyan",
        expand=False,
    ))

    if stats["games_by_role"]:
        t = Table(title="Games by Role", box=box.SIMPLE_HEAVY, show_header=True)
        t.add_column("Role",  style="bold")
        t.add_column("Games", justify="right")
        for role, cnt in sorted(stats["games_by_role"].items()):
            t.add_row(role, str(cnt))
        console.print(t)

    if stats["games_by_patch"]:
        t = Table(title="Games by Patch (recent first)", box=box.SIMPLE_HEAVY, show_header=True)
        t.add_column("Patch", style="bold")
        t.add_column("Games", justify="right")
        for patch, cnt in list(stats["games_by_patch"].items())[:10]:
            t.add_row(patch, str(cnt))
        console.print(t)


def cmd_players(args: argparse.Namespace) -> None:
    """Show players in the database, optionally filtered by tier."""
    tier = (args.tier or "").upper() or None

    if tier:
        players = db.get_players_by_tier(tier)
        title   = f"Players — {tier}"
    else:
        with db.get_connection() as conn:
            players = conn.execute(
                "SELECT * FROM players ORDER BY tier, lp DESC LIMIT 100"
            ).fetchall()
        title = "Players (top 100)"

    if not players:
        console.print("[yellow]No players found.[/yellow]")
        return

    t = Table(title=title, box=box.SIMPLE_HEAVY, show_header=True)
    t.add_column("Name",     style="bold")
    t.add_column("Tag")
    t.add_column("Tier")
    t.add_column("Div")
    t.add_column("LP",  justify="right")
    t.add_column("W",   justify="right")
    t.add_column("L",   justify="right")

    for p in players:
        wr = ""
        if p["wins"] and p["losses"]:
            total = p["wins"] + p["losses"]
            wr    = f"  ({round(p['wins']/total*100)}%)" if total else ""
        t.add_row(
            p["game_name"],
            p["tag_line"],
            p["tier"] or "—",
            p["division"] or "—",
            str(p["lp"] or "—"),
            str(p["wins"] or "—"),
            str(p["losses"] or "—"),
        )

    console.print(t)


def cmd_games(args: argparse.Namespace) -> None:
    """Show stored Karthus games for a player."""
    name = args.name
    if "#" in name:
        game_name, tag_line = name.rsplit("#", 1)
    else:
        game_name, tag_line = name, ""

    player = db.find_player_by_name(game_name, tag_line)
    if not player:
        # Try partial match
        with db.get_connection() as conn:
            player = conn.execute(
                "SELECT * FROM players WHERE LOWER(game_name) = LOWER(?)",
                (game_name,),
            ).fetchone()

    if not player:
        console.print(f"[red]Player '{name}' not found in database.[/red]")
        return

    matches = db.get_matches_for_player(player["puuid"])
    if not matches:
        console.print(f"[yellow]No Karthus games stored for {player['game_name']}#{player['tag_line']}.[/yellow]")
        return

    t = Table(
        title=f"Karthus games — {player['game_name']}#{player['tag_line']}",
        box=box.SIMPLE_HEAVY,
    )
    t.add_column("Match ID",    style="dim", no_wrap=True)
    t.add_column("Patch")
    t.add_column("Role")
    t.add_column("Result",      justify="center")
    t.add_column("Duration",    justify="right")

    for m in matches:
        mins = (m["game_duration_s"] or 0) // 60
        secs = (m["game_duration_s"] or 0) % 60
        result = "[green]WIN[/green]" if m["win"] else "[red]LOSS[/red]"
        t.add_row(
            m["match_id"],
            m["patch"] or "—",
            m["karthus_role"] or "—",
            result,
            f"{mins}:{secs:02d}",
        )

    console.print(t)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="karthus-wpa",
        description="Karthus Win Probability Added — data collection & analysis",
    )
    sub = parser.add_subparsers(dest="command")

    # collect
    p_collect = sub.add_parser("collect", help="Collect Karthus games from the Riot API")
    p_collect.add_argument("--seed",        metavar="NAME#TAG", help="Seed player Riot ID (e.g. SqfeWalk#NA1)")
    p_collect.add_argument("--max-players", metavar="N", type=int, help="Stop after N players (for testing)")
    p_collect.add_argument("--verbose", "-v", action="store_true", help="Print each player and match being fetched")

    # stats
    sub.add_parser("stats", help="Show database collection statistics")

    # players
    p_players = sub.add_parser("players", help="List players in the database")
    p_players.add_argument("--tier", metavar="TIER", help="Filter by tier (e.g. DIAMOND, CHALLENGER)")

    # games
    p_games = sub.add_parser("games", help="Show stored Karthus games for a player")
    p_games.add_argument("name", metavar="NAME#TAG", help="Player Riot ID")

    return parser


def main() -> None:
    db.init_db()

    parser = build_parser()
    args   = parser.parse_args()

    if args.command == "collect":
        cmd_collect(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "players":
        cmd_players(args)
    elif args.command == "games":
        cmd_games(args)
    else:
        # No subcommand: show help + quick stats
        console.print(Panel(
            "[bold cyan]Karthus WPA[/bold cyan]\n\n"
            "  [green]collect[/green]              Collect games from Riot API\n"
            "  [green]collect --seed NAME#TAG[/green]  BFS from one player\n"
            "  [green]stats[/green]                Database overview\n"
            "  [green]players[/green]              List collected players\n"
            "  [green]games NAME#TAG[/green]        Show games for a player\n\n"
            "Run [bold]python main.py --help[/bold] for full usage.",
            title="[bold]Karthus WPA[/bold]",
            border_style="cyan",
            expand=False,
        ))
        console.print()
        cmd_stats(args)


if __name__ == "__main__":
    main()
