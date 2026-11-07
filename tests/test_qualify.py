from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdr_loop.llm import FakeProvider
from sdr_loop.models import Lead
from sdr_loop.steps.extract import record_to_lead
from sdr_loop.steps.qualify import load_icp, qualify_lead, rule_check

ROOT = Path(__file__).resolve().parents[1]
ICP_ = load_icp(ROOT / "icp.yaml")[0]


def lead(**kw: object) -> Lead:
    base: dict[str, object] = dict(
        linkedin_url="https://www.linkedin.com/in/x",
        full_name="X Y",
        title="Head of Growth",
        company="Bright Reach",
        industry="Marketing & Advertising",
        headcount=25,
        country="US",
        email="x@y.com",
        email_status="safe",
    )
    return Lead(**(base | kw))  # type: ignore[arg-type]


def test_icp_loads_with_stable_version() -> None:
    icp, v1 = load_icp(ROOT / "icp.yaml")
    assert "Marketing & Advertising" in icp.industries
    assert v1 == load_icp(ROOT / "icp.yaml")[1] and len(v1) == 10


@pytest.mark.parametrize(
    ("kw", "needle"),
    [
        ({"country": "IN"}, "Country"),
        ({"headcount": 30000}, "Headcount"),
        ({"headcount": 1}, "Headcount"),
        ({"title": "Marketing Intern"}, "excluded"),
        ({"title": "Software Engineer"}, "excluded"),
        ({"title": "Barista"}, "no target role"),
        ({"company": "Lemlist"}, "disqualifier"),
    ],
)
def test_rule_rejections(kw: dict[str, object], needle: str) -> None:
    q = rule_check(lead(**kw), ICP_)
    assert q is not None and q.verdict == "not_fit" and q.decided_by == "rule"
    assert needle.lower() in q.reasons[0].lower()


def test_rules_do_not_overmatch() -> None:
    assert rule_check(lead(title="Sales Manager, International"), ICP_) is None  # not 'intern'
    assert rule_check(lead(title="Technical Recruiter"), ICP_) is None  # 'recruit' prefix
    assert rule_check(lead(headcount=None), ICP_) is None  # unknown headcount goes to the LLM


async def test_t4_outside_country_rejected_without_llm_call(
    fake_llm: dict[str, FakeProvider],
) -> None:
    q, ref = await qualify_lead(lead(country="BR"), ICP_)
    assert q.verdict == "not_fit" and ref == "rules"
    assert fake_llm["together"].calls == [] and fake_llm["anthropic"].calls == []


async def test_llm_qualification_and_verdict_derived_from_score(
    fake_llm: dict[str, FakeProvider],
) -> None:
    fake_llm["together"].script = [
        json.dumps({"score": 82, "verdict": "maybe", "reasons": ["agency", "right role"]})
    ]
    q, ref = await qualify_lead(lead(), ICP_)
    assert q.verdict == "fit" and q.score == 82 and q.decided_by == "llm"  # score wins
    assert ref.startswith("qualify@")
    sent = fake_llm["together"].calls[0]["messages"][0]["content"]
    assert "x@y.com" not in sent and "linkedin.com" not in sent  # no PII sent for qualification


async def test_t6_malformed_json_retry_then_fallback_returns_valid_object(
    fake_llm: dict[str, FakeProvider],
) -> None:
    ok = json.dumps({"score": 30, "verdict": "not_fit", "reasons": ["wrong industry"]})
    fake_llm["together"].script = ["oops", '{"score": "high"}', ok]  # 2 invalid on primary
    q, _ = await qualify_lead(lead(industry="Construction"), ICP_)
    assert q.verdict == "not_fit" and q.score == 30
    assert len(fake_llm["together"].calls) == 3  # primary x2 (corrective retry) + fallback


def test_fixture_rule_archetypes_are_caught_by_rules() -> None:
    """Every code-rule archetype in the golden set is rejected without needing the LLM."""
    rows = [
        json.loads(line) for line in (ROOT / "evals/golden_leads.jsonl").read_text().splitlines()
    ]
    by = {"wrong_country", "too_big", "too_small", "bad_title", "competitor"}
    for r in rows:
        lead_ = record_to_lead(r["lead"])
        assert lead_ is not None
        q = rule_check(lead_, ICP_)
        if r["archetype"] in by:
            assert q is not None, r
        elif r["archetype"] == "fit":
            assert q is None, r  # fit leads must never be killed by a rule
