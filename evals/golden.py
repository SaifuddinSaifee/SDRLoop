"""Eval-side alias of the runtime golden loader (single implementation in sdr_loop.learn.golden)."""

from __future__ import annotations

from pathlib import Path

from sdr_loop.learn import golden as _g

EVALS = Path(__file__).resolve().parent


def golden_leads():  # type: ignore[no-untyped-def]
    return _g.golden_leads(EVALS)


def golden_facts():  # type: ignore[no-untyped-def]
    return _g.golden_facts(EVALS)


def golden_fit_with_facts():  # type: ignore[no-untyped-def]
    return _g.golden_fit_with_facts(EVALS)
