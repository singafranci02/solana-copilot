"""Detection-source parity — the guard on a biased recovery population.

Recovery only succeeds when the tape can be walked back to the anchor, which
favours quieter coins. That is a selection effect with a known direction, and both
populations feed training indistinguishably once stored.
"""

from eval import source_parity as sp


def test_comparison_is_age_matched():
    """Poll-recovered coins are all recent. Without a maturity floor their youth
    reads as survival: the first run showed 94.2% vs 40.0% collapse, mostly age."""
    assert sp.MATURITY_S >= 3600
    assert ":maturity" in sp._POP


def test_team_exit_is_reported_but_never_a_finding():
    """A recovered coin has no team cluster, so its exit label is structurally
    NULL. Flagging that as divergence would be flagging a definition."""
    flags = {name: is_finding for name, _, is_finding in sp.METRICS}
    assert flags["team-exit observed"] is False
    assert flags["collapse rate"] is True


def test_suspended_is_not_agreement():
    rows = [{"metric": "collapse rate", "verdict": "SUSPENDED"}]
    assert not sp.has_material_divergence(rows)


def test_only_material_established_differences_are_findings():
    assert sp.has_material_divergence([{"verdict": "DIVERGENT"}])
    assert not sp.has_material_divergence([{"verdict": "established but small"}])
    assert not sp.has_material_divergence([{"verdict": "no difference detected"}])


def test_material_threshold_is_meaningful():
    assert 0.05 <= sp.MATERIAL_DIFF <= 0.50
