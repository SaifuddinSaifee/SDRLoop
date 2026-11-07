"""Tracing abstraction: Langfuse in production, an in-memory recorder in tests.

One trace per lead, one span per step, generations/tools as child spans, scores on the trace.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

SpanKind = Literal["span", "generation", "tool"]

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d")


def mask_pii(data: Any) -> Any:
    """Redact emails and phone numbers anywhere in a nested structure."""
    if isinstance(data, str):
        return _PHONE.sub("[PHONE]", _EMAIL.sub("[EMAIL]", data))
    if isinstance(data, dict):
        return {k: mask_pii(v) for k, v in data.items()}
    if isinstance(data, list | tuple):
        return [mask_pii(v) for v in data]
    return data


class SpanHandle(Protocol):
    def update(self, **fields: Any) -> None: ...
    def event(self, name: str, **data: Any) -> None: ...


class TraceHandle(Protocol):
    trace_id: str

    def score(self, name: str, value: float | str, comment: str | None = None) -> None: ...


class Tracer(Protocol):
    def trace(
        self,
        name: str,
        *,
        seed: str,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AbstractContextManager[TraceHandle]: ...

    def span(
        self,
        name: str,
        *,
        kind: SpanKind = "span",
        input: Any = None,
        metadata: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> AbstractContextManager[SpanHandle]: ...

    def flush(self) -> None: ...

    def delete_trace(self, trace_id: str) -> None: ...


def lead_trace_seed(lead_id: int) -> str:
    """One trace per lead. The pipeline and the later approve/outbox steps share this seed so
    they append to the same trace."""
    return f"lead:{lead_id}"


# ---------------------------------------------------------------- in-memory (tests)


@dataclass
class RecordedSpan:
    name: str
    kind: SpanKind
    parent: RecordedSpan | None
    input: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    model: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def update(self, **fields: Any) -> None:
        self.fields.update(mask_pii(fields))  # same masking the Langfuse client applies

    def event(self, name: str, **data: Any) -> None:
        self.events.append((name, mask_pii(data)))


@dataclass
class RecordedTrace:
    name: str
    trace_id: str
    tags: list[str]
    metadata: dict[str, Any]
    spans: list[RecordedSpan] = field(default_factory=list)
    scores: dict[str, float | str] = field(default_factory=dict)

    def score(self, name: str, value: float | str, comment: str | None = None) -> None:
        self.scores[name] = value

    def step_spans(self) -> list[RecordedSpan]:
        return [s for s in self.spans if s.kind == "span" and s.parent is None]


class InMemoryTracer:
    def __init__(self) -> None:
        self.traces: list[RecordedTrace] = []
        self._trace: ContextVar[RecordedTrace | None] = ContextVar("mem_trace", default=None)
        self._span: ContextVar[RecordedSpan | None] = ContextVar("mem_span", default=None)

    @contextmanager
    def trace(
        self,
        name: str,
        *,
        seed: str,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[RecordedTrace]:
        trace_id = uuid.uuid5(uuid.NAMESPACE_URL, seed).hex
        t = next((x for x in self.traces if x.trace_id == trace_id), None)
        if t is None:  # same seed re-enters the same trace (approve/outbox come hours later)
            t = RecordedTrace(name, trace_id, tags or [], metadata or {})
            self.traces.append(t)
        tok = self._trace.set(t)
        try:
            yield t
        finally:
            self._trace.reset(tok)

    @contextmanager
    def span(
        self,
        name: str,
        *,
        kind: SpanKind = "span",
        input: Any = None,
        metadata: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> Iterator[RecordedSpan]:
        parent = self._span.get()
        s = RecordedSpan(name, kind, parent, mask_pii(input), mask_pii(metadata or {}), model)
        if (t := self._trace.get()) is not None:
            t.spans.append(s)
        tok = self._span.set(s)
        try:
            yield s
        except BaseException as e:
            s.event("error", type=type(e).__name__, message=str(e))
            raise
        finally:
            self._span.reset(tok)

    def flush(self) -> None:
        pass

    def delete_trace(self, trace_id: str) -> None:
        self.traces = [t for t in self.traces if t.trace_id != trace_id]


# ---------------------------------------------------------------- no-op


class _NullHandle:
    trace_id = ""

    def update(self, **fields: Any) -> None:
        pass

    def event(self, name: str, **data: Any) -> None:
        pass

    def score(self, name: str, value: float | str, comment: str | None = None) -> None:
        pass


class NullTracer:
    def trace(self, name: str, **_: Any) -> AbstractContextManager[_NullHandle]:
        return nullcontext(_NullHandle())

    def span(self, name: str, **_: Any) -> AbstractContextManager[_NullHandle]:
        return nullcontext(_NullHandle())

    def flush(self) -> None:
        pass

    def delete_trace(self, trace_id: str) -> None:
        pass


# ---------------------------------------------------------------- Langfuse


class _LfSpan:
    def __init__(self, obs: Any) -> None:
        self._obs = obs

    def update(self, **fields: Any) -> None:
        self._obs.update(**fields)

    def event(self, name: str, **data: Any) -> None:
        self._obs.create_event(name=name, metadata=data)


class _LfTrace:
    def __init__(self, client: Any, trace_id: str) -> None:
        self._client = client
        self.trace_id = trace_id

    def score(self, name: str, value: float | str, comment: str | None = None) -> None:
        self._client.create_score(
            trace_id=self.trace_id,
            name=name,
            value=value,
            comment=comment,
            data_type="NUMERIC" if isinstance(value, int | float) else "CATEGORICAL",
        )


class LangfuseTracer:
    def __init__(self, public_key: str, secret_key: str, host: str) -> None:
        from langfuse import Langfuse

        self._client = Langfuse(
            public_key=public_key, secret_key=secret_key, host=host, mask=_lf_mask
        )

    @contextmanager
    def trace(
        self,
        name: str,
        *,
        seed: str,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[_LfTrace]:
        from langfuse import Langfuse, propagate_attributes

        trace_id = Langfuse.create_trace_id(seed=seed)
        with (
            self._client.start_as_current_observation(
                name=name, as_type="span", trace_context={"trace_id": trace_id}
            ),
            propagate_attributes(trace_name=name, tags=tags or [], metadata=_str_meta(metadata)),
        ):
            yield _LfTrace(self._client, trace_id)

    @contextmanager
    def span(
        self,
        name: str,
        *,
        kind: SpanKind = "span",
        input: Any = None,
        metadata: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> Iterator[_LfSpan]:
        as_type = {"span": "span", "generation": "generation", "tool": "tool"}[kind]
        kwargs: dict[str, Any] = {"name": name, "as_type": as_type, "input": input}
        if metadata:
            kwargs["metadata"] = metadata
        if model:
            kwargs["model"] = model
        with self._client.start_as_current_observation(**kwargs) as obs:
            handle = _LfSpan(obs)
            try:
                yield handle
            except BaseException as e:
                obs.update(level="ERROR", status_message=f"{type(e).__name__}: {e}")
                raise

    def flush(self) -> None:
        self._client.flush()

    def delete_trace(self, trace_id: str) -> None:
        self._client.api.trace.delete(trace_id)


def _lf_mask(*, data: Any, **_: Any) -> Any:
    return mask_pii(data)


def _str_meta(metadata: dict[str, Any] | None) -> dict[str, str]:
    return {k: str(v)[:200] for k, v in (metadata or {}).items()}


# ---------------------------------------------------------------- step spans


@contextmanager
def step_span(name: str, **kw: Any) -> Iterator[SpanHandle]:
    """A pipeline-step span that rolls up the LLM tokens and cost spent inside it (read from the
    lead budget), so every step carries cost and tokens even though generations are children."""
    from sdr_loop.budget import current_lead_budget

    lb = current_lead_budget()
    tokens0, usd0 = (lb.tokens, lb.spent_usd) if lb else (0, 0.0)
    with get_tracer().span(name, **kw) as sp:
        try:
            yield sp
        finally:
            tokens = (lb.tokens - tokens0) if lb else 0
            usd = (lb.spent_usd - usd0) if lb else 0.0
            sp.update(usage_details={"total": tokens}, cost_details={"total": usd})


# ---------------------------------------------------------------- global accessor

_tracer: Tracer = NullTracer()


def set_tracer(t: Tracer) -> None:
    global _tracer
    _tracer = t


def get_tracer() -> Tracer:
    return _tracer


def init_tracer_from_settings() -> Tracer:
    from sdr_loop.config import get_settings

    s = get_settings()
    if s.langfuse_public_key and s.langfuse_secret_key:
        set_tracer(LangfuseTracer(s.langfuse_public_key, s.langfuse_secret_key, s.langfuse_host))
    return get_tracer()
