import pytest

fakeredis = pytest.importorskip("fakeredis")
from dave_backend.models import JobCreate, JobRecord, JobState
from dave_backend.store import RedisStore


def new_record():
    from uuid import uuid4
    return JobRecord(id=uuid4(), request=JobCreate(), submission_token="stable")


@pytest.fixture
def store():
    return RedisStore("redis://unused", client=fakeredis.FakeRedis(decode_responses=True), lock_seconds=3)


def test_create_persist_list_and_duplicate_safe(store):
    rec = new_record()
    assert store.create(rec)
    assert store.get(str(rec.id)) == rec
    assert store.list()[0].id == rec.id
    assert not store.create(rec)


def test_atomic_idempotency_same_key_and_conflicting_request(store):
    first = new_record()
    same, created = store.create_idempotent(first, "request-1")
    assert created and same.id == first.id
    second = new_record()
    replay, created = store.create_idempotent(second, "request-1")
    assert not created and replay.id == first.id


def test_revision_cas_and_cancellation_wins_stale_save(store):
    rec = new_record()
    store.create(rec)
    stale = store.get(str(rec.id))
    assert store.request_cancel(str(rec.id)).cancel_requested
    stale.state = JobState.RUNNING
    assert not store.save(stale, expected_revision=stale.revision)
    live = store.get(str(rec.id))
    assert live.cancel_requested
    assert live.state == JobState.CANCELLED


def test_lock_renew_cannot_resurrect_or_steal(store):
    assert store.acquire_lock("one") == "one"
    assert not store.acquire_lock("two")
    assert store.renew_lock("one")
    store.client.delete("dave:controller:lock")
    assert not store.renew_lock("one")
    assert store.acquire_lock("two") == "two"
    assert not store.save(new_record(), expected_revision=0, lock_token="one")


def test_fenced_save_requires_live_owner(store):
    rec = new_record(); store.create(rec)
    assert store.acquire_lock("owner") == "owner"
    rec.state = JobState.PENDING
    assert store.save(rec, expected_revision=0, lock_token="owner")
    assert rec.revision == 1
    stale = store.get(str(rec.id))
    stale.state = JobState.COMPLETED
    assert not store.save(stale, expected_revision=0, lock_token="owner")
