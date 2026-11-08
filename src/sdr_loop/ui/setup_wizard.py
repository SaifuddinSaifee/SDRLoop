"""Setup page: everything the agent needs, step by step, saved where the rest of the app reads it.

Company & offer -> campaign.yaml        Who to target -> icp.yaml
Keys and switches -> .env (and this process's environment, so they apply at once)
Secrets are write-only here: the page shows whether a key is set, never the key.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import httpx
import streamlit as st
import yaml
from streamlit.delta_generator import DeltaGenerator

from sdr_loop import health, mailer
from sdr_loop.bot.setup import TelegramSetupError, find_chats, send_test
from sdr_loop.campaign import Campaign, Sender, load_campaign, save_campaign
from sdr_loop.config import Settings, get_settings
from sdr_loop.envfile import save_setting
from sdr_loop.models import ICP
from sdr_loop.reset import ResetPlan, ResetRefused, fresh_start

ICP_PATH = Path("icp.yaml")
ICON = {"ok": "✅", "warn": "⚠️", "fail": "❌"}
REGIONS = {
    "EU (cloud.langfuse.com)": "https://cloud.langfuse.com",
    "US (us.cloud.langfuse.com)": "https://us.cloud.langfuse.com",
    "JP (jp.cloud.langfuse.com)": "https://jp.cloud.langfuse.com",
}
EFFORTS = ["low", "medium", "high", "max"]


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _saved(msg: str, clear: Callable[[], None]) -> None:
    clear()
    st.toast(msg)
    st.rerun()


def _secret(label: str, is_set: bool, key: str) -> str:
    return st.text_input(
        label,
        type="password",
        key=key,
        placeholder="•••••••• saved (leave empty to keep)" if is_set else "paste here",
    )


def setup_page(checks: list[health.Check], clear_checks: Callable[[], None]) -> None:
    s = get_settings()
    by = {c.key: c for c in checks}
    st.header("Setup")
    st.caption(
        "Work top to bottom. Each step saves on its own; the status updates as you go. "
        "Required: 1, 2, 3. Recommended: 4, 5. Optional: 6, 7."
    )
    first_open = next((c.key for c in checks if c.status != "ok"), None)

    def step(n: int, title: str, key: str | None) -> DeltaGenerator:
        c = by.get(key) if key else None
        icon = ICON[c.status] if c else "•"
        exp = st.expander(f"{icon} {n}. {title}", expanded=key is not None and key == first_open)
        if c is not None and c.status != "ok":
            exp.caption(f"{c.detail} {c.fix}".strip())
        elif c is not None:
            exp.caption(c.detail)
        return exp

    with step(1, "Company & offer: who the emails are from", "campaign"):
        company_step(clear_checks)
    with step(2, "Who to target (ideal customer profile)", None):
        icp_step()
    with step(3, "AI models (Together AI)", "together"):
        models_step(s, clear_checks)
    with step(4, "Real leads (Evaboot + Sales Navigator)", "evaboot"):
        leads_step(s, clear_checks)
    with step(5, "Email delivery (demo inbox)", "email"):
        email_step(s, by["email"], clear_checks)
    with step(6, "Approvals on your phone (Telegram, optional)", "telegram"):
        telegram_step(s, by["telegram"], clear_checks)
    with step(7, "Tracing (Langfuse, optional)", "langfuse"):
        langfuse_step(s, by["langfuse"], clear_checks)
    with st.expander("🧹 Fresh start: clear leads, drafts and history"):
        reset_step(s, clear_checks)


# ------------------------------------------------------------------------------------- steps


def company_step(clear: Callable[[], None]) -> None:
    c, _ = load_campaign()
    st.markdown(
        "The writer may only say what **allowed claims** support about you, and the critic "
        "flags anything else, the same way it flags unsupported claims about a prospect."
    )
    with st.form("campaign"):
        a, b = st.columns(2)
        company = a.text_input("Company", c.company)
        website = b.text_input("Website", c.website)
        one_liner = st.text_area("What you do (one or two sentences)", c.one_liner, height=80)
        audience = st.text_area("Who it's for", c.audience, height=70)
        pain = st.text_area("Problem you solve", c.pain, height=90)
        value = st.text_input("Value in one line", c.value)
        allowed = st.text_area(
            "Allowed claims about you (one per line)", "\n".join(c.allowed_claims), height=200
        )
        forbidden = st.text_area(
            "Never claim (one per line)", "\n".join(c.forbidden_claims), height=110
        )
        goal = st.text_input("Goal of this campaign", c.goal)
        ask = st.text_input("The ask (what you want the prospect to do)", c.ask)
        tone = st.text_input("Tone", c.tone)
        x, y, z = st.columns(3)
        sname = x.text_input("Sender name", c.sender.name)
        stitle = y.text_input("Sender title", c.sender.title)
        semail = z.text_input("Sender email (From:)", c.sender.email)
        if st.form_submit_button("Save company & offer", type="primary"):
            new = Campaign(
                company=company.strip(), website=website.strip(), one_liner=one_liner.strip(),
                audience=audience.strip(), pain=pain.strip(), value=value.strip(),
                allowed_claims=_lines(allowed), forbidden_claims=_lines(forbidden),
                goal=goal.strip(), ask=ask.strip(), tone=tone.strip(),
                sender=Sender(name=sname.strip(), title=stitle.strip(), email=semail.strip()),
                search_urls=c.search_urls,
            )  # fmt: skip
            save_campaign(new)
            _saved("Company & offer saved.", clear)


def icp_step() -> None:
    icp = ICP.model_validate(yaml.safe_load(ICP_PATH.read_text()) if ICP_PATH.exists() else {})
    st.markdown(
        "Hard rules run in code first (no AI cost); the AI then scores the rest against the "
        "description and your offer. Leads below 75/100 are not written to."
    )
    with st.form("icp"):
        desc = st.text_area("Who you want to reach", icp.description, height=80)
        a, b = st.columns(2)
        industries = a.text_area("Industries (one per line)", "\n".join(icp.industries), height=150)
        countries = b.text_area(
            "Countries (ISO codes, one per line; empty = any)", "\n".join(icp.countries), height=150
        )
        titles = a.text_area(
            "Title keywords to include (one per line)", "\n".join(icp.title_keywords), height=180
        )
        exclude = b.text_area(
            "Title words to exclude (one per line)", "\n".join(icp.title_exclude), height=180
        )
        lo = a.number_input("Min company headcount", 1, 1_000_000, icp.min_headcount)
        hi = b.number_input("Max company headcount", 1, 1_000_000, icp.max_headcount)
        disq = st.text_area(
            "Never contact these companies (competitors, customers; one per line)",
            "\n".join(icp.disqualifier_companies), height=120,
        )  # fmt: skip
        if st.form_submit_button("Save target profile", type="primary"):
            new = icp.model_copy(update={
                "description": desc.strip(), "industries": _lines(industries),
                "countries": [c.upper() for c in _lines(countries)],
                "title_keywords": [t.lower() for t in _lines(titles)],
                "title_exclude": [t.lower() for t in _lines(exclude)],
                "min_headcount": int(lo), "max_headcount": int(hi),
                "disqualifier_companies": _lines(disq),
            })  # fmt: skip
            ICP_PATH.write_text(
                "# Ideal customer profile: edit on the control panel's Setup page.\n"
                + yaml.safe_dump(new.model_dump(), sort_keys=False, allow_unicode=True)
            )
            st.toast("Target profile saved.")


def models_step(s: Settings, clear: Callable[[], None]) -> None:
    st.markdown(
        "All models run on [Together AI](https://api.together.ai): GLM-5.3 writes, GLM-5.3 "
        "Flash qualifies and researches, DeepSeek critiques (a different family from the "
        "writer). Typical cost: under $0.01 per lead."
    )
    with st.form("models"):
        key = _secret("Together AI API key", bool(s.together_api_key), "together_key")
        effort = st.selectbox(
            "GLM thinking effort (higher = slower, pricier, sometimes better)",
            EFFORTS, index=EFFORTS.index(s.glm_reasoning_effort),
        )  # fmt: skip
        if st.form_submit_button("Save and test", type="primary"):
            if key:
                save_setting("TOGETHER_API_KEY", key)
            save_setting("GLM_REASONING_EFFORT", effort)
            _saved("Saved.", clear)


def leads_step(s: Settings, clear: Callable[[], None]) -> None:
    c, _ = load_campaign()
    st.markdown(
        "Evaboot extracts the people from your **own Sales Navigator search** (you need an active "
        "Sales Navigator seat) and finds and verifies their emails. **1 lead = 1 Evaboot credit.** "
        "Only leads with a `safe` email are processed. Use **Limit** on the Home page to keep the "
        "first runs small."
    )
    modes = {
        "Live: the real Evaboot API (your Sales Navigator seat + credits)": "live",
        "Simulated: a demo export of real companies (no Sales Navigator needed)": "simulated",
    }
    with st.form("leads"):
        mode = st.radio(
            "Evaboot connection", list(modes),
            index=list(modes.values()).index(s.evaboot_mode),
            help="Simulated speaks the same API locally: every later step (research, AI, "
            "approval, email) is real.",
        )  # fmt: skip
        key = _secret("Evaboot API key", bool(s.evaboot_api_key), "evaboot_key")
        urls = st.text_area(
            "Sales Navigator search URLs (one per line; copy from your browser)",
            "\n".join(c.search_urls), height=100,
            placeholder="https://www.linkedin.com/sales/search/people?query=...",
        )  # fmt: skip
        if st.form_submit_button("Save", type="primary"):
            bad = [u for u in _lines(urls) if "linkedin.com/sales" not in u]
            if bad:
                st.error(f"Not a Sales Navigator URL: {bad[0][:80]}")
                return
            if key:
                save_setting("EVABOOT_API_KEY", key)
            save_setting("EVABOOT_MODE", modes[mode])
            save_campaign(c.model_copy(update={"search_urls": _lines(urls)}))
            _saved("Saved.", clear)


def email_step(s: Settings, check: health.Check, clear: Callable[[], None]) -> None:
    c, _ = load_campaign()
    st.markdown(
        "Approved emails are **sent for real, but to [Mailpit](https://mailpit.axllent.org)**, a "
        "local inbox that catches every message and delivers nothing. Real leads, real "
        "addresses, nobody receives anything. The app refuses any SMTP server that is not local."
    )
    with st.form("email"):
        on = st.toggle("Send approved emails to the demo inbox", s.email_mode == "mailpit")
        delay = st.number_input(
            "Send the follow-up (email 2) after N minutes", 0.0, 10_080.0,
            float(s.followup_delay_minutes), step=1.0,
        )  # fmt: skip
        if st.form_submit_button("Save", type="primary"):
            save_setting("EMAIL_MODE", "mailpit" if on else "off")
            save_setting("FOLLOWUP_DELAY_MINUTES", f"{delay:g}")
            _saved("Saved.", clear)
    a, b, c2 = st.columns(3)
    if check.action == "start_mailpit" and a.button("▶ Start Mailpit"):
        with st.spinner("Starting Mailpit (Docker)…"):
            ok, note = health.start_mailpit()
        (st.success if ok else st.error)(note)
        if ok:
            clear()
    if check.status == "ok":
        a.link_button("Open the inbox ↗", mailer.inbox_url(s))
        if b.button("Send a test email"):
            msg = mailer.build_message(
                from_name=c.sender.name or c.company, from_email=c.sender.email or "sdr@test",
                to=c.sender.email or "you@sdr-loop.test", subject="sdr-loop test",
                body="If you can read this in Mailpit, delivery works.",
            )  # fmt: skip
            try:
                asyncio.run(mailer.send(s, msg))
                st.toast("Sent: check the inbox.")
            except (OSError, mailer.UnsafeDelivery) as e:
                st.error(str(e))
    c2.caption(f"From: {c.sender.name} <{c.sender.email}> (set in step 1)")


def telegram_step(s: Settings, check: health.Check, clear: Callable[[], None]) -> None:
    st.markdown(
        "Get each draft on your phone with Approve / Edit / Reject buttons, and start runs with "
        "`/run`. In Telegram: message **@BotFather**, send `/newbot`, pick a name and a username "
        "ending in `bot`, and paste the token below."
    )
    with st.form("telegram"):
        token = _secret("Bot token", bool(s.telegram_bot_token), "tg_token")
        if st.form_submit_button("Save", type="primary") and token:
            save_setting("TELEGRAM_BOT_TOKEN", token)
            _saved("Saved. Now open your bot, press Start, send 'hi', then 'Find my chat'.", clear)
    if not s.telegram_bot_token:
        return
    a, b = st.columns(2)
    if not s.telegram_chat_id and a.button("Find my chat"):

        async def find() -> list[str]:
            async with httpx.AsyncClient(timeout=15) as client:
                return [str(c.chat_id) for c in await find_chats(client, s.telegram_bot_token)]

        try:
            chats = asyncio.run(find())
        except TelegramSetupError as e:
            st.error(str(e))
            return
        if not chats:
            st.warning("No messages yet: open your bot in Telegram, press Start, send 'hi'.")
            return
        save_setting("TELEGRAM_CHAT_ID", chats[0])
        _saved(f"Saved chat {chats[0]}.", clear)
    if s.telegram_chat_id and check.status == "ok" and b.button("Send a test message"):

        async def test() -> None:
            async with httpx.AsyncClient(timeout=15) as client:
                await send_test(client, s.telegram_bot_token, s.telegram_chat_id)

        try:
            asyncio.run(test())
            st.toast("Sent: check your phone.")
        except TelegramSetupError as e:
            st.error(str(e))
    if s.telegram_chat_id:
        st.caption("The bot runs inside `sdr-loop start`; it picks up a new token within seconds.")


def langfuse_step(s: Settings, check: health.Check, clear: Callable[[], None]) -> None:
    st.markdown(
        "One trace per lead (every step, model call, cost and score) and prompt versions for the "
        "learning loop. Keys: Langfuse → Settings → API keys. Pick the region your project is in."
    )
    current = next((k for k, v in REGIONS.items() if v == s.langfuse_host.rstrip("/")), None)
    with st.form("langfuse"):
        pk = _secret("Public key (pk-lf-…)", bool(s.langfuse_public_key), "lf_pk")
        sk = _secret("Secret key (sk-lf-…)", bool(s.langfuse_secret_key), "lf_sk")
        region = st.selectbox(
            "Region", list(REGIONS), index=list(REGIONS).index(current) if current else 0
        )
        if st.form_submit_button("Save and test", type="primary"):
            if pk:
                save_setting("LANGFUSE_PUBLIC_KEY", pk)
            if sk:
                save_setting("LANGFUSE_SECRET_KEY", sk)
            save_setting("LANGFUSE_HOST", REGIONS[region])
            _saved("Saved.", clear)
    if check.action == "upload_prompts" and st.button("Upload prompts"):
        from sdr_loop.prompts import prompts_to_upload, push_local_prompts

        _saved("Uploaded: " + ", ".join(push_local_prompts(prompts_to_upload())), clear)


def reset_step(s: Settings, clear: Callable[[], None]) -> None:
    st.markdown(
        "Use this before switching to real leads, so demo data doesn't mix in. Your setup "
        "(keys, company, target profile, prompts) is kept. Langfuse traces stay in Langfuse."
    )
    data = st.checkbox("Leads, runs, drafts, decisions and sent emails", True)
    learning = st.checkbox("What the learning loop learned (eval cases, promotion history)", True)
    outbox = st.checkbox("The outbox file", True)
    jobs = st.checkbox("Run history and logs", True)
    inbox = st.checkbox("The demo inbox (Mailpit)", True)
    confirm = st.text_input("Type RESET to confirm")
    if st.button("Clear", type="primary", disabled=confirm.strip() != "RESET"):
        try:
            done = asyncio.run(fresh_start(s, ResetPlan(data, learning, outbox, jobs, inbox)))
        except ResetRefused as e:
            st.error(str(e))
            return
        for line in done:
            st.success(line)
        clear()
