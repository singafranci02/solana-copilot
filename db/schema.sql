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

-- ── graduation_events ─────────────────────────────────────────────────────────
-- Records the moment a token completes its bonding curve and migrates to PumpSwap.
-- Only graduated tokens (~0.7-0.8% of all launches) receive structural analysis.
CREATE TABLE IF NOT EXISTS graduation_events (
    token_mint              TEXT PRIMARY KEY REFERENCES tokens(mint),
    graduated_at            INTEGER NOT NULL,             -- unix epoch
    graduation_mc_usd       REAL,                         -- MC at graduation (~$69K)
    sol_raised              REAL,                         -- SOL raised on BC (~85 SOL)
    detection_lag_seconds   INTEGER NOT NULL DEFAULT 0,   -- our latency vs event
    pumpswap_pool_address   TEXT,
    bc_top_holders_json     TEXT NOT NULL DEFAULT '[]',   -- JSON [{wallet, pct}] top-20 at grad
    structural_verdict      TEXT CHECK (structural_verdict IN ('SKIP','WATCH','STRUCTURALLY_SOUND',NULL)),
    verdict_confidence      REAL,                         -- 0.0–1.0
    smart_money_count       INTEGER NOT NULL DEFAULT 0,
    dominant_factors_json   TEXT NOT NULL DEFAULT '[]'    -- JSON string[] from StructuralRead
);

CREATE INDEX IF NOT EXISTS idx_grad_events_graduated_at ON graduation_events(graduated_at);

-- ── wallet_stats ──────────────────────────────────────────────────────────────
-- Incremental win/loss counters per wallet, updated after each 4h outcome.
-- Never fully recomputed — only incremented. win_rate is NULL until
-- total_calls >= 15 (enforced at query time, not in DB).
CREATE TABLE IF NOT EXISTS wallet_stats (
    address             TEXT PRIMARY KEY REFERENCES wallets(address),
    graduated_calls     INTEGER NOT NULL DEFAULT 0,   -- BC purchases of graduated tokens
    wins                INTEGER NOT NULL DEFAULT 0,   -- moon outcomes at 4h
    losses              INTEGER NOT NULL DEFAULT 0,   -- rug outcomes at 4h
    total_calls         INTEGER NOT NULL DEFAULT 0,
    win_rate            REAL,                          -- NULL until total_calls >= 15
    last_updated        INTEGER NOT NULL DEFAULT 0
);

-- ── funder_reputation ────────────────────────────────────────────────────────
-- Track record of a funding wallet across graduated launches it funded.
-- is_known_rugger is only set when rug_rate > 0.65 AND COUNT(graduated_mints) >= 8.
CREATE TABLE IF NOT EXISTS funder_reputation (
    funding_source      TEXT PRIMARY KEY,
    graduated_mints     TEXT NOT NULL DEFAULT '[]',   -- JSON array of mints
    rug_count           INTEGER NOT NULL DEFAULT 0,
    moon_count          INTEGER NOT NULL DEFAULT 0,
    ok_count            INTEGER NOT NULL DEFAULT 0,
    rug_rate            REAL NOT NULL DEFAULT 0.0,
    avg_bundle_pct      REAL NOT NULL DEFAULT 0.0,
    avg_dev_pct         REAL NOT NULL DEFAULT 0.0,
    last_seen           INTEGER,
    is_known_rugger     INTEGER NOT NULL DEFAULT 0
                        CHECK (is_known_rugger IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_funder_rep_rug_rate ON funder_reputation(rug_rate DESC);
CREATE INDEX IF NOT EXISTS idx_funder_rep_last_seen ON funder_reputation(last_seen);

-- ── team_clusters ─────────────────────────────────────────────────────────────
-- Per-token team cluster detected from BC-phase buyers at graduation.
-- Richer than wallet_clusters: tracks supply_pct at the moment of graduation.
CREATE TABLE IF NOT EXISTS team_clusters (
    cluster_id               TEXT PRIMARY KEY,
    token_mint               TEXT NOT NULL REFERENCES tokens(mint),
    funding_source           TEXT,
    member_addresses         TEXT NOT NULL DEFAULT '[]', -- JSON array
    supply_pct_at_graduation REAL NOT NULL DEFAULT 0.0,  -- % supply at graduation
    first_buy_offset_seconds REAL NOT NULL DEFAULT 0.0,  -- seconds after launch
    is_bc_sniper             INTEGER NOT NULL DEFAULT 0   -- bought within first 30s
                             CHECK (is_bc_sniper IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_team_clusters_token_mint     ON team_clusters(token_mint);
CREATE INDEX IF NOT EXISTS idx_team_clusters_funding_source ON team_clusters(funding_source);

-- ── post_grad_behavior ────────────────────────────────────────────────────────
-- Selling/holding behavior of team cluster + early snipers post-graduation.
-- Checked at graduation_time + 1h / 4h / 24h.
CREATE TABLE IF NOT EXISTS post_grad_behavior (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    token_mint               TEXT NOT NULL REFERENCES tokens(mint),
    checked_at               INTEGER NOT NULL,
    check_offset_h           INTEGER NOT NULL,             -- 1, 4, or 24
    holders_remaining_count  INTEGER,
    team_sold_pct            REAL,                         -- % of team position sold
    snipers_sold_pct         REAL,
    liquidity_usd            REAL,
    team_buy_count           INTEGER NOT NULL DEFAULT 0,   -- team buy txns since graduation
    team_sell_count          INTEGER NOT NULL DEFAULT 0,   -- team sell txns since graduation
    team_net_sol             REAL,                         -- sell SOL − buy SOL (positive = net out)
    coordinated_sell_count   INTEGER NOT NULL DEFAULT 0,   -- 5-min windows with ≥2 team sellers
    distribution_signal      TEXT NOT NULL DEFAULT 'HOLDING'
                             CHECK (distribution_signal IN ('ACCUMULATING','HOLDING','DISTRIBUTING','DUMPED'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_post_grad_mint_offset
    ON post_grad_behavior(token_mint, check_offset_h);
CREATE INDEX IF NOT EXISTS idx_post_grad_checked_at ON post_grad_behavior(checked_at);

-- ── post_grad_swaps ───────────────────────────────────────────────────────────
-- Individual buy/sell transactions by team cluster members for a graduated token.
-- Reconstructed from Helius transaction history at each 1h/4h/24h check. The
-- composite PK dedups across re-fetches (parse_swap yields no tx signature).
CREATE TABLE IF NOT EXISTS post_grad_swaps (
    token_mint     TEXT NOT NULL REFERENCES tokens(mint),
    wallet_address TEXT NOT NULL,
    side           TEXT NOT NULL CHECK (side IN ('buy','sell')),
    sol_amount     REAL NOT NULL,
    token_amount   REAL NOT NULL,
    price_sol      REAL,                       -- sol_amount/token_amount, NULL if token_amount=0
    ts             INTEGER NOT NULL,           -- unix epoch (>= graduated_at)
    slot           INTEGER NOT NULL,
    is_sniper      INTEGER NOT NULL DEFAULT 0 CHECK (is_sniper IN (0,1)),
    is_team        INTEGER NOT NULL DEFAULT 1 CHECK (is_team IN (0,1)),
    PRIMARY KEY (token_mint, wallet_address, slot, side)
);

CREATE INDEX IF NOT EXISTS idx_pgs_token_ts ON post_grad_swaps(token_mint, ts);

-- ── bc_accumulation ───────────────────────────────────────────────────────────
-- Per-holder bonding-curve accumulation profile, reconstructed AT graduation
-- (when BC txs are still in each wallet's recent-100-tx window). Captures HOW a
-- holder built their position pre-graduation — the predictive half of the thesis.
CREATE TABLE IF NOT EXISTS bc_accumulation (
    token_mint         TEXT NOT NULL REFERENCES tokens(mint),
    wallet_address     TEXT NOT NULL,
    first_buy_offset_s REAL,                       -- entry timing vs token created_at
    bc_buy_count       INTEGER NOT NULL DEFAULT 0,
    bc_sell_count      INTEGER NOT NULL DEFAULT 0,
    total_sol_in       REAL NOT NULL DEFAULT 0.0,
    accumulation_style TEXT CHECK (accumulation_style IN ('sniped','gradual','single',NULL)),
    PRIMARY KEY (token_mint, wallet_address)
);

CREATE INDEX IF NOT EXISTS idx_bc_accum_token ON bc_accumulation(token_mint);

-- ── holder_snapshots ──────────────────────────────────────────────────────────
-- Holder-base trajectory at each 1h/4h/24h check: count, concentration, churn.
-- Distinguishes organic growth from team churn. holder_count is a top-20 proxy
-- unless holder_count_is_total=1 (true total via DAS pagination — deferred).
CREATE TABLE IF NOT EXISTS holder_snapshots (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    token_mint            TEXT NOT NULL REFERENCES tokens(mint),
    checked_at            INTEGER NOT NULL,
    check_offset_h        INTEGER NOT NULL,
    holder_count          INTEGER,
    holder_count_is_total INTEGER NOT NULL DEFAULT 0,
    top10_pct             REAL,
    new_holder_count      INTEGER NOT NULL DEFAULT 0,
    churned_holder_count  INTEGER NOT NULL DEFAULT 0,
    new_smart_money_count INTEGER NOT NULL DEFAULT 0,
    top10_value_usd       REAL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_holder_snap_mint_offset
    ON holder_snapshots(token_mint, check_offset_h);

-- ── live_trades ───────────────────────────────────────────────────────────────
-- Full order flow (every buy/sell, every wallet) for watched/backfilled tokens,
-- with each trade's wallet tagged (team/smart_money/known_rugger/new/unknown).
-- High volume — SQLite is the source of truth; Supabase gets only aggregates +
-- a recent-N tape. source='live' (PumpPortal stream) or 'backfill' (Helius pool history).
CREATE TABLE IF NOT EXISTS live_trades (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    token_mint     TEXT NOT NULL REFERENCES tokens(mint),
    wallet_address TEXT NOT NULL,
    side           TEXT NOT NULL CHECK (side IN ('buy','sell')),
    sol_amount     REAL NOT NULL,
    token_amount   REAL NOT NULL,
    price_sol      REAL,
    price_usd      REAL,
    ts             INTEGER NOT NULL,
    slot           INTEGER,
    signature      TEXT,
    source         TEXT NOT NULL CHECK (source IN ('live','backfill')),
    wallet_tag     TEXT NOT NULL DEFAULT 'unknown'
                   CHECK (wallet_tag IN ('team','smart_money','known_rugger','new','unknown')),
    dedup_key      TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_live_trades_token_ts  ON live_trades(token_mint, ts);
CREATE INDEX IF NOT EXISTS idx_live_trades_token_tag ON live_trades(token_mint, wallet_tag);

-- ── coin_coordination + coordinated_entities ─────────────────────────────────
-- Coordinated-entity detection: groups wallets acting as one team on a coin via
-- same-slot bundles + shared funder + buy-size + lockstep selling (union-find).
-- Survives fresh-wallet rotation. coin_coordination = per-coin rollup; the
-- entities table = each detected coordinated group.
CREATE TABLE IF NOT EXISTS coin_coordination (
    token_mint                  TEXT PRIMARY KEY REFERENCES tokens(mint),
    computed_at                 INTEGER NOT NULL,
    source                      TEXT NOT NULL CHECK (source IN ('batch','live')),
    entity_count                INTEGER NOT NULL DEFAULT 0,
    bundled_supply_pct          REAL NOT NULL DEFAULT 0.0,
    bundle_wallet_count         INTEGER NOT NULL DEFAULT 0,
    largest_bundle_size         INTEGER NOT NULL DEFAULT 0,
    largest_entity_supply_pct   REAL NOT NULL DEFAULT 0.0,
    largest_entity_wallet_count INTEGER NOT NULL DEFAULT 0,
    largest_entity_fresh_ratio  REAL NOT NULL DEFAULT 0.0,
    largest_entity_state        TEXT
);

CREATE TABLE IF NOT EXISTS coordinated_entities (
    token_mint       TEXT NOT NULL REFERENCES tokens(mint),
    entity_id        TEXT NOT NULL,
    member_addresses TEXT NOT NULL DEFAULT '[]',   -- JSON
    wallet_count     INTEGER NOT NULL,
    supply_pct       REAL NOT NULL,
    fresh_ratio      REAL NOT NULL DEFAULT 0.0,
    state            TEXT,
    edge_sources     TEXT NOT NULL DEFAULT '[]',   -- JSON: which signals linked it
    computed_at      INTEGER NOT NULL,
    PRIMARY KEY (token_mint, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_coord_ent_token ON coordinated_entities(token_mint);

-- ── wallet_graph ──────────────────────────────────────────────────────────────
-- Co-occurrence graph: wallets that habitually appear together in team clusters.
-- Survives wallet rotation — recycling even 1-2 wallets across launches exposes
-- the same team. Pairs stored with wallet_a < wallet_b (canonical ordering).
CREATE TABLE IF NOT EXISTS wallet_graph (
    wallet_a           TEXT NOT NULL,
    wallet_b           TEXT NOT NULL,
    co_appearances     INTEGER NOT NULL DEFAULT 1,
    rug_co_appearances INTEGER NOT NULL DEFAULT 0,   -- co-appearances in rug outcomes
    last_seen_together INTEGER NOT NULL,              -- unix epoch
    PRIMARY KEY (wallet_a, wallet_b),
    CHECK (wallet_a < wallet_b)
);

CREATE INDEX IF NOT EXISTS idx_wg_a ON wallet_graph(wallet_a);
CREATE INDEX IF NOT EXISTS idx_wg_b ON wallet_graph(wallet_b);

-- ── cex_hotwallets ────────────────────────────────────────────────────────────
-- Known CEX hot wallet addresses on Solana. Seeded from cex_wallets.py;
-- extended over time via Solscan/Arkham verification.
CREATE TABLE IF NOT EXISTS cex_hotwallets (
    address     TEXT PRIMARY KEY,
    exchange    TEXT NOT NULL,
    label       TEXT,
    confirmed   INTEGER NOT NULL DEFAULT 0 CHECK (confirmed IN (0, 1)),
    added_at    INTEGER NOT NULL
);
