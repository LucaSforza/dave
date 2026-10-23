"""Lease-protected, revision-CAS cluster reconciliation."""
import logging
import re
import threading
import time
from datetime import datetime, timezone

from .models import AllocationRecord, JobState

log = logging.getLogger(__name__)
TERMINAL = {JobState.COMPLETED, JobState.CANCELLED, JobState.FAILED, JobState.NEEDS_ATTENTION}
SCHED_TERMINAL = {"COMPLETED", "CANCELLED", "FAILED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY", "PREEMPTED", "BOOT_FAIL", "DEADLINE", "REVOKED"}


class LeaseLost(RuntimeError):
    pass


class ClusterController:
    def __init__(self, store, transport, settings):
        self.store, self.transport, self.settings = store, transport, settings
        self._lease_lost = threading.Event()

    def tick(self):
        token = self.store.acquire_lock()
        if token is None:
            return
        self._lease_lost.clear()
        interval = max(0.1, min(float(self.settings.worker_lock_seconds) / 3, 5.0))
        stop = threading.Event()

        def heartbeat():
            while not stop.wait(interval):
                try:
                    if not self.store.renew_lock(token):
                        self._lease_lost.set()
                        return
                except Exception:
                    log.exception("Controller lease renewal failed")
                    self._lease_lost.set()
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            # Oldest-first bounded batch avoids permanent starvation by a busy newest page.
            active = [record for record in reversed(self.store.list()) if record.state not in TERMINAL]
            allocated = [record for record in active if record.allocations and record.allocations[-1].state != "NOT_SUBMITTED"]
            queued = [record for record in active if record not in allocated]
            # Reconcile existing allocations first. At most one new submission
            # per pass; the remote account-wide quota remains authoritative.
            for record in allocated[:31] + queued[:1]:
                self._assert_lease()
                latest = self.store.get(str(record.id))
                if latest:
                    try:
                        self.reconcile(latest, token)
                    except LeaseLost:
                        break
                    except Exception as exc:
                        log.exception("Reconciliation failed for %s", record.id)
                        self._record_recoverable_error(record.id, token, str(exc))
        finally:
            stop.set()
            thread.join(timeout=1)
            if not self._lease_lost.is_set():
                self.store.release_lock(token)

    def _assert_lease(self):
        if self._lease_lost.is_set():
            raise LeaseLost("Controller lease lost")

    def _save(self, record, token):
        self._assert_lease()
        record.updated_at = datetime.now(timezone.utc)
        if not self.store.save(record, expected_revision=record.revision, lock_token=token):
            raise LeaseLost("Record revision changed or controller fencing token expired")

    def _record_recoverable_error(self, job_id, token, error):
        if self._lease_lost.is_set():
            return
        try:
            record = self.store.get(str(job_id))
            if record and record.state not in TERMINAL:
                record.reconciliation_error = error[:1000]
                record.last_reconciled_at = datetime.now(timezone.utc)
                self._save(record, token)
        except Exception:
            log.exception("Could not persist reconciliation failure for %s", job_id)

    def reconcile(self, record, token=None):
        # Direct calls remain useful in unit tests; production always supplies the lease token.
        token = token or ""
        self._assert_lease()
        record = self.store.get(str(record.id)) or record
        if record.state in TERMINAL:
            return
        if record.cancel_requested:
            allocation = record.allocations[-1] if record.allocations else None
            if allocation is None or allocation.state in SCHED_TERMINAL | {"NOT_SUBMITTED"}:
                record.state = JobState.CANCELLED
                self._save(record, token)
                return
            payload = {"job_id": str(record.id), "allocation_index": allocation.index}
            # Inspect even if the submission reply was lost: it may have created
            # a job whose numeric id never reached Redis.
            result = self.transport.call("inspect", payload)
            self._assert_lease()
            state = str(result.get("scheduler_state", "UNKNOWN")).upper()
            if result.get("slurm_job_id"):
                allocation.slurm_job_id = str(result["slurm_job_id"])
            if state in SCHED_TERMINAL | {"NOT_SUBMITTED"}:
                allocation.state = state
                allocation.finished_at = datetime.now(timezone.utc)
                record.state = JobState.CANCELLED
                record.runtime["ready"] = False
            elif state == "UNKNOWN":
                record.reconciliation_error = "Cancellation awaiting authoritative Slurm state"
            else:
                allocation.state = state
                self.transport.call("cancel", payload)
                self._assert_lease()
                record.reconciliation_error = None
            record.last_reconciled_at = datetime.now(timezone.utc)
            self._save(record, token)
            return

        if not record.allocations:
            record.allocations.append(AllocationRecord(index=1, submission_token=record.submission_token))
        allocation = record.allocations[-1]
        if allocation.state == "NOT_SUBMITTED":
            # Persist intent before calling: after a lost response inspect by deterministic token;
            # never infer that the remote submission did not happen from an SSH exception.
            allocation.state = "SUBMISSION_UNKNOWN"
            record.state = JobState.SUBMITTING
            self._save(record, token)
            self._assert_lease()
            latest = self.store.get(str(record.id))
            if latest is None or latest.cancel_requested or latest.revision != record.revision:
                # API cancellation may have won after submission intent was
                # persisted. Leave UNKNOWN to inspect on the next pass.
                return
            result = self.transport.call("submit", {"job_id": str(record.id), "allocation_index": allocation.index,
                "request": record.request.model_dump(mode="json"), "checkpoint": record.checkpoint,
                "submission_token": allocation.submission_token})
            self._assert_lease()
            if result.get("status") == "busy":
                allocation.state = "NOT_SUBMITTED"
                record.state = JobState.QUEUED
            elif result.get("status") == "submitted" and str(result.get("slurm_job_id", "")).isdigit():
                allocation.slurm_job_id = str(result["slurm_job_id"])
                allocation.state = "PENDING"
                allocation.submitted_at = datetime.now(timezone.utc)
                record.state = JobState.PENDING
            else:
                record.reconciliation_error = "Invalid submit receipt; inspect required"
            self._save(record, token)
            return

        self._assert_lease()
        result = self.transport.call("inspect", {"job_id": str(record.id), "allocation_index": allocation.index})
        self._assert_lease()
        state = str(result.get("scheduler_state", "UNKNOWN")).upper()
        runtime = result.get("runtime") or {}
        record.runtime = {
            "ready": state == "RUNNING" and runtime.get("ready") is True,
            "rollover": runtime.get("rollover") is True,
            "control_results": runtime.get("control_results") or {},
        }
        if result.get("slurm_job_id") and str(result["slurm_job_id"]).isdigit():
            allocation.slurm_job_id = str(result["slurm_job_id"])
        if state == "NOT_SUBMITTED":
            # Safe only because inspect authoritatively found no receipt/job; submit uses stable token.
            allocation.state = "NOT_SUBMITTED"
            record.state = JobState.QUEUED
        elif state in {"UNKNOWN", ""}:
            record.reconciliation_error = "Scheduler accounting unavailable; state retained for retry"
        elif state in {"PENDING", "CONFIGURING", "REQUEUED"}:
            allocation.state, record.state = state, JobState.PENDING
        elif state in {"RUNNING", "COMPLETING"}:
            allocation.state = state
            record.state = JobState.RUNNING if runtime.get("ready") else JobState.PENDING
        elif state in SCHED_TERMINAL:
            allocation.state = state
            allocation.finished_at = allocation.finished_at or datetime.now(timezone.utc)
            checkpoint = runtime.get("checkpoint")
            if state == "COMPLETED" and runtime.get("rollover") is True and self._valid_checkpoint(checkpoint, record, allocation):
                record.checkpoint = checkpoint
                if record.request.auto_resume and allocation.index < record.request.max_allocations:
                    record.state = JobState.WAITING_RESUME
                    record.allocations.append(AllocationRecord(index=allocation.index + 1,
                        submission_token=f"{record.id}:{allocation.index + 1}"))
                else:
                    record.state = JobState.COMPLETED
            elif state == "COMPLETED" and (runtime.get("rollover") or runtime.get("error")):
                record.state = JobState.NEEDS_ATTENTION
                record.error = runtime.get("error") or "Rollover checkpoint missing or incompatible; automatic resume blocked"
            elif state == "COMPLETED":
                record.state = JobState.COMPLETED
            elif state == "CANCELLED":
                record.state = JobState.CANCELLED
            else:
                record.state = JobState.NEEDS_ATTENTION
                record.error = runtime.get("error") or f"Allocation ended in {state}"
        else:
            record.reconciliation_error = f"Unrecognized scheduler state: {state[:80]}"
        if state in SCHED_TERMINAL | {"NOT_SUBMITTED", "PENDING", "CONFIGURING", "REQUEUED", "RUNNING", "COMPLETING"}:
            record.reconciliation_error = None
        record.last_reconciled_at = datetime.now(timezone.utc)
        self._save(record, token)

    @staticmethod
    def _valid_checkpoint(checkpoint, record, allocation):
        def safe_name(value):
            return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) is not None

        return (isinstance(checkpoint, dict) and checkpoint.get("complete") is True
                and checkpoint.get("job_id") == str(record.id)
                and checkpoint.get("allocation_index") == allocation.index
                and checkpoint.get("scenario") == record.request.scenario
                and checkpoint.get("fidelity") == "explicit_state_only"
                and checkpoint.get("version") == 1 and checkpoint.get("rollover") is True
                and safe_name(checkpoint.get("snapshot")) and safe_name(checkpoint.get("manifest_path"))
                and isinstance(checkpoint.get("sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", checkpoint["sha256"]) is not None)
