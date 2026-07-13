"""The audit's baseline bands encode hard-won lessons — pin them against edits."""

from eval.audit import BASE_RATE_BANDS, ROC_BANDS, PREWARN_PRECISION_MIN


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
