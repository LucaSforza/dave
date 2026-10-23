import httpx
import pytest

from dave_client import DaveClient


def test_client_http_operations_and_context_cleanup():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    client = DaveClient("http://local", timeout=2)
    client.http.close()
    client.http = httpx.Client(base_url="http://local", transport=httpx.MockTransport(handler))
    with client as active:
        assert active.json("GET", "/cluster") == {"ok": True}
        assert active.create({"scenario": "uw_lidar"}, "key") == {"ok": True}
        assert requests[-1].headers["Idempotency-Key"] == "key"
        assert active.control("id", "pause") == {"ok": True}
        assert active.cancel("id") == {"ok": True}
    assert client.http.is_closed


def test_desktop_tunnel_validates_descriptor_before_spawning(monkeypatch):
    client = DaveClient()
    monkeypatch.setattr("dave_client.subprocess.Popen", lambda *_args, **_kwargs: pytest.fail("must not spawn"))
    with pytest.raises(ValueError):
        with client.desktop_tunnel({"ready": True, "node": "-oProxyCommand=bad", "user": "me", "port": 6080}):
            pass


def test_desktop_tunnel_uses_verified_control_master_and_closes(monkeypatch):
    calls = {}

    class FakeSocket:
        def bind(self, address):
            calls["bind"] = address

        def getsockname(self):
            return ("127.0.0.1", 43981)

        def close(self):
            pass

    class FakeProcess:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return 0 if self.terminated else None

        def terminate(self):
            self.terminated = True
            calls["terminated"] = True

        def wait(self, timeout):
            calls["wait"] = timeout

    monkeypatch.setattr("dave_client.socket.socket", FakeSocket)
    monkeypatch.setattr("dave_client.subprocess.Popen", lambda args, **kwargs: (calls.update(master=args, master_kwargs=kwargs) or FakeProcess()))

    def run(args, **kwargs):
        calls.setdefault("control", []).append(args)
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr("dave_client.subprocess.run", run)
    client = DaveClient()
    with client.desktop_tunnel({"ready": True, "node": "compute-2", "user": "dave", "port": 6080}) as url:
        assert url == "http://127.0.0.1:43981/vnc.html?autoconnect=true&resize=remote"
    assert "StrictHostKeyChecking=yes" in calls["master"]
    assert "-J" in calls["master"] and "cluster" in calls["master"]
    assert "-M" in calls["master"] and "-S" in calls["master"]
    assert any("forward" in command for command in calls["control"])
    assert any("exit" in command for command in calls["control"])
    assert calls["terminated"]


def test_desktop_tunnel_never_yields_when_forward_port_is_hijacked(monkeypatch):
    calls = []

    class FakeSocket:
        def bind(self, address):
            pass

        def getsockname(self):
            return ("127.0.0.1", 43981)

        def close(self):
            pass

    class FakeProcess:
        def poll(self):
            return None

        def terminate(self):
            calls.append("terminated")

        def wait(self, timeout):
            pass

    monkeypatch.setattr("dave_client.socket.socket", FakeSocket)
    monkeypatch.setattr("dave_client.subprocess.Popen", lambda *_a, **_kw: FakeProcess())

    def run(args, **kwargs):
        calls.append(args)
        if "forward" in args:
            raise __import__("subprocess").CalledProcessError(255, args)
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr("dave_client.subprocess.run", run)
    client = DaveClient()
    with pytest.raises(RuntimeError, match="could not bind"):
        with client.desktop_tunnel({"ready": True, "node": "compute-2", "user": "dave", "port": 6080}):
            pytest.fail("must not yield for a port not owned by SSH")
    assert "terminated" in calls
    assert any("exit" in command for command in calls if isinstance(command, list))
