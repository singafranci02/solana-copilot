"use client";

import { useEffect, useState } from "react";
import { supabase, isConfigured } from "@/lib/supabase";
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

  if (!isConfigured) {
    return (
      <div className="flex flex-col items-center justify-center h-48 gap-2 text-center">
        <p className="text-yellow-500 text-lg">Supabase not connected</p>
        <p className="text-zinc-600 text-sm max-w-md">
          Add NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY to Vercel environment variables.
        </p>
      </div>
    );
  }

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
        <table className="w-full text-sm min-w-[900px]">
          <thead>
            <tr className="border-b border-zinc-800 text-zinc-500 text-xs uppercase tracking-wide bg-zinc-900/50">
              <th className="text-left px-4 py-3 w-32">Time</th>
              <th className="text-left px-4 py-3 w-40">Token</th>
              <th className="text-left px-4 py-3">Algorithm signals</th>
              <th className="text-left px-4 py-3 w-32">Verdict</th>
              <th className="text-left px-4 py-3">Why</th>
              <th className="text-center px-3 py-3 w-16">1h</th>
              <th className="text-center px-3 py-3 w-16">4h</th>
              <th className="text-center px-3 py-3 w-24">24h</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <Row key={row.token_mint} row={row} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Row({ row }: { row: GraduationRow }) {
  const isMatch = row.verdict !== null && row.outcome_24h !== null;
  const correct = isMatch && (
    (row.verdict === "SKIP" && row.outcome_24h === "rug") ||
    (row.verdict !== "SKIP" && row.outcome_24h !== "rug")
  );
  const rowBg = isMatch
    ? correct ? "bg-green-950/20" : "bg-red-950/20"
    : "hover:bg-zinc-900/40";

  const solscanUrl = `https://solscan.io/token/${row.token_mint}`;

  return (
    <tr className={`border-b border-zinc-900 transition-colors ${rowBg}`}>

      {/* Time */}
      <td className="px-4 py-3 text-zinc-500 font-mono text-xs whitespace-nowrap align-top pt-4">
        {formatDate(row.graduated_at)}
        <div className="text-zinc-700 text-xs mt-0.5">{row.detection_lag_seconds}s lag</div>
      </td>

      {/* Token */}
      <td className="px-4 py-3 align-top pt-4">
        <div className="flex flex-col gap-0.5">
          <a
            href={solscanUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="text-white font-semibold hover:text-zinc-300 transition-colors"
          >
            {row.symbol ? `$${row.symbol}` : formatMint(row.token_mint)}
          </a>
          {row.name && row.name !== row.symbol && (
            <span className="text-zinc-600 text-xs truncate max-w-[130px]">{row.name}</span>
          )}
          <span className="text-zinc-700 text-xs font-mono">{formatMint(row.token_mint)}</span>
          {row.is_known_rugger && (
            <span className="text-red-500 text-xs font-semibold">⚠ known rugger</span>
          )}
        </div>
      </td>

      {/* Algorithm signals */}
      <td className="px-4 py-3 align-top pt-4">
        <div className="flex flex-wrap gap-1.5">
          <SmBadge count={row.smart_money_count} />
          <TeamBadge pct={row.supply_pct_at_graduation} />
          {row.is_bc_sniper && (
            <Pill color="orange">⚡ sniper</Pill>
          )}
          <FunderBadge rugRate={row.funder_rug_rate} isKnownRugger={row.is_known_rugger} />
        </div>
      </td>

      {/* Verdict */}
      <td className="px-4 py-3 align-top pt-4">
        <div className="flex flex-col gap-1.5">
          <VerdictBadge verdict={row.verdict} />
          {row.confidence !== null && (
            <div className="w-20">
              <div className="bg-zinc-800 rounded-full h-1">
                <div
                  className={`h-1 rounded-full ${
                    row.verdict === "STRUCTURALLY_SOUND" ? "bg-green-500"
                    : row.verdict === "SKIP" ? "bg-red-500"
                    : "bg-yellow-500"
                  }`}
                  style={{ width: `${Math.round(row.confidence * 100)}%` }}
                />
              </div>
              <span className="text-zinc-600 text-xs font-mono">
                {Math.round(row.confidence * 100)}%
              </span>
            </div>
          )}
        </div>
      </td>

      {/* Why — dominant factors */}
      <td className="px-4 py-3 align-top pt-4 max-w-[220px]">
        <FactorTags factors={row.dominant_factors_json} />
      </td>

      {/* 1h signal */}
      <td className="px-3 py-3 text-center align-top pt-4">
        <SignalPip signal={row.signal_1h} />
      </td>

      {/* 4h signal */}
      <td className="px-3 py-3 text-center align-top pt-4">
        <SignalPip signal={row.signal_4h} />
      </td>

      {/* 24h outcome */}
      <td className="px-3 py-3 text-center align-top pt-4">
        <div className="flex flex-col items-center gap-1">
          <OutcomeChip outcome={row.outcome_24h} />
          {(row.outcome_1h || row.outcome_4h) && (
            <div className="flex gap-1 mt-0.5">
              {row.outcome_1h && <MiniOutcome outcome={row.outcome_1h} label="1h" />}
              {row.outcome_4h && <MiniOutcome outcome={row.outcome_4h} label="4h" />}
            </div>
          )}
        </div>
      </td>
    </tr>
  );
}


// ── sub-components ────────────────────────────────────────────────────────────

function Pill({ color, children }: { color: string; children: React.ReactNode }) {
  const colors: Record<string, string> = {
    green:  "bg-green-900/50 text-green-300 border-green-800",
    yellow: "bg-yellow-900/50 text-yellow-300 border-yellow-800",
    red:    "bg-red-900/50 text-red-300 border-red-800",
    orange: "bg-orange-900/50 text-orange-300 border-orange-800",
    zinc:   "bg-zinc-800/50 text-zinc-400 border-zinc-700",
  };
  return (
    <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-xs font-mono border ${colors[color] ?? colors.zinc}`}>
      {children}
    </span>
  );
}

function SmBadge({ count }: { count: number }) {
  if (count === 0) return <Pill color="zinc">0 SM</Pill>;
  if (count >= 2)  return <Pill color="green">{count} SM ✓</Pill>;
  return <Pill color="yellow">{count} SM</Pill>;
}

function TeamBadge({ pct }: { pct: number | null }) {
  if (pct === null) return null;
  const color = pct >= 50 ? "red" : pct >= 30 ? "yellow" : "zinc";
  return <Pill color={color}>{pct.toFixed(1)}% team</Pill>;
}

function FunderBadge({ rugRate, isKnownRugger }: { rugRate: number | null; isKnownRugger: boolean | null }) {
  if (isKnownRugger) return <Pill color="red">rugger ⚠</Pill>;
  if (rugRate === null) return null;
  if (rugRate >= 0.65) return <Pill color="red">funder {(rugRate * 100).toFixed(0)}% rug</Pill>;
  if (rugRate >= 0.40) return <Pill color="yellow">funder {(rugRate * 100).toFixed(0)}% rug</Pill>;
  if (rugRate <= 0.20) return <Pill color="green">funder ✓</Pill>;
  return null;
}

function FactorTags({ factors }: { factors: string[] | null }) {
  if (!factors || factors.length === 0) {
    return <span className="text-zinc-700 text-xs">no signal data</span>;
  }
  return (
    <div className="flex flex-wrap gap-1">
      {factors.map((f, i) => {
        const isPos = f.startsWith("+");
        const isNeg = f.startsWith("-") || f.includes("rugger") || f.includes("DUMPED") || f.includes("sniper");
        const color = isPos ? "green" : isNeg ? "red" : "zinc";
        return <Pill key={i} color={color}>{f}</Pill>;
      })}
    </div>
  );
}

function MiniOutcome({ outcome, label }: { outcome: string; label: string }) {
  const color =
    outcome === "moon" ? "text-purple-500"
    : outcome === "rug" ? "text-red-600"
    : "text-zinc-600";
  return (
    <span className={`text-xs font-mono ${color}`}>{label}</span>
  );
}
