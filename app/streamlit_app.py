"""Review queue (draft next to its cited sources) and KPI page.

  uv sync --extra ui && uv run streamlit run app/streamlit_app.py

Decisions go through the same BotController path as Telegram, so tracing, the approval-record
guard and the dry-run outbox behave identically.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import streamlit as st

from sdr_loop import kpis
from sdr_loop.bot.controller import BotController
from sdr_loop.config import get_settings
from sdr_loop.memory import Repository
from sdr_loop.models import Email, Sequence
from sdr_loop.observability import init_tracer_from_settings
from sdr_loop.steps import approve as ap


def run[T](fn: Callable[[Repository], Awaitable[T]]) -> T:
    """Fresh engine per call: Streamlit reruns the script, and async engines are loop-bound."""

    async def go() -> T:
        repo = Repository(get_settings().database_url)
        try:
            return await fn(repo)
        finally:
            await repo.close()

    return asyncio.run(go())


@st.cache_resource
def _init() -> None:
    init_tracer_from_settings()


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
        st.success("Nothing waiting for review.")
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
        controller = lambda repo: BotController(repo, get_settings())  # noqa: E731
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


def kpi_page() -> None:
    st.header("KPIs")
    st.caption(
        "Per writer prompt version; promoted versions should lift approval and cut edit distance."
    )
    st.subheader("Per prompt version")
    st.dataframe(run(kpis.per_prompt_version), use_container_width=True)
    st.subheader("Per run (cost per qualified lead; target under $0.05)")
    st.dataframe(run(kpis.per_run), use_container_width=True)
    st.subheader("Prompt promotions")
    rows = run(kpis.promotions)
    st.dataframe(rows, use_container_width=True) if rows else st.info("No learning cycles yet.")


def main() -> None:
    st.set_page_config(page_title="SDR loop", layout="wide")
    _init()
    page = st.sidebar.radio("Page", ["Review queue", "KPIs"])
    (review_page if page == "Review queue" else kpi_page)()


main()
