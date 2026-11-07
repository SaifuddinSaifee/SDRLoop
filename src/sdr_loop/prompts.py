"""Prompt access: Langfuse prompt management when configured, local seed files otherwise.

Labels: `production` = champion, `challenger` = candidate under test. Local files are
prompts/<name>.v<N>.md and act as the offline fallback and the seed for `prompts push`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sdr_loop.config import get_settings

PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"
_client: Any = None


@dataclass(frozen=True)
class PromptVersion:
    name: str
    text: str
    version: str  # "lf-<n>" from Langfuse, "local-<n>" from files

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"


def _local_versions(name: str) -> list[tuple[int, Path]]:
    found = []
    for p in PROMPT_DIR.glob(f"{name}.v*.md"):
        if m := re.search(r"\.v(\d+)\.md$", p.name):
            found.append((int(m.group(1)), p))
    return sorted(found)


def get_local(name: str, version: int | None = None) -> PromptVersion:
    versions = _local_versions(name)
    if not versions:
        raise FileNotFoundError(f"no local prompt for {name}")
    n, path = versions[-1] if version is None else next(v for v in versions if v[0] == version)
    return PromptVersion(name, path.read_text(), f"local-{n}")


def langfuse_client() -> Any:
    global _client
    if _client is None:
        from langfuse import Langfuse

        s = get_settings()
        _client = Langfuse(
            public_key=s.langfuse_public_key, secret_key=s.langfuse_secret_key, host=s.langfuse_host
        )
    return _client


def get_prompt(name: str, label: str = "production") -> PromptVersion:
    s = get_settings()
    if s.offline or not (s.langfuse_public_key and s.langfuse_secret_key):
        return get_local(name)
    try:
        p = langfuse_client().get_prompt(name, label=label, type="text", max_retries=1)
        return PromptVersion(name, p.prompt, f"lf-{p.version}")
    except Exception:  # Langfuse unreachable or prompt missing: fall back, never block a run
        return get_local(name)


def push_local_prompts() -> list[str]:
    """Seed Langfuse with the latest local file of each prompt, labelled `production`."""
    lf = langfuse_client()
    pushed = []
    for name in ("qualify", "research", "writer", "critic"):
        pv = get_local(name)
        lf.create_prompt(name=name, prompt=pv.text, labels=["production"], type="text")
        pushed.append(pv.ref)
    return pushed
