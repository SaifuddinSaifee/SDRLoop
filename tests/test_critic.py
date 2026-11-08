from __future__ import annotations

import json

import pytest

from sdr_loop.llm import FakeProvider
from sdr_loop.models import Claim, Email, Fact, Lead, Sequence
from sdr_loop.steps.critic import code_checks, faithfulness, review, write_with_critic

FACTS = [
    Fact(id="f1", source_url="https://a.example/", snippet="Atlas Growth was founded in 2022.")
]


def lead() -> Lead:
    return Lead(
        linkedin_url="https://www.linkedin.com/in/a-b",
        full_name="A B",
        title="Founder",
        company="Atlas Growth",
        country="GB",
        email="a@a.example",
        email_status="safe",
    )


def email(
    body: str = "Saw Atlas Growth started in 2022. Open to a quick call this week?",
    claims: list[Claim] | None = None,
    subject: str = "Quick idea",
) -> Email:
    if claims is None:
        claims = [Claim(text="started in 2022", fact_ids=["f1"])]
    return Email(subject=subject, body=body, claims=claims)


def seq(e1: Email | None = None, e2: Email | None = None) -> Sequence:
    return Sequence(
        email_1=e1 or email(),
        email_2=e2
        or email("One more angle: we verify every email. Worth a short reply?", claims=[]),
    )


def scores(f: int = 5, p: int = 4, c: int = 5, t: int = 5, uncited: list[str] | None = None) -> str:
    r = dict.fromkeys(["faithfulness", "personalisation", "clarity", "tone"], "ok")
    return json.dumps(
        {
            "faithfulness": f,
            "personalisation": p,
            "clarity": c,
            "tone": t,
            "reasons": r,
            "uncited_claims": uncited or [],
        }
    )


def test_clean_draft_passes_code_checks() -> None:
    assert code_checks(seq(), FACTS).passed


@pytest.mark.parametrize(
    ("e1", "needle"),
    [
        (email("word " * 95 + "Open to a call?"), "words"),
        (email("This is free for you. Open to a quick call?"), "banned"),
        (email("See https://a.example and https://b.example. Open to a call?"), "link"),
        (email("Open to a call? Can we also meet next week?"), "exactly one ask"),
        (email("Atlas Growth is great. We help teams."), "exactly one ask"),
        (email(claims=[Claim(text="x", fact_ids=["f9"])]), "does not exist"),  # E8
        (email(claims=[Claim(text="x", fact_ids=[])]), "without a fact id"),
        (email(subject="Congrats on the round"), "banned"),
    ],
)
def test_code_check_failures(e1: Email, needle: str) -> None:
    r = code_checks(seq(e1=e1), FACTS)
    assert not r.passed and any(needle in f for f in r.failures), r.failures


def test_email_2_has_a_tighter_word_limit() -> None:
    long2 = email("word " * 70 + "Worth a reply?", claims=[])
    assert not code_checks(seq(e2=long2), FACTS).passed


def test_faithfulness_rate() -> None:
    s = seq(e1=email(claims=[Claim(text="a", fact_ids=["f1"]), Claim(text="b", fact_ids=["f2"])]))
    assert faithfulness(s, FACTS) == (1, 2)
    assert faithfulness(seq(e1=email(claims=[])), FACTS) == (0, 0)


async def test_t12_code_failure_skips_the_llm_critic(fake_llm: dict[str, FakeProvider]) -> None:
    r = await review(lead(), FACTS, seq(e1=email("word " * 95 + "Open to a call?")))
    assert not r.passed and r.scores is None
    assert fake_llm["judge"].calls == []


async def test_t13_invented_fact_scores_low_and_goes_back_to_writer(
    fake_llm: dict[str, FakeProvider],
) -> None:
    invented = "Atlas Growth just raised a Series B."
    bad = seq(
        e1=email(
            f"{invented} Open to a quick call?", claims=[Claim(text=invented, fact_ids=["f1"])]
        )
    )
    good = seq()
    fake_llm["writer"].script = [bad.model_dump_json(), good.model_dump_json()]
    # critic wrongly rates faithfulness 5 but lists the uncited claim: code caps it at 2
    fake_llm["judge"].script = [scores(f=5, uncited=[invented]), scores()]
    out = await write_with_critic(lead(), FACTS)
    assert out.attempts == 2 and not out.flagged and out.report.passed
    first_review = fake_llm["judge"].calls[0]
    assert invented in first_review["messages"][0]["content"]
    rewrite = fake_llm["writer"].calls[1]["messages"][0]["content"]
    assert "uncited claim" in rewrite  # critic notes reached the writer


async def test_capped_faithfulness_blocks_pass(fake_llm: dict[str, FakeProvider]) -> None:
    fake_llm["judge"].script = [scores(f=5, uncited=["made up"])]
    r = await review(lead(), FACTS, seq())
    assert r.scores is not None and r.scores.faithfulness == 2 and not r.passed


async def test_e8_second_failure_is_flagged_not_retried_forever(
    fake_llm: dict[str, FakeProvider],
) -> None:
    ghost = seq(e1=email(claims=[Claim(text="x", fact_ids=["f9"])]))
    fake_llm["writer"].script = [ghost.model_dump_json(), ghost.model_dump_json()]
    out = await write_with_critic(lead(), FACTS)
    assert out.attempts == 2 and "critic_failed" in out.flags and out.flagged
    assert len(fake_llm["writer"].calls) == 2 and fake_llm["judge"].calls == []


async def test_first_pass_success_makes_one_write_one_critic_call(
    fake_llm: dict[str, FakeProvider],
) -> None:
    fake_llm["writer"].script = [seq().model_dump_json()]
    fake_llm["judge"].script = [scores()]
    out = await write_with_critic(lead(), FACTS)
    assert out.attempts == 1 and out.report.passed and out.flags == []


# ---------------------------------------------------------------------- found in the first real run


@pytest.mark.parametrize(
    ("body", "asks"),
    [
        ("Evaboot can also schedule a Sales Navigator search daily. Open to a call?", 1),
        ("We book meetings for agencies. Worth a 15-minute call this week?", 1),
        ("Open to a 15-minute call this week? A yes/no reply works too.", 2),
        ("Can we book a call this week?", 1),
        ("Let me know if a call makes sense.", 1),
    ],
)
def test_ask_count_ignores_product_verbs(body: str, asks: int) -> None:
    from sdr_loop.steps.critic import ask_count

    assert ask_count(body) == asks


def test_fact_ids_in_the_email_body_fail_code_checks() -> None:
    from sdr_loop.models import Claim, Email, Fact, Sequence
    from sdr_loop.steps.critic import code_checks

    facts = [Fact(id="f1", source_url="https://a.io", snippet="Acme launched in 2022.")]
    seq = Sequence(
        email_1=Email(subject="Hi", body="Acme launched in 2022 [f1]. Open to a call?",
                      claims=[Claim(text="launched in 2022", fact_ids=["f1"])]),
        email_2=Email(subject="Re: Hi", body="Since launch (f1), a lot changed. Worth a reply?",
                      claims=[]),
    )  # fmt: skip
    fails = code_checks(seq, facts).failures
    assert any("email_1: fact id marker" in f for f in fails)
    assert any("email_2: fact id marker" in f for f in fails)
