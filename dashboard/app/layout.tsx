import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "solana-copilot",
  description: "Pump.fun graduation monitor — structural analysis feed",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen antialiased">
        <header className="border-b border-zinc-800 px-6 py-4 flex items-center gap-3">
          <div className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
          <span className="text-zinc-200 font-semibold tracking-tight">solana-copilot</span>
          <span className="text-zinc-600 text-sm">graduation feed</span>
        </header>
        <main className="max-w-7xl mx-auto px-4 sm:px-6 py-6">
          {children}
        </main>
      </body>
    </html>
  );
}
