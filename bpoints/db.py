"""Postgres writes. Every table is in the `bpoints` schema."""

import os
import time
from pathlib import Path

import psycopg

SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"


def open_conn(**kw):
    """psycopg.connect, retried for about two minutes: the owner's PC drops
    DNS lookups for a minute now and then."""
    for attempt in range(7):
        try:
            return psycopg.connect(os.environ["DATABASE_URL"], **kw)
        except psycopg.OperationalError:
            if attempt == 6:
                raise
            time.sleep(min(5 * 2 ** attempt, 40))


def connect():
    conn = open_conn(autocommit=False)
    with conn.cursor() as cur:
        cur.execute(SCHEMA.read_text(encoding="utf-8"))
    conn.commit()
    return conn


GAME_COLS = ["league", "game_id", "season", "tipoff", "status", "neutral", "home_id", "away_id",
             "home_name", "away_name", "home_pts", "away_pts", "home_reg", "away_reg", "home_h1",
             "away_h1", "periods", "home_poss", "away_poss", "close_total", "close_spread"]
TEAM_COLS = ["league", "team_id", "name", "short_name", "location", "abbrev"]


def upsert(conn, table: str, cols: list[str], rows: list[dict], key: list[str], touch: str = ""):
    if not rows:
        return
    # A feed without a line (the EuroLeague's own) must not wipe one written
    # from elsewhere (oddsapi_lines.py): those columns keep their value.
    keep = {"close_total", "close_spread"}
    updates = ", ".join([f"{c}=coalesce(EXCLUDED.{c}, {table}.{c})" if c in keep else f"{c}=EXCLUDED.{c}"
                         for c in cols if c not in key] +
                        ([f"{touch}=now()"] if touch else []))
    sql = (f"INSERT INTO bpoints.{table} ({', '.join(cols)}) "
           f"VALUES ({', '.join(['%s'] * len(cols))}) "
           f"ON CONFLICT ({', '.join(key)}) DO UPDATE SET {updates}")
    with conn.cursor() as cur:
        cur.executemany(sql, [[r.get(c) for c in cols] for r in rows])


def save_games(conn, games: list[dict], teams: list[dict]):
    # One row per key: a team appears in many games.
    teams = list({(t["league"], t["team_id"]): t for t in teams}.values())
    games = list({(g["league"], g["game_id"]): g for g in games}.values())
    upsert(conn, "team", TEAM_COLS, teams, ["league", "team_id"])
    upsert(conn, "game", GAME_COLS, games, ["league", "game_id"], touch="fetched_at")
    conn.commit()


def save_odds(conn, bookmaker: str, e: dict, rows: list[dict]):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO bpoints.bk_event (bookmaker, event_id, kickoff, league, our_league,
                                                     team1, team2, url)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (bookmaker, event_id) DO UPDATE
                          SET kickoff=EXCLUDED.kickoff, league=EXCLUDED.league,
                              our_league=EXCLUDED.our_league, url=EXCLUDED.url, last_seen=now()""",
                    (bookmaker, e["event_id"], e["kickoff"], e["league"], e.get("our_league"),
                     e["team1"], e["team2"], e.get("url")))
        cur.executemany(
            """INSERT INTO bpoints.odds (bookmaker, event_id, market, period, team, side, line,
                                         price, price_open)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (bookmaker, event_id, market, period, team, side, line) DO UPDATE
                  SET price=EXCLUDED.price, last_seen=now()""",
            [(bookmaker, e["event_id"], r["market"], r["period"], r["team"], r["side"], r["line"],
              r["price"], r["price"]) for r in rows if r.get("price")])
    conn.commit()
