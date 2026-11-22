"""Basketball results (and scheduled games) into bpoints.game.

    python collect.py                         # every league: the last 5 days and the next 7
    python collect.py --history               # every league since HISTORY (hours the first time)
    python collect.py --leagues nba,ncaam --start 2025-01-01 --end 2025-03-01

ESPN: NBA, WNBA, NCAA men and women. Euroleague's API: EuroLeague and EuroCup
(always read a whole season at a time - one request). ACB's match pages: Liga
Endesa, one page per game (finished ones are cached, so only the first run is long).
"""

import argparse
import logging
import sys
from datetime import date, timedelta

from dotenv import load_dotenv

from bpoints import acb, db, espn, euroleague

log = logging.getLogger("collect")

HISTORY = {"nba": date(2023, 10, 1), "wnba": date(2024, 5, 1), "ncaam": date(2024, 11, 1),
           "ncaaw": date(2024, 11, 1), "euroleague": 2022, "eurocup": 2022,
           "acb": 102800, "nbl": date(2023, 9, 1)}   # for acb: the first match id read (2023-24 onwards), not a year
# Days with no games in a league are skipped by the scoreboard anyway, but the
# off-season is a few hundred requests saved.
SEASON_MONTHS = {"nba": {10, 11, 12, 1, 2, 3, 4, 5, 6}, "wnba": {5, 6, 7, 8, 9, 10},
                 "ncaam": {11, 12, 1, 2, 3, 4}, "ncaaw": {11, 12, 1, 2, 3, 4},
                 "nbl": {9, 10, 11, 12, 1, 2, 3}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--leagues", default=",".join(HISTORY))
    ap.add_argument("--history", action="store_true")
    ap.add_argument("--start", type=date.fromisoformat)
    ap.add_argument("--end", type=date.fromisoformat)
    ap.add_argument("--lines", action="store_true",
                    help="fill closing totals missing from the summaries (ESPN leagues)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    load_dotenv()
    conn = db.connect()
    today = date.today()
    if args.lines:
        # (the NBL has no DraftKings line to find)
        return backfill_lines(conn, [lg for lg in args.leagues.split(",")
                                     if lg in espn.LEAGUES and lg != "nbl"])
    failed = []
    for league in args.leagues.split(","):
        try:
            collect_league(conn, league, args, today)
        except Exception as e:            # one feed down must not stop the others
            conn.rollback()
            log.error("%s: %s - skipped this run", league, e)
            failed.append(league)
    return 1 if failed else 0


def collect_league(conn, league, args, today):
    if league == "acb":
        first, last = acb.current_block()
        if args.history:
            first = HISTORY[league]
        out, pending = [], []

        def save():
            # A fresh connection each time: the one opened at the start is
            # dropped by the server during the long scrape.
            c = db.connect()
            try:
                db.save_games(c, [x for x, _ in pending], [t for _, tt in pending for t in tt])
            finally:
                c.close()
            pending.clear()

        for g, ts in acb.games(first, last):
            out.append((g, ts))
            pending.append((g, ts))
            if len(pending) >= 100:          # a long first run is saved as it goes
                save()
        if pending:
            save()
        log.info("acb ids %d-%d: %d games, %d final", first, last, len(out),
                 sum(g["status"] == "final" for g, _ in out))
        return
    if league in euroleague.LEAGUES:
        cur = euroleague.current_season()
        first = HISTORY[league] if args.history else cur
        if args.start:
            first = args.start.year if args.start.month >= 8 else args.start.year - 1
        for year in range(first, cur + 1):
            out = list(euroleague.games(league, year))
            db.save_games(conn, [g for g, _ in out], [t for _, ts in out for t in ts])
            log.info("%s %d-%02d: %d games, %d final", league, year, (year + 1) % 100,
                     len(out), sum(g["status"] == "final" for g, _ in out))
        return
    start = args.start or (HISTORY[league] if args.history else today - timedelta(days=5))
    end = args.end or today + timedelta(days=7)
    day = start
    while day <= end:
        chunk_end = min(end, day + timedelta(days=13))
        if any(d.month in SEASON_MONTHS[league]
               for d in (day, chunk_end, day + (chunk_end - day) / 2)):
            games, teams = [], []
            for g, ts in espn.games(league, day, chunk_end):
                games.append(g)
                teams += ts
            db.save_games(conn, games, teams)
            log.info("%s %s..%s: %d games, %d final, %d with a closing total", league, day,
                     chunk_end, len(games), sum(g["status"] == "final" for g in games),
                     sum(g.get("close_total") is not None for g in games))
        day = chunk_end + timedelta(days=1)


def backfill_lines(conn, leagues):
    from concurrent.futures import ThreadPoolExecutor
    for league in leagues:
        with conn.cursor() as cur:
            cur.execute("""SELECT game_id FROM bpoints.game WHERE league = %s AND status = 'final'
                              AND close_total IS NULL ORDER BY tipoff""", (league,))
            ids = [r[0] for r in cur.fetchall()]
        log.info("%s: %d finished games without a closing total", league, len(ids))
        for i in range(0, len(ids), 300):
            chunk = ids[i:i + 300]
            with ThreadPoolExecutor(6) as pool:
                lines = list(pool.map(lambda g: espn.closing_line(league, g), chunk))
            with conn.cursor() as cur:
                cur.executemany("""UPDATE bpoints.game SET close_total = %s, close_spread = %s
                                    WHERE league = %s AND game_id = %s""",
                                [(ln[0], ln[1], league, g) for g, ln in zip(chunk, lines) if ln])
            conn.commit()
            log.info("%s: %d/%d, %d lines found in this chunk", league, i + len(chunk), len(ids),
                     sum(ln is not None for ln in lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
