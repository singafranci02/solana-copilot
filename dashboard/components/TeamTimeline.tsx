"use client";

import { useEffect, useState } from "react";
import { supabase } from "@/lib/supabase";
import type { GraduationRow, PostGradSwap, BcAccumulation, CoinCoordination, CoordinatedEntity } from "@/lib/types";

export function TeamTimeline({ row }: { row: GraduationRow }) {
  const [swaps, setSwaps] = useState<PostGradSwap[] | null>(null);
  const [bc, setBc] = useState<BcAccumulation[] | null>(null);
  const [coord, setCoord] = useState<CoinCoordination | null>(null);
  const [entities, setEntities] = useState<CoordinatedEntity[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    (async () => {
      const [swapRes, bcRes, coordRes, entRes] = await Promise.all([
        supabase.from("post_grad_swaps").select("*").eq("token_mint", row.token_mint).order("ts", { ascending: true }),
        supabase.from("bc_accumulation").select("*").eq("token_mint", row.token_mint).order("first_buy_offset_s", { ascending: true }),
        supabase.from("coin_coordination").select("*").eq("token_mint", row.token_mint).eq("phase", "launch").maybeSingle(),
        supabase.from("coordinated_entities").select("*").eq("token_mint", row.token_mint).eq("phase", "launch").order("supply_pct", { ascending: false }),
      ]);
      setSwaps((swapRes.data as PostGradSwap[]) ?? []);
      setBc((bcRes.data as BcAccumulation[]) ?? []);
      setCoord((coordRes.data as unknown as CoinCoordination) ?? null);
      setEntities((entRes.data as CoordinatedEntity[]) ?? []);
      setLoading(false);
    })();
  }, [row.token_mint]);

  return (
    <div className="bg-zinc-950/60 border-t border-zinc-800 px-4 py-4">
      {/* Aggregate metrics */}
      <div className="flex flex-wrap gap-2 mb-3">
        <Metric label="buys" value={row.team_buy_count_24h} color="green" />
        <Metric label="sells" value={row.team_sell_count_24h} color="red" />
        <Metric
          label="net SOL"
          value={row.team_net_sol_24h !== null ? row.team_net_sol_24h.toFixed(2) : null}
          color={row.team_net_sol_24h !== null && row.team_net_sol_24h > 0 ? "red" : "green"}
          hint={row.team_net_sol_24h !== null && row.team_net_sol_24h > 0 ? "team took SOL out" : "team net buyer"}
        />
        <Metric
          label="snipers sold"
          value={row.snipers_sold_pct_24h !== null ? `${row.snipers_sold_pct_24h.toFixed(0)}%` : null}
          color={row.snipers_sold_pct_24h !== null && row.snipers_sold_pct_24h > 50 ? "red" : "zinc"}
        />
        <Metric
          label="coordinated dumps"
          value={row.coordinated_sell_count_24h}
          color={row.coordinated_sell_count_24h !== null && row.coordinated_sell_count_24h > 0 ? "red" : "zinc"}
        />
        <Metric
          label="liquidity"
          value={row.liquidity_usd_24h !== null ? `$${formatUsd(row.liquidity_usd_24h)}` : null}
          color="zinc"
        />
      </div>

      {/* Holder trajectory (F3) */}
      <div className="flex flex-wrap gap-2 mb-3">
        <Metric label="holders" value={row.holder_count_24h} color="zinc" hint="top-20 tracked count" />
        <Metric
          label="top10 conc"
          value={row.top10_pct_24h !== null ? `${row.top10_pct_24h.toFixed(0)}%` : null}
          color={row.top10_pct_24h !== null && row.top10_pct_24h > 60 ? "red" : "zinc"}
        />
        <Metric
          label="new holders"
          value={row.new_holder_count_24h}
          color={row.new_holder_count_24h !== null && row.new_holder_count_24h > 0 ? "green" : "zinc"}
        />
        <Metric label="churned" value={row.churned_holder_count_24h} color="zinc" />
        <Metric
          label="new smart $"
          value={row.new_smart_money_count_24h}
          color={row.new_smart_money_count_24h !== null && row.new_smart_money_count_24h > 0 ? "green" : "zinc"}
          hint="smart money entering post-grad = bullish"
        />
      </div>

      {/* Coordinated-entity detection */}
      {coord && (
        <div className="mb-3">
          <div className="flex items-center gap-2 mb-1.5">
            <p className="text-zinc-600 text-xs uppercase tracking-wide">Coordinated entities</p>
            {coord.bundled_supply_pct > 30 && (
              <span className="inline-flex px-1.5 py-0.5 rounded text-xs font-mono border bg-red-900/50 text-red-300 border-red-800">
                ⚠ {coord.bundled_supply_pct.toFixed(0)}% bundled
              </span>
            )}
          </div>
          <div className="flex flex-wrap gap-2 mb-2">
            <Metric label="bundled supply" value={`${coord.bundled_supply_pct.toFixed(1)}%`}
              color={coord.bundled_supply_pct > 30 ? "red" : "zinc"} hint="% of buy volume in same-slot bundles" />
            <Metric label="entities" value={coord.entity_count} color="zinc"
              hint="distinct coordinated wallet groups" />
            <Metric label="largest team" value={`${coord.largest_entity_wallet_count}w`}
              color={coord.largest_entity_wallet_count >= 3 ? "red" : "zinc"}
              hint={`controls ${coord.largest_entity_supply_pct.toFixed(1)}% of supply`} />
            <Metric label="fresh ratio"
              value={`${(coord.largest_entity_fresh_ratio * 100).toFixed(0)}%`}
              color={coord.largest_entity_fresh_ratio > 0.5 ? "red" : "zinc"}
              hint="fraction of largest entity that are fresh wallets" />
            {coord.largest_entity_state && (
              <Metric label="state" value={coord.largest_entity_state}
                color={["DISTRIBUTING", "DUMPED"].includes(coord.largest_entity_state) ? "red" : "green"} />
            )}
          </div>
          {entities.length > 0 && (
            <div className="space-y-1 max-h-32 overflow-y-auto">
              {entities.slice(0, 6).map((e) => (
                <div key={e.entity_id} className="flex items-center gap-3 text-xs font-mono">
                  <span className="text-zinc-300 w-12 text-right">{e.supply_pct.toFixed(1)}%</span>
                  <span className="text-zinc-500 w-10">{e.wallet_count}w</span>
                  <span className={`w-24 ${["DISTRIBUTING", "DUMPED"].includes(e.state ?? "") ? "text-red-400" : "text-zinc-400"}`}>
                    {e.state}
                  </span>
                  <span className="text-zinc-600 truncate">{(e.edge_sources ?? []).join(", ")}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* BC accumulation (F1) */}
      {bc && bc.length > 0 && (
        <div className="mb-3">
          <p className="text-zinc-600 text-xs uppercase tracking-wide mb-1.5">
            Bonding-curve accumulation (pre-graduation)
          </p>
          <div className="space-y-1 max-h-40 overflow-y-auto">
            {bc.map((h) => (
              <div key={h.wallet_address} className="flex items-center gap-3 text-xs font-mono">
                <StyleTag style={h.accumulation_style} />
                <span className="text-zinc-500 w-20">
                  {h.first_buy_offset_s !== null ? `+${Math.round(h.first_buy_offset_s)}s` : "—"}
                </span>
                <span className="text-zinc-400 w-24 text-right">{h.total_sol_in.toFixed(2)} SOL in</span>
                <span className="text-zinc-600 w-16">{h.bc_buy_count}b / {h.bc_sell_count}s</span>
                <a
                  href={`https://solscan.io/account/${h.wallet_address}`}
                  target="_blank" rel="noopener noreferrer"
                  className="text-zinc-600 hover:text-zinc-400 truncate"
                >
                  {h.wallet_address.slice(0, 4)}…{h.wallet_address.slice(-4)}
                </a>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Post-grad timeline */}
      {loading ? (
        <p className="text-zinc-600 text-xs">loading transactions…</p>
      ) : !swaps || swaps.length === 0 ? (
        <p className="text-zinc-600 text-xs">
          No team transactions recorded yet. Tracking runs at the 1h / 4h / 24h checks for
          tokens that still have liquidity.
        </p>
      ) : (
        <SwapTimeline swaps={swaps} graduatedAt={row.graduated_at} />
      )}
    </div>
  );
}

function SwapTimeline({ swaps, graduatedAt }: { swaps: PostGradSwap[]; graduatedAt: number }) {
  // cumulative net SOL out (sells positive, buys negative) for the sparkline
  let cum = 0;
  const points = swaps.map((s) => {
    cum += s.side === "sell" ? s.sol_amount : -s.sol_amount;
    return { ts: s.ts, cum, swap: s };
  });
  const maxAbs = Math.max(1, ...points.map((p) => Math.abs(p.cum)));

  return (
    <div className="space-y-3">
      {/* cumulative net-SOL bar */}
      <div>
        <p className="text-zinc-600 text-xs mb-1">cumulative net SOL (right = team taking SOL out)</p>
        <div className="flex items-end gap-px h-10">
          {points.map((p, i) => {
            const h = Math.max(2, (Math.abs(p.cum) / maxAbs) * 40);
            const out = p.cum > 0;
            return (
              <div
                key={i}
                className={`flex-1 ${out ? "bg-red-600/70" : "bg-green-600/70"}`}
                style={{ height: `${h}px` }}
                title={`${p.swap.side} ${p.swap.sol_amount.toFixed(3)} SOL — cum ${p.cum.toFixed(2)}`}
              />
            );
          })}
        </div>
      </div>

      {/* individual swaps */}
      <div className="max-h-48 overflow-y-auto space-y-1">
        {swaps.map((s, i) => {
          const mins = Math.round((s.ts - graduatedAt) / 60);
          return (
            <div key={i} className="flex items-center gap-3 text-xs font-mono">
              <span className={`w-10 ${s.side === "sell" ? "text-red-400" : "text-green-400"}`}>
                {s.side.toUpperCase()}
              </span>
              <span className="text-zinc-500 w-16">+{mins}m</span>
              <span className="text-zinc-300 w-20 text-right">{s.sol_amount.toFixed(3)} SOL</span>
              <a
                href={`https://solscan.io/account/${s.wallet_address}`}
                target="_blank"
                rel="noopener noreferrer"
                className="text-zinc-600 hover:text-zinc-400 truncate"
              >
                {s.wallet_address.slice(0, 4)}…{s.wallet_address.slice(-4)}
              </a>
              {s.is_sniper && <span className="text-orange-400" title="BC sniper">⚡</span>}
              {s.is_smart_money && <span className="text-green-400" title="smart money">★</span>}
              {!s.is_team && <span className="text-zinc-600" title="non-team holder">·</span>}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function StyleTag({ style }: { style: string | null }) {
  const map: Record<string, string> = {
    sniped: "bg-red-900/50 text-red-300 border-red-800",
    gradual: "bg-zinc-800/50 text-zinc-400 border-zinc-700",
    single: "bg-yellow-900/40 text-yellow-300 border-yellow-800",
  };
  const cls = style ? map[style] ?? map.gradual : "bg-zinc-800/50 text-zinc-600 border-zinc-700";
  return (
    <span className={`inline-flex px-1.5 py-0.5 rounded text-xs font-mono border w-16 justify-center ${cls}`}>
      {style ?? "—"}
    </span>
  );
}

function Metric({ label, value, color, hint }: {
  label: string; value: string | number | null; color: "green" | "red" | "zinc"; hint?: string;
}) {
  const colors = { green: "text-green-400", red: "text-red-400", zinc: "text-zinc-300" };
  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded px-2.5 py-1.5" title={hint}>
      <span className="text-zinc-600 text-xs">{label} </span>
      <span className={`text-xs font-mono font-bold ${colors[color]}`}>
        {value ?? "—"}
      </span>
    </div>
  );
}

function formatUsd(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return n.toFixed(0);
}
