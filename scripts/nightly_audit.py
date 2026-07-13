"""Nightly full-pipeline audit runner — Telegram ping ONLY on failure.

Runs eval.audit in full mode. Silence means green; a message means a stage
regressed and deploys should stop until it is fixed.

    launchd: com.solana-copilot.audit (daily 05:10, after the quiet US night)
"""

import asyncio
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    r = subprocess.run(
        [sys.executable, "-m", "eval.audit"],
        cwd=ROOT, capture_output=True, text=True, timeout=1800,
    )
    # refresh the track record regardless of audit result — it must never skip a day
    try:
        subprocess.run([sys.executable, "scripts/track_record.py"],
                       cwd=ROOT, capture_output=True, timeout=300)
    except Exception:
        pass
    if r.returncode == 0:
        return 0
    tail = "\n".join((r.stdout or "").strip().splitlines()[-12:])
    from src.notifications.telegram import send_message
    asyncio.run(send_message(
        "🔴 <b>NIGHTLY AUDIT FAILED</b>\n"
        "A pipeline stage regressed — do not deploy until fixed.\n"
        f"<pre>{tail[:3000]}</pre>"
    ))
    return 1


if __name__ == "__main__":
    sys.exit(main())
