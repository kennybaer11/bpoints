"""The Odds API (the-odds-api.com): the market's line for the European games,
where DraftKings (ESPN) has none.

    GET /v4/sports/basketball_euroleague/odds?regions=eu&markets=totals,spreads

One call returns every upcoming game with each European bookmaker's main total
and handicap. Its cost is one credit per market per region (2 here); the free
plan has 500 a month, and every answer says how many are left.

The centre is Pinnacle's line when it is among the books (the sharpest there
is), else the median of all of them - either way much sharper than Betano's
own ladder, which is what the European games were centred on before.
"""

import logging
import os
import statistics
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import dotenv_values

log = logging.getLogger(__name__)

API = "https://api.the-odds-api.com/v4"
SPORTS = {"euroleague": "basketball_euroleague"}
POSESSION_ENV = Path(os.getenv("POSESSION_ENV", r"C:\Users\kenny\posession\scraper\.env"))
SHARP = "pinnacle"


def key() -> str:
    k = os.getenv("ODDS_API_KEY") or (
        dotenv_values(POSESSION_ENV).get("ODDS_API_KEY") if POSESSION_ENV.exists() else None)
    if not k:
        raise SystemExit(f"ODDS_API_KEY is not set (nor in {POSESSION_ENV})")
    return k


class OutOfCredits(RuntimeError):
    pass


def _get(path: str, params: dict) -> tuple[list, int | None]:
    params = {**params, "apiKey": key()}
    for attempt in range(5):
        try:
            r = requests.get(API + path, params=params, timeout=40)
            break
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 4:
                raise
            time.sleep(min(5 * 2 ** attempt, 40))
    left = r.headers.get("x-requests-remaining")
    left = int(float(left)) if left not in (None, "") else None
    if r.status_code in (401, 429) or (r.status_code == 422 and "QUOTA" in r.text.upper()):
        raise OutOfCredits(f"the odds api {path}: HTTP {r.status_code} {r.text[:200]}")
    r.raise_for_status()
    return r.json(), left


def _centre(points: dict[str, float]) -> tuple[float, str] | None:
    if not points:
        return None
    if SHARP in points:
        return points[SHARP], SHARP
    return statistics.median(points.values()), f"median of {len(points)}"


def games(league: str) -> tuple[list[dict], int | None]:
    """Each upcoming game: kickoff, home and away names, the centre total and
    the home team's handicap (negative when it is favoured), and where they
    came from; plus the credits left."""
    data, left = _get(f"/sports/{SPORTS[league]}/odds",
                      {"regions": "eu", "markets": "totals,spreads", "oddsFormat": "decimal"})
    out = []
    for e in data or []:
        totals, spreads = {}, {}
        for b in e.get("bookmakers") or []:
            for m in b.get("markets") or []:
                oc = m.get("outcomes") or []
                if m.get("key") == "totals":
                    pts = {o.get("point") for o in oc if o.get("point") is not None}
                    if len(pts) == 1:            # its main line, over and under
                        totals[b["key"]] = float(pts.pop())
                elif m.get("key") == "spreads":
                    home = next((o for o in oc if o.get("name") == e["home_team"]), None)
                    if home and home.get("point") is not None:
                        spreads[b["key"]] = float(home["point"])
        t, s = _centre(totals), _centre(spreads)
        out.append({"kickoff": datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")),
                    "team1": e["home_team"], "team2": e["away_team"],
                    "total": t[0] if t else None, "spread": s[0] if s else None,
                    "from": t[1] if t else None, "books": sorted(totals)})
    return out, left
