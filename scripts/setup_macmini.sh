#!/usr/bin/env bash
# setup_macmini.sh — idempotent one-time setup for the Mac mini
# Run once as your regular user (not root). sudo will be prompted for pmset.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
USERNAME=$(whoami)

echo "==> solana-copilot Mac mini setup"
echo "    project root: $PROJECT_DIR"
echo "    user:         $USERNAME"
echo ""

# ── 1. Homebrew ────────────────────────────────────────────────────────────────
if command -v brew &>/dev/null; then
  echo "[skip] Homebrew already installed"
else
  echo "[install] Installing Homebrew…"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null || true
fi

# ── 2. uv ─────────────────────────────────────────────────────────────────────
if command -v uv &>/dev/null; then
  echo "[skip] uv already installed ($(uv --version))"
else
  echo "[install] Installing uv…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# ── 3. Ollama ─────────────────────────────────────────────────────────────────
if command -v ollama &>/dev/null; then
  echo "[skip] Ollama already installed"
else
  echo "[install] Installing Ollama via Homebrew…"
  brew install ollama
fi

if ! pgrep -x ollama &>/dev/null; then
  echo "[start] Starting Ollama server in background…"
  nohup ollama serve &>/tmp/ollama.log &
  sleep 3
fi

# ── 4. Pull llama3.1:8b ───────────────────────────────────────────────────────
if ollama list 2>/dev/null | grep -q "llama3.1:8b"; then
  echo "[skip] llama3.1:8b already pulled"
else
  echo "[pull] Pulling llama3.1:8b (takes a few minutes)…"
  ollama pull llama3.1:8b
fi

# ── 5. Disable sleep ──────────────────────────────────────────────────────────
CURRENT_SLEEP=$(sudo pmset -g | awk '/^[ \t]*sleep/ {print $2}')
if [ "${CURRENT_SLEEP:-1}" = "0" ]; then
  echo "[skip] Sleep already disabled"
else
  echo "[config] Disabling system sleep (requires sudo)…"
  sudo pmset -a sleep 0
  sudo pmset -a disksleep 0
  sudo pmset -a displaysleep 0
fi

# ── 6. Create directories ──────────────────────────────────────────────────────
echo "[setup] Creating db/ and logs/ directories…"
mkdir -p "$PROJECT_DIR/db"
mkdir -p "$PROJECT_DIR/logs"

# ── 7. .env ───────────────────────────────────────────────────────────────────
if [ -f "$PROJECT_DIR/.env" ]; then
  echo "[skip] .env already exists"
else
  echo "[setup] Creating .env from .env.example…"
  cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
  echo "  !! Add your HELIUS_API_KEY to .env before starting services."
fi

# ── 8. Install dependencies ───────────────────────────────────────────────────
echo "[install] Running uv sync…"
cd "$PROJECT_DIR"
uv sync

# ── 9. Run DB migration ───────────────────────────────────────────────────────
echo "[db] Running schema migration…"
cd "$PROJECT_DIR"
uv run python -c "from src.common.db import migrate; migrate()"
echo "[db] Migration complete."

# ── 10. Install launchd services ──────────────────────────────────────────────
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$LAUNCH_AGENTS"

SERVICES=(analyzer wallet_watcher pump_monitor narrative_tracker graduation_monitor)

for svc in "${SERVICES[@]}"; do
  PLIST_SRC="$PROJECT_DIR/scripts/launchd/${svc}.plist"
  PLIST_DST="$LAUNCH_AGENTS/com.solana-copilot.${svc//_/-}.plist"

  if [ ! -f "$PLIST_SRC" ]; then
    echo "[skip] no plist for $svc"
    continue
  fi

  # Replace YOUR_USERNAME placeholder with actual username
  sed "s/YOUR_USERNAME/$USERNAME/g" "$PLIST_SRC" > "$PLIST_DST"

  # Unload first if already loaded (idempotent)
  launchctl unload "$PLIST_DST" 2>/dev/null || true
  launchctl load "$PLIST_DST"
  echo "[service] installed + loaded: com.solana-copilot.${svc//_/-}"
done

echo ""
echo "==> Setup complete."
echo ""
echo "Services running:"
launchctl list | grep solana-copilot || echo "  (none found — check logs/)"
echo ""
echo "Next steps:"
echo "  1. Make sure .env has HELIUS_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY"
echo "  2. Watch graduation feed: tail -f $PROJECT_DIR/logs/graduation_monitor.log"
echo "  3. Watch pump monitor:    tail -f $PROJECT_DIR/logs/pump_monitor.log"
