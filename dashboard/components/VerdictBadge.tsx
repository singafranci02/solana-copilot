import type { Verdict } from "@/lib/types";
import { verdictLabel } from "@/lib/types";

const styles: Record<string, string> = {
  STRUCTURALLY_SOUND: "bg-green-900/60 text-green-300 border border-green-700",
  WATCH:              "bg-yellow-900/60 text-yellow-300 border border-yellow-700",
  SKIP:               "bg-red-900/60 text-red-300 border border-red-700",
};

export function VerdictBadge({ verdict }: { verdict: Verdict | null }) {
  const cls = verdict ? styles[verdict] : "bg-zinc-800 text-zinc-500";
  return (
    <span className={`inline-block rounded px-2 py-0.5 text-xs font-mono font-semibold ${cls}`}>
      {verdictLabel(verdict)}
    </span>
  );
}
