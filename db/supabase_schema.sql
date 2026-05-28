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
    verdict_confidence      DOUBLE PRECISION
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
    distribution_signal      TEXT NOT NULL DEFAULT 'HOLDING'
                             CHECK (distribution_signal IN ('ACCUMULATING','HOLDING','DISTRIBUTING','DUMPED')),
    UNIQUE (token_mint, check_offset_h)
);

ALTER TABLE post_grad_behavior ENABLE ROW LEVEL SECURITY;
CREATE POLICY "read-only anon" ON post_grad_behavior FOR SELECT USING (true);

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

-- ── dashboard view — one row per graduation with everything needed ─────────────
CREATE OR REPLACE VIEW graduation_feed AS
SELECT
    ge.token_mint,
    ge.graduated_at,
    ge.detection_lag_seconds,
    ge.structural_verdict   AS verdict,
    ge.verdict_confidence   AS confidence,
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
    fr.is_known_rugger
FROM graduation_events ge
LEFT JOIN tokens t             ON t.mint              = ge.token_mint
LEFT JOIN team_clusters tc     ON tc.token_mint        = ge.token_mint
LEFT JOIN post_grad_behavior pgb_1h
                               ON pgb_1h.token_mint    = ge.token_mint
                              AND pgb_1h.check_offset_h = 1
LEFT JOIN post_grad_behavior pgb_4h
                               ON pgb_4h.token_mint    = ge.token_mint
                              AND pgb_4h.check_offset_h = 4
LEFT JOIN coin_outcomes co_1h  ON co_1h.token_mint     = ge.token_mint
                              AND co_1h.check_offset_h  = 1
LEFT JOIN coin_outcomes co_4h  ON co_4h.token_mint     = ge.token_mint
                              AND co_4h.check_offset_h  = 4
LEFT JOIN coin_outcomes co_24h ON co_24h.token_mint    = ge.token_mint
                              AND co_24h.check_offset_h = 24
LEFT JOIN funder_reputation fr ON fr.funding_source    = tc.funding_source
ORDER BY ge.graduated_at DESC;
