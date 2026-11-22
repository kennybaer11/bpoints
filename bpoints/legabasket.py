"""Lega Basket's own JSON (www.legabasket.it/api/...) for Italy's Serie A (LBA):
every game of a season, played or scheduled, with the score by quarter.

    /api/championships/get-championships?items=1000&cs_id=1        the seasons
    /api/championships/get-championships-calendar-by-id?id=C&d=N   round N of championship C
    /api/championships/get-championships-matches-by-id?id=G        one game (the quarters)

A season is two championships, the regular season and the playoffs (2026-27's
regular season is 602). A round lists its games with the final score; the
quarters come from each game's own call. Overtime is one figure however many
periods it took, so a game that went to overtime counts as five periods.

Clubs are keyed on their three-letter code (TRN): the numeric team id is new
every season. Finished rounds and games are cached on disk for good; a round
still to be played is read again when it is close or its copy is a day old.
robots.txt asks for two seconds between requests; a refusal stops the run.
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

BASE = "https://www.legabasket.it/api/championships"
LEAGUE = "lba"
CACHE = Path(__file__).resolve().parent.parent / "cache" / "lba"
GAP = 2.0
FINAL = "2"                          # game_status: "0" scheduled, "2" final

# What Betano calls a club when that is not a part of Lega Basket's name
# (names.py compares a bookmaker's name with every name a team has).
BETANO_NAME: dict[str, str] = {}

_s = requests.Session()
_s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                 "AppleWebKit/537.36 Chrome/128 Safari/537.36",
                   "Accept": "application/json"})
_last = 0.0


class Blocked(RuntimeError):
    """Lega Basket answered with something other than its JSON."""


class ServerError(RuntimeError):
    """One call failing on Lega Basket's side (game 24208's did): skip it, not the run."""


def _get(path: str, _tries: int = 0) -> dict:
    global _last
    wait = GAP - (time.monotonic() - _last)
    if wait > 0:
        time.sleep(wait)
    _last = time.monotonic()
    for attempt in range(7):          # through this PC's DNS drops, as in espn.py
        try:
            r = _s.get(BASE + path, timeout=40)
            break
        except (requests.ConnectionError, requests.Timeout):   # a timeout is not a ConnectionError
            if attempt == 6:
                raise
            log.warning("lba %s: connection failed or timed out - retrying", path)
            time.sleep(min(5 * 2 ** attempt, 40))
    if r.status_code >= 500:
        if _tries < 2:
            time.sleep(5)
            return _get(path, _tries + 1)
        raise ServerError(f"lba {path}: HTTP {r.status_code}")
    if r.status_code in (403, 429) or "json" not in r.headers.get("content-type", ""):
        raise Blocked(f"lba {path}: HTTP {r.status_code}, {r.headers.get('content-type')}")
    r.raise_for_status()
    return r.json()


def championships(first_year: int) -> list[dict]:
    """The Serie A championships (regular season, playoffs) from first_year on."""
    data = _get("/get-championships?items=1000&cs_id=1")
    return sorted((c for c in data.get("competitions", []) if int(c.get("year") or 0) >= first_year),
                  key=lambda c: (int(c["year"]), c["id"]))


def _round(champ: int, day: int, now: datetime) -> dict:
    """One round: its games, and the list of the championship's rounds."""
    path = CACHE / "rounds" / f"{champ}_{day}.json"
    if path.exists():
        c = json.loads(path.read_text(encoding="utf-8"))
        ms = c["matches"]
        done = ms and all(m["game_status"] == FINAL for m in ms)
        start = min((datetime.fromisoformat(m["match_datetime"]) for m in ms), default=now)
        age = now - datetime.fromisoformat(c["fetched"])
        if done or (age < timedelta(days=1) and start - now > timedelta(days=7)):
            return c
    data = _get(f"/get-championships-calendar-by-id?id={champ}&d={day}")
    c = {"matches": data.get("matches") or [],
         "days": [d["event_serial"] for d in (data.get("filters") or {}).get("days", [])],
         "fetched": now.isoformat()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    return c


def _quarters(game_id: int) -> dict | None:
    """A finished game's quarters, cached for good."""
    path = CACHE / "games" / f"{game_id}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    try:
        m = _get(f"/get-championships-matches-by-id?id={game_id}").get("match") or {}
    except ServerError as e:          # the game is stored without its quarters; tried again next run
        log.warning("%s - no quarters this run", e)
        return None
    if str(m.get("game_status")) != FINAL:
        return None
    keep = [f"q{i}_{s}" for i in range(1, 5) for s in ("hs", "vs")] + [
        "ot_hs", "ot_vs", "home_final_score", "visitor_final_score"]
    c = {k: m.get(k) for k in keep}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(c), encoding="utf-8")
    return c


def _row(m: dict, year: int) -> tuple[dict, list[dict]] | None:
    if not m.get("h_club_code") or not m.get("v_club_code") or not m.get("match_datetime"):
        return None
    final = m["game_status"] == FINAL and m.get("home_final_score") is not None
    row = {"league": LEAGUE, "game_id": str(m["id"]), "season": year + 1,
           "tipoff": datetime.fromisoformat(m["match_datetime"]).astimezone(timezone.utc),
           "status": "final" if final else "scheduled", "neutral": False,
           "home_id": m["h_club_code"], "away_id": m["v_club_code"],
           "home_name": m["h_team_name"], "away_name": m["v_team_name"]}
    if final:
        row.update(home_pts=m["home_final_score"], away_pts=m["visitor_final_score"])
        q = _quarters(m["id"])
        if q and all(q.get(f"q{i}_{s}") is not None for i in range(1, 5) for s in ("hs", "vs")):
            hr = sum(q[f"q{i}_hs"] for i in range(1, 5))
            ar = sum(q[f"q{i}_vs"] for i in range(1, 5))
            ho, ao = q.get("ot_hs") or 0, q.get("ot_vs") or 0
            # The quarters must add up to the final score, or the split is not used.
            if hr + ho == row["home_pts"] and ar + ao == row["away_pts"]:
                row.update(home_reg=hr, away_reg=ar, home_h1=q["q1_hs"] + q["q2_hs"],
                           away_h1=q["q1_vs"] + q["q2_vs"], periods=5 if ho or ao else 4)
    teams = [{"league": LEAGUE, "team_id": m[f"{s}_club_code"], "name": m[f"{s}_team_name"],
              "short_name": None, "location": BETANO_NAME.get(m[f"{s}_club_code"]),
              "abbrev": m[f"{s}_club_code"]} for s in ("h", "v")]
    return row, teams


def games(first_year: int):
    """Serie A games of every season from first_year (the year it starts) on."""
    now = datetime.now(timezone.utc)
    for ch in championships(first_year):
        first = _round(ch["id"], 1, now)
        for day in first["days"] or [1]:
            rnd = first if day == 1 else _round(ch["id"], day, now)
            for m in rnd["matches"]:
                out = _row(m, int(ch["year"]))
                if out:
                    yield out


def current_season() -> int:
    """The year a season starts in: 2026 for 2026-27."""
    t = datetime.now(timezone.utc)
    return t.year if t.month >= 8 else t.year - 1
