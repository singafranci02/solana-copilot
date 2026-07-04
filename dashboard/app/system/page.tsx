"use client";

import { useEffect, useState } from "react";
import { supabase, isConfigured } from "@/lib/supabase";

export const dynamic = "force-dynamic";

const ST_MONTHLY_BUDGET = 200_000;

interface Sys {
  lastGradTs: number | null;
  grads24h: number;
  totalGrads: number;
  v2Grads: number;
  stMonth: number;
  rpcToday: number;
  stToday: number;
  // pipeline stage counts + freshness
  teamScored: number;         // graduations with scored team members
  microResolved: number;      // graduations with slot microstructure
  coordLaunch: number;
  outcomes4h: number;
  outcomes24h: number;
  lastOutcomeTs: number | null;
  // behavioral engine proof
  slotSnipesTotal: number;
  bundlesTotal: number;
  creatorLinkedTotal: number;
  // learning maturity
  walletFp3: number;
  funders8: number;
  walletGraph: number;
  choreoFunders: number;
  // verdict accuracy
  resolved: number;
  correct: number;
}

const EMPTY: Sys = {
  lastGradTs: null, grads24h: 0, totalGrads: 0, v2Grads: 0,
  stMonth: 0, rpcToday: 0, stToday: 0,
  teamScored: 0, microResolved: 0, coordLaunch: 0, outcomes4h: 0, outcomes24h: 0,
  lastOutcomeTs: null, slotSnipesTotal: 0, bundlesTotal: 0, creatorLinkedTotal: 0,
  walletFp3: 0, funders8: 0, walletGraph: 0, choreoFunders: 0, resolved: 0, correct: 0,
};

export default function SystemPage() {
  const [s, setS] = useState<Sys>(EMPTY);
  const [loading, setLoading] = useState(true);
  const [now, setNow] = useState(Math.floor(Date.now() / 1000));

  useEffect(() => {
    if (!isConfigured) { setLoading(false); return; }
    load();
    const iv = setInterval(() => { load(); setNow(Math.floor(Date.now() / 1000)); }, 60_000);
    return () => clearInterval(iv);
  }, []);

  async function load() {
    const cut24 = Math.floor(Date.now() / 1000) - 86_400;
    const monthStart = new Date().toISOString().slice(0, 8) + "01";
    const today = new Date().toISOString().slice(0, 10);
    const cnt = (q: any) => q.then((r: any) => r.count ?? 0);

    const [
      latest, grads24, total, v2, stMonthRows, apiToday,
      teamScored, micro, coord, o4h, o24h, lastO,
      microAgg, creatorLinked, wfp, funders, wg, choreo, feed,
    ] = await Promise.all([
      supabase.from("graduation_events").select("graduated_at").order("graduated_at", { ascending: false }).limit(1),
      cnt(supabase.from("graduation_events").select("*", { count: "exact", head: true }).gte("graduated_at", cut24)),
      cnt(supabase.from("graduation_events").select("*", { count: "exact", head: true })),
      cnt(supabase.from("graduation_events").select("*", { count: "exact", head: true }).gte("pipeline_version", 2)),
      supabase.from("api_usage").select("provider,count").eq("provider", "solana_tracker").gte("day", monthStart),
      supabase.from("api_usage").select("provider,count,day").eq("day", today),
      cnt(supabase.from("team_members").select("token_mint", { count: "exact", head: true }).eq("is_member", true)),
      cnt(supabase.from("bc_flow_features").select("*", { count: "exact", head: true }).not("launch_slot_snipe_count", "is", null)),
      cnt(supabase.from("coin_coordination").select("*", { count: "exact", head: true }).eq("phase", "launch")),
      cnt(supabase.from("coin_outcomes").select("*", { count: "exact", head: true }).eq("check_offset_h", 4)),
      cnt(supabase.from("coin_outcomes").select("*", { count: "exact", head: true }).eq("check_offset_h", 24)),
      supabase.from("coin_outcomes").select("checked_at").order("checked_at", { ascending: false }).limit(1),
      supabase.from("bc_flow_features").select("launch_slot_snipe_count,bundled_adjacent_count"),
      cnt(supabase.from("team_members").select("*", { count: "exact", head: true }).eq("is_member", true).filter("evidence_json->>funding", "eq", "creator_linked")),
      cnt(supabase.from("wallet_behavior").select("*", { count: "exact", head: true }).gte("n_coins_bc", 3)),
      supabase.from("funder_reputation").select("rug_count,moon_count,ok_count"),
      cnt(supabase.from("wallet_graph").select("*", { count: "exact", head: true })),
      supabase.from("team_member_behavior").select("token_mint").not("exit_order", "is", null),
      supabase.from("graduation_feed").select("verdict,outcome_24h").not("outcome_24h", "is", null).limit(3000),
    ]);

    const stMonth = ((stMonthRows.data as { count: number }[]) || []).reduce((a, r) => a + r.count, 0);
    const apiT = (apiToday.data as { provider: string; count: number }[]) || [];
    const stToday = apiT.filter(r => r.provider === "solana_tracker").reduce((a, r) => a + r.count, 0);
    const rpcToday = apiT.filter(r => r.provider === "rpc").reduce((a, r) => a + r.count, 0);
    const microRows = (microAgg.data as any[]) || [];
    const funderRows = (funders.data as any[]) || [];
    const choreoMints = new Set(((choreo.data as { token_mint: string }[]) || []).map(r => r.token_mint)).size;
    const feedRows = (feed.data as { verdict: string | null; outcome_24h: string | null }[]) || [];
    const correct = feedRows.filter(r =>
      r.verdict && ((r.verdict === "SKIP" && r.outcome_24h === "rug") || (r.verdict !== "SKIP" && r.outcome_24h !== "rug"))
    ).length;

    setS({
      lastGradTs: (latest.data?.[0] as { graduated_at: number } | undefined)?.graduated_at ?? null,
      grads24h: grads24, totalGrads: total, v2Grads: v2,
      stMonth, rpcToday, stToday,
      teamScored, microResolved: micro, coordLaunch: coord, outcomes4h: o4h, outcomes24h: o24h,
      lastOutcomeTs: (lastO.data?.[0] as { checked_at: number } | undefined)?.checked_at ?? null,
      slotSnipesTotal: microRows.reduce((a, r) => a + (r.launch_slot_snipe_count || 0), 0),
      bundlesTotal: microRows.reduce((a, r) => a + (r.bundled_adjacent_count || 0), 0),
      creatorLinkedTotal: creatorLinked,
      walletFp3: wfp,
      funders8: funderRows.filter(f => (f.rug_count + f.moon_count + f.ok_count) >= 8).length,
      walletGraph: wg, choreoFunders: choreoMints,
      resolved: feedRows.length, correct,
    });
    setLoading(false);
  }

  if (!isConfigured) return <Notice title="Supabase not connected" body="Add NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY in Vercel." />;

  const lagMin = s.lastGradTs ? Math.floor((now - s.lastGradTs) / 60) : null;
  const status = lagMin === null ? "unknown" : lagMin < 60 ? "live" : lagMin < 180 ? "quiet" : "stalled";
  const outcomeLagMin = s.lastOutcomeTs ? Math.floor((now - s.lastOutcomeTs) / 60) : null;
  const stPct = s.stMonth / ST_MONTHLY_BUDGET;
  const accuracy = s.resolved > 0 ? s.correct / s.resolved : 0;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-zinc-100 text-2xl font-bold tracking-tight">System</h1>
        <p className="text-zinc-500 text-sm mt-1">
          Live proof the pipeline is running end-to-end — every stage, the API budget, and how the
          behavioral engine is performing. Refreshes each minute.
        </p>
      </div>

      {/* Status strip */}
      <div className="flex items-center gap-4 flex-wrap text-sm font-mono bg-zinc-900 border border-zinc-800 rounded-lg px-4 py-3">
        <span className="flex items-center gap-2">
          <span className={`w-2 h-2 rounded-full ${status === "live" ? "bg-green-500" : status === "quiet" ? "bg-yellow-500" : status === "stalled" ? "bg-red-500 animate-pulse" : "bg-zinc-600"}`} />
          <span className={status === "live" ? "text-green-400" : status === "quiet" ? "text-yellow-400" : status === "stalled" ? "text-red-400" : "text-zinc-500"}>
            graduation-monitor {status === "stalled" ? "STALLED" : status}
          </span>
        </span>
        {lagMin !== null && <Sep />}
        {lagMin !== null && <span className="text-zinc-500">last graduation <span className="text-zinc-300">{fmtAgo(lagMin)}</span> ago</span>}
        <Sep />
        <span className="text-zinc-500"><span className="text-zinc-300">{s.grads24h}</span> grads / 24h</span>
        <Sep />
        <span className="text-zinc-500">outcome tracker {outcomeLagMin !== null ? <span className="text-zinc-300">{fmtAgo(outcomeLagMin)} ago</span> : <span className="text-zinc-600">idle</span>}</span>
      </div>

      {/* API budget + accuracy */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <Gauge
          label="Solana Tracker · this month"
          value={s.stMonth} max={ST_MONTHLY_BUDGET}
          sub={`${s.stToday.toLocaleString()} today · ${ST_MONTHLY_BUDGET.toLocaleString()} budget`}
          warn={stPct > 0.85} danger={stPct > 0.97}
        />
        <StatBig label="Free RPC · today" value={s.rpcToday.toLocaleString()} sub="funding + microstructure · unmetered" accent="zinc" />
        <StatBig
          label="Verdict accuracy · 24h"
          value={s.resolved > 0 ? `${Math.round(accuracy * 100)}%` : "—"}
          sub={s.resolved > 0 ? `${s.correct} / ${s.resolved} resolved calls` : "no resolved outcomes yet"}
          accent={accuracy >= 0.6 ? "green" : accuracy > 0 ? "yellow" : "zinc"}
        />
      </div>

      {/* Pipeline stages */}
      <Panel title="Pipeline — every stage, live">
        <div className="space-y-1">
          <Stage n={1} label="Graduations detected" count={s.totalGrads} ok={status === "live" || status === "quiet"} note={`${s.v2Grads} on pipeline v2 (clean)`} fresh={lagMin} />
          <Stage n={2} label="Team detection (probabilistic)" count={s.teamScored} ok={s.teamScored > 0} note="wallets scored with evidence" />
          <Stage n={3} label="Slot microstructure resolved" count={s.microResolved} ok={s.microResolved > 0} note="first 50 buys → slot + block index" />
          <Stage n={4} label="Launch coordination" count={s.coordLaunch} ok={s.coordLaunch > 0} note="bundling / shared-funder entities" />
          <Stage n={5} label="Outcomes tracked (4h / 24h)" count={s.outcomes4h} ok={s.outcomes4h > 0} note={`${s.outcomes24h} have 24h outcome`} fresh={outcomeLagMin} />
          <Stage n={6} label="Self-learning updated" count={s.walletFp3 + s.funders8} ok={s.walletFp3 + s.funders8 > 0} note="wallet fingerprints + funder reputation" />
        </div>
      </Panel>

      {/* Behavioral engine proof */}
      <Panel title="Behavioral engine — what it has caught">
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <Mini label="Launch-slot snipes" value={s.slotSnipesTotal} hint="buys in the very first slot — not humanly reactable" />
          <Mini label="Atomic bundles" value={s.bundlesTotal} hint="same-block adjacent buys (Jito-style)" />
          <Mini label="Creator-funded team wallets" value={s.creatorLinkedTotal} hint="deployer → wallet funding fingerprint" />
          <Mini label="Cross-coin fingerprints" value={s.walletFp3} hint="wallets profiled across ≥3 coins" />
        </div>
      </Panel>

      {/* Learning maturity */}
      <Panel title="Self-learning maturity">
        <div className="space-y-2.5">
          <Bar label="Pattern buckets" current={s.outcomes4h} target={30} unit="4h outcomes" />
          <Bar label="Wallet fingerprints" current={s.walletFp3} target={20} unit="wallets ≥3 coins" />
          <Bar label="Funder reputations" current={s.funders8} target={5} unit="funders ≥8 launches" />
          <Bar label="Wallet co-appearance graph" current={s.walletGraph} target={50} unit="edges" />
          <Bar label="Exit choreography" current={s.choreoFunders} target={20} unit="tokens tracked" />
        </div>
        <p className="text-zinc-600 text-xs mt-3">
          These grow only from the bot&apos;s own observations. A meter fills as clean pipeline-v2 data
          accumulates; verdict factors switch on when their sample clears the significance gate.
        </p>
      </Panel>

      {loading && <p className="text-zinc-600 text-xs font-mono">refreshing…</p>}
    </div>
  );
}

// ── components ────────────────────────────────────────────────────────────────

function Sep() { return <span className="text-zinc-700">·</span>; }

function fmtAgo(min: number): string {
  if (min < 60) return `${min}m`;
  const h = Math.floor(min / 60);
  return h < 24 ? `${h}h` : `${Math.floor(h / 24)}d`;
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
      <p className="text-zinc-400 text-xs uppercase tracking-wide mb-3">{title}</p>
      {children}
    </div>
  );
}

function Gauge({ label, value, max, sub, warn, danger }: { label: string; value: number; max: number; sub: string; warn?: boolean; danger?: boolean }) {
  const pct = Math.min(1, value / max);
  const barColor = danger ? "bg-red-500" : warn ? "bg-yellow-500" : "bg-green-500";
  const valColor = danger ? "text-red-400" : warn ? "text-yellow-400" : "text-zinc-100";
  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3">
      <p className="text-zinc-500 text-xs uppercase tracking-wide mb-1">{label}</p>
      <p className={`text-2xl font-mono font-bold ${valColor}`}>{value.toLocaleString()}<span className="text-zinc-600 text-sm"> / {(max / 1000).toFixed(0)}k</span></p>
      <div className="mt-2 bg-zinc-800 rounded-full h-1.5"><div className={`${barColor} h-1.5 rounded-full`} style={{ width: `${pct * 100}%` }} /></div>
      <p className="text-zinc-600 text-xs mt-1.5">{sub} · <span className={valColor}>{Math.round(pct * 100)}%</span> used</p>
    </div>
  );
}

function StatBig({ label, value, sub, accent }: { label: string; value: string; sub: string; accent: "green" | "yellow" | "zinc" }) {
  const c = { green: "text-green-400", yellow: "text-yellow-400", zinc: "text-zinc-100" }[accent];
  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3">
      <p className="text-zinc-500 text-xs uppercase tracking-wide mb-1">{label}</p>
      <p className={`text-2xl font-mono font-bold ${c}`}>{value}</p>
      <p className="text-zinc-600 text-xs mt-1.5">{sub}</p>
    </div>
  );
}

function Stage({ n, label, count, ok, note, fresh }: { n: number; label: string; count: number; ok: boolean; note: string; fresh?: number | null }) {
  const stale = fresh !== undefined && fresh !== null && fresh > 180;
  const dot = !ok ? "bg-zinc-700" : stale ? "bg-yellow-500" : "bg-green-500";
  return (
    <div className="flex items-center gap-3 py-1.5 border-b border-zinc-800/60 last:border-0">
      <span className="text-zinc-600 text-xs font-mono w-4 shrink-0">{n}</span>
      <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${dot}`} />
      <span className="text-zinc-200 text-sm flex-1 min-w-0 truncate">{label}</span>
      <span className="text-zinc-600 text-xs hidden sm:block truncate max-w-[45%]">{note}</span>
      <span className="text-zinc-100 text-sm font-mono tabular-nums w-16 text-right shrink-0">{count.toLocaleString()}</span>
    </div>
  );
}

function Mini({ label, value, hint }: { label: string; value: number; hint: string }) {
  return (
    <div className="bg-zinc-950/60 border border-zinc-800 rounded p-3">
      <p className="text-white text-xl font-mono font-bold tabular-nums">{value.toLocaleString()}</p>
      <p className="text-zinc-300 text-xs mt-0.5">{label}</p>
      <p className="text-zinc-600 text-[11px] mt-1 leading-tight">{hint}</p>
    </div>
  );
}

function Bar({ label, current, target, unit }: { label: string; current: number; target: number; unit: string }) {
  const pct = Math.min(1, current / target);
  const mature = current >= target;
  return (
    <div className="flex items-center gap-3">
      <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${mature ? "bg-green-500" : "bg-zinc-600"}`} />
      <span className="text-zinc-400 text-xs w-48 truncate">{label}</span>
      <div className="flex-1 bg-zinc-800 rounded-full h-1.5"><div className={`h-1.5 rounded-full ${mature ? "bg-green-500" : "bg-zinc-500"}`} style={{ width: `${pct * 100}%` }} /></div>
      <span className={`text-xs font-mono w-28 text-right tabular-nums ${mature ? "text-green-400" : "text-zinc-500"}`}>{current} / {target} {unit.split(" ")[0]}</span>
    </div>
  );
}

function Notice({ title, body }: { title: string; body: string }) {
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900 text-zinc-400 p-4">
      <p className="font-semibold text-sm">{title}</p>
      <p className="text-xs mt-1 opacity-80">{body}</p>
    </div>
  );
}
