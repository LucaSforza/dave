import json
import os
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest

import run_cluster
from dave_backend.models import JobCreate, JobRecord, JobState
from dave_client import DaveClient


def record(**kwargs):
    return JobRecord(id=uuid4(), request=kwargs.pop("request", JobCreate()), submission_token="test", **kwargs)


def client_for(handler):
    client = DaveClient("http://127.0.0.1:8000")
    client.http.close()
    client.http = httpx.Client(base_url=client.base_url, transport=httpx.MockTransport(handler))
    return client


def test_environment_created_with_current_identity_and_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.delenv("DAVE_ENV_FILE", raising=False)
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / ".env.example").write_text("DAVE_UID=1000\nDAVE_GID=1000\nDAVE_SSH_HOST=cluster\n")
    path = run_cluster.prepare_environment(tmp_path)
    assert f"DAVE_UID={os.getuid()}" in path.read_text()
    assert f"DAVE_GID={os.getgid()}" in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_text("user-owned configuration\n")
    assert run_cluster.prepare_environment(tmp_path).read_text() == "user-owned configuration\n"


def test_missing_custom_environment_is_not_created(tmp_path, monkeypatch):
    target = tmp_path / "custom.env"
    monkeypatch.setenv("DAVE_ENV_FILE", str(target))
    with pytest.raises(ValueError, match="DAVE_ENV_FILE"):
        run_cluster.prepare_environment(tmp_path)
    assert not target.exists()


def test_api_url_uses_configured_port_and_restricts_local_target(tmp_path, monkeypatch):
    monkeypatch.delenv("DAVE_API_URL", raising=False)
    monkeypatch.delenv("DAVE_API_PORT", raising=False)
    env = tmp_path / ".env"
    env.write_text("DAVE_API_PORT=8123\n")
    assert run_cluster.api_url(env) == "http://127.0.0.1:8123"
    for url in ("http://example.com", "http://user:secret@localhost:8000", "http://localhost:8000/x", "http://localhost:0"):
        with pytest.raises(ValueError):
            run_cluster.api_url(env, url)


def test_preflight_failure_prevents_job_creation():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"ok": False, "errors": ["image", "config"]})

    with client_for(handler) as client:
        with pytest.raises(RuntimeError, match="image, config"):
            run_cluster.launch(client, JobCreate())
    assert paths == ["/cluster"]


def test_preflight_ssh_error_prevents_job_creation():
    with client_for(lambda request: httpx.Response(503, json={"detail": "unavailable"})) as client:
        with pytest.raises(RuntimeError, match="Preflight SSH"):
            run_cluster.launch(client, JobCreate())


def test_matching_active_session_is_reused_without_post():
    active = record(state=JobState.PENDING)
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={"ok": True} if request.url.path == "/cluster" else [active.model_dump(mode="json")])

    with client_for(handler) as client:
        selected, reused = run_cluster.launch(client, JobCreate())
    assert reused and selected.id == active.id
    assert calls == ["GET", "GET"]


@pytest.mark.parametrize("active", [record(request=JobCreate(memory_gb=64)), record(cancel_requested=True)])
def test_different_or_cancelling_session_requires_explicit_new(active):
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"ok": True} if request.url.path == "/cluster" else [active.model_dump(mode="json")])

    with client_for(handler) as client:
        with pytest.raises(RuntimeError, match="--new"):
            run_cluster.launch(client, JobCreate())


def test_explicit_new_queues_through_api_with_validated_parameters():
    created = record(request=JobCreate(gpus=0, max_allocations=1))
    posts = []

    def handler(request):
        if request.url.path == "/cluster":
            return httpx.Response(200, json={"ok": True})
        assert request.method == "POST" and request.url.path == "/jobs"
        posts.append(request)
        return httpx.Response(202, json=created.model_dump(mode="json"))

    with client_for(handler) as client:
        selected, reused = run_cluster.launch(client, created.request, new=True)
    assert not reused and selected.id == created.id
    assert json.loads(posts[0].content)["gpus"] == 0
    assert posts[0].headers["Idempotency-Key"].startswith("run-cluster-")


def test_uncertain_create_retry_preserves_idempotency_key():
    created = record()
    client = Mock()
    client.json.side_effect = [{"ok": True}, []]
    client.create.side_effect = [httpx.ReadTimeout("lost response"), created.model_dump(mode="json")]
    selected, reused = run_cluster.launch(client, JobCreate())
    assert selected.id == created.id and not reused
    assert client.create.call_args_list[0] == client.create.call_args_list[1]


def test_invalid_resources_do_not_start_stack(monkeypatch):
    start = Mock()
    monkeypatch.setattr(run_cluster, "start_stack", start)
    assert run_cluster.main(["--json", '{"gpus":3}']) == 1
    start.assert_not_called()


def test_main_starts_backend_checks_health_and_submits_via_api(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("DAVE_API_PORT=8123\n")
    monkeypatch.delenv("DAVE_API_URL", raising=False)
    monkeypatch.delenv("DAVE_API_PORT", raising=False)
    monkeypatch.setattr(run_cluster, "prepare_environment", lambda: env)
    start = Mock()
    monkeypatch.setattr(run_cluster, "start_stack", start)
    created = record()
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/cluster":
            return httpx.Response(200, json={"ok": True})
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(202, json=created.model_dump(mode="json"))

    urls = []

    def factory(url):
        urls.append(url)
        return client_for(handler)

    monkeypatch.setattr(run_cluster, "DaveClient", factory)
    assert run_cluster.main([]) == 0
    start.assert_called_once_with(env)
    assert urls == ["http://127.0.0.1:8123"]
    assert calls == [("GET", "/health"), ("GET", "/cluster"), ("GET", "/jobs"), ("POST", "/jobs")]


def test_desktop_wait_timeout_does_not_cancel_job(monkeypatch):
    client = Mock()
    pending = record(state=JobState.PENDING)
    client.job.return_value = pending.model_dump(mode="json")
    monkeypatch.setattr(run_cluster.time, "monotonic", Mock(side_effect=[0, 0, 2]))
    monkeypatch.setattr(run_cluster.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError, match="NON è stata annullata"):
        run_cluster.wait_for_desktop(client, str(pending.id), timeout=1)
    client.cancel.assert_not_called()


def test_stack_receives_resolved_environment_path(tmp_path, monkeypatch):
    run = Mock()
    monkeypatch.setattr(run_cluster.subprocess, "run", run)
    env = tmp_path / "custom.env"
    run_cluster.start_stack(env)
    assert run.call_args.kwargs["env"]["DAVE_ENV_FILE"] == str(env)
    assert run.call_args.args[0][-3:] == ["up", "-d", "--build"]
    assert run.call_args.kwargs["check"] is True


def test_malformed_preflight_response_does_not_create_job():
    with client_for(lambda _: httpx.Response(200, json=[])) as client:
        with pytest.raises(RuntimeError, match="preflight non valida"):
            run_cluster.launch(client, JobCreate())


def test_desktop_wait_stops_on_terminal_job_without_cancelling():
    client = Mock()
    failed = record(state=JobState.FAILED, error="runtime failed")
    client.job.return_value = failed.model_dump(mode="json")
    with pytest.raises(RuntimeError, match="runtime failed"):
        run_cluster.wait_for_desktop(client, str(failed.id), timeout=10)
    client.cancel.assert_not_called()


def test_backend_readiness_retries_connection_failure(monkeypatch):
    client = Mock()
    response = Mock()
    response.json.return_value = {"status": "ok"}
    client.request.side_effect = [httpx.ConnectError("starting"), response]
    monkeypatch.setattr(run_cluster.time, "sleep", lambda _: None)
    run_cluster.wait_for_backend(client, timeout=5)
    assert client.request.call_count == 2


def test_uncertain_create_reports_same_recovery_key():
    client = Mock()
    client.json.side_effect = [{"ok": True}, []]
    client.create.side_effect = httpx.ReadTimeout("lost response")
    with pytest.raises(RuntimeError, match="recovery-key"):
        run_cluster.launch(client, JobCreate(), idempotency_key="recovery-key")
    assert client.create.call_count == 3
    assert all(call.args[1] == "recovery-key" for call in client.create.call_args_list)


def test_main_desktop_uses_existing_client_after_readiness(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("")
    monkeypatch.setattr(run_cluster, "prepare_environment", lambda: env)
    monkeypatch.setattr(run_cluster, "start_stack", Mock())
    monkeypatch.setattr(run_cluster, "wait_for_backend", Mock())
    job = record(state=JobState.RUNNING)
    monkeypatch.setattr(run_cluster, "launch", Mock(return_value=(job, True)))
    wait = Mock()
    desktop = Mock(return_value=0)
    monkeypatch.setattr(run_cluster, "wait_for_desktop", wait)
    monkeypatch.setattr(run_cluster, "client_main", desktop)
    assert run_cluster.main(["--api", "http://127.0.0.1:8000", "--desktop"]) == 0
    assert wait.call_args.args[1:] == (str(job.id), 600)
    desktop.assert_called_once_with(["--api", "http://127.0.0.1:8000", "desktop", str(job.id)])
