"""What is in bpoints.game, per league and season.

    python coverage.py
"""

import os

import psycopg
from dotenv import load_dotenv

load_dotenv()
with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
    print(f"{'league':11s} {'season':>6s} {'games':>6s} {'final':>6s} {'close':>6s} {'poss':>6s} {'h1':>6s}")
    for r in conn.execute("""SELECT league, season, count(*), count(*) FILTER (WHERE status = 'final'),
                                    count(close_total), count(home_poss), count(home_h1)
                               FROM bpoints.game GROUP BY 1, 2 ORDER BY 1, 2"""):
        print(f"{r[0]:11s} {r[1] or 0:6d} {r[2]:6d} {r[3]:6d} {r[4]:6d} {r[5]:6d} {r[6]:6d}")
