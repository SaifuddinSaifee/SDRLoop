"""Settings, model routing table and price table."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Provider = Literal["anthropic", "together", "fake"]
Task = Literal["qualify", "research", "write", "critic", "challenger"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str = ""
    together_api_key: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    evaboot_api_key: str = ""
    evaboot_webhook_secret: str = "change-me"
    public_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    database_url: str = "sqlite+aiosqlite:///./sdr_loop.db"
    outbox_path: str = "outbox.jsonl"

    dry_run: bool = True
    learn_enabled: bool = True
    pinned_prompts: str = ""  # comma-separated prompt names the loop must not touch
    learn_min_new_cases: int = 10
    offline: bool = False
    concurrency: int = 5
    per_lead_usd_cap: float = 0.05
    per_lead_token_cap: int = 40_000
    per_run_usd_cap: float = 5.0
    max_tool_calls: int = 3
    llm_timeout_s: float = 60.0


@lru_cache
def get_settings() -> Settings:
    return Settings()


@dataclass(frozen=True)
class ModelSpec:
    provider: Provider
    model: str


@dataclass(frozen=True)
class Route:
    primary: ModelSpec
    fallback: ModelSpec | None = None


# Defaults. The qualify/critic Together model ids are confirmed in the day-2 bakeoff.
ROUTES: dict[str, Route] = {
    "write": Route(
        ModelSpec("anthropic", "claude-sonnet-5-5"), ModelSpec("anthropic", "claude-haiku-5-5")
    ),
    "research": Route(ModelSpec("anthropic", "claude-haiku-5-5")),
    "challenger": Route(ModelSpec("anthropic", "claude-sonnet-5-5")),
    "qualify": Route(
        ModelSpec("together", "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
        ModelSpec("together", "Qwen/Qwen2.5-72B-Instruct-Turbo"),
    ),
    "critic": Route(
        ModelSpec("together", "Qwen/Qwen2.5-72B-Instruct-Turbo"),
        ModelSpec("together", "deepseek-ai/DeepSeek-V3"),
    ),
}

# USD per 1M tokens (input, output). Together prices are approximate; check before reporting KPIs.
PRICES: dict[str, tuple[float, float]] = {
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-opus-5-5": (4.00, 20.00),
    "meta-llama/Llama-3.3-70B-Instruct-Turbo": (0.88, 0.88),
    "Qwen/Qwen2.5-72B-Instruct-Turbo": (1.20, 1.20),
    "deepseek-ai/DeepSeek-V3": (1.25, 1.25),
    "fake-model": (1.00, 1.00),
}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICES.get(model, (1.00, 1.00))
    return (input_tokens * pin + output_tokens * pout) / 1_000_000
