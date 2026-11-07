"""THE LLM gateway. No other module may import `anthropic` or call a model provider.

Responsibilities: routing (task -> primary/fallback model), timeouts, retries with backoff,
JSON/schema validation with one corrective retry, fallback model, budget checks, cost
accounting, and Langfuse generation spans (including retry/fallback events).

Messages use a neutral format:
  {"role": "user", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "input"}]}
  {"role": "tool", "tool_call_id": str, "content": str}
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from sdr_loop import budget
from sdr_loop.config import ROUTES, ModelSpec, Settings, cost_usd, get_settings
from sdr_loop.observability import get_tracer

M = TypeVar("M", bound=BaseModel)
Message = dict[str, Any]


class LLMError(Exception):
    """All models for a task failed."""


class RetryableError(Exception):
    """Transient provider failure (429, 5xx, timeout, connection)."""


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class RawResult:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = "end_turn"


@dataclass
class LLMResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_s: float
    tool_calls: list[ToolCall] = field(default_factory=list)
    parsed: Any = None
    stop_reason: str = "end_turn"
    fallback_used: bool = False


class Provider(Protocol):
    async def complete(
        self,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        max_tokens: int,
    ) -> RawResult: ...


# --------------------------------------------------------------------------- providers


class AnthropicProvider:
    def __init__(self, api_key: str, timeout_s: float) -> None:
        import anthropic

        # SDK retries are off: the gateway owns retries/backoff/fallback.
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_s, max_retries=0)

    async def complete(
        self,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        max_tokens: int,
    ) -> RawResult:
        import anthropic

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": _to_anthropic_messages(messages),
        }
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ]
            kwargs["tool_choice"] = {"type": "auto"}  # forced tool_choice 400s on Claude 5.x
        # Claude 5.x: no temperature/top_p, no prefill, no thinking param needed.
        try:
            resp = await self._client.messages.create(**kwargs)
        except (anthropic.RateLimitError, anthropic.APIConnectionError) as e:
            raise RetryableError(str(e)) from e
        except anthropic.APIStatusError as e:
            if e.status_code >= 500 or e.status_code in (408, 409, 429):
                raise RetryableError(str(e)) from e
            raise
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [
            ToolCall(b.id, b.name, dict(b.input)) for b in resp.content if b.type == "tool_use"
        ]
        return RawResult(
            text, calls, resp.usage.input_tokens, resp.usage.output_tokens, resp.stop_reason or ""
        )


def _to_anthropic_messages(messages: list[Message]) -> list[Message]:
    out: list[Message] = []
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
        elif role == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls", []):
                blocks.append(
                    {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]}
                )
            out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": m["tool_call_id"],
                "content": m["content"],
            }
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)  # parallel results share one user message
            else:
                out.append({"role": "user", "content": [block]})
    return out


class TogetherProvider:
    URL = "https://api.together.xyz/v1/chat/completions"

    def __init__(
        self,
        api_key: str,
        timeout_s: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._key = api_key
        self._client = client or httpx.AsyncClient(timeout=timeout_s)

    async def complete(
        self,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        max_tokens: int,
    ) -> RawResult:
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "messages": [{"role": "system", "content": system}, *_to_openai_messages(messages)],
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]
        try:
            r = await self._client.post(
                self.URL, json=body, headers={"Authorization": f"Bearer {self._key}"}
            )
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise RetryableError(str(e)) from e
        if r.status_code in (408, 409, 429) or r.status_code >= 500:
            raise RetryableError(f"together {r.status_code}")
        r.raise_for_status()
        data = r.json()
        msg = data["choices"][0]["message"]
        calls = [
            ToolCall(
                tc["id"], tc["function"]["name"], json.loads(tc["function"]["arguments"] or "{}")
            )
            for tc in msg.get("tool_calls") or []
        ]
        usage = data.get("usage", {})
        return RawResult(
            msg.get("content") or "",
            calls,
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            data["choices"][0].get("finish_reason", ""),
        )


def _to_openai_messages(messages: list[Message]) -> list[Message]:
    out: list[Message] = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            out.append(
                {
                    "role": "assistant",
                    "content": m.get("content") or "",
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {"name": tc["name"], "arguments": json.dumps(tc["input"])},
                        }
                        for tc in m["tool_calls"]
                    ],
                }
            )
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


class FakeProvider:
    """Scripted provider for tests. `script` items: str | RawResult | Exception, or a callable
    `(model, system, messages, tools) -> str | RawResult | Exception` for dynamic behaviour."""

    def __init__(self, script: Any = None) -> None:
        self.script = script if script is not None else []
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        max_tokens: int,
    ) -> RawResult:
        self.calls.append({"model": model, "system": system, "messages": messages, "tools": tools})
        if callable(self.script):
            item = self.script(model, system, messages, tools)
        else:
            if not self.script:
                raise AssertionError("FakeProvider script exhausted")
            item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        result: RawResult = RawResult(text=item) if isinstance(item, str) else item
        if not result.input_tokens:
            result.input_tokens = max(1, sum(len(str(m.get("content", ""))) for m in messages) // 4)
            result.output_tokens = max(1, len(result.text) // 4)
        return result


# --------------------------------------------------------------------------- gateway

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Parse a JSON object from model text, tolerating code fences and surrounding prose."""
    t = text.strip()
    if m := _JSON_FENCE.search(t):
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end > start:
            return json.loads(t[start : end + 1])
        raise


class LLMGateway:
    def __init__(
        self,
        providers: dict[str, Provider],
        settings: Settings | None = None,
        *,
        backoff_base: float = 1.0,
        max_attempts: int = 3,
    ) -> None:
        self.providers = providers
        self.settings = settings or get_settings()
        self.backoff_base = backoff_base
        self.max_attempts = max_attempts

    async def complete(
        self,
        task: str,
        system: str,
        messages: list[Message],
        *,
        response_model: type[M] | None = None,
        tools: list[ToolSpec] | None = None,
        max_tokens: int = 2000,
        routes: dict[str, Any] | None = None,
    ) -> LLMResult:
        route = (routes or ROUTES)[task]
        specs: list[ModelSpec] = [route.primary] + ([route.fallback] if route.fallback else [])
        if response_model is not None:
            schema = json.dumps(response_model.model_json_schema())
            system = (
                f"{system}\n\nRespond with ONLY a JSON object that validates against this JSON "
                f"schema, no prose, no code fences:\n{schema}"
            )
        errors: list[str] = []
        tracer = get_tracer()
        for i, spec in enumerate(specs):
            try:
                res = await self._run_model(
                    task, spec, system, list(messages), tools, max_tokens, response_model
                )
                res.fallback_used = i > 0
                return res
            except budget.BudgetExceeded:
                raise
            except (RetryableError, _OutputInvalid, httpx.HTTPStatusError) as e:
                errors.append(f"{spec.model}: {e}")
                with tracer.span("fallback", metadata={"task": task, "failed": spec.model}) as sp:
                    sp.event("model_failed", model=spec.model, error=str(e)[:300])
        raise LLMError(f"all models failed for task {task}: {'; '.join(errors)}")

    async def _run_model(
        self,
        task: str,
        spec: ModelSpec,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        max_tokens: int,
        response_model: type[M] | None,
    ) -> LLMResult:
        provider = self.providers[spec.provider]
        attempts = 2 if response_model is not None else 1  # one corrective retry (E9)
        last_err = ""
        for attempt in range(attempts):
            if attempt:
                messages = [
                    *messages,
                    {
                        "role": "user",
                        "content": f"Your previous reply was invalid: {last_err}\n"
                        "Reply again with ONLY the corrected JSON object.",
                    },
                ]
            raw, latency = await self._call(
                task, spec, provider, system, messages, tools, max_tokens
            )
            res = LLMResult(
                text=raw.text,
                model=spec.model,
                input_tokens=raw.input_tokens,
                output_tokens=raw.output_tokens,
                cost_usd=cost_usd(spec.model, raw.input_tokens, raw.output_tokens),
                latency_s=latency,
                tool_calls=raw.tool_calls,
                stop_reason=raw.stop_reason,
            )
            if response_model is None:
                return res
            try:
                res.parsed = response_model.model_validate(extract_json(raw.text))
                return res
            except (ValidationError, json.JSONDecodeError) as e:
                last_err = str(e)[:500]
                messages = [*messages, {"role": "assistant", "content": raw.text}]
        raise _OutputInvalid(last_err)

    async def _call(
        self,
        task: str,
        spec: ModelSpec,
        provider: Provider,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        max_tokens: int,
    ) -> tuple[RawResult, float]:
        tracer = get_tracer()
        with tracer.span(
            f"llm.{task}", kind="generation", input=messages[-1:], model=spec.model
        ) as sp:
            retries = AsyncRetrying(
                stop=stop_after_attempt(self.max_attempts),
                wait=wait_exponential(multiplier=self.backoff_base, max=20),
                retry=retry_if_exception_type(RetryableError),
                reraise=True,
            )
            n = 0
            async for attempt in retries:
                with attempt:
                    budget.check_budgets()  # E11: stop before spending more
                    n += 1
                    if n > 1:
                        sp.event("retry", attempt=n)
                    t0 = time.perf_counter()
                    raw = await provider.complete(spec.model, system, messages, tools, max_tokens)
                    latency = time.perf_counter() - t0
            usd = cost_usd(spec.model, raw.input_tokens, raw.output_tokens)
            budget.charge(raw.input_tokens + raw.output_tokens, usd)
            sp.update(
                output=raw.text[:2000],
                usage_details={"input": raw.input_tokens, "output": raw.output_tokens},
                cost_details={"total": usd},
                metadata={"latency_s": round(latency, 3), "attempts": n},
            )
            return raw, latency


class _OutputInvalid(Exception):
    """Model output failed schema validation after the corrective retry."""


# --------------------------------------------------------------------------- global accessor

_gateway: LLMGateway | None = None


def set_gateway(g: LLMGateway | None) -> None:
    global _gateway
    _gateway = g


def get_gateway() -> LLMGateway:
    global _gateway
    if _gateway is None:
        s = get_settings()
        providers: dict[str, Provider] = {}
        if s.anthropic_api_key:
            providers["anthropic"] = AnthropicProvider(s.anthropic_api_key, s.llm_timeout_s)
        if s.together_api_key:
            providers["together"] = TogetherProvider(s.together_api_key, s.llm_timeout_s)
        _gateway = LLMGateway(providers, s)
    return _gateway


async def complete(task: str, system: str, messages: list[Message], **kw: Any) -> LLMResult:
    return await get_gateway().complete(task, system, messages, **kw)


__all__ = [
    "FakeProvider",
    "LLMError",
    "LLMGateway",
    "LLMResult",
    "RawResult",
    "RetryableError",
    "ToolCall",
    "ToolSpec",
    "complete",
    "extract_json",
    "get_gateway",
    "set_gateway",
]
