#!/usr/bin/env python3
"""Import or verify the exact Git revisions in a vcstool-format lock file."""

import subprocess
import sys
import time
from pathlib import Path

import yaml


def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def run_git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "http.version=HTTP/1.1", *args],
        check=check,
        text=True,
    )


def normalized_url(url: str) -> str:
    if url.endswith(".git"):
        url = url[:-4]
    return url.rstrip("/").lower()


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: import_repos.py LOCK.repos DESTINATION", file=sys.stderr)
        return 2

    lock_path = Path(sys.argv[1])
    destination = Path(sys.argv[2])
    repositories = yaml.safe_load(lock_path.read_text(encoding="utf-8"))["repositories"]
    destination.mkdir(parents=True, exist_ok=True)

    for name, spec in repositories.items():
        repo = destination / name
        url = spec["url"]
        revision = spec["version"]

        if repo.exists() and not (repo / ".git").exists():
            raise RuntimeError(f"{repo} exists but is not a Git checkout; leaving it untouched")

        if not repo.exists():
            for attempt in range(1, 6):
                result = run_git("clone", "--no-checkout", url, str(repo), check=False)
                if result.returncode == 0:
                    break
                print(f"clone {name} failed (attempt {attempt}/5)", file=sys.stderr)
                if attempt == 5:
                    return result.returncode
                time.sleep(attempt * 2)

        current_url = git(repo, "remote", "get-url", "origin")
        if normalized_url(current_url) != normalized_url(url):
            raise RuntimeError(f"{name} origin differs from lock file: {current_url}")

        dirty = git(repo, "status", "--porcelain")
        if dirty:
            raise RuntimeError(f"{name} has local changes; refusing to change its checkout")

        current_revision = git(repo, "rev-parse", "HEAD", check=False)
        if current_revision != revision:
            fetched = run_git(
                "-C", str(repo), "fetch", "--depth=1", "origin", revision, check=False
            )
            if fetched.returncode != 0:
                return fetched.returncode
            git(repo, "checkout", "--detach", revision)

        print(f"{name}: {revision}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, KeyError, yaml.YAMLError) as error:
        print(f"dependency lock error: {error}", file=sys.stderr)
        raise SystemExit(1)
