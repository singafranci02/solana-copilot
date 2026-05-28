import type { DistributionSignal } from "@/lib/types";

const styles: Record<string, string> = {
  ACCUMULATING: "text-green-400",
  HOLDING:      "text-zinc-400",
  DISTRIBUTING: "text-yellow-400",
  DUMPED:       "text-red-500",
};

const abbr: Record<string, string> = {
  ACCUMULATING: "ACC",
  HOLDING:      "HOL",
  DISTRIBUTING: "DIST",
  DUMPED:       "DUMP",
};

export function SignalPip({ signal }: { signal: DistributionSignal | null }) {
  if (!signal) return <span className="text-zinc-700 text-xs">—</span>;
  return (
    <span className={`text-xs font-mono font-semibold ${styles[signal]}`}>
      {abbr[signal]}
    </span>
  );
}
