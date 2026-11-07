from __future__ import annotations

import pytest
from pydantic import BaseModel

from sdr_loop import budget, llm
from sdr_loop.config import ROUTES
from sdr_loop.llm import FakeProvider, RawResult, RetryableError
from sdr_loop.observability import InMemoryTracer, mask_pii


class Out(BaseModel):
    x: int


async def test_returns_parsed_model(fake_llm: dict[str, FakeProvider]) -> None:
    fake_llm["judge"].script = ['{"x": 3}']
    r = await llm.complete(
        "qualify", "sys", [{"role": "user", "content": "hi"}], response_model=Out
    )
    assert r.parsed == Out(x=3)
    assert r.model == ROUTES["qualify"].primary.model
    assert not r.fallback_used


async def test_json_in_code_fence_is_accepted(fake_llm: dict[str, FakeProvider]) -> None:
    fake_llm["judge"].script = ['Sure:\n```json\n{"x": 1}\n```']
    r = await llm.complete("qualify", "s", [{"role": "user", "content": "q"}], response_model=Out)
    assert r.parsed.x == 1


async def test_malformed_json_one_retry_with_error_then_valid(
    fake_llm: dict[str, FakeProvider],
) -> None:  # T6 part 1
    fake_llm["judge"].script = ["not json", '{"x": 2}']
    r = await llm.complete("qualify", "s", [{"role": "user", "content": "q"}], response_model=Out)
    assert r.parsed.x == 2
    assert not r.fallback_used
    retry_msgs = fake_llm["judge"].calls[1]["messages"]
    assert "invalid" in retry_msgs[-1]["content"]


async def test_malformed_twice_falls_back_to_fallback_model(
    fake_llm: dict[str, FakeProvider],
) -> None:  # T6 part 2
    fake_llm["judge"].script = ["bad", "still bad", '{"x": 9}']
    r = await llm.complete("qualify", "s", [{"role": "user", "content": "q"}], response_model=Out)
    assert r.parsed.x == 9
    assert r.fallback_used
    assert r.model == ROUTES["qualify"].fallback.model  # type: ignore[union-attr]


async def test_transient_error_is_retried(fake_llm: dict[str, FakeProvider]) -> None:
    fake_llm["judge"].script = [RetryableError("429"), RetryableError("503"), "ok"]
    r = await llm.complete("qualify", "s", [{"role": "user", "content": "q"}])
    assert r.text == "ok"
    assert not r.fallback_used


async def test_exhausted_retries_use_fallback_model_and_span_records_it(
    fake_llm: dict[str, FakeProvider], tracer: InMemoryTracer
) -> None:  # E10
    fake_llm["judge"].script = [RetryableError("x")] * 3 + ["from fallback"]
    with tracer.trace("t", seed="s"):
        r = await llm.complete("qualify", "s", [{"role": "user", "content": "q"}])
    assert r.fallback_used and r.text == "from fallback"
    gens = [s for s in tracer.traces[0].spans if s.kind == "generation"]
    assert [g.model for g in gens] == [
        ROUTES["qualify"].primary.model,
        ROUTES["qualify"].fallback.model,  # type: ignore[union-attr]
    ]
    assert any(s.name == "fallback" for s in tracer.traces[0].spans)


async def test_all_models_fail_raises(fake_llm: dict[str, FakeProvider]) -> None:
    fake_llm["judge"].script = [RetryableError("x")] * 6
    with pytest.raises(llm.LLMError):
        await llm.complete("qualify", "s", [{"role": "user", "content": "q"}])


async def test_generation_span_has_usage_and_cost(
    fake_llm: dict[str, FakeProvider], tracer: InMemoryTracer
) -> None:
    fake_llm["writer"].script = [RawResult(text="hello", input_tokens=1000, output_tokens=500)]
    with tracer.trace("t", seed="s"):
        r = await llm.complete("write", "s", [{"role": "user", "content": "q"}])
    span = tracer.traces[0].spans[0]
    assert span.fields["usage_details"] == {"input": 1000, "output": 500}
    assert span.fields["cost_details"]["total"] == pytest.approx(r.cost_usd)
    # GLM-5.3 is $1.40 in / $4.40 out per 1M tokens
    assert r.cost_usd == pytest.approx((1000 * 1.40 + 500 * 4.40) / 1e6)


async def test_lead_budget_stops_calls(fake_llm: dict[str, FakeProvider]) -> None:  # E11
    fake_llm["writer"].script = [RawResult(text="a", input_tokens=1000, output_tokens=1000)] * 3
    budget.set_lead_budget(budget.LeadBudget(max_usd=0.001, max_tokens=10**6, max_tool_calls=3))
    await llm.complete("write", "s", [{"role": "user", "content": "q"}])
    with pytest.raises(budget.BudgetExceeded):
        await llm.complete("write", "s", [{"role": "user", "content": "q"}])
    assert len(fake_llm["writer"].calls) == 1


async def test_run_budget_stops_calls(fake_llm: dict[str, FakeProvider]) -> None:  # T20 core
    fake_llm["writer"].script = [RawResult(text="a", input_tokens=40_000, output_tokens=0)] * 3
    budget.set_run_budget(budget.RunBudget(max_usd=0.10))
    await llm.complete("write", "s", [{"role": "user", "content": "q"}])  # ~$0.056 on GLM-5.3
    await llm.complete(
        "write", "s", [{"role": "user", "content": "q"}]
    )  # ~$0.112 total, over the cap
    with pytest.raises(budget.BudgetExceeded) as ei:
        await llm.complete("write", "s", [{"role": "user", "content": "q"}])
    assert ei.value.scope == "run"


def test_tool_call_budget() -> None:
    b = budget.LeadBudget(max_usd=1, max_tokens=10, max_tool_calls=3)
    assert [b.try_tool_call() for _ in range(4)] == [True, True, True, False]


def test_pii_mask() -> None:
    out = mask_pii({"a": "x jo@acme.com y", "b": ["+44 20 7946 0958"]})
    assert out == {"a": "x [EMAIL] y", "b": ["[PHONE]"]}
