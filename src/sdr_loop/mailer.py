"""Demo email delivery: approved emails go to a local Mailpit inbox, never to the internet.

Mailpit (https://mailpit.axllent.org) accepts SMTP and shows every message in a web inbox without
relaying it, so a demo can use real leads' addresses and nobody receives anything. The guard is
in code, not just config: `EMAIL_MODE` must be `mailpit` and the SMTP host must be local. There
is no code path that sends to any other server (v1 has no real-send adapters).
"""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from urllib.parse import urlparse

import httpx

from sdr_loop.config import Settings

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "mailpit"})


class UnsafeDelivery(Exception):
    """Refused: delivery is off, or the target is not a local demo inbox."""


def assert_safe(s: Settings) -> None:
    if s.email_mode != "mailpit":
        raise UnsafeDelivery("Email delivery is off (EMAIL_MODE is not 'mailpit').")
    if s.smtp_host not in LOCAL_HOSTS:
        raise UnsafeDelivery(
            f"Refusing to send through {s.smtp_host!r}: only a local Mailpit inbox is allowed "
            f"({', '.join(sorted(LOCAL_HOSTS))})."
        )


def build_message(
    *,
    from_name: str,
    from_email: str,
    to: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_email))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=from_email.rpartition("@")[2] or "sdr-loop.test")
    msg["X-SDR-Loop"] = "demo"
    if in_reply_to:  # the follow-up threads under email 1, as it would in a real inbox
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    return msg


def _smtp_send(host: str, port: int, msg: EmailMessage) -> None:
    with smtplib.SMTP(host, port, timeout=10) as smtp:
        smtp.send_message(msg)


async def send(s: Settings, msg: EmailMessage) -> str:
    """Deliver to the demo inbox. Returns the Message-ID."""
    assert_safe(s)
    await asyncio.to_thread(_smtp_send, s.smtp_host, s.smtp_port, msg)
    return str(msg["Message-ID"])


def inbox_url(s: Settings) -> str:
    return (s.mailpit_ui_url or s.mailpit_url).rstrip("/")


def _local_api(s: Settings) -> str:
    host = urlparse(s.mailpit_url).hostname or ""
    if host not in LOCAL_HOSTS:
        raise UnsafeDelivery(f"MAILPIT_URL must point at a local Mailpit, not {host!r}.")
    return s.mailpit_url.rstrip("/")


async def mailpit_version(s: Settings, client: httpx.AsyncClient) -> str | None:
    """Mailpit's version if its API answers, else None."""
    try:
        r = await client.get(f"{_local_api(s)}/api/v1/info")
        return str(r.json().get("Version", "?")) if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError, UnsafeDelivery):
        return None


async def clear_inbox(s: Settings, client: httpx.AsyncClient) -> bool:
    try:
        r = await client.delete(f"{_local_api(s)}/api/v1/messages")
    except (httpx.HTTPError, UnsafeDelivery):
        return False
    return r.status_code == 200
