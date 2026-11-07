from __future__ import annotations

import pytest

from sdr_loop.config import get_settings
from sdr_loop.llm import set_gateway
from sdr_loop.observability import init_tracer_from_settings


@pytest.fixture(autouse=True)
def _real_models(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("eval") is None:
        return
    s = get_settings()
    if not s.together_api_key:
        pytest.skip("evals need TOGETHER_API_KEY")
    set_gateway(None)  # build real providers from settings
    init_tracer_from_settings()
