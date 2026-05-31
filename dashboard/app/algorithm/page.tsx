"use client";

import { useEffect, useState } from "react";
import { supabase, isConfigured } from "@/lib/supabase";
import { ALGO_VARIABLES, VERDICT_RULE, MATURITY_THRESHOLDS, type AlgoVariable, type Category } from "@/lib/algorithmConfig";

interface DataCounts {
  total_graduated: number;
  with_verdict: number;
  sound: number;
  watch: number;
  skip: number;
  outcomes_4h: number;
  outcomes_24h: number;
  wallet_graph_pairs: number;
  funders_8plus: number;
  wallets_15plus: number;
  team_supply_low: number;   // <20%
  team_supply_mid: number;   // 20-50%
  team_supply_high: number;  // >50%
  dist_holding: number;
  dist_distributing: number;
  dist_accumulating: number;
  dist_dumped: number;
}

const EMPTY: DataCounts = {
  total_graduated: 0, with_verdict: 0, sound: 0, watch: 0, skip: 0,
  outcomes_4h: 0, outcomes_24h: 0, wallet_graph_pairs: 0,
  funders_8plus: 0, wallets_15plus: 0,
  team_supply_low: 0, team_supply_mid: 0, team_supply_high: 0,
  dist_holding: 0, dist_distributing: 0, dist_accumulating: 0, dist_dumped: 0,
};

export default function AlgorithmPage() {
  const [counts, setCounts] = useState<DataCounts>(EMPTY);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<Category | "all">("all");

  useEffect(() => {
    if (!isConfigured) { setLoading(false); return; }
    fetchCounts();
  }, []);

  async function fetchCounts() {
    const [grads, co4h, co24h, wg, fr, ws, tc, pgb] = await Promise.all([
      supabase.from("graduation_events").select("structural_verdict", { count: "exact" }),
      supabase.from("coin_outcomes").select("*", { count: "exact" }).eq("check_offset_h", 4),
      supabase.from("coin_outcomes").select("*", { count: "exact" }).eq("check_offset_h", 24),
      supabase.from("wallet_graph").select("*", { count: "exact" }),
      supabase.from("funder_reputation").select("*").gte("rug_count", 0),  // all funders
      supabase.from("wallet_stats").select("total_calls").gte("total_calls", 15),
      supabase.from("team_clusters").select("supply_pct_at_graduation"),
      supabase.from("post_grad_behavior").select("distribution_signal"),
    ]);

    const verdicts = (grads.data || []) as { structural_verdict: string | null }[];
    const tcData = (tc.data || []) as { supply_pct_at_graduation: number }[];
    const pgbData = (pgb.data || []) as { distribution_signal: string }[];
    const frData = (fr.data || []) as { rug_count: number; moon_count: number; ok_count: number }[];

    setCounts({
      total_graduated: grads.count ?? 0,
      with_verdict: verdicts.filter(v => v.structural_verdict !== null).length,
      sound: verdicts.filter(v => v.structural_verdict === "STRUCTURALLY_SOUND").length,
      watch: verdicts.filter(v => v.structural_verdict === "WATCH").length,
      skip: verdicts.filter(v => v.structural_verdict === "SKIP").length,
      outcomes_4h: co4h.count ?? 0,
      outcomes_24h: co24h.count ?? 0,
      wallet_graph_pairs: wg.count ?? 0,
      funders_8plus: frData.filter(f => (f.rug_count + f.moon_count + f.ok_count) >= 8).length,
      wallets_15plus: ws.data?.length ?? 0,
      team_supply_low: tcData.filter(t => t.supply_pct_at_graduation < 20).length,
      team_supply_mid: tcData.filter(t => t.supply_pct_at_graduation >= 20 && t.supply_pct_at_graduation <= 50).length,
      team_supply_high: tcData.filter(t => t.supply_pct_at_graduation > 50).length,
      dist_holding: pgbData.filter(p => p.distribution_signal === "HOLDING").length,
      dist_distributing: pgbData.filter(p => p.distribution_signal === "DISTRIBUTING").length,
      dist_accumulating: pgbData.filter(p => p.distribution_signal === "ACCUMULATING").length,
      dist_dumped: pgbData.filter(p => p.distribution_signal === "DUMPED").length,
    });
    setLoading(false);
  }

  const matureSignals = ALGO_VARIABLES.filter(v => getObservations(v, counts) >= v.minSample).length;
  const filtered = filter === "all" ? ALGO_VARIABLES : ALGO_VARIABLES.filter(v => v.category === filter);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-zinc-100 text-2xl font-bold tracking-tight">Algorithm State</h1>
        <p className="text-zinc-500 text-sm mt-1">
          {ALGO_VARIABLES.length} variables · {counts.total_graduated} graduations observed · {matureSignals} signals above minimum sample size
        </p>
      </div>

      {/* Data summary */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <StatCard label="Graduated" value={counts.total_graduated} sub="total analysed" />
        <StatCard label="4h Outcomes" value={counts.outcomes_4h} sub={`of ${counts.total_graduated}`} pct={counts.total_graduated > 0 ? counts.outcomes_4h / counts.total_graduated : 0} />
        <StatCard label="24h Outcomes" value={counts.outcomes_24h} sub={`of ${counts.total_graduated}`} pct={counts.total_graduated > 0 ? counts.outcomes_24h / counts.total_graduated : 0} />
        <StatCard label="Mature Signals" value={matureSignals} sub={`of ${ALGO_VARIABLES.length} variables`} pct={matureSignals / ALGO_VARIABLES.length} />
      </div>

      {/* Maturity milestone bars */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
        <p className="text-zinc-400 text-xs uppercase tracking-wide mb-3">Signal maturity milestones</p>
        <div className="space-y-2.5">
          <MilestoneBar label="Pattern buckets (30 each)" current={counts.outcomes_4h} target={30} unit="4h outcomes" />
          <MilestoneBar label="Wallet win rates (15 trades)" current={counts.wallets_15plus} target={10} unit="mature wallets" />
          <MilestoneBar label="Funder reputation (8 launches)" current={counts.funders_8plus} target={5} unit="known funders" />
          <MilestoneBar label="Wallet graph (2 co-appearances)" current={counts.wallet_graph_pairs} target={20} unit="graph edges" />
        </div>
      </div>

      {/* Verdict logic */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
        <p className="text-zinc-400 text-xs uppercase tracking-wide mb-3">Verdict decision rules</p>
        <div className="grid grid-cols-3 gap-3 text-sm">
          <div className="bg-green-900/30 border border-green-800 rounded p-3">
            <p className="text-green-300 font-mono font-semibold mb-1">SOUND</p>
            <p className="text-zinc-400 text-xs">{VERDICT_RULE.sound}</p>
          </div>
          <div className="bg-yellow-900/30 border border-yellow-800 rounded p-3">
            <p className="text-yellow-300 font-mono font-semibold mb-1">WATCH</p>
            <p className="text-zinc-400 text-xs">{VERDICT_RULE.watch}</p>
          </div>
          <div className="bg-red-900/30 border border-red-800 rounded p-3">
            <p className="text-red-300 font-mono font-semibold mb-1">SKIP</p>
            <p className="text-zinc-400 text-xs">{VERDICT_RULE.skip}</p>
          </div>
        </div>
      </div>

      {/* Variable filters */}
      <div className="flex gap-2 flex-wrap">
        {(["all", "hard_skip", "positive", "negative", "memory"] as const).map(f => {
          const labels: Record<string, string> = { all: "All", hard_skip: "Hard SKIP", positive: "Positive", negative: "Negative", memory: "Memory" };
          const activeColors: Record<string, string> = { all: "bg-zinc-700 text-white", hard_skip: "bg-red-900 text-red-200", positive: "bg-green-900 text-green-200", negative: "bg-yellow-900 text-yellow-200", memory: "bg-purple-900 text-purple-200" };
          const count = f === "all" ? ALGO_VARIABLES.length : ALGO_VARIABLES.filter(v => v.category === f).length;
          return (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-3 py-1 rounded-full text-xs font-mono transition-colors ${filter === f ? activeColors[f] : "bg-zinc-900 text-zinc-500 hover:text-zinc-300 border border-zinc-800"}`}
            >
              {labels[f]} <span className="opacity-60">{count}</span>
            </button>
          );
        })}
      </div>

      {/* Variables list */}
      <div className="space-y-2">
        {filtered.map(v => (
          <VariableCard key={v.id} variable={v} observations={getObservations(v, counts)} loading={loading} />
        ))}
      </div>
    </div>
  );
}


// ── sub-components ────────────────────────────────────────────────────────────

function StatCard({ label, value, sub, pct }: { label: string; value: number; sub: string; pct?: number }) {
  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3">
      <p className="text-zinc-500 text-xs uppercase tracking-wide mb-1">{label}</p>
      <p className="text-white text-2xl font-mono font-bold">{value}</p>
      <p className="text-zinc-600 text-xs mt-0.5">{sub}</p>
      {pct !== undefined && (
        <div className="mt-2 bg-zinc-800 rounded-full h-1">
          <div className="bg-zinc-500 h-1 rounded-full" style={{ width: `${Math.min(100, pct * 100)}%` }} />
        </div>
      )}
    </div>
  );
}

function MilestoneBar({ label, current, target, unit }: { label: string; current: number; target: number; unit: string }) {
  const pct = Math.min(1, current / target);
  const mature = current >= target;
  return (
    <div className="flex items-center gap-3">
      <div className={`w-1.5 h-1.5 rounded-full shrink-0 ${mature ? "bg-green-500" : "bg-zinc-600"}`} />
      <span className="text-zinc-400 text-xs w-64 truncate">{label}</span>
      <div className="flex-1 bg-zinc-800 rounded-full h-1.5">
        <div className={`h-1.5 rounded-full ${mature ? "bg-green-500" : "bg-zinc-500"}`} style={{ width: `${pct * 100}%` }} />
      </div>
      <span className={`text-xs font-mono w-24 text-right ${mature ? "text-green-400" : "text-zinc-500"}`}>
        {current} / {target} {unit.split(" ")[0]}
      </span>
    </div>
  );
}

function VariableCard({ variable: v, observations, loading }: { variable: AlgoVariable; observations: number; loading: boolean }) {
  const mature = observations >= v.minSample;
  const growing = observations > 0 && !mature;

  const categoryColors: Record<string, string> = {
    hard_skip: "border-red-900 bg-red-950/10",
    positive: "border-green-900 bg-green-950/10",
    negative: "border-yellow-900 bg-yellow-950/10",
    memory: "border-purple-900 bg-purple-950/10",
  };

  const impactColors: Record<string, string> = {
    SKIP: "text-red-400 bg-red-900/40",
    "+2": "text-green-300 bg-green-900/40",
    "+1": "text-green-500 bg-green-900/30",
    "-1": "text-yellow-400 bg-yellow-900/30",
    "-2": "text-orange-400 bg-orange-900/30",
    info: "text-zinc-400 bg-zinc-800/50",
  };

  const maturityDot = mature
    ? "bg-green-500 shadow-[0_0_6px_rgba(34,197,94,0.6)]"
    : growing ? "bg-yellow-500" : "bg-zinc-700";

  const maturityLabel = mature ? "mature" : growing ? "growing" : "cold";

  return (
    <div className={`rounded-lg border ${categoryColors[v.category]} p-3`}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            <span className={`inline-flex px-2 py-0.5 rounded text-xs font-mono font-bold ${impactColors[v.impact]}`}>
              {v.impact}
            </span>
            <span className="text-zinc-100 text-sm font-semibold">{v.name}</span>
            {!v.hardcoded && (
              <span className="text-purple-400 text-xs font-mono opacity-60">data-derived</span>
            )}
          </div>
          <p className="text-zinc-500 text-xs">{v.description}</p>
          <p className="text-zinc-700 text-xs font-mono mt-1">{v.threshold}</p>
        </div>

        <div className="flex flex-col items-end gap-1 shrink-0">
          <div className="flex items-center gap-1.5">
            <div className={`w-1.5 h-1.5 rounded-full ${maturityDot}`} />
            <span className={`text-xs font-mono ${mature ? "text-green-400" : growing ? "text-yellow-400" : "text-zinc-600"}`}>
              {maturityLabel}
            </span>
          </div>
          <span className="text-zinc-600 text-xs font-mono">
            {loading ? "…" : `${observations} / ${v.minSample} obs`}
          </span>
        </div>
      </div>
    </div>
  );
}


// ── helpers ───────────────────────────────────────────────────────────────────

function getObservations(v: AlgoVariable, counts: DataCounts): number {
  switch (v.id) {
    case "known_rugger":
    case "good_funder":
    case "partial_rugger":
      return counts.funders_8plus;
    case "sm_2plus":
    case "sm_1":
      return counts.wallets_15plus;
    case "team_holding":
      return counts.dist_holding;
    case "team_accumulating":
      return counts.dist_accumulating;
    case "team_distributing":
      return counts.dist_distributing;
    case "already_dumped":
      return counts.dist_dumped;
    case "low_team_supply":
      return counts.team_supply_low;
    case "sniper_heavy":
    case "top_holder_high":
    case "top_holder_mid":
    case "top3_bundled":
      return counts.team_supply_high + counts.team_supply_mid;
    case "graduation_push":
    case "bundled_graduation":
    case "bc_very_fast":
    case "bc_fast":
    case "low_bc_buyers":
      return counts.with_verdict;
    case "wallet_graph_rug":
    case "graph_soft":
      return counts.wallet_graph_pairs;
    case "fingerprint_match":
      return counts.funders_8plus;
    case "pump_ring_24h":
    case "pump_ring_7d":
    case "dump_timing":
      return counts.outcomes_4h;
    default:
      return counts.with_verdict;
  }
}
