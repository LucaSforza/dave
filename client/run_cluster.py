"""One-command local backend startup and safe cluster-session submission."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from dotenv import dotenv_values

from dave_backend.models import JobCreate, JobRecord, JobState
from dave_client import DaveClient, main as client_main

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {JobState.COMPLETED, JobState.CANCELLED, JobState.FAILED, JobState.NEEDS_ATTENTION}


def prepare_environment(root: Path = ROOT) -> Path:
    """Create only the default local .env, exclusively, without overwriting it."""
    override = os.environ.get("DAVE_ENV_FILE")
    path = Path(override).expanduser() if override else root / "backend" / ".env"
    if not path.is_absolute():
        path = root / path
    if not path.exists():
        if override:
            raise ValueError("DAVE_ENV_FILE non esiste: prepara il file di configurazione prima dell'avvio.")
        template = (root / "backend" / ".env.example").read_text()
        template = re.sub(r"(?m)^DAVE_UID=.*$", f"DAVE_UID={os.getuid()}", template)
        template = re.sub(r"(?m)^DAVE_GID=.*$", f"DAVE_GID={os.getgid()}", template)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w") as stream:
                stream.write(template)
            print("Creato backend/.env con UID/GID correnti; configurazione SSH non modificata.")
    return path.resolve()


def api_url(env_file: Path, explicit: str | None = None) -> str:
    config = dotenv_values(env_file)
    port = os.environ.get("DAVE_API_PORT") or config.get("DAVE_API_PORT") or "8000"
    url = explicit or os.environ.get("DAVE_API_URL") or config.get("DAVE_API_URL") or f"http://127.0.0.1:{port}"
    parsed = urlsplit(url)
    if (parsed.scheme not in ("http", "https") or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("run-cluster richiede l'URL locale del backend, senza credenziali o percorsi.")
    selected_port = parsed.port
    if selected_port is not None and not 1 <= selected_port <= 65535:
        raise ValueError("Porta API non valida")
    host = "[::1]" if parsed.hostname == "::1" else parsed.hostname
    return f"{parsed.scheme}://{host}" + (f":{selected_port}" if selected_port else "")


def start_stack(env_file: Path):
    subprocess.run(["bash", str(ROOT / "backend" / "compose.sh"), "up", "-d", "--build"],
                   cwd=ROOT, check=True, env={**os.environ, "DAVE_ENV_FILE": str(env_file)})


def wait_for_backend(client: DaveClient, timeout: float):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = client.request("GET", "/health", timeout=2)
            if response.json().get("status") == "ok":
                return
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(.5)
    raise RuntimeError("Backend/Redis non pronti entro il limite. Controlla `just backend-logs`.")


def launch(client: DaveClient, request: JobCreate, *, new: bool = False, idempotency_key: str | None = None):
    try:
        preflight = client.json("GET", "/cluster")
    except httpx.HTTPError as error:
        raise RuntimeError("Preflight SSH non disponibile. Verifica `ssh cluster`, l'accesso dal container e "
                           "il runtime in cluster/README.md. Nessuna nuova sessione creata.") from error
    if not isinstance(preflight, dict):
        raise RuntimeError("Risposta preflight non valida. Nessuna nuova sessione creata.")
    if preflight.get("ok") is not True:
        missing = ", ".join(str(item) for item in preflight.get("errors", [])) or "prerequisiti remoti"
        raise RuntimeError(f"Preflight cluster fallito ({missing}). Prepara runtime e SIF secondo cluster/README.md; "
                           "nessuna nuova sessione creata, nessuna build sul login node.")
    if not new:
        active = [JobRecord.model_validate(item) for item in client.json("GET", "/jobs")]
        active = [record for record in active if record.state not in TERMINAL]
        for record in active:
            if not record.cancel_requested and record.request == request:
                return record, True
        if active:
            raise RuntimeError(f"Esiste già una sessione attiva o in annullamento ({active[0].id}). "
                               "Usa gli stessi parametri per riutilizzarla, annullala o scegli esplicitamente --new.")
    # Repeat an uncertain HTTP request only with the SAME idempotency key.
    key = idempotency_key or "run-cluster-" + str(uuid4())
    for attempt in range(3):
        try:
            return JobRecord.model_validate(client.create(request.model_dump(mode="json"), key)), False
        except httpx.TransportError as error:
            if attempt == 2:
                raise RuntimeError("Esito della creazione incerto: controlla `just backend-client list` prima di riprovare. "
                                   f"Chiave di idempotenza: {key}") from error


def wait_for_desktop(client: DaveClient, job_id: str, timeout: float):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        job = JobRecord.model_validate(client.job(job_id))
        if job.state != last:
            print(f"Stato: {job.state}", flush=True)
            last = job.state
        if job.cancel_requested or job.state in TERMINAL:
            raise RuntimeError(f"Sessione non interattiva: {job.state}. {job.error or job.reconciliation_error or ''}")
        if job.state == JobState.RUNNING:
            return
        time.sleep(2)
    raise RuntimeError("Attesa del desktop scaduta. La sessione NON è stata annullata; controlla stato e log.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Avvia backend e sessione RexROV via API/Slurm, senza build sul login.")
    parser.add_argument("--json", default="{}", help="Parametri JobCreate JSON (default: 16 CPU, 1 GPU, 32 GiB, 30 min, max 2 allocazioni)")
    parser.add_argument("--new", action="store_true", help="Crea una nuova sessione anche se ne esiste una attiva")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--api", help="URL locale API; altrimenti usa DAVE_API_URL o DAVE_API_PORT")
    parser.add_argument("--desktop", action="store_true", help="Attendi la readiness e apri Gazebo via SSH/noVNC")
    parser.add_argument("--startup-timeout", type=float, default=90)
    parser.add_argument("--wait-seconds", type=float, default=600, help="Attesa massima desktop; non annulla alla scadenza")
    args = parser.parse_args(argv)
    job_id = None
    try:
        request = JobCreate.model_validate(json.loads(args.json))
        if not 1 <= args.startup_timeout <= 600 or not 1 <= args.wait_seconds <= 86400:
            raise ValueError("Timeout fuori intervallo")
        if args.idempotency_key and not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", args.idempotency_key):
            raise ValueError("Chiave di idempotenza non valida")
        env_file = prepare_environment()
        url = api_url(env_file, args.api)
        start_stack(env_file)
        with DaveClient(url) as client:
            wait_for_backend(client, args.startup_timeout)
            print(f"Richiesta: {request.cpus} CPU, {request.gpus} GPU, {request.memory_gb} GiB, "
                  f"{request.walltime_minutes} min/allocazione, massimo {request.max_allocations} allocazioni.")
            job, reused = launch(client, request, new=args.new, idempotency_key=args.idempotency_key)
            job_id = str(job.id)
            print(f"{'Sessione riutilizzata' if reused else 'Sessione accodata'}: {job_id}", flush=True)
            print(f"Stato: {job.state}; il worker gestisce l'invio e la coda Slurm.")
            print(f"API: {url}/docs\nStato: just backend-client --api {url} status {job_id}\n"
                  f"Stop:  just backend-client --api {url} cancel {job_id}")
            if args.desktop:
                print("Attendo il desktop. Ctrl-C interrompe l'attesa/tunnel, NON annulla il job.", flush=True)
                wait_for_desktop(client, job_id, args.wait_seconds)
            else:
                print(f"Desktop (quando running): just backend-client --api {url} desktop {job_id}")
        if args.desktop:
            return client_main(["--api", url, "desktop", job_id])
        return 0
    except KeyboardInterrupt:
        print("\nAvvio/attesa interrotti; nessun annullamento automatico. Controlla `just backend-client list`.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, httpx.HTTPError) as error:
        print(f"run-cluster: {error}", file=sys.stderr)
        if job_id:
            print(f"La sessione {job_id} resta gestita dal backend: controlla stato/log o annullala esplicitamente.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
