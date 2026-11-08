"""Demo delivery: approved sequences go to the local Mailpit inbox only, once, threaded."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import httpx
import pytest

from sdr_loop import health, mailer
from sdr_loop.bot.controller import BotController
from sdr_loop.config import Settings
from sdr_loop.memory import Repository
from sdr_loop.models import Claim, Email, Fact, Lead, Sequence
from sdr_loop.observability import InMemoryTracer
from sdr_loop.steps.outbox import NotApproved
from sdr_loop.steps.send import dispatch_due, schedule_sequence

T0 = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)


@pytest.fixture
def mail(settings: Settings) -> Settings:
    return settings.model_copy(update={"email_mode": "mailpit", "followup_delay_minutes": 3.0,
                                       "telegram_chat_id": "42"})  # fmt: skip


async def approved_draft(repo: Repository, s: Settings, action: str = "approve") -> int:
    run = await repo.create_run("u", "v1")
    lead = Lead(linkedin_url="https://www.linkedin.com/in/x", full_name="Ana Ruiz",
                title="Head of Sales", company="Atlas", email="ana@atlas.example",
                email_status="safe")  # fmt: skip
    lid, _ = await repo.upsert_lead(lead, run)
    await repo.save_facts(lid, [Fact(id="f1", source_url="https://atlas.example/",
                                     snippet="Atlas was founded in 2022.")])  # fmt: skip
    seq = Sequence(
        email_1=Email(subject="Idea", body="Atlas started in 2022. Open to a call?",
                      claims=[Claim(text="started in 2022", fact_ids=["f1"])]),
        email_2=Email(subject="", body="One more angle. Worth a reply?", claims=[]),
    )  # fmt: skip
    did = await repo.save_draft(lid, seq, prompt_version="writer@local-2")
    c = BotController(repo, s)
    if action == "edit":
        await c.decide(did, "edit", new_text="Subject: Mine\n\nShort. Call?\n---\nBump. Reply?")
    elif action:
        await c.decide(did, action, reason="bad_tone" if action == "reject" else None)
    return did


class Outbox:
    """A fake inbox standing in for SMTP."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.msgs: list[EmailMessage] = []
        self.fail = fail

    async def __call__(self, s: Settings, msg: EmailMessage) -> str:
        if self.fail:
            raise self.fail
        self.msgs.append(msg)
        return str(msg["Message-ID"])


def test_delivery_refuses_anything_but_a_local_mailpit(settings: Settings) -> None:
    with pytest.raises(mailer.UnsafeDelivery, match="off"):
        mailer.assert_safe(settings)
    remote = settings.model_copy(update={"email_mode": "mailpit", "smtp_host": "smtp.gmail.com"})
    with pytest.raises(mailer.UnsafeDelivery, match="only a local Mailpit"):
        mailer.assert_safe(remote)
    mailer.assert_safe(settings.model_copy(update={"email_mode": "mailpit"}))


async def test_send_never_opens_a_connection_when_unsafe(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []
    monkeypatch.setattr(mailer, "_smtp_send", lambda *a: called.append("smtp"))
    remote = settings.model_copy(update={"email_mode": "mailpit", "smtp_host": "10.0.0.5"})
    msg = mailer.build_message(from_name="S", from_email="s@x.test", to="a@b.c", subject="s",
                               body="b")  # fmt: skip
    with pytest.raises(mailer.UnsafeDelivery):
        await mailer.send(remote, msg)
    assert called == []


async def test_approval_schedules_both_emails_once(
    repo: Repository, mail: Settings, tracer: InMemoryTracer
) -> None:
    did = await approved_draft(repo, mail)
    rows = await repo.list_sends()
    assert sorted((r.step, r.to_email, r.status) for r in rows) == [
        (1, "ana@atlas.example", "scheduled"), (2, "ana@atlas.example", "scheduled")]  # fmt: skip
    one, two = sorted(rows, key=lambda r: r.step)
    assert two.due_at - one.due_at == timedelta(minutes=3)
    assert two.subject == "Re: Idea"  # empty follow-up subject threads under email 1
    assert await schedule_sequence(repo, did, mail) == 0  # idempotent: unique (draft, step)


async def test_human_edit_is_what_gets_sent(
    repo: Repository, mail: Settings, tracer: InMemoryTracer
) -> None:
    await approved_draft(repo, mail, action="edit")
    one = next(r for r in await repo.list_sends() if r.step == 1)
    assert (one.subject, one.body) == ("Mine", "Short. Call?")


async def test_nothing_scheduled_when_off_or_rejected_or_unapproved(
    repo: Repository, settings: Settings, mail: Settings, tracer: InMemoryTracer
) -> None:
    did = await approved_draft(repo, settings.model_copy(update={"telegram_chat_id": "42"}))
    assert await repo.list_sends() == []  # EMAIL_MODE=off
    await approved_draft_other(repo, mail)
    assert await repo.list_sends() == []  # rejected
    with pytest.raises(NotApproved):
        await schedule_sequence(repo, await pending_draft(repo), mail)
    assert did


async def approved_draft_other(repo: Repository, s: Settings) -> None:
    run = await repo.create_run("u2", "v1")
    lid, _ = await repo.upsert_lead(
        Lead(linkedin_url="https://www.linkedin.com/in/y", full_name="B", title="CEO",
             company="Beta", email="b@beta.example", email_status="safe"), run)  # fmt: skip
    seq = Sequence(email_1=Email(subject="s", body="b. Call?", claims=[]),
                   email_2=Email(subject="s2", body="b2", claims=[]))  # fmt: skip
    did = await repo.save_draft(lid, seq, prompt_version="w")
    await BotController(repo, s).decide(did, "reject", reason="wrong_fit")


async def pending_draft(repo: Repository) -> int:
    run = await repo.create_run("u3", "v1")
    lid, _ = await repo.upsert_lead(
        Lead(linkedin_url="https://www.linkedin.com/in/z", full_name="C", title="CEO",
             company="Gamma", email="c@gamma.example", email_status="safe"), run)  # fmt: skip
    seq = Sequence(email_1=Email(subject="s", body="b. Call?", claims=[]),
                   email_2=Email(subject="s2", body="b2", claims=[]))  # fmt: skip
    return await repo.save_draft(lid, seq, prompt_version="w")


async def test_dispatch_sends_email_1_now_and_threads_email_2_later(
    repo: Repository, mail: Settings, tracer: InMemoryTracer
) -> None:
    await approved_draft(repo, mail)
    box = Outbox()
    now = datetime.now(UTC)
    assert await dispatch_due(repo, mail, now=now, sender=box) == ["draft 1 email 1"]
    assert await dispatch_due(repo, mail, now=now, sender=box) == []  # follow-up not due yet
    later = now + timedelta(minutes=4)
    assert await dispatch_due(repo, mail, now=later, sender=box) == ["draft 1 email 2"]
    first, second = box.msgs
    assert first["To"] == "ana@atlas.example" and first["X-SDR-Loop"] == "demo"
    assert "Saif" in first["From"]  # sender from campaign.yaml
    assert second["In-Reply-To"] == first["Message-ID"]
    assert await dispatch_due(repo, mail, now=later, sender=box) == []  # never twice
    assert {r.status for r in await repo.list_sends()} == {"sent"}


async def test_inbox_down_retries_and_unsafe_config_fails(
    repo: Repository, mail: Settings, tracer: InMemoryTracer
) -> None:
    await approved_draft(repo, mail)
    now = datetime.now(UTC)
    await dispatch_due(repo, mail, now=now, sender=Outbox(ConnectionRefusedError("down")))
    one = next(r for r in await repo.list_sends() if r.step == 1)
    assert one.status == "scheduled" and "ConnectionRefusedError" in (one.error or "")
    assert one.due_at.replace(tzinfo=UTC) > now  # retried later, not hammered
    later = now + timedelta(minutes=2)
    await dispatch_due(repo, mail, now=later, sender=Outbox(mailer.UnsafeDelivery("no")))
    one = next(r for r in await repo.list_sends() if r.step == 1)
    assert one.status == "failed"


async def test_purge_and_reset_remove_sent_emails(
    repo: Repository, mail: Settings, tracer: InMemoryTracer
) -> None:
    did = await approved_draft(repo, mail)
    lead_id = (await repo.get_draft_by_id(did)).lead_id
    counts = await repo.purge_lead_rows(lead_id)
    assert counts["sends"] == 2 and await repo.list_sends() == []
    await approved_draft_other(repo, mail)
    counts = await repo.reset_all()
    assert counts["leads"] == 1 and await repo.count_leads() == 0


async def test_email_check_states(mail: Settings, settings: Settings) -> None:
    def client(resp: httpx.Response | None) -> httpx.AsyncClient:
        def handler(req: httpx.Request) -> httpx.Response:
            if resp is None:
                raise httpx.ConnectError("refused")
            return resp

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async with client(None) as http:
        assert (await health.check_email(settings, http)).status == "warn"  # off
        down = await health.check_email(mail, http)
        assert down.status == "fail" and down.action == "start_mailpit"
    async with client(httpx.Response(200, json={"Version": "v1.27.0"})) as http:
        up = await health.check_email(mail, http)
        assert up.status == "ok" and "v1.27.0" in up.detail
    remote = mail.model_copy(update={"mailpit_url": "https://mail.example.com"})
    async with client(httpx.Response(200, json={"Version": "x"})) as http:
        assert (await health.check_email(remote, http)).status == "fail"  # never a remote API
