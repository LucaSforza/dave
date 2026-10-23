from uuid import uuid4

from dave_backend.controller import ClusterController
from dave_backend.models import AllocationRecord, JobCreate, JobRecord, JobState


class Settings:
    worker_lock_seconds = 3


class Store:
    def __init__(self, record): self.record = record; self.lock = None; self.saves = 0
    def acquire_lock(self):
        if self.lock: return None
        self.lock = "lease"; return self.lock
    def renew_lock(self, token): return self.lock == token
    def release_lock(self, token):
        if self.lock == token: self.lock = None
    def list(self): return [self.record.model_copy(deep=True)]
    def get(self, job_id): return self.record.model_copy(deep=True)
    def save(self, record, expected_revision=None, lock_token=None):
        if expected_revision != self.record.revision or (lock_token and lock_token != self.lock): return False
        record.revision += 1; self.record = record.model_copy(deep=True); self.saves += 1; return True


class Transport:
    def __init__(self, responses): self.responses = list(responses); self.calls = []
    def call(self, op, payload):
        self.calls.append((op, payload))
        item = self.responses.pop(0)
        if isinstance(item, Exception): raise item
        return item


def create(**kw):
    record_fields = {k: kw.pop(k) for k in list(kw) if k in {"state", "allocations", "cancel_requested", "checkpoint", "error"}}
    return JobRecord(id=uuid4(), request=JobCreate(**kw), submission_token="token", **record_fields)


def checkpoint(record, index=1):
    return {"complete": True, "job_id": str(record.id), "allocation_index": index,
            "scenario": record.request.scenario, "fidelity": "explicit_state_only", "rollover": True,
            "version": 1, "snapshot": "state.json", "manifest_path": "state.manifest.json", "sha256": "a" * 64}


def test_submit_and_pending_reconcile():
    record = create()
    store, transport = Store(record), Transport([{"status": "submitted", "slurm_job_id": "123"},
                                                   {"scheduler_state": "PENDING", "runtime": {}}])
    ctl = ClusterController(store, transport, Settings())
    ctl.tick()
    assert store.record.state == JobState.PENDING
    assert store.record.allocations[0].slurm_job_id == "123"
    ctl.tick()
    assert store.record.state == JobState.PENDING
    assert [op for op, _ in transport.calls] == ["submit", "inspect"]


def test_ambiguous_submit_is_inspected_not_blindly_retried():
    record = create()
    store = Store(record)
    transport = Transport([TimeoutError("lost submit response"), {"scheduler_state": "PENDING", "runtime": {}}])
    ctl = ClusterController(store, transport, Settings())
    ctl.tick()
    assert store.record.allocations[0].state == "SUBMISSION_UNKNOWN"
    ctl.tick()
    assert [x[0] for x in transport.calls] == ["submit", "inspect"]


def test_busy_external_quota_retries_safely():
    store, transport = Store(create()), Transport([{"status": "busy"}, {"status": "submitted", "slurm_job_id": "22"}])
    ctl = ClusterController(store, transport, Settings())
    ctl.tick()
    assert store.record.state == JobState.QUEUED
    ctl.tick()
    assert store.record.state == JobState.PENDING
    assert len([c for c in transport.calls if c[0] == "submit"]) == 2


def test_complete_rollover_requires_valid_checkpoint_and_respects_budget():
    record = create(max_allocations=2, allocations=[AllocationRecord(index=1, state="RUNNING", slurm_job_id="1", submission_token="a")])
    store = Store(record)
    transport = Transport([{"scheduler_state": "COMPLETED", "runtime": {"rollover": True, "checkpoint": checkpoint(record)}}])
    ClusterController(store, transport, Settings()).tick()
    assert store.record.state == JobState.WAITING_RESUME
    assert len(store.record.allocations) == 2


def test_invalid_or_nonclean_rollover_never_renews():
    record = create(allocations=[AllocationRecord(index=1, state="RUNNING", slurm_job_id="1", submission_token="a")])
    store = Store(record)
    transport = Transport([{"scheduler_state": "TIMEOUT", "runtime": {"rollover": True, "checkpoint": checkpoint(record)}}])
    ClusterController(store, transport, Settings()).tick()
    assert store.record.state == JobState.NEEDS_ATTENTION
    assert len(store.record.allocations) == 1


def test_valid_rollover_at_allocation_budget_completes_without_successor():
    record = create(max_allocations=1,
                    allocations=[AllocationRecord(index=1, state="RUNNING", slurm_job_id="1", submission_token="a")])
    store = Store(record)
    transport = Transport([{"scheduler_state": "COMPLETED", "runtime": {"rollover": True, "checkpoint": checkpoint(record)}}])
    ClusterController(store, transport, Settings()).tick()
    assert store.record.state == JobState.COMPLETED
    assert len(store.record.allocations) == 1


def test_cancelled_allocation_never_resumes_even_with_checkpoint():
    record = create(state=JobState.RUNNING, cancel_requested=True,
                    allocations=[AllocationRecord(index=1, state="RUNNING", slurm_job_id="1", submission_token="a")])
    store = Store(record)
    transport = Transport([{"scheduler_state": "RUNNING", "slurm_job_id": "1"}, {"status": "cancel_requested"}])
    ClusterController(store, transport, Settings()).tick()
    assert store.record.state == JobState.RUNNING
    assert len(store.record.allocations) == 1


def test_unknown_accounting_retries_without_terminalizing():
    record = create(state=JobState.PENDING, allocations=[AllocationRecord(index=1, state="PENDING", slurm_job_id="1", submission_token="a")])
    store = Store(record)
    ctl = ClusterController(store, Transport([{"scheduler_state": "UNKNOWN"}, {"scheduler_state": "RUNNING", "runtime": {"ready": False}}]), Settings())
    ctl.tick()
    assert store.record.state == JobState.PENDING
    assert store.record.reconciliation_error
    ctl.tick()
    assert store.record.state == JobState.PENDING
    assert len(store.record.allocations) == 1


def test_cancel_intent_waits_for_scheduler_confirmation():
    record = create(state=JobState.RUNNING, cancel_requested=True,
                    allocations=[AllocationRecord(index=1, state="RUNNING", slurm_job_id="1", submission_token="a")])
    store = Store(record)
    ctl = ClusterController(store, Transport([{"scheduler_state": "RUNNING", "slurm_job_id": "1"}, {"status": "cancel_requested"}]), Settings())
    ctl.tick()
    assert store.record.cancel_requested
    assert store.record.state == JobState.RUNNING


def test_cancel_is_confirmed_on_subsequent_scheduler_terminal_state():
    record = create(state=JobState.RUNNING, cancel_requested=True,
                    allocations=[AllocationRecord(index=1, state="RUNNING", slurm_job_id="1", submission_token="a")])
    store = Store(record)
    transport = Transport([
        {"scheduler_state": "RUNNING", "slurm_job_id": "1"}, {"status": "cancel_requested"},
        {"scheduler_state": "CANCELLED", "slurm_job_id": "1"},
    ])
    ctl = ClusterController(store, transport, Settings())
    ctl.tick()
    assert store.record.state == JobState.RUNNING
    ctl.tick()
    assert store.record.state == JobState.CANCELLED
    assert [op for op, _ in transport.calls] == ["inspect", "cancel", "inspect"]


def test_cancel_recovers_job_id_after_lost_submission_reply():
    record = create(state=JobState.SUBMITTING, cancel_requested=True,
                    allocations=[AllocationRecord(index=1, state="SUBMISSION_UNKNOWN", submission_token="a")])
    store = Store(record)
    transport = Transport([{"scheduler_state": "PENDING", "slurm_job_id": "55"}, {"status": "cancel_requested"}])
    ClusterController(store, transport, Settings()).tick()
    assert store.record.allocations[0].slurm_job_id == "55"
    assert [op for op, _ in transport.calls] == ["inspect", "cancel"]


def test_invalid_checkpoint_on_completed_rollover_requires_attention():
    record = create(allocations=[AllocationRecord(index=1, state="RUNNING", slurm_job_id="1", submission_token="a")])
    store = Store(record)
    transport = Transport([{"scheduler_state": "COMPLETED", "runtime": {"rollover": True, "checkpoint": {"complete": False}}}])
    ClusterController(store, transport, Settings()).tick()
    assert store.record.state == JobState.NEEDS_ATTENTION
    assert len(store.record.allocations) == 1


def test_terminal_records_are_not_reconciled_or_resubmitted():
    record = create(state=JobState.COMPLETED, allocations=[AllocationRecord(index=1, state="COMPLETED", slurm_job_id="1", submission_token="a")])
    transport = Transport([])
    ClusterController(Store(record), transport, Settings()).tick()
    assert transport.calls == []


def test_lock_loss_stops_before_post_ssh_write():
    record = create()
    store = Store(record)
    class LostTransport:
        def call(self, op, payload):
            store.lock = None
            return {"status": "submitted", "slurm_job_id": "1"}
    ctl = ClusterController(store, LostTransport(), Settings())
    ctl.tick()
    assert store.record.allocations[0].state == "SUBMISSION_UNKNOWN"
    assert store.record.allocations[0].slurm_job_id is None


def test_cancel_between_intent_and_submit_blocks_remote_submission():
    record = create()

    class CancellingStore(Store):
        def save(self, record, **kwargs):
            result = super().save(record, **kwargs)
            if result and record.state == JobState.SUBMITTING:
                self.record.cancel_requested = True
                self.record.revision += 1
            return result

    store = CancellingStore(record)
    transport = Transport([])
    ClusterController(store, transport, Settings()).tick()
    assert transport.calls == []
    assert store.record.cancel_requested
