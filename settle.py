"""Settle priced lines and tips against the results in bpoints.game.

    python settle.py

price.py pairs each Betano game with a game in the results feed before it
starts (bk_event.game_id, swapped). Once that game is final, every line on
it is settled: totals and team totals include overtime, first-half lines
count the first half only. A whole-number line that lands exactly is void
(the stake comes back). A game with no result five days after tip-off
(postponed, or missing from the feed) voids its tips.
"""

import logging
import sys

from dotenv import load_dotenv

from bpoints import db

log = logging.getLogger("settle")

ROWS = """
    SELECT {key}, x.market, x.period, x.team, x.side, x.line::float, e.swapped,
           g.home_pts, g.away_pts, g.home_h1, g.away_h1
      FROM bpoints.{table} x
      JOIN bpoints.bk_event e USING (bookmaker, event_id)
      JOIN bpoints.game g ON g.league = e.our_league AND g.game_id = e.game_id
     WHERE x.result IS NULL AND g.status = 'final' AND g.home_pts IS NOT NULL"""


def outcome(market, period, team, side, line, swapped, hp, ap, h1h, h1a):
    """(result, actual) for one line; bookmaker team 1 is the home team
    unless swapped."""
    if period == "h1":
        if h1h is None or h1a is None:
            return "void", None
        hp, ap = h1h, h1a
    p1, p2 = (ap, hp) if swapped else (hp, ap)
    actual = p1 + p2 if market == "total" else p1 if team == 1 else p2
    if actual == line:
        return "void", actual
    win = actual > line if side == "over" else actual < line
    return ("won" if win else "lost"), actual


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    load_dotenv()
    conn = db.connect()
    with conn.cursor() as cur:
        cur.execute(ROWS.format(key="x.bookmaker, x.event_id", table="priced"))
        done = []
        for bk, ev, market, period, team, side, line, sw, hp, ap, h1h, h1a in cur.fetchall():
            res, actual = outcome(market, period, team, side, line, sw, hp, ap, h1h, h1a)
            done.append((res, actual, bk, ev, market, period, team, side, line))
        cur.executemany("""UPDATE bpoints.priced SET result=%s, actual=%s, settled_at=now()
                            WHERE (bookmaker, event_id, market, period, team, side, line)
                                = (%s,%s,%s,%s,%s,%s,%s)""", done)
        log.info("%d lines settled", len(done))

        cur.execute(ROWS.format(key="x.bookmaker, x.event_id, x.slot", table="advice"))
        tips = []
        for bk, ev, slot, market, period, team, side, line, sw, hp, ap, h1h, h1a in cur.fetchall():
            res, actual = outcome(market, period, team, side, line, sw, hp, ap, h1h, h1a)
            tips.append((res, actual, bk, ev, slot))
        cur.executemany("""UPDATE bpoints.advice SET result=%s, actual=%s, settled_at=now()
                            WHERE (bookmaker, event_id, slot) = (%s,%s,%s)""", tips)
        cur.execute("""
            UPDATE bpoints.advice a SET result = 'void', settled_at = now()
              FROM bpoints.bk_event e
              LEFT JOIN bpoints.game g ON g.league = e.our_league AND g.game_id = e.game_id
             WHERE (e.bookmaker, e.event_id) = (a.bookmaker, a.event_id)
               AND a.result IS NULL AND a.kickoff < now() - interval '5 days'
               AND (g.game_id IS NULL OR g.status <> 'final')""")
        log.info("%d tips settled, %d voided for no result", len(tips), cur.rowcount)
    conn.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
