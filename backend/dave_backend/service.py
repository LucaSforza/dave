from datetime import datetime, timezone
from uuid import uuid4

from .models import JobCreate, JobRecord, JobState


class IdempotencyConflict(ValueError):
    """An idempotency key was reused for a different request."""


class JobService:
    def __init__(self, store):
        self.store = store

    def create_job(self, request: JobCreate, idempotency_key: str | None = None) -> JobRecord:
        request = request if isinstance(request, JobCreate) else JobCreate.model_validate(request)
        if idempotency_key:
            # Persist key with a request fingerprint; atomically implemented by RedisStore.
            existing = self.store.get_idempotent(idempotency_key) if hasattr(self.store, "get_idempotent") else None
            if existing:
                if existing.request != request:
                    raise IdempotencyConflict("Idempotency key already used with different request")
                return existing
        rec = JobRecord(id=uuid4(), request=request, submission_token=str(uuid4()))
        if idempotency_key and hasattr(self.store, "create_idempotent"):
            saved, _ = self.store.create_idempotent(rec, idempotency_key)
            if saved.request != request:
                raise IdempotencyConflict("Idempotency key already used with different request")
            return saved
        if not self.store.create(rec):
            raise RuntimeError("Could not persist job")
        return rec

    def get_job(self, job_id):
        return self.store.get(str(job_id))

    def list_jobs(self):
        return self.store.list()

    def cancel_job(self, job_id):
        if hasattr(self.store, "request_cancel"):
            return self.store.request_cancel(str(job_id))
        record = self.get_job(job_id)
        if record is None:
            return None
        record.cancel_requested = True
        record.updated_at = datetime.now(timezone.utc)
        if record.state in (JobState.QUEUED, JobState.WAITING_RESUME):
            record.state = JobState.CANCELLED
        self.store.save(record, expected_revision=record.revision)
        return self.get_job(job_id) or record
