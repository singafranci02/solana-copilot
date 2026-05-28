-- solana-copilot SQLite schema
-- Run via src/common/db.py:migrate() — idempotent (CREATE IF NOT EXISTS throughout)

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ── tokens ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tokens (
    mint                    TEXT PRIMARY KEY,
    symbol                  TEXT,
    name                    TEXT,
    launchpad               TEXT NOT NULL CHECK (launchpad IN ('pump.fun', 'bags', 'unknown')),
    created_at              INTEGER NOT NULL,          -- unix epoch
    market_cap_usd_snapshot REAL,
    holders_count_snapshot  INTEGER,
    lp_burned               INTEGER NOT NULL DEFAULT 0 CHECK (lp_burned IN (0, 1)),
    top10_pct               REAL,                     -- % supply held by top 10 wallets
    bundle_pct              REAL,                     -- % bought in coordinated bundle at launch
    dev_pct                 REAL,                     -- % supply held by detected dev cluster
    narrative_tags          TEXT NOT NULL DEFAULT '[]' -- JSON array of narrative labels
);

CREATE INDEX IF NOT EXISTS idx_tokens_created_at  ON tokens (created_at);
CREATE INDEX IF NOT EXISTS idx_tokens_launchpad   ON tokens (launchpad);

-- ── wallets ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wallets (
    address         TEXT PRIMARY KEY,
    label           TEXT,                             -- human tag: "smart money", "dev", etc.
    smart_money_score REAL NOT NULL DEFAULT 0.0,      -- 0-1
    win_rate_90d    REAL,                             -- % of profitable trades last 90 d
    total_trades    INTEGER NOT NULL DEFAULT 0,
    first_seen      INTEGER,                          -- unix epoch of first observed tx
    funding_source  TEXT                              -- funding wallet address or 'cex'
);

CREATE INDEX IF NOT EXISTS idx_wallets_funding_source ON wallets (funding_source);
CREATE INDEX IF NOT EXISTS idx_wallets_smart_money    ON wallets (smart_money_score);

-- ── wallet_clusters ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wallet_clusters (
    cluster_id       TEXT PRIMARY KEY,                -- UUID
    funding_source   TEXT NOT NULL,
    funded_at        INTEGER,                         -- unix epoch
    member_addresses TEXT NOT NULL DEFAULT '[]',      -- JSON array of wallet addresses
    is_likely_team   INTEGER NOT NULL DEFAULT 0 CHECK (is_likely_team IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_clusters_funding_source ON wallet_clusters (funding_source);
CREATE INDEX IF NOT EXISTS idx_clusters_funded_at      ON wallet_clusters (funded_at);

-- ── token_buyers ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS token_buyers (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    token_mint        TEXT NOT NULL REFERENCES tokens (mint),
    wallet_address    TEXT NOT NULL REFERENCES wallets (address),
    bought_at         INTEGER NOT NULL,               -- unix epoch
    sol_amount        REAL NOT NULL,
    tokens_received   REAL NOT NULL,
    position_size_pct REAL,                           -- % of token supply
    exit_price_sol    REAL,                           -- NULL until closed
    exit_at           INTEGER                         -- NULL until closed
);

CREATE INDEX IF NOT EXISTS idx_buyers_token_mint      ON token_buyers (token_mint);
CREATE INDEX IF NOT EXISTS idx_buyers_wallet_address  ON token_buyers (wallet_address);
CREATE INDEX IF NOT EXISTS idx_buyers_bought_at       ON token_buyers (bought_at);

-- ── my_trades ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS my_trades (
    tx_signature                TEXT PRIMARY KEY,
    token_mint                  TEXT NOT NULL REFERENCES tokens (mint),
    side                        TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    ts                          INTEGER NOT NULL,     -- unix epoch
    sol_amount                  REAL NOT NULL,
    tokens                      REAL NOT NULL,
    price_sol                   REAL NOT NULL,
    mc_at_entry                 REAL,
    holders_at_entry            INTEGER,
    smart_money_in_count_at_entry INTEGER,
    lp_burned                   INTEGER CHECK (lp_burned IN (0, 1)),
    top10_pct                   REAL,
    bundle_pct                  REAL,
    dev_pct                     REAL,
    source_tag                  TEXT,                 -- e.g. "smart_money_alert", "manual"
    conviction                  INTEGER CHECK (conviction BETWEEN 1 AND 5),
    rules_followed              TEXT,                 -- JSON array of rule IDs
    exit_reason                 TEXT,
    notes                       TEXT
);

CREATE INDEX IF NOT EXISTS idx_my_trades_token_mint ON my_trades (token_mint);
CREATE INDEX IF NOT EXISTS idx_my_trades_ts         ON my_trades (ts);

-- ── narratives ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS narratives (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    label            TEXT NOT NULL UNIQUE,
    keywords         TEXT NOT NULL DEFAULT '[]',      -- JSON array
    started_at       INTEGER NOT NULL,                -- unix epoch
    peak_velocity    REAL NOT NULL DEFAULT 0.0,       -- mentions/hour at peak
    current_velocity REAL NOT NULL DEFAULT 0.0,       -- mentions/hour rolling 1h
    status           TEXT NOT NULL DEFAULT 'emerging' CHECK (status IN ('emerging', 'hot', 'fading', 'dead'))
);

CREATE INDEX IF NOT EXISTS idx_narratives_status     ON narratives (status);
CREATE INDEX IF NOT EXISTS idx_narratives_started_at ON narratives (started_at);

-- ── narrative_mentions ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS narrative_mentions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    narrative_id  INTEGER NOT NULL REFERENCES narratives (id),
    x_handle      TEXT NOT NULL,
    posted_at     INTEGER NOT NULL,                   -- unix epoch
    follower_count INTEGER,
    text_excerpt  TEXT
);

CREATE INDEX IF NOT EXISTS idx_mentions_narrative_id ON narrative_mentions (narrative_id);
CREATE INDEX IF NOT EXISTS idx_mentions_posted_at    ON narrative_mentions (posted_at);
CREATE INDEX IF NOT EXISTS idx_mentions_x_handle     ON narrative_mentions (x_handle);

-- ── coin_outcomes ─────────────────────────────────────────────────────────────
-- Price snapshots taken automatically at 1h / 4h / 24h after launch.
-- This is the ground-truth that drives wallet win-rate computation.
CREATE TABLE IF NOT EXISTS coin_outcomes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token_mint      TEXT NOT NULL REFERENCES tokens (mint),
    check_offset_h  INTEGER NOT NULL,                   -- 1, 4, or 24
    checked_at      INTEGER NOT NULL,                   -- unix epoch when snapshot taken
    mc_usd          REAL,                               -- market cap at check time
    price_change_pct REAL,                              -- % change from launch MC snapshot
    classified      TEXT CHECK (classified IN ('moon', 'ok', 'rug', NULL))
                    -- moon: >3x, ok: flat/up, rug: >-70%
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_outcomes_mint_offset
    ON coin_outcomes (token_mint, check_offset_h);
CREATE INDEX IF NOT EXISTS idx_outcomes_checked_at ON coin_outcomes (checked_at);

-- ── team_fingerprints ─────────────────────────────────────────────────────────
-- Persistent fingerprint of a known dev team, built from observed launches.
-- Used to match new coins to known serial developers.
CREATE TABLE IF NOT EXISTS team_fingerprints (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint_id      TEXT NOT NULL UNIQUE,           -- UUID
    funding_source      TEXT NOT NULL,                  -- funder address
    known_mints         TEXT NOT NULL DEFAULT '[]',     -- JSON — mints they launched
    outcome_labels      TEXT NOT NULL DEFAULT '[]',     -- JSON — "moon"/"rug"/"ok" per mint
    avg_cluster_size    REAL NOT NULL DEFAULT 0.0,
    avg_bundle_pct      REAL NOT NULL DEFAULT 0.0,
    avg_dev_pct         REAL NOT NULL DEFAULT 0.0,
    rug_rate            REAL NOT NULL DEFAULT 0.0,      -- fraction of launches that rugged
    moon_rate           REAL NOT NULL DEFAULT 0.0,
    last_seen           INTEGER,                        -- epoch of most recent launch
    description_keywords TEXT NOT NULL DEFAULT '[]'     -- JSON — common words in their descriptions
);

CREATE INDEX IF NOT EXISTS idx_fingerprints_funding_source
    ON team_fingerprints (funding_source);
CREATE INDEX IF NOT EXISTS idx_fingerprints_rug_rate
    ON team_fingerprints (rug_rate DESC);
