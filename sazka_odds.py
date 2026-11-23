"""Store SazkaBet's basketball points lines (bookmaker 'sazka').

    python sazka_odds.py --dry-run     # print what would be stored
    python sazka_odds.py               # store every points line of games still to start

One call for all our leagues (bpoints.sazka.LEAGUES): each game's total-points
ladder and its first-half total. Run with odds.py on the owner's PC, and on the
server every two hours (where it may be refused, as Betano refuses it).
"""

import argparse
import logging
import sys
from collections import Counter

from dotenv import load_dotenv

from bpoints import sazka

log = logging.getLogger("sazka")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--leagues", help="only these of ours, e.g. nba,ncaam")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    load_dotenv()
    try:
        games = sazka.games(args.leagues.split(",") if args.leagues else None)
    except sazka.Blocked as e:
        log.error("SazkaBet refused: %s", e)
        return 1
    by_league = Counter(e["our_league"] for e, _ in games)
    log.info("%d games to start (%s), %d lines", len(games),
             ", ".join(f"{k} {v}" for k, v in by_league.most_common()), sum(len(r) for _, r in games))
    if args.dry_run:
        for e, rows in games:
            mk = Counter(f"{r['market']}/{r['period']}" for r in rows)
            print(f"{e['kickoff']:%d.%m %H:%M}  {e['team1']} - {e['team2']}  [{e['league']}]  {dict(mk)}")
        return 0
    from bpoints import db
    conn = db.connect()
    for e, rows in games:
        if rows:
            db.save_odds(conn, "sazka", e, rows)
    log.info("stored")
    return 0


if __name__ == "__main__":
    sys.exit(main())
