"""Background jobs: a pipeline run or a learning cycle started from the web app, Telegram or the
nightly schedule, without anyone typing a command.

A job is one `sdr-loop` CLI invocation in a detached process, so the web app and the bot stay
responsive and a job survives a page reload. One job at a time: two runs would compete for the same
budget and leads. State is a JSON file per job plus its log, under `STATE_DIR/jobs/`:

    python -m sdr_loop.jobs <job.json>   # the runner: executes the command, records the exit code
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

JobKind = Literal["run", "learn"]
CLI = [sys.executable, "-m", "sdr_loop.cli"]
STARTUP_GRACE_S = 15
_LOG_PREFIXES = ("DEBUG:", "INFO:", "WARNING:", "ERROR:", "CRITICAL:")


class JobBusy(Exception):
    def __init__(self, job: Job) -> None:
        super().__init__(f"{job.label} is still running (started {job.started_at:%H:%M}).")
        self.job = job


@dataclass
class Job:
    id: str
    kind: JobKind
    label: str
    argv: list[str]
    source: str  # "web", "telegram", "schedule"
    started_at: datetime
    pid: int | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    announced: bool = False
    path: Path = field(default=Path(), repr=False)

    @property
    def log_path(self) -> Path:
        return self.path.with_suffix(".log")

    @property
    def running(self) -> bool:
        if self.finished_at is not None:
            return False
        if self.pid is None:  # the runner has not recorded its pid yet
            return (datetime.now(UTC) - self.started_at).total_seconds() < STARTUP_GRACE_S
        return _alive(self.pid)

    @property
    def state(self) -> str:
        if self.running:
            return "running"
        if self.finished_at is None:
            return "died"  # runner killed (reboot, OOM) before it could record an exit code
        return "done" if self.exit_code == 0 else "failed"

    def log_tail(self, lines: int = 40) -> str:
        if not self.log_path.exists():
            return ""
        return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-lines:])

    def progress(self) -> tuple[int, int] | None:
        """(leads done, leads total) from the pipeline's progress lines, once it has started."""
        log = self.log_path.read_text(errors="replace") if self.log_path.exists() else ""
        done = re.findall(r"lead (\d+)/(\d+) done", log)
        if done:
            return int(done[-1][0]), int(done[-1][1])
        total = re.findall(r"processing (\d+) leads", log)
        return (0, int(total[-1])) if total else None

    def summary(self) -> str:
        """The command's own report (the last `Run N:` / `learn:` block), or the error line."""
        log = self.log_path.read_text(errors="replace") if self.log_path.exists() else ""
        lines = [ln for ln in log.splitlines() if ln.strip()]
        marker = "Run " if self.kind == "run" else "learn:"
        starts = [i for i, ln in enumerate(lines) if ln.startswith(marker)]
        if self.state == "done" and starts:
            return "\n".join(ln for ln in lines[starts[-1] :] if not ln.startswith(_LOG_PREFIXES))
        if self.state == "done":
            return "finished."
        last = lines[-1][:300] if lines else "no output"
        return f"{self.state} (exit {self.exit_code}): {last}"

    def save(self) -> None:
        data = asdict(self)
        data.pop("path")
        for k in ("started_at", "finished_at"):
            if data[k] is not None:
                data[k] = data[k].isoformat()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self.path)  # atomic: readers never see half a file

    @classmethod
    def load(cls, path: Path) -> Job:
        data: dict[str, Any] = json.loads(path.read_text())
        for k in ("started_at", "finished_at"):
            if data.get(k):
                data[k] = datetime.fromisoformat(data[k])
        return cls(**data, path=path)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


_children: list[subprocess.Popen[bytes]] = []


def _reap() -> None:
    """Collect finished runner processes we spawned, so they don't linger as zombies."""
    for p in list(_children):
        if p.poll() is not None:
            _children.remove(p)


class JobStore:
    def __init__(self, state_dir: str | Path, cli: list[str] | None = None) -> None:
        self.dir = Path(state_dir) / "jobs"
        self.cli = cli or CLI

    def all(self) -> list[Job]:
        _reap()
        if not self.dir.exists():
            return []
        return sorted((Job.load(p) for p in self.dir.glob("*.json")), key=lambda j: j.started_at)

    def latest(self) -> Job | None:
        jobs = self.all()
        return jobs[-1] if jobs else None

    def current(self) -> Job | None:
        return next((j for j in reversed(self.all()) if j.running), None)

    def start(self, kind: JobKind, args: list[str], *, label: str, source: str) -> Job:
        if (busy := self.current()) is not None:
            raise JobBusy(busy)
        self.dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        job_id = f"{now:%Y%m%d-%H%M%S-%f}-{kind}"
        path = self.dir / f"{job_id}.json"
        job = Job(job_id, kind, label, [*self.cli, *args], source, now, path=path)
        job.save()
        proc = subprocess.Popen(  # noqa: S603  (fixed argv, no shell)
            [sys.executable, "-m", "sdr_loop.jobs", str(job.path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # Ctrl-C on the web app or bot does not kill a running job
        )
        _children.append(proc)
        job.pid = proc.pid  # in memory only: the runner records it on disk (no write race)
        return job

    def unannounced(self) -> list[Job]:
        return [j for j in self.all() if not j.running and not j.announced]

    def mark_announced(self, job: Job) -> None:
        job.announced = True
        job.save()


DEFAULT_LIMIT = 5


def run_args(url: str | None, limit: int | None, fake_llm: bool = False) -> tuple[list[str], str]:
    """CLI args and a human label for a pipeline run (recorded export when no URL is given)."""
    args = ["run", url] if url else ["run", "--fixture"]
    if limit:
        args += ["--limit", str(limit)]
    if fake_llm:
        args.append("--fake-llm")
    source = "Sales Navigator search" if url else "recorded export"
    label = (
        f"Run on {source}"
        + (f" ({limit} leads)" if limit else "")
        + (" [demo]" if fake_llm else "")
    )
    return args, label


def execute(path: Path) -> int:
    """Runner body: run the job's command with output to its log, then record how it ended."""
    job = Job.load(path)
    job.pid = os.getpid()
    job.save()
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    with job.log_path.open("ab") as log:
        rc = subprocess.call(job.argv, stdout=log, stderr=subprocess.STDOUT, env=env)  # noqa: S603
    job = Job.load(path)
    job.exit_code, job.finished_at = rc, datetime.now(UTC)
    job.save()
    return rc


if __name__ == "__main__":
    sys.exit(execute(Path(sys.argv[1])))
