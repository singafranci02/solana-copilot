"use client";

import { useEffect, useState } from "react";
import { supabase } from "@/lib/supabase";
import type { GraduationRow } from "@/lib/types";
import { formatMint, formatDate } from "@/lib/types";
import { VerdictBadge } from "./VerdictBadge";
import { OutcomeChip } from "./OutcomeChip";
import { SignalPip } from "./SignalPip";
import { StatsBar } from "./StatsBar";

const PAGE_SIZE = 50;

export function GraduationTable() {
  const [rows, setRows] = useState<GraduationRow[]>([]);
  const [loading, setLoading] = useState(true);

  async function load() {
    const { data, error } = await supabase
      .from("graduation_feed")
      .select("*")
      .order("graduated_at", { ascending: false })
      .limit(PAGE_SIZE);

    if (!error && data) setRows(data as GraduationRow[]);
    setLoading(false);
  }

  useEffect(() => {
    load();

    // Realtime: re-fetch when any of the source tables change
    const channel = supabase
      .channel("graduation-feed-changes")
      .on("postgres_changes", { event: "*", schema: "public", table: "graduation_events" }, load)
      .on("postgres_changes", { event: "*", schema: "public", table: "coin_outcomes" }, load)
      .on("postgres_changes", { event: "*", schema: "public", table: "post_grad_behavior" }, load)
      .subscribe();

    return () => { supabase.removeChannel(channel); };
  }, []);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-48 text-zinc-600">
        connecting to feed…
      </div>
    );
  }

  if (rows.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-48 gap-2 text-center">
        <p className="text-zinc-400 text-lg">No graduations yet</p>
        <p className="text-zinc-600 text-sm max-w-md">
          The bot is running on the Mac mini and monitoring Pump.fun.
          Tokens appear here the moment they graduate (~85 SOL raised).
        </p>
      </div>
    );
  }

  return (
    <div>
      <StatsBar rows={rows} />
      <div className="overflow-x-auto rounded-lg border border-zinc-800">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-zinc-800 text-zinc-500 text-xs uppercase tracking-wide">
              <th className="text-left px-4 py-3">Time</th>
              <th className="text-left px-4 py-3">Token</th>
              <th className="text-left px-4 py-3">Verdict</th>
              <th className="text-right px-4 py-3">Team %</th>
              <th className="text-center px-4 py-3">Sniper</th>
              <th className="text-center px-4 py-3">1h sig</th>
              <th className="text-center px-4 py-3">4h sig</th>
              <th className="text-center px-4 py-3">24h outcome</th>
              <th className="text-right px-4 py-3">Lag</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <GraduationRow key={row.token_mint} row={row} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function GraduationRow({ row }: { row: GraduationRow }) {
  const isMatch = row.verdict !== null && row.outcome_24h !== null;
  const correct = isMatch && (
    (row.verdict === "SKIP" && row.outcome_24h === "rug") ||
    (row.verdict !== "SKIP" && row.outcome_24h !== "rug")
  );
  const rowBg = isMatch
    ? correct ? "bg-green-950/20" : "bg-red-950/20"
    : "hover:bg-zinc-900/50";

  return (
    <tr className={`border-b border-zinc-900 transition-colors ${rowBg}`}>
      <td className="px-4 py-3 text-zinc-500 font-mono text-xs whitespace-nowrap">
        {formatDate(row.graduated_at)}
      </td>
      <td className="px-4 py-3">
        <div className="flex flex-col gap-0.5">
          <span className="text-white font-semibold">
            {row.symbol ? `$${row.symbol}` : formatMint(row.token_mint)}
          </span>
          {row.is_known_rugger && (
            <span className="text-red-500 text-xs font-mono">⚠ known rugger</span>
          )}
        </div>
      </td>
      <td className="px-4 py-3">
        <div className="flex flex-col gap-1">
          <VerdictBadge verdict={row.verdict} />
          {row.confidence !== null && (
            <span className="text-zinc-600 text-xs font-mono">
              {Math.round(row.confidence * 100)}% conf
            </span>
          )}
        </div>
      </td>
      <td className="px-4 py-3 text-right font-mono">
        {row.supply_pct_at_graduation !== null ? (
          <span className={row.supply_pct_at_graduation > 40 ? "text-red-400" : "text-zinc-300"}>
            {row.supply_pct_at_graduation.toFixed(1)}%
          </span>
        ) : <span className="text-zinc-700">—</span>}
      </td>
      <td className="px-4 py-3 text-center">
        {row.is_bc_sniper
          ? <span className="text-orange-400 text-xs font-mono">⚡ YES</span>
          : <span className="text-zinc-700 text-xs">—</span>}
      </td>
      <td className="px-4 py-3 text-center"><SignalPip signal={row.signal_1h} /></td>
      <td className="px-4 py-3 text-center"><SignalPip signal={row.signal_4h} /></td>
      <td className="px-4 py-3 text-center"><OutcomeChip outcome={row.outcome_24h} /></td>
      <td className="px-4 py-3 text-right text-zinc-600 font-mono text-xs">
        {row.detection_lag_seconds}s
      </td>
    </tr>
  );
}
