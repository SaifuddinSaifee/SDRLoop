"""The control panel: start runs, watch them, review drafts, run learning, check setup.

Launched by `sdr-loop start` (or `uv run streamlit run src/sdr_loop/ui/streamlit_app.py`).

Runs and learning cycles are background jobs (`sdr_loop.jobs`), the same ones Telegram's /run and
the nightly schedule start. Review decisions go through the same BotController path as Telegram,
so tracing, the approval-record guard and the dry-run outbox behave identically.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import streamlit as st

from sdr_loop import health, kpis, mailer
from sdr_loop.bot.controller import BotController
from sdr_loop.bot.setup import TelegramSetupError, send_test
from sdr_loop.campaign import load_campaign
from sdr_loop.config import get_settings
from sdr_loop.jobs import DEFAULT_LIMIT, Job, JobBusy, JobStore, run_args
from sdr_loop.memory import Repository
from sdr_loop.models import Email, Sequence
from sdr_loop.observability import init_tracer_from_settings
from sdr_loop.steps import approve as ap
from sdr_loop.steps.send import dispatch_due
from sdr_loop.ui.setup_wizard import setup_page

PAGES = ["Setup", "Home", "Review queue", "Sent emails", "Learning", "KPIs"]
ICON = {"ok": "✅", "warn": "⚠️", "fail": "❌"}


def run[T](fn: Callable[[Repository], Awaitable[T]]) -> T:
    """Fresh engine per call: Streamlit reruns the script, and async engines are loop-bound."""

    async def go() -> T:
        repo = Repository(get_settings().database_url)
        if get_settings().database_url.startswith("sqlite"):
            await repo.init_schema()
        try:
            return await fn(repo)
        finally:
            await repo.close()

    return asyncio.run(go())


@st.cache_resource
def _init() -> None:
    init_tracer_from_settings()


def store() -> JobStore:
    return JobStore(get_settings().state_dir)


@st.cache_data(ttl=60, show_spinner="Checking your setup…")
def setup_checks() -> list[health.Check]:
    checks, _, _ = asyncio.run(health.run_checks(get_settings()))
    return checks


def start_job(kind: str, args: list[str], label: str) -> None:
    try:
        store().start("learn" if kind == "learn" else "run", args, label=label, source="web")
    except JobBusy as e:
        st.warning(str(e))
        return
    st.rerun()


# ---------------------------------------------------------------------- Home


def home_page() -> None:
    st.title("SDR loop")
    st.caption(
        "Sales Navigator leads → qualified → researched → two-email drafts → your approval → "
        "dry-run outbox. Every edit and rejection teaches the writer."
    )
    stats = run(_stats)
    c = st.columns(4)
    c[0].metric("Drafts waiting for you", stats["pending"])
    c[1].metric("Leads stored", stats["leads"])
    c[2].metric("Approved (outbox)", stats["approved"])
    c[3].metric("Model spend", f"${stats['spend']:.2f}")
    if stats["pending"]:
        where = " or in Telegram" if get_settings().telegram_chat_id else ""
        st.info(f"📝 {stats['pending']} draft(s) to review: open **Review queue**{where}.")

    checks = setup_checks()
    left, right = st.columns([3, 2])
    with left:
        run_form({c.key: c for c in checks})
        job_panel()
    with right:
        setup_panel(checks)


async def _stats(repo: Repository) -> dict[str, Any]:
    pending = await repo.drafts_with_status("pending_approval")
    approved = await repo.drafts_with_status("approved")
    runs = await kpis.per_run(repo)
    return {
        "pending": len(pending),
        "leads": await repo.count_leads(),
        "approved": len(approved),
        "spend": sum(r["cost_usd"] or 0.0 for r in runs),
    }


def run_form(checks: dict[str, health.Check]) -> None:
    st.subheader("Start a run")
    busy = store().current()
    searches = load_campaign()[0].search_urls
    live_ready = checks["evaboot"].status == "ok"
    with st.form("run"):
        source = st.radio(
            "Leads from",
            ["Recorded export (no Evaboot call)", "Live Sales Navigator search (Evaboot)"],
            index=1 if live_ready else 0,
        )
        if searches:
            url = st.selectbox("Sales Navigator search (saved in Setup → Leads)", searches)
        else:
            url = st.text_input("Sales Navigator search URL (live search only)")
        limit = st.number_input("How many leads", min_value=1, max_value=300, value=DEFAULT_LIMIT)
        demo = st.checkbox(
            "Demo mode: canned AI output, no API cost",
            value=checks["together"].status == "fail",
        )
        submitted = st.form_submit_button("▶ Start run", disabled=busy is not None, type="primary")
    if busy is not None:
        st.caption(f"⏳ {busy.label} is running; start another when it finishes.")
    if not submitted:
        return
    live = source.startswith("Live")
    url = url or ""
    if live and "linkedin.com/sales" not in url:
        st.error("Paste a Sales Navigator search URL (linkedin.com/sales/...) for a live run.")
    elif live and not get_settings().evaboot_api_key:
        st.error("A live run needs your Evaboot API key: Setup → Leads.")
    elif not demo and checks["together"].status == "fail":
        st.error("Real runs need a working TOGETHER_API_KEY. Tick Demo mode to try it without AI.")
    else:
        args, label = run_args(url if live else None, int(limit), demo)
        start_job("run", args, label)


@st.fragment(run_every=2)
def job_panel() -> None:
    job = store().latest()
    if job is None:
        return
    was_running = st.session_state.get("job_running") == job.id
    st.session_state["job_running"] = job.id if job.running else None
    if was_running and not job.running:
        st.rerun()  # refresh the counters and the review queue
    _job_status(job)


def _job_status(job: Job) -> None:
    if job.running:
        secs = int((datetime.now(UTC) - job.started_at).total_seconds())
        st.info(f"⏳ **{job.label}**: running for {secs}s")
        if (p := job.progress()) is not None and p[1]:
            st.progress(p[0] / p[1], text=f"{p[0]} of {p[1]} leads done")
        with st.expander("Live log"):
            st.code(job.log_tail(25) or "starting…", language=None)
        return
    when = f"{job.finished_at:%H:%M} UTC" if job.finished_at else ""
    if job.state == "done":
        st.success(f"**{job.label}** finished {when}")
        st.code(job.summary(), language=None)
    else:
        st.error(f"**{job.label}** {job.state} {when}")
        with st.expander("Log", expanded=True):
            st.code(job.log_tail(40) or "no output", language=None)


def setup_panel(checks: list[health.Check]) -> None:
    st.subheader("Setup")
    s = get_settings()
    for c in checks:
        st.markdown(f"{ICON[c.status]} **{c.name}**  \n{c.detail}")
        if c.fix and c.status != "ok":
            st.caption(f"Fix: {c.fix}")
    if any(c.status == "fail" for c in checks):
        st.warning("Open the **Setup** page (sidebar) to finish setup step by step.")
    b1, b2, b3 = st.columns(3)
    if b1.button("Re-check"):
        setup_checks.clear()
        st.rerun()
    by_key = {c.key: c for c in checks}
    if by_key["langfuse"].action == "upload_prompts" and b2.button("Upload prompts"):
        from sdr_loop.prompts import prompts_to_upload, push_local_prompts

        st.toast("Uploaded: " + ", ".join(push_local_prompts(prompts_to_upload())))
        setup_checks.clear()
        st.rerun()
    if by_key["telegram"].status == "ok" and b3.button("Test Telegram"):

        async def go() -> None:
            async with httpx.AsyncClient(timeout=15) as client:
                await send_test(client, s.telegram_bot_token, s.telegram_chat_id)

        try:
            asyncio.run(go())
            st.toast("Test message sent: check your phone.")
        except TelegramSetupError as e:
            st.error(str(e))
    if s.langfuse_public_key:
        st.markdown(f"[Open Langfuse traces ↗]({s.langfuse_host.rstrip('/')})")


# ---------------------------------------------------------------------- Review


def review_page() -> None:
    st.header("Review queue")

    async def pending(repo: Repository) -> list[dict[str, Any]]:
        out = []
        for d in await repo.drafts_with_status("pending_approval"):
            lead = await repo.get_lead(d.lead_id)
            out.append({"draft": d, "lead": lead, "facts": await repo.get_facts(d.lead_id)})
        return out

    items = run(pending)
    if not items:
        st.success("Nothing waiting for review. Start a run from Home.")
        return
    labels = {
        i["draft"].id: f"#{i['draft'].id} · {i['lead'].name} @ {i['lead'].company}"
        + (" ⚠️" if i["draft"].flagged else "")
        for i in items
    }
    chosen = st.sidebar.radio("Draft", list(labels), format_func=labels.get)
    item = next(i for i in items if i["draft"].id == chosen)
    d, lead, facts = item["draft"], item["lead"], item["facts"]
    seq = Sequence(email_1=Email(**d.email_1), email_2=Email(**d.email_2))

    left, right = st.columns([3, 2])
    with left:
        st.subheader(f"{lead.name}, {lead.title} @ {lead.company}")
        if d.flagged:
            st.warning(f"Flagged: {d.flag_reason}")
        text = st.text_area("Draft (edit freely; keep `---` between the two emails)",
                            ap.sequence_text(seq), height=380, key=f"text-{d.id}")  # fmt: skip
        reason = st.selectbox("Reject reason", ap.REJECT_REASONS, key=f"reason-{d.id}")
        c1, c2, c3 = st.columns(3)

        def controller(repo: Repository) -> BotController:
            return BotController(repo, get_settings())

        try:
            if c1.button("✅ Approve as written", key=f"a-{d.id}"):
                run(lambda r: controller(r).decide(d.id, "approve"))
                st.rerun()
            if c2.button("✏️ Save my edit", key=f"e-{d.id}"):
                run(lambda r: controller(r).decide(d.id, "edit", new_text=text))
                st.rerun()
            if c3.button("🗑 Reject", key=f"r-{d.id}"):
                run(lambda r: controller(r).decide(d.id, "reject", reason=reason))
                st.rerun()
        except ap.EditFormatError as e:
            st.error(str(e))
    with right:
        st.subheader("Cited sources")
        cited = set(d.cited_fact_ids or [])
        for f in facts:
            badge = "✅ cited" if f.id in cited else "not cited"
            st.markdown(f"**[{f.id}]** {badge}  \n> {f.snippet}  \n{f.source_url}")
        crit = (d.critic_scores or {}).get("scores")
        if crit:
            st.subheader("Critic")
            st.json({k: crit[k] for k in ("faithfulness", "personalisation", "clarity", "tone")})
            for k, v in (crit.get("reasons") or {}).items():
                st.caption(f"{k}: {v}")
        code = (d.critic_scores or {}).get("code") or {}
        if code.get("failures"):
            st.error("Code checks: " + "; ".join(code["failures"]))


# ---------------------------------------------------------------------- Sent emails


def sent_page() -> None:
    st.header("Sent emails")
    s = get_settings()
    if s.email_mode != "mailpit":
        st.info("Email delivery is off: approved emails only go to the outbox file. "
                "Turn it on in Setup → Email delivery.")  # fmt: skip
    else:
        st.caption(
            f"Approved emails go to the demo inbox (Mailpit), never to the real address. "
            f"Email 2 follows {s.followup_delay_minutes:g} min after email 1."
        )
        a, b, c = st.columns(3)
        a.link_button("Open the inbox ↗", mailer.inbox_url(s))
        if b.button("Send due emails now"):
            st.toast(f"Sent {len(run(lambda r: dispatch_due(r, s)))} email(s).")
        if c.button("Send follow-ups now (skip the wait)"):
            later = datetime.now(UTC) + timedelta(days=3650)
            st.toast(f"Sent {len(run(lambda r: dispatch_due(r, s, now=later)))} email(s).")
    rows = run(lambda r: r.list_sends())
    if not rows:
        st.info("Nothing sent yet. Approve a draft in the Review queue.")
        return
    st.dataframe(
        [
            {"draft": r.draft_id, "email": r.step, "to": r.to_email, "subject": r.subject,
             "status": r.status, "due": r.due_at, "sent": r.sent_at, "error": r.error}
            for r in rows
        ],
        use_container_width=True,
    )  # fmt: skip


# ---------------------------------------------------------------------- Learning


def learning_page() -> None:
    st.header("Learning")
    s = get_settings()
    st.markdown(
        "Each edit and rejection becomes an eval case. A learning cycle writes a challenger "
        "writer prompt and replays it against the current champion. The challenger is promoted "
        "only if it scores higher **and** makes zero invalid citations. "
        f"With `sdr-loop start` running, this happens every night at {s.learn_at}."
    )
    busy = store().current()
    if st.button("🧠 Run a learning cycle now", disabled=busy is not None, type="primary"):
        start_job("learn", ["learn"], "Learning cycle")
    job_panel()
    st.subheader("Promotion history")
    rows = run(kpis.promotions)
    st.dataframe(rows, use_container_width=True) if rows else st.info("No learning cycles yet.")


# ---------------------------------------------------------------------- KPIs


def kpi_page() -> None:
    st.header("KPIs")
    st.caption(
        "Per writer prompt version; promoted versions should lift approval and cut edit distance."
    )
    st.subheader("Per prompt version")
    st.dataframe(run(kpis.per_prompt_version), use_container_width=True)
    st.subheader("Per run (cost per qualified lead; target under $0.05)")
    st.dataframe(run(kpis.per_run), use_container_width=True)


def main() -> None:
    st.set_page_config(page_title="SDR loop", layout="wide")
    get_settings.cache_clear()  # pick up .env edits without restarting
    _init()
    checks = setup_checks()
    needs_setup = any(c.status == "fail" for c in checks)
    page = st.sidebar.radio("Page", PAGES, index=0 if needs_setup else 1)
    if page == "Setup":
        setup_page(checks, setup_checks.clear)
        return
    {"Home": home_page, "Review queue": review_page, "Sent emails": sent_page,
     "Learning": learning_page, "KPIs": kpi_page}[page]()  # fmt: skip


main()
