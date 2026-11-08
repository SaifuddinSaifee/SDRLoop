"""Settings, model routing table and price table."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Task = Literal["qualify", "research", "write", "critic", "challenger"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    together_api_key: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    # Langfuse's docs call this LANGFUSE_BASE_URL; accept both. The region matters: keys from
    # us./jp. cloud return 401 on the default EU host.
    langfuse_host: str = Field(
        "https://cloud.langfuse.com",
        validation_alias=AliasChoices("LANGFUSE_HOST", "LANGFUSE_BASE_URL"),
    )
    evaboot_api_key: str = ""
    evaboot_webhook_secret: str = "change-me"
    public_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    database_url: str = "sqlite+aiosqlite:///./sdr_loop.db"
    outbox_path: str = "outbox.jsonl"
    state_dir: str = ".sdr-loop"  # background job records and logs
    learn_at: str = "02:00"  # local time of the nightly learning cycle (`sdr-loop start`)

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
    llm_timeout_s: float = (
        90.0  # GLM thinks before answering: allow more than a non-reasoning model
    )
    # GLM-5.3 always thinks; this only shortens the trace (low | medium | high | max).
    glm_reasoning_effort: str = "low"


@lru_cache
def get_settings() -> Settings:
    return Settings()


@dataclass(frozen=True)
class ModelSpec:
    provider: str  # key into the gateway's provider registry ("together" in production)
    model: str


@dataclass(frozen=True)
class Route:
    primary: ModelSpec
    fallback: ModelSpec | None = None


GLM = "zai-org/GLM-5.3"
GLM_FLASH = "zai-org/GLM-5.3-Flash"
DEEPSEEK_FLASH = "deepseek-ai/DeepSeek-V4.1-Flash"
DEEPSEEK_PRO = "deepseek-ai/DeepSeek-V4-Pro-0813"

# Everything runs on Together AI. The critic must come from a different model family than the
# writer (a model grading its own style is too lenient): GLM writes, DeepSeek critiques.
# Model ids are from Together's serverless model table; they have not been exercised live yet.
ROUTES: dict[str, Route] = {
    "write": Route(ModelSpec("together", GLM), ModelSpec("together", GLM_FLASH)),
    "challenger": Route(ModelSpec("together", GLM), ModelSpec("together", GLM_FLASH)),
    "research": Route(ModelSpec("together", GLM_FLASH), ModelSpec("together", GLM)),
    "qualify": Route(ModelSpec("together", GLM_FLASH), ModelSpec("together", DEEPSEEK_FLASH)),
    "critic": Route(ModelSpec("together", DEEPSEEK_FLASH), ModelSpec("together", DEEPSEEK_PRO)),
}

# USD per 1M tokens (input, output), from Together's serverless table. Cached-input discounts are
# ignored, so reported cost is a slight overestimate.
PRICES: dict[str, tuple[float, float]] = {
    GLM: (1.40, 4.40),
    GLM_FLASH: (0.15, 0.50),
    DEEPSEEK_FLASH: (0.30, 1.20),
    DEEPSEEK_PRO: (1.32, 3.96),
    "fake-model": (1.00, 1.00),
}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICES.get(model, (1.00, 1.00))
    return (input_tokens * pin + output_tokens * pout) / 1_000_000
