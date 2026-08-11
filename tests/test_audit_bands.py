"""The audit's baseline bands encode hard-won lessons — pin them against edits."""

from eval.audit import (
    BASE_RATE_BANDS, ROC_BANDS, PREWARN_PRECISION_MIN, PREWARN_MIN_LIFT,
    RETIRED_HEADS,
)


def test_moon_head_must_stay_capped():
    """moon10x is measured-unpredictable (0.583). A future edit raising its ceiling
    would re-open the door to the price_run leak class. See NEGATIVE_RESULTS #1."""
    lo, hi = ROC_BANDS["moon10x"]
    assert lo is None          # no minimum — the head SHOULD be near chance
    assert hi <= 0.70          # "suddenly works" must FAIL as suspicious


def test_base_rate_bands_catch_the_known_corruptions():
    """The two silent-corruption incidents must sit OUTSIDE their bands."""
    assert not (BASE_RATE_BANDS["moon10x"][0] <= 0.26 <= BASE_RATE_BANDS["moon10x"][1])
    assert not (BASE_RATE_BANDS["survive60"][0] <= 0.53 <= BASE_RATE_BANDS["survive60"][1])


def test_prewarn_precision_floor_is_high():
    """A warning wrong more than ~1-in-7 trains users to ignore it."""
    assert PREWARN_PRECISION_MIN >= 0.85


def test_prewarn_is_judged_on_lift_not_absolute_precision():
    """At a 96.5% base rate, 94% 'precision' is a LOSS against assuming every team
    exits. The alert must beat not bothering, so the gate is lift over the base
    rate — an absolute floor certifies nothing here."""
    assert PREWARN_MIN_LIFT >= 0.0


def test_retired_heads_never_block_a_deployment():
    """A retired head has a saturated base rate, so a low ROC reports the market
    rather than a broken model. Leaving it a floor froze all three artifacts on
    stale labels for six days — the retrain gates on a clean audit."""
    for head in RETIRED_HEADS:
        assert head in ROC_BANDS, head
        assert ROC_BANDS[head][0] is None, f"{head} is retired but still has a floor"


def test_retired_heads_keep_their_leak_tripwire():
    """The ceiling is the half that matters after retirement: a head we concluded
    is uninformative suddenly scoring well is evidence of a leak, not a discovery."""
    for head in RETIRED_HEADS:
        hi = ROC_BANDS[head][1]
        assert hi is not None and hi <= 0.96, f"{head} lost its ceiling"


def test_at_least_one_head_still_blocks():
    """Retiring heads must not quietly disarm the audit entirely."""
    blocking = [h for h, (lo, _) in ROC_BANDS.items() if lo is not None]
    assert blocking, "no head can block a deployment — the gate is disarmed"
    assert "distribute" in blocking      # the thesis head: team structure -> outcome
