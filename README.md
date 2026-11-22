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

The European leagues (EuroLeague, EuroCup, Liga Endesa) have no sharper line to centre
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

## On the server

`deploy/run_bpoints.sh` in the posession repo runs every two hours: collect,
settle, price, advice, notify. **Betano refuses the server** (HTTP 403), so
its lines come from the owner's PC, like the tennis and CS2 lines:

```powershell
powershell -ExecutionPolicy Bypass -File C:\Users\kenny\bpoints\betano_local.ps1
```
