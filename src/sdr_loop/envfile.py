"""Update one key in `.env` in place, so setup steps can save what they discover (the Telegram chat
id) instead of asking the operator to copy it by hand. Comments, order and other keys are kept."""

from __future__ import annotations

import re
from pathlib import Path

ENV_PATH = Path(".env")


def set_env_value(key: str, value: str, path: Path = ENV_PATH) -> None:
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
        raise ValueError(f"bad env key {key!r}")
    if "\n" in value:
        raise ValueError("env values must be one line")
    line = f"{key}={value}"
    lines = path.read_text().splitlines() if path.exists() else []
    pattern = re.compile(rf"^\s*(export\s+)?{key}\s*=")
    for i, old in enumerate(lines):
        if pattern.match(old):
            lines[i] = line
            break
    else:
        lines.append(line)
    path.write_text("\n".join(lines) + "\n")
