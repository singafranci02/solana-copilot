// Matches the graduation_feed view in supabase_schema.sql

export type Verdict = "SKIP" | "WATCH" | "STRUCTURALLY_SOUND";
export type Outcome = "moon" | "ok" | "rug";
export type DistributionSignal = "ACCUMULATING" | "HOLDING" | "DISTRIBUTING" | "DUMPED";

export interface GraduationRow {
  token_mint: string;
  graduated_at: number;          // unix epoch seconds
  detection_lag_seconds: number;
  verdict: Verdict | null;
  confidence: number | null;
  smart_money_count: number;
  dominant_factors_json: string[] | null;  // Supabase returns JSONB as parsed array
  pumpswap_pool_address: string | null;
  symbol: string | null;
  name: string | null;
  supply_pct_at_graduation: number | null;
  is_bc_sniper: boolean | null;
  funding_source: string | null;
  signal_1h: DistributionSignal | null;
  signal_4h: DistributionSignal | null;
  outcome_1h: Outcome | null;
  outcome_4h: Outcome | null;
  outcome_24h: Outcome | null;
  funder_rug_rate: number | null;
  is_known_rugger: boolean | null;
  team_buy_count_24h: number | null;
  team_sell_count_24h: number | null;
  team_net_sol_24h: number | null;
  snipers_sold_pct_24h: number | null;
  coordinated_sell_count_24h: number | null;
  liquidity_usd_24h: number | null;
  holder_count_24h: number | null;
  top10_pct_24h: number | null;
  new_holder_count_24h: number | null;
  churned_holder_count_24h: number | null;
  new_smart_money_count_24h: number | null;
  top10_value_usd_24h: number | null;
  // pipeline provenance
  pipeline_version: number | null;
  creator_wallet: string | null;
  // microstructure (Phase B)
  launch_slot_snipe_count: number | null;
  buys_first_3_slots: number | null;
  max_same_slot_group: number | null;
  bundled_adjacent_count: number | null;
  bc_n_buyers: number | null;
  top5_buyer_share: number | null;
  gini_buy_size: number | null;
  // launch coordination
  launch_bundled_pct: number | null;
  launch_entity_count: number | null;
  launch_largest_entity_pct: number | null;
  // team-scoring evidence (Phase A)
  member_count: number | null;
  candidate_count: number | null;
  max_score: number | null;
  creator_linked_count: number | null;
  // serial-deployer reputation
  creator_rug_rate: number | null;
  is_serial_rugger: boolean | null;
  creator_n: number | null;
}

export interface TeamMember {
  token_mint: string;
  wallet: string;
  score: number;
  is_member: boolean;
  evidence_json: Record<string, unknown>;
}

export interface BcAccumulation {
  token_mint: string;
  wallet_address: string;
  first_buy_offset_s: number | null;
  bc_buy_count: number;
  bc_sell_count: number;
  total_sol_in: number;
  accumulation_style: "sniped" | "gradual" | "single" | null;
}

export interface CoinCoordination {
  token_mint: string;
  phase?: string;
  entity_count: number;
  bundled_supply_pct: number;
  bundle_wallet_count: number;
  largest_bundle_size: number;
  largest_entity_supply_pct: number;
  largest_entity_wallet_count: number;
  largest_entity_fresh_ratio: number;
  largest_entity_state: string | null;
}

export interface CoordinatedEntity {
  token_mint: string;
  entity_id: string;
  member_addresses: string[];
  wallet_count: number;
  supply_pct: number;
  fresh_ratio: number;
  state: string | null;
  edge_sources: string[];
}

export interface PostGradSwap {
  token_mint: string;
  wallet_address: string;
  side: "buy" | "sell";
  sol_amount: number;
  token_amount: number;
  price_sol: number | null;
  ts: number;
  slot: number;
  is_sniper: boolean;
  is_team: boolean;
  is_smart_money?: boolean;
}

export type Database = {
  public: {
    Tables: {
      graduation_events: { Row: Record<string, unknown> };
      team_clusters: { Row: { supply_pct_at_graduation: number } };
      coin_outcomes: { Row: { check_offset_h: number; classified: string | null; checked_at: number } };
      post_grad_behavior: { Row: { distribution_signal: string } };
      funder_reputation: { Row: { rug_count: number; moon_count: number; ok_count: number } };
      wallet_stats: { Row: { total_calls: number } };
      wallet_graph: { Row: { wallet_a: string; wallet_b: string; co_appearances: number; rug_co_appearances: number } };
      post_grad_swaps: { Row: PostGradSwap };
      bc_accumulation: { Row: BcAccumulation };
      holder_snapshots: { Row: Record<string, unknown> };
      coin_coordination: { Row: CoinCoordination };
      coordinated_entities: { Row: CoordinatedEntity };
      team_members: { Row: TeamMember };
      wallet_behavior: { Row: Record<string, unknown> };
      bc_flow_features: { Row: Record<string, unknown> };
      creator_reputation: { Row: Record<string, unknown> };
      api_usage: { Row: { day: string; provider: string; endpoint: string; count: number } };
      mirror_counts: { Row: { metric: string; value: number; updated_at: number } };
      bc_microstructure: { Row: Record<string, unknown> };
      team_member_behavior: { Row: Record<string, unknown> };
      team_fingerprints: { Row: Record<string, unknown> };
      tokens: { Row: Record<string, unknown> };
    };
    Views: {
      graduation_feed: { Row: GraduationRow };
    };
  };
};

// ── helpers ────────────────────────────────────────────────────────────────────

export function verdictLabel(v: Verdict | null): string {
  if (v === "STRUCTURALLY_SOUND") return "SOUND";
  return v ?? "—";
}

export function formatMint(mint: string): string {
  return mint.slice(0, 4) + "…" + mint.slice(-4);
}

export function formatTs(epoch: number): string {
  return new Date(epoch * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatDate(epoch: number): string {
  return new Date(epoch * 1000).toLocaleDateString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
