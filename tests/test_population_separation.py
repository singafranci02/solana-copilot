"""Mayhem is collected as DATA and must never become a recommendation.

Owner decision 2026-08-17: Mayhem is pump.fun's own enhanced mode and carries ~89%
of graduations, so excluding it cost ~10x the training data. It is now analysed and
stored — but alerts stay classic-only, and the two populations stay separable until
someone deliberately combines them.

Before this, safety came from never ingesting Mayhem at all. That guarantee is gone,
so these tests carry it instead.
"""

from eval._common import BOTH, CLASSIC, MAYHEM


def test_classic_is_the_default_population():
    """Every existing caller must keep the population it was written against —
    combining is an explicit act, never a silent change of meaning."""
    import inspect

    from eval._common import load_samples
    assert inspect.signature(load_samples).parameters["platforms"].default == CLASSIC


def test_populations_are_disjoint():
    assert not set(CLASSIC) & set(MAYHEM)


def test_both_is_exactly_the_union():
    assert set(BOTH) == set(CLASSIC) | set(MAYHEM)


def test_no_population_admits_a_foreign_launchpad():
    """rapidlaunch/bonk.fun are a different product and were never in scope."""
    for pop in (CLASSIC, MAYHEM, BOTH):
        assert all(p in ("pump.fun", "mayhem") for p in pop)


def test_alert_paths_gate_on_classic_only():
    """Both coin-level alerts must compare platform against 'pump.fun' exactly.
    A gate written as 'not foreign' would now let Mayhem through."""
    from pathlib import Path
    root = Path(__file__).parent.parent
    for rel, fn in (("src/ingest/graduation_monitor.py", "pre-warn"),
                    ("src/analyzer/distribution.py", "exit alarm")):
        src = (root / rel).read_text()
        assert 'platform"] != "pump.fun"' in src, f"{fn} lost its classic-only gate"


def test_mayhem_is_not_purged_by_the_reresolver():
    """The re-resolver used to delete anything non-classic; that would erase the
    population we now deliberately collect."""
    from pathlib import Path
    src = (Path(__file__).parent.parent / "scripts/reresolve_platforms.py").read_text()
    assert 'p != "mayhem"' in src, "re-resolver would purge collected Mayhem data"
