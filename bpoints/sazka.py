"""SazkaBet's own JSON (www.allwyn.cz/rest/sportsbook/...; SazkaBet is Allwyn's
since 2026) for basketball points lines.

    /rest/sportsbook/events/prematch?leagueIds=551&leagueIds=1059   games still to start

One call lists every game of the leagues asked for, each with its featured
markets inline: TOTAL_POINTS_OVER/UNDER (both teams' points, a short ladder)
and TOTAL_POINTS_OVER/UNDER_1ST_HALF (the main line only). Each team's points
are on a game's own page, which comes from another host (apigw.allwyn.cz) and
is not read. The totals are taken to include overtime, as Betano's do: their
lines sit within a few points of Betano's, above as often as below.

The site's own code sends a fixed X-API-KEY with these calls (the same for
every visitor; without it the answer is 401), so this does too - the owner
chose to read SazkaBet this way. Read gently, one call a second; a refusal
stops the run.
"""

import logging
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

BASE = "https://www.allwyn.cz"
API_KEY = "tMfvrMeSypT5qQW"           # the website's own, sent by every browser
GAP = 1.0
# SazkaBet's league id -> ours (the id is in its league pages' links:
# /kurzove-sazky/kurzy/5/551 is the NBA).
LEAGUES = {551: "nba", 1059: "ncaam", 9783: "ncaaw", 670: "euroleague", 2366: "eurocup",
           819: "acb", 514: "lba", 2365: "nbl"}
MARKETS = {"TOTAL_POINTS_OVER/UNDER": ("total", "ft"),
           "TOTAL_POINTS_OVER/UNDER_1ST_HALF": ("total", "h1")}

# SazkaBet writes some American places in Czech, and marks women's teams with
# "Ž" or "(W)"; names.py compares English names.
CZECH = [("Severní Karolína", "North Carolina"), ("Jižní Karolína", "South Carolina"),
         ("Severní Dakota", "North Dakota"), ("Jižní Dakota", "South Dakota"),
         ("Západní Virginie", "West Virginia"), ("Virginie", "Virginia"),
         ("Kalifornie", "California"), ("Nové Mexiko", "New Mexico"), ("Severní Texas", "North Texas"),
         ("Jižní Florida", "South Florida"), ("Jižní Alabama", "South Alabama"),
         ("Severní Iowa", "Northern Iowa"), ("Severní Arizona", "Northern Arizona"),
         ("Pensylvánie", "Pennsylvania"), ("Georgie", "Georgia"), ("Gruzie", "Georgia")]


def english(name: str) -> str:
    for cz, en in CZECH:
        name = name.replace(cz, en)
    name = name.replace("(W)", "")
    return " ".join(w for w in name.split() if w != "Ž")


_s = requests.Session()
_s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                 "AppleWebKit/537.36 Chrome/128 Safari/537.36",
                   "Accept": "application/json", "X-API-KEY": API_KEY})
_last = 0.0


class Blocked(RuntimeError):
    """SazkaBet answered with something other than its JSON."""


def _get(path: str, params=None):
    global _last
    wait = GAP - (time.monotonic() - _last)
    if wait > 0:
        time.sleep(wait)
    _last = time.monotonic()
    for attempt in range(7):          # through this PC's DNS drops, as in betano.py
        try:
            r = _s.get(BASE + path, params=params, timeout=40)
            break
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 6:
                raise
            log.warning("sazka %s: connection failed - retrying", path)
            time.sleep(min(5 * 2 ** attempt, 40))
    if r.status_code in (401, 403, 429, 503) or "json" not in r.headers.get("content-type", ""):
        raise Blocked(f"sazka {path}: HTTP {r.status_code}, {r.headers.get('content-type')}")
    r.raise_for_status()
    return r.json()


def games(leagues: list[str] | None = None) -> list[tuple[dict, list[dict]]]:
    """(event, odds rows) for every game still to start in our leagues."""
    ids = [i for i, ours in LEAGUES.items() if not leagues or ours in leagues]
    data = _get("/rest/sportsbook/events/prematch", params=[("leagueIds", i) for i in ids])
    now = datetime.now(timezone.utc)
    out = []
    for e in data or []:
        league_id = int(e.get("competitionDrilldownTagId") or 0)
        teams = {t.get("side"): t.get("name") for t in e.get("teams") or []}
        start = datetime.fromisoformat(e["startTime"].replace("Z", "+00:00"))
        if (league_id not in LEAGUES or e.get("liveNow") or e.get("started") or start <= now
                or not teams.get("HOME") or not teams.get("AWAY")):
            continue
        league_name = next((n["name"] for n in e.get("drilldownNodes") or []
                            if str(n.get("id")) == str(league_id)), str(league_id))
        ev = {"event_id": str(e["id"]), "kickoff": start, "league": league_name,
              "our_league": LEAGUES[league_id], "team1": english(teams["HOME"]),
              "team2": english(teams["AWAY"]),
              "url": f"/kurzove-sazky/kurzy/5?eventId={e['id']}"}
        rows = {}
        for m in e.get("groupedMarkets") or []:
            spec = MARKETS.get(m.get("groupCode"))
            if not spec:
                continue
            market, period = spec
            for o in m.get("outcomes") or []:
                name, line = o.get("name") or "", o.get("handicapValue")
                price = (o.get("prices") or [{}])[0].get("decimal")
                side = "over" if name.startswith("Nad") else "under" if name.startswith("Pod") else None
                if side and line is not None and price and o.get("active", True):
                    rows[(market, period, 0, side, float(line))] = float(price)
        out.append((ev, [{"market": k[0], "period": k[1], "team": k[2], "side": k[3], "line": k[4],
                          "price": p} for k, p in rows.items()]))
    return out
