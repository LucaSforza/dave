import json
import os
import re
import subprocess
import tempfile


class TransportError(RuntimeError):
    pass


class SSHTransport:
    OPERATIONS = {"preflight", "submit", "inspect", "cancel", "logs", "artifacts", "download", "control", "desktop"}

    def __init__(self, settings, *, runner=None):
        self.settings = settings
        self.runner = runner

    def call(self, operation: str, payload: dict) -> dict:
        if operation not in self.OPERATIONS:
            raise ValueError("Unsupported remote operation")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", self.settings.ssh_host):
            raise ValueError("ssh_host must be a safe SSH alias or hostname")
        if len(json.dumps(payload).encode()) > 1_048_576:
            raise ValueError("Request payload exceeds 1 MiB")
        root = self.settings.remote_root
        if not root or root.startswith("/") or any(x in ("", ".", "..") for x in root.split("/")) or not all(part.replace("-", "a").replace("_", "a").replace(".", "a").isalnum() for part in root.split("/")):
            raise ValueError("remote_root must be a safe home-relative path")
        command = f"python3 \"$HOME/{root}/runtime/dispatch.py\" {operation} --root \"$HOME/{root}\""
        args = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", f"ConnectTimeout={max(1, int(self.settings.command_timeout))}"]
        if self.settings.ssh_config:
            args += ["-F", self.settings.ssh_config]
        if self.settings.ssh_identity:
            args += ["-i", self.settings.ssh_identity]
        if self.settings.use_ssh_agent:
            agent = os.environ.get("SSH_AUTH_SOCK")
            if not agent:
                raise TransportError("SSH agent mode requires SSH_AUTH_SOCK")
            args += ["-o", "IdentityAgent=" + agent]
        args += [self.settings.ssh_host, command]
        limit = max(self.settings.ssh_output_limit, 12 * 1024 * 1024 if operation == "download" else 0)
        try:
            if self.runner is not None:  # Injectable runner for deterministic unit tests.
                result = self.runner(args, input=json.dumps(payload).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     timeout=self.settings.command_timeout, check=False)
                stdout, stderr, returncode = result.stdout, result.stderr, result.returncode
            else:
                with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
                    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=out, stderr=err)
                    try:
                        proc.communicate(json.dumps(payload).encode(), timeout=self.settings.command_timeout)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.communicate()
                        raise
                    returncode = proc.returncode
                    out.seek(0, 2); out_size = out.tell()
                    err.seek(0, 2); err_size = err.tell()
                    if out_size > limit or err_size > min(limit, 1_048_576):
                        raise TransportError("Remote response exceeded output limit")
                    out.seek(0); stdout = out.read(limit + 1)
                    err.seek(0); stderr = err.read(min(limit, 1_048_576) + 1)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TransportError(f"SSH operation failed: {exc}") from exc
        if len(stdout) > limit or len(stderr) > min(limit, 1_048_576):
            raise TransportError("Remote response exceeded output limit")
        try:
            body = json.loads(stdout)
        except (ValueError, TypeError) as exc:
            raise TransportError("Remote returned invalid JSON") from exc
        if returncode or not isinstance(body, dict) or body.get("error"):
            raise TransportError(str(body.get("error", "Remote command failed")) if isinstance(body, dict) else "Remote command failed")
        return body
