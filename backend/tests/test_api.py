from uuid import UUID

from fastapi.testclient import TestClient

from dave_backend.api import create_app
from dave_backend.models import AllocationRecord, JobCreate, JobRecord, JobState
from dave_backend.settings import Settings
from dave_backend.transport import TransportError


class MemoryStore:
    def __init__(self):
        self.records = {}
        self.idempotency = {}

    def create(self, record):
        self.records[str(record.id)] = record
        return True

    def get(self, job_id):
        return self.records.get(str(job_id))

    def list(self):
        return list(self.records.values())

    def save(self, record, **_kwargs):
        self.records[str(record.id)] = record

    def get_idempotent(self, key):
        job_id = self.idempotency.get(key)
        return self.get(job_id) if job_id else None

    def create_idempotent(self, record, key):
        existing = self.get_idempotent(key)
        if existing:
            return existing, False
        self.create(record)
        self.idempotency[key] = str(record.id)
        return record, True


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.responses = {}

    def call(self, operation, payload):
        self.calls.append((operation, payload))
        return self.responses.get(operation, {})


def setup():
    store, transport = MemoryStore(), FakeTransport()
    return TestClient(create_app(store=store, transport=transport, settings=Settings())), store, transport


def make_running(store):
    request = JobCreate()
    record = JobRecord(id=UUID("4678be89-881d-4f29-9ad2-77670cc25d72"), request=request, submission_token="opaque")
    record.allocations.append(AllocationRecord(index=1, submission_token="opaque"))
    record.state = JobState.RUNNING
    store.records[str(record.id)] = record
    return record


def test_health_create_list_read_cancel_and_idempotency():
    client, store, transport = setup()
    assert client.get("/health").json() == {"status": "ok", "redis": "ok"}
    response = client.post("/jobs", headers={"Idempotency-Key": "req-one"}, json={"scenario": "uw_lidar"})
    assert response.status_code == 202
    job_id = response.json()["id"]
    assert client.get("/jobs").json()[0]["id"] == job_id
    assert client.get(f"/jobs/{job_id}").status_code == 200
    assert client.post("/jobs", headers={"Idempotency-Key": "req-one"}, json={"scenario": "uw_lidar"}).json()["id"] == job_id
    assert transport.calls == []  # Creating a job does not contact the cluster.
    assert client.post(f"/jobs/{job_id}/cancel").json()["cancel_requested"] is True
    assert client.get("/jobs/not-a-uuid").status_code == 422
    assert client.get("/jobs/00000000-0000-0000-0000-000000000001").status_code == 404
    assert client.post("/jobs", json={"unknown": "field"}).status_code == 422


def test_cluster_and_bounded_running_job_operations():
    client, store, transport = setup()
    record = make_running(store)
    transport.responses["preflight"] = {"ok": True, "checks": {}, "errors": []}
    transport.responses["logs"] = {"text": "ok", "next_offset": 2}
    transport.responses["artifacts"] = {"artifacts": [{"path": "logs/a.txt", "size": 2}]}
    transport.responses["download"] = {"data_base64": "b2s=", "path": "logs/a.txt"}
    transport.responses["desktop"] = {"ready": True, "node": "cn1", "user": "alice", "port": 6080}
    transport.responses["control"] = {"status": "queued"}

    assert client.get("/cluster").json()["ok"]
    assert client.get(f"/jobs/{record.id}/logs?offset=0&limit=65536").status_code == 200
    assert client.get(f"/jobs/{record.id}/logs?limit=65537").status_code == 422
    assert client.get(f"/jobs/{record.id}/desktop").json()["node"] == "cn1"
    assert client.post(f"/jobs/{record.id}/control", json={"action": "pause"}).status_code == 200
    assert client.post(f"/jobs/{record.id}/control", json={"action": "exec"}).status_code == 422
    assert client.get(f"/jobs/{record.id}/artifacts/../secret/download").status_code in (404, 422)
    response = client.get(f"/jobs/{record.id}/artifacts/logs/a.txt/download")
    assert response.content == b"ok"
    assert all(call[1].get("job_id") == str(record.id) for call in transport.calls if "job_id" in call[1])


def test_desktop_requires_current_running_allocation_and_descriptor_validation():
    client, store, transport = setup()
    record = make_running(store)
    transport.responses["desktop"] = {"ready": False}
    assert client.get(f"/jobs/{record.id}/desktop").status_code == 409
    record.cancel_requested = True
    assert client.get(f"/jobs/{record.id}/desktop").status_code == 409
    assert transport.calls == [("desktop", {"job_id": str(record.id), "allocation_index": 1})]


def test_logs_remain_available_for_terminal_allocations():
    client, store, transport = setup()
    record = make_running(store)
    record.state = JobState.FAILED
    transport.responses["logs"] = {"text": "failed", "next_offset": 6}
    assert client.get(f"/jobs/{record.id}/logs").json()["text"] == "failed"
    assert client.get(f"/jobs/{record.id}/desktop").status_code == 409
    assert client.post(f"/jobs/{record.id}/control", json={"action": "resume"}).status_code == 409


def test_health_reports_redis_readiness_failure_safely():
    from redis.exceptions import ConnectionError as RedisConnectionError

    client, store, _ = setup()

    class UnavailableRedis:
        def ping(self):
            raise RedisConnectionError("private connection details")

    store.client = UnavailableRedis()
    response = client.get("/health")
    assert response.status_code == 503
    assert "private" not in response.text
    assert client.get("/health/live").status_code == 200


def test_redis_errors_from_job_endpoints_map_to_sanitized_503():
    from redis.exceptions import RedisError

    client, store, _ = setup()

    def unavailable(*_args, **_kwargs):
        raise RedisError("secret redis host")

    store.create = unavailable
    store.list = unavailable
    store.get = unavailable
    store.get_idempotent = unavailable
    assert client.post("/jobs", json={}).status_code == 503
    assert client.get("/jobs").status_code == 503
    assert client.get("/jobs/00000000-0000-0000-0000-000000000001").status_code == 503
    assert client.post("/jobs/00000000-0000-0000-0000-000000000001/cancel").status_code == 503
    assert "secret" not in client.get("/jobs").text


def test_idempotency_key_is_restricted():
    client, _, _ = setup()
    assert client.post("/jobs", headers={"Idempotency-Key": "bad key\r\n"}, json={}).status_code == 422


def test_transport_failure_is_sanitized():
    client, store, transport = setup()

    def broken(*_):
        raise TransportError("secret and traceback details")

    transport.call = broken
    response = client.get("/cluster")
    assert response.status_code == 503
    assert "secret" not in response.text
