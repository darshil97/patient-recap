"""
Insight jobs: accept, run, report.

The chain is three sequential model calls, around a minute in total, which is
past common proxy timeouts. So the API records a job, returns 202, and the caller
polls. Jobs live in Postgres rather than memory so they survive a restart and
stay queryable.

Jobs execute in-process on a worker thread. That is deliberate for now: no extra
infrastructure, and a single practice generates a handful of jobs a day. If a
process dies mid-job the row would be stuck in 'running', so reap_orphans() is
called at startup to fail anything a previous process abandoned.

TENANCY ON THE ASYNC PATH

`enqueue` and `get_job` are called from a request and take the caller's scope.
`run_job` is not: it runs later, on a background task, holding nothing but a job
id. Rather than let it query unscoped, it resolves the job's owning doctor from
the database first — `jobs_repo.owner_doctor_id` — and then does every read and
write under that scope. So there is no code path in the application that touches
patient data without naming a tenant, including the one that runs after the
response has already been sent.
"""

from __future__ import annotations

import logging
import os
import socket
import uuid
from typing import Any, Dict, List, Optional

from app.core.errors import AppError, ConflictError, NotFoundError, QuotaExhaustedError
from app.db.models import JobRow
from app.db.pool import connection
from app.db.repositories import jobs as jobs_repo
from app.db.repositories import visits as visits_repo
from app.db.tenancy import DoctorScope
from app.services import insight_service

logger = logging.getLogger(__name__)

# Identifies this process so orphaned jobs from other processes are detectable.
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def reap_orphans() -> int:
    """Fail jobs left mid-flight by a previous process. Call at startup.

    Deliberately cross-doctor: startup has no request and therefore no doctor, and
    reaping only one doctor's abandoned jobs would leave every other doctor's
    stuck in 'running' with a UI polling them forever. See jobs_repo.fail_orphans.
    """
    with connection() as conn:
        count = jobs_repo.fail_orphans(conn, WORKER_ID)
    if count:
        logger.warning("Failed %d orphaned insight job(s) from a previous run", count)
    return count


def enqueue(
    scope: DoctorScope,
    visit_id: str,
    stop_after: int = 3,
    regenerate: bool = False,
    model: Optional[str] = None,
) -> JobRow:
    """Validate the request and record a queued job for the scoped doctor.

    Everything that can be checked cheaply is checked here, so the caller gets a
    real error instead of a job that fails a second later: unknown visit, no
    transcript attached, insights already present, or a run already in flight.

    A visit belonging to another doctor is a 404 from the first check, and the
    INSERT itself is scoped too, so a job cannot be queued against it even if this
    ordering ever changed.
    """
    with connection() as conn:
        visit = visits_repo.get(conn, scope, visit_id)
        if visit is None:
            raise NotFoundError(f"No visit with id {visit_id}.")

        existing = jobs_repo.active_for_visit(conn, scope, visit_id)
        if existing:
            raise ConflictError(
                f"A job is already {existing.status} for this visit.",
                details={"job_id": existing.job_id, "status": existing.status},
            )

        if visit.insights_generated and not regenerate:
            raise ConflictError(
                f"Visit {visit_id} already has insights. "
                f"Pass regenerate=true to rebuild them.",
                details={"visit_id": visit_id},
            )

        # Fails fast on a missing or out-of-bounds transcript path.
        insight_service.load_visit_transcript(visit)

        job = jobs_repo.create(conn, scope, visit_id=visit_id,
                               stop_after=stop_after, regenerate=regenerate,
                               model_name=model)
        if job is None:
            # The INSERT selects its visit under the doctor filter, so this is the
            # same "not yours" answer as above, reached through the write.
            raise NotFoundError(f"No visit with id {visit_id}.")

    logger.info("Queued job %s for visit %s", job.job_id, visit_id)
    return job


def run_job(job_id: str) -> None:
    """Execute a queued job. Runs on a worker thread; never raises.

    Every outcome is recorded on the job row, because the caller is polling and
    an exception escaping here would leave them waiting forever.

    The first thing this does is establish whose job it is. There is no request
    behind a background task, so the owning doctor is resolved from the job's
    visit and then used for every subsequent query — the job row itself is re-read
    under that scope, not the one it was found with.
    """
    with connection() as conn:
        owner = jobs_repo.owner_doctor_id(conn, job_id)
        if owner is None:
            # No visit, or no patient behind it. Nothing to attribute the work to,
            # so there is no scope under which it would be safe to run.
            logger.error("Job %s has no resolvable owning doctor; not running",
                         job_id)
            jobs_repo.mark_failed(conn, job_id, "not_found",
                                  "The visit behind this job no longer exists.")
            return

        job = jobs_repo.get(conn, owner, job_id)
        if job is None:
            logger.error("Job %s vanished before it could run", job_id)
            return
        if job.status != "queued":
            logger.warning("Job %s is %s, not queued; skipping", job_id, job.status)
            return

        visit = visits_repo.get(conn, owner, job.visit_id)
        if visit is None:
            jobs_repo.mark_failed(conn, job_id, "not_found", "Visit no longer exists.")
            return

        jobs_repo.mark_running(conn, job_id, WORKER_ID)

        def on_event(event: Dict[str, Any]) -> None:
            if event.get("event") == "layer_done":
                jobs_repo.record_layer(conn, job_id, int(event["number"]))

        try:
            outcome = insight_service.generate_for_visit(
                conn, owner, visit,
                model=job.model_name,
                stop_after=job.stop_after,
                regenerate=job.regenerate,
                progress=on_event,
            )
            jobs_repo.mark_succeeded(conn, job_id, outcome.saved.insight_id,
                                     outcome.total_tokens)
            logger.info("Job %s succeeded (%s)", job_id, outcome.saved.verb)

        except QuotaExhaustedError as exc:
            # Its own status: worth retrying unchanged once quota resets.
            jobs_repo.mark_failed(conn, job_id, exc.code, exc.message, quota=True)
            logger.warning("Job %s hit quota exhaustion", job_id)

        except AppError as exc:
            jobs_repo.mark_failed(conn, job_id, exc.code, exc.message)
            logger.warning("Job %s failed: %s", job_id, exc.message)

        except Exception as exc:  # pragma: no cover - unexpected
            jobs_repo.mark_failed(conn, job_id, "internal_error", str(exc))
            logger.exception("Job %s failed unexpectedly", job_id)


def list_jobs(conn, scope: DoctorScope, limit: int = 50) -> List[JobRow]:
    """Recent jobs for one doctor.

    No total: this is a fixed-size tail for diagnostics, not a paged list, and a
    count over every job a practice has ever run answers a question nobody asked.
    """
    return jobs_repo.list_recent(conn, scope, limit=limit)


def get_job(scope: DoctorScope, job_id: str) -> JobRow:
    """Poll one job.

    Scoped, and worth saying why explicitly: `GET /jobs/{job_id}` has no patient
    or visit anywhere in its path, so nothing about the route suggests it returns
    tenant data — and the frontend polls it every 2.5 seconds while a brief
    generates. Unscoped it reported another doctor's progress, error messages and
    token spend. A job id from another doctor now 404s.
    """
    with connection() as conn:
        job = jobs_repo.get(conn, scope, job_id)
    if job is None:
        raise NotFoundError(f"No job with id {job_id}.")
    return job
