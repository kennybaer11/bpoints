"""Fortuna's points lines through webscraper.io's Cloud, twice a day.

    python fortuna_pipeline.py --dry-run      # which games it would read; no jobs, no credits
    python fortuna_pipeline.py --links-only   # stage 1 (a credit per league page) and the pick
    python fortuna_pipeline.py                # both stages, then store the lines (bookmaker 'fortuna')

Fortuna (ifortuna.cz) serves scripts a Cloudflare "Geoblock" page, but not
webscraper.io's Cloud browser - the same way as Chance (chance_pipeline.py):

  1. the league pages of the tipped leagues -> every game's link and teams;
  2. only the games that pair with one of ours tipping off in the next
     --hours (12) -> each game's page, with the first-half cards clicked open.

A game page has full ladders: the total, each team's points (both with
overtime) and the same three for the first half. Each odds button names its
line ("Méně než 203.5: 3.05"). The scraped time is not used (the Cloud browser
runs in another timezone); the tip-off is the results feed's.
"""

import argparse
import logging
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from psycopg.rows import dict_row

from bpoints import names, webscraper as ws

log = logging.getLogger("fortuna")

BOOKMAKER = "fortuna"
SITE = "https://www.ifortuna.cz"
LEAGUE_PAGES = {
    "nba": f"{SITE}/sazeni/basketbal/usa-1/nba",
    "ncaam": f"{SITE}/sazeni/basketbal/usa-1/ncaa-4",
    "euroleague": f"{SITE}/sazeni/basketbal/mezinarodni/euroliga-muzi",
}

LEAGUE_NAMES = {"nba": "NBA", "ncaam": "NCAA", "euroleague": "Euroliga"}

CARD = "div[data-testing-selector='MatchDetailCard']"
LINKS_SITEMAP = {
    "_id": "bpoints_fortuna_links",
    "startUrl": list(LEAGUE_PAGES.values()),
    "selectors": [
        {"id": "game", "type": "SelectorLink", "parentSelectors": ["_root"],
         "selector": "a[href^='/sazeni/basketbal/']", "multiple": True, "linkType": "linkFromHref"},
    ],
}
GAMES_SITEMAP = {
    "_id": "bpoints_fortuna_games",
    "startUrl": [f"{SITE}/sazeni/basketbal/usa-1/nba/orlando-magic-detroit-pistons"],
    "selectors": [
        {"id": "home", "type": "SelectorText", "parentSelectors": ["_root"],
         "selector": "span.participants-panel__participant-label:eq(0)", "multiple": False, "regex": ""},
        {"id": "away", "type": "SelectorText", "parentSelectors": ["_root"],
         "selector": "span.participants-panel__participant-label:eq(1)", "multiple": False, "regex": ""},
        # The points cards, the first-half ones clicked open first (they come shut).
        {"id": "card", "type": "SelectorElementClick", "parentSelectors": ["_root"],
         "selector": f"{CARD}[aria-label*='bodů']",
         "clickElementSelector": f"{CARD}[aria-expanded='false'][aria-label*='bodů v 1. poločasu'] > div.cursor-pointer",
         "clickType": "clickOnce", "discardInitialElements": "do-not-discard",
         "clickElementUniquenessType": "uniqueCSSSelector", "delay": 1500, "multiple": True},
        {"id": "market", "type": "SelectorElementAttribute", "parentSelectors": ["card"],
         "selector": "_parent_", "extractAttribute": "aria-label", "multiple": False},
        {"id": "outcome", "type": "SelectorElementAttribute", "parentSelectors": ["card"],
         "selector": "button[data-testing-selector='OddsButton']", "extractAttribute": "aria-label",
         "multiple": True},
    ],
}

# A listed game's text runs together ("dnes 18:00BBOrlando MagicDetroit
# PistonsVýsledek zápasuOrlando Magic2.19Remíza17.00Detroit Pistons1.81..."):
# the teams are read from its first market, each name followed by its odds.
TEAMS = re.compile(r"(?:Výsledek zápasu|Vítěz zápasu \(včetně prodloužení\))(.+?)\d+\.\d+"
                   r"(?:Remíza\d+\.\d+)?(.+?)\d+\.\d+")
START = re.compile(r"^\s*(?:dnes|zítra|\w+\s)?\s*(?:\d{1,2}\.\s?\d{1,2}\.\s*)?\d{1,2}:\d{2}(?:BB)?")


def candidates(text: str) -> list[tuple[str, str]]:
    """The two teams of a listed game: from its first market when it shows one;
    else (a game listed without odds: "dnes 13:00Rutgers Scarlet KnightsSacred
    Heart Pioneers") each place where a small letter meets a capital, for
    pair() to try in turn - only the right split pairs with a game of ours."""
    m = TEAMS.search(text)
    if m:
        return [(m.group(1).strip(), m.group(2).strip())]
    s = START.sub("", text).strip()
    return [(s[:i + 1].strip(), s[i + 1:].strip())
            for i in range(len(s) - 1) if re.match(r"[a-z\.\)]", s[i]) and s[i + 1].isupper()]


OUTCOME = re.compile(r"^(Méně|Více) než ([\d.]+)\s*:\s*([\d.]+)$")


def market_of(label: str, home: str, away: str) -> tuple[str, str, int] | None:
    """(market, period, team) for a card's title, or None for any other card:
    'Počet bodů (včetně prodloužení)', 'Orlando Magic počet bodů v 1. poločasu'."""
    label = " ".join(label.split())
    m = re.fullmatch(r"(.*?)\s*[Pp]očet bodů (\(včetně prodloužení\)|v 1\. poločasu)", label)
    if not m or "hráč" in label:
        return None
    period = "ft" if "prodloužení" in m.group(2) else "h1"
    who = m.group(1).strip()
    if not who:
        return "total", period, 0
    who = " ".join(who.split())
    if who == " ".join(home.split()):
        return "team_total", period, 1
    if who == " ".join(away.split()):
        return "team_total", period, 2
    return None


def lines(records: list[dict], home: str, away: str) -> list[dict]:
    rows = {}
    for r in records:
        spec = market_of(r.get("market") or "", home, away)
        m = OUTCOME.match((r.get("outcome") or "").strip())
        if not spec or not m:
            continue
        side = "under" if m.group(1) == "Méně" else "over"
        market, period, team = spec
        rows[(market, period, team, side, float(m.group(2)))] = float(m.group(3))
    return [{"market": k[0], "period": k[1], "team": k[2], "side": k[3], "line": k[4], "price": p}
            for k, p in rows.items()]


def our_games(cur, leagues, hours):
    cur.execute("""SELECT * FROM bpoints.game WHERE league = ANY(%s) AND status <> 'final'
                      AND tipoff BETWEEN now() + interval '10 minutes' AND now() + %s * interval '1 hour'""",
                (leagues, hours))
    by = defaultdict(list)
    for g in cur.fetchall():
        by[g["league"]].append(g)
    return by


def pick(cur, link_rows, games):
    """(league, game link, our game, swapped) for each listed Fortuna game of ours.
    A game's link is /sazeni/basketbal/<country>/<league>/<home>-<away>; its
    text names the two teams on their own lines (live games add the score)."""
    page_league = {url: lg for lg, url in LEAGUE_PAGES.items()}
    teams = {}
    for lg in games:
        cur.execute("SELECT * FROM bpoints.team WHERE league = %s", (lg,))
        teams[lg] = {t["team_id"]: t for t in cur.fetchall()}
    out, seen = [], set()
    mid = datetime.now(timezone.utc) + timedelta(hours=6)
    for r in link_rows:
        lg = page_league.get(r.get("web-scraper-start-url") or r.get("web_scraper_start_url") or "")
        href = r.get("game-href") or ""
        path = "/" + href.split("://", 1)[-1].split("/", 1)[-1] if "://" in href else href
        parts = path.strip("/").split("/")
        if not lg or len(parts) != 5 or path in seen or not path.startswith(
                LEAGUE_PAGES[lg].replace(SITE, "") + "/"):
            continue
        hit = None
        for t1, t2 in candidates(r.get("game") or ""):
            hit = names.pair({"team1": t1, "team2": t2, "kickoff": mid}, games.get(lg, []), teams.get(lg, {}))
            if hit:
                break
        if hit:
            seen.add(path)
            out.append((lg, SITE + path, hit[0], hit[1]))
    return out


def store(games, scraped):
    from bpoints import db
    conn = db.connect()
    try:
        for e, rows in games:
            if not rows:
                continue
            db.save_odds(conn, BOOKMAKER, e, rows)
            with conn.cursor() as cur:
                cur.execute("UPDATE bpoints.bk_event SET last_seen = %s WHERE (bookmaker, event_id) = (%s, %s)",
                            (scraped, BOOKMAKER, e["event_id"]))
                cur.execute("""UPDATE bpoints.odds SET last_seen = %s
                                WHERE (bookmaker, event_id) = (%s, %s)
                                  AND last_seen >= now() - interval '1 minute'""",
                            (scraped, BOOKMAKER, e["event_id"]))
            conn.commit()
    finally:
        conn.close()
    log.info("%d games stored", sum(1 for _, r in games if r))


def main():
    ap = argparse.ArgumentParser(description="Fortuna's points lines via webscraper.io.")
    ap.add_argument("--hours", type=float, default=12)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--links-only", action="store_true")
    ap.add_argument("--min-gap-hours", type=float, default=3)
    ap.add_argument("--min-credits", type=int, default=500)
    ap.add_argument("--max-games", type=int, default=60)
    ap.add_argument("--leagues", help="only these of ours, e.g. euroleague")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    load_dotenv(".env")
    from advice import LEAGUES
    from bpoints import db

    def fresh():
        return db.open_conn(row_factory=dict_row)

    leagues = [lg for lg in LEAGUES if lg in LEAGUE_PAGES
               and (not args.leagues or lg in args.leagues.split(","))]
    with fresh() as conn:
        games = our_games(conn.cursor(), leagues, args.hours)
    log.info("our games in the next %g hours: %s", args.hours,
             ", ".join(f"{lg} {len(games.get(lg, []))}" for lg in leagues))
    credits = ws.credits_left()
    log.info("webscraper.io credits left: %s", credits)
    if args.dry_run:
        log.info("would read %d league pages, then up to %d games (a credit each)",
                 sum(1 for lg in leagues if games.get(lg)),
                 min(args.max_games, sum(len(v) for v in games.values())))
        return 0
    with fresh() as conn:
        last = conn.execute("SELECT max(last_seen) AS t FROM bpoints.bk_event WHERE bookmaker = %s",
                            (BOOKMAKER,)).fetchone()["t"]
    if last and datetime.now(timezone.utc) - last < timedelta(hours=args.min_gap_hours):
        log.info("last Fortuna read %s ago - skipped", str(datetime.now(timezone.utc) - last).split(".")[0])
        return 0
    if credits is not None and credits < args.min_credits:
        log.error("only %d credits left (keeping %d for the football and tennis odds) - skipped",
                  credits, args.min_credits)
        return 1
    if not any(games.values()):
        log.info("no games of ours to read")
        return 0

    job = ws.wait_for(ws.start_job(ws.ensure_sitemap(LINKS_SITEMAP),
                                   [LEAGUE_PAGES[lg] for lg in leagues if games.get(lg)]))
    if job.get("status") != "finished":
        log.error("stage 1 job %s: %s", job.get("id"), job.get("status"))
        return 1
    rows = ws.job_rows(job["id"])
    with fresh() as conn:
        picked = pick(conn.cursor(), rows, games)
    log.info("stage 1: %d of our games listed on Fortuna", len(picked))
    for lg, link, g, _ in picked:
        log.info("  %s  %s - %s  %s", lg, g["home_name"], g["away_name"], link)
    if args.links_only or not picked:
        return 0
    picked = sorted(picked, key=lambda x: x[2]["tipoff"])[:args.max_games]

    job = ws.wait_for(ws.start_job(ws.ensure_sitemap(GAMES_SITEMAP), [link for _, link, _, _ in picked]))
    scraped = datetime.now(timezone.utc)
    if job.get("status") != "finished":
        log.error("stage 2 job %s: %s", job.get("id"), job.get("status"))
        return 1
    by_url = defaultdict(list)
    for r in ws.job_rows(job["id"]):
        by_url[(r.get("web-scraper-start-url") or r.get("web_scraper_start_url") or "").rstrip("/")].append(r)
    out = []
    for lg, link, g, _ in picked:
        recs = by_url.get(link.rstrip("/"), [])
        first = next((r for r in recs if r.get("home") and r.get("away")), None)
        if not first:
            log.warning("no page read for %s", link)
            continue
        t1, t2 = " ".join(first["home"].split()), " ".join(first["away"].split())
        path = link.replace(SITE, "")
        e = {"event_id": path.rsplit("/", 1)[-1], "kickoff": g["tipoff"], "league": LEAGUE_NAMES[lg], "our_league": lg,
             "team1": t1, "team2": t2, "url": path}
        rows = lines(recs, t1, t2)
        out.append((e, rows))
        log.info("  %s - %s: %d lines", t1, t2, len(rows))
    store(out, scraped)
    log.info("credits left: %s", ws.credits_left())
    return 0


if __name__ == "__main__":
    sys.exit(main())
