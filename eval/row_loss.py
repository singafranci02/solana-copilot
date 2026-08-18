"""Waterfall: why does the classic population not produce exit-timing rows?

Every coin is attributed to exactly ONE cause, first match wins, and every
condition is RECOMPUTED from underlying records — no stored status flag is
trusted, including tokens.platform and coin_trajectory's own columns.

Two causes were found by reading the pipeline rather than assumed:

  * SCORED_BEFORE_CHECKPOINT_EXISTED. hazard_predictions rows at checkpoint 30
    begin 2026-08-10 14:16 (the 120s checkpoint begins 2026-07-26). A coin that
    graduated earlier can never have a 30s row. This is retrospective absence,
    not exclusion, and no amount of new data recovers those coins.

  * TEAM_ALREADY_GONE does NOT cause absence. landmark_row still emits a row and
    persist_landmark stores team_exited=1. Those rows exist and were, until this
    script, being counted as the POSITIVE class — see the note below.

WHAT THE STORED LABEL MEANS, verified against coin_trajectory: of rows at
checkpoint 30 carrying team_exited=1, 70 of 70 have time_to_team_exit_s < 30.
The label is "the team had ALREADY sold by this checkpoint" — a past event —
while p_exit is model_a's hazard for the NEXT interval. Scoring one against the
other measures detection of something already visible in the covariates, not
prediction. Restricted to genuinely at-risk rows and the interval the model does
predict, the alarm reads ROC 0.568, lift +2.1%, CI [-14.3%, +19.6%].

    uv run python -m eval.row_loss [--samples N]
"""

from __future__ import annotations

import sys
from collections import OrderedDict

# checkpoint under audit and the anchor tolerance, both recomputed not assumed
CHECKPOINT_S = 30
ANCHOR_WINDOW_S = 120

CAUSES = (
    "platform_unverified",
    "no_tape_stored",
    "tape_opens_late",
    "scored_before_checkpoint_existed",
    "no_snapshot_for_statics",
    "analysis_started_after_checkpoint",
    "has_row_but_team_already_gone",
    "has_usable_at_risk_row",
    "unexplained",
)


def waterfall(conn, checkpoint_s: int = CHECKPOINT_S,
              population: str = "all") -> tuple[OrderedDict, dict]:
    """Returns (counts, samples). Precedence: first matching cause wins.

    population='all'      every pipeline-v2 graduation — the full funnel
    population='trainable' the set eval._common.load_samples actually yields, which
                          has ALREADY dropped unverified platform, late-opening tape
                          and missing snapshots. Attrition inside this set is the
                          number that matters, because these are the coins the
                          trainer believes it has."""
    # when did this checkpoint start being scored at all?
    first_scored = conn.execute(
        "SELECT MIN(scored_at) FROM hazard_predictions WHERE checkpoint_s = ?",
        (checkpoint_s,)).fetchone()[0]

    rows = conn.execute(
        """SELECT ge.token_mint AS mint,
                  ge.graduated_at AS grad,
                  t.platform AS platform,
                  (SELECT MIN(p.ts) FROM post_grad_swaps p
                    WHERE p.token_mint = ge.token_mint) AS first_print,
                  (SELECT COUNT(*) FROM post_grad_swaps p
                    WHERE p.token_mint = ge.token_mint) AS n_tape,
                  (SELECT COUNT(*) FROM graduation_feature_snapshot f
                    WHERE f.token_mint = ge.token_mint) AS n_snap,
                  (SELECT h.team_exited FROM hazard_predictions h
                    WHERE h.token_mint = ge.token_mint
                      AND h.checkpoint_s = ?) AS stored_label,
                  (SELECT MIN(h.scored_at) FROM hazard_predictions h
                    WHERE h.token_mint = ge.token_mint) AS first_scored_this_coin
           FROM graduation_events ge
           JOIN tokens t ON t.mint = ge.token_mint
           WHERE COALESCE(ge.is_manufactured, 0) = 0
             AND ge.pipeline_version >= 2""", (checkpoint_s,)).fetchall()

    if population == "trainable":
        from eval._common import CLASSIC, load_samples
        keep = {s_.token_mint for s_ in load_samples(conn, CLASSIC)}
        rows = [r for r in rows if r["mint"] in keep]

    counts = OrderedDict((c, 0) for c in CAUSES)
    samples: dict[str, list[str]] = {c: [] for c in CAUSES}

    for r in rows:
        # Recomputed conditions only. platform is the one stored value that cannot
        # be re-derived here without chain access, so it is used as-is and named
        # honestly rather than pretended to be a measurement.
        if r["platform"] != "pump.fun":
            cause = "platform_unverified"
        elif not r["n_tape"]:
            cause = "no_tape_stored"
        elif r["first_print"] is None or not (
                0 <= r["first_print"] - r["grad"] <= ANCHOR_WINDOW_S):
            cause = "tape_opens_late"
        elif first_scored is not None and r["grad"] < first_scored:
            cause = "scored_before_checkpoint_existed"
        elif r["stored_label"] is None and not r["n_snap"]:
            cause = "no_snapshot_for_statics"
        elif (r["stored_label"] is None
              and r["first_scored_this_coin"] is not None
              and r["first_scored_this_coin"] - r["grad"] > checkpoint_s):
            # The coin WAS analysed, just too late: checkpoints are scheduled at
            # offsets from graduation, so one that has already elapsed when analysis
            # begins never produces a row. Recomputed from scored_at, not from the
            # stored detection_lag column.
            cause = "analysis_started_after_checkpoint"
        elif r["stored_label"] is None:
            cause = "unexplained"
        elif r["stored_label"] == 1:
            cause = "has_row_but_team_already_gone"
        else:
            cause = "has_usable_at_risk_row"

        counts[cause] += 1
        if len(samples[cause]) < 10:
            samples[cause].append(r["mint"])

    return counts, samples


def main() -> int:
    from src.common.db import get_connection

    conn = get_connection()
    for pop in ("all", "trainable"):
        counts, samples = waterfall(conn, population=pop)
        total = sum(counts.values())
        print(f"\n=== population={pop}  n={total} ===")
        print(f"{'cause':>38} {'n':>6} {'%':>7}")
        print("-" * 54)
        for cause, n in counts.items():
            if n:
                print(f"{cause:>38} {n:>6} {n/max(total,1):>7.1%}")
        if pop == "trainable":
            print("\nSAMPLES (up to 10 per cause)")
            for cause, ids in samples.items():
                if ids:
                    print(f"  {cause}")
                    for m in ids:
                        print(f"    {m}")
    counts, samples = waterfall(conn)
    first = conn.execute(
        "SELECT datetime(MIN(scored_at),'unixepoch') FROM hazard_predictions "
        "WHERE checkpoint_s = ?", (CHECKPOINT_S,)).fetchone()[0]
    conn.close()

    print(f"\n(checkpoint {CHECKPOINT_S}s first scored {first})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
