"""Per-lead and per-run budgets, carried in contextvars."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


class BudgetExceeded(Exception):
    def __init__(self, scope: str, detail: str) -> None:
        super().__init__(f"{scope} budget exceeded: {detail}")
        self.scope = scope


@dataclass
class RunBudget:
    max_usd: float
    spent_usd: float = 0.0

    def check(self) -> None:
        if self.spent_usd >= self.max_usd:
            raise BudgetExceeded("run", f"${self.spent_usd:.4f} >= ${self.max_usd:.4f}")


@dataclass
class LeadBudget:
    max_usd: float
    max_tokens: int
    max_tool_calls: int
    spent_usd: float = 0.0
    tokens: int = 0
    tool_calls: int = 0

    def check(self) -> None:
        if self.spent_usd >= self.max_usd:
            raise BudgetExceeded("lead", f"${self.spent_usd:.4f} >= ${self.max_usd:.4f}")
        if self.tokens >= self.max_tokens:
            raise BudgetExceeded("lead", f"{self.tokens} tokens >= {self.max_tokens}")

    def try_tool_call(self) -> bool:
        """Reserve one tool call. False means the cap is reached; the call must be refused."""
        if self.tool_calls >= self.max_tool_calls:
            return False
        self.tool_calls += 1
        return True


_run: ContextVar[RunBudget | None] = ContextVar("run_budget", default=None)
_lead: ContextVar[LeadBudget | None] = ContextVar("lead_budget", default=None)


def set_run_budget(b: RunBudget | None) -> None:
    _run.set(b)


def set_lead_budget(b: LeadBudget | None) -> None:
    _lead.set(b)


def current_lead_budget() -> LeadBudget | None:
    return _lead.get()


def check_budgets() -> None:
    if (r := _run.get()) is not None:
        r.check()
    if (lb := _lead.get()) is not None:
        lb.check()


def charge(tokens: int, usd: float) -> None:
    if (r := _run.get()) is not None:
        r.spent_usd += usd
    if (lb := _lead.get()) is not None:
        lb.spent_usd += usd
        lb.tokens += tokens
