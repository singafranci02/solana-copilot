import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "solana-copilot",
  description: "Pump.fun graduation monitor — structural analysis feed",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen antialiased">
        <header className="border-b border-zinc-800 px-6 py-3 flex items-center gap-4">
          <div className="flex items-center gap-2">
            <div className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
            <span className="text-zinc-200 font-semibold tracking-tight">solana-copilot</span>
          </div>
          <nav className="flex items-center gap-1 ml-2">
            {[
              ["/", "Feed"],
              ["/system", "System"],
              ["/algorithm", "Algorithm"],
              ["/insights", "Insights"],
            ].map(([href, label]) => (
              <Link
                key={href}
                href={href}
                className="px-3 py-1 rounded text-xs font-mono text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800 transition-colors"
              >
                {label}
              </Link>
            ))}
          </nav>
        </header>
        <main className="max-w-7xl mx-auto px-4 sm:px-6 py-6">
          {children}
        </main>
      </body>
    </html>
  );
}
