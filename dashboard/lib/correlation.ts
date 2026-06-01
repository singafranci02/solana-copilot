// Client-side correlation analysis over graduation_feed rows.
// For each variable we bucket the tokens, compute rug rate per bucket, and
// measure "predictive power" = spread in rug rate across buckets that have
// enough samples. A variable whose rug rate barely moves across buckets is
// noise; one that swings from 30% to 90% is a real signal.

import type { GraduationRow, Outcome } from "./types";

export const MIN_BUCKET_N = 5;      // ignore buckets below this for spread calc
export const MIN_TOTAL_N = 20;      // variable needs this many resolved tokens

export interface Bucket {
  label: string;
  n: number;
  rugRate: number;
  moonRate: number;
}

export interface VariableInsight {
  id: string;
  name: string;
  description: string;
  buckets: Bucket[];
  totalN: number;
  spread: number;          // max rugRate − min rugRate over qualifying buckets
  predictive: "strong" | "moderate" | "weak" | "insufficient";
}

// resolved outcome — prefer 24h, fall back to 4h then 1h
function outcomeOf(r: GraduationRow): Outcome | null {
  return r.outcome_24h ?? r.outcome_4h ?? r.outcome_1h ?? null;
}

function buildBuckets(
  rows: GraduationRow[],
  bucketFn: (r: GraduationRow) => string | null,
  order?: string[],
): Bucket[] {
  const map = new Map<string, { n: number; rugs: number; moons: number }>();
  for (const r of rows) {
    const key = bucketFn(r);
    if (key === null) continue;
    const o = outcomeOf(r);
    if (o === null) continue;
    const cur = map.get(key) ?? { n: 0, rugs: 0, moons: 0 };
    cur.n += 1;
    if (o === "rug") cur.rugs += 1;
    if (o === "moon") cur.moons += 1;
    map.set(key, cur);
  }

  let entries = Array.from(map.entries());
  if (order) {
    entries = entries.sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0]));
  }

  return entries.map(([label, v]) => ({
    label,
    n: v.n,
    rugRate: v.n > 0 ? v.rugs / v.n : 0,
    moonRate: v.n > 0 ? v.moons / v.n : 0,
  }));
}

function classify(spread: number, totalN: number): VariableInsight["predictive"] {
  if (totalN < MIN_TOTAL_N) return "insufficient";
  if (spread >= 0.30) return "strong";
  if (spread >= 0.15) return "moderate";
  return "weak";
}

function makeInsight(
  id: string,
  name: string,
  description: string,
  rows: GraduationRow[],
  bucketFn: (r: GraduationRow) => string | null,
  order?: string[],
): VariableInsight {
  const buckets = buildBuckets(rows, bucketFn, order);
  const totalN = buckets.reduce((a, b) => a + b.n, 0);
  const qualifying = buckets.filter((b) => b.n >= MIN_BUCKET_N);
  const rugRates = qualifying.map((b) => b.rugRate);
  const spread = rugRates.length >= 2 ? Math.max(...rugRates) - Math.min(...rugRates) : 0;
  return { id, name, description, buckets, totalN, spread, predictive: classify(spread, totalN) };
}

export function computeInsights(rows: GraduationRow[]): VariableInsight[] {
  const insights: VariableInsight[] = [
    makeInsight(
      "team_supply",
      "Team supply % at graduation",
      "How much of supply the team cluster held when the token graduated.",
      rows,
      (r) => {
        const p = r.supply_pct_at_graduation;
        if (p === null) return null;
        if (p < 20) return "0-20%";
        if (p < 35) return "20-35%";
        if (p < 50) return "35-50%";
        return "50%+";
      },
      ["0-20%", "20-35%", "35-50%", "50%+"],
    ),
    makeInsight(
      "smart_money",
      "Smart money count",
      "Number of smart-money wallets that bought during the bonding curve.",
      rows,
      (r) => {
        const c = r.smart_money_count ?? 0;
        if (c === 0) return "0";
        if (c === 1) return "1";
        return "2+";
      },
      ["0", "1", "2+"],
    ),
    makeInsight(
      "sniper",
      "BC sniper flag",
      "Whether the team bought within 30s of launch.",
      rows,
      (r) => (r.is_bc_sniper === null ? null : r.is_bc_sniper ? "sniper" : "not sniper"),
      ["not sniper", "sniper"],
    ),
    makeInsight(
      "signal_1h",
      "1h distribution signal",
      "Team behaviour one hour after graduation.",
      rows,
      (r) => r.signal_1h,
      ["ACCUMULATING", "HOLDING", "DISTRIBUTING", "DUMPED"],
    ),
    makeInsight(
      "signal_4h",
      "4h distribution signal",
      "Team behaviour four hours after graduation.",
      rows,
      (r) => r.signal_4h,
      ["ACCUMULATING", "HOLDING", "DISTRIBUTING", "DUMPED"],
    ),
    makeInsight(
      "verdict",
      "Bot verdict",
      "How well the bot's own verdict separates rug from non-rug (the headline accuracy test).",
      rows,
      (r) => r.verdict,
      ["STRUCTURALLY_SOUND", "WATCH", "SKIP"],
    ),
    makeInsight(
      "funder_rug",
      "Funder historical rug rate",
      "The funding wallet's own track record across prior launches.",
      rows,
      (r) => {
        const fr = r.funder_rug_rate;
        if (fr === null) return null;
        if (fr < 0.2) return "0-20%";
        if (fr < 0.5) return "20-50%";
        if (fr < 0.8) return "50-80%";
        return "80-100%";
      },
      ["0-20%", "20-50%", "50-80%", "80-100%"],
    ),
    makeInsight(
      "detection",
      "Detection method",
      "Whether the token was caught live (WebSocket) or via REST poll.",
      rows,
      (r) => (r.detection_lag_seconds < 0 ? "REST poll" : "WebSocket"),
      ["WebSocket", "REST poll"],
    ),
  ];

  // Sort strongest signal first; insufficient data last
  const rank = { strong: 0, moderate: 1, weak: 2, insufficient: 3 };
  return insights.sort((a, b) => {
    if (rank[a.predictive] !== rank[b.predictive]) return rank[a.predictive] - rank[b.predictive];
    return b.spread - a.spread;
  });
}

export function overallStats(rows: GraduationRow[]) {
  let resolved = 0, rugs = 0, moons = 0, oks = 0;
  for (const r of rows) {
    const o = r.outcome_24h ?? r.outcome_4h ?? r.outcome_1h ?? null;
    if (o === null) continue;
    resolved += 1;
    if (o === "rug") rugs += 1;
    else if (o === "moon") moons += 1;
    else oks += 1;
  }
  return {
    resolved,
    baselineRugRate: resolved > 0 ? rugs / resolved : 0,
    moonRate: resolved > 0 ? moons / resolved : 0,
    okRate: resolved > 0 ? oks / resolved : 0,
  };
}
