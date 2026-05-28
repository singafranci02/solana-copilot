"use client";

import { useEffect, useState } from "react";
import { supabase, isConfigured } from "@/lib/supabase";
import type { GraduationRow, Verdict } from "@/lib/types";
import { formatMint, formatDate } from "@/lib/types";
import { VerdictBadge } from "./VerdictBadge";
import { OutcomeChip } from "./OutcomeChip";
import { SignalPip } from "./SignalPip";
import { StatsBar } from "./StatsBar";

const PAGE_SIZE = 12;

type Filter = "all" | "STRUCTURALLY_SOUND" | "WATCH" | "SKIP" | "resolved";

export function GraduationTable() {
  const [rows, setRows] = useState<GraduationRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<Filter>("all");
  const [page, setPage] = useState(1);

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
      .limit(200);
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
    return <div className="flex items-center justify-center h-48 text-zinc-600">connecting…</div>;
  }

  if (rows.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-48 gap-2 text-center">
        <p className="text-zinc-400 text-lg">No graduations yet</p>
        <p className="text-zinc-600 text-sm max-w-md">
          The bot is running on the Mac mini. Tokens appear here the moment they graduate (~85 SOL raised).
        </p>
      </div>
    );
  }

  const filtered = rows.filter((r) => {
    if (filter === "all") return true;
    if (filter === "resolved") return r.outcome_24h !== null;
    return r.verdict === filter;
  });

  const visible = filtered.slice(0, page * PAGE_SIZE);
  const hasMore = visible.length < filtered.length;

  return (
    <div>
      <StatsBar rows={rows} />

      {/* Filter tabs */}
      <div className="flex items-center gap-2 mb-4 flex-wrap">
        {(["all", "STRUCTURALLY_SOUND", "WATCH", "SKIP", "resolved"] as Filter[]).map((f) => {
          const count = f === "all" ? rows.length
            : f === "resolved" ? rows.filter((r) => r.outcome_24h !== null).length
            : rows.filter((r) => r.verdict === f).length;
          const labels: Record<Filter, string> = {
            all: "All",
            STRUCTURALLY_SOUND: "Sound",
            WATCH: "Watch",
            SKIP: "Skip",
            resolved: "Resolved",
          };
          const activeColors: Record<Filter, string> = {
            all: "bg-zinc-700 text-white",
            STRUCTURALLY_SOUND: "bg-green-800 text-green-200",
            WATCH: "bg-yellow-800 text-yellow-200",
            SKIP: "bg-red-800 text-red-200",
            resolved: "bg-purple-800 text-purple-200",
          };
          const isActive = filter === f;
          return (
            <button
              key={f}
              onClick={() => { setFilter(f); setPage(1); }}
              className={`px-3 py-1 rounded-full text-xs font-mono transition-colors ${
                isActive ? activeColors[f] : "bg-zinc-900 text-zinc-500 hover:text-zinc-300 border border-zinc-800"
              }`}
            >
              {labels[f]} <span className="opacity-60">{count}</span>
            </button>
          );
        })}
      </div>

      {/* Cards */}
      <div className="space-y-3">
        {visible.map((row) => <TokenCard key={row.token_mint} row={row} />)}
      </div>

      {hasMore && (
        <button
          onClick={() => setPage((p) => p + 1)}
          className="mt-4 w-full py-2 rounded-lg border border-zinc-800 text-zinc-500 hover:text-zinc-300 hover:border-zinc-600 text-sm transition-colors"
        >
          Load more ({filtered.length - visible.length} remaining)
        </button>
      )}
    </div>
  );
}


// ── Token Journey Card ────────────────────────────────────────────────────────

function TokenCard({ row }: { row: GraduationRow }) {
  const isResolved = row.outcome_24h !== null;
  const isMatch = isResolved && row.verdict !== null;
  const correct = isMatch && (
    (row.verdict === "SKIP" && row.outcome_24h === "rug") ||
    (row.verdict !== "SKIP" && row.outcome_24h !== "rug")
  );

  const borderColor = isResolved
    ? correct ? "border-green-900" : "border-red-900"
    : "border-zinc-800";

  const solscanUrl = `https://solscan.io/token/${row.token_mint}`;

  return (
    <div className={`rounded-lg border ${borderColor} bg-zinc-900/60 overflow-hidden`}>

      {/* Header row */}
      <div className="flex items-start justify-between gap-3 px-4 pt-4 pb-3">
        <div className="flex flex-col gap-0.5 min-w-0">
          <div className="flex items-center gap-2">
            <a
              href={solscanUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="text-white font-bold text-base hover:text-zinc-300 transition-colors"
            >
              {row.symbol ? `$${row.symbol}` : formatMint(row.token_mint)}
            </a>
            {row.is_known_rugger && (
              <span className="text-red-500 text-xs font-mono">⚠ known rugger</span>
            )}
          </div>
          {row.name && row.name !== row.symbol && (
            <span className="text-zinc-500 text-xs truncate">{row.name}</span>
          )}
          <span className="text-zinc-700 text-xs font-mono">{formatDate(row.graduated_at)} · {row.detection_lag_seconds}s lag</span>
        </div>

        <div className="flex flex-col items-end gap-1.5 shrink-0">
          <VerdictBadge verdict={row.verdict} />
          {row.confidence !== null && (
            <div className="flex items-center gap-1.5">
              <div className="w-16 bg-zinc-800 rounded-full h-1">
                <div
                  className={`h-1 rounded-full ${
                    row.verdict === "STRUCTURALLY_SOUND" ? "bg-green-500"
                    : row.verdict === "SKIP" ? "bg-red-500"
                    : "bg-yellow-500"
                  }`}
                  style={{ width: `${Math.round(row.confidence * 100)}%` }}
                />
              </div>
              <span className="text-zinc-600 text-xs font-mono">{Math.round(row.confidence * 100)}%</span>
            </div>
          )}
        </div>
      </div>

      {/* Signals + factors */}
      <div className="px-4 pb-3 flex flex-col gap-2">
        {/* Signals row */}
        <div className="flex flex-wrap gap-1.5">
          <SmBadge count={row.smart_money_count} />
          <TeamBadge pct={row.supply_pct_at_graduation} />
          {row.is_bc_sniper && <Pill color="orange">⚡ sniper</Pill>}
          <FunderBadge rugRate={row.funder_rug_rate} isKnownRugger={row.is_known_rugger} />
        </div>

        {/* Factor pills — what drove the verdict */}
        <FactorTags factors={row.dominant_factors_json} />
      </div>

      {/* Journey timeline */}
      <div className="border-t border-zinc-800 px-4 py-3">
        <p className="text-zinc-600 text-xs uppercase tracking-wide mb-2">Journey after graduation</p>
        <div className="flex items-center gap-2 flex-wrap">
          <JourneyStep label="Grad" done={true}>
            <span className="text-zinc-400 text-xs font-mono">↗ ~$69K MC</span>
          </JourneyStep>
          <Arrow />
          <JourneyStep label="1h" done={!!(row.signal_1h || row.outcome_1h)}>
            <SignalPip signal={row.signal_1h} />
            {row.outcome_1h && <OutcomeChip outcome={row.outcome_1h} />}
          </JourneyStep>
          <Arrow />
          <JourneyStep label="4h" done={!!(row.signal_4h || row.outcome_4h)}>
            <SignalPip signal={row.signal_4h} />
            {row.outcome_4h && <OutcomeChip outcome={row.outcome_4h} />}
          </JourneyStep>
          <Arrow />
          <JourneyStep label="24h" done={row.outcome_24h !== null}>
            {row.outcome_24h
              ? <OutcomeChip outcome={row.outcome_24h} />
              : <span className="text-zinc-700 text-xs font-mono">pending</span>
            }
          </JourneyStep>
        </div>
      </div>
    </div>
  );
}


// ── small UI helpers ──────────────────────────────────────────────────────────

function Arrow() {
  return <span className="text-zinc-700 text-xs">→</span>;
}

function JourneyStep({ label, done, children }: {
  label: string; done: boolean; children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-1 min-w-[48px]">
      <span className={`text-xs font-mono ${done ? "text-zinc-400" : "text-zinc-700"}`}>{label}</span>
      <div className="flex items-center gap-1">{children}</div>
    </div>
  );
}

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
  if (!factors || factors.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1">
      {factors.map((f, i) => {
        const isPos = f.startsWith("+");
        const isNeg = f.startsWith("-") || f.includes("rugger") || f.includes("DUMPED") || f.includes("sniper");
        return <Pill key={i} color={isPos ? "green" : isNeg ? "red" : "zinc"}>{f}</Pill>;
      })}
    </div>
  );
}
