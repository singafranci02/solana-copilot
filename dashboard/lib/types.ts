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
}

export type Database = {
  public: {
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
