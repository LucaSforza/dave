#!/usr/bin/env python3
"""Compute-node session supervisor, ROS Noetic compatible Python 3.8+."""
import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import socket

try:
    from ros_adapter import RexrovAdapter
except ImportError:
    RexrovAdapter = None


class Session:
    def __init__(self, root, job, index):
        self.root, self.job, self.index = root, job, index
        self.jobdir = os.path.join(root, "jobs", job)
        self.alloc = os.path.join(self.jobdir, "allocations", str(index))
        self.stop = threading.Event()
        self.rollover = False
        self.children = []
        self.critical_children = []
        self.adapter = None
        self.image = None
        self.started = time.monotonic()
        self.deadline = None

    def manifest(self, **fields):
        path = os.path.join(self.alloc, "runtime.json")
        data = {"ready": False, "rollover": False, "checkpoint": None, "control_results": {},
                "desktop": None, "error": None, "timestamp": time.time()}
        try:
            with open(path) as f:
                data.update(json.load(f))
        except (OSError, ValueError):
            pass
        data.update(fields)
        data["timestamp"] = time.time()
        atomic_json(path, data)

    def signal(self, signum, _frame):
        if signum == signal.SIGUSR1:
            self.rollover = True
        self.stop.set()

    def run(self):
        os.umask(0o077)
        for sig in (signal.SIGUSR1, signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self.signal)
        try:
            self.manifest(ready=False, rollover=False, error=None)
            try:
                with open(os.path.join(self.alloc, "request.json")) as f: self.request = json.load(f)
            except (OSError, ValueError): self.request = {}
            minutes = self.request.get("walltime_minutes", 30)
            if type(minutes) is not int or not 5 <= minutes <= 30:
                raise RuntimeError("invalid requested walltime")
            # Leave four minutes for checkpoint/teardown before Slurm's warning.
            self.deadline = self.started + max(60, minutes * 60 - 240)
            timer = threading.Thread(target=self.safety_timer, daemon=True)
            timer.start()
            self.setup()
            self.configure_masters()
            self.restore()
            self.start_desktop()
            self.start_simulation()
            self.check_ready()
            if self.stop.is_set():
                if self.rollover:
                    self.checkpoint()
                self.shutdown()
                self.manifest(ready=False, rollover=self.rollover)
                return 0
            self.manifest(ready=True, error=None)
            self.command_loop()
            if self.rollover:
                self.checkpoint()
            self.shutdown()
            self.manifest(ready=False, rollover=self.rollover)
            return 0
        except Exception as exc:
            self.shutdown()
            self.manifest(ready=False, error="{}: {}".format(type(exc).__name__, exc), rollover=False)
            traceback.print_exc(file=sys.stderr)
            return 1

    def setup(self):
        with open(os.path.join(self.root, "config.json")) as f:
            config = json.load(f)
        self.image = config.get("image")
        if not self.image:
            raise RuntimeError("configured Singularity image path is absent")
        self.config = config
        if config.get("use_nvidia") is not True:
            os.environ["LIBGL_ALWAYS_SOFTWARE"] = "1"
        self.manifest(ready=False, error=None)

    def command(self, cmd, env=None, critical=True):
        with open(os.path.join(self.alloc, "runtime.log"), "a") as stream:
            p = subprocess.Popen(cmd, env=env, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        self.children.append(p)
        if critical:
            self.critical_children.append(p)
        return p

    def start_desktop(self):
        # All listeners are explicitly loopback-only. Original Gazebo GUI runs in this X server.
        env = os.environ.copy()
        self.display = self.available_display()
        reserved = getattr(self, "master_ports", set())
        self.vnc_port = self.available_port(exclude=reserved)
        self.web_port = self.available_port(exclude=reserved | {self.vnc_port})
        env.update({"DISPLAY": self.display, "HOME": os.path.join(self.alloc, "home")})
        os.makedirs(env["HOME"], mode=0o700, exist_ok=True)
        env["XAUTHORITY"] = self.create_xauthority()
        self.xauthority = env["XAUTHORITY"]
        self.command(["Xvfb", self.display, "-auth", env["XAUTHORITY"], "-nolisten", "tcp",
                      "-screen", "0", "1600x900x24"], env)
        self.wait_processes(5)
        self.command(["openbox"], env)
        self.command(["xterm", "-geometry", "100x30+20+20", "-e", "/bin/bash", "-lc",
                      "source /opt/ros/noetic/setup.bash; source /opt/dave/devel/setup.bash; python3 /opt/dave/keyboard_teleop.py; exec bash"], env,
                     critical=False)
        self.command(["x11vnc", "-localhost", "-forever", "-shared", "-auth", env["XAUTHORITY"], "-display", self.display,
                      "-rfbport", str(self.vnc_port), "-passwdfile", self.vnc_password_file()], env)
        # Focal ships noVNC 1.0 (launch.sh, not modern novnc_proxy).
        # Invoke the packaged foreground websockify server directly.
        self.command(["websockify", "--web=/usr/share/novnc", "127.0.0.1:{}".format(self.web_port),
                      "127.0.0.1:{}".format(self.vnc_port)], env)
        self.wait_processes(10)
        if not self.wait_port("127.0.0.1", self.vnc_port, 10):
            raise RuntimeError("VNC service failed readiness on port {}".format(self.vnc_port))
        if not self.wait_http(self.web_port, 10):
            raise RuntimeError("noVNC did not serve its desktop page on port {}".format(self.web_port))

    def create_xauthority(self):
        import secrets
        path = os.path.join(self.alloc, ".Xauthority")
        if os.path.islink(path):
            raise RuntimeError("Xauthority file may not be a symlink")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        cookie = secrets.token_hex(16)
        try:
            subprocess.run(["xauth", "-f", path], input="add {} MIT-MAGIC-COOKIE-1 {}\n".format(self.display, cookie),
                           text=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.CalledProcessError):
            raise RuntimeError("unable to initialize private Xauthority cookie")
        os.chmod(path, 0o600)
        return path

    def vnc_password_file(self):
        # Read from a private file; never put the password in subprocess argv
        # (visible through ps and possibly exception tracebacks).
        path = os.path.join(self.alloc, ".vnc-secret")
        if os.path.islink(path):
            raise RuntimeError("VNC authentication files may not be symlinks")
        if not os.path.exists(path):
            import secrets
            secret = secrets.token_urlsafe(24)[:8]
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "w") as f: f.write(secret)
        else:
            with open(path) as f: secret = f.read().strip()
            if len(secret) != 8:
                raise RuntimeError("private VNC password file is invalid")
            os.chmod(path, 0o600)
        return path

    def start_simulation(self):
        env = os.environ.copy()
        env["DISPLAY"] = self.display
        env["XAUTHORITY"] = self.xauthority
        env["DAVE_JOB_ROOT"] = self.jobdir
        # Image entrypoint owns the pinned DAVE launch command.
        self.command(["/opt/dave/cluster-launch", "--scenario", "uw_lidar",
                      "--x", str(self.request.get("x", 4)), "--y", str(self.request.get("y", 4)),
                      "--z", str(self.request.get("z", -93)), "--yaw", str(self.request.get("yaw", -1.8))], env)
        if RexrovAdapter is None:
            raise RuntimeError("ROS adapter module unavailable")
        self.adapter = RexrovAdapter()
        self.adapter.wait_for_ros(timeout=120)
        self.wait_for_health(timeout=30, allow_paused=True)
        if getattr(self, "pending_restore", None):
            with open(self.pending_restore) as f:
                self.adapter.restore(json.load(f))
            self.wait_for_health(timeout=30, allow_paused=True)
        # gazebo_ros' client wrapper also loads its ROS API plugin: without
        # this remapping it registers as /gazebo and shuts down gzserver.
        self.command(["/opt/ros/noetic/lib/gazebo_ros/gzclient", "__name:=gazebo_gui"], env)
        self.wait_processes(3)

    def check_ready(self):
        self.wait_for_health(timeout=10, allow_paused=True)
        host = os.environ.get("SLURMD_NODENAME", "")
        if not host:
            raise RuntimeError("Slurm compute node identity unavailable")
        self.manifest(desktop={"node": host, "user": os.environ.get("USER", ""), "port": self.web_port,
                               "url": "http://127.0.0.1:{}/vnc.html".format(self.web_port)})

    def configure_masters(self):
        ros_port = self.available_port()
        gazebo_port = self.available_port(exclude={ros_port})
        self.master_ports = {ros_port, gazebo_port}
        os.environ["ROS_MASTER_URI"] = "http://127.0.0.1:{}".format(ros_port)
        os.environ["GAZEBO_MASTER_URI"] = "http://127.0.0.1:{}".format(gazebo_port)
        os.environ["GAZEBO_IP"] = "127.0.0.1"
        os.environ["ROS_IP"] = "127.0.0.1"
        os.environ.pop("ROS_HOSTNAME", None)

    def wait_for_health(self, timeout, allow_paused=False):
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            try:
                self.adapter.validate_health(allow_paused=allow_paused)
                return
            except Exception as error:
                last_error = error
                time.sleep(.1)
        raise RuntimeError("ROS sensor/model health did not become ready: {}".format(last_error))

    @staticmethod
    def available_display():
        for number in range(90, 200):
            if not os.path.exists("/tmp/.X11-unix/X{}".format(number)) and not os.path.exists("/tmp/.X{}-lock".format(number)):
                return ":{}".format(number)
        raise RuntimeError("no unoccupied X display is available")

    @staticmethod
    def available_port(exclude=None):
        exclude = exclude or set()
        for _ in range(20):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            finally:
                sock.close()
            if port not in exclude:
                return port
        raise RuntimeError("no unoccupied loopback desktop port available")

    def restore(self):
        if self.index == 1:
            return
        cpdir = os.path.join(self.jobdir, "checkpoints")
        resume_path = os.path.join(self.alloc, "resume.json")
        if not os.path.isfile(resume_path) or os.path.islink(resume_path):
            raise RuntimeError("successor allocation has no submitted checkpoint reference")
        with open(resume_path) as f:
            manifest = json.load(f)
        if (manifest.get("complete") is not True or manifest.get("rollover") is not True or manifest.get("job_id") != self.job or
                manifest.get("scenario") != "uw_lidar" or manifest.get("fidelity") != "explicit_state_only" or
                type(manifest.get("version")) is not int or manifest.get("version") != 1 or
                type(manifest.get("allocation_index")) is not int or manifest.get("allocation_index") != self.index - 1):
            raise RuntimeError("checkpoint does not match immediately previous allocation")
        if os.path.islink(cpdir) or os.path.realpath(cpdir) != cpdir:
            raise RuntimeError("checkpoint directory escapes job storage")
        snapshot_name = manifest.get("snapshot")
        if not isinstance(snapshot_name, str) or os.path.basename(snapshot_name) != snapshot_name:
            raise RuntimeError("checkpoint snapshot path is invalid")
        snap = os.path.realpath(os.path.join(cpdir, snapshot_name))
        if os.path.commonpath((os.path.realpath(cpdir), snap)) != os.path.realpath(cpdir) or os.path.islink(os.path.join(cpdir, snapshot_name)):
            raise RuntimeError("checkpoint snapshot escapes storage")
        named_manifest = manifest.get("manifest_path")
        if not isinstance(named_manifest, str) or os.path.basename(named_manifest) != named_manifest:
            raise RuntimeError("checkpoint manifest path is invalid")
        manifest_path = os.path.realpath(os.path.join(cpdir, named_manifest))
        if os.path.commonpath((os.path.realpath(cpdir), manifest_path)) != os.path.realpath(cpdir) or os.path.islink(os.path.join(cpdir, named_manifest)):
            raise RuntimeError("checkpoint manifest escapes storage")
        with open(manifest_path) as f:
            stored_manifest = json.load(f)
        if stored_manifest != manifest:
            raise RuntimeError("submitted checkpoint does not match stored completed manifest")
        with open(snap, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        if digest != manifest.get("sha256"):
            raise RuntimeError("checkpoint hash mismatch")
        self.pending_restore = snap

    def checkpoint(self):
        if not self.adapter:
            return
        was_paused = self.adapter.is_paused()
        paused_here = False
        try:
            if not was_paused:
                self.adapter.validate_health()
                self.adapter.pause()
                paused_here = True
            state = self.adapter.snapshot(pause_state=was_paused)
            self.adapter.stop_input()
            cpdir = os.path.join(self.jobdir, "checkpoints")
            if os.path.islink(cpdir) or os.path.realpath(cpdir) != cpdir:
                raise RuntimeError("checkpoint directory escapes job storage")
            os.makedirs(cpdir, mode=0o700, exist_ok=True)
            stamp = "{}-{}".format(self.index, time.time_ns())
            snapshot = stamp + ".json"
            snap_path = os.path.join(cpdir, snapshot)
            atomic_json(snap_path, state)
            with open(snap_path, "rb") as stream:
                digest = hashlib.sha256(stream.read()).hexdigest()
            manifest = {"version": 1, "complete": True, "job_id": self.job, "scenario": "uw_lidar",
                        "allocation_index": self.index, "created": time.time(), "snapshot": snapshot,
                        "sha256": digest, "fidelity": "explicit_state_only", "rollover": self.rollover}
            mp = stamp + ".manifest.json"
            manifest["manifest_path"] = mp
            atomic_json(os.path.join(cpdir, mp), manifest)
            self.manifest(checkpoint=dict(manifest), rollover=self.rollover)
        finally:
            if not self.rollover and paused_here:
                self.adapter.resume()

    def command_loop(self):
        last = 0
        while not self.stop.wait(1):
            for proc in self.critical_children:
                if proc.poll() is not None:
                    raise RuntimeError("desktop or simulator child exited unexpectedly")
            self.wait_for_health(timeout=30, allow_paused=True)
            cmd_dir = os.path.join(self.alloc, "commands")
            if os.path.isdir(cmd_dir):
                for name in sorted(x for x in os.listdir(cmd_dir) if x.endswith(".json")):
                    try:
                        seq = int(name[:-5])
                        if seq <= last:
                            continue
                        with open(os.path.join(cmd_dir, name)) as f:
                            action = json.load(f)["action"]
                        if action not in ("pause", "resume", "reset", "checkpoint"):
                            raise RuntimeError("unsupported queued control")
                        self.apply_control(seq, action)
                        last = seq
                    except Exception as error:
                        try:
                            seq = int(name[:-5])
                            self.record_control(seq, "unknown", "error", "{}: {}".format(type(error).__name__, str(error)[:300]))
                            last = seq
                        except Exception:
                            pass
                        continue
            self.maybe_periodic_checkpoint()

    def maybe_periodic_checkpoint(self, now=None):
        # Periodic safety checkpoint; never sets rollover by itself.
        now = time.time() if now is None else now
        interval = int(self.config.get("checkpoint_interval_seconds", 60))
        if now - getattr(self, "last_checkpoint", 0) >= interval:
            self.checkpoint()
            self.last_checkpoint = now
            return True
        return False

    def apply_control(self, sequence, action):
        try:
            if action == "pause": self.adapter.pause()
            elif action == "resume": self.adapter.resume()
            elif action == "reset": self.adapter.reset()
            elif action == "checkpoint": self.checkpoint()
            else: raise RuntimeError("unsupported control")
            self.record_control(sequence, action, "ok", None)
        except Exception as error:
            self.record_control(sequence, action, "error", "{}: {}".format(type(error).__name__, str(error)[:300]))

    def safety_timer(self):
        remaining = max(0, self.deadline - time.monotonic())
        if self.stop.wait(remaining):
            return
        self.rollover = True
        self.stop.set()

    def record_control(self, sequence, action, status, error):
        path = os.path.join(self.alloc, "commands", "results", "{:020d}.json".format(sequence))
        result = {"sequence": sequence, "action": action, "status": status, "error": error, "timestamp": time.time()}
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        atomic_json(path, result)
        manifest = self.read_manifest()
        results = manifest.get("control_results", {})
        results[str(sequence)] = result
        for old in sorted(results, key=lambda value: int(value))[:-32]:
            results.pop(old, None)
        self.manifest(control_results=results)

    def read_manifest(self):
        try:
            with open(os.path.join(self.alloc, "runtime.json")) as f: return json.load(f)
        except (OSError, ValueError): return {}

    def wait_processes(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            failed = [p for p in self.children if p.poll() is not None]
            if failed:
                raise RuntimeError("desktop process failed during startup")
            time.sleep(.1)

    @staticmethod
    def wait_port(host, port, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, port), timeout=.2):
                    return True
            except OSError:
                time.sleep(.1)
        return False

    @staticmethod
    def wait_http(port, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.5) as conn:
                    conn.sendall(b"GET /vnc.html HTTP/1.0\r\nHost: localhost\r\n\r\n")
                    response = conn.recv(256)
                if b" 200 " in response.split(b"\r\n", 1)[0]:
                    return True
            except OSError:
                pass
            time.sleep(.1)
        return False

    def shutdown(self):
        self.manifest(ready=False)
        if self.adapter:
            try: self.adapter.stop_input()
            except Exception: pass
        for proc in reversed(self.children):
            if proc.poll() is None:
                try: os.killpg(proc.pid, signal.SIGTERM)
                except OSError: pass
        for proc in reversed(self.children):
            try: proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try: os.killpg(proc.pid, signal.SIGKILL)
                except OSError: pass


def atomic_json(path, value):
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, separators=(",", ":")); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
        dfd = os.open(os.path.dirname(path), os.O_RDONLY)
        try: os.fsync(dfd)
        finally: os.close(dfd)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--root", required=True); p.add_argument("--job", required=True); p.add_argument("--index", type=int, required=True)
    a = p.parse_args()
    sys.exit(Session(a.root, a.job, a.index).run())


if __name__ == "__main__": main()
