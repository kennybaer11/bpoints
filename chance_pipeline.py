"""Chance's points lines through webscraper.io's Cloud, twice a day.

    python chance_pipeline.py --dry-run     # which games it would scrape; no jobs, no credits
    python chance_pipeline.py --links-only  # stage 1 (one credit per league page) and the pick
    python chance_pipeline.py               # both stages, then store the lines

Chance turns away scripts but not webscraper.io's Cloud browser (posession's
football and tennis odds come the same way). One page is one credit, from the
account posession shares, so only what can be tipped is read:

  1. the league pages of the tipped leagues (advice.LEAGUES) -> game links;
  2. only the games that pair with one of ours tipping off in the next
     --hours (12) -> each game's points markets.

The Cloud browser runs nine hours behind Prague, so Chance's "Dnes | 21:40" is
read in the wrong timezone and can name the wrong day: the scraped time is
never used. A game is found by its teams among ours, and its tip-off is the
results feed's. The lines are stored with the time of the scrape; advice.py
stops tipping them MAX_AGE after it.

A run is skipped when the last one was under --min-gap-hours ago, or when the
account is down to --min-credits (the football and tennis odds need them too).
"""

import argparse
import logging
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from psycopg.rows import dict_row

import chance_import as ci
from bpoints import names, webscraper as ws

log = logging.getLogger("chance")

# Chance's league pages for the tipped leagues (the id is its competition id).
# The WNBA's is added when its season is on Chance (not listed in the winter).
LEAGUE_PAGES = {
    "nba": "https://www.chance.cz/kurzy/basketbal/basketbal-muzi/nba-23",
    "ncaam": "https://www.chance.cz/kurzy/basketbal/basketbal-muzi/usa-ncaa-80576",
}

LINKS_SITEMAP = {
    "_id": "bpoints_chance_links",
    "startUrl": list(LEAGUE_PAGES.values()),
    "selectors": [
        {"id": "game", "type": "SelectorElement", "parentSelectors": ["_root"],
         "selector": "[data-atid^='content||MATCH']", "multiple": True},
        {"id": "match_id", "type": "SelectorElementAttribute", "parentSelectors": ["game"],
         "selector": "span[data-m]", "extractAttribute": "data-m", "multiple": False},
        {"id": "match_name", "type": "SelectorText", "parentSelectors": ["game"],
         "selector": "span[data-m]", "multiple": False, "regex": ""},
        {"id": "match_link", "type": "SelectorLink", "parentSelectors": ["game"],
         "selector": "a[href*='/kurzy/zapas/']", "multiple": False, "linkType": "linkFromHref"},
    ],
}


def _market(col: str, selection_id: str, with_team: bool = False) -> list[dict]:
    """A points market: one element per row of its ladder, under then over."""
    btn = "div[class*='BetButton-styled__BetButton']:eq({})"
    out = [{"id": col, "type": "SelectorElement", "parentSelectors": ["_root"],
            "selector": f"[data-my-selection-id='{selection_id}'] div[class*='EventTableBox-styled__Row']",
            "multiple": True}]
    if with_team:
        out.append({"id": f"{col}_team", "type": "SelectorText", "parentSelectors": [col],
                    "selector": "div.truncate", "multiple": False, "regex": ""})
    for i, side in enumerate(("under", "over")):
        out.append({"id": f"{col}_{side}", "type": "SelectorText", "parentSelectors": [col],
                    "selector": btn.format(i) + " span[class*='BetButton-styled__Name']",
                    "multiple": False, "regex": ""})
        out.append({"id": f"{col}_{side}_odds", "type": "SelectorText", "parentSelectors": [col],
                    "selector": btn.format(i) + " > div > span", "multiple": False, "regex": ""})
    return out


GAMES_SITEMAP = {
    "_id": "bpoints_chance_games",
    "startUrl": ["https://www.chance.cz/kurzy/zapas/basketbal-miami-heat-oklahoma-city-thunder/8523606"],
    "selectors": [
        {"id": "home", "type": "SelectorText", "parentSelectors": ["_root"],
         "selector": "div[class*='MatchDetail-styled__Name']:eq(0)", "multiple": False, "regex": ""},
        {"id": "away", "type": "SelectorText", "parentSelectors": ["_root"],
         "selector": "div[class*='MatchDetail-styled__Name']:eq(1)", "multiple": False, "regex": ""},
        *_market("total_ft", "7-ASIAN_TOTAL-0"),
        *_market("team_ft", "7-TOTAL_PARTICIPANT-0", with_team=True),
        *_market("total_h1", "7-ASIAN_TOTAL-2"),
    ],
}


def our_games(cur, leagues: list[str], hours: float) -> dict[str, list[dict]]:
    """Our games tipping off in the next `hours`, and the teams' names, per league."""
    cur.execute("""SELECT * FROM bpoints.game WHERE league = ANY(%s) AND status <> 'final'
                      AND tipoff BETWEEN now() + interval '10 minutes' AND now() + %s * interval '1 hour'""",
                (leagues, hours))
    by = defaultdict(list)
    for g in cur.fetchall():
        by[g["league"]].append(g)
    return by


def pick(cur, link_rows: list[dict], games: dict[str, list[dict]]) -> list[tuple[str, str, dict, bool]]:
    """(league, game link, our game, swapped) for each listed Chance game that is one of ours."""
    page_league = {url: lg for lg, url in LEAGUE_PAGES.items()}
    teams = {}
    for lg in games:
        cur.execute("SELECT * FROM bpoints.team WHERE league = %s", (lg,))
        teams[lg] = {t["team_id"]: t for t in cur.fetchall()}
    out, seen = [], set()
    for r in link_rows:
        lg = page_league.get(r.get("web-scraper-start-url") or r.get("web_scraper_start_url") or "")
        link, name = r.get("match_link-href") or "", r.get("match_name") or ""
        if not lg or not ci.match_id(link) or " - " not in name or ci.match_id(link) in seen:
            continue
        t1, t2 = (s.strip() for s in name.split(" - ", 1))
        # Any time inside the window will do: pair() looks 18 hours either side.
        mid = datetime.now(timezone.utc) + timedelta(hours=6)
        hit = names.pair({"team1": t1, "team2": t2, "kickoff": mid}, games.get(lg, []), teams.get(lg, {}))
        if hit:
            seen.add(ci.match_id(link))
            out.append((lg, link, hit[0], hit[1]))
    return out


def main():
    ap = argparse.ArgumentParser(description="Chance's points lines via webscraper.io.")
    ap.add_argument("--hours", type=float, default=12, help="games tipping off within this many hours")
    ap.add_argument("--dry-run", action="store_true", help="no jobs: what would be scraped")
    ap.add_argument("--links-only", action="store_true", help="stage 1 and the pick, no stage 2")
    ap.add_argument("--min-gap-hours", type=float, default=3)
    ap.add_argument("--min-credits", type=int, default=500)
    ap.add_argument("--max-games", type=int, default=60, help="a cap on stage 2's credits")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    load_dotenv(".env")
    from advice import LEAGUES
    from bpoints import db

    # A short connection each time: the database drops one left idle while a
    # job runs, as it did the first time this ran.
    def fresh():
        return db.open_conn(row_factory=dict_row)

    leagues = [lg for lg in LEAGUES if lg in LEAGUE_PAGES]
    with fresh() as conn:
        games = our_games(conn.cursor(), leagues, args.hours)
    log.info("our games in the next %g hours: %s", args.hours,
             ", ".join(f"{lg} {len(games.get(lg, []))}" for lg in leagues))
    credits = ws.credits_left()
    log.info("webscraper.io credits left: %s", credits)
    if args.dry_run:
        log.info("would scrape %d league pages, then up to %d games (one credit each)",
                 len(leagues), min(args.max_games, sum(len(v) for v in games.values())))
        return 0
    with fresh() as conn:
        last = conn.execute("SELECT max(last_seen) AS t FROM bpoints.bk_event WHERE bookmaker = %s",
                            (ci.BOOKMAKER,)).fetchone()["t"]
    if last and datetime.now(timezone.utc) - last < timedelta(hours=args.min_gap_hours):
        log.info("last Chance scrape %s ago - under %g hours, skipped",
                 str(datetime.now(timezone.utc) - last).split(".")[0], args.min_gap_hours)
        return 0
    if credits is not None and credits < args.min_credits:
        log.error("only %d credits left (keeping %d for the football and tennis odds) - skipped",
                  credits, args.min_credits)
        return 1
    if not any(games.values()):
        log.info("no games of ours to scrape")
        return 0

    links_id = ws.ensure_sitemap(LINKS_SITEMAP)
    job = ws.wait_for(ws.start_job(links_id, [LEAGUE_PAGES[lg] for lg in leagues if games.get(lg)]))
    if job.get("status") != "finished":
        log.error("stage 1 job %s: %s", job.get("id"), job.get("status"))
        return 1
    rows = ws.job_rows(job["id"])
    with fresh() as conn:
        picked = pick(conn.cursor(), rows, games)
    log.info("stage 1: %d of our games listed on Chance", len(picked))
    for lg, link, g, swapped in picked:
        log.info("  %s  %s - %s  %s", lg, g["home_name"], g["away_name"], link)
    if args.links_only or not picked:
        return 0
    picked = sorted(picked, key=lambda x: x[2]["tipoff"])[:args.max_games]

    games_id = ws.ensure_sitemap(GAMES_SITEMAP)
    job = ws.wait_for(ws.start_job(games_id, [link for _, link, _, _ in picked]))
    scraped = datetime.now(timezone.utc)
    if job.get("status") != "finished":
        log.error("stage 2 job %s: %s", job.get("id"), job.get("status"))
        return 1
    by_url = defaultdict(list)
    for r in ws.job_rows(job["id"]):
        by_url[ci.match_id(r.get("web-scraper-start-url") or r.get("web_scraper_start_url") or "")].append(r)
    out = []
    for lg, link, g, swapped in picked:
        ev = ci.match_id(link)
        recs = by_url.get(ev, [])
        first = next((r for r in recs if r.get("home") and r.get("away")), None)
        if not first:
            log.warning("no page read for %s", link)
            continue
        t1, t2 = first["home"].strip(), first["away"].strip()
        e = {"event_id": ev, "kickoff": g["tipoff"], "league": lg, "our_league": lg,
             "team1": t1, "team2": t2, "url": ci.site_path(link)}
        rows = ci.market_lines(recs, t1, t2)
        out.append((e, rows))
        log.info("  %s - %s: %d lines", t1, t2, len(rows))
    ci.store(out, scraped)
    log.info("credits left: %s", ws.credits_left())
    return 0


if __name__ == "__main__":
    sys.exit(main())
