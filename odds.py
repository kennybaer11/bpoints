"""Store Betano's basketball points lines.

    python odds.py --dry-run     # print what would be stored
    python odds.py               # store every points line of games still to start

Only the leagues the model covers (bpoints.betano.LEAGUES). Lines stop
changing at tip-off: a started game is no longer listed, so its last stored
price is the closing one.
"""

import argparse
import logging
import sys
from collections import Counter

from dotenv import load_dotenv

from bpoints import betano

log = logging.getLogger("odds")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--leagues", help="only these of ours, e.g. nba,euroleague")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    load_dotenv()
    conn = None
    if not args.dry_run:
        from bpoints import db
        conn = db.connect()
    want = set(args.leagues.split(",")) if args.leagues else None
    try:
        lgs = [lg for lg in betano.leagues() if not want or lg["ours"] in want]
        events = [e for lg in lgs for e in betano.league_events(lg)]
    except betano.Blocked as e:
        log.error("Betano refused: %s", e)
        return 1
    log.info("%d games to start in %d leagues", len(events), len(lgs))
    for e in events:
        try:
            rows = betano.event_odds(e)
        except betano.Blocked as err:
            log.error("Betano refused: %s - stopping", err)
            return 1
        kinds = Counter(f"{r['market']}/{r['period']}" for r in rows)
        log.info("%s  %s - %s  [%s]  %s", e["kickoff"].strftime("%d.%m %H:%M"), e["team1"],
                 e["team2"], e["league"], dict(kinds) or "no points lines")
        if conn and rows:
            db.save_odds(conn, "betano", e, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
