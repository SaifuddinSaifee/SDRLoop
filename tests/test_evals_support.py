from __future__ import annotations

from pathlib import Path

from evals.critic_agreement import agreement
from evals.golden import golden_fit_with_facts, golden_leads

from sdr_loop.steps.qualify import load_icp, rule_check


def row(h: tuple[int, int, int, int], c: tuple[int, int, int, int]) -> dict[str, dict[str, int]]:
    d = ("faithfulness", "personalisation", "clarity", "tone")
    return {"human": dict(zip(d, h, strict=True)), "critic": dict(zip(d, c, strict=True))}


def test_agreement_metrics() -> None:
    rows = [row((5, 4, 5, 5), (5, 4, 5, 5)), row((2, 3, 4, 4), (5, 3, 4, 4))]
    a = agreement(rows)
    assert a["exact"] == 7 / 8 and a["within_1"] == 7 / 8
    assert a["verdict_agreement"] == 0.5  # critic passed a draft the human failed
    assert a["trustworthy"] is False


def test_golden_set_shape() -> None:
    leads = golden_leads()
    assert 40 <= len(leads) <= 60
    assert 0.3 < sum(1 for _, lab in leads if lab == "fit") / len(leads) < 0.6
    assert len(golden_fit_with_facts()) == sum(1 for _, lab in leads if lab == "fit")


def test_golden_facts_are_valid_inputs_for_the_code_checks() -> None:
    icp, _ = load_icp(Path("icp.yaml"))
    for lead, facts in golden_fit_with_facts():
        assert 3 <= len(facts) <= 4 and [f.id for f in facts][0] == "f1"
        assert all(len(f.snippet) > 25 for f in facts)
        assert rule_check(lead, icp) is None
