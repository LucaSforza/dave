import json
import subprocess

import pytest

from dave_backend.settings import Settings
from dave_backend.transport import SSHTransport, TransportError


def test_explicit_agent_mode_overrides_host_socket_path(monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/ssh-agent.sock")
    captured = []

    def runner(args, **kwargs):
        captured.extend(args)
        return type("Result", (), {"returncode": 0, "stdout": b'{"ok":true}', "stderr": b""})()

    transport = SSHTransport(Settings(use_ssh_agent=True), runner=runner)
    assert transport.call("preflight", {})["ok"]
    assert "IdentityAgent=/run/ssh-agent.sock" in captured


def test_explicit_agent_mode_requires_socket_environment(monkeypatch):
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    with pytest.raises(TransportError, match="SSH_AUTH_SOCK"):
        SSHTransport(Settings(use_ssh_agent=True)).call("preflight", {})


class Result:
    returncode = 0
    stdout = b'{"ok":true}'
    stderr = b""


def settings(**kw):
    return Settings(**kw)


def test_ssh_argv_and_json_stdin_are_structured():
    seen = {}
    def runner(args, **kwargs):
        seen.update(args=args, kwargs=kwargs)
        return Result()
    assert SSHTransport(settings(ssh_config="/tmp/config", ssh_identity="/tmp/id"), runner=runner).call("preflight", {}) == {"ok": True}
    args = seen["args"]
    assert args[0] == "ssh" and "StrictHostKeyChecking=yes" in args
    assert "-F" in args and "-i" in args
    assert args[-1].startswith("python3 ") and "preflight" in args[-1]
    assert json.loads(seen["kwargs"]["input"]) == {}
    assert "shell" not in seen["kwargs"]


@pytest.mark.parametrize("kwargs", [{"operation": "arbitrary"}, {"operation": "preflight", "ssh_host": "-oProxyCommand=x"},
                                     {"operation": "preflight", "remote_root": "../bad"}])
def test_rejects_operation_host_and_path_injection(kwargs):
    operation = kwargs.pop("operation")
    tr = SSHTransport(settings(**kwargs), runner=lambda *a, **k: Result())
    with pytest.raises(ValueError): tr.call(operation, {})


def test_transport_rejects_nonzero_invalid_json_and_oversize():
    tr = SSHTransport(settings(), runner=lambda *a, **k: type("R", (), {"returncode": 1, "stdout": b'{"error":"bad"}', "stderr": b""})())
    with pytest.raises(TransportError): tr.call("preflight", {})
    tr = SSHTransport(settings(), runner=lambda *a, **k: type("R", (), {"returncode": 0, "stdout": b"no", "stderr": b""})())
    with pytest.raises(TransportError): tr.call("preflight", {})
    tr = SSHTransport(settings(ssh_output_limit=1024), runner=lambda *a, **k: type("R", (), {"returncode": 0, "stdout": b"x" * 1025, "stderr": b""})())
    with pytest.raises(TransportError): tr.call("preflight", {})


def test_transport_timeout_is_explicit():
    def timeout(*args, **kwargs): raise subprocess.TimeoutExpired(args[0], 1)
    tr = SSHTransport(settings(command_timeout=1), runner=timeout)
    with pytest.raises(TransportError): tr.call("submit", {})
