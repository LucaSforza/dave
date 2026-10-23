"""Small HTTP client and CLI for the DAVE local API."""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import webbrowser
from contextlib import contextmanager
from urllib.parse import quote

import httpx


class DaveClient:
    def __init__(self, base_url: str | None = None, timeout: float = 30):
        self.base_url = (base_url or os.getenv("DAVE_API_URL", "http://127.0.0.1:8000")).rstrip("/")
        self.http = httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self):
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def request(self, method: str, path: str, **kwargs):
        response = self.http.request(method, path, **kwargs)
        response.raise_for_status()
        return response

    def json(self, method: str, path: str, **kwargs):
        return self.request(method, path, **kwargs).json()

    def create(self, payload: dict, idempotency_key: str | None = None):
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        return self.json("POST", "/jobs", json=payload, headers=headers)

    def job(self, job_id: str):
        return self.json("GET", f"/jobs/{job_id}")

    def logs(self, job_id: str, offset: int = 0, limit: int = 65536):
        return self.json("GET", f"/jobs/{job_id}/logs", params={"offset": offset, "limit": limit})

    def artifacts(self, job_id: str):
        return self.json("GET", f"/jobs/{job_id}/artifacts")

    def control(self, job_id: str, action: str):
        return self.json("POST", f"/jobs/{job_id}/control", json={"action": action})

    def cancel(self, job_id: str):
        return self.json("POST", f"/jobs/{job_id}/cancel")

    def download(self, job_id: str, artifact_path: str, destination: str):
        path = "/jobs/{}/artifacts/{}/download".format(job_id, quote(artifact_path, safe="/"))
        data = self.request("GET", path).content
        with open(destination, "wb") as output:
            output.write(data)

    @contextmanager
    def desktop_tunnel(self, descriptor: dict, local_port: int | None = None):
        """Open a verified SSH jump-host tunnel and always close it on exit."""
        node, user, remote_port = descriptor.get("node"), descriptor.get("user"), descriptor.get("port")
        if descriptor.get("ready") is not True or not isinstance(node, str) or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", node) or ".." in node or not isinstance(user, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}", user) or type(remote_port) is not int or not 1 <= remote_port <= 65535:
            raise ValueError("Invalid desktop descriptor")
        listener = socket.socket()
        try:
            listener.bind(("127.0.0.1", local_port or 0))
            chosen_port = listener.getsockname()[1]
        finally:
            listener.close()

        # Create a private control socket and establish/verify the SSH master
        # before requesting the local forward. The -O forward command only
        # succeeds if OpenSSH actually bound the requested local port; merely
        # connecting to an open port is not evidence that our tunnel owns it.
        target = f"{user}@{node}"
        common = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o",
                  "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=10", "-J", "cluster"]
        with tempfile.TemporaryDirectory(prefix="dave-ssh-") as private_dir:
            control_path = os.path.join(private_dir, "control")
            proc = None
            try:
                proc = subprocess.Popen(
                    ["ssh", *common, "-M", "-N", "-S", control_path, target],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        raise RuntimeError("SSH tunnel master exited before becoming ready")
                    try:
                        subprocess.run(["ssh", "-S", control_path, "-O", "check", target],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, timeout=1, check=True)
                        break
                    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
                        time.sleep(0.1)
                else:
                    raise TimeoutError("Timed out waiting for SSH tunnel master")

                forward = ["ssh", "-S", control_path, "-O", "forward", "-L",
                           f"127.0.0.1:{chosen_port}:127.0.0.1:{remote_port}", target]
                try:
                    subprocess.run(forward, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=10, check=True)
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
                    raise RuntimeError("SSH tunnel could not bind the requested local port") from exc

                yield f"http://127.0.0.1:{chosen_port}/vnc.html?autoconnect=true&resize=remote"
            finally:
                if proc is not None:
                    # Ask the master to exit cleanly, then reap it even if the
                    # control command fails. No SSH diagnostics contain secrets
                    # in the user-facing exception path.
                    try:
                        subprocess.run(["ssh", "-S", control_path, "-O", "exit", target],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, timeout=3, check=False)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                    if proc.poll() is None:
                        proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="dave", description="DAVE Cluster Control API client")
    parser.add_argument("--api", default=os.getenv("DAVE_API_URL", "http://127.0.0.1:8000"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    sub.add_parser("list")
    create = sub.add_parser("launch", aliases=["create"])
    create.add_argument("--idempotency-key")
    create.add_argument("--json", default="{}", help="JobCreate JSON (defaults shown in API schema)")
    for name in ("status", "logs", "artifacts", "control", "cancel", "desktop", "download"):
        command = sub.add_parser(name)
        if name != "preflight":
            command.add_argument("job_id")
        if name == "logs":
            command.add_argument("--offset", type=int, default=0)
            command.add_argument("--limit", type=int, default=65536)
        if name == "control":
            command.add_argument("action", choices=("pause", "resume", "reset", "checkpoint"))
        if name == "download":
            command.add_argument("artifact_path")
            command.add_argument("destination")
    args = parser.parse_args(argv)
    client = DaveClient(args.api)
    try:
        if args.command == "preflight":
            result = client.json("GET", "/cluster")
        elif args.command in ("launch", "create"):
            result = client.create(json.loads(args.json), args.idempotency_key)
        elif args.command == "list":
            result = client.json("GET", "/jobs")
        elif args.command == "status":
            result = client.job(args.job_id)
        elif args.command == "logs":
            result = client.logs(args.job_id, args.offset, args.limit)
        elif args.command == "artifacts":
            result = client.artifacts(args.job_id)
        elif args.command == "control":
            result = client.control(args.job_id, args.action)
        elif args.command == "cancel":
            result = client.cancel(args.job_id)
        elif args.command == "download":
            client.download(args.job_id, args.artifact_path, args.destination)
            result = {"downloaded": args.destination}
        elif args.command == "desktop":
            descriptor = client.json("GET", f"/jobs/{args.job_id}/desktop")
            with client.desktop_tunnel(descriptor) as url:
                webbrowser.open(url)
                print(f"Desktop open at {url}; press Ctrl-C to close the SSH tunnel", file=sys.stderr)
                try:
                    while True:
                        time.sleep(3600)
                except KeyboardInterrupt:
                    return 0
        else:
            result = client.json("GET", "/health")
        print(json.dumps(result, indent=2))
        return 0
    except (httpx.HTTPError, OSError, ValueError, RuntimeError, TimeoutError) as exc:
        print(f"dave: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
