"""The points model: how many points each team scores in regulation.

Each team has an attack and a defence rating, on the log scale, so that

    log E[home points] = log L + h/2 + att_home + dfn_away
    log E[away points] = log L - h/2 + att_away + dfn_home

(L the league's points per team per game, h home court; no h at a neutral
site). Multiplicative, because pace multiplies both teams' scoring: a fast
team adds points at both ends. After each game every rating moves along its
error in log points (an online fit, like Elo), faster in a team's first
games of a season. At a new season the ratings shrink towards zero, as
rosters change - in college much more than in the NBA.

A game's points are then Normal around those means, with a spread that
grows with the square root of the mean (scoring is a sum of many small
events), and overtime is added as a mixture: regulation ends level with the
chance that the margin's distribution puts on zero.
"""

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.stats import norm


@dataclass
class Params:
    k: float = 0.035            # rating step per game, log points
    k_early: float = 3.0        # extra step in a team's first games: k * (1 + k_early / (1 + n))
    carry: float = 0.7          # share of a rating kept into a new season
    k_league: float = 0.004     # step of the league average and home court
    # Spread: sd = c * sqrt(mean) for one team's points, the two teams'
    # residuals correlated rho; the margin's sd follows from the same two.
    c_team: float = 1.05
    rho: float = 0.10
    ot_points: float = 20.0     # both teams' points in one overtime
    reg_periods: int = 4


# Spreads (c_team, rho) from calibrate.py: results around DraftKings' closing
# line, 2023-2026 (NBA), 2024-2026 (NCAA, WNBA). The others are guesses.
LEAGUE_PARAMS = {
    "nba":        Params(k=0.030, carry=0.75, c_team=1.058, rho=0.25, ot_points=21.5),
    "wnba":       Params(k=0.035, carry=0.70, c_team=1.203, rho=0.37, ot_points=17.5),
    "ncaam":      Params(k=0.045, carry=0.55, c_team=1.168, rho=0.37, ot_points=15.5, reg_periods=2),
    "ncaaw":      Params(k=0.045, carry=0.55, c_team=1.15, rho=0.05, ot_points=14.0),
    "euroleague": Params(k=0.035, carry=0.65, c_team=1.05, rho=0.10, ot_points=17.5),
    "eurocup":    Params(k=0.040, carry=0.60, c_team=1.05, rho=0.10, ot_points=17.5),
    "acb":        Params(k=0.035, carry=0.65, c_team=1.05, rho=0.10, ot_points=17.5),
    "nbl":        Params(k=0.040, carry=0.60, c_team=1.05, rho=0.10, ot_points=20.0),
}
START_L = {"nba": 114.0, "wnba": 82.0, "ncaam": 72.0, "ncaaw": 65.0, "euroleague": 82.0,
           "eurocup": 82.0, "acb": 88.0, "nbl": 92.0}
START_H = {"nba": 0.02, "wnba": 0.03, "ncaam": 0.045, "ncaaw": 0.045, "euroleague": 0.045,
           "eurocup": 0.045, "acb": 0.045, "nbl": 0.03}


@dataclass
class State:
    league: str
    p: Params
    log_l: float
    h: float
    att: dict = field(default_factory=dict)
    dfn: dict = field(default_factory=dict)
    n: dict = field(default_factory=dict)       # games this season
    season: int | None = None

    @classmethod
    def new(cls, league: str, p: Params | None = None):
        return cls(league, p or LEAGUE_PARAMS[league], math.log(START_L[league]), START_H[league])

    def new_season(self, season):
        if self.season is not None and season != self.season:
            c = self.p.carry
            self.att = {t: v * c for t, v in self.att.items()}
            self.dfn = {t: v * c for t, v in self.dfn.items()}
            self.n = {}
        self.season = season

    def means(self, home: str, away: str, neutral: bool = False) -> tuple[float, float]:
        h = 0.0 if neutral else self.h / 2
        mh = math.exp(self.log_l + h + self.att.get(home, 0.0) + self.dfn.get(away, 0.0))
        ma = math.exp(self.log_l - h + self.att.get(away, 0.0) + self.dfn.get(home, 0.0))
        return mh, ma

    def update(self, home: str, away: str, neutral: bool, pts_h: int, pts_a: int):
        mh, ma = self.means(home, away, neutral)
        eh, ea = math.log(max(pts_h, 1) / mh), math.log(max(pts_a, 1) / ma)
        p = self.p
        kh = p.k * (1 + p.k_early / (1 + self.n.get(home, 0)))
        ka = p.k * (1 + p.k_early / (1 + self.n.get(away, 0)))
        self.att[home] = self.att.get(home, 0.0) + kh * eh
        self.dfn[away] = self.dfn.get(away, 0.0) + ka * eh
        self.att[away] = self.att.get(away, 0.0) + ka * ea
        self.dfn[home] = self.dfn.get(home, 0.0) + kh * ea
        self.log_l += p.k_league * (eh + ea) / 2
        if not neutral:
            self.h += p.k_league * (eh - ea)
        self.n[home] = self.n.get(home, 0) + 1
        self.n[away] = self.n.get(away, 0) + 1


def spreads(p: Params, mh: float, ma: float) -> dict:
    """sd of each team's points, of the total and of the margin (home - away)."""
    sh, sa = p.c_team * math.sqrt(mh), p.c_team * math.sqrt(ma)
    cov = p.rho * sh * sa
    return {"home": sh, "away": sa, "total": math.sqrt(sh * sh + sa * sa + 2 * cov),
            "margin": math.sqrt(sh * sh + sa * sa - 2 * cov)}


def p_overtime(margin_mean: float, margin_sd: float) -> float:
    """Chance regulation ends level: the margin's mass on (-0.5, 0.5)."""
    return float(norm.cdf(0.5, margin_mean, margin_sd) - norm.cdf(-0.5, margin_mean, margin_sd))


def p_over(line: float, mean: float, sd: float, p_ot: float = 0.0, ot_add: float = 0.0,
           ot_sd: float = 0.0) -> float:
    """P(points > line) with overtime as a mixture. A whole-number line can
    push; the chance is then of winning given no push (the stake comes back)."""
    def sf(x, m, s):
        return norm.sf(x, m, s)

    def mix(x):
        return (1 - p_ot) * sf(x, mean, sd) + p_ot * sf(x, mean + ot_add, math.hypot(sd, ot_sd))

    if float(line).is_integer():
        over, under = mix(line + 0.5), 1 - mix(line - 0.5)
        return float(over / (over + under))
    return float(mix(line))


def implied_centre(lines: list[tuple[float, float]], sd: float) -> float | None:
    """The mean of a Normal that fits a bookmaker's ladder best, given its
    no-vig P(over) at each line and a spread. Least squares on the probit
    scale, which a ladder's near-50% lines dominate."""
    pts = [(ln, q) for ln, q in lines if 0.03 < q < 0.97]
    if not pts:
        return None
    # line = mean - sd * z(q)  =>  mean = line + sd * z(q)
    est = [ln + sd * norm.ppf(q) for ln, q in pts]
    w = [q * (1 - q) for _, q in pts]
    return float(np.average(est, weights=w))
