"""Weekly gated retrain — how the system "learns with time" without hand-holding.

Retrains both artifacts on all data to date (each retrain re-derives the calibrated
alert threshold), then runs the FULL audit. If the audit fails, the previous
artifacts are restored and Telegram is pinged — a bad retrain can never go live.

Measured basis: recency weighting adds nothing yet (NEGATIVE_RESULTS #10) — the
right cadence is simply retraining on everything, weekly, gated.
"""

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
MODELS = ROOT / "models"
ARTIFACTS = ["verdict_model_v4.pkl", "early_model_v1.pkl"]


def run(args, timeout):
    return subprocess.run([sys.executable, *args], cwd=ROOT,
                          capture_output=True, text=True, timeout=timeout)


def main() -> int:
    backups = {}
    for a in ARTIFACTS:
        src = MODELS / a
        if src.exists():
            backups[a] = src.with_suffix(".pkl.bak")
            shutil.copy2(src, backups[a])

    r1 = run(["scripts/train_model.py"], 3600)
    r2 = run(["scripts/train_early_model.py"], 3600)
    audit = run(["-m", "eval.audit"], 1800)

    if r1.returncode == 0 and r2.returncode == 0 and audit.returncode == 0:
        for b in backups.values():
            b.unlink(missing_ok=True)
        # graduation monitor caches the artifact per process — reload it
        subprocess.run(["launchctl", "kickstart", "-k",
                        "gui/501/com.solana-copilot.graduation-monitor"],
                       capture_output=True)
        return 0

    for a, b in backups.items():          # restore — a bad retrain never ships
        shutil.copy2(b, MODELS / a)
        b.unlink(missing_ok=True)
    tail = "\n".join((audit.stdout or r1.stderr or r2.stderr or "").splitlines()[-8:])
    from src.notifications.telegram import send_message
    asyncio.run(send_message(
        "🔴 <b>WEEKLY RETRAIN REJECTED</b>\n"
        "New artifacts failed the audit — previous models restored, nothing shipped.\n"
        f"<pre>{tail[:2500]}</pre>"))
    return 1


if __name__ == "__main__":
    sys.exit(main())
