import { GraduationTable } from "@/components/GraduationTable";
import { HealthBar } from "@/components/HealthBar";

export const dynamic = "force-dynamic";
export const revalidate = 0;

export default function Home() {
  return (
    <div>
      <div className="mb-4">
        <h1 className="text-zinc-100 text-2xl font-bold tracking-tight">
          Graduated Tokens
        </h1>
        <p className="text-zinc-500 text-sm mt-1">
          Last 50 tokens that completed the Pump.fun bonding curve (~85 SOL raised → PumpSwap).
          Bot verdict shown at graduation time. 24h outcome updates live as it comes in.
        </p>
      </div>
      <HealthBar />
      <GraduationTable />
    </div>
  );
}
