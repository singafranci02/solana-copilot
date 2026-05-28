import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        sound: "#22c55e",
        watch: "#f59e0b",
        skip: "#ef4444",
        moon: "#a78bfa",
        rug: "#ef4444",
        ok: "#6b7280",
      },
    },
  },
  plugins: [],
};

export default config;
