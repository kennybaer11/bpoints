"""ACB's own match pages (live.acb.com) for the Liga Endesa: every game with the
score by quarter, overtime as a fifth entry.

    live.acb.com/partidos/x-vs-y-{id}/estadisticas    (the slug is ignored)

The page embeds the match header (start, status, scores, quarterScores and the
two clubs). There is no listing endpoint we can use, but a season's league
games are a contiguous block of ids (2026-27: 105375-105680, 306 games). The
id space is shared with other competitions (Copa, Supercopa...) and is not in
date order, so a game is kept only when its competitionId is 1.

A finished game is cached on disk for good; an unfinished one is read again
when it is close or its copy is a day old. Read gently, one page every half
second; a refusal stops the run.
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

URL = "https://live.acb.com/partidos/x-vs-y-{}/estadisticas"
LEAGUE = "acb"
COMPETITION = 1                      # Liga Endesa
CACHE = Path(__file__).resolve().parent.parent / "cache" / "acb"
GAP = 0.5
# The first id of the current season's block, and how far past the newest known
# id to look for playoff games (they follow the 306 league games).
CURRENT_FIRST = 105375
LOOKAHEAD = 25

# What Betano calls a club when that is not a part of ACB's name (names.py
# compares a bookmaker's name with every name a team has; this is "location").
BETANO_NAME = {"8": "Joventut Badalona", "2": "Barcelona", "591": "Basquet Girona",
               "657": "Basquet Coruna", "658": "Forca Lleida", "13": "Valencie",
               "549": "San Pablo Burgos", "3": "Baskonia", "16": "Zaragoza", "10": "Manresa"}

_s = requests.Session()
_s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                 "AppleWebKit/537.36 Chrome/128 Safari/537.36"})
_last = 0.0


class Blocked(RuntimeError):
    """ACB answered with something other than a page."""


def _page(match_id: int, _tries: int = 0) -> str | None:
    global _last
    wait = GAP - (time.monotonic() - _last)
    if wait > 0:
        time.sleep(wait)
    _last = time.monotonic()
    for attempt in range(7):          # through this PC's DNS drops, as in espn.py
        try:
            r = _s.get(URL.format(match_id), timeout=40)
            break
        except requests.ConnectionError:
            if attempt == 6:
                raise
            log.warning("acb %d: connection failed - retrying", match_id)
            time.sleep(min(5 * 2 ** attempt, 40))
    if r.status_code in (403, 429, 503):
        raise Blocked(f"acb {match_id}: HTTP {r.status_code}")
    if r.status_code == 404:
        return None
    if r.status_code >= 500:          # one id's page failing (id 105160 did): skip it, not the run
        if _tries < 2:
            time.sleep(5)
            return _page(match_id, _tries + 1)
        log.warning("acb %d: HTTP %d - skipped", match_id, r.status_code)
        return None
    r.raise_for_status()
    return r.text.replace('\\"', '"')


def _header(match_id: int) -> dict | None:
    """The match header, or None when there is no such match."""
    text = _page(match_id)
    k = -1 if text is None else text.find('"initialMatchHeader":')
    if k < 0:
        return None
    try:
        return json.JSONDecoder().raw_decode(text[k + len('"initialMatchHeader":'):])[0]
    except ValueError:
        return None


def _slim(h: dict) -> dict:
    """What we keep of a header (the rest is broadcasters and artwork)."""
    teams = h.get("teams") or {}
    keep = ("id", "clubId", "fullName", "shortName", "abbreviatedName")
    return {"matchId": h["matchId"], "competitionId": h.get("competitionId"),
            "start": h.get("start"), "status": h.get("status"),
            "homeScore": h.get("currentHomeScore"), "awayScore": h.get("currentAwayScore"),
            "quarterScores": h.get("quarterScores") or [],
            "teams": {side: {k: t.get(k) for k in keep} for side, t in teams.items()},
            "fetched": datetime.now(timezone.utc).isoformat()}


def _fresh(c: dict, now: datetime) -> bool:
    """Is the cached copy good enough?"""
    if c.get("other") or c.get("status") == "FINALIZED":
        return True
    age = now - datetime.fromisoformat(c["fetched"])
    start = datetime.fromisoformat(c["start"].replace("Z", "+00:00")) if c.get("start") else now
    return age < timedelta(days=1) and start - now > timedelta(days=7)


def match(match_id: int, now: datetime | None = None) -> dict | None:
    """A slimmed header, cached; None for an id with no match. Matches of
    other competitions are cached as such and come back as {"other": True}."""
    now = now or datetime.now(timezone.utc)
    path = CACHE / f"{match_id}.json"
    if path.exists():
        c = json.loads(path.read_text(encoding="utf-8"))
        if _fresh(c, now):
            return c
    h = _header(match_id)
    if h is None:
        return None
    c = _slim(h) if h.get("competitionId") == COMPETITION else {"matchId": match_id, "other": True}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    return c


def season_of(start: datetime) -> int:
    """The year the season ends in: autumn starts one."""
    return start.year + 1 if start.month >= 8 else start.year


def _row(c: dict) -> tuple[dict, list[dict]] | None:
    t = c.get("teams") or {}
    home, away = t.get("home") or {}, t.get("away") or {}
    if not c.get("start") or not home.get("clubId") or not away.get("clubId"):
        return None
    start = datetime.fromisoformat(c["start"].replace("Z", "+00:00"))
    final = c["status"] == "FINALIZED" and c.get("homeScore") is not None
    row = {"league": LEAGUE, "game_id": str(c["matchId"]), "season": season_of(start),
           "tipoff": start, "status": "final" if final else "scheduled", "neutral": False,
           "home_id": str(home["clubId"]), "away_id": str(away["clubId"]),
           "home_name": home["fullName"], "away_name": away["fullName"]}
    if final:
        row.update(home_pts=c["homeScore"], away_pts=c["awayScore"])
        q = sorted(c["quarterScores"], key=lambda x: x["quarter"])
        q = [x for x in q if x["quarter"] >= 1]
        # The quarters must add up to the final score, or the split is not used.
        if (len(q) >= 4 and sum(x["home"] for x in q) == c["homeScore"]
                and sum(x["away"] for x in q) == c["awayScore"]):
            row.update(home_reg=sum(x["home"] for x in q[:4]), away_reg=sum(x["away"] for x in q[:4]),
                       home_h1=sum(x["home"] for x in q[:2]), away_h1=sum(x["away"] for x in q[:2]),
                       periods=len(q))
    teams = [{"league": LEAGUE, "team_id": str(s["clubId"]), "name": s["fullName"],
              "short_name": s.get("shortName"),
              "location": BETANO_NAME.get(str(s["clubId"])), "abbrev": s.get("abbreviatedName")}
             for s in (home, away)]
    return row, teams


def known_ids() -> list[int]:
    return sorted(int(p.stem) for p in CACHE.glob("*.json")) if CACHE.exists() else []


def games(first: int, last: int):
    """Liga Endesa games for ids first..last (inclusive), one page each unless cached."""
    now = datetime.now(timezone.utc)
    for i in range(first, last + 1):
        c = match(i, now)
        if not c or c.get("other"):
            continue
        out = _row(c)
        if out:
            yield out


def current_block() -> tuple[int, int]:
    """The ids to read for the running season: from its first id to a little
    past the newest Liga game known (the playoffs get ids after the league games)."""
    mine = [i for i in known_ids() if i >= CURRENT_FIRST]
    top = max(mine, default=CURRENT_FIRST)
    # Walk the lookahead from the newest id that really is a Liga game.
    liga = [i for i in mine if not json.loads((CACHE / f"{i}.json").read_text(encoding="utf-8")).get("other")]
    return CURRENT_FIRST, max(liga, default=CURRENT_FIRST) + LOOKAHEAD if liga else top + 320
