"""Store Chance's basketball points lines from a webscraper.io CSV.

    python chance_import.py chance.csv               # scraped when the file was saved
    python chance_import.py chance.csv --scraped-at "2026-11-22 18:30"
    python chance_import.py chance.csv --dry-run     # print what would be stored

Chance turns away scripts (a bot check, HTTP 403), so its lines are scraped in
a browser with the webscraper.io extension and chance_sitemap.json, then read
in here. The CSV has one row per line of a market: home, away, when,
competition, the game's link (game-href, ending in Chance's match id), and for
one of total_ft, team_ft (with the team's name), total_h1 the under and over
selections ("Méně než 212.5", "Více než 212.5") and their prices.

Chance shows a game's time as "Dnes | 21:40", "Zítra | 0:10" or "24.11. | 19:00",
Prague time, and that time is when betting closes, ten minutes after tip-off.
"Dnes" and "Zítra" are read against the time of the scrape, which is also
stored as the lines' last_seen: advice.py stops tipping Chance lines a few
hours after it (MAX_AGE there), because they are only as fresh as the scrape.
"""

import argparse
import csv
import logging
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from bpoints import names

log = logging.getLogger("chance")

BOOKMAKER = "chance"
PRAGUE = ZoneInfo("Europe/Prague")
CLOSE_AFTER_TIPOFF = timedelta(minutes=10)
# Chance's competition -> ours; women's games say "Basketbal - ženy".
LEAGUES = [("usa - ncaa", "ženy", "ncaaw"), ("usa - ncaa", None, "ncaam"), ("wnba", None, "wnba"),
           ("nba", None, "nba"), ("euroliga", None, "euroleague"), ("eurocup", None, "eurocup"),
           ("španělská liga", None, "acb"), ("italská liga", None, "lba"),
           ("australská", None, "nbl")]
MARKETS = {"total_ft": ("total", "ft"), "team_ft": ("team_total", "ft"), "total_h1": ("total", "h1")}


def our_league(competition: str) -> str | None:
    c = " ".join(competition.lower().split())
    for key, gender, ours in LEAGUES:
        if gender is None and "žen" in c:
            continue
        if (key == "nba" and re.search(r"(^|\W)nba(\W|$)", c) and "wnba" not in c) or \
                (key != "nba" and key in c and (gender is None or gender in c)):
            return ours
    return None


def kickoff(when: str, scraped: datetime) -> datetime | None:
    """Tip-off from Chance's "Dnes | 21:40" (betting closes 10 minutes after it)."""
    w = " ".join(when.replace("|", " ").split()).lower()
    t = re.search(r"(\d{1,2}):(\d{2})", w)
    if not t:
        return None
    day = scraped.date()
    if "zítra" in w:
        day += timedelta(days=1)
    elif (d := re.search(r"(\d{1,2})\.\s*(\d{1,2})\.", w)):
        day = day.replace(month=int(d.group(2)), day=int(d.group(1)))
        if day < scraped.date() - timedelta(days=60):      # 03.01. read in December
            day = day.replace(year=day.year + 1)
    elif "dnes" not in w:
        return None
    closes = datetime(day.year, day.month, day.day, int(t.group(1)), int(t.group(2)), tzinfo=PRAGUE)
    return closes - CLOSE_AFTER_TIPOFF


def selection(name: str) -> tuple[str, float] | None:
    m = re.match(r"\s*(Méně|Více)\s+než\s+([\d.,]+)", name or "")
    if not m:
        return None
    return ("under" if m.group(1) == "Méně" else "over"), float(m.group(2).replace(",", "."))


def price(text: str) -> float | None:
    try:
        p = float((text or "").strip().replace(",", "."))
    except ValueError:
        return None
    return p if p > 1 else None


def match_id(url: str) -> str | None:
    """Chance's match id from a game's link (/kurzy/zapas/basketbal-x-y/8523606[/co-se-sazi])."""
    m = re.search(r"/kurzy/zapas/[^/]+/(\d+)", url or "")
    return m.group(1) if m else None


def market_lines(records, team1: str, team2: str, skipped: Counter | None = None) -> list[dict]:
    """One game's scraped rows (from the CSV or a webscraper.io job, the same
    columns) -> odds rows. team1 is the home team, as Chance names it."""
    out = {}
    for r in records:
        for col, (market, period) in MARKETS.items():
            if not r.get(f"{col}_under") and not r.get(f"{col}_over"):
                continue
            team = 0
            if market == "team_total":
                who = (r.get(f"{col}_team") or "").strip()
                s1, s2 = names.similarity(who, team1), names.similarity(who, team2)
                if max(s1, s2) < 0.75 or s1 == s2:
                    if skipped is not None:
                        skipped["team not known"] += 1
                    continue
                team = 1 if s1 > s2 else 2
            for side_col in ("under", "over"):
                sel, p = selection(r.get(f"{col}_{side_col}")), price(r.get(f"{col}_{side_col}_odds"))
                if not sel or not p:
                    continue
                side, line = sel
                out[(market, period, team, side, line)] = p
    return [{"market": k[0], "period": k[1], "team": k[2], "side": k[3], "line": k[4], "price": p}
            for k, p in out.items()]


def read(path: Path, scraped: datetime):
    """(event, odds rows) per game in the CSV."""
    events, records = {}, defaultdict(list)
    skipped = Counter()
    with path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            href = r.get("game-href") or ""
            ev = match_id(href)
            if not ev or not r.get("home") or not r.get("away"):
                skipped["no game"] += 1
                continue
            if ev not in events:
                ko = kickoff(r.get("when") or "", scraped)
                if ko is None:
                    skipped["no time"] += 1
                    continue
                comp = " ".join((r.get("competition") or "").split())
                events[ev] = {"event_id": ev, "kickoff": ko, "league": comp,
                              "our_league": our_league(comp), "team1": r["home"].strip(),
                              "team2": r["away"].strip(), "url": href}
            records[ev].append(r)
    out = [(e, market_lines(records[ev], e["team1"], e["team2"], skipped)) for ev, e in events.items()]
    for k, n in skipped.items():
        log.info("%d rows skipped: %s", n, k)
    yield from out


def main():
    ap = argparse.ArgumentParser(description="Chance's points lines from a webscraper.io CSV.")
    ap.add_argument("csv", type=Path)
    ap.add_argument("--scraped-at", help="Prague time of the scrape, e.g. '2026-11-22 18:30' "
                                         "(default: when the file was saved)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    load_dotenv()
    scraped = (datetime.fromisoformat(args.scraped_at).replace(tzinfo=PRAGUE) if args.scraped_at
               else datetime.fromtimestamp(args.csv.stat().st_mtime, PRAGUE))
    age = datetime.now(PRAGUE) - scraped
    if age > timedelta(hours=3):
        log.warning("scraped %s ago - advice.py will not tip lines this old", str(age).split(".")[0])
    games = [(e, rows) for e, rows in read(args.csv, scraped) if e["kickoff"] > datetime.now(PRAGUE)]
    by_league = Counter(e["our_league"] or "-" for e, _ in games)
    log.info("%d games still to start (%s), %d lines", len(games),
             ", ".join(f"{k} {v}" for k, v in by_league.most_common()), sum(len(r) for _, r in games))
    if args.dry_run:
        for e, rows in games:
            mk = Counter(f"{r['market']}/{r['period']}" for r in rows)
            print(f"{e['kickoff']:%d.%m %H:%M}  {e['team1']} - {e['team2']}  [{e['league']} -> "
                  f"{e['our_league']}]  {dict(mk)}")
        return 0
    store(games, scraped)
    return 0


def store(games, scraped: datetime):
    """Write (event, odds rows) pairs, the lines stamped with the scrape's time."""
    from bpoints import db
    conn = db.connect()
    try:
        for e, rows in games:
            if not rows:
                continue
            db.save_odds(conn, BOOKMAKER, e, rows)
            # The lines are as fresh as the scrape, not the import.
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


if __name__ == "__main__":
    sys.exit(main())
