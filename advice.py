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
    with the best blended edge among the total and both teams' points, at
    whichever bookmaker (Betano, Chance, SazkaBet, Fortuna) has it - a ladder's lines, a total
    and its team totals, and the same line at two bookmakers are one bet;
  - stake: quarter Kelly on the chance blended half and half with the
    bookmaker's, scaled down above odds of 3.0, 1-10 tenths of a unit, and the
    tips on one game share the largest single stake between them.

Until tip-off a tip is rewritten each run, and one no longer advised is kept
with backed = false (withdrawn). From tip-off the database refuses changes.
"""

import json
import os
import subprocess
import sys
from collections import defaultdict

import psycopg
from dotenv import load_dotenv

from bpoints import db

EDGE = 0.06
MIN_ODDS, MAX_ODDS = 1.40, 3.50
BLEND = 0.75
PERIODS = ["ft", "h1"]
# No full-game tips in these leagues (from 27 Nov 2026). For the NCAA, DraftKings'
# line is no sharper than Betano's - over 204 full games its close missed the
# total by 13.45 points on average, Betano's own centre by 13.40 - and the model
# scored worse than Betano's no-vig chances on its full-game lines (~550 games),
# so an NCAA full-game "edge" is noise. Its full-game tips: 4 won of 13.
# NBA full games are where the model does beat Betano (64-86 games), so their
# overs are back (off 26 Nov 2026 on 1 won of 6).
NO_FT_LEAGUES = ["ncaam"]
# Only where the centre is a sharp line - DraftKings' (ESPN), or for the EuroLeague
# Pinnacle's or the European books' median (The Odds API). Lines centred on the
# bookmaker's own ladder are priced and settled (the admin page) but not tipped.
LEAGUES = ["nba", "ncaam", "wnba",   # ncaaw once its spread is calibrated
           "euroleague"]             # centred on The Odds API's line (oddsapi_lines.py)
SHARP_CENTRES = ["draftkings", "oddsapi"]


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


# Lines from a scrape (chance_pipeline.py twice a day, or chance_import.py) are only as
# fresh as that scrape: older than this, they are not tipped (and a tip on them
# is withdrawn). Betano's are read on every run, so they have no limit here.
MAX_AGE = {"chance": "6 hours",   # scraped at 15:00 and 21:00: covers each evening
           "fortuna": "6 hours",  # the same (fortuna_pipeline.py)
           "sazka": "3 hours"}    # read every run; older means the reads stopped

CANDIDATES = """
    SELECT p.bookmaker, p.event_id, e.kickoff, e.league, e.team1, e.team2,
           coalesce(e.our_league || ':' || e.game_id, p.bookmaker || ':' || p.event_id),
           p.market, p.period, p.team, p.side, p.line::float, p.price::float,
           p.p_model::float, p.p_book::float
      FROM bpoints.priced p
      JOIN bpoints.bk_event e USING (bookmaker, event_id)
     WHERE e.kickoff > now() AND p.period = ANY(%s) AND e.our_league = ANY(%s)
       AND p.p_book IS NOT NULL AND p.centre_from = ANY(%s)
       AND NOT EXISTS (SELECT 1 FROM jsonb_each_text(%s::jsonb) a
                        WHERE a.key = e.bookmaker AND e.last_seen < now() - a.value::interval)"""


def main():
    load_dotenv()
    version = code_version()
    with db.open_conn() as conn, conn.cursor() as cur:
        # A placed tip is locked, and so is one Telegram has announced: the
        # announcement is the bet, sent once and never revised (40% of the
        # first 158 announced were later withdrawn as prices moved, which no
        # one could follow). Its game and period are taken: another
        # bookmaker's line there would be the same bet a second time.
        cur.execute("""SELECT coalesce(e.our_league || ':' || e.game_id, a.bookmaker || ':' || a.event_id),
                              a.period
                         FROM bpoints.advice a JOIN bpoints.bk_event e USING (bookmaker, event_id)
                        WHERE (a.placed OR a.told_at IS NOT NULL) AND a.backed AND a.kickoff > now()""")
        placed = set(cur.fetchall())
        cur.execute(CANDIDATES, (PERIODS, LEAGUES, SHARP_CENTRES, json.dumps(MAX_AGE)))
        best, events = {}, {}
        for (bk, ev, kickoff, league, t1, t2, game, market, period, team, side, line, price,
             p_raw, p_book) in cur.fetchall():
            events[(bk, ev)] = (kickoff, league, t1, t2)
            if not (MIN_ODDS <= price <= MAX_ODDS):
                continue
            if period == "ft" and game.split(":")[0] in NO_FT_LEAGUES:
                continue
            p = BLEND * p_raw + (1 - BLEND) * p_book
            edge = p * price - 1
            if edge < EDGE:
                continue
            # One tip per game and period: the total and the two teams' points
            # move together (a fast game lifts all three), so they are one bet -
            # and the same bet at another bookmaker is still the same bet, so the
            # game is the results feed's (Betano and Chance both pair to it).
            k = (game, period)
            if k in placed:
                continue
            if k not in best or edge > best[k]["edge"]:
                best[k] = {"bk": bk, "ev": ev, "market": market, "period": period, "team": team,
                           "side": side, "line": line, "odds": price, "p": p, "p_raw": p_raw,
                           "edge": edge}

        by_match = defaultdict(list)
        for k, t in best.items():
            t["own"] = stake(t["p"], t["odds"])
            by_match[k[0]].append(t)
        for tips in by_match.values():
            budget, want = max(t["own"] for t in tips), sum(t["own"] for t in tips)
            for t in tips:
                t["rating"] = max(1, round(t["own"] * budget / want))

        for t in best.values():
            bk, ev, slot = t["bk"], t["ev"], t["period"]
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
                WHERE bpoints.advice.kickoff > now() AND NOT bpoints.advice.placed
                  AND bpoints.advice.told_at IS NULL""",
                        (bk, ev, slot, kickoff, league, t1, t2, t["market"], t["period"],
                         t["team"], t["side"], t["line"], t["odds"], t["p"], t["p_raw"],
                         t["edge"], t["rating"], version))

        keep = [f"{t['bk']}|{t['ev']}|{t['period']}" for t in best.values()]
        cur.execute("""
            UPDATE bpoints.advice SET backed = false, advised_at = now()
             WHERE backed AND kickoff > now() AND NOT placed AND told_at IS NULL
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
