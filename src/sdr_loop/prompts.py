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


def _champion_file(name: str) -> Path:
    return PROMPT_DIR / f"{name}.champion"


def _champion_n(name: str) -> int | None:
    f = _champion_file(name)
    return int(f.read_text().strip()) if f.exists() else None


def get_local(name: str, version: int | None = None) -> PromptVersion:
    """A local prompt version. Default = the champion (pointer file) or else the latest file."""
    versions = _local_versions(name)
    if not versions:
        raise FileNotFoundError(f"no local prompt for {name}")
    if version is None:
        version = _champion_n(name) or versions[-1][0]
    n, path = next(v for v in versions if v[0] == version)
    return PromptVersion(name, path.read_text(), f"local-{n}")


def _use_langfuse() -> bool:
    s = get_settings()
    return not s.offline and bool(s.langfuse_public_key and s.langfuse_secret_key)


def version_number(pv: PromptVersion) -> int:
    return int(pv.version.split("-")[1])


def langfuse_client() -> Any:
    global _client
    if _client is None:
        from langfuse import Langfuse

        s = get_settings()
        _client = Langfuse(
            public_key=s.langfuse_public_key, secret_key=s.langfuse_secret_key, host=s.langfuse_host
        )
    return _client


def get_challenger(name: str) -> PromptVersion | None:
    """The newest unpromoted candidate, if any."""
    if _use_langfuse():
        try:
            p = langfuse_client().get_prompt(name, label="challenger", type="text", max_retries=1)
            return PromptVersion(name, p.prompt, f"lf-{p.version}")
        except Exception:
            return None
    champ = _champion_n(name) or 0
    latest = _local_versions(name)[-1][0]
    return get_local(name, latest) if latest > champ else None


def save_challenger(name: str, text: str) -> PromptVersion:
    """Store a candidate prompt labelled `challenger`. It never becomes live by itself."""
    if _use_langfuse():
        p = langfuse_client().create_prompt(
            name=name, prompt=text, labels=["challenger"], type="text"
        )
        return PromptVersion(name, text, f"lf-{p.version}")
    latest = _local_versions(name)[-1][0]
    if _champion_n(name) is None:  # pin today's champion before adding a newer file
        _champion_file(name).write_text(str(latest))
    n = latest + 1
    (PROMPT_DIR / f"{name}.v{n}.md").write_text(text)
    return PromptVersion(name, text, f"local-{n}")


def promote_prompt(pv: PromptVersion) -> None:
    """Make `pv` the champion (Langfuse: move the `production` label; local: champion pointer)."""
    if pv.version.startswith("lf-"):
        langfuse_client().update_prompt(
            name=pv.name, version=version_number(pv), new_labels=["production"]
        )
    else:
        _champion_file(pv.name).write_text(str(version_number(pv)))


def get_prompt(name: str, label: str = "production") -> PromptVersion:
    if label == "challenger":
        if (c := get_challenger(name)) is None:
            raise LookupError(f"no challenger for {name}")
        return c
    if not _use_langfuse():
        return get_local(name)
    try:
        p = langfuse_client().get_prompt(name, label=label, type="text", max_retries=1)
        return PromptVersion(name, p.prompt, f"lf-{p.version}")
    except Exception:  # Langfuse unreachable or prompt missing: fall back, never block a run
        return get_local(name)


PIPELINE_PROMPTS = ("qualify", "research", "writer", "critic")


def missing_prompts(lf: Any | None = None) -> list[str]:
    """Pipeline prompts with no `production` version in Langfuse (local files are used instead)."""
    lf = lf or langfuse_client()
    missing = []
    for name in PIPELINE_PROMPTS:
        try:
            lf.get_prompt(name, label="production", type="text", max_retries=0, cache_ttl_seconds=0)
        except Exception:
            missing.append(name)
    return missing


def stale_seed_prompts(lf: Any | None = None) -> list[str]:
    """Prompts whose Langfuse `production` text is an OLDER local seed file, i.e. we shipped a newer
    one. A version the learning loop promoted is never a local file, so it is never reported."""
    lf = lf or langfuse_client()
    stale = []
    for name in PIPELINE_PROMPTS:
        try:
            live = lf.get_prompt(
                name, label="production", type="text", max_retries=0, cache_ttl_seconds=0
            ).prompt
        except Exception:
            continue
        texts = [p.read_text() for _, p in _local_versions(name)]
        if live != texts[-1] and live in texts[:-1]:
            stale.append(name)
    return stale


def prompts_to_upload(lf: Any | None = None) -> list[str]:
    """Missing in Langfuse, or still on an older built-in version."""
    lf = lf or langfuse_client()
    missing = missing_prompts(lf)
    return missing + [n for n in stale_seed_prompts(lf) if n not in missing]


def push_local_prompts(names: tuple[str, ...] | list[str] = PIPELINE_PROMPTS) -> list[str]:
    """Seed Langfuse with the latest local file of each prompt, labelled `production`."""
    lf = langfuse_client()
    pushed = []
    for name in names:
        pv = get_local(name)
        lf.create_prompt(name=name, prompt=pv.text, labels=["production"], type="text")
        pushed.append(pv.ref)
    return pushed
