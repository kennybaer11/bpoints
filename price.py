"""The model's price on every stored Betano points line.

    python price.py              # print the best-rated lines of games still to start
    python price.py --store      # also write bpoints.priced (and pair games with results)
    python price.py --top 0      # print nothing

For each game still to start:

  1. pair Betano's game with the league's scheduled game (bpoints.names);
  2. the model's expected regulation points for each team (bpoints.model,
     replayed over every finished game of the league);
  3. Betano's own centre for each ladder - the mean of a Normal with the
     model's spread that fits Betano's no-vig chances best;
  4. the expected points the lines are priced on: Betano's centre moved
     towards the model's by the league's weight W (from backtest.py: how much
     of the model's disagreement with a closing line turns out right), and
     overtime added as a mixture for full-game lines;
  5. P(over) and P(under) at each line, and the edge at Betano's price.

So the market sets where the distribution sits, the model nudges it, and the
model alone sets its shape - how likely the lines away from the centre are.
"""

import argparse
import logging
import os
import sys
from collections import defaultdict
from datetime import timedelta

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

from bpoints import db, names
from bpoints.model import State, implied_centre, p_over, p_overtime, spreads

log = logging.getLogger("price")

# How far the expected points move from Betano's centre towards the model's.
# 0 = Betano's centre, 1 = the model's. From backtest.py's slope of
# (total - close) on (model - close) against DraftKings' closing totals, a
# little under it for safety; leagues without closing lines get a small one.
W = {"nba": 0.0, "wnba": 0.0, "ncaam": 0.10, "ncaaw": 0.25, "euroleague": 0.15,
     "eurocup": 0.15, "acb": 0.15, "nbl": 0.15, "lba": 0.15}
# A first half's spread as a share of the whole game's: NBA 11.9 / 17.8 around
# the close (calibrate.py), a little under the sqrt(1/2) that halving the
# scoring alone would give - the end of a game adds variance (fouling, garbage time).
H1_SD = {"nba": 0.67, "ncaam": 0.60, "wnba": 0.60}
# One team's first half spreads relatively wider than the total's - the two
# teams' halves partly cancel in the total. Measured the same way against
# DraftKings' close (team expected = (total -+ spread) / 2): NCAA 0.678 over
# 12,446 games, NBA 0.721 over 4,493, WNBA 0.668 over 359. Pricing team halves
# with the total's ratio made the model 7-12% too sure of them, so their edges
# were overstated (NCAA team-half tips: 12 won from 24 against 14.7 expected).
H1_SD_TEAM = {"nba": 0.72, "ncaam": 0.68, "wnba": 0.67}
H1_SHARE = {"nba": 0.505, "wnba": 0.50, "ncaam": 0.475, "ncaaw": 0.49, "euroleague": 0.50,
            "eurocup": 0.50, "acb": 0.484, "nbl": 0.511, "lba": 0.49}   # acb, nbl: measured, 2023-26


def replay(cur, league: str) -> State:
    cur.execute("""SELECT season, home_id, away_id, neutral,
                          coalesce(home_reg, home_pts) AS h, coalesce(away_reg, away_pts) AS a
                     FROM bpoints.game
                    WHERE league = %s AND status = 'final' AND home_pts IS NOT NULL
                    ORDER BY tipoff, game_id""", (league,))
    st = State.new(league)
    for g in cur.fetchall():
        st.new_season(g["season"])
        st.update(g["home_id"], g["away_id"], g["neutral"], g["h"], g["a"])
    return st


def h1_share(cur, league: str) -> float:
    # Of the full-game points, overtime included: it is applied to the full-game centre.
    # Measured on this month and the one before, in the last three seasons: the
    # share moves through a season. NCAA first halves score 47.4% in November and
    # December and 46.8% from January, every season; one figure for the whole
    # year (47.0%) put November's first-half centre 0.6 points low, and the
    # settled lines showed it (actual minus the model's centre +1.15, Betano's
    # own +0.53, over 109 games), with the unders it tipped going 12 from 24.
    cur.execute("""SELECT sum(home_h1 + away_h1)::float / nullif(sum(home_pts + away_pts), 0) AS s,
                          count(*) AS n
                     FROM bpoints.game WHERE league = %s AND status = 'final' AND home_h1 IS NOT NULL
                      AND tipoff > now() - interval '3 years'
                      AND extract(month FROM tipoff AT TIME ZONE 'America/New_York') IN
                          (extract(month FROM now()), extract(month FROM now() - interval '1 month'))""",
                (league,))
    r = cur.fetchone()
    if r and r["n"] and r["n"] > 200:
        return r["s"]
    # Too few games in these months (a league's first weeks): the last two years.
    cur.execute("""SELECT sum(home_h1 + away_h1)::float / nullif(sum(home_pts + away_pts), 0) AS s,
                          count(*) AS n
                     FROM bpoints.game WHERE league = %s AND status = 'final' AND home_h1 IS NOT NULL
                      AND tipoff > now() - interval '2 years'""", (league,))
    r = cur.fetchone()
    return r["s"] if r and r["n"] and r["n"] > 200 else H1_SHARE[league]


def pair_events(cur, league: str, events: list[dict]):
    """Pair each unpaired event with a game; returns event_id -> (game, swapped)."""
    cur.execute("""SELECT * FROM bpoints.game WHERE league = %s
                      AND tipoff BETWEEN now() - interval '3 days' AND now() + interval '14 days'""",
                (league,))
    games = cur.fetchall()
    by_id = {g["game_id"]: g for g in games}
    cur.execute("SELECT * FROM bpoints.team WHERE league = %s", (league,))
    teams = {t["team_id"]: t for t in cur.fetchall()}
    out = {}
    for e in events:
        if e["game_id"] and e["game_id"] in by_id:
            out[e["event_id"]] = (by_id[e["game_id"]], e["swapped"])
            continue
        hit = names.pair(e, games, teams)
        if hit:
            out[e["event_id"]] = hit
        else:
            log.info("no game for %s - %s (%s, %s)", e["team1"], e["team2"], e["league"],
                     e["kickoff"].strftime("%d.%m %H:%M"))
    return out


def reference(game: dict, swapped: bool) -> dict | None:
    """Full-game expected points from DraftKings' line (ESPN): the total, and
    each team's share of it from the spread - home = (total - home line) / 2.
    Keyed in the bookmaker's order (0 = total, 1, 2 = teams)."""
    t, s = game.get("close_total"), game.get("close_spread")
    if t is None or s is None:
        return None
    t, s = float(t), float(s)
    lo, hi = PLAUSIBLE[game["league"]]
    if not lo <= t <= hi or abs(s) > 0.4 * t:
        return None
    home, away = (t - s) / 2, (t + s) / 2
    return {0: t, 1: away if swapped else home, 2: home if swapped else away}


# Totals outside these are typing errors in the feed (an NBA close of 73.5).
PLAUSIBLE = {"nba": (180, 280), "wnba": (130, 200), "ncaam": (100, 190), "ncaaw": (90, 180),
             "euroleague": (130, 210), "eurocup": (130, 220), "acb": (130, 210), "nbl": (140, 240), "lba": (130, 210)}


def price_event(st: State, share: float, game: dict, swapped: bool, odds: list[dict]) -> list[dict]:
    """Every line of one game. Full-game points are priced as one Normal
    around the expected points with overtime in them (the spread calibrated
    on full-game results, overtime included); the first half on its share."""
    p = st.p
    mh, ma = st.means(game["home_id"], game["away_id"], game["neutral"])
    sd = spreads(p, mh, ma)
    ot = p_overtime(mh - ma, sd["margin"]) * p.ot_points
    model = {0: mh + ma + ot, 1: (ma if swapped else mh) + ot / 2, 2: (mh if swapped else ma) + ot / 2}
    spread = {0: sd["total"], 1: sd["away" if swapped else "home"], 2: sd["home" if swapped else "away"]}
    ref = reference(game, swapped)
    groups = defaultdict(dict)
    for o in odds:
        groups[(o["market"], o["period"], o["team"])].setdefault(float(o["line"]), {})[o["side"]] = float(o["price"])
    out = []
    for (market, period, team), ladder in groups.items():
        k = 0 if market == "total" else team
        mu_model, s, mu_ref = model[k], spread[k], ref[k] if ref else None
        if period == "h1":
            ratio = (H1_SD_TEAM.get(st.league, 0.75) if k else H1_SD.get(st.league, 0.70))
            mu_model, s = mu_model * share, s * ratio
            mu_ref = mu_ref * share if mu_ref is not None else None
        no_vig = [(ln, (1 / pr["over"]) / (1 / pr["over"] + 1 / pr["under"]))
                  for ln, pr in ladder.items() if "over" in pr and "under" in pr]
        mu_book = implied_centre(no_vig, s)
        base, source = ((mu_ref, "draftkings") if mu_ref is not None else
                        (mu_book, "betano") if mu_book is not None else (mu_model, "model"))
        mu = base + W[st.league] * (mu_model - base)
        q_book = dict(no_vig)
        for ln, sides in ladder.items():
            po = p_over(ln, mu, s)
            q = q_book.get(ln)
            for side, price in sides.items():
                pm = po if side == "over" else 1 - po
                out.append({"market": market, "period": period, "team": team, "side": side,
                            "line": ln, "price": price,
                            "other_price": sides.get("under" if side == "over" else "over"),
                            "p_model": pm, "p_book": None if q is None else (q if side == "over" else 1 - q),
                            "edge": pm * price - 1, "mu": mu, "sd": s, "mu_model": mu_model,
                            "mu_book": mu_book, "mu_ref": mu_ref, "centre_from": source})
    return out


COLS = ["market", "period", "team", "side", "line", "price", "other_price", "p_model", "p_book",
        "edge", "mu", "sd", "mu_model", "mu_book", "mu_ref", "centre_from"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", action="store_true")
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    load_dotenv()
    shown = []
    with db.open_conn(row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("""SELECT * FROM bpoints.bk_event WHERE kickoff > now() AND our_league IS NOT NULL
                        ORDER BY our_league, kickoff""")
        events = cur.fetchall()
        by_league = defaultdict(list)
        for e in events:
            by_league[e["our_league"]].append(e)
        n_lines = 0
        for league, evs in by_league.items():
            st = replay(cur, league)
            share = h1_share(cur, league)
            pairs = pair_events(cur, league, evs)
            log.info("%s: %d Betano games, %d paired", league, len(evs), len(pairs))
            for e in evs:
                if e["event_id"] not in pairs:
                    continue
                game, swapped = pairs[e["event_id"]]
                # Only the lines Betano showed at its latest look at this game:
                # save_odds stamps the event and its lines with the same now(),
                # so a line it has since moved off (58.5 -> 60.5) is older.
                cur.execute("""SELECT market, period, team, side, line, price FROM bpoints.odds
                                WHERE (bookmaker, event_id) = (%s, %s) AND last_seen >= %s""",
                            (e["bookmaker"], e["event_id"], e["last_seen"]))
                rows = price_event(st, share, game, swapped, cur.fetchall())
                n_lines += len(rows)
                for r in rows:
                    shown.append((r["edge"], e, r))
                if args.store:
                    cur.execute("""UPDATE bpoints.bk_event SET game_id = %s, swapped = %s
                                    WHERE (bookmaker, event_id) = (%s, %s)""",
                                (game["game_id"], swapped, e["bookmaker"], e["event_id"]))
                    # The game is still to start: its prices are rewritten, and
                    # a line no longer offered must not stay to be tipped.
                    cur.execute("DELETE FROM bpoints.priced WHERE (bookmaker, event_id) = (%s, %s)",
                                (e["bookmaker"], e["event_id"]))
                    cur.executemany(
                        f"""INSERT INTO bpoints.priced (bookmaker, event_id, {', '.join(COLS)})
                            VALUES (%s, %s, {', '.join(['%s'] * len(COLS))})
                            ON CONFLICT (bookmaker, event_id, market, period, team, side, line)
                            DO UPDATE SET {', '.join(f'{c}=EXCLUDED.{c}' for c in COLS[5:])},
                                          priced_at = now()""",
                        [[e["bookmaker"], e["event_id"]] + [r[c] for c in COLS] for r in rows])
        if args.store:
            conn.commit()
    log.info("%d lines priced%s", n_lines, " and stored" if args.store else "")
    shown.sort(key=lambda x: -x[0])
    for edge, e, r in shown[:args.top]:
        who = {1: e["team1"], 2: e["team2"]}.get(r["team"], "")
        print(f"{e['kickoff']:%d.%m %H:%M} {e['team1'][:22]:22s} - {e['team2'][:22]:22s} "
              f"{r['market']:10s} {r['period']} {who[:14]:14s} {r['side']:5s} {r['line']:6.1f} "
              f"@ {r['price']:.2f}  p {r['p_model']:.3f} (book {r['p_book'] or 0:.3f})  "
              f"edge {edge:+.1%}  mu {r["mu"]:.1f} [{r["centre_from"]}] (model {r["mu_model"]:.1f}, betano {r["mu_book"] or 0:.1f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
