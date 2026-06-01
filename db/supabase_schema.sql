-- solana-copilot Supabase (Postgres) schema
-- Paste this into Supabase → SQL Editor → New Query → Run
-- This is a read-optimised mirror of the SQLite schema.
-- The Mac mini writes to SQLite and syncs here asynchronously.

-- Enable RLS on all tables so the anon key is read-only from the dashboard
-- (service_role key bypasses RLS and is used only by the Mac mini sync)

-- ── tokens ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tokens (
    mint                    TEXT PRIMARY KEY,
    symbol                  TEXT,
    name                    TEXT,
    launchpad               TEXT,
    created_at              BIGINT NOT NULL,
    market_cap_usd_snapshot DOUBLE PRECISION,
    lp_burned               BOOLEAN NOT NULL DEFAULT FALSE,
    bundle_pct              DOUBLE PRECISION,
    dev_pct                 DOUBLE PRECISION,
    narrative_tags          JSONB NOT NULL DEFAULT '[]'
);

ALTER TABLE tokens ENABLE ROW LEVEL SECURITY;
CREATE POLICY "read-only anon" ON tokens FOR SELECT USING (true);

-- ── graduation_events ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS graduation_events (
    token_mint              TEXT PRIMARY KEY REFERENCES tokens(mint),
    graduated_at            BIGINT NOT NULL,
    graduation_mc_usd       DOUBLE PRECISION,
    sol_raised              DOUBLE PRECISION,
    detection_lag_seconds   INTEGER NOT NULL DEFAULT 0,
    pumpswap_pool_address   TEXT,
    bc_top_holders_json     JSONB NOT NULL DEFAULT '[]',
    structural_verdict      TEXT CHECK (structural_verdict IN ('SKIP','WATCH','STRUCTURALLY_SOUND')),
    verdict_confidence      DOUBLE PRECISION,
    smart_money_count       INTEGER NOT NULL DEFAULT 0,
    dominant_factors_json   JSONB NOT NULL DEFAULT '[]'
);

ALTER TABLE graduation_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY "read-only anon" ON graduation_events FOR SELECT USING (true);

CREATE INDEX IF NOT EXISTS idx_grad_graduated_at ON graduation_events(graduated_at DESC);
CREATE INDEX IF NOT EXISTS idx_grad_verdict ON graduation_events(structural_verdict);

-- ── team_clusters ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS team_clusters (
    cluster_id               TEXT PRIMARY KEY,
    token_mint               TEXT NOT NULL REFERENCES tokens(mint),
    funding_source           TEXT,
    member_addresses         JSONB NOT NULL DEFAULT '[]',
    supply_pct_at_graduation DOUBLE PRECISION NOT NULL DEFAULT 0,
    first_buy_offset_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
    is_bc_sniper             BOOLEAN NOT NULL DEFAULT FALSE
);

ALTER TABLE team_clusters ENABLE ROW LEVEL SECURITY;
CREATE POLICY "read-only anon" ON team_clusters FOR SELECT USING (true);

CREATE INDEX IF NOT EXISTS idx_tc_token_mint ON team_clusters(token_mint);

-- ── coin_outcomes ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS coin_outcomes (
    id               BIGSERIAL PRIMARY KEY,
    token_mint       TEXT NOT NULL REFERENCES tokens(mint),
    check_offset_h   INTEGER NOT NULL,
    checked_at       BIGINT NOT NULL,
    mc_usd           DOUBLE PRECISION,
    price_change_pct DOUBLE PRECISION,
    classified       TEXT CHECK (classified IN ('moon','ok','rug')),
    UNIQUE (token_mint, check_offset_h)
);

ALTER TABLE coin_outcomes ENABLE ROW LEVEL SECURITY;
CREATE POLICY "read-only anon" ON coin_outcomes FOR SELECT USING (true);

CREATE INDEX IF NOT EXISTS idx_co_token_mint ON coin_outcomes(token_mint);
CREATE INDEX IF NOT EXISTS idx_co_checked_at ON coin_outcomes(checked_at DESC);

-- ── post_grad_behavior ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS post_grad_behavior (
    id                       BIGSERIAL PRIMARY KEY,
    token_mint               TEXT NOT NULL REFERENCES tokens(mint),
    checked_at               BIGINT NOT NULL,
    check_offset_h           INTEGER NOT NULL,
    holders_remaining_count  INTEGER,
    team_sold_pct            DOUBLE PRECISION,
    snipers_sold_pct         DOUBLE PRECISION,
    liquidity_usd            DOUBLE PRECISION,
    team_buy_count           INTEGER NOT NULL DEFAULT 0,
    team_sell_count          INTEGER NOT NULL DEFAULT 0,
    team_net_sol             DOUBLE PRECISION,
    coordinated_sell_count   INTEGER NOT NULL DEFAULT 0,
    distribution_signal      TEXT NOT NULL DEFAULT 'HOLDING'
                             CHECK (distribution_signal IN ('ACCUMULATING','HOLDING','DISTRIBUTING','DUMPED')),
    UNIQUE (token_mint, check_offset_h)
);

ALTER TABLE post_grad_behavior ENABLE ROW LEVEL SECURITY;
CREATE POLICY "read-only anon" ON post_grad_behavior FOR SELECT USING (true);

-- For existing Supabase installs, add the new columns:
ALTER TABLE post_grad_behavior ADD COLUMN IF NOT EXISTS team_buy_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE post_grad_behavior ADD COLUMN IF NOT EXISTS team_sell_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE post_grad_behavior ADD COLUMN IF NOT EXISTS team_net_sol DOUBLE PRECISION;
ALTER TABLE post_grad_behavior ADD COLUMN IF NOT EXISTS coordinated_sell_count INTEGER NOT NULL DEFAULT 0;

-- ── post_grad_swaps ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS post_grad_swaps (
    token_mint     TEXT NOT NULL REFERENCES tokens(mint),
    wallet_address TEXT NOT NULL,
    side           TEXT NOT NULL CHECK (side IN ('buy','sell')),
    sol_amount     DOUBLE PRECISION NOT NULL,
    token_amount   DOUBLE PRECISION NOT NULL,
    price_sol      DOUBLE PRECISION,
    ts             BIGINT NOT NULL,
    slot           BIGINT NOT NULL,
    is_sniper      BOOLEAN NOT NULL DEFAULT FALSE,
    is_team        BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (token_mint, wallet_address, slot, side)
);

ALTER TABLE post_grad_swaps ENABLE ROW LEVEL SECURITY;
CREATE POLICY "read-only anon" ON post_grad_swaps FOR SELECT USING (true);

CREATE INDEX IF NOT EXISTS idx_pgs_token_ts ON post_grad_swaps(token_mint, ts);

CREATE INDEX IF NOT EXISTS idx_pgb_token_mint ON post_grad_behavior(token_mint);

-- ── funder_reputation ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS funder_reputation (
    funding_source  TEXT PRIMARY KEY,
    graduated_mints JSONB NOT NULL DEFAULT '[]',
    rug_count       INTEGER NOT NULL DEFAULT 0,
    moon_count      INTEGER NOT NULL DEFAULT 0,
    ok_count        INTEGER NOT NULL DEFAULT 0,
    rug_rate        DOUBLE PRECISION NOT NULL DEFAULT 0,
    moon_rate       DOUBLE PRECISION NOT NULL DEFAULT 0,
    avg_bundle_pct  DOUBLE PRECISION NOT NULL DEFAULT 0,
    avg_dev_pct     DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_seen       BIGINT,
    is_known_rugger BOOLEAN NOT NULL DEFAULT FALSE
);

ALTER TABLE funder_reputation ENABLE ROW LEVEL SECURITY;
CREATE POLICY "read-only anon" ON funder_reputation FOR SELECT USING (true);

-- ── wallet_stats ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wallet_stats (
    address         TEXT PRIMARY KEY,
    graduated_calls INTEGER NOT NULL DEFAULT 0,
    wins            INTEGER NOT NULL DEFAULT 0,
    losses          INTEGER NOT NULL DEFAULT 0,
    total_calls     INTEGER NOT NULL DEFAULT 0,
    win_rate        DOUBLE PRECISION,
    last_updated    BIGINT NOT NULL DEFAULT 0
);

ALTER TABLE wallet_stats ENABLE ROW LEVEL SECURITY;
CREATE POLICY "read-only anon" ON wallet_stats FOR SELECT USING (true);

-- ── wallet_graph ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS wallet_graph (
    wallet_a           TEXT NOT NULL,
    wallet_b           TEXT NOT NULL,
    co_appearances     INTEGER NOT NULL DEFAULT 1,
    rug_co_appearances INTEGER NOT NULL DEFAULT 0,
    last_seen_together BIGINT NOT NULL,
    PRIMARY KEY (wallet_a, wallet_b),
    CHECK (wallet_a < wallet_b)
);

ALTER TABLE wallet_graph ENABLE ROW LEVEL SECURITY;
CREATE POLICY "read-only anon" ON wallet_graph FOR SELECT USING (true);

CREATE INDEX IF NOT EXISTS idx_wg_a ON wallet_graph(wallet_a);
CREATE INDEX IF NOT EXISTS idx_wg_b ON wallet_graph(wallet_b);

-- ── bc_accumulation ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS bc_accumulation (
    token_mint         TEXT NOT NULL REFERENCES tokens(mint),
    wallet_address     TEXT NOT NULL,
    first_buy_offset_s DOUBLE PRECISION,
    bc_buy_count       INTEGER NOT NULL DEFAULT 0,
    bc_sell_count      INTEGER NOT NULL DEFAULT 0,
    total_sol_in       DOUBLE PRECISION NOT NULL DEFAULT 0,
    accumulation_style TEXT,
    PRIMARY KEY (token_mint, wallet_address)
);
ALTER TABLE bc_accumulation ENABLE ROW LEVEL SECURITY;
CREATE POLICY "read-only anon" ON bc_accumulation FOR SELECT USING (true);
CREATE INDEX IF NOT EXISTS idx_bc_accum_token ON bc_accumulation(token_mint);

-- ── holder_snapshots ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS holder_snapshots (
    id                    BIGSERIAL PRIMARY KEY,
    token_mint            TEXT NOT NULL REFERENCES tokens(mint),
    checked_at            BIGINT NOT NULL,
    check_offset_h        INTEGER NOT NULL,
    holder_count          INTEGER,
    holder_count_is_total BOOLEAN NOT NULL DEFAULT FALSE,
    top10_pct             DOUBLE PRECISION,
    new_holder_count      INTEGER NOT NULL DEFAULT 0,
    churned_holder_count  INTEGER NOT NULL DEFAULT 0,
    new_smart_money_count INTEGER NOT NULL DEFAULT 0,
    top10_value_usd       DOUBLE PRECISION,
    UNIQUE (token_mint, check_offset_h)
);
ALTER TABLE holder_snapshots ENABLE ROW LEVEL SECURITY;
CREATE POLICY "read-only anon" ON holder_snapshots FOR SELECT USING (true);

-- For existing installs: add is_smart_money to post_grad_swaps
ALTER TABLE post_grad_swaps ADD COLUMN IF NOT EXISTS is_smart_money BOOLEAN NOT NULL DEFAULT FALSE;

-- ── dashboard view — one row per graduation with everything needed ─────────────
CREATE OR REPLACE VIEW graduation_feed AS
SELECT
    ge.token_mint,
    ge.graduated_at,
    ge.detection_lag_seconds,
    ge.structural_verdict       AS verdict,
    ge.verdict_confidence       AS confidence,
    ge.smart_money_count,
    ge.dominant_factors_json,
    ge.pumpswap_pool_address,
    t.symbol,
    t.name,
    tc.supply_pct_at_graduation,
    tc.is_bc_sniper,
    tc.funding_source,
    pgb_1h.distribution_signal   AS signal_1h,
    pgb_4h.distribution_signal   AS signal_4h,
    co_1h.classified             AS outcome_1h,
    co_4h.classified             AS outcome_4h,
    co_24h.classified            AS outcome_24h,
    fr.rug_rate                  AS funder_rug_rate,
    fr.is_known_rugger,
    pgb_24h.team_buy_count          AS team_buy_count_24h,
    pgb_24h.team_sell_count         AS team_sell_count_24h,
    pgb_24h.team_net_sol            AS team_net_sol_24h,
    pgb_24h.snipers_sold_pct        AS snipers_sold_pct_24h,
    pgb_24h.coordinated_sell_count  AS coordinated_sell_count_24h,
    pgb_24h.liquidity_usd           AS liquidity_usd_24h,
    hs_24h.holder_count             AS holder_count_24h,
    hs_24h.top10_pct                AS top10_pct_24h,
    hs_24h.new_holder_count         AS new_holder_count_24h,
    hs_24h.churned_holder_count     AS churned_holder_count_24h,
    hs_24h.new_smart_money_count    AS new_smart_money_count_24h,
    hs_24h.top10_value_usd          AS top10_value_usd_24h
FROM graduation_events ge
LEFT JOIN tokens t             ON t.mint              = ge.token_mint
LEFT JOIN team_clusters tc     ON tc.token_mint        = ge.token_mint
LEFT JOIN post_grad_behavior pgb_1h
                               ON pgb_1h.token_mint    = ge.token_mint
                              AND pgb_1h.check_offset_h = 1
LEFT JOIN post_grad_behavior pgb_4h
                               ON pgb_4h.token_mint    = ge.token_mint
                              AND pgb_4h.check_offset_h = 4
LEFT JOIN post_grad_behavior pgb_24h
                               ON pgb_24h.token_mint    = ge.token_mint
                              AND pgb_24h.check_offset_h = 24
LEFT JOIN coin_outcomes co_1h  ON co_1h.token_mint     = ge.token_mint
                              AND co_1h.check_offset_h  = 1
LEFT JOIN coin_outcomes co_4h  ON co_4h.token_mint     = ge.token_mint
                              AND co_4h.check_offset_h  = 4
LEFT JOIN coin_outcomes co_24h ON co_24h.token_mint    = ge.token_mint
                              AND co_24h.check_offset_h = 24
LEFT JOIN holder_snapshots hs_24h
                               ON hs_24h.token_mint     = ge.token_mint
                              AND hs_24h.check_offset_h  = 24
LEFT JOIN funder_reputation fr ON fr.funding_source    = tc.funding_source
ORDER BY ge.graduated_at DESC;
