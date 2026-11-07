from __future__ import annotations

import json

from sdr_loop.llm import FakeProvider
from sdr_loop.models import Fact, Lead
from sdr_loop.steps.write import flags_for, write_sequence


def lead(country: str = "GB") -> Lead:
    return Lead(
        linkedin_url="https://www.linkedin.com/in/a-b",
        full_name="A B",
        title="Founder",
        company="Atlas Growth",
        industry="Marketing & Advertising",
        country=country,
        email="a@atlas.example",
        email_status="safe",
    )


FACTS = [
    Fact(id="f1", source_url="https://atlas.example/", snippet="Atlas Growth was founded in 2022.")
]


def seq_json(claims: list[dict[str, object]]) -> str:
    e = {
        "subject": "Quick idea",
        "body": "Hi A, short body. Open to a quick call?",
        "claims": claims,
    }
    return json.dumps({"email_1": e, "email_2": {**e, "claims": []}})


async def test_t10_draft_claims_cite_fact_ids_and_input_has_facts_but_no_email(
    fake_llm: dict[str, FakeProvider],
) -> None:
    fake_llm["writer"].script = [
        seq_json([{"text": "Atlas Growth was founded in 2022", "fact_ids": ["f1"]}])
    ]
    seq, ref = await write_sequence(lead(), FACTS)
    assert seq.email_1.claims[0].fact_ids == ["f1"] and ref.startswith("writer@")
    sent = fake_llm["writer"].calls[0]["messages"][0]["content"]
    assert '"id": "f1"' in sent and "a@atlas.example" not in sent


async def test_t11_zero_facts_means_no_personal_claims_and_flag(
    fake_llm: dict[str, FakeProvider],
) -> None:
    fake_llm["writer"].script = [seq_json([])]
    seq, _ = await write_sequence(lead(), [])
    assert seq.email_1.claims == [] and seq.email_2.claims == []
    assert '"facts": []' in fake_llm["writer"].calls[0]["messages"][0]["content"]
    assert flags_for(lead(), []) == ["no_facts"]


def test_non_english_market_is_flagged() -> None:
    assert flags_for(lead("FR"), FACTS) == ["non_english_market"]
    assert flags_for(lead("US"), FACTS) == []


async def test_revision_notes_reach_the_writer(fake_llm: dict[str, FakeProvider]) -> None:
    fake_llm["writer"].script = [seq_json([])]
    await write_sequence(lead(), FACTS, revision_notes="too long")
    assert "too long" in fake_llm["writer"].calls[0]["messages"][0]["content"]
