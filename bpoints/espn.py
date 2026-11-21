"""ESPN's public JSON (site.api.espn.com) for NBA, WNBA and college games.

    /apis/site/v2/sports/basketball/{league}/scoreboard?dates=YYYYMMDD   a day's games
    /apis/site/v2/sports/basketball/{league}/summary?event=ID            one game

The summary has the score by period, the box score (enough for possessions)
and the "pick centre": DraftKings' line as it stood when the game started -
the closing total that the model is tested against.

Finished games are cached on disk for good; a day's scoreboard is cached once
the day is more than two days old.
"""

import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball"
LEAGUES = {"nba": "nba", "wnba": "wnba", "ncaam": "mens-college-basketball",
           "ncaaw": "womens-college-basketball"}
CACHE = Path(__file__).resolve().parent.parent / "cache"

_s = requests.Session()
_s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                 "AppleWebKit/537.36 Chrome/128 Safari/537.36"})


def _get(url: str, keep: bool) -> dict:
    path = CACHE / "espn" / (hashlib.sha1(url.encode()).hexdigest() + ".json")
    if keep and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    for attempt in range(4):
        try:
            r = _s.get(url, timeout=30)
            if r.status_code == 404:
                return {}
            r.raise_for_status()
            body = r.json()
            break
        except (requests.RequestException, ValueError) as e:
            if attempt == 3:
                raise
            log.warning("%s: %s - retrying", url, e)
            time.sleep(2 + 4 * attempt)
    if keep:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body), encoding="utf-8")
    return body


def scoreboard(league: str, day: date) -> list[dict]:
    """The day's games as ESPN lists them (ids, status, teams)."""
    url = f"{BASE}/{LEAGUES[league]}/scoreboard?dates={day:%Y%m%d}&limit=500"
    if league.startswith("ncaa"):
        url += "&groups=50"                       # every Division I game, not just the top 25
    old = day < date.today() - timedelta(days=2)
    return _get(url, keep=old).get("events", [])


def summary(league: str, event_id: str, final: bool) -> dict:
    return _get(f"{BASE}/{LEAGUES[league]}/summary?event={event_id}", keep=final)


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _stat(team_box: dict, name: str):
    for s in team_box.get("statistics", []):
        if s.get("name") == name:
            return s.get("displayValue")
    return None


def _possessions(team_box: dict, opp_box: dict):
    """FGA - ORB + TO + 0.44 FTA - the usual estimate."""
    try:
        fga = int(_stat(team_box, "fieldGoalsMade-fieldGoalsAttempted").split("-")[1])
        fta = int(_stat(team_box, "freeThrowsMade-freeThrowsAttempted").split("-")[1])
        orb = int(_stat(team_box, "offensiveRebounds"))
        tov = int(_stat(team_box, "totalTurnovers") or _stat(team_box, "turnovers"))
    except (AttributeError, IndexError, TypeError, ValueError):
        return None
    return round(fga - orb + tov + 0.44 * fta, 1)


def parse_event(league: str, ev: dict, summ: dict | None) -> tuple[dict, list[dict]] | None:
    """One game row and its two teams' names, from the scoreboard entry and,
    for a finished game, its summary."""
    comp = (ev.get("competitions") or [{}])[0]
    sides = {c.get("homeAway"): c for c in comp.get("competitors", [])}
    if set(sides) != {"home", "away"}:
        return None
    state = comp.get("status", {}).get("type", {})
    status = ("final" if state.get("completed") else
              "scheduled" if state.get("state") == "pre" else "other")
    row = {"league": league, "game_id": str(ev["id"]),
           "season": (ev.get("season") or {}).get("year"),
           "tipoff": datetime.fromisoformat(ev["date"].replace("Z", "+00:00")),
           "status": status, "neutral": bool(comp.get("neutralSite"))}
    teams = []
    for side in ("home", "away"):
        c = sides[side]
        t = c.get("team", {})
        row[f"{side}_id"] = str(t.get("id"))
        row[f"{side}_name"] = t.get("displayName") or t.get("name") or "?"
        teams.append({"league": league, "team_id": str(t.get("id")), "name": row[f"{side}_name"],
                      "short_name": t.get("shortDisplayName"), "location": t.get("location"),
                      "abbrev": t.get("abbreviation")})
        if status == "final":
            row[f"{side}_pts"] = _int(c.get("score"))
            per = [_int(x.get("value", x.get("displayValue"))) for x in c.get("linescores", [])]
            per = [p for p in per if p is not None]
            reg = 2 if league == "ncaam" else 4
            if len(per) >= reg:
                row[f"{side}_reg"] = sum(per[:reg])
                row[f"{side}_h1"] = sum(per[:reg // 2])
                row["periods"] = len(per)
    # A game still to play: the scoreboard carries DraftKings' current line,
    # which becomes the closing one (the summary's) once it is played.
    for o in comp.get("odds") or []:
        if o.get("overUnder") is not None:
            row["close_total"], row["close_spread"] = o.get("overUnder"), o.get("spread")
            break
    if summ:
        box ={str(t.get("team", {}).get("id")): t for t in summ.get("boxscore", {}).get("teams", [])}
        h, a = box.get(row["home_id"]), box.get(row["away_id"])
        if h and a:
            row["home_poss"], row["away_poss"] = _possessions(h, a), _possessions(a, h)
        for pc in summ.get("pickcenter") or []:
            if pc.get("overUnder") is not None:
                row["close_total"] = pc.get("overUnder")
                row["close_spread"] = pc.get("spread")
                break
    return row, teams


def games(league: str, start: date, end: date, workers: int = 6):
    """Every game between two dates (inclusive), with summaries for the
    finished ones fetched a few at a time."""
    day = start
    while day <= end:
        evs = scoreboard(league, day)
        finals = [e for e in evs
                  if (e.get("competitions") or [{}])[0].get("status", {}).get("type", {}).get("completed")]
        with ThreadPoolExecutor(workers) as pool:
            summs = dict(zip([e["id"] for e in finals],
                             pool.map(lambda e: summary(league, e["id"], True), finals)))
        for e in evs:
            out = parse_event(league, e, summs.get(e["id"]))
            if out:
                yield out
        day += timedelta(days=1)


CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues"


def closing_line(league: str, game_id: str) -> tuple[float, float] | None:
    """(total, home spread) from ESPN's odds feed, for games whose summary has
    no pick centre (seasons before 2025-26). The first pre-game provider with a
    total; ESPN keeps the line as it stood at tip-off."""
    d = _get(f"{CORE}/{LEAGUES[league]}/events/{game_id}/competitions/{game_id}/odds?limit=50",
             keep=True)
    for it in d.get("items", []):
        name = (it.get("provider") or {}).get("name", "")
        if "live" in name.lower() or it.get("overUnder") is None:
            continue
        return it["overUnder"], it.get("spread")
    return None


def now_utc():
    return datetime.now(timezone.utc)
