"""Exercise the actual Redis Lua engine, not only fakeredis' implementation."""
import os
import shutil
import subprocess
import tempfile
import time
from uuid import uuid4

import pytest
import redis

from dave_backend.models import JobCreate, JobRecord, JobState
from dave_backend.store import RedisStore


@pytest.fixture
def live_store():
    binary = shutil.which("redis-server")
    if not binary:
        pytest.skip("optional real-Redis checks require redis-server")
    temporary_root = "/tmp/opencode" if os.path.isdir("/tmp/opencode") else None
    with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
        sock = os.path.join(directory, "redis.sock")
        process = subprocess.Popen(
            [binary, "--port", "0", "--unixsocket", sock, "--save", "", "--appendonly", "no"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        client = redis.Redis(unix_socket_path=sock, decode_responses=True)
        try:
            for _ in range(150):
                try:
                    if client.ping():
                        break
                except redis.RedisError:
                    time.sleep(0.02)
            else:
                pytest.fail("isolated Redis did not start")
            yield RedisStore("unused", client=client)
        finally:
            client.close()
            process.terminate()
            process.wait(timeout=5)


def test_real_redis_cancellation_preserves_empty_arrays(live_store):
    record = JobRecord(id=uuid4(), request=JobCreate(), submission_token="stable")
    live_store.create(record)
    cancelled = live_store.request_cancel(str(record.id))
    assert cancelled.state == JobState.CANCELLED
    assert cancelled.allocations == []
    assert live_store.get(str(record.id)).allocations == []


def test_real_redis_fenced_save_preserves_nested_checkpoint_arrays(live_store):
    record = JobRecord(id=uuid4(), request=JobCreate(), submission_token="stable", checkpoint={"joints": []})
    live_store.create(record)
    token = live_store.acquire_lock()
    assert live_store.save(record, lock_token=token)
    loaded = live_store.get(str(record.id))
    assert loaded.allocations == []
    assert loaded.checkpoint["joints"] == []
    assert loaded.revision == 1


def test_aof_preserves_jobs_and_idempotency_after_redis_restart():
    from dave_backend.service import JobService

    binary = shutil.which("redis-server")
    if not binary:
        pytest.skip("optional AOF integration requires redis-server")
    temporary_root = "/tmp/opencode" if os.path.isdir("/tmp/opencode") else None
    with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
        socket_path = os.path.join(directory, "redis.sock")
        command = [binary, "--port", "0", "--unixsocket", socket_path, "--dir", directory,
                   "--save", "", "--appendonly", "yes", "--appendfsync", "always"]
        job_id = None
        for restart in range(2):
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            client = redis.Redis(unix_socket_path=socket_path, decode_responses=True)
            try:
                for _ in range(150):
                    try:
                        if client.ping():
                            break
                    except redis.RedisError:
                        time.sleep(.02)
                else:
                    pytest.fail("persistent Redis did not start")
                store = RedisStore("unused", client=client)
                record = JobService(store).create_job(JobCreate(), idempotency_key="persist-across-restart")
                if restart == 0:
                    job_id = record.id
                else:
                    assert record.id == job_id
                    assert len(store.list()) == 1
                    assert record.allocations == []
            finally:
                client.close()
                process.terminate()
                process.wait(timeout=5)


def test_real_redis_cancel_cannot_erase_newer_allocation(live_store):
    from dave_backend.models import AllocationRecord

    record = JobRecord(id=uuid4(), request=JobCreate(), submission_token="stable")
    live_store.create(record)
    record.allocations.append(AllocationRecord(index=1, submission_token="stable", slurm_job_id="12", state="RUNNING"))
    record.state = JobState.RUNNING
    assert live_store.save(record)
    cancelled = live_store.request_cancel(str(record.id))
    assert cancelled.allocations[-1].slurm_job_id == "12"
    assert cancelled.cancel_requested
    assert cancelled.state == JobState.RUNNING
    assert not live_store.save(record)


def test_api_controller_restart_rollover_and_cancel_with_real_redis(live_store):
    from fastapi.testclient import TestClient

    from dave_backend.api import create_app
    from dave_backend.controller import ClusterController
    from dave_backend.settings import Settings

    class Scheduler:
        """Protocol fake only: no SSH or Slurm commands executed."""
        def __init__(self):
            self.states = {}
            self.submissions = []
            self.snapshots = {}

        def call(self, operation, payload):
            index = payload["allocation_index"]
            if operation == "submit":
                self.submissions.append(payload.copy())
                self.states[index] = "PENDING"
                return {"status": "submitted", "slurm_job_id": str(100 + index)}
            if operation == "cancel":
                self.states[index] = "CANCELLED"
                return {"status": "cancel_requested"}
            assert operation == "inspect"
            return {"scheduler_state": self.states[index], "slurm_job_id": str(100 + index),
                    "runtime": self.snapshots.get(index, {"ready": self.states[index] == "RUNNING"})}

    scheduler = Scheduler()
    settings = Settings()
    client = TestClient(create_app(store=live_store, transport=scheduler, settings=settings))
    response = client.post("/jobs", json={"max_allocations": 2})
    assert response.status_code == 202
    job_id = response.json()["id"]
    controller = ClusterController(live_store, scheduler, settings)
    controller.tick()
    scheduler.states[1] = "RUNNING"
    controller.tick()
    assert client.get(f"/jobs/{job_id}").json()["state"] == "running"
    checkpoint = {"complete": True, "job_id": job_id, "scenario": "uw_lidar", "allocation_index": 1,
                  "fidelity": "explicit_state_only", "version": 1, "sha256": "a" * 64,
                  "rollover": True, "snapshot": "1-test.json", "manifest_path": "1-test.manifest.json"}
    scheduler.states[1] = "COMPLETED"
    scheduler.snapshots[1] = {"ready": False, "rollover": True, "checkpoint": checkpoint}
    # New controller instance models a worker restart; Redis retains all intent.
    controller = ClusterController(live_store, scheduler, settings)
    controller.tick()
    assert client.get(f"/jobs/{job_id}").json()["state"] == "waiting_resume"
    controller.tick()
    assert len(scheduler.submissions) == 2
    assert scheduler.submissions[1]["checkpoint"] == checkpoint
    assert scheduler.submissions[1]["allocation_index"] == 2
    scheduler.states[2] = "RUNNING"
    controller.tick()
    assert client.post(f"/jobs/{job_id}/cancel").status_code == 200
    controller.tick()  # request scancel
    controller.tick()  # authoritative cancellation confirmation
    final = client.get(f"/jobs/{job_id}").json()
    assert final["state"] == "cancelled"
    assert len(final["allocations"]) == 2
    controller.tick()
    assert len(scheduler.submissions) == 2


def test_full_remote_protocol_checkpoint_and_rollover_without_cluster(live_store, tmp_path, monkeypatch):
    """Real dispatcher/files/checkpoint/Redis, only ROS and Slurm are faked."""
    import json
    import re
    from pathlib import Path

    from dave_backend.controller import ClusterController
    from dave_backend.service import JobService
    from dave_backend.settings import Settings

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "cluster"))
    from runtime.dispatch import Dispatcher, atomic_json
    from runtime.session import Session

    root = tmp_path / "remote"
    root.mkdir(mode=0o700)
    image = root / "test.sif"
    image.touch()  # no runtime is executed by this test
    atomic_json(str(root / "config.json"), {"image": str(image)})

    class FakeSlurm:
        def __init__(self):
            self.jobs = {}
            self.scripts = []

        def __call__(self, args, **kwargs):
            output = ""
            if args[0] == "sbatch":
                script = Path(args[-1]).read_text()
                self.scripts.append(script)
                name = re.search(r"^#SBATCH --job-name=(.+)$", script, re.M).group(1)
                job_id = str(100 + len(self.jobs))
                self.jobs[job_id] = {"name": name, "state": "PENDING"}
                output = job_id
            elif args[0] == "squeue":
                if "-j" in args:
                    job = self.jobs[args[args.index("-j") + 1]]
                    output = job["state"] if job["state"] in {"RUNNING", "PENDING", "COMPLETING"} else ""
                else:
                    output = "\n".join(f"{sid}|{job['name']}" for sid, job in self.jobs.items()
                                       if job["state"] in {"RUNNING", "PENDING", "COMPLETING"})
            elif args[0] == "sacct":
                if "-j" in args:
                    output = self.jobs[args[args.index("-j") + 1]]["state"]
                else:
                    output = "\n".join(f"{sid}|{job['name']}|{job['state']}" for sid, job in self.jobs.items())
            elif args[0] == "scancel":
                self.jobs[args[1]]["state"] = "CANCELLED"
            else:
                pytest.fail(f"unexpected command: {args}")
            return subprocess.CompletedProcess(args, 0, output, "")

    scheduler = FakeSlurm()
    dispatcher = Dispatcher(str(root), runner=scheduler)

    class LocalTransport:
        def call(self, operation, payload):
            return dispatcher.run(dict(payload, operation=operation))

    service = JobService(live_store)
    job = service.create_job(JobCreate(max_allocations=2))
    controller = ClusterController(live_store, LocalTransport(), Settings())
    controller.tick()
    assert "#SBATCH --partition=students" in scheduler.scripts[0]
    assert "#SBATCH --gres=gpu:1" in scheduler.scripts[0]
    scheduler.jobs["100"]["state"] = "RUNNING"
    session = Session(str(root), str(job.id), 1)
    session.manifest(ready=True)
    controller.tick()
    assert service.get_job(job.id).state == JobState.RUNNING

    class FakeAdapter:
        def is_paused(self): return False
        def validate_health(self): pass
        def pause(self): pass
        def stop_input(self): pass
        def snapshot(self, pause_state=None):
            return {"schema": 1, "model": "rexrov", "pause_state": pause_state, "joints": {"names": [], "positions": []}}

    session.adapter = FakeAdapter()
    session.rollover = True
    session.checkpoint()
    session.manifest(ready=False, rollover=True)
    scheduler.jobs["100"]["state"] = "COMPLETED"
    controller.tick()
    assert service.get_job(job.id).state == JobState.WAITING_RESUME
    controller.tick()
    assert len(scheduler.jobs) == 2
    resumed = Session(str(root), str(job.id), 2)
    resumed.restore()
    assert json.loads(Path(resumed.pending_restore).read_text())["model"] == "rexrov"
    service.cancel_job(job.id)
    controller.tick()
    controller.tick()
    assert service.get_job(job.id).state == JobState.CANCELLED
