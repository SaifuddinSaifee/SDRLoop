from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from sdr_loop import prompts
from sdr_loop.config import Settings
from sdr_loop.db.schema import EvalCase, PromptPromotion
from sdr_loop.learn.build_eval_set import build_eval_cases
from sdr_loop.learn.challenger import digest, sanity_check
from sdr_loop.learn.cycle import run_learning_cycle
from sdr_loop.learn.promote import (
    CaseScore,
    PromptScore,
    decide,
    items_from_cases,
    score_item,
)  # fmt: skip
from sdr_loop.llm import FakeProvider
from sdr_loop.memory import Repository
from sdr_loop.models import Claim, Email, Fact, Lead, Sequence
from sdr_loop.steps import approve as ap

ROOT = Path(__file__).resolve().parents[1]
FACT = Fact(id="f1", source_url="https://a.example/", snippet="Acme builds outbound tooling.")
MARK = "CHALLENGER_PROMPT"


@pytest.fixture(autouse=True)
def prompt_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "prompts"
    shutil.copytree(ROOT / "prompts", d)
    monkeypatch.setattr(prompts, "PROMPT_DIR", d)
    return d


def seq(tight: bool, fact_ids: list[str] | None = None, words: int = 0) -> Sequence:
    pad = " ".join(["filler"] * words)
    body = ("TIGHT Hi, noticed Acme. Open to a call?" if tight else
            "Hi, I noticed what Acme builds and wanted to reach out about our lead lists. "
            "Open to a quick call this week?") + (" " + pad if pad else "")  # fmt: skip
    claims = [Claim(text="Acme builds tooling", fact_ids=fact_ids or ["f1"])]
    return Sequence(
        email_1=Email(subject="Idea", body=body, claims=claims),
        email_2=Email(subject="Re: Idea", body="One more angle. Worth a reply?", claims=[]),
    )


class World:
    """Scripted models. Writer output depends on whether the challenger marker is in its prompt."""

    def __init__(self, fakes: dict[str, FakeProvider]) -> None:
        self.champion = lambda lead: seq(False)
        self.challenger = lambda lead: seq(True)
        self.proposal = {
            "prompt": f"{MARK} Cite fact ids in claims. Be TIGHT.",
            "rationale": "shorter",
        }
        fakes["writer"].script = self.writer
        fakes["judge"].script = self.judge
        self.fakes = fakes

    def writer(self, model: str, system: str, messages: Any, tools: Any) -> str:
        if system.startswith("You improve a cold-email WRITER"):
            return json.dumps(self.proposal)
        who = self.challenger if MARK in system else self.champion
        company = json.loads(messages[0]["content"])["lead"]["company"]
        return who(company).model_dump_json()

    def judge(self, model: str, system: str, messages: Any, tools: Any) -> str:
        tight = "TIGHT" in messages[0]["content"]
        v = 5 if tight else 3
        r = dict.fromkeys(["faithfulness", "personalisation", "clarity", "tone"], "x")
        return json.dumps({"faithfulness": 5, "personalisation": v, "clarity": v, "tone": v,
                           "reasons": r, "uncited_claims": []})  # fmt: skip

    @property
    def writer_calls(self) -> int:
        return len(self.fakes["writer"].calls)


async def seed_decisions(
    repo: Repository, edits: int, rejects: int = 0, uncited: bool = False
) -> None:
    run = await repo.create_run("u", "v1")
    for i in range(edits + rejects):
        lead = Lead(linkedin_url=f"https://www.linkedin.com/in/p{i}", full_name=f"P {i}",
                    title="Founder", company=f"Acme {i}", industry="Marketing & Advertising",
                    country="US", email=f"p{i}@acme.example", email_status="safe")  # fmt: skip
        lid, _ = await repo.upsert_lead(lead, run)
        await repo.save_facts(lid, [FACT])
        did = await repo.save_draft(lid, seq(False), prompt_version="writer@local-1")
        if i < edits:
            text = sequence_text_tight()
            if uncited:
                text = text.replace("noticed Acme", "noticed Acme raised $40M with Sequoia")
            await ap.edit(repo, did, text, [FACT])
        else:
            await ap.reject(repo, did, "too_long")


def sequence_text_tight() -> str:
    return ap.sequence_text(seq(True))


def golden() -> list[tuple[Lead, list[Fact]]]:
    return [
        (Lead(linkedin_url=f"u{i}", full_name="G", title="Founder", company=f"Gold {i}"), [FACT])
        for i in range(3)
    ]


@pytest.fixture
def world(fake_llm: dict[str, FakeProvider]) -> World:
    return World(fake_llm)


def settings_with(settings: Settings, **kw: Any) -> Settings:
    return settings.model_copy(update=kw)


# ------------------------------------------------------------------ T17: signals -> cases


async def test_t17_ten_edits_become_ten_eval_cases_with_input_and_expected(
    repo: Repository,
) -> None:
    await seed_decisions(repo, edits=10)
    assert await build_eval_cases(repo) == 10
    assert await build_eval_cases(repo) == 0  # idempotent
    async with repo.session() as s:
        cases = list((await s.execute(select(EvalCase))).scalars())
    assert len(cases) == 10 and all(c.source == "edit" for c in cases)
    c = cases[0]
    assert c.input["facts"][0]["id"] == "f1" and c.input["lead"]["company"].startswith("Acme")
    assert "email" not in json.dumps(c.input["lead"]).lower().replace("email_status", "")
    assert c.expected is not None and "TIGHT" in c.expected["final_text"]


async def test_rejections_become_negative_cases_and_approvals_teach_nothing(
    repo: Repository,
) -> None:
    await seed_decisions(repo, edits=1, rejects=2)
    run = await repo.create_run("u", "v1")
    lead = Lead(linkedin_url="https://www.linkedin.com/in/ok", full_name="Ok", company="Fine Co")
    lid, _ = await repo.upsert_lead(lead, run)
    did = await repo.save_draft(lid, seq(False), prompt_version="w@1")
    await ap.approve(repo, did)
    assert await build_eval_cases(repo) == 3
    async with repo.session() as s:
        rej = [c for c in (await s.execute(select(EvalCase))).scalars() if c.source == "reject"]
    assert len(rej) == 2 and rej[0].expected is None and rej[0].input["reason"] == "too_long"


async def test_e14_edit_with_ungrounded_claims_is_not_used_as_expected_text(
    repo: Repository,
) -> None:
    await seed_decisions(repo, edits=1, uncited=True)
    await build_eval_cases(repo)
    async with repo.session() as s:
        case = (await s.execute(select(EvalCase))).scalars().one()
    assert case.expected is not None and case.expected["ungrounded"] is True
    assert items_from_cases([case])[0].expected_text is None


# ------------------------------------------------------------------ cycle


async def test_t18_challenger_beats_champion_is_promoted_and_recorded(
    repo: Repository, settings: Settings, world: World
) -> None:
    await seed_decisions(repo, edits=10)
    res = await run_learning_cycle(repo, settings_with(settings), golden())
    assert res.outcome == "promoted" and res.promoted, res
    async with repo.session() as s:
        row = (await s.execute(select(PromptPromotion))).scalars().one()
        used = [c.used_in_promotion for c in (await s.execute(select(EvalCase))).scalars()]
    assert row.promoted and row.challenger_score > row.champion_score
    assert (row.from_version, row.to_version) == ("3", "4") and row.prompt_name == "writer"
    assert row.detail["rationale"] == "shorter" and row.detail["challenger_invalid_citations"] == 0
    assert all(used)
    assert prompts.get_prompt("writer").version == "local-4"  # the label moved: new champion
    assert MARK in prompts.get_prompt("writer").text


async def test_t19_e15_better_average_but_one_invalid_fact_id_is_not_promoted(
    repo: Repository, settings: Settings, world: World
) -> None:
    await seed_decisions(repo, edits=10)
    world.challenger = lambda company: seq(True, ["f9"]) if company == "Acme 3" else seq(True)
    res = await run_learning_cycle(repo, settings_with(settings), golden())
    assert res.outcome == "archived" and not res.promoted
    assert any("hard gate" in r for r in res.detail["reasons"])
    assert res.detail["challenger_avg"] > res.detail["champion_avg"]  # it WOULD have won on score
    async with repo.session() as s:
        row = (await s.execute(select(PromptPromotion))).scalars().one()
    assert row.promoted is False and row.detail["challenger_invalid_citations"] == 1
    assert prompts.get_prompt("writer").version == "local-3"  # champion untouched
    assert prompts.get_challenger("writer") is not None  # archived candidate is still inspectable


async def test_e16_too_few_new_cases_skips_the_night(
    repo: Repository, settings: Settings, world: World
) -> None:
    await seed_decisions(repo, edits=9)
    res = await run_learning_cycle(repo, settings_with(settings), golden())
    assert res.outcome == "skipped" and "9 new eval cases" in res.reason
    assert world.writer_calls == 0
    async with repo.session() as s:
        assert (await s.execute(select(PromptPromotion))).first() is None


@pytest.mark.parametrize(
    ("kw", "needle"),
    [({"learn_enabled": False}, "LEARN_ENABLED"), ({"pinned_prompts": "writer"}, "pinned")],
)
async def test_kill_switch_and_pin_stop_the_loop_before_any_model_call(
    repo: Repository, settings: Settings, world: World, kw: dict[str, Any], needle: str
) -> None:
    await seed_decisions(repo, edits=12)
    res = await run_learning_cycle(repo, settings_with(settings, **kw), golden())
    assert res.outcome == "skipped" and needle in res.reason and world.writer_calls == 0


async def test_challenger_that_drops_grounding_rules_is_rejected_before_scoring(
    repo: Repository, settings: Settings, world: World
) -> None:
    await seed_decisions(repo, edits=10)
    world.proposal = {"prompt": "Write nice emails.", "rationale": "simpler"}
    res = await run_learning_cycle(repo, settings_with(settings), golden())
    assert res.outcome == "rejected_challenger" and "grounding" in res.reason
    assert prompts.get_prompt("writer").version == "local-3"
    assert world.writer_calls == 1  # only the proposal call; no scoring spend


async def test_losing_challenger_is_archived_with_scores(
    repo: Repository, settings: Settings, world: World
) -> None:
    await seed_decisions(repo, edits=10)
    world.challenger = lambda company: seq(False)  # no better than champion
    res = await run_learning_cycle(repo, settings_with(settings), golden())
    assert res.outcome == "archived" and "does not beat" in res.reason
    async with repo.session() as s:
        assert (await s.execute(select(PromptPromotion))).scalars().one().promoted is False


# ------------------------------------------------------------------ unit: guardrails


def ps(*scores: tuple[str, str, float, int, int]) -> PromptScore:
    return PromptScore([CaseScore(k, s, v, ok, tot) for k, s, v, ok, tot in scores])


def test_decide_requires_strict_win() -> None:
    a = ps(("c1", "cases", 0.6, 1, 1), ("g1", "golden", 0.6, 1, 1))
    assert decide(a, a).promoted is False
    b = ps(("c1", "cases", 0.7, 1, 1), ("g1", "golden", 0.7, 1, 1))
    assert decide(a, b).promoted is True


def test_decide_blocks_faithfulness_drop_and_golden_loss() -> None:
    champ = ps(("c1", "cases", 0.5, 2, 2), ("g1", "golden", 0.9, 2, 2))
    lower_faith = ps(("c1", "cases", 0.9, 1, 2), ("g1", "golden", 0.9, 2, 2))
    d = decide(champ, lower_faith)
    assert not d.promoted and any("faithfulness" in r for r in d.reasons)
    golden_loss = ps(("c1", "cases", 1.0, 2, 2), ("g1", "golden", 0.8, 2, 2))
    d = decide(champ, golden_loss)  # avg wins (0.9 vs 0.7) but the fixed golden set got worse
    assert not d.promoted and any("golden" in r for r in d.reasons)


def test_decide_blocks_more_than_five_percent_regressions() -> None:
    n = 20
    champ = ps(*[(f"c{i}", "cases", 0.5, 1, 1) for i in range(n)])
    # big wins on 18 cases, clear losses on 2 (10% > 5%)
    chall = ps(*[(f"c{i}", "cases", 0.2 if i < 2 else 0.9, 1, 1) for i in range(n)])
    d = decide(champ, chall)
    assert not d.promoted and d.detail["regression_rate"] == pytest.approx(0.1)
    one_loss = ps(*[(f"c{i}", "cases", 0.2 if i < 1 else 0.9, 1, 1) for i in range(n)])
    assert decide(champ, one_loss).promoted is True  # exactly 5% is allowed


def test_sanity_check_rules() -> None:
    champ = "Cite fact ids in claims. Write emails."
    assert sanity_check(champ, champ + " Be brief.") is None
    assert sanity_check(champ, "") == "empty prompt"
    assert "too long" in (sanity_check(champ, champ * 3) or "")
    assert "grounding" in (sanity_check(champ, "Be brief and kind.") or "")


async def test_negative_case_scores_zero_when_the_rejected_behaviour_repeats(
    fake_llm: dict[str, FakeProvider], repo: Repository
) -> None:
    from sdr_loop.learn.promote import Item

    lead = Lead(linkedin_url="u", full_name="A", title="Founder", company="Acme")
    good_critic = json.dumps(
        {"faithfulness": 5, "personalisation": 5, "clarity": 5, "tone": 5,
         "reasons": {}, "uncited_claims": []}
    )  # fmt: skip
    p = prompts.get_prompt("writer")
    cases = (
        (55, "passes code checks but is over the 75-word threshold"),
        (70, "also fails the 90-word code check, so the critic never runs"),
    )
    for words, why in cases:
        fake_llm["writer"].script = [seq(False, words=words).model_dump_json()] * 2
        fake_llm["judge"].script = [good_critic] * 2
        repeated = await score_item(p, Item("k", "cases", lead, [FACT], reject_reason="too_long"))
        assert repeated.repeated_rejection and repeated.score == 0.0, why
    fake_llm["writer"].script = [seq(False, words=0).model_dump_json()]
    fake_llm["judge"].script = [good_critic]
    fine = await score_item(p, Item("k2", "cases", lead, [FACT], reject_reason="too_long"))
    assert not fine.repeated_rejection and fine.score > 0.9


def test_digest_summarises_what_the_human_keeps_fixing() -> None:
    def case(src: str, reason: str | None) -> EvalCase:
        return EvalCase(
            source=src,
            input={"reason": reason, "original": {"email_1": {"body": "long long"}}},
            expected={"final_text": "short"} if src == "edit" else None,
        )

    d = digest([case("reject", "too_long"), case("reject", "too_long"), case("edit", None)])
    assert d["reject_reasons"] == {"too_long": 2} and d["n_edits"] == 1
    assert d["edit_examples"] == [{"before": "long long", "after": "short"}]  # type: ignore[comparison-overlap]
