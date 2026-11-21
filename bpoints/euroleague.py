"""Euroleague's own API (api-live.euroleague.net) for the EuroLeague (E) and
the EuroCup (U): every game of a season, played or scheduled, with the score
by quarter.

    /v2/competitions/{E|U}/seasons/{E2026}/games

A finished season is cached on disk; the current one is read again each run.
"""

import json
import logging
from datetime import date, datetime
from pathlib import Path

import requests

log = logging.getLogger(__name__)

BASE = "https://api-live.euroleague.net/v2/competitions"
LEAGUES = {"euroleague": "E", "eurocup": "U"}
CACHE = Path(__file__).resolve().parent.parent / "cache" / "euroleague"

_s = requests.Session()
_s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                 "AppleWebKit/537.36 Chrome/128 Safari/537.36",
                   "Accept": "application/json"})


def current_season() -> int:
    """The year a season starts in: 2026 for 2026-27 (seasons start in autumn)."""
    t = date.today()
    return t.year if t.month >= 8 else t.year - 1


def _season_games(code: str, year: int) -> list[dict]:
    path = CACHE / f"{code}{year}.json"
    if year < current_season() and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    r = _s.get(f"{BASE}/{code}/seasons/{code}{year}/games", timeout=60)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    data = r.json().get("data", [])
    if year < current_season():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    return data


def _partials(p: dict) -> list[int]:
    out = [p.get(f"partials{i}") for i in range(1, 5)]
    out = [x for x in out if x is not None]
    extra = p.get("extraPeriods") or {}
    out += [v for _, v in sorted(extra.items()) if v is not None]
    return out


def games(league: str, year: int):
    code = LEAGUES[league]
    for g in _season_games(code, year):
        home, away = g.get("local") or {}, g.get("road") or {}
        hc, ac = home.get("club") or {}, away.get("club") or {}
        if not hc.get("code") or not ac.get("code") or not g.get("utcDate"):
            continue
        played = bool(g.get("played"))
        row = {"league": league, "game_id": g["identifier"], "season": year + 1,
               "tipoff": datetime.fromisoformat(g["utcDate"].replace("Z", "+00:00")),
               "status": "final" if played else "scheduled", "neutral": False,
               "home_id": hc["code"], "away_id": ac["code"],
               "home_name": hc.get("name") or hc["code"], "away_name": ac.get("name") or ac["code"]}
        if played:
            hp, ap = _partials(home.get("partials") or {}), _partials(away.get("partials") or {})
            row.update(home_pts=home.get("score"), away_pts=away.get("score"))
            if len(hp) >= 4 and len(ap) >= 4:
                row.update(home_reg=sum(hp[:4]), away_reg=sum(ap[:4]), home_h1=sum(hp[:2]),
                           away_h1=sum(ap[:2]), periods=max(len(hp), len(ap)))
        teams = [{"league": league, "team_id": c["code"], "name": c.get("name") or c["code"],
                  "short_name": c.get("editorialName") or c.get("abbreviatedName"),
                  "location": c.get("abbreviatedName"), "abbrev": c.get("tvCode")}
                 for c in (hc, ac)]
        yield row, teams
