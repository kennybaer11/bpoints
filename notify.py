#!/usr/bin/env python
"""Telegram messages for the basketball tips - the twin of the CS2 notify.py:
same bot, same chat, with a 🏀 so the sports are told apart.

    python notify.py             announce new tips
    python notify.py --dry-run   print what would be sent, send nothing
    python notify.py --test      one message, to check the wiring

Each tip is announced once and never revised. Until then advice.py may move
or withdraw it unseen; once Telegram has accepted the message (told_at), the
tip is locked like a placed one, so the site and the record keep what was
sent. Nothing is marked as told until Telegram has accepted it, so a failed
send is retried on the next run.
"""

import argparse
import html
import logging
import math
import os
import sys
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from psycopg.rows import dict_row

log = logging.getLogger("notify")

PRAGUE = ZoneInfo("Europe/Prague")
TELEGRAM_LIMIT = 3500
SITE = "https://betken.cz/basketball"
# Only tips at this value or more are announced. On the first 95 settled, those
# at 6-8% won 10 of 26 (the odds implied 12.9), those above won 38 of 69 (31.7).
# The others are still tips on the site.
ANNOUNCE_EDGE = 0.08
BOOKS = {"betano": "Betano", "chance": "Chance", "sazka": "SazkaBet", "fortuna": "Fortuna"}
MARKETS = {"total": "total points", "team_total": "team points"}

BET = "(market || '|' || period || '|' || team || '|' || side || '|' || line::float)"


def api(method, token, **params):
    """Telegram call. The token never reaches the log, only the method name."""
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=params, timeout=30)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {body.get('description') or r.status_code}")
    return body["result"]


def bet_text(market, period, team, side, line, t1, t2):
    who = {1: t1, 2: t2}.get(team) if market == "team_total" else None
    half = " (1st half)" if period == "h1" else ""
    return f"{who + ' ' if who else ''}{side.upper()} {line:.1f}{half}"


def floor_odds(r) -> float | None:
    """The lowest odds at which the tip still has value: 1 / its (blended)
    chance, rounded up. Betano's price often drops after a tip; above this the
    bet is still worth placing, below it skip it."""
    p = float(r["p_model"] or 0)
    return math.ceil(100 / p) / 100 if p > 0 else None


def describe(r, changed=False):
    e = html.escape
    start = r["kickoff"].astimezone(PRAGUE)
    now = bet_text(r["market"], r["period"], r["team"], r["side"], float(r["line"]),
                   r["team1"], r["team2"])

    def tip(text, rating, odds=None):
        if not text:
            return "no bet"
        low = floor_odds(r) if odds else None
        out = (f"<b>{e(text)}</b>" + (f" @ {odds:.2f}" if odds else "")
               + (f" (down to {low:.2f})" if low and low < odds else ""))
        return out + (f" · stake {rating}/10" if rating else "")

    out = [f"<b>{e(r['team1'])} v {e(r['team2'])}</b>",
           f"{e(r['league'] or '')} · {start:%a %d %b %H:%M} · {MARKETS.get(r['market'], r['market'])}"
           f"{', 1st half' if r['period'] == 'h1' else ''}"]
    if changed:
        was = None
        if r["told_bet"]:
            m, per, t, s, ln = r["told_bet"].split("|")
            was = bet_text(m, per, int(t), s, float(ln), r["team1"], r["team2"])
        out.append(f"{tip(was, r['told_rating'])} → "
                   f"{tip(now if r['backed'] else None, r['rating'], float(r['odds']))} · "
                   f"{BOOKS.get(r['bookmaker'], r['bookmaker'])}")
    else:
        out.append(f"{tip(now, r['rating'], float(r['odds']))} · edge {float(r['edge']):+.1%} · "
                   f"{BOOKS.get(r['bookmaker'], r['bookmaker'])}")
    return "\n".join(out)


def fetch(conn, where):
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"SELECT *, {BET} AS bet FROM bpoints.advice WHERE kickoff > now() AND {where} "
                    "ORDER BY kickoff, team1, slot")
        return cur.fetchall()


def build(rows, changed=False):
    n = f"{len(rows)} tip{'' if len(rows) == 1 else 's'}"
    if changed:
        header = f"🔄🏀 <b>Basketball advice changed</b> - {n}"
        footer = ("\n\n<i>Tips are recomputed until the game starts; the record keeps the last "
                  f"version before it.</i>\n<a href=\"{SITE}\">Basketball tips</a>")
    else:
        header = f"🏀 <b>New basketball tips</b> - {n}"
        footer = ("\n\n<i>Over/under points lines at +8% value or more, at most one per game and half. "
                  f"A new model, not yet proven against Betano - small stakes. \"Down to\" is the "
                  f"lowest price still worth taking if the odds drop.</i>\n"
                  f"<a href=\"{SITE}\">Basketball tips</a>")
    messages, current = [], header
    for r in rows:
        block = "\n\n" + describe(r, changed)
        if len(current) + len(block) + len(footer) > TELEGRAM_LIMIT:
            messages.append(current + footer)
            current = header + " (cont.)"
        current += block
    messages.append(current + footer)
    return messages


def mark_told(conn, rows):
    with conn.cursor() as cur:
        for r in rows:
            # first_*: the tip as first announced, kept for the record's statistics
            # (COALESCE: set once, whatever a later message says).
            cur.execute(f"""UPDATE bpoints.advice
                               SET told_bet = CASE WHEN backed THEN {BET} END,
                                   told_rating = rating, told_at = now(),
                                   first_market = COALESCE(first_market, market),
                                   first_period = COALESCE(first_period, period),
                                   first_team = COALESCE(first_team, team),
                                   first_side = COALESCE(first_side, side),
                                   first_line = COALESCE(first_line, line),
                                   first_odds = COALESCE(first_odds, odds),
                                   first_rating = CASE WHEN first_odds IS NULL THEN rating
                                                       ELSE first_rating END
                             WHERE (bookmaker, event_id, slot) = (%s, %s, %s)""",
                        (r["bookmaker"], r["event_id"], r["slot"]))
    conn.commit()


def main():
    ap = argparse.ArgumentParser(description="Telegram alerts for basketball tips.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    load_dotenv()
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")

    if args.test:
        if not (token and chat):
            raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first.")
        api("sendMessage", token, chat_id=chat, text="🏀 Basketball tips are connected.")
        print("Test message sent.")
        return 0

    from bpoints import db
    conn = db.connect()
    fresh = fetch(conn, f"told_at IS NULL AND backed AND edge >= {ANNOUNCE_EDGE}")
    if not fresh:
        log.info("No new basketball tip.")
        return 0
    messages = build(fresh)
    if args.dry_run or not (token and chat):
        if not args.dry_run:
            log.info("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set - %d tip(s) left unannounced.",
                     len(fresh))
        for m in messages:
            print(m, end="\n\n")
        return 0
    for m in messages:
        api("sendMessage", token, chat_id=chat, text=m, parse_mode="HTML",
            disable_web_page_preview="true")
    mark_told(conn, fresh)
    log.info("Announced %d tip(s) in %d message(s).", len(fresh), len(messages))
    return 0


if __name__ == "__main__":
    sys.exit(main())
