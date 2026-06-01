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
}

export type Database = {
  public: {
    Tables: {
      graduation_events: { Row: Record<string, unknown> };
      team_clusters: { Row: { supply_pct_at_graduation: number } };
      coin_outcomes: { Row: { check_offset_h: number; classified: string | null } };
      post_grad_behavior: { Row: { distribution_signal: string } };
      funder_reputation: { Row: { rug_count: number; moon_count: number; ok_count: number } };
      wallet_stats: { Row: { total_calls: number } };
      wallet_graph: { Row: { wallet_a: string; wallet_b: string; co_appearances: number; rug_co_appearances: number } };
      post_grad_swaps: { Row: PostGradSwap };
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
