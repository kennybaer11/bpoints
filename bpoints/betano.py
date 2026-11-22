"""Betano's own JSON (www.betano.cz/api/...) for basketball points lines.

    /api/sport/basketbal/                  every league
    /api{league url}                       a league's games
    /api/zapas-sance/{slug}/{id}/?bt=N     one game; N picks the market tab ("all")

Market types are stable codes. Totals include overtime (Betano marks the
regulation-only markets, "zákl. hrací doba", and these are not):

    FTPO   both teams' points, a ladder of lines
    OUHO   the home team's (first-named) points, a ladder
    OUAO   the away team's points, a ladder
    OUH1   first-half points; FH1H / FH1A each team's first-half points

A market comes either as a plain list of selections or as a table (rows of
over/under pairs); both are read. Read gently: a request a second. A
challenge page instead of JSON stops the run; it never tries to get past one.
"""

import logging
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

BASE = "https://www.betano.cz"
GAP = 1.0
MARKETS = {"FTPO": ("total", "ft", 0), "OUHO": ("team_total", "ft", 1),
           "OUAO": ("team_total", "ft", 2), "OUH1": ("total", "h1", 0),
           "FH1H": ("team_total", "h1", 1), "FH1A": ("team_total", "h1", 2)}
# Betano's league URL -> our league. Only leagues with a results feed are
# priced: without results there is no model and nothing to settle.
LEAGUES = {"/usa/nba/": "nba", "/usa/nba-cup/": "nba", "/usa/wnba/": "wnba",
           "/usa/ncaa/": "ncaam", "/usa/wncaa/": "ncaaw",
           "/euroliga/euroliga/": "euroleague", "/eurocup/eurocup/": "eurocup",
           "/spanelsko/liga-endesa/": "acb", "/australie/nbl/": "nbl"}

_s = requests.Session()
_s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                 "AppleWebKit/537.36 Chrome/128 Safari/537.36",
                   "Accept": "application/json"})
_last = 0.0


class Blocked(RuntimeError):
    """Betano answered with something other than its JSON."""


def _get(path: str) -> dict:
    global _last
    wait = GAP - (time.monotonic() - _last)
    if wait > 0:
        time.sleep(wait)
    _last = time.monotonic()
    # A failed lookup or dropped connection (this PC loses DNS for a minute now
    # and then) is retried for about two minutes; a refusal is not retried.
    for attempt in range(7):
        try:
            r = _s.get(BASE + path, timeout=30)
            break
        except requests.ConnectionError:
            if attempt == 6:
                raise
            log.warning("%s: connection failed - retrying", path)
            time.sleep(min(5 * 2 ** attempt, 40))
    if r.status_code in (403, 429, 503) or "json" not in r.headers.get("content-type", ""):
        raise Blocked(f"{path}: HTTP {r.status_code}, {r.headers.get('content-type')}")
    r.raise_for_status()
    body = r.json()
    if "data" not in body:
        log.warning("%s: no data (errorCode %s)", path, body.get("errorCode"))
        return {}
    return body["data"]


def leagues() -> list[dict]:
    out = []
    for group in _get("/api/sport/basketbal/").get("regionGroups", []):
        for region in group.get("regions", []):
            for lg in region.get("leagues", []):
                url = lg.get("url", "")
                ours = next((v for k, v in LEAGUES.items() if k in url), None)
                if ours:
                    out.append({"name": lg.get("name", ""), "url": url, "ours": ours})
    return out


def league_events(league: dict) -> list[dict]:
    """Games still to start."""
    data = _get("/api" + league["url"])
    now = datetime.now(timezone.utc)
    out = []
    for block in data.get("blocks", []):
        for e in block.get("events", []):
            parts = e.get("participants") or []
            start = datetime.fromtimestamp(e["startTime"] / 1000, timezone.utc)
            if e.get("liveNow") or len(parts) != 2 or start <= now:
                continue
            out.append({"event_id": str(e["id"]), "kickoff": start, "league": league["name"],
                        "our_league": league["ours"], "url": e["url"],
                        "team1": parts[0]["name"], "team2": parts[1]["name"]})
    return out


def _selections(m: dict):
    """Every selection of a market, whichever shape it comes in, with the
    column it sits in (0 over, 1 under) when it is a table."""
    for s in m.get("selections") or []:
        yield s, None
    for row in (m.get("tableLayout") or {}).get("rows", []):
        for g in row.get("groupSelections", []):
            for s in g.get("selections", []):
                yield s, s.get("columnIndex")


def event_odds(event: dict) -> list[dict]:
    """Every selection of the points markets, one row each."""
    path = "/api" + event["url"]
    data = _get(path)
    tabs = data.get("markets") or data.get("nonBetBuilderTabs") or []
    alls = [t["id"] for t in tabs if t.get("type") in ("all", "allmarkets")]
    ev = _get(f"{path}?bt={alls[0]}")["event"] if alls else data.get("event", {})
    rows = {}
    for m in ev.get("markets", []):
        spec = MARKETS.get(str(m.get("type")))
        if not spec:
            continue
        market, period, team = spec
        for s, col in _selections(m):
            name, line, price = s.get("name", ""), s.get("handicap"), s.get("price")
            side = ("over" if name.startswith("Více") else "under" if name.startswith("Méně")
                    else {0: "over", 1: "under"}.get(col))
            if side is None or line is None or not price:
                continue
            # The same line can appear in a market's short list and its full
            # ladder; keep one.
            rows[(market, period, team, side, float(line))] = price
    return [{"market": k[0], "period": k[1], "team": k[2], "side": k[3], "line": k[4], "price": p}
            for k, p in rows.items()]
