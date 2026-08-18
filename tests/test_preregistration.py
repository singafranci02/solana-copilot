"""Pins the pre-registered pooling rule.

These constants were frozen on 2026-08-18, before Mayhem data existed in useful
quantity. Their whole purpose is to be inconvenient later: pooling is worth ~10x
the training data, so when the comparison comes back ambiguous there will be a
reason to loosen something. If one of these tests fails, that loosening happened —
which is the point. It should require an explicit, reviewable edit, not a quiet one.
"""

from eval import preregistration as pre


def test_constants_are_unchanged_since_registration():
    assert pre.REGISTERED_ON == "2026-08-18"
    assert pre.CHECKPOINT_S == 30
    assert pre.ALARM_QUANTILE == 0.80
    assert pre.EQUIVALENCE_MARGIN == 0.10
    assert pre.MIN_INDIVIDUAL_LIFT == 0.10
    assert pre.N_REQUIRED_PER_ARM == 1600


def test_the_test_is_equivalence_not_difference():
    """Failing to find a difference must not license pooling. The rule requires
    the whole CI to sit inside the margin — positive evidence of similarity."""
    import inspect
    src = inspect.getsource(pre.evaluate)
    assert "abs(lo) <= EQUIVALENCE_MARGIN" in src
    assert "abs(hi) <= EQUIVALENCE_MARGIN" in src


def test_it_refuses_a_verdict_below_the_registered_n():
    class FakeConn:
        def execute(self, *_a):
            class R:
                def fetchall(self): return []
            return R()
    r = pre.evaluate(FakeConn())
    assert r["verdict"] == "INSUFFICIENT_DATA"
    assert "lift_classic" not in r, "an underpowered run must not leak a directional hint"


def test_both_arms_must_individually_work():
    """Two equally useless models are 'equivalent'. Pooling also requires each arm
    to clear its own base rate."""
    import inspect
    assert "MIN_INDIVIDUAL_LIFT" in inspect.getsource(pre.evaluate)


def test_required_n_is_consistent_with_the_registered_margin():
    """Sized from the bootstrap table in the module docstring: half-width scales as
    1/sqrt(n), and 11.6pp at n=1200 needs ~1600 to clear a 10pp margin."""
    assert pre.N_REQUIRED_PER_ARM >= 1200
