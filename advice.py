"""Write bpoints.advice - the basketball tips - from the model's prices
(bpoints.priced).

    python advice.py            # after price.py --store

The rules, as for the tennis and CS2 tips:

  - the expected points sit where Betano's own ladder puts them, moved only a
    little towards the model (price.py) - for the American leagues they sit on
    DraftKings' line instead, much the sharper market; the chance is then blended 3:1 with
    Betano's own for the line itself (margin taken out), so a tip needs the
    model AND some room against the market;
  - a tip needs EDGE (+6%) on that blended chance, odds between MIN_ODDS and
    MAX_ODDS, and a price on both sides of the line (to take the margin out);
  - at most one tip per game and period (whole game, first half): the line
    with the best blended edge among the total and both teams' points - a
    ladder's lines, and a total and its team totals, are one bet many times;
  - stake: quarter Kelly on the chance blended half and half with the
    bookmaker's, scaled down above odds of 3.0, 1-10 tenths of a unit, and the
    tips on one game share the largest single stake between them.

Until tip-off a tip is rewritten each run, and one no longer advised is kept
with backed = false (withdrawn). From tip-off the database refuses changes.
"""

import os
import subprocess
import sys
from collections import defaultdict

import psycopg
from dotenv import load_dotenv

EDGE = 0.06
MIN_ODDS, MAX_ODDS = 1.40, 3.50
BLEND = 0.75
PERIODS = ["ft", "h1"]
# Only where the centre is DraftKings' line: the European leagues have no sharper
# reference, and their lines are priced and settled (the admin page) but not tipped.
LEAGUES = ["nba"]           # ncaam, wnba, ncaaw once their spreads are calibrated


def code_version():
    try:
        return subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], capture_output=True,
                              text=True, check=True,
                              cwd=os.path.dirname(os.path.abspath(__file__))).stdout.strip() or None
    except Exception:
        return None


def stake(p: float, odds: float) -> int:
    p = (p + 1 / odds) / 2
    b = odds - 1
    kelly = (p * odds - 1) / b if b > 0 else 0.0
    quarter = max(kelly, 0.0) / 4 * min(1.0, 3.0 / odds)
    return max(1, min(10, round(quarter * 100)))


CANDIDATES = """
    SELECT p.bookmaker, p.event_id, e.kickoff, e.league, e.team1, e.team2,
           p.market, p.period, p.team, p.side, p.line::float, p.price::float,
           p.p_model::float, p.p_book::float
      FROM bpoints.priced p
      JOIN bpoints.bk_event e USING (bookmaker, event_id)
     WHERE e.kickoff > now() AND p.period = ANY(%s) AND e.our_league = ANY(%s)
       AND p.p_book IS NOT NULL AND p.centre_from = 'draftkings'"""


def main():
    load_dotenv()
    version = code_version()
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
        cur.execute(CANDIDATES, (PERIODS, LEAGUES))
        best, events = {}, {}
        for (bk, ev, kickoff, league, t1, t2, market, period, team, side, line, price,
             p_raw, p_book) in cur.fetchall():
            events[(bk, ev)] = (kickoff, league, t1, t2)
            if not (MIN_ODDS <= price <= MAX_ODDS):
                continue
            p = BLEND * p_raw + (1 - BLEND) * p_book
            edge = p * price - 1
            if edge < EDGE:
                continue
            # One tip per game and period: the total and the two teams' points
            # move together (a fast game lifts all three), so they are one bet.
            k = (bk, ev, period)
            if k not in best or edge > best[k]["edge"]:
                best[k] = {"market": market, "period": period, "team": team, "side": side,
                           "line": line, "odds": price, "p": p, "p_raw": p_raw, "edge": edge}

        by_match = defaultdict(list)
        for k, t in best.items():
            t["own"] = stake(t["p"], t["odds"])
            by_match[k[:2]].append(t)
        for tips in by_match.values():
            budget, want = max(t["own"] for t in tips), sum(t["own"] for t in tips)
            for t in tips:
                t["rating"] = max(1, round(t["own"] * budget / want))

        for (bk, ev, slot), t in best.items():
            kickoff, league, t1, t2 = events[(bk, ev)]
            cur.execute("""
                INSERT INTO bpoints.advice (bookmaker, event_id, slot, kickoff, league, team1, team2,
                                            market, period, team, side, line, odds, p_model, p_raw,
                                            edge, rating, backed, code_version)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,%s)
                ON CONFLICT (bookmaker, event_id, slot) DO UPDATE SET
                  kickoff=EXCLUDED.kickoff, market=EXCLUDED.market, period=EXCLUDED.period,
                  team=EXCLUDED.team, side=EXCLUDED.side, line=EXCLUDED.line, odds=EXCLUDED.odds,
                  p_model=EXCLUDED.p_model, p_raw=EXCLUDED.p_raw, edge=EXCLUDED.edge,
                  rating=EXCLUDED.rating, backed=true, code_version=EXCLUDED.code_version,
                  advised_at=now()
                WHERE bpoints.advice.kickoff > now() AND NOT bpoints.advice.placed""",
                        (bk, ev, slot, kickoff, league, t1, t2, t["market"], t["period"],
                         t["team"], t["side"], t["line"], t["odds"], t["p"], t["p_raw"],
                         t["edge"], t["rating"], version))

        keep = [f"{bk}|{ev}|{slot}" for bk, ev, slot in best]
        cur.execute("""
            UPDATE bpoints.advice SET backed = false, advised_at = now()
             WHERE backed AND kickoff > now() AND NOT placed
               AND NOT (bookmaker || '|' || event_id || '|' || slot = ANY(%s))""", (keep,))
        withdrawn = cur.rowcount
        cur.execute("""
            UPDATE bpoints.advice a SET kickoff = e.kickoff
              FROM bpoints.bk_event e
             WHERE (e.bookmaker, e.event_id) = (a.bookmaker, a.event_id)
               AND a.kickoff > now() AND a.kickoff <> e.kickoff""")
        moved = cur.rowcount
    print(f"{len(best)} tips on {len(by_match)} games, {withdrawn} withdrawn, {moved} start times moved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
