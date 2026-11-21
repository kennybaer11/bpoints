"""Walk-forward test of the points model against DraftKings' closing totals.

    python backtest.py                         # every league with closing lines
    python backtest.py --leagues nba --start 2025-10-01
    python backtest.py --tune nba              # grid over k and carry, fit before --start
    python backtest.py --csv bt_nba.csv        # one row per game, for a closer look

Each game is predicted from the games before it only. Reported per league:
how far the model and the closing line miss the real total, whether the
model's disagreement with the close predicts which side of it the total
lands (the slope - 0 means the close already knows everything the model
does), and what betting the side the model prefers would have won at
-110 (1.91), by how far apart they were.
"""

import argparse
import itertools
import logging
import math
import os
import sys
from dataclasses import replace
from datetime import date

import numpy as np
import pandas as pd
import psycopg
from dotenv import load_dotenv

from bpoints.model import LEAGUE_PARAMS, State, p_overtime, spreads

log = logging.getLogger("backtest")
PRICE = 1.909


def load(league: str) -> pd.DataFrame:
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        df = pd.read_sql("""SELECT * FROM bpoints.game WHERE league = %s AND status = 'final'
                              AND home_pts IS NOT NULL ORDER BY tipoff, game_id""",
                         conn, params=(league,))
    for c in ("close_total", "close_spread", "home_poss", "away_poss"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def walk(df: pd.DataFrame, league: str, params=None) -> pd.DataFrame:
    """Pre-game predictions for every game, in order."""
    st = State.new(league, params)
    p = st.p
    rows = []
    for g in df.itertuples(index=False):
        st.new_season(g.season)
        mh, ma = st.means(g.home_id, g.away_id, bool(g.neutral))
        sd = spreads(p, mh, ma)
        pot = p_overtime(mh - ma, sd["margin"])
        rows.append((mh, ma, mh + ma + pot * p.ot_points, sd["total"], pot,
                     min(st.n.get(g.home_id, 0), st.n.get(g.away_id, 0))))
        rh = g.home_reg if not pd.isna(g.home_reg) else g.home_pts
        ra = g.away_reg if not pd.isna(g.away_reg) else g.away_pts
        st.update(g.home_id, g.away_id, bool(g.neutral), int(rh), int(ra))
    out = df.copy()
    out[["mu_h", "mu_a", "mu", "sd", "p_ot", "n_min"]] = rows
    out["total"] = out.home_pts + out.away_pts
    return out


def report(b: pd.DataFrame, league: str, start: date, min_games: int):
    t = b[(b.tipoff.dt.date >= start) & (b.n_min >= min_games)]
    c = t.dropna(subset=["close_total"])
    print(f"\n== {league}: {len(t)} games from {start}, {len(c)} with a closing total")
    if t.empty:
        return
    print(f"   model  MAE {np.mean(np.abs(t.total - t.mu)):.2f}  RMSE {np.sqrt(np.mean((t.total - t.mu) ** 2)):.2f}"
          f"  bias {np.mean(t.total - t.mu):+.2f}  predicted sd {t.sd.mean():.1f}")
    if len(c) < 50:
        return
    r_close = c.total - c.close_total
    r_model = c.total - c.mu
    print(f"   on games with a close: model RMSE {np.sqrt(np.mean(r_model ** 2)):.2f},"
          f" close RMSE {np.sqrt(np.mean(r_close ** 2)):.2f}, sd(total - close) {r_close.std():.2f}")
    d = c.mu - c.close_total
    slope, icpt = np.polyfit(d, r_close, 1)
    se = np.sqrt(np.sum((r_close - (slope * d + icpt)) ** 2) / (len(c) - 2) / np.sum((d - d.mean()) ** 2))
    print(f"   (total - close) = {icpt:+.2f} + {slope:.3f} x (model - close)   slope se {se:.3f}"
          f"   sd(model - close) {d.std():.2f}")
    print("   bet the model's side at 1.91:")
    for lo in (0, 2, 4, 6, 8, 10):
        sel = c[np.abs(d) >= lo]
        dd = (sel.mu - sel.close_total)
        res = sel.total - sel.close_total
        push = res == 0
        win = ((dd > 0) & (res > 0)) | ((dd < 0) & (res < 0))
        n = int((~push).sum())
        if n < 20:
            break
        w = int(win.sum())
        profit = w * (PRICE - 1) - (n - w)
        print(f"     |model - close| >= {lo:2d}: {n:5d} bets  won {w / n:.1%}  ROI {profit / n:+.1%}")
    # Calibration of the spread around the close: how often the total lands
    # beyond close +/- x, against a Normal with the model's sd.
    print("   tails around the close (share of games, real v Normal(close, model sd)):")
    for x in (10, 20, 30):
        real = np.mean(np.abs(r_close) > x)
        nrm = np.mean(2 * (1 - __import__("scipy.stats").stats.norm.cdf(x, 0, c.sd)))
        print(f"     |total - close| > {x}: {real:.1%} v {nrm:.1%}")


def tune(df, league, start):
    base = LEAGUE_PARAMS[league]
    best = None
    for k, carry, ke in itertools.product((0.02, 0.03, 0.04, 0.05, 0.06), (0.4, 0.55, 0.7, 0.85),
                                          (1.0, 3.0, 6.0)):
        b = walk(df, league, replace(base, k=k, carry=carry, k_early=ke))
        fit = b[(b.tipoff.dt.date < start) & (b.season > b.season.min())]
        mse = float(np.mean((fit.total - fit.mu) ** 2))
        print(f"   k {k:.3f} carry {carry:.2f} k_early {ke:.0f}: RMSE {math.sqrt(mse):.3f}")
        if best is None or mse < best[0]:
            best = (mse, k, carry, ke)
    print(f"best: k {best[1]} carry {best[2]} k_early {best[3]}, RMSE {math.sqrt(best[0]):.3f}")
    return replace(base, k=best[1], carry=best[2], k_early=best[3])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--leagues", default="nba,ncaam,wnba,ncaaw,euroleague,eurocup")
    ap.add_argument("--start", type=date.fromisoformat, default=date(2025, 7, 1))
    ap.add_argument("--min-games", type=int, default=5)
    ap.add_argument("--tune")
    ap.add_argument("--csv")
    args = ap.parse_args()
    load_dotenv()
    if args.tune:
        df = load(args.tune)
        p = tune(df, args.tune, args.start)
        report(walk(df, args.tune, p), args.tune, args.start, args.min_games)
        return 0
    frames = []
    for league in args.leagues.split(","):
        df = load(league)
        if df.empty:
            print(f"\n== {league}: no games")
            continue
        b = walk(df, league)
        report(b, league, args.start, args.min_games)
        frames.append(b)
    if args.csv and frames:
        pd.concat(frames).to_csv(args.csv, index=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
