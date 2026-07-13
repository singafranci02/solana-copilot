"""HOW the team is selling — a risk grade, never an entry signal.

Measured out-of-time on the 0-15min window (n=940, see eval/NEGATIVE_RESULTS.md §6),
the *shape* of the team's exit genuinely separates what happens next. The single
strongest read is simply how many of them are heading for the door:

    few team sellers, done early   -> 29.1% chance of a sustained >=2x bounce
    many team sellers, still going -> 11.3%

That is a 2.6x spread on the same event, so it belongs in the exit alarm: a lone wallet
trimming is not the same event as the whole cluster unloading, and the alert should not
say the same thing about both.

READ THIS BEFORE USING IT FOR ANYTHING ELSE. The bounce these features discriminate is
NOT tradeable. You capture +46% when it bounces and eat -41% when it doesn't, so you
need a 47.1% hit rate to break even — and the best slice of this feature space reaches
29.4%. Every entry rule tested loses money. The gap is arithmetic, not a modelling
failure, and no amount of extra features closes it.

So: severity grading for a HOLDER deciding to exit. Never a buy signal.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SellStructure:
    n_sells: int = 0
    n_sellers: int = 0            # distinct team wallets selling — the strongest read
    share_of_sells: float = 0.0   # team's share of all sell volume in the window
    still_selling: bool = False   # selling in the final third = the exit isn't finished
    severity: str = "LOW"         # LOW | ELEVATED | CRITICAL
    note: str = ""


def grade_sell_structure(swaps, graduated_at: int, team: set[str],
                         window_s: int) -> SellStructure | None:
    """Grade the team's exit shape over the first `window_s`. Pure. None if no team sell."""
    cut = graduated_at + window_s
    win = [s for s in swaps if graduated_at <= s.timestamp <= cut]
    team_sells = [s for s in win if s.side == "sell" and s.signer in team]
    if not team_sells:
        return None

    g = SellStructure()
    g.n_sells = len(team_sells)
    g.n_sellers = len({s.signer for s in team_sells})

    all_sell_sol = sum(s.sol_amount or 0.0 for s in win if s.side == "sell")
    team_sell_sol = sum(s.sol_amount or 0.0 for s in team_sells)
    g.share_of_sells = team_sell_sol / all_sell_sol if all_sell_sol > 0 else 0.0

    last_third = graduated_at + window_s * 2 / 3
    g.still_selling = any(s.timestamp >= last_third for s in team_sells)

    # Thresholds are the measured terciles, not invented numbers: >=3 sellers sits in the
    # high tercile (11.3% bounce), a lone seller who has stopped sits in the low one (29.1%).
    if g.n_sellers >= 3 and g.still_selling:
        g.severity = "CRITICAL"
        g.note = f"{g.n_sellers} team wallets selling and not finished"
    elif g.n_sellers >= 3 or (g.still_selling and g.share_of_sells > 0.3):
        g.severity = "ELEVATED"
        g.note = f"{g.n_sellers} team wallet(s) selling, {g.share_of_sells:.0%} of all sell flow"
    else:
        g.severity = "LOW"
        g.note = f"{g.n_sellers} team wallet(s), {'still going' if g.still_selling else 'appears done'}"
    return g
