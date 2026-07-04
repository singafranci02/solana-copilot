-- Dashboard rehaul migration — run this ALONE in the Supabase SQL editor.
-- Do NOT re-run the whole supabase_schema.sql: it re-issues CREATE POLICY on
-- pre-existing tables (e.g. "tokens"), which errors. This file only creates the
-- new objects and is fully idempotent (DROP POLICY IF EXISTS before each CREATE).

-- ── new tables ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS team_members (
    token_mint    TEXT NOT NULL,
    wallet        TEXT NOT NULL,
    score         DOUBLE PRECISION NOT NULL,
    is_member     BOOLEAN NOT NULL DEFAULT FALSE,
    evidence_json JSONB NOT NULL DEFAULT '{}',
    computed_at   BIGINT,
    PRIMARY KEY (token_mint, wallet)
);
CREATE INDEX IF NOT EXISTS idx_team_members_wallet ON team_members(wallet);

CREATE TABLE IF NOT EXISTS wallet_behavior (
    address                TEXT PRIMARY KEY,
    n_coins_bc             INTEGER NOT NULL DEFAULT 0,
    sniper_rate            DOUBLE PRECISION,
    avg_first_buy_offset_s DOUBLE PRECISION, std_first_buy_offset_s DOUBLE PRECISION,
    avg_buy_size_sol       DOUBLE PRECISION, cv_buy_size DOUBLE PRECISION,
    pct_sniped             DOUBLE PRECISION, pct_gradual DOUBLE PRECISION, pct_single DOUBLE PRECISION,
    avg_hold_duration_s    DOUBLE PRECISION,
    exit_one_shot_frac     DOUBLE PRECISION,
    avg_exit_order         DOUBLE PRECISION,
    n_coins_exit           INTEGER NOT NULL DEFAULT 0,
    pnl_proxy              DOUBLE PRECISION,
    avg_slot_reaction      DOUBLE PRECISION,
    sig_count              INTEGER, wallet_age_days DOUBLE PRECISION,
    last_updated           BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bc_flow_features (
    token_mint                   TEXT PRIMARY KEY,
    n_trades INTEGER, n_buyers INTEGER, n_sellers INTEGER,
    buys_first_60s INTEGER, same_second_bundle_count INTEGER,
    top5_buyer_share DOUBLE PRECISION, gini_buy_size DOUBLE PRECISION,
    sol_in DOUBLE PRECISION, sol_out DOUBLE PRECISION,
    launch_slot_snipe_count INTEGER, buys_first_slot INTEGER,
    buys_first_3_slots INTEGER, distinct_slots_first_20_buys INTEGER,
    max_same_slot_group INTEGER, bundled_adjacent_count INTEGER
);

CREATE TABLE IF NOT EXISTS creator_reputation (
    creator_wallet   TEXT PRIMARY KEY,
    graduated_mints  JSONB NOT NULL DEFAULT '[]',
    rug_count INTEGER NOT NULL DEFAULT 0, moon_count INTEGER NOT NULL DEFAULT 0,
    ok_count INTEGER NOT NULL DEFAULT 0, rug_rate DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_seen BIGINT, is_serial_rugger BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS api_usage (
    day TEXT NOT NULL, provider TEXT NOT NULL, endpoint TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, provider, endpoint)
);

-- Exit choreography (Phase D) — who sells first, in what order.
CREATE TABLE IF NOT EXISTS team_member_behavior (
    token_mint          TEXT NOT NULL,
    wallet              TEXT NOT NULL,
    exit_order          INTEGER,
    first_sell_offset_s DOUBLE PRECISION,
    sold_pct_1h DOUBLE PRECISION, sold_pct_4h DOUBLE PRECISION, sold_pct_24h DOUBLE PRECISION,
    is_first_seller     BOOLEAN NOT NULL DEFAULT FALSE,
    participated_coordinated_sell BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at          BIGINT,
    PRIMARY KEY (token_mint, wallet)
);
CREATE INDEX IF NOT EXISTS idx_tmb_wallet ON team_member_behavior(wallet);

-- ── RLS + read-only anon policy (idempotent) ──────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['team_members','wallet_behavior','bc_flow_features','creator_reputation','api_usage','team_member_behavior']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS "read-only anon" ON %I', t);
        EXECUTE format('CREATE POLICY "read-only anon" ON %I FOR SELECT USING (true)', t);
    END LOOP;
END $$;

-- ── extended feed view ────────────────────────────────────────────────────────
DROP VIEW IF EXISTS graduation_feed;
CREATE VIEW graduation_feed AS
SELECT
    ge.token_mint, ge.graduated_at, ge.detection_lag_seconds,
    ge.structural_verdict AS verdict, ge.verdict_confidence AS confidence,
    ge.smart_money_count, ge.dominant_factors_json, ge.pumpswap_pool_address,
    ge.pipeline_version,
    t.symbol, t.name, t.creator_wallet,
    tc.supply_pct_at_graduation, tc.is_bc_sniper, tc.funding_source,
    pgb_1h.distribution_signal AS signal_1h,
    pgb_4h.distribution_signal AS signal_4h,
    co_1h.classified AS outcome_1h, co_4h.classified AS outcome_4h,
    co_24h.classified AS outcome_24h,
    fr.rug_rate AS funder_rug_rate, fr.is_known_rugger,
    pgb_24h.team_buy_count AS team_buy_count_24h,
    pgb_24h.team_sell_count AS team_sell_count_24h,
    pgb_24h.team_net_sol AS team_net_sol_24h,
    pgb_24h.snipers_sold_pct AS snipers_sold_pct_24h,
    pgb_24h.coordinated_sell_count AS coordinated_sell_count_24h,
    pgb_24h.liquidity_usd AS liquidity_usd_24h,
    hs_24h.holder_count AS holder_count_24h,
    hs_24h.top10_pct AS top10_pct_24h,
    hs_24h.new_holder_count AS new_holder_count_24h,
    hs_24h.churned_holder_count AS churned_holder_count_24h,
    hs_24h.new_smart_money_count AS new_smart_money_count_24h,
    hs_24h.top10_value_usd AS top10_value_usd_24h,
    bff.launch_slot_snipe_count, bff.buys_first_3_slots,
    bff.max_same_slot_group, bff.bundled_adjacent_count,
    bff.n_buyers AS bc_n_buyers, bff.top5_buyer_share, bff.gini_buy_size,
    ccl.bundled_supply_pct AS launch_bundled_pct,
    ccl.entity_count AS launch_entity_count,
    ccl.largest_entity_supply_pct AS launch_largest_entity_pct,
    tm.member_count, tm.candidate_count, tm.max_score, tm.creator_linked_count,
    cr.rug_rate AS creator_rug_rate, cr.is_serial_rugger,
    COALESCE(jsonb_array_length(cr.graduated_mints), 0) AS creator_n
FROM graduation_events ge
LEFT JOIN tokens t             ON t.mint = ge.token_mint
LEFT JOIN team_clusters tc     ON tc.token_mint = ge.token_mint
LEFT JOIN post_grad_behavior pgb_1h  ON pgb_1h.token_mint = ge.token_mint AND pgb_1h.check_offset_h = 1
LEFT JOIN post_grad_behavior pgb_4h  ON pgb_4h.token_mint = ge.token_mint AND pgb_4h.check_offset_h = 4
LEFT JOIN post_grad_behavior pgb_24h ON pgb_24h.token_mint = ge.token_mint AND pgb_24h.check_offset_h = 24
LEFT JOIN coin_outcomes co_1h  ON co_1h.token_mint = ge.token_mint AND co_1h.check_offset_h = 1
LEFT JOIN coin_outcomes co_4h  ON co_4h.token_mint = ge.token_mint AND co_4h.check_offset_h = 4
LEFT JOIN coin_outcomes co_24h ON co_24h.token_mint = ge.token_mint AND co_24h.check_offset_h = 24
LEFT JOIN holder_snapshots hs_24h ON hs_24h.token_mint = ge.token_mint AND hs_24h.check_offset_h = 24
LEFT JOIN funder_reputation fr ON fr.funding_source = tc.funding_source
LEFT JOIN bc_flow_features bff ON bff.token_mint = ge.token_mint
LEFT JOIN coin_coordination ccl ON ccl.token_mint = ge.token_mint AND ccl.phase = 'launch'
LEFT JOIN creator_reputation cr ON cr.creator_wallet = t.creator_wallet
LEFT JOIN (
    SELECT token_mint,
           COUNT(*) FILTER (WHERE is_member) AS member_count,
           COUNT(*) AS candidate_count,
           MAX(score) AS max_score,
           COUNT(*) FILTER (WHERE evidence_json->>'funding' = 'creator_linked') AS creator_linked_count
    FROM team_members GROUP BY token_mint
) tm ON tm.token_mint = ge.token_mint
ORDER BY ge.graduated_at DESC;
