# bpoints — pricing basketball over/under points lines

The basketball counterpart to posession (football possession), aces (tennis
aces) and cs2 (CS2 rounds): turn each game into a distribution of points,
price every Betano over/under line on it (both teams' total, each team's
points, for the whole game and the first half), and tip the lines that are
out of step.

Separate from posession, but shares its Neon database through its own
`bpoints` schema, so `DATABASE_URL` is the same string.

## Where the edge is

Not in out-guessing the American market. A team-rating model
(`bpoints/model.py`) run walk-forward over three NBA seasons adds nothing to
DraftKings' closing total:

```
== nba: 2839 games from 2024-10-01, all with a closing total
   model RMSE 18.64, close RMSE 17.96
   (total - close) = +0.51 + 0.059 x (model - close)   slope se 0.069
```

So the centre of every American game comes from DraftKings (ESPN publishes
its line), and the model's job is the **shape** around it, measured on the
same three seasons (`calibrate.py`):

- the total lands around the close with sd 17.8 in the NBA (Normal tails,
  kurtosis +0.06), each team's points 11.3, correlated +0.25;
- a **first half scores 50.5% of the points** - every NBA season the first
  half outscores the second (2026-27: 114.7 v 111.9 in regulation) - and its
  sd around 0.505 x close is 11.9.

Betano's lines are then compared with that. On the first day (21 Nov 2026)
its full-game totals sat within a point of DraftKings', but its first-half
lines ran from 48% to 52% of its own full-game total - three or four points
off, at prices near evens. That is what most tips are. Whether enough of it
is left after Betano's margin is what the settled record (betken.cz) will
show.

The European leagues (EuroLeague, EuroCup, Liga Endesa, Italy's Serie A) have no sharper line to centre
on: their lines are priced around Betano's own centre and settled, but not
tipped.

## Data

- **ESPN** (`bpoints/espn.py`): NBA, WNBA, NCAA men and women and the Australian NBL (no DraftKings line there, so it is priced around Betano's centre and not tipped) - every game
  with the score by period, the box score (possessions) and DraftKings' line
  (the summary's pick centre from 2026-27, ESPN's odds feed before that).
- **EuroLeague's own API** (`bpoints/euroleague.py`): EuroLeague and EuroCup,
  every game with the score by quarter.
- **ACB's match pages** (`bpoints/acb.py`): the Liga Endesa, one page per game
  (`live.acb.com/partidos/x-vs-y-{id}/estadisticas`) with the score by quarter,
  overtime as a fifth. A season's league games are a contiguous block of ids
  (2026-27: 105375-105680) in an id space shared with other competitions, so a
  game counts only when its competitionId is 1. Finished games are cached in
  `cache/acb/`; `collect.py --history --leagues acb` reads ids from 102800 (2023-24
  on) and takes about 45 minutes the first time.
- **Lega Basket's API** (`bpoints/legabasket.py`): Italy's Serie A (LBA),
  `www.legabasket.it/api/championships/...`. A season is two championships
  (regular season, playoffs; 2026-27's regular season is 602), listed round by
  round, with each finished game's quarters from its own call; overtime is one
  figure, so any overtime game counts as five periods. Clubs are keyed on their
  three-letter code (the numeric team id is new every season). Two seconds
  between requests, as robots.txt asks; finished rounds and games are cached in
  `cache/lba/`. `collect.py --history --leagues lba` (2023-24 on) takes about
  40 minutes the first time.

```bash
pip install -r requirements.txt
python collect.py --history     # since 2023 (NBA) / 2024 (NCAA, WNBA); about an hour the first time
python collect.py --lines       # closing totals missing from older summaries
python collect.py               # refresh: the last 5 days and the next 7
python coverage.py              # what is stored, per league and season
```

## The model and the tests

```bash
python backtest.py --csv bt_all.csv   # walk-forward v DraftKings' close, per league
python backtest.py --tune nba         # grid over the rating step and season carry-over
python calibrate.py bt_all.csv        # spreads around the close, first-half share, overtime
```

`bpoints/model.py`: attack and defence ratings per team on the log scale
(pace multiplies both teams' scoring), updated after every game, shrunk at a
new season. Kept for the leagues without a sharper line and as a check; its
weight against DraftKings' centre is 0 in the NBA (`price.W`).

## Betano lines, prices, tips

```bash
python odds.py              # Betano's points lines for games still to start -> bpoints.odds
python price.py --store     # the price on every line -> bpoints.priced (pairs games with results)
python advice.py            # the tips -> bpoints.advice, betken.cz/basketball
python settle.py            # settle lines and tips once the game is final
python notify.py            # Telegram: new and changed tips (same bot as tennis and CS2)
```

Tip rules (`advice.py`): the model's chance blended 3:1 with Betano's own
(margin out), +6% value or more, odds 1.40-3.50, a price on both sides of the
line, centred on DraftKings, at most one tip per game and period (a total and
its team totals are one bet), quarter-Kelly stakes shared per game. A tip is
rewritten until its game starts and locked by the database from then on.
Totals include overtime, as Betano settles them; a whole-number line that
lands exactly is void.

## SazkaBet

SazkaBet (now www.allwyn.cz) lists every game of a league in one JSON call,
each with its total-points ladder and its first-half total
(`bpoints/sazka.py`; each team's points are on another host and not read).
Its site sends a fixed `X-API-KEY` with these calls and so does this.
`sazka_odds.py` stores the lines as bookmaker `sazka`, after `odds.py` on the
PC and every two hours on the server; Czech place names ("Severní Karolína")
are put into English for pairing. The totals are taken to include overtime, as
Betano's do (their lines sit within a few points of Betano's, either side).

## Chance

Chance (chance.cz) has the same points markets as Betano - total and each
team's points including overtime, and first-half points, each a ladder - but
turns away anything that is not a browser (Cloudflare and its own bot check,
HTTP 403). webscraper.io's Cloud browser gets through, as it does for
posession's football and tennis odds, so `chance_pipeline.py` reads Chance in
two jobs: the tipped leagues' pages (NBA, NCAA men), then only the games that
pair with ours tipping off in the next 12 hours - a credit a page, about 35 a
run, from the account posession shares (it stops at 500 left). The Cloud
browser runs nine hours behind Prague, so Chance's own times are never read:
a game's tip-off is the results feed's. The server runs it at 15:00 and 21:00.

```bash
python chance_pipeline.py --dry-run      # which games, no credits
python chance_pipeline.py --links-only   # stage 1 and the pick (2 credits)
python chance_pipeline.py                # both stages, store the lines
```

By hand, in Chrome with the webscraper.io extension and `chance_sitemap.json`
(DevTools -> Web Scraper -> Create new sitemap -> Import sitemap -> Scrape),
exported as CSV and read in:

```bash
python chance_import.py chance.csv --dry-run   # what would be stored
python chance_import.py chance.csv             # -> bpoints.odds (bookmaker 'chance')
python price.py --store && python advice.py && python notify.py
```

Chance shows a game's time as "Dnes | 21:40" or "Zítra | 0:10" - when betting
closes, ten minutes after tip-off - so it is read against the time of the
scrape (the file's, or `--scraped-at`). The lines are only as fresh as that
scrape: `advice.py` stops tipping them six hours after it (`MAX_AGE`). A game
priced at both bookmakers still gets one tip per period, at whichever has the
better edge.

## On the server

`deploy/run_bpoints.sh` in the posession repo runs every two hours: collect,
settle, price, advice, notify - and at 15:00 and 21:00 Chance's lines first
(`run_bpoints.sh chance`). **Betano refuses the server** (HTTP 403), so
its lines come from the owner's PC, like the tennis and CS2 lines:

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\kenny\bpoints\betano_local.ps1
```
