"use client";

import { useEffect, useState } from "react";
import { supabase, isConfigured } from "@/lib/supabase";

interface Health {
  lastGraduationTs: number | null;
  count24h: number;
  totalResolved: number;
  totalAnalyzed: number;
  skipRate: number;
}

export function HealthBar() {
  const [health, setHealth] = useState<Health | null>(null);
  const [now, setNow] = useState(Math.floor(Date.now() / 1000));

  useEffect(() => {
    if (!isConfigured) return;
    fetch();
    const interval = setInterval(() => {
      fetch();
      setNow(Math.floor(Date.now() / 1000));
    }, 60_000);
    return () => clearInterval(interval);
  }, []);

  async function fetch() {
    const cutoff24h = Math.floor(Date.now() / 1000) - 86_400;

    const [latest, count24h, resolved, total, skips] = await Promise.all([
      supabase.from("graduation_events").select("graduated_at").order("graduated_at", { ascending: false }).limit(1),
      supabase.from("graduation_events").select("*", { count: "exact", head: true }).gte("graduated_at", cutoff24h),
      supabase.from("coin_outcomes").select("*", { count: "exact", head: true }).not("classified", "is", null),
      supabase.from("graduation_events").select("*", { count: "exact", head: true }).not("structural_verdict", "is", null),
      supabase.from("graduation_events").select("*", { count: "exact", head: true }).eq("structural_verdict", "SKIP"),
    ]);

    const lastTs = (latest.data?.[0] as { graduated_at: number } | undefined)?.graduated_at ?? null;
    const tot = total.count ?? 0;

    setHealth({
      lastGraduationTs: lastTs,
      count24h: count24h.count ?? 0,
      totalResolved: resolved.count ?? 0,
      totalAnalyzed: tot,
      skipRate: tot > 0 ? (skips.count ?? 0) / tot : 0,
    });
    setNow(Math.floor(Date.now() / 1000));
  }

  if (!isConfigured || !health) return null;

  const lagMinutes = health.lastGraduationTs ? Math.floor((now - health.lastGraduationTs) / 60) : null;

  const botStatus = lagMinutes === null ? "unknown"
    : lagMinutes < 120 ? "live"
    : lagMinutes < 360 ? "quiet"
    : "stalled";

  const statusColor = { live: "text-green-400", quiet: "text-yellow-400", stalled: "text-red-400", unknown: "text-zinc-500" }[botStatus];
  const dotColor = { live: "bg-green-500", quiet: "bg-yellow-500", stalled: "bg-red-500 animate-pulse", unknown: "bg-zinc-600" }[botStatus];
  const statusLabel = { live: "live", quiet: "quiet", stalled: "STALLED", unknown: "unknown" }[botStatus];

  const resolvedPct = health.totalAnalyzed > 0 ? Math.round(health.totalResolved / health.totalAnalyzed * 100) : 0;

  return (
    <div className="flex items-center gap-4 flex-wrap text-xs font-mono mb-4 px-1">
      {/* Bot status */}
      <div className="flex items-center gap-1.5">
        <div className={`w-1.5 h-1.5 rounded-full ${dotColor}`} />
        <span className={statusColor}>bot {statusLabel}</span>
        {lagMinutes !== null && (
          <span className="text-zinc-600">
            · last grad {lagMinutes < 60 ? `${lagMinutes}m` : `${Math.floor(lagMinutes / 60)}h`} ago
          </span>
        )}
      </div>

      <span className="text-zinc-700">|</span>

      {/* 24h activity */}
      <span className="text-zinc-500">
        <span className="text-zinc-300">{health.count24h}</span> grads/24h
      </span>

      <span className="text-zinc-700">|</span>

      {/* Total analyzed */}
      <span className="text-zinc-500">
        <span className="text-zinc-300">{health.totalAnalyzed}</span> analyzed total
      </span>

      <span className="text-zinc-700">|</span>

      {/* Outcomes resolved */}
      <span className="text-zinc-500">
        <span className={resolvedPct > 50 ? "text-green-400" : "text-yellow-400"}>{resolvedPct}%</span> outcomes resolved
      </span>

      <span className="text-zinc-700">|</span>

      {/* Skip rate */}
      <span className="text-zinc-500">
        <span className="text-red-400">{Math.round(health.skipRate * 100)}%</span> SKIP rate
      </span>
    </div>
  );
}
