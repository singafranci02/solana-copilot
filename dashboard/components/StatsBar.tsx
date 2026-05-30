"use client";

import type { GraduationRow } from "@/lib/types";

interface Props { rows: GraduationRow[]; }

export function StatsBar({ rows }: Props) {
  const total = rows.length;
  const sound = rows.filter((r) => r.verdict === "STRUCTURALLY_SOUND").length;
  const watch = rows.filter((r) => r.verdict === "WATCH").length;
  const skip  = rows.filter((r) => r.verdict === "SKIP").length;

  const withOutcome24 = rows.filter((r) => r.outcome_24h !== null);
  const moon = withOutcome24.filter((r) => r.outcome_24h === "moon").length;
  const ok   = withOutcome24.filter((r) => r.outcome_24h === "ok").length;
  const rug  = withOutcome24.filter((r) => r.outcome_24h === "rug").length;

  const assessed = withOutcome24.filter((r) => r.verdict !== null);
  const correct = assessed.filter((r) =>
    r.verdict === "SKIP" ? r.outcome_24h === "rug" : r.outcome_24h !== "rug"
  ).length;
  const accuracy = assessed.length > 0 ? (correct / assessed.length) * 100 : null;

  const skipPrecision = (() => {
    const skipped = assessed.filter((r) => r.verdict === "SKIP");
    if (skipped.length === 0) return null;
    return (skipped.filter((r) => r.outcome_24h === "rug").length / skipped.length) * 100;
  })();

  const wsRows = rows.filter((r) => r.detection_lag_seconds >= 0);
  const avgLag = wsRows.length > 0
    ? Math.round(wsRows.reduce((a, r) => a + r.detection_lag_seconds, 0) / wsRows.length)
    : null;

  const soundPct = total > 0 ? Math.round(sound / total * 100) : 0;
  const watchPct = total > 0 ? Math.round(watch / total * 100) : 0;
  const skipPct  = total > 0 ? Math.round(skip  / total * 100) : 0;

  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-6">

      {/* Verdicts */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4 col-span-2 lg:col-span-1">
        <p className="text-zinc-500 text-xs uppercase tracking-wide mb-3">Verdicts · {total} total</p>
        <div className="space-y-2">
          <VerdictBar label="SOUND" count={sound} pct={soundPct} color="bg-green-500" />
          <VerdictBar label="WATCH" count={watch} pct={watchPct} color="bg-yellow-500" />
          <VerdictBar label="SKIP"  count={skip}  pct={skipPct}  color="bg-red-500" />
        </div>
      </div>

      {/* Accuracy */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
        <p className="text-zinc-500 text-xs uppercase tracking-wide mb-1">Bot Accuracy</p>
        <p className={`text-3xl font-mono font-bold ${
          accuracy === null ? "text-zinc-600"
          : accuracy >= 70 ? "text-green-400"
          : accuracy >= 50 ? "text-yellow-400"
          : "text-red-400"
        }`}>
          {accuracy !== null ? `${accuracy.toFixed(0)}%` : "—"}
        </p>
        <p className="text-zinc-600 text-xs mt-1">
          {assessed.length > 0 ? `${correct}/${assessed.length} correct` : "awaiting outcomes"}
        </p>
        {skipPrecision !== null && (
          <p className="text-zinc-500 text-xs mt-2">
            SKIP caught <span className="text-red-400 font-mono">{skipPrecision.toFixed(0)}%</span> of rugs
          </p>
        )}
      </div>

      {/* Outcomes */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
        <p className="text-zinc-500 text-xs uppercase tracking-wide mb-3">24h Outcomes · {withOutcome24.length}</p>
        <div className="flex items-end gap-3">
          <OutcomeStat label="MOON" count={moon} color="text-purple-400" />
          <OutcomeStat label="OK"   count={ok}   color="text-zinc-400" />
          <OutcomeStat label="RUG"  count={rug}  color="text-red-400" />
        </div>
      </div>

      {/* Detection speed */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
        <p className="text-zinc-500 text-xs uppercase tracking-wide mb-1">Avg Detection</p>
        <p className="text-3xl font-mono font-bold text-white">{avgLag !== null ? `${avgLag}s` : "—"}</p>
        <p className="text-zinc-600 text-xs mt-1">
          {avgLag !== null ? "WS detection lag" : "no WS detections yet"}
        </p>
        <p className="text-zinc-500 text-xs mt-2">
          Ruggers blocked{" "}
          <span className="text-red-400 font-mono">
            {rows.filter((r) => r.is_known_rugger && r.verdict === "SKIP").length}
          </span>
        </p>
      </div>
    </div>
  );
}

function VerdictBar({ label, count, pct, color }: {
  label: string; count: number; pct: number; color: string;
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-zinc-400 text-xs font-mono w-10">{label}</span>
      <div className="flex-1 bg-zinc-800 rounded-full h-1.5">
        <div className={`${color} h-1.5 rounded-full transition-all`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-zinc-300 text-xs font-mono w-6 text-right">{count}</span>
    </div>
  );
}

function OutcomeStat({ label, count, color }: { label: string; count: number; color: string }) {
  return (
    <div className="text-center">
      <p className={`text-2xl font-mono font-bold ${color}`}>{count}</p>
      <p className="text-zinc-600 text-xs">{label}</p>
    </div>
  );
}
