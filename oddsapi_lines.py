"""The market's total and handicap for the European games, from The Odds API,
written to bpoints.game (close_total, close_spread, close_from = 'oddsapi') -
the slot DraftKings' line fills for the American leagues, so price.py centres
these games on it and advice.py may tip them.

    python oddsapi_lines.py --dry-run    # print what would be written
    python oddsapi_lines.py              # write it

A call costs 2 credits of a 500-a-month free plan, so it is made only when a
game of ours starts within --hours (36) and the last call is --min-gap-hours (4)
old; and never with fewer than --min-credits (40) left.
"""

import argparse
import logging
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from bpoints import db, names, oddsapi

log = logging.getLogger("oddsapi")
STAMP = Path(__file__).resolve().parent / "cache" / "oddsapi"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--hours", type=float, default=36)
    ap.add_argument("--min-gap-hours", type=float, default=4)
    ap.add_argument("--min-credits", type=int, default=40)
    ap.add_argument("--force", action="store_true", help="ignore the gap")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    load_dotenv()
    STAMP.mkdir(parents=True, exist_ok=True)
    conn = db.connect()
    for league in oddsapi.SPORTS:
        stamp = STAMP / f"{league}.last"
        try:
            last, left_before = stamp.read_text().split()
            last, left_before = float(last), int(left_before)
        except (OSError, ValueError):
            last, left_before = 0.0, None
        if not args.force and time.time() - last < args.min_gap_hours * 3600:
            log.info("%s: last call %.1f h ago - skipped", league, (time.time() - last) / 3600)
            continue
        if left_before is not None and left_before < args.min_credits:
            log.warning("%s: only %d credits left - skipped", league, left_before)
            continue
        with conn.cursor() as cur:
            cur.execute("""SELECT g.*, h.name hn, a.name an FROM bpoints.game g
                             LEFT JOIN bpoints.team h ON (h.league, h.team_id) = (g.league, g.home_id)
                             LEFT JOIN bpoints.team a ON (a.league, a.team_id) = (g.league, g.away_id)
                            WHERE g.league = %s AND g.tipoff BETWEEN now() AND now() + %s * interval '1 hour'""",
                        (league, args.hours))
            cols = [d.name for d in cur.description]
            soon = [dict(zip(cols, r)) for r in cur.fetchall()]
            if not soon:
                log.info("%s: no game in the next %g hours - no call", league, args.hours)
                continue
            cur.execute("""SELECT * FROM bpoints.game WHERE league = %s
                              AND tipoff BETWEEN now() - interval '1 day' AND now() + interval '10 days'""",
                        (league,))
            cols = [d.name for d in cur.description]
            games = [dict(zip(cols, r)) for r in cur.fetchall()]
            cur.execute("SELECT * FROM bpoints.team WHERE league = %s", (league,))
            cols = [d.name for d in cur.description]
            teams = {r[cols.index("team_id")]: dict(zip(cols, r)) for r in cur.fetchall()}
        try:
            lines, left = oddsapi.games(league)
        except oddsapi.OutOfCredits as e:
            log.error("%s", e)
            return 1
        if not args.dry_run:
            stamp.write_text(f"{time.time():.0f} {left if left is not None else -1}")
        log.info("%s: %d games from The Odds API, %s credits left", league, len(lines), left)
        written = 0
        for ln in lines:
            hit = names.pair(ln, games, teams)
            if not hit:
                log.info("  no game for %s - %s (%s)", ln["team1"], ln["team2"], ln["kickoff"].strftime("%d.%m %H:%M"))
                continue
            g, swapped = hit
            if ln["total"] is None:
                continue
            # Its handicap is for its home team: ours if not swapped.
            spread = None if ln["spread"] is None else (-ln["spread"] if swapped else ln["spread"])
            print(f"  {g['tipoff']:%d.%m %H:%M} {g['home_name']} - {g['away_name']}: total {ln['total']}, "
                  f"home {spread}  [{ln['from']}; {', '.join(ln['books'])}]")
            if args.dry_run or g["status"] == "final":
                continue
            with conn.cursor() as cur:
                cur.execute("""UPDATE bpoints.game SET close_total = %s, close_spread = %s,
                                      close_from = 'oddsapi'
                                WHERE (league, game_id) = (%s, %s) AND tipoff > now()""",
                            (ln["total"], spread, league, g["game_id"]))
                written += cur.rowcount
        conn.commit()
        log.info("%s: %d games' lines written", league, written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
