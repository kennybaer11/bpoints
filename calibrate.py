"""How widely real scores spread around the closing line - the shape the
lines away from the centre are priced on.

    python calibrate.py bt_all.csv        # a backtest.py --csv file

For the total and each team's points (the close's split: home = (total -
home spread) / 2), the sd of the result around the close, by league and by
how high the close is, and the first half as a share of the game.
"""

import sys

import numpy as np
import pandas as pd

df = pd.read_csv(sys.argv[1], parse_dates=["tipoff"])
df = df.dropna(subset=["close_total", "close_spread"])
df["exp_h"] = (df.close_total - df.close_spread) / 2
df["exp_a"] = (df.close_total + df.close_spread) / 2
df["r_tot"] = df.home_pts + df.away_pts - df.close_total
df["r_h"] = df.home_pts - df.exp_h
df["r_a"] = df.away_pts - df.exp_a
for lg, g in df.groupby("league"):
    print(f"\n== {lg}: {len(g)} games with a close")
    rho = np.corrcoef(g.r_h, g.r_a)[0, 1]
    print(f"   sd total {g.r_tot.std():.2f}  sd home {g.r_h.std():.2f}  sd away {g.r_a.std():.2f}"
          f"  corr {rho:+.3f}  bias total {g.r_tot.mean():+.2f}")
    # sd = c * sqrt(mean) ?
    c_tot = g.r_tot.std() / np.sqrt(g.close_total.mean())
    c_team = pd.concat([g.r_h, g.r_a]).std() / np.sqrt(pd.concat([g.exp_h, g.exp_a]).mean())
    print(f"   c_total {c_tot:.3f}  c_team {c_team:.3f}  (sd / sqrt(mean))")
    g = g.assign(band=pd.qcut(g.close_total, 4))
    for b, x in g.groupby("band", observed=True):
        print(f"     close {str(b):18s} n {len(x):5d}  sd total {x.r_tot.std():.2f}"
              f"  -> c {x.r_tot.std() / np.sqrt(x.close_total.mean()):.3f}")
    # Kurtosis: is Normal right in the tails?
    z = g.r_tot / g.r_tot.std()
    print(f"   kurtosis (Normal 0): {((z ** 4).mean() - 3):+.2f}")
    h1 = g.dropna(subset=["home_h1"])
    if len(h1):
        share = (h1.home_h1 + h1.away_h1).sum() / (h1.home_pts + h1.away_pts).sum()
        r1 = h1.home_h1 + h1.away_h1 - share * h1.close_total
        print(f"   first half: {share:.3f} of the points, sd around share x close {r1.std():.2f}"
              f" (c {r1.std() / np.sqrt((share * h1.close_total).mean()):.3f})")
    ot = (g.periods > (2 if lg == "ncaam" else 4)).mean()
    print(f"   overtime in {ot:.1%} of games")
