#!/usr/bin/env python3
"""Stdlib-only login-node JSON dispatcher for DAVE remote sessions."""
import argparse
import base64
import contextlib
import fcntl
import getpass
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

MAX_READ = 8 * 1024 * 1024
MAX_LOG = 64 * 1024
MAX_ARTIFACTS = 256
OPS = {"preflight", "submit", "inspect", "cancel", "logs", "artifacts", "download", "control", "desktop"}
SLURM_ID = re.compile(r"^[0-9]+$")
CONTROLS = {"pause", "resume", "reset", "checkpoint"}


class DispatchError(Exception):
    def __init__(self, message, code="invalid_request"):
        super().__init__(message)
        self.code = code


def atomic_json(path, value):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        dfd = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_text(path, value):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        dfd = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Dispatcher:
    def __init__(self, root, runner=None):
        self.root = os.path.realpath(os.path.expanduser(root))
        if self.root in (os.path.sep, os.path.realpath(os.path.expanduser("~"))):
            raise DispatchError("remote root must be a dedicated private subdirectory")
        self.runner = runner or subprocess.run
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        root_stat = os.stat(self.root)
        if root_stat.st_uid != os.geteuid() or stat.S_IMODE(root_stat.st_mode) & 0o077:
            raise DispatchError("remote root must be owned by this user and mode 0700")

    @staticmethod
    @contextlib.contextmanager
    def locked(path):
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def run(self, args):
        if not isinstance(args, dict):
            raise DispatchError("request must be a JSON object")
        op = args.get("operation")
        if op not in OPS:
            raise DispatchError("unsupported operation")
        return getattr(self, "op_" + op)(args)

    def jobdir(self, p):
        jid = p.get("job_id")
        if not isinstance(jid, str):
            raise DispatchError("invalid job id")
        try:
            if str(uuid.UUID(jid)) != jid:
                raise ValueError()
        except ValueError:
            raise DispatchError("invalid job id")
        jobs = os.path.join(self.root, "jobs")
        d = os.path.join(jobs, jid)
        if os.path.islink(jobs) or os.path.islink(d):
            raise DispatchError("job path contains a symlink")
        os.makedirs(jobs, mode=0o700, exist_ok=True)
        os.makedirs(d, mode=0o700, exist_ok=True)
        if os.path.realpath(d) != d:
            raise DispatchError("job path escapes remote root")
        os.chmod(jobs, 0o700)
        os.chmod(d, 0o700)
        return d

    def alloc(self, p):
        job = self.jobdir(p)
        index = p.get("allocation_index")
        if type(index) is not int or not 1 <= index <= 4:
            raise DispatchError("invalid allocation index")
        path = os.path.join(job, "allocations", str(index))
        allocations = os.path.join(job, "allocations")
        if os.path.islink(allocations) or os.path.islink(path):
            raise DispatchError("allocation path contains a symlink")
        os.makedirs(allocations, mode=0o700, exist_ok=True)
        os.makedirs(path, mode=0o700, exist_ok=True)
        if os.path.realpath(path) != path:
            raise DispatchError("allocation path escapes remote root")
        os.chmod(allocations, 0o700)
        os.chmod(path, 0o700)
        return job, path

    def safe_file(self, base, relative):
        if not isinstance(relative, str) or not relative or os.path.isabs(relative):
            raise DispatchError("invalid relative path")
        if self.has_symlink_component(base, relative):
            raise DispatchError("symlink path is not allowed")
        candidate = os.path.realpath(os.path.join(base, relative))
        if os.path.commonpath((os.path.realpath(base), candidate)) != os.path.realpath(base):
            raise DispatchError("path escapes job root")
        return candidate

    def command(self, argv, timeout=10):
        try:
            r = self.runner(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise DispatchError("Slurm command unavailable", "slurm_unavailable")
        if r.returncode:
            raise DispatchError("Slurm command failed", "slurm_error")
        if len(r.stdout) > 1024 * 1024:
            raise DispatchError("Slurm response exceeded metadata limit", "slurm_output_too_large")
        return r.stdout.strip()

    def op_preflight(self, _):
        cfgpath = os.path.join(self.root, "config.json")
        checks = {"sbatch": shutil.which("sbatch") is not None,
                  "squeue": shutil.which("squeue") is not None,
                  "sacct": shutil.which("sacct") is not None,
                  "config": os.path.isfile(cfgpath) and not os.path.islink(cfgpath)}
        cfg = {}
        try:
            if checks["config"]:
                with open(cfgpath) as f:
                    cfg = json.load(f)
        except (OSError, ValueError):
            pass
        image = cfg.get("image")
        checks["image"] = bool(image and os.path.isabs(image) and os.path.isfile(image))
        checks["singularity"] = bool(shutil.which("singularity") or shutil.which("apptainer"))
        errors = [k for k, v in checks.items() if not v]
        return {"ok": not errors, "checks": checks, "errors": errors}

    def op_submit(self, p):
        job, alloc = self.alloc(p)
        receipt = os.path.join(alloc, "submission.json")
        with self.locked(os.path.join(self.root, ".account-submit.lock")), self.locked(os.path.join(job, ".submit.lock")):
            # Single account-wide lock closes the race between quota probe and sbatch.
            if os.path.isfile(receipt):
                data = self.read_json(receipt)
                sid = str(data.get("slurm_job_id", ""))
                if not SLURM_ID.fullmatch(sid):
                    raise DispatchError("corrupt submission receipt", "receipt_invalid")
                return {"status": "submitted", "slurm_job_id": sid}
            name = "dave-{}-{}".format(p["job_id"].replace("-", ""), p["allocation_index"])
            intent = os.path.join(alloc, "submission.intent.json")
            # A prior intent without receipt means sbatch may have succeeded. Never retry
            # unless the exact deterministic allocation is found in queue/accounting.
            had_intent = os.path.isfile(intent)
            try:
                existing = self.command(["squeue", "-h", "-u", getpass.getuser(), "-o", "%i|%200j"])
            except DispatchError:
                raise DispatchError("cannot verify account-wide submission quota", "slurm_unavailable")
            for line in existing.splitlines():
                parts = line.split("|", 1)
                if len(parts) == 2 and parts[1] == name and SLURM_ID.fullmatch(parts[0]):
                    self.save_receipt(receipt, parts[0], name)
                    return {"status": "submitted", "slurm_job_id": parts[0]}
            if had_intent:
                recovered = self.reconcile_intent(name, receipt)
                if recovered:
                    return {"status": "submitted", "slurm_job_id": recovered}
                raise DispatchError("submission intent has no authoritative Slurm record; manual reconciliation required", "submission_ambiguous")
            # Account-wide MaxSubmit=1: any queued/running job blocks this allocation.
            if existing.strip():
                return {"status": "busy", "reason": "user has another queued or running job"}
            cfg = self.read_config()
            image = cfg.get("image")
            if not image or not os.path.isabs(image) or not os.path.isfile(image):
                raise DispatchError("configured runtime image is missing", "image_missing")
            req = p.get("request") or {}
            if req.get("scenario", "uw_lidar") != "uw_lidar":
                raise DispatchError("unsupported scenario")
            for key, default in (("x", 4), ("y", 4), ("z", -93), ("yaw", -1.8)):
                value = req.get(key, default)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise DispatchError("invalid {} coordinate".format(key))
                if key != "yaw" and not -1000 <= value <= 1000:
                    raise DispatchError("{} coordinate out of bounds".format(key))
                if key == "yaw" and not -6.2832 <= value <= 6.2832:
                    raise DispatchError("yaw out of bounds")
            if p["allocation_index"] > 1:
                checkpoint = p.get("checkpoint")
                if not self.valid_checkpoint(checkpoint, p["job_id"], p["allocation_index"] - 1):
                    raise DispatchError("successor requires the immediately preceding completed checkpoint", "checkpoint_invalid")
                previous = os.path.join(job, "allocations", str(p["allocation_index"] - 1))
                previous_runtime = self.read_json(os.path.join(previous, "runtime.json"))
                previous_receipt = self.read_json(os.path.join(previous, "submission.json"))
                if (previous_runtime.get("ready") is not False or previous_runtime.get("rollover") is not True or
                        previous_runtime.get("checkpoint") != checkpoint or not previous_receipt.get("slurm_job_id")):
                    raise DispatchError("previous allocation did not publish this rollover checkpoint", "checkpoint_invalid")
                previous_id = str(previous_receipt["slurm_job_id"])
                if not SLURM_ID.fullmatch(previous_id):
                    raise DispatchError("previous Slurm receipt is invalid", "receipt_invalid")
                previous_queue_state = self.queue_state(previous_id)
                if previous_queue_state:
                    raise DispatchError("previous allocation is still active", "allocation_active")
                if self.accounting_state(previous_id) != "COMPLETED":
                    raise DispatchError("previous allocation is not confirmed COMPLETED", "checkpoint_invalid")
                atomic_json(os.path.join(alloc, "resume.json"), checkpoint)
            cpus = self.bound(req.get("cpus", 16), 1, 16, "cpus")
            gpus = self.bound(req.get("gpus", 1), 0, 2, "gpus")
            mem = self.bound(req.get("memory_gb", 32), 1, 1024, "memory_gb")
            minutes = self.bound(req.get("walltime_minutes", 30), 5, 30, "walltime_minutes")
            script = os.path.join(alloc, "run.sh")
            atomic_json(os.path.join(alloc, "request.json"), req)
            runtime = os.path.join(os.path.dirname(__file__), "runtime.sh")
            with open(script, "w") as f:
                f.write("#!/bin/bash\n#SBATCH --nodes=1\n#SBATCH --ntasks=1\n#SBATCH --no-requeue\n")
                f.write("#SBATCH --cpus-per-task={}\n#SBATCH --mem={}G\n".format(cpus, mem))
                if gpus:
                    f.write("#SBATCH --gres=gpu:{}\n".format(gpus))
                f.write("#SBATCH --time=00:{:02d}:00\n#SBATCH --signal=B:USR1@180\n".format(minutes))
                f.write("#SBATCH --partition=students\n#SBATCH --job-name={}\n".format(name))
                f.write("exec /bin/bash {} {} {} {}\n".format(
                    self.quote(runtime), self.quote(self.root), self.quote(p["job_id"]), p["allocation_index"]))
            os.chmod(script, 0o700)
            atomic_json(intent, {"job_name": name, "created": time.time()})
            try:
                out = self.command(["sbatch", "--parsable", "--partition=students",
                                    "--chdir=" + alloc,
                                    "--output=" + os.path.join(alloc, "slurm-%j.log"), script], timeout=20)
            except DispatchError:
                # Do not resubmit on lost response. Next reconciliation discovers deterministic name.
                raise DispatchError("submission outcome ambiguous; reconcile before retry", "submission_ambiguous")
            sid = out.split(";", 1)[0]
            if not SLURM_ID.fullmatch(sid):
                raise DispatchError("invalid sbatch response", "submission_ambiguous")
            self.save_receipt(receipt, sid, name)
            return {"status": "submitted", "slurm_job_id": sid}

    def save_receipt(self, path, sid, name):
        atomic_json(path, {"slurm_job_id": sid, "job_name": name, "created": time.time()})

    @staticmethod
    def quote(value):
        import shlex
        return shlex.quote(str(value))

    @staticmethod
    def bound(value, low, high, name):
        if type(value) is not int or not low <= value <= high:
            raise DispatchError("invalid {}".format(name))
        return value

    def read_config(self):
        path = os.path.join(self.root, "config.json")
        try:
            if os.path.islink(path):
                raise OSError("config symlink not allowed")
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            raise DispatchError("remote config missing or invalid", "config_missing")

    @staticmethod
    def read_json(path):
        if os.path.islink(path):
            return {}
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    @staticmethod
    def valid_checkpoint(value, job_id, expected_index):
        safe_name = lambda item: isinstance(item, str) and item not in (".", "..") and not item.startswith(".") and os.path.basename(item) == item and re.fullmatch(r"[A-Za-z0-9_.-]+", item)
        return (isinstance(value, dict) and value.get("complete") is True and
                value.get("job_id") == job_id and value.get("scenario") == "uw_lidar" and
                type(value.get("allocation_index")) is int and value.get("allocation_index") == expected_index and value.get("fidelity") == "explicit_state_only" and
                type(value.get("version")) is int and value.get("version") == 1 and
                value.get("rollover") is True and
                safe_name(value.get("snapshot")) and safe_name(value.get("manifest_path")) and
                isinstance(value.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]))

    @staticmethod
    def find_named_job(text, name):
        for line in text.splitlines():
            parts = line.split("|", 1)
            if len(parts) == 2 and parts[1].strip() == name and SLURM_ID.fullmatch(parts[0].strip()):
                return parts[0].strip()
        return None

    def reconcile_intent(self, name, receipt_path):
        try:
            output = self.command(["sacct", "-n", "-X", "--user", getpass.getuser(), "--name", name,
                                  "--format=JobIDRaw,JobName%200,State", "--parsable2"])
        except DispatchError:
            return None
        for line in output.splitlines():
            fields = [field.strip() for field in line.split("|")]
            if len(fields) >= 3 and fields[1] == name and SLURM_ID.fullmatch(fields[0]):
                self.save_receipt(receipt_path, fields[0], name)
                return fields[0]
        return None

    def op_inspect(self, p):
        _, alloc = self.alloc(p)
        receipt = self.read_json(os.path.join(alloc, "submission.json"))
        sid = receipt.get("slurm_job_id")
        if not sid:
            intent = os.path.isfile(os.path.join(alloc, "submission.intent.json"))
            name = "dave-{}-{}".format(p["job_id"].replace("-", ""), p["allocation_index"])
            if intent:
                try:
                    q = self.command(["squeue", "-h", "-u", getpass.getuser(), "-o", "%i|%200j"])
                    sid = self.find_named_job(q, name)
                    if sid:
                        self.save_receipt(os.path.join(alloc, "submission.json"), sid, name)
                    else:
                        sid = self.reconcile_intent(name, os.path.join(alloc, "submission.json"))
                except DispatchError:
                    sid = None
                if sid:
                    qstate = self.queue_state(sid)
                    if qstate:
                        state = qstate
                    else:
                        state = self.accounting_state(sid)
                else:
                    state = "UNKNOWN"
            else:
                state = "NOT_SUBMITTED"
        else:
            sid = str(sid)
            if not SLURM_ID.fullmatch(sid):
                raise DispatchError("corrupt submission receipt", "receipt_invalid")
            q = self.queue_state(sid)
            if q:
                state = q
            else:
                try:
                    state = self.accounting_state(sid)
                except DispatchError:
                    state = "UNKNOWN"
        runtime = self.read_json(os.path.join(alloc, "runtime.json"))
        checkpoint = runtime.get("checkpoint")
        return {"scheduler_state": state, "slurm_job_id": sid,
                "runtime": {"ready": runtime.get("ready"), "rollover": runtime.get("rollover"),
                            "checkpoint": checkpoint, "error": runtime.get("error"),
                            "desktop": runtime.get("desktop"),
                            "control_results": runtime.get("control_results", {})}}

    def accounting_state(self, sid):
        try:
            result = self.command(["sacct", "-n", "-X", "-j", sid, "--format=State%40", "--parsable2"])
            return result.splitlines()[0].split("|")[0].split()[0].rstrip("+") if result else "UNKNOWN"
        except DispatchError:
            return "UNKNOWN"

    def queue_state(self, sid):
        try:
            result = self.command(["squeue", "-h", "-j", sid, "-o", "%T"])
            return result.splitlines()[0] if result else ""
        except DispatchError:
            # Accounting remains authoritative for jobs already purged from squeue.
            return None

    def op_cancel(self, p):
        _, alloc = self.alloc(p)
        r = self.read_json(os.path.join(alloc, "submission.json"))
        if not r.get("slurm_job_id") and os.path.isfile(os.path.join(alloc, "submission.intent.json")):
            name = "dave-{}-{}".format(p["job_id"].replace("-", ""), p["allocation_index"])
            try:
                queue = self.command(["squeue", "-h", "-u", getpass.getuser(), "-o", "%i|%200j"])
            except DispatchError:
                raise DispatchError("cannot safely resolve allocation for cancellation", "slurm_unavailable")
            sid = self.find_named_job(queue, name)
            if sid:
                self.save_receipt(os.path.join(alloc, "submission.json"), sid, name)
            else:
                sid = self.reconcile_intent(name, os.path.join(alloc, "submission.json"))
            if not sid:
                raise DispatchError("allocation submission remains ambiguous", "submission_ambiguous")
            r = {"slurm_job_id": sid}
        if not r.get("slurm_job_id"):
            return {"status": "not_found"}
        sid = str(r["slurm_job_id"])
        if not SLURM_ID.fullmatch(sid):
            raise DispatchError("corrupt submission receipt", "receipt_invalid")
        self.command(["scancel", sid])
        return {"status": "cancel_requested"}

    def op_logs(self, p):
        _, alloc = self.alloc(p)
        offset = p.get("offset", 0)
        raw_limit = p.get("limit", MAX_LOG)
        if type(raw_limit) is not int or raw_limit < 1:
            raise DispatchError("invalid limit")
        limit = min(raw_limit, MAX_LOG)
        if type(offset) is not int or offset < 0:
            raise DispatchError("invalid log offset")
        path = self.safe_file(alloc, "runtime.log")
        if not os.path.exists(path):
            receipt = self.read_json(os.path.join(alloc, "submission.json"))
            sid = str(receipt.get("slurm_job_id", ""))
            if SLURM_ID.fullmatch(sid):
                path = self.safe_file(alloc, "slurm-{}.log".format(sid))
            if not os.path.exists(path):
                return {"text": "", "next_offset": offset}
        with open(path, "rb") as f:
            f.seek(offset)
            b = f.read(limit)
        return {"text": b.decode("utf-8", "replace"), "next_offset": offset + len(b)}

    def op_artifacts(self, p):
        job = self.jobdir(p)
        found = []
        for base, dirs, files in os.walk(job, followlinks=False):
            relbase = os.path.relpath(base, job)
            dirs[:] = [d for d in dirs if not d.startswith(".") and not os.path.islink(os.path.join(base, d)) and
                       ((relbase == "." and d in ("checkpoints", "artifacts")) or relbase != ".")]
            for filename in files:
                path = os.path.join(base, filename)
                rel = os.path.relpath(path, job)
                if not self.is_public_artifact_path(rel) or os.path.islink(path) or not os.path.isfile(path):
                    continue
                try:
                    size = os.path.getsize(path)
                except OSError:
                    continue
                if size <= MAX_READ:
                    found.append({"path": rel, "size": size})
                if len(found) >= MAX_ARTIFACTS:
                    return {"artifacts": found}
        return {"artifacts": found}

    def op_download(self, p):
        job = self.jobdir(p)
        path = self.safe_file(job, p.get("path"))
        if not self.is_public_artifact_path(p.get("path")) or self.has_symlink_component(job, p.get("path")) or os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) > MAX_READ:
            raise DispatchError("artifact unavailable or exceeds limit")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as f:
            data = f.read(MAX_READ + 1)
        if len(data) > MAX_READ:
            raise DispatchError("artifact exceeds limit")
        return {"data_base64": base64.b64encode(data).decode("ascii"), "path": p["path"]}

    @staticmethod
    def is_public_artifact_path(relative):
        if not isinstance(relative, str) or os.path.isabs(relative):
            return False
        components = relative.split(os.sep)
        return (len(components) >= 2 and components[0] in ("checkpoints", "artifacts") and
                all(part not in ("", ".", "..") and not part.startswith(".") and "secret" not in part.lower()
                    for part in components))

    @staticmethod
    def has_symlink_component(base, relative):
        cursor = base
        for component in relative.split(os.sep):
            cursor = os.path.join(cursor, component)
            if os.path.islink(cursor):
                return True
        return False

    def op_control(self, p):
        _, alloc = self.alloc(p)
        action = p.get("action")
        if action not in CONTROLS:
            raise DispatchError("invalid control action")
        commands = os.path.join(alloc, "commands")
        if os.path.islink(commands):
            raise DispatchError("command queue path contains a symlink")
        os.makedirs(commands, mode=0o700, exist_ok=True)
        os.chmod(commands, 0o700)
        seqpath = os.path.join(commands, "sequence")
        with self.locked(os.path.join(commands, ".lock")):
            try:
                if os.path.islink(seqpath): raise DispatchError("command sequence path contains a symlink")
                with open(seqpath) as f:
                    seq = int(f.read()) + 1
            except (OSError, ValueError):
                prior = [int(name[:-5]) for name in os.listdir(commands)
                         if name.endswith(".json") and name[:-5].isdigit()]
                seq = max(prior, default=0) + 1
            atomic_text(seqpath, str(seq))
            atomic_json(os.path.join(commands, "{:020d}.json".format(seq)), {"sequence": seq, "action": action})
        return {"queued": True, "sequence": seq}

    def op_desktop(self, p):
        observed = self.op_inspect(p)
        if observed.get("scheduler_state") != "RUNNING":
            return {"ready": False}
        result = observed["runtime"]
        desc = result.get("desktop")
        if not result.get("ready") or not isinstance(desc, dict):
            return {"ready": False}
        host = desc.get("node")
        port = desc.get("port")
        user = desc.get("user")
        if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9.-]+", host) or type(port) is not int or not 1 <= port <= 65535 or not isinstance(user, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", user):
            raise DispatchError("invalid desktop descriptor", "manifest_invalid")
        return {"ready": True, "node": host, "user": user, "port": port}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation")
    parser.add_argument("--root", required=True)
    ns = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
        payload["operation"] = ns.operation
        result = Dispatcher(ns.root).run(payload)
        print(json.dumps(result, separators=(",", ":")))
    except Exception as exc:
        code = exc.code if isinstance(exc, DispatchError) else "internal_error"
        response = json.dumps({"error": str(exc), "code": code}, separators=(",", ":"))
        print(response)
        print(response, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
