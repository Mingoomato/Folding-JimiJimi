"""Job registry with progress + ETA, so a client can render a loading screen.

A PPTX with OCR takes minutes: structure parsing runs ~18s per slide image. A
plain blocking POST gives the caller nothing to show for that time (and invites
proxy timeouts), so conversions can run as a background job whose progress is
polled.

ETA is **self-correcting rather than hardcoded**: we track how far along the job
is as a 0..1 fraction and extrapolate from time actually spent —
``eta = elapsed / fraction - elapsed``. If a machine is slow, or a deck has
unusually heavy images, the estimate adapts on its own instead of lying with a
fixed per-image constant.

The registry is in-process and in-memory: it is progress for the run happening
right now, not durable history. Finished jobs are pruned after a TTL.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.data_paths import atomic_write_text, state_path

logger = logging.getLogger("doc2md.progress")

# Learned throughput per format, so a job can quote an ETA from second 0 instead
# of showing 0% with no estimate while an opaque library call runs.
_timing_lock = threading.Lock()

# How much of the total wall-clock each phase typically accounts for. Conversion
# dominates because that is where OCR lives; the rest is near-instant bookkeeping.
PHASE_WEIGHTS: dict[str, float] = {
    "convert": 0.90,
    "postprocess": 0.03,
    "metadata": 0.02,
    "frontmatter": 0.01,
    "chunking": 0.04,
}
PHASE_ORDER = list(PHASE_WEIGHTS)

FINISHED_TTL_SECONDS = 900  # keep completed jobs pollable for 15 minutes
MAX_JOBS = 200


def _load_timing() -> dict:
    timing_file = state_path("timing.json")
    if not timing_file.exists():
        return {}
    try:
        return json.loads(timing_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def estimate_seconds(path: str) -> float | None:
    """Predicted duration for this file from past runs of the same format."""
    p = Path(path)
    fmt = p.suffix.lower().lstrip(".")
    try:
        size = p.stat().st_size
    except OSError:
        return None
    stats = _load_timing().get(fmt)
    if not stats or stats.get("bytes", 0) <= 0:
        return None
    rate = stats["bytes"] / stats["seconds"]  # bytes per second
    if rate <= 0:
        return None
    return max(1.0, size / rate)


def record_timing(path: str, seconds: float) -> None:
    """Fold this run into the per-format throughput history."""
    p = Path(path)
    fmt = p.suffix.lower().lstrip(".")
    try:
        size = p.stat().st_size
    except OSError:
        return
    if seconds <= 0 or size <= 0:
        return
    with _timing_lock:
        data = _load_timing()
        entry = data.setdefault(fmt, {"bytes": 0.0, "seconds": 0.0, "runs": 0})
        # decay old samples so the estimate tracks the current machine
        entry["bytes"] = entry["bytes"] * 0.7 + size
        entry["seconds"] = entry["seconds"] * 0.7 + seconds
        entry["runs"] = entry.get("runs", 0) + 1
        try:
            atomic_write_text(state_path("timing.json"), json.dumps(data, indent=2))
        except Exception as e:
            logger.debug("could not persist timing history: %s", e)


@dataclass
class Job:
    job_id: str
    path: str
    status: str = "queued"  # queued | running | done | failed
    phase: str = "queued"
    message: str = ""
    current: int = 0
    total: int = 0
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    error: str | None = None
    result: object | None = None
    cached: bool = False
    baseline_seconds: float | None = None  # predicted duration from past runs

    # ---- derived -----------------------------------------------------------

    @property
    def elapsed_seconds(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0.0, end - self.started_at)

    @property
    def fraction(self) -> float:
        """Overall completion in 0..1, from finished phases plus progress within
        the current one."""
        if self.status == "done":
            return 1.0
        if self.phase not in PHASE_WEIGHTS:
            return 0.0
        done = sum(PHASE_WEIGHTS[p] for p in PHASE_ORDER[: PHASE_ORDER.index(self.phase)])
        within = (self.current / self.total) if self.total > 0 else 0.0
        return min(0.999, done + PHASE_WEIGHTS[self.phase] * within)

    @property
    def percent(self) -> float:
        return round(self.fraction * 100, 1)

    @property
    def eta_seconds(self) -> float | None:
        """Seconds remaining.

        Prefers extrapolating from progress actually made (self-correcting). While
        a phase is still opaque — e.g. a single library call with no sub-progress —
        falls back to the learned per-format baseline so a loading bar has
        something to show instead of sitting at 0% with no estimate.
        """
        if self.status in ("done", "failed"):
            return 0.0
        frac = self.fraction
        if frac > 0.02:
            remaining = self.elapsed_seconds / frac - self.elapsed_seconds
            return round(max(0.0, remaining), 1)
        if self.baseline_seconds:
            return round(max(0.0, self.baseline_seconds - self.elapsed_seconds), 1)
        return None

    @property
    def eta_source(self) -> str:
        if self.status in ("done", "failed"):
            return "final"
        if self.fraction > 0.02:
            return "measured"
        return "baseline" if self.baseline_seconds else "unknown"

    def snapshot(self) -> dict:
        return {
            "job_id": self.job_id,
            "path": self.path,
            "filename": Path(self.path).name,
            "status": self.status,
            "phase": self.phase,
            "message": self.message,
            "current": self.current,
            "total": self.total,
            "percent": self.percent,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "eta_seconds": self.eta_seconds,
            "eta_source": self.eta_source,
            "cached": self.cached,
            "error": self.error,
            "has_result": self.result is not None,
        }


_jobs: dict[str, Job] = {}
_lock = threading.Lock()


def _prune_locked() -> None:
    now = time.time()
    stale = [
        jid
        for jid, j in _jobs.items()
        if j.finished_at is not None and now - j.finished_at > FINISHED_TTL_SECONDS
    ]
    for jid in stale:
        _jobs.pop(jid, None)
    if len(_jobs) > MAX_JOBS:
        # drop the oldest finished jobs first
        finished = sorted(
            (j for j in _jobs.values() if j.finished_at is not None),
            key=lambda j: j.finished_at or 0,
        )
        for j in finished[: len(_jobs) - MAX_JOBS]:
            _jobs.pop(j.job_id, None)


def create(path: str, job_id: str | None = None) -> Job:
    job = Job(
        job_id=job_id or uuid.uuid4().hex,
        path=path,
        baseline_seconds=estimate_seconds(path),
    )
    with _lock:
        _prune_locked()
        _jobs[job.job_id] = job
    return job


def get(job_id: str) -> Job | None:
    with _lock:
        return _jobs.get(job_id)


def list_jobs() -> list[dict]:
    with _lock:
        jobs = list(_jobs.values())
    return [j.snapshot() for j in sorted(jobs, key=lambda j: j.started_at, reverse=True)]


def update(
    job: Job | None,
    *,
    phase: str | None = None,
    message: str | None = None,
    current: int | None = None,
    total: int | None = None,
) -> None:
    """Record progress. Accepts None so callers can stay uninstrumented-safe."""
    if job is None:
        return
    with _lock:
        job.status = "running"
        if phase is not None and phase != job.phase:
            job.phase = phase
            job.current = 0
            job.total = 0
        if total is not None:
            job.total = total
        if current is not None:
            job.current = current
        if message is not None:
            job.message = message
        job.updated_at = time.time()
        snap_pct, snap_eta = job.percent, job.eta_seconds

    eta = f"{snap_eta:.0f}s" if snap_eta is not None else "?"
    logger.info(
        "[%s] %s %.1f%% ETA %s — %s",
        job.job_id[:8],
        job.phase,
        snap_pct,
        eta,
        job.message,
    )


def finish(job: Job | None, result: object, cached: bool = False) -> None:
    if job is None:
        return
    with _lock:
        job.status = "done"
        job.phase = "done"
        job.message = "완료"
        job.result = result
        job.cached = cached
        job.finished_at = time.time()
        job.updated_at = job.finished_at
        elapsed = job.elapsed_seconds
        path = job.path
    # only real conversions teach us anything about throughput
    if not cached:
        record_timing(path, elapsed)
    logger.info("[%s] done in %.1fs", job.job_id[:8], elapsed)


_batches: dict[str, list[str]] = {}


def create_batch(job_ids: list[str], batch_id: str | None = None) -> str:
    bid = batch_id or uuid.uuid4().hex
    with _lock:
        _batches[bid] = list(job_ids)
    return bid


def batch_snapshot(batch_id: str) -> dict | None:
    """Per-file progress plus an overall ETA.

    Jobs run one at a time (a single worker), so the overall estimate is the
    running file's remaining time plus each queued file's predicted duration.
    """
    with _lock:
        job_ids = _batches.get(batch_id)
        jobs = [_jobs[j] for j in job_ids if j in _jobs] if job_ids else None
    if jobs is None:
        return None

    files = [j.snapshot() for j in jobs]
    total = len(jobs)
    done = sum(1 for j in jobs if j.status in ("done", "failed"))

    overall_eta: float | None = 0.0
    for j in jobs:
        if j.status in ("done", "failed"):
            continue
        if j.status == "running":
            part = j.eta_seconds
        else:  # queued — nothing measured yet, lean on the learned baseline
            part = j.baseline_seconds
        if part is None:
            overall_eta = None
            break
        overall_eta += part

    percent = round(sum(j.fraction for j in jobs) / total * 100, 1) if total else 0.0
    return {
        "batch_id": batch_id,
        "total_files": total,
        "done_files": done,
        "percent": percent,
        "eta_seconds": round(overall_eta, 1) if overall_eta is not None else None,
        "files": files,
    }


def fail(job: Job | None, error: str) -> None:
    if job is None:
        return
    with _lock:
        job.status = "failed"
        job.error = error
        job.finished_at = time.time()
        job.updated_at = job.finished_at
    logger.warning("[%s] failed: %s", job.job_id[:8], error)
