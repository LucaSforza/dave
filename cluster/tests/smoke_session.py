#!/usr/bin/env python3
"""Explicit local-container smoke test; real ROS/Gazebo, no Slurm or SSH.

Run only locally or within an authorized compute allocation, never on login.
The original GUI is confined to Xvfb; no browser/host window is opened.
"""
import argparse
import json
import os
import signal
import sys
import threading
import time
import uuid
from pathlib import Path

from dispatch import Dispatcher, atomic_json
from session import Session


def read(path):
    try:
        with open(path) as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-local-smoke", action="store_true", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--index", type=int, default=1)
    args = parser.parse_args()
    if str(uuid.UUID(args.job)) != args.job or args.index not in (1, 2):
        parser.error("canonical UUID and allocation index 1 or 2 required")
    root = Path(args.root).resolve()
    root.mkdir(mode=0o700, exist_ok=True)
    root.chmod(0o700)
    atomic_json(str(root / "config.json"), {"image": "direct-local-OCI-smoke-not-SIF", "use_nvidia": False,
                                           "checkpoint_interval_seconds": 60})
    allocation = root / "jobs" / args.job / "allocations" / str(args.index)
    allocation.mkdir(parents=True, mode=0o700, exist_ok=True)
    atomic_json(str(allocation / "request.json"), {"scenario": "uw_lidar", "gpus": 0, "walltime_minutes": 10})
    if args.index == 2:
        previous = read(root / "jobs" / args.job / "allocations" / "1" / "runtime.json")
        if not previous.get("checkpoint"):
            raise RuntimeError("first smoke allocation must produce a complete checkpoint")
        atomic_json(str(allocation / "resume.json"), previous["checkpoint"])
    os.environ["SLURMD_NODENAME"] = "local-oci-smoke-not-slurm"
    session = Session(str(root), args.job, args.index)
    dispatcher = Dispatcher(str(root))
    failures = []

    def drive():
        try:
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                manifest = read(allocation / "runtime.json")
                if manifest.get("error"):
                    raise RuntimeError(manifest["error"])
                if manifest.get("ready"):
                    break
                time.sleep(.2)
            else:
                raise RuntimeError("smoke runtime did not become ready")
            for action in ("pause", "checkpoint", "resume"):
                response = dispatcher.op_control({"job_id": args.job, "allocation_index": args.index, "action": action})
                result_path = allocation / "commands" / "results" / "{:020d}.json".format(response["sequence"])
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    result = read(result_path)
                    if result:
                        if result.get("status") != "ok":
                            raise RuntimeError("{} control failed: {}".format(action, result.get("error")))
                        break
                    time.sleep(.2)
                else:
                    raise RuntimeError("{} control did not complete".format(action))
            os.kill(os.getpid(), signal.SIGUSR1)
        except Exception as error:
            failures.append(str(error))
            os.kill(os.getpid(), signal.SIGTERM)

    observer = threading.Thread(target=drive, daemon=True)
    observer.start()
    result = session.run()
    observer.join(timeout=2)
    manifest = read(allocation / "runtime.json")
    if result or failures or not manifest.get("checkpoint") or manifest.get("rollover") is not True:
        log = allocation / "runtime.log"
        if log.exists():
            print(log.read_text(errors="replace")[-12000:], file=sys.stderr)
        print(json.dumps({"result": "failed", "manifest": manifest, "errors": failures}), file=sys.stderr)
        return 1
    print(json.dumps({"result": "passed", "allocation_index": args.index, "live_cluster": False,
                      "original_Gazebo_GUI": "supervised inside Xvfb", "RGB_and_LiDAR": "health validated",
                      "controls": "pause/checkpoint/resume", "checkpoint": manifest["checkpoint"],
                      "restored": args.index == 2}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
