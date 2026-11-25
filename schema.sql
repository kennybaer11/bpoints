-- bpoints: basketball results, for pricing over/under points lines (match
-- total, each team's points). Its own schema, so it shares the Neon database
-- with posession, aces and cs2 without colliding.

CREATE SCHEMA IF NOT EXISTS bpoints;

-- One row per game, played or scheduled. league: nba, wnba, ncaam, ncaaw
-- (ESPN), euroleague, eurocup (Euroleague's own API), acb (Liga Endesa, ACB's match pages), lba (Italy's Serie A, Lega Basket's API). Points include overtime;
-- reg_* are regulation only (overtime is bet on by Betano's totals, but the
-- model learns from regulation scoring and adds overtime separately).
CREATE TABLE IF NOT EXISTS bpoints.game (
  league       TEXT NOT NULL,
  game_id      TEXT NOT NULL,
  season       INTEGER,                 -- the year the season ends in
  tipoff       TIMESTAMPTZ NOT NULL,
  status       TEXT NOT NULL,           -- final | scheduled | other
  neutral      BOOLEAN NOT NULL DEFAULT false,
  home_id      TEXT NOT NULL,
  away_id      TEXT NOT NULL,
  home_name    TEXT NOT NULL,
  away_name    TEXT NOT NULL,
  home_pts     INTEGER,
  away_pts     INTEGER,
  home_reg     INTEGER,                 -- regulation points
  away_reg     INTEGER,
  home_h1      INTEGER,                 -- first-half points
  away_h1      INTEGER,
  periods      INTEGER,                 -- 4 (2 halves in NCAA men), more with overtime
  home_poss    NUMERIC,                 -- FGA - ORB + TO + 0.44 FTA, when the box score has it
  away_poss    NUMERIC,
  close_total  NUMERIC,                 -- DraftKings' closing total, from ESPN's pick centre
  close_spread NUMERIC,                 -- home team's line
  fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (league, game_id)
);
CREATE INDEX IF NOT EXISTS game_tipoff_idx ON bpoints.game (league, tipoff);

-- The names each source uses for a team, so the bookmaker's names can be
-- matched: ESPN gives a full name, a short one and the location.
CREATE TABLE IF NOT EXISTS bpoints.team (
  league      TEXT NOT NULL,
  team_id     TEXT NOT NULL,
  name        TEXT NOT NULL,
  short_name  TEXT,
  location    TEXT,
  abbrev      TEXT,
  PRIMARY KEY (league, team_id)
);

-- Bookmaker matches and every selection of their points markets. A line is
-- one row, rewritten while it is on offer: price is the latest, price_open
-- the first seen.
CREATE TABLE IF NOT EXISTS bpoints.bk_event (
  bookmaker   TEXT NOT NULL,              -- betano
  event_id    TEXT NOT NULL,
  kickoff     TIMESTAMPTZ NOT NULL,
  league      TEXT,                       -- the bookmaker's league name
  our_league  TEXT,                       -- nba, ncaam, ... when it is one we model
  team1       TEXT NOT NULL,              -- the bookmaker's names and order (team1 = home)
  team2       TEXT NOT NULL,
  url         TEXT,
  game_id     TEXT,                       -- bpoints.game, once paired
  swapped     BOOLEAN,                    -- bookmaker team1 is the game's away team
  first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (bookmaker, event_id)
);

-- market: total (both teams), team_total (team 1/2); period: ft (with
-- overtime, as Betano settles), h1 (first half).
CREATE TABLE IF NOT EXISTS bpoints.odds (
  bookmaker   TEXT    NOT NULL,
  event_id    TEXT    NOT NULL,
  market      TEXT    NOT NULL,
  period      TEXT    NOT NULL DEFAULT 'ft',
  team        INTEGER NOT NULL DEFAULT 0,
  side        TEXT    NOT NULL,           -- over | under
  line        NUMERIC NOT NULL,
  price       NUMERIC NOT NULL,
  price_open  NUMERIC NOT NULL,
  first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (bookmaker, event_id, market, period, team, side, line),
  FOREIGN KEY (bookmaker, event_id) REFERENCES bpoints.bk_event (bookmaker, event_id) ON DELETE CASCADE
);

-- The model's price on each stored line (price.py --store), rewritten while
-- the game is still to start, so the last one before tip-off stays.
-- mu: the expected points the line is on; sd: its spread; mu_model: the
-- model's own expectation before it is blended with the market's.
CREATE TABLE IF NOT EXISTS bpoints.priced (
  bookmaker   TEXT    NOT NULL,
  event_id    TEXT    NOT NULL,
  market      TEXT    NOT NULL,
  period      TEXT    NOT NULL DEFAULT 'ft',
  team        INTEGER NOT NULL DEFAULT 0,
  side        TEXT    NOT NULL,
  line        NUMERIC NOT NULL,
  price       NUMERIC NOT NULL,
  other_price NUMERIC,                     -- the opposite side, same line
  p_model     NUMERIC NOT NULL,
  p_book      NUMERIC,                     -- Betano's own chance, margin out
  edge        NUMERIC NOT NULL,            -- p_model * price - 1
  mu          NUMERIC NOT NULL,
  sd          NUMERIC NOT NULL,
  mu_model    NUMERIC,
  mu_book     NUMERIC,                     -- the centre of Betano's own ladder
  priced_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  result      TEXT,                        -- won | lost | void (push or no game)
  actual      INTEGER,
  settled_at  TIMESTAMPTZ,
  PRIMARY KEY (bookmaker, event_id, market, period, team, side, line),
  FOREIGN KEY (bookmaker, event_id) REFERENCES bpoints.bk_event (bookmaker, event_id) ON DELETE CASCADE
);

-- The advice the site gave, as it stood when the game started - the twin of
-- cs2.advice. At most one tip per game and period (ft, h1): Betano's
-- ladders offer a dozen correlated lines per market, and a total moves with its
-- team totals. Rewritten by advice.py
-- until tip-off, locked by the trigger below from then on.
CREATE TABLE IF NOT EXISTS bpoints.advice (
  bookmaker        TEXT    NOT NULL,
  event_id         TEXT    NOT NULL,
  slot             TEXT    NOT NULL,     -- the period: ft, h1
  kickoff          TIMESTAMPTZ NOT NULL,
  league           TEXT,
  team1            TEXT    NOT NULL,
  team2            TEXT    NOT NULL,
  market           TEXT    NOT NULL,
  period           TEXT    NOT NULL DEFAULT 'ft',
  team             INTEGER NOT NULL DEFAULT 0,
  side             TEXT    NOT NULL,
  line             NUMERIC NOT NULL,
  odds             NUMERIC NOT NULL,
  p_model          NUMERIC NOT NULL,     -- the chance the tip is priced on
  p_raw            NUMERIC NOT NULL,     -- the model's own chance
  edge             NUMERIC NOT NULL,     -- p_model * odds - 1
  rating           SMALLINT,             -- stake, tenths of a unit
  backed           BOOLEAN NOT NULL,     -- false: advised, then withdrawn before tip-off
  code_version     TEXT,
  first_advised_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  advised_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  result           TEXT,                 -- won | lost | void
  actual           INTEGER,
  settled_at       TIMESTAMPTZ,
  placed           BOOLEAN NOT NULL DEFAULT false,
  placed_at        TIMESTAMPTZ,
  told_bet         TEXT,                 -- what Telegram was last told (notify.py)
  told_rating      SMALLINT,
  told_at          TIMESTAMPTZ,
  PRIMARY KEY (bookmaker, event_id, slot),
  FOREIGN KEY (bookmaker, event_id) REFERENCES bpoints.bk_event (bookmaker, event_id)
);
CREATE INDEX IF NOT EXISTS advice_kickoff_idx ON bpoints.advice (kickoff);

-- The tip as Telegram first announced it (notify.py fills these once, when a tip
-- is first told, and never changes them): the bet the record's statistics count,
-- whatever the tip became afterwards. first_result/first_actual are settle.py's.
ALTER TABLE bpoints.advice ADD COLUMN IF NOT EXISTS first_market TEXT;
ALTER TABLE bpoints.advice ADD COLUMN IF NOT EXISTS first_period TEXT;
ALTER TABLE bpoints.advice ADD COLUMN IF NOT EXISTS first_team   INTEGER;
ALTER TABLE bpoints.advice ADD COLUMN IF NOT EXISTS first_side   TEXT;
ALTER TABLE bpoints.advice ADD COLUMN IF NOT EXISTS first_line   NUMERIC;
ALTER TABLE bpoints.advice ADD COLUMN IF NOT EXISTS first_odds   NUMERIC;
ALTER TABLE bpoints.advice ADD COLUMN IF NOT EXISTS first_rating SMALLINT;
ALTER TABLE bpoints.advice ADD COLUMN IF NOT EXISTS first_result TEXT;      -- won | lost | void
ALTER TABLE bpoints.advice ADD COLUMN IF NOT EXISTS first_actual INTEGER;

-- The reference centre a line was priced on: DraftKings' line from ESPN for
-- the American leagues (a much sharper market than Betano's on them).
ALTER TABLE bpoints.priced ADD COLUMN IF NOT EXISTS mu_ref NUMERIC;
ALTER TABLE bpoints.priced ADD COLUMN IF NOT EXISTS centre_from TEXT;   -- draftkings | oddsapi | betano | model
-- Where close_total/close_spread came from: NULL = DraftKings (ESPN), 'oddsapi' = The Odds API
-- (the European games: Pinnacle's line, else the median of the European books).
ALTER TABLE bpoints.game ADD COLUMN IF NOT EXISTS close_from TEXT;

CREATE OR REPLACE FUNCTION bpoints.advice_locked() RETURNS trigger AS $$
BEGIN
  IF OLD.kickoff <= now() AND
     (NEW.market, NEW.period, NEW.team, NEW.side, NEW.line, NEW.odds, NEW.p_model, NEW.edge,
      NEW.backed, NEW.kickoff, NEW.rating)
       IS DISTINCT FROM
     (OLD.market, OLD.period, OLD.team, OLD.side, OLD.line, OLD.odds, OLD.p_model, OLD.edge,
      OLD.backed, OLD.kickoff, OLD.rating) THEN
    RAISE EXCEPTION 'bpoints.advice %/%/% is locked: the game started at %',
      OLD.bookmaker, OLD.event_id, OLD.slot, OLD.kickoff;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS advice_locked ON bpoints.advice;
CREATE TRIGGER advice_locked BEFORE UPDATE ON bpoints.advice
  FOR EACH ROW EXECUTE FUNCTION bpoints.advice_locked();
CREATE OR REPLACE RULE advice_no_delete AS ON DELETE TO bpoints.advice
  WHERE OLD.kickoff <= now() DO INSTEAD NOTHING;
