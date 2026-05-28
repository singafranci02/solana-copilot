import type { Outcome } from "@/lib/types";

const styles: Record<string, string> = {
  moon: "bg-purple-900/60 text-purple-300 border border-purple-700",
  ok:   "bg-zinc-700/60 text-zinc-300 border border-zinc-600",
  rug:  "bg-red-900/60 text-red-400 border border-red-800",
};

const labels: Record<string, string> = {
  moon: "🌙 MOON",
  ok:   "OK",
  rug:  "💀 RUG",
};

export function OutcomeChip({ outcome }: { outcome: Outcome | null }) {
  if (!outcome) return <span className="text-zinc-600 text-xs">pending</span>;
  return (
    <span className={`inline-block rounded px-2 py-0.5 text-xs font-mono font-semibold ${styles[outcome]}`}>
      {labels[outcome]}
    </span>
  );
}
