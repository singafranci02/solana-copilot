"use client";

import type { GraduationRow } from "@/lib/types";

interface Props {
  rows: GraduationRow[];
}

export function StatsBar({ rows }: Props) {
  const total = rows.length;
  const withOutcome = rows.filter((r) => r.outcome_24h !== null);

  const verdictCount = (v: string) => rows.filter((r) => r.verdict === v).length;
  const sound = verdictCount("STRUCTURALLY_SOUND");
  const watch = verdictCount("WATCH");
  const skip  = verdictCount("SKIP");

  // Accuracy: for rows that had a verdict AND a 24h outcome
  const assessed = withOutcome.filter((r) => r.verdict !== null);
  // STRUCTURALLY_SOUND / WATCH → good if not rug; SKIP → good if rug
  const correct = assessed.filter((r) => {
    if (r.verdict === "SKIP") return r.outcome_24h === "rug";
    return r.outcome_24h !== "rug";
  }).length;
  const accuracy = assessed.length > 0 ? (correct / assessed.length) * 100 : null;

  const avgLag = total > 0
    ? Math.round(rows.reduce((a, r) => a + r.detection_lag_seconds, 0) / total)
    : 0;

  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-6 gap-3 mb-6">
      <Stat label="Graduated" value={total.toString()} />
      <Stat label="Sound / Watch / Skip"
        value={`${sound} / ${watch} / ${skip}`}
        sub={total > 0 ? `${Math.round(sound/total*100)}% sound` : undefined} />
      <Stat label="24h Outcomes" value={withOutcome.length.toString()}
        sub={`of ${total} analysed`} />
      <Stat label="Bot Accuracy"
        value={accuracy !== null ? `${accuracy.toFixed(0)}%` : "—"}
        sub={assessed.length > 0 ? `n=${assessed.length}` : "waiting for data"} />
      <Stat label="Avg Detection Lag" value={`${avgLag}s`} />
      <Stat label="Known Ruggers Blocked"
        value={rows.filter((r) => r.is_known_rugger && r.verdict === "SKIP").length.toString()} />
    </div>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-3">
      <p className="text-zinc-500 text-xs uppercase tracking-wide mb-1">{label}</p>
      <p className="text-white text-xl font-mono font-bold">{value}</p>
      {sub && <p className="text-zinc-600 text-xs mt-0.5">{sub}</p>}
    </div>
  );
}
