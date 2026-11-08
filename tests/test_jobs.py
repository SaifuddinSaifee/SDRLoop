"""Background jobs: one at a time, detached, exit code and summary recorded, announced once."""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

from sdr_loop.envfile import set_env_value
from sdr_loop.jobs import Job, JobBusy, JobStore, run_args


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def wait(job: Job, timeout: float = 30) -> Job:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        j = Job.load(job.path)
        if j.finished_at is not None:
            return j
        time.sleep(0.1)
    raise AssertionError(f"job did not finish: {Job.load(job.path).log_tail()}")


def test_job_runs_detached_and_records_report_summary(tmp_path: Path) -> None:
    report = "INFO:x:noise\\nRun 7: completed\\n  leads:   pending_approval=2\\nINFO:langfuse:flush"
    store = JobStore(tmp_path, cli=py(f"print('{report}')"))
    job = wait(store.start("run", [], label="Run on recorded export", source="web"))
    assert job.state == "done" and job.exit_code == 0
    assert job.summary() == "Run 7: completed\n  leads:   pending_approval=2"  # log noise dropped


def test_failed_job_reports_exit_code_and_last_line(tmp_path: Path) -> None:
    store = JobStore(tmp_path, cli=py("import sys; print('boom: no key'); sys.exit(3)"))
    job = wait(store.start("run", [], label="r", source="web"))
    assert job.state == "failed"
    assert job.summary() == "failed (exit 3): boom: no key"


def test_only_one_job_at_a_time(tmp_path: Path) -> None:
    store = JobStore(tmp_path, cli=py("import time; time.sleep(2)"))
    first = store.start("run", [], label="slow run", source="web")
    with pytest.raises(JobBusy, match="slow run is still running"):
        store.start("learn", [], label="learn", source="telegram")
    assert store.current() is not None and store.current().id == first.id  # type: ignore[union-attr]
    wait(first)
    assert store.current() is None
    wait(store.start("learn", [], label="learn", source="telegram"))  # free again


def test_unannounced_lists_finished_jobs_once(tmp_path: Path) -> None:
    store = JobStore(tmp_path, cli=py("print('learn: skipped (3 new cases)')"))
    wait(store.start("learn", [], label="l", source="schedule"))
    [job] = store.unannounced()
    store.mark_announced(job)
    assert store.unannounced() == []


def test_progress_parsed_from_pipeline_log_lines(tmp_path: Path) -> None:
    code = "print('INFO:p:run 1: processing 4 leads'); print('INFO:p:lead 3/4 done: lead 9 x')"
    store = JobStore(tmp_path, cli=py(code))
    assert wait(store.start("run", [], label="r", source="web")).progress() == (3, 4)


def test_runner_killed_before_finishing_is_reported_as_died(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    store.dir.mkdir(parents=True)
    from datetime import UTC, datetime

    job = Job(
        "x", "run", "r", [], "web", datetime.now(UTC), pid=2**22 + 12345, path=store.dir / "x.json"
    )
    job.save()
    assert Job.load(job.path).state == "died" and store.current() is None


def test_run_args() -> None:
    assert run_args(None, 5) == (
        ["run", "--fixture", "--limit", "5"],
        "Run on recorded export (5 leads)",
    )
    args, label = run_args("https://www.linkedin.com/sales/search/people?x=1", 20, fake_llm=True)
    assert args == [
        "run",
        "https://www.linkedin.com/sales/search/people?x=1",
        "--limit",
        "20",
        "--fake-llm",
    ]
    assert label == "Run on Sales Navigator search (20 leads) [demo]"


def test_real_pipeline_job_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The actual `sdr-loop run` in a detached job: drafts land in the DB, progress is logged."""
    db = tmp_path / "jobs.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    monkeypatch.setenv("OUTBOX_PATH", str(tmp_path / "outbox.jsonl"))
    args, label = run_args(None, 3, fake_llm=True)
    job = wait(JobStore(tmp_path).start("run", args, label=label, source="web"), timeout=120)
    assert job.state == "done", job.log_tail()
    assert job.summary().startswith("Run 1: completed")
    assert job.progress() == (3, 3)
    n = sqlite3.connect(db).execute("select count(*) from drafts").fetchone()[0]
    assert n >= 1


def test_set_env_value_replaces_in_place_and_appends(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text('# comment\nTELEGRAM_BOT_TOKEN="abc"\nTELEGRAM_CHAT_ID=\nOTHER=1\n')
    set_env_value("TELEGRAM_CHAT_ID", "123", env)
    set_env_value("NEW_KEY", "x", env)
    assert env.read_text() == (
        '# comment\nTELEGRAM_BOT_TOKEN="abc"\nTELEGRAM_CHAT_ID=123\nOTHER=1\nNEW_KEY=x\n'
    )
    with pytest.raises(ValueError):
        set_env_value("BAD", "two\nlines", env)
