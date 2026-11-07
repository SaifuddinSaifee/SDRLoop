"""TogetherProvider against a mocked HTTP transport: request shape, GLM quirks, response parsing."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from sdr_loop import llm
from sdr_loop.config import DEEPSEEK_FLASH, GLM, GLM_FLASH, ROUTES, Settings, get_settings
from sdr_loop.llm import (
    LLMGateway,
    RetryableError,
    TogetherProvider,
    ToolSpec,
    clean_text,
    request_params,
)

MSGS = [{"role": "user", "content": "hi"}]


def provider(handler: Any, effort: str = "low") -> tuple[TogetherProvider, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)

    client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return TogetherProvider("k-secret", 5, client=client, reasoning_effort=effort), seen


def ok(content: str = "hello", **message: Any) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content, **message}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 250},
        },
    )


def body(req: httpx.Request) -> dict[str, Any]:
    return json.loads(req.content)  # type: ignore[no-any-return]


async def test_glm_request_uses_recommended_sampling_low_reasoning_and_headroom() -> None:
    p, seen = provider(lambda r: ok())
    await p.complete(GLM, "sys", MSGS, None, 800)
    (req,) = seen
    b = body(req)
    assert str(req.url) == "https://api.together.ai/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer k-secret"
    assert b["model"] == GLM and b["temperature"] == 1.0 and b["top_p"] == 0.95
    assert b["reasoning_effort"] == "low"
    assert b["max_tokens"] == 800 + 1024  # thinking shares the budget, so add headroom
    assert b["messages"][0] == {"role": "system", "content": "sys"}
    assert "tools" not in b and "tool_choice" not in b


async def test_reasoning_effort_is_configurable_and_scales_the_headroom() -> None:
    p, seen = provider(lambda r: ok(), effort="medium")
    await p.complete(GLM_FLASH, "s", MSGS, None, 500)
    assert (
        body(seen[0])["reasoning_effort"] == "medium" and body(seen[0])["max_tokens"] == 500 + 4096
    )


async def test_non_glm_models_get_neither_reasoning_effort_nor_glm_sampling() -> None:
    p, seen = provider(lambda r: ok())
    await p.complete(DEEPSEEK_FLASH, "s", MSGS, None, 800)
    b = body(seen[0])
    assert b["temperature"] == 0.0 and "reasoning_effort" not in b and "top_p" not in b
    assert b["max_tokens"] == 800 + 2048  # generic headroom for any model that may think


def test_request_params_unit() -> None:
    assert request_params(GLM, "high") == {
        "temperature": 1.0,
        "top_p": 0.95,
        "reasoning_effort": "high",
    }
    assert request_params("zai-org/GLM-5.3-Flash", "low")["reasoning_effort"] == "low"
    assert request_params("deepseek-ai/anything", "high") == {"temperature": 0.0}


async def test_answer_is_content_only_and_reasoning_trace_is_ignored() -> None:
    resp = ok('{"x": 1}', reasoning_content='thinking... {"x": 999} ...done')
    p, _ = provider(lambda r: resp)
    r = await p.complete(GLM, "s", MSGS, None, 100)
    assert r.text == '{"x": 1}'
    assert (r.input_tokens, r.output_tokens, r.stop_reason) == (100, 250, "stop")  # usage kept


async def test_empty_content_with_reasoning_only_yields_empty_text_not_a_crash() -> None:
    p, _ = provider(lambda r: ok(None, reasoning_content="ran out of tokens thinking"))  # type: ignore[arg-type]
    assert (await p.complete(GLM, "s", MSGS, None, 100)).text == ""


def test_inline_think_blocks_are_stripped() -> None:
    assert clean_text('<think>maybe {"x": 5}</think>\n{"x": 1}') == '{"x": 1}'
    assert clean_text("<THINK>\nmulti\nline\n</THINK>answer") == "answer"
    assert clean_text(None) == "" and clean_text("plain") == "plain"


async def test_tools_are_sent_in_openai_format_and_tool_calls_are_parsed() -> None:
    args = '{"url": "https://a.example/"}'
    calls = [
        {"id": "c1", "type": "function", "function": {"name": "fetch_page", "arguments": args}}
    ]
    p, seen = provider(lambda r: ok("", tool_calls=calls))
    spec = ToolSpec(
        "fetch_page", "d", {"type": "object", "properties": {"url": {"type": "string"}}}
    )
    r = await p.complete(GLM_FLASH, "s", MSGS, [spec], 500)
    b = body(seen[0])
    assert b["tools"] == [{"type": "function", "function": {
        "name": "fetch_page", "description": "d", "parameters": spec.input_schema}}]  # fmt: skip
    assert "tool_choice" not in b
    assert [(c.id, c.name, c.input) for c in r.tool_calls] == [
        ("c1", "fetch_page", {"url": "https://a.example/"})
    ]


async def test_tool_loop_messages_are_translated_to_openai_roles() -> None:
    p, seen = provider(lambda r: ok())
    msgs = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "name": "read_lead", "input": {}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
    ]
    await p.complete(GLM_FLASH, "s", msgs, None, 100)
    sent = body(seen[0])["messages"]
    assert sent[2]["tool_calls"][0]["function"] == {"name": "read_lead", "arguments": "{}"}
    assert sent[3] == {"role": "tool", "tool_call_id": "c1", "content": "result"}


@pytest.mark.parametrize("status", [408, 429, 500, 503])
async def test_transient_http_errors_are_retryable(status: int) -> None:
    p, _ = provider(lambda r: httpx.Response(status))
    with pytest.raises(RetryableError):
        await p.complete(GLM, "s", MSGS, None, 100)


async def test_timeouts_are_retryable_and_client_errors_are_not() -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    p, _ = provider(boom)
    with pytest.raises(RetryableError):
        await p.complete(GLM, "s", MSGS, None, 100)
    p2, _ = provider(lambda r: httpx.Response(400, json={"error": "bad model"}))
    with pytest.raises(httpx.HTTPStatusError):
        await p2.complete(GLM, "s", MSGS, None, 100)


# ---------------------------------------------------------------- through the gateway


class Out(BaseModel):
    x: int


async def test_gateway_routes_write_to_glm_and_prices_it_at_glm_rates(settings: Settings) -> None:
    p, seen = provider(lambda r: ok('{"x": 7}'))
    gw = LLMGateway(
        {"together": p}, settings, backoff_base=0
    )  # real ROUTES: everything is Together
    r = await gw.complete("write", "sys", MSGS, response_model=Out)
    assert r.parsed == Out(x=7) and r.model == GLM and not r.fallback_used
    assert body(seen[0])["model"] == GLM
    assert r.cost_usd == pytest.approx((100 * 1.40 + 250 * 4.40) / 1e6)
    assert (
        "JSON schema" in body(seen[0])["messages"][0]["content"]
        or "schema" in body(seen[0])["messages"][0]["content"]
    )


async def test_gateway_falls_back_from_glm_to_flash_when_the_primary_keeps_failing(
    settings: Settings,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503) if body(req)["model"] == GLM else ok("from flash")

    p, seen = provider(handler)
    gw = LLMGateway({"together": p}, settings, backoff_base=0)
    r = await gw.complete("write", "s", MSGS)
    assert r.text == "from flash" and r.fallback_used and r.model == GLM_FLASH
    assert [body(q)["model"] for q in seen] == [
        GLM,
        GLM,
        GLM,
        GLM_FLASH,
    ]  # 3 attempts, then fallback


def test_every_route_uses_together_and_the_critic_is_not_the_writers_family() -> None:
    assert {s.provider for r in ROUTES.values() for s in (r.primary, r.fallback) if s} == {
        "together"
    }
    assert ROUTES["write"].primary.model.startswith("zai-org/GLM")
    assert not ROUTES["critic"].primary.model.startswith("zai-org/GLM")  # grader != writer family
    assert ROUTES["critic"].fallback is not None
    assert not ROUTES["critic"].fallback.model.startswith("zai-org/GLM")


def test_default_gateway_registers_only_together(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOGETHER_API_KEY", "k")
    get_settings.cache_clear()
    llm.set_gateway(None)
    try:
        assert set(llm.get_gateway().providers) == {"together"}
    finally:
        llm.set_gateway(None)
        get_settings.cache_clear()
