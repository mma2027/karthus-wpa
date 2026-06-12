"""
riot_client.py — Async Riot Games API client for karthus-wpa.

Uses aiohttp with a global semaphore to respect the dev-key rate limit
(20 req/s). All methods are async coroutines; run them inside an
asyncio event loop (see collector.py).

Endpoints used
--------------
- /riot/account/v1/accounts/by-riot-id/{gameName}/{tagLine}
- /lol/summoner/v4/summoners/by-puuid/{puuid}
- /lol/league/v4/entries/by-summoner/{summonerId}
- /lol/league/v4/challengerleagues/by-queue/{queue}
- /lol/league/v4/grandmasterleagues/by-queue/{queue}
- /lol/league/v4/masterleagues/by-queue/{queue}
- /lol/match/v5/matches/by-puuid/{puuid}/ids
- /lol/match/v5/matches/{matchId}
- /lol/match/v5/matches/{matchId}/timeline
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

import aiohttp
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLATFORM_TO_ROUTING: dict[str, str] = {
    "na1":  "americas", "br1":  "americas", "la1":  "americas", "la2":  "americas",
    "euw1": "europe",   "eun1": "europe",   "tr1":  "europe",   "ru":   "europe",
    "kr":   "asia",     "jp1":  "asia",
    "oc1":  "sea",      "sg2":  "sea",      "ph2":  "sea",      "tw2":  "sea",
    "vn2":  "sea",      "th2":  "sea",
}

RANKED_SOLO_QUEUE = "RANKED_SOLO_5x5"
KARTHUS_ID = 30

# Semaphore keeps concurrent in-flight requests to 18, staying under the
# 20 req/s dev key limit even with network latency variation.
_SEMAPHORE: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _SEMAPHORE
    if _SEMAPHORE is None:
        _SEMAPHORE = asyncio.Semaphore(18)
    return _SEMAPHORE


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class RiotAPIError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"[{status}] {message}")


class RateLimitError(RiotAPIError):
    pass


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class RiotClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        platform: str = "na1",
        session: Optional[aiohttp.ClientSession] = None,
    ):
        self.api_key  = api_key or os.getenv("RIOT_API_KEY", "")
        self.platform = platform.lower()
        self.routing  = PLATFORM_TO_ROUTING.get(self.platform, "americas")
        self._session = session  # external session; None = create per-request

    # ------------------------------------------------------------------
    # Internal HTTP
    # ------------------------------------------------------------------

    async def _get(self, url: str, params: Optional[dict] = None) -> dict | list:
        sem = _get_semaphore()
        headers = {"X-Riot-Token": self.api_key}

        async with sem:
            session = self._session
            owns_session = session is None
            if owns_session:
                session = aiohttp.ClientSession()
            try:
                async with session.get(url, headers=headers, params=params or {}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 429:
                        retry_after = int(resp.headers.get("Retry-After", "5"))
                        await asyncio.sleep(retry_after + 1.0)
                        # Release semaphore before recursive retry
                    elif resp.status == 404:
                        raise RiotAPIError(404, f"Not found: {url}")
                    elif resp.status == 403:
                        raise RiotAPIError(403, "Forbidden — check your API key")
                    elif resp.status == 401:
                        raise RiotAPIError(401, "Unauthorized — API key missing or invalid")
                    else:
                        body = await resp.text()
                        raise RiotAPIError(resp.status, f"{url} → {body[:200]}")
            finally:
                if owns_session:
                    await session.close()

        # Retry once after rate-limit sleep (outside semaphore to avoid deadlock)
        return await self._get(url, params)

    # ------------------------------------------------------------------
    # Account API  (regional routing)
    # ------------------------------------------------------------------

    async def get_account_by_riot_id(self, game_name: str, tag_line: str) -> dict:
        """Returns {puuid, gameName, tagLine}."""
        url = (
            f"https://{self.routing}.api.riotgames.com"
            f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
        )
        return await self._get(url)

    # ------------------------------------------------------------------
    # Summoner API  (platform routing)
    # ------------------------------------------------------------------

    async def get_summoner_by_puuid(self, puuid: str) -> dict:
        """Returns {id, accountId, puuid, profileIconId, revisionDate, summonerLevel}."""
        url = (
            f"https://{self.platform}.api.riotgames.com"
            f"/lol/summoner/v4/summoners/by-puuid/{puuid}"
        )
        return await self._get(url)

    async def get_summoner_by_id(self, summoner_id: str) -> dict:
        """Returns {id, accountId, puuid, profileIconId, revisionDate, summonerLevel}."""
        url = (
            f"https://{self.platform}.api.riotgames.com"
            f"/lol/summoner/v4/summoners/{summoner_id}"
        )
        return await self._get(url)

    # ------------------------------------------------------------------
    # League API  (platform routing)
    # ------------------------------------------------------------------

    async def get_rank(self, summoner_id: str) -> Optional[dict]:
        """
        Returns the RANKED_SOLO_5x5 entry for this summoner, or None if unranked.
        Fields: {tier, rank, leaguePoints, wins, losses, ...}
        """
        url = (
            f"https://{self.platform}.api.riotgames.com"
            f"/lol/league/v4/entries/by-summoner/{summoner_id}"
        )
        try:
            entries: list = await self._get(url)
            for entry in entries:
                if entry.get("queueType") == RANKED_SOLO_QUEUE:
                    return entry
            return None
        except RiotAPIError:
            return None

    async def get_ladder(self, tier: str = "challenger") -> list[dict]:
        """
        Returns the full league entry list for challenger / grandmaster / master.
        Each entry has {summonerId, summonerName, leaguePoints, wins, losses, ...}.
        tier must be one of: 'challenger', 'grandmaster', 'master'
        """
        tier = tier.lower()
        if tier == "challenger":
            path = "challengerleagues"
        elif tier == "grandmaster":
            path = "grandmasterleagues"
        else:
            path = "masterleagues"

        url = (
            f"https://{self.platform}.api.riotgames.com"
            f"/lol/league/v4/{path}/by-queue/{RANKED_SOLO_QUEUE}"
        )
        data: dict = await self._get(url)
        return data.get("entries", [])

    # ------------------------------------------------------------------
    # Match API  (regional routing)
    # ------------------------------------------------------------------

    async def get_match_ids(
        self,
        puuid: str,
        champion: Optional[int] = None,
        count: int = 100,
        queue: int = 420,
    ) -> list[str]:
        """
        Fetch up to `count` ranked solo/duo match IDs for `puuid`.
        Pass champion=30 to filter for Karthus games only.
        Paginates automatically until `count` is satisfied or no more pages.
        """
        url = (
            f"https://{self.routing}.api.riotgames.com"
            f"/lol/match/v5/matches/by-puuid/{puuid}/ids"
        )
        all_ids: list[str] = []
        page_size = min(count, 100)
        start = 0

        while len(all_ids) < count:
            params: dict = {"queue": queue, "start": start, "count": page_size}
            if champion is not None:
                params["champion"] = champion
            try:
                batch: list = await self._get(url, params)
            except RiotAPIError:
                break
            all_ids.extend(batch)
            if len(batch) < page_size:
                break  # no more pages
            start += page_size

        return all_ids[:count]

    async def get_match(self, match_id: str) -> dict:
        """Full match summary (end-of-game stats for all 10 participants)."""
        url = (
            f"https://{self.routing}.api.riotgames.com"
            f"/lol/match/v5/matches/{match_id}"
        )
        return await self._get(url)

    async def get_match_timeline(self, match_id: str) -> dict:
        """Per-minute frame data + events for all 10 participants."""
        url = (
            f"https://{self.routing}.api.riotgames.com"
            f"/lol/match/v5/matches/{match_id}/timeline"
        )
        return await self._get(url)


# ---------------------------------------------------------------------------
# Convenience: build a shared session for bulk collection
# ---------------------------------------------------------------------------

def make_session() -> aiohttp.ClientSession:
    """Create a persistent aiohttp session for use across many requests."""
    connector = aiohttp.TCPConnector(limit=32, ttl_dns_cache=300)
    return aiohttp.ClientSession(connector=connector)
