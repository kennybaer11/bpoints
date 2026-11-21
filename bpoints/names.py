"""Pairing a bookmaker's game with a game in bpoints.game.

The bookmaker names a team its own way ("Gonzaga", "Paris Basketball",
"Crvena Zvezda") and the results feed another ("Gonzaga Bulldogs", "PARIS
BASKETBALL", "Crvena Zvezda Meridianbet Belgrade"). A team is compared with
every name the feed has for it (full, short, location, abbreviation), and a
game is paired only when both teams match one of the league's games that
starts within 18 hours of the bookmaker's time, clearly better than any
other game.
"""

import re
import unicodedata
from datetime import timedelta
from difflib import SequenceMatcher

NOISE = {"bc", "bk", "kk", "fc", "cb", "sc", "basket", "basketball", "club", "the", "university",
         "univ", "of", "u", "w", "women", "womens"}
SAME = {"st": "state", "saint": "st", "intl": "international", "n": "north", "s": "south"}


def norm(name: str) -> list[str]:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\(.*?\)", " ", s)
    toks = re.findall(r"[a-z0-9]+", s.replace("&", " and "))
    return [SAME.get(t, t) for t in toks if t not in NOISE]


def similarity(a: str, b: str) -> float:
    ta, tb = norm(a), norm(b)
    if not ta or not tb:
        return 0.0
    if ta == tb:
        return 1.0
    sa, sb = set(ta), set(tb)
    if sa <= sb or sb <= sa:
        # "gonzaga" in "gonzaga bulldogs": strong, but less so when the
        # shorter name is one common word ("state").
        return 0.92 if min(len(sa), len(sb)) > 1 or len(min(ta, tb, key=len)[0]) > 4 else 0.7
    return SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio() * 0.9


def team_score(bk_name: str, team: dict) -> float:
    names = [team.get(k) for k in ("name", "short_name", "location", "abbrev")]
    return max((similarity(bk_name, n) for n in names if n), default=0.0)


def pair(event: dict, games: list[dict], teams: dict) -> tuple[dict, bool] | None:
    """(game, swapped) for a bookmaker event, or None. games: the league's
    games near the time; teams: team_id -> its names."""
    scored = []
    for g in games:
        if abs(g["tipoff"] - event["kickoff"]) > timedelta(hours=18):
            continue
        h, a = teams.get(g["home_id"], {"name": g["home_name"]}), teams.get(g["away_id"], {"name": g["away_name"]})
        straight = min(team_score(event["team1"], h), team_score(event["team2"], a))
        swapped = min(team_score(event["team1"], a), team_score(event["team2"], h))
        scored.append((max(straight, swapped), swapped > straight, g))
    scored.sort(key=lambda x: -x[0])
    if not scored or scored[0][0] < 0.75:
        return None
    if len(scored) > 1 and scored[1][0] > scored[0][0] - 0.1:
        return None
    return scored[0][2], scored[0][1]
