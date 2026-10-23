#!/usr/bin/env python3
"""Fetch exact locked commits without vcstool's remote ref-type probing."""
import pathlib
import re
import subprocess
import sys
import time

import yaml


def git(path, *args):
    return subprocess.run(["git", "-c", "http.version=HTTP/1.1", "-C", str(path), *args], check=True)


def main():
    lock, destination = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    repositories = yaml.safe_load(lock.read_text())["repositories"]
    destination.mkdir(parents=True, exist_ok=True)
    for name, spec in repositories.items():
        revision = spec["version"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("lock requires safe names and exact commit SHAs")
        if spec.get("type") != "git" or not spec["url"].startswith("https://github.com/"):
            raise ValueError("unsupported locked repository")
        checkout = destination / name
        if checkout.exists():
            raise RuntimeError("refusing to overwrite existing checkout: {}".format(checkout))
        checkout.mkdir()
        git(checkout, "init")
        git(checkout, "remote", "add", "origin", spec["url"])
        for attempt in range(3):
            try:
                git(checkout, "fetch", "--depth=1", "origin", revision)
                break
            except subprocess.CalledProcessError:
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))
        git(checkout, "checkout", "--detach", revision)
        actual = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
        if actual != revision:
            raise RuntimeError("checkout differs from lock")
        print("{}: {}".format(name, actual), flush=True)


if __name__ == "__main__":
    main()
