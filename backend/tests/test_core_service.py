from dave_backend.models import JobCreate, JobRecord
from dave_backend.service import IdempotencyConflict, JobService


class FakeStore:
    def __init__(self): self.records = {}; self.keys = {}
    def get(self, key): return self.records.get(str(key))
    def list(self): return list(self.records.values())
    def create(self, rec):
        if str(rec.id) in self.records: return False
        self.records[str(rec.id)] = rec
        return True
    def create_idempotent(self, rec, key):
        if key in self.keys: return self.records[self.keys[key]], False
        self.create(rec); self.keys[key] = str(rec.id); return rec, True
    def get_idempotent(self, key): return self.records.get(self.keys.get(key))
    def save(self, rec, expected_revision=None, lock_token=None):
        self.records[str(rec.id)] = rec
        return True
    def request_cancel(self, job_id):
        rec = self.get(job_id)
        if rec:
            rec.cancel_requested = True
            self.save(rec)
        return rec


def test_idempotency_and_conflict():
    service = JobService(FakeStore())
    first = service.create_job(JobCreate(), "k")
    assert service.create_job(JobCreate(), "k").id == first.id
    try:
        service.create_job(JobCreate(cpus=2), "k")
    except IdempotencyConflict:
        pass
    else:
        assert False, "expected conflicting idempotency key to fail"
