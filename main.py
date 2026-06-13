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
from analyzer import print_player_analysis, print_item_tierlist, print_rune_analysis

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
            patch_window=args.patch_window,
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


def cmd_train(args: argparse.Namespace) -> None:
    """Train a win probability model for a role."""
    import model as mdl
    from collector import _fetch_valid_patches
    import aiohttp, asyncio

    role = (args.role or "").upper()
    if not role:
        console.print("[red]--role is required. Example: python main.py train --role MID[/red]")
        return

    patches: set | None = None
    if args.patch_window:
        async def _get_patches():
            async with aiohttp.ClientSession() as s:
                return await _fetch_valid_patches(s, args.patch_window)
        patches = asyncio.run(_get_patches())
        if patches:
            console.print(f"[dim]Patch window ({args.patch_window}): {', '.join(sorted(patches, key=db.patch_sort_key, reverse=True))}[/dim]")

    mdl.train_model(role, patches=patches)


def cmd_analyze(args: argparse.Namespace) -> None:
    """Print WPA breakdown for a player."""
    name  = args.name
    role  = (args.role or "").upper() or None
    games = args.games or 20
    print_player_analysis(name, role=role, n_games=games)


def cmd_tierlist(args: argparse.Namespace) -> None:
    """Print item WPA tier list(s)."""
    role = (args.role or "").upper() or None
    rank = args.purchase_rank or None
    print_item_tierlist(role, purchase_rank=rank)


def cmd_rune(args: argparse.Namespace) -> None:
    """Print keystone rune win rates for a role."""
    role = (args.role or "").upper()
    if not role:
        console.print("[red]--role is required. Example: python main.py rune --role MID[/red]")
        return
    print_rune_analysis(role)


def cmd_reset(_args: argparse.Namespace) -> None:
    """Wipe the database — deletes all collected data and recreates empty schema."""
    console.print(
        "[bold red]WARNING:[/bold red] This will permanently delete all collected games, "
        "players, timelines, and events."
    )
    confirm = input("Type 'yes' to confirm: ").strip().lower()
    if confirm != "yes":
        console.print("[yellow]Aborted.[/yellow]")
        return

    import db as _db
    if _db.DB_PATH.exists():
        _db.DB_PATH.unlink()
    _db.init_db()
    console.print("[bold green]Database wiped and schema recreated.[/bold green]")


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
    p_collect.add_argument("--seed",         metavar="NAME#TAG", help="Seed player Riot ID (e.g. SqfeWalk#NA1)")
    p_collect.add_argument("--max-players",  metavar="N", type=int, help="Stop after N players (for testing)")
    p_collect.add_argument("--patch-window", metavar="N", type=int, default=5, help="Only store games from the N most recent patches (default: 5)")
    p_collect.add_argument("--verbose", "-v", action="store_true", help="Print each player and match being fetched")

    # stats
    sub.add_parser("stats", help="Show database collection statistics")

    # players
    p_players = sub.add_parser("players", help="List players in the database")
    p_players.add_argument("--tier", metavar="TIER", help="Filter by tier (e.g. DIAMOND, CHALLENGER)")

    # games
    p_games = sub.add_parser("games", help="Show stored Karthus games for a player")
    p_games.add_argument("name", metavar="NAME#TAG", help="Player Riot ID")

    # train
    p_train = sub.add_parser("train", help="Train a win probability model for a role")
    p_train.add_argument("--role",         metavar="ROLE",  required=True, help="Role to train (MID, JUNGLE, BOTTOM, SUPPORT, TOP)")
    p_train.add_argument("--patch-window", metavar="N",     type=int,      help="Restrict training to N most recent patches")

    # analyze
    p_analyze = sub.add_parser("analyze", help="Show WPA breakdown for a player")
    p_analyze.add_argument("name",          metavar="NAME#TAG",  help="Player Riot ID")
    p_analyze.add_argument("--role",        metavar="ROLE",      help="Filter to a specific role")
    p_analyze.add_argument("--games",       metavar="N",         type=int, default=20, help="Number of recent games to analyze (default: 20)")

    # tierlist
    p_tier = sub.add_parser("tierlist", help="Show item WPA tier list (omit flags to show all roles and ranks)")
    p_tier.add_argument("--role",          metavar="ROLE", help="Filter to one role (MID, JUNGLE, BOTTOM, SUPPORT, TOP); default: all roles")
    p_tier.add_argument("--purchase-rank", metavar="N",    type=int, help="Filter to one purchase slot (1=first item, 2=second, …); default: all")

    # rune
    p_rune = sub.add_parser("rune", help="Show keystone rune win rates for a role")
    p_rune.add_argument("--role", metavar="ROLE", required=True, help="Role (MID, JUNGLE, BOTTOM, SUPPORT, TOP)")

    # reset
    sub.add_parser("reset", help="Wipe the database and start fresh (asks for confirmation)")

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
    elif args.command == "train":
        cmd_train(args)
    elif args.command == "analyze":
        cmd_analyze(args)
    elif args.command == "tierlist":
        cmd_tierlist(args)
    elif args.command == "rune":
        cmd_rune(args)
    elif args.command == "reset":
        cmd_reset(args)
    else:
        # No subcommand: show help + quick stats
        console.print(Panel(
            "[bold cyan]Karthus WPA[/bold cyan]\n\n"
            "  [green]collect[/green]                    Collect games from Riot API\n"
            "  [green]collect --seed NAME#TAG[/green]    BFS from one player\n"
            "  [green]stats[/green]                      Database overview\n"
            "  [green]players[/green]                    List collected players\n"
            "  [green]games NAME#TAG[/green]              Show games for a player\n"
            "  [green]train --role ROLE[/green]           Train win probability model\n"
            "  [green]analyze NAME#TAG[/green]            WPA breakdown for a player\n"
            "  [green]tierlist[/green]                    Item WPA tier list (all roles & ranks)\n"
            "  [green]tierlist --role ROLE[/green]        Item WPA tier list for one role\n"
            "  [green]rune --role ROLE[/green]            Keystone rune win rates\n"
            "  [green]reset[/green]                      Wipe database and start fresh\n\n"
            "Run [bold]python main.py --help[/bold] for full usage.",
            title="[bold]Karthus WPA[/bold]",
            border_style="cyan",
            expand=False,
        ))
        console.print()
        cmd_stats(args)


if __name__ == "__main__":
    main()
