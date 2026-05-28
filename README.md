# Solana Co-pilot

A Solana memecoin trading co-pilot specialised for **Pump.fun** and **Bags** launchpads.
Paste a token mint address → get a plain-English trader summary of who's behind the coin,
what the bundle looks like, and whether smart money is in.

---

## Architecture

```
Mac mini (always-on)
├── analyzer        FastAPI server on :8000  → web UI + REST API
├── wallet_watcher  polls smart-money wallets via Helius
└── narrative_tracker  clusters X/Twitter mentions into active narratives

Your laptop/phone → connects via local network or Tailscale
```

All services are managed by launchd and restart automatically.

---

## Prerequisites

| Tool | Min version | Install |
|------|-------------|---------|
| macOS | 13 Ventura | — |
| Homebrew | any | `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"` |
| uv | 0.5+ | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Ollama | 0.5+ | `brew install ollama` |
| llama3.1:8b | — | `ollama pull llama3.1:8b` |

---

## Mac mini specific setup

### Disable sleep (required for always-on)

```bash
sudo pmset -a sleep 0
sudo pmset -a disksleep 0
sudo pmset -a displaysleep 0
```

Re-enable with `sudo pmset -a sleep 1` when needed.

### Tailscale (optional but recommended for remote access)

```bash
brew install --cask tailscale
# Open Tailscale.app → log in → enable MagicDNS
# Then access the co-pilot from your laptop at http://macmini:8000
```

Without Tailscale, the server is only reachable on your local LAN.

---

## One-time setup

```bash
# 1. Clone the repo onto the Mac mini
git clone <your-repo> ~/solana-copilot
cd ~/solana-copilot

# 2. Run the idempotent setup script
bash scripts/setup_macmini.sh
# This installs Homebrew, uv, Ollama, pulls llama3.1:8b,
# disables sleep, and runs uv sync.

# 3. Fill in your API keys
cp .env.example .env
nano .env   # or your editor of choice
```

---

## Environment variables (.env)

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `HELIUS_API_KEY` | Yes | — | Get at helius.dev |
| `GMGN_API_KEY` | Yes | — | Get at gmgn.ai |
| `BAGS_API_KEY` | Yes | — | Get at bags.fm |
| `X_API_KEY` | No | — | X API v2 bearer token. Narrative tracking stubs without it. |
| `ANTHROPIC_API_KEY` | No | — | Only needed when `LLM_PROVIDER=anthropic` |
| `LLM_PROVIDER` | No | `ollama` | `ollama` or `anthropic` |
| `LLM_MODEL` | No | `llama3.1:8b` | Any Ollama model name, or Claude model ID |
| `DB_PATH` | No | `./db/copilot.db` | Relative to project root |
| `SERVER_HOST` | No | `0.0.0.0` | Bind address |
| `SERVER_PORT` | No | `8000` | HTTP port |

---

## Install dependencies

```bash
cd ~/solana-copilot
uv sync
```

This creates `.venv/` inside the project directory.

---

## Run in development

Open four terminal tabs (or use tmux):

```bash
# Tab 1 — analyzer API + web UI
cd ~/solana-copilot
uv run uvicorn src.services.analyzer_server:app --reload --host 0.0.0.0 --port 8000

# Tab 2 — wallet watcher
uv run python -m src.services.wallet_watcher

# Tab 3 — narrative tracker
uv run python -m src.services.narrative_tracker

# Tab 4 — Ollama (if not already running)
ollama serve
```

Open `http://localhost:8000` in your browser.

---

## Run tests

```bash
cd ~/solana-copilot
uv run pytest -v
```

---

## Production: install as launchd services

The plist files live in `scripts/launchd/`. Before installing, **replace
`YOUR_USERNAME`** with your Mac mini username in all three files:

```bash
# Quick replacement (run from project root)
USERNAME=$(whoami)
sed -i '' "s/YOUR_USERNAME/$USERNAME/g" scripts/launchd/*.plist
```

Then install and load:

```bash
mkdir -p ~/Library/LaunchAgents
mkdir -p ~/solana-copilot/logs

cp scripts/launchd/analyzer.plist         ~/Library/LaunchAgents/com.solana-copilot.analyzer.plist
cp scripts/launchd/wallet_watcher.plist   ~/Library/LaunchAgents/com.solana-copilot.wallet-watcher.plist
cp scripts/launchd/narrative_tracker.plist ~/Library/LaunchAgents/com.solana-copilot.narrative-tracker.plist

launchctl load ~/Library/LaunchAgents/com.solana-copilot.analyzer.plist
launchctl load ~/Library/LaunchAgents/com.solana-copilot.wallet-watcher.plist
launchctl load ~/Library/LaunchAgents/com.solana-copilot.narrative-tracker.plist
```

Check status:

```bash
launchctl list | grep solana-copilot
```

View logs:

```bash
tail -f ~/solana-copilot/logs/analyzer.log
tail -f ~/solana-copilot/logs/wallet_watcher.log
tail -f ~/solana-copilot/logs/narrative_tracker.log
```

Stop a service:

```bash
launchctl unload ~/Library/LaunchAgents/com.solana-copilot.analyzer.plist
```

---

## Project layout

```
src/
  common/      config, db, models (shared across services)
  ingest/      Helius, GMGN, Bags, X API clients
  analyzer/    clustering, team detection, smart money, summarize
  strategy/    entry/exit rules + backtester
  services/    FastAPI server, wallet watcher, narrative tracker, journal
  ui/          Jinja2 templates + CSS
db/
  schema.sql   SQLite schema (run via db.migrate())
scripts/
  setup_macmini.sh
  launchd/     plist files for each service
tests/
  test_smoke.py
```

---

## Session log

| Session | Focus |
|---------|-------|
| 1 | Project bootstrap — structure, schema, stubs, setup script |
| 2 | Helius client, wallet clustering, team detection, LLM summarize |
| 3 | GMGN + Bags integration, smart money scoring |
| 4 | Narrative tracker, X ingest |
| 5 | Rules engine + backtester |
| 6 | Journal + web UI polish |
