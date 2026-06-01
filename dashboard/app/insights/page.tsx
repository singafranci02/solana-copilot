"use client";

import { useEffect, useState } from "react";
import { supabase, isConfigured } from "@/lib/supabase";
import type { GraduationRow } from "@/lib/types";
import { computeInsights, overallStats, type VariableInsight, type Bucket } from "@/lib/correlation";

export const dynamic = "force-dynamic";

export default function InsightsPage() {
  const [rows, setRows] = useState<GraduationRow[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!isConfigured) { setLoading(false); return; }
    (async () => {
      const { data } = await supabase
        .from("graduation_feed")
        .select("*")
        .order("graduated_at", { ascending: false })
        .limit(2000);
      if (data) setRows(data as GraduationRow[]);
      setLoading(false);
    })();
  }, []);

  if (!isConfigured) {
    return <Notice title="Supabase not connected" body="Add NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY to Vercel." />;
  }
  if (loading) {
    return <div className="flex items-center justify-center h-48 text-zinc-600">computing correlations…</div>;
  }

  const insights = computeInsights(rows);
  const stats = overallStats(rows);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-zinc-100 text-2xl font-bold tracking-tight">Signal Insights</h1>
        <p className="text-zinc-500 text-sm mt-1">
          Which variables actually predict outcomes. Predictive power = how much the rug rate
          swings across a variable&apos;s buckets. Flat = noise. Big swing = real signal.
        </p>
      </div>

      {/* Baseline */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <StatCard label="Resolved outcomes" value={String(stats.resolved)} sub={`of ${rows.length} loaded`} />
        <StatCard label="Baseline rug rate" value={`${Math.round(stats.baselineRugRate * 100)}%`} sub="across all tokens" accent="red" />
        <StatCard label="OK rate" value={`${Math.round(stats.okRate * 100)}%`} sub="survived" />
        <StatCard label="Moon rate" value={`${Math.round(stats.moonRate * 100)}%`} sub="≥3× graduation MC" accent="purple" />
      </div>

      {stats.resolved < 20 && (
        <Notice
          title="Limited data"
          body={`Only ${stats.resolved} resolved outcomes so far. Correlations need ~20+ to be meaningful and ~100+ to be reliable. Treat everything below as hypothesis, not fact.`}
          tone="warning"
        />
      )}

      {/* How to read */}
      <p className="text-zinc-600 text-xs">
        Each bar shows the rug rate within that bucket. Compare against the{" "}
        <span className="text-red-400">{Math.round(stats.baselineRugRate * 100)}% baseline</span> —
        a bucket far above baseline means that condition predicts rugs; far below means it predicts survival.
      </p>

      {/* Variable cards */}
      <div className="space-y-3">
        {insights.map((ins) => (
          <InsightCard key={ins.id} insight={ins} baseline={stats.baselineRugRate} />
        ))}
      </div>
    </div>
  );
}


function InsightCard({ insight, baseline }: { insight: VariableInsight; baseline: number }) {
  const badge = {
    strong: { label: "STRONG SIGNAL", cls: "bg-green-900/60 text-green-300 border-green-700" },
    moderate: { label: "MODERATE", cls: "bg-yellow-900/60 text-yellow-300 border-yellow-700" },
    weak: { label: "WEAK / NOISE", cls: "bg-zinc-800 text-zinc-500 border-zinc-700" },
    insufficient: { label: "INSUFFICIENT DATA", cls: "bg-zinc-900 text-zinc-600 border-zinc-800" },
  }[insight.predictive];

  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-4">
      <div className="flex items-start justify-between gap-3 mb-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-zinc-100 text-sm font-semibold">{insight.name}</span>
            <span className={`inline-flex px-1.5 py-0.5 rounded text-xs font-mono border ${badge.cls}`}>
              {badge.label}
            </span>
          </div>
          <p className="text-zinc-600 text-xs mt-0.5">{insight.description}</p>
        </div>
        <div className="text-right shrink-0">
          <p className="text-zinc-300 text-sm font-mono font-bold">{Math.round(insight.spread * 100)}pt</p>
          <p className="text-zinc-600 text-xs">spread · n={insight.totalN}</p>
        </div>
      </div>

      <div className="space-y-1.5">
        {insight.buckets.length === 0 && (
          <p className="text-zinc-700 text-xs">no resolved data yet</p>
        )}
        {insight.buckets.map((b) => (
          <BucketBar key={b.label} bucket={b} baseline={baseline} />
        ))}
      </div>
    </div>
  );
}

function BucketBar({ bucket, baseline }: { bucket: Bucket; baseline: number }) {
  const pct = Math.round(bucket.rugRate * 100);
  const faded = bucket.n < 5;
  const aboveBaseline = bucket.rugRate > baseline + 0.05;
  const belowBaseline = bucket.rugRate < baseline - 0.05;

  const barColor = faded ? "bg-zinc-700"
    : aboveBaseline ? "bg-red-500"
    : belowBaseline ? "bg-green-500"
    : "bg-zinc-500";

  return (
    <div className={`flex items-center gap-2 ${faded ? "opacity-40" : ""}`}>
      <span className="text-zinc-400 text-xs font-mono w-28 truncate">{bucket.label}</span>
      <div className="flex-1 bg-zinc-800 rounded-full h-2 relative">
        <div className={`${barColor} h-2 rounded-full transition-all`} style={{ width: `${pct}%` }} />
        {/* baseline marker */}
        <div
          className="absolute top-0 h-2 w-px bg-zinc-500/70"
          style={{ left: `${Math.round(baseline * 100)}%` }}
          title={`baseline ${Math.round(baseline * 100)}%`}
        />
      </div>
      <span className={`text-xs font-mono w-10 text-right ${aboveBaseline ? "text-red-400" : belowBaseline ? "text-green-400" : "text-zinc-400"}`}>
        {pct}%
      </span>
      <span className="text-zinc-600 text-xs font-mono w-12 text-right">n={bucket.n}</span>
    </div>
  );
}

function StatCard({ label, value, sub, accent }: { label: string; value: string; sub: string; accent?: "red" | "purple" }) {
  const valueColor = accent === "red" ? "text-red-400" : accent === "purple" ? "text-purple-400" : "text-white";
  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3">
      <p className="text-zinc-500 text-xs uppercase tracking-wide mb-1">{label}</p>
      <p className={`text-2xl font-mono font-bold ${valueColor}`}>{value}</p>
      <p className="text-zinc-600 text-xs mt-0.5">{sub}</p>
    </div>
  );
}

function Notice({ title, body, tone = "info" }: { title: string; body: string; tone?: "info" | "warning" }) {
  const cls = tone === "warning"
    ? "border-yellow-800 bg-yellow-950/20 text-yellow-300"
    : "border-zinc-800 bg-zinc-900 text-zinc-400";
  return (
    <div className={`rounded-lg border ${cls} p-4`}>
      <p className="font-semibold text-sm">{title}</p>
      <p className="text-xs mt-1 opacity-80">{body}</p>
    </div>
  );
}
