"""The campaign brief: what the writer, critic and qualifier are told about us."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdr_loop import health
from sdr_loop.campaign import Campaign, Sender, load_campaign, save_campaign
from sdr_loop.config import get_settings
from sdr_loop.models import Lead
from sdr_loop.prompts import get_local
from sdr_loop.steps.write import writer_input


def test_shipped_evaboot_brief_is_complete_and_has_guardrails() -> None:
    c, version = load_campaign(Path("campaign.yaml"))
    assert c.company == "Evaboot" and c.missing() == []
    assert version.startswith("campaign-")
    never = " ".join(c.forbidden_claims).lower()
    assert "free trial" in never and "phone" in never  # not offered per evaboot.com


def test_writer_sees_the_brief_and_v2_prompt_is_generic() -> None:
    lead = Lead(linkedin_url="u", full_name="A", title="CEO", company="Atlas")
    payload = json.loads(writer_input(lead, []))
    assert payload["ABOUT_US"]["company"] == "Evaboot"
    assert payload["ABOUT_US"]["sign_off"]["name"]
    prompt = get_local("writer").text
    assert "Evaboot" not in prompt and "ABOUT_US" in prompt  # company lives in data, not prompt
    assert "ABOUT_US" in get_local("critic").text


def test_save_and_load_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "c.yaml"
    monkeypatch.setenv("CAMPAIGN_PATH", str(p))
    get_settings.cache_clear()
    c = Campaign(
        company="Gather", one_liner="Collects documents.", ask="20 minutes?",
        allowed_claims=["Clients need no account"], sender=Sender(name="Saif"),
    )  # fmt: skip
    save_campaign(c)
    loaded, v1 = load_campaign()
    assert loaded == c
    save_campaign(c.model_copy(update={"ask": "15 minutes?"}))
    assert load_campaign()[1] != v1  # version changes with content (traced per lead)


def test_campaign_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAMPAIGN_PATH", str(tmp_path / "none.yaml"))
    get_settings.cache_clear()
    c = health.check_campaign()
    assert c.status == "fail" and "company" in c.detail and "sender name" in c.detail
