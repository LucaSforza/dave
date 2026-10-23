# DAVE backend (local-only)

This starts the FastAPI/Redis controller on your PC. It is intentionally bound to
`127.0.0.1`, is a single-user local service (not a multi-user security boundary),
and does not submit jobs when imported or when an API request creates a logical
job. Only the worker submits jobs. No real cluster is required for tests.

## Start

### One command: backend + cluster session

```sh
just run-cluster
# Optional: wait for readiness and open the original Gazebo desktop
just run-cluster --desktop
```

This initializes `backend/.env` if absent (current UID/GID, mode 0600), without
overwriting existing settings. It builds/starts the **local backend**, waits for
Redis/API health, runs the read-only remote preflight, then requests a session
through the API. The worker handles submission and the Slurm queue; the command
prints the logical ID and status/cancel/desktop commands, not a false claim that
a compute node is immediately available.

The remote runtime and SIF must already be prepared per `cluster/README.md`.
Missing prerequisites stop the command before any new session is created; the
backend stays available for diagnostics. No upload, remote build or login-node
simulation is performed. Starting the worker may resume previously persisted
sessions according to their existing allocation budgets.

Defaults: 16 CPUs, 1 GPU, 32 GiB, 30 minutes per allocation, at most 2 allocations.
An identical active session is reused on subsequent runs; a different active or
cancelling session requires explicit `--new` to create another queued session.

```sh
# Initial software-rendered session without reserving a GPU; no rollover
just run-cluster --json '{"gpus":0,"max_allocations":1}'
# Explicitly request another session
just run-cluster --new
# If host SSH requires an agent
DAVE_FORWARD_AGENT=1 just run-cluster
```

`--desktop` waits at most 600 seconds by default (`--wait-seconds` overrides it).
Timeout/Ctrl-C does not cancel the job. Retrieve the private VNC password using
the cluster guide. Backend/API port overrides in `.env` are respected; an
explicit `--api` must point at a local API. Use `just run-cluster --help` for options.

### Backend only

Install Docker Compose, `uv`, and `just`. Configure the existing OpenSSH alias
`cluster` on the host, including known-host verification and your normal identity.
Then:

```sh
cp backend/.env.example backend/.env
# Set DAVE_UID and DAVE_GID to id -u and id -g if they are not 1000.
just backend-up
```

Compose mounts `${DAVE_SSH_DIR:-$HOME/.ssh}` read-only and runs as the configured
UID/GID so OpenSSH permissions remain usable. Never add keys to the image or
repository. The alias must be able to reach the cluster from inside the
container; `host.docker.internal` is mapped to the Linux host gateway for setups
whose SSH configuration needs it. No Docker socket is mounted.

The `just` recipes detect Docker emulated by rootless Podman and apply the
`compose.podman.yaml` keep-id mapping automatically. Matching numeric UID alone
is insufficient for rootless SSH bind mounts. The local simulation stack is
unaffected. For an encrypted key held by your host agent, opt in explicitly:

```sh
DAVE_FORWARD_AGENT=1 just backend-up
```

This requires a valid host `SSH_AUTH_SOCK`; no credentials are copied into an
image. For direct Compose invocation select the Podman/agent overrides yourself.

Compose builds the application user's home at the same absolute path as your
host home and mounts only `.ssh` read-only at that path, not your whole home.
Existing absolute `IdentityFile`, `Include` and known-host paths under `.ssh`
therefore work without editing your host SSH config. External helper executables
or agent sockets outside `.ssh` need explicit mounts/options. Before a desktop tunnel, provision
the compute node's host key in the mounted known_hosts using your normal trusted
host-key verification process; `StrictHostKeyChecking=yes` is deliberately kept
for both the cluster alias and compute target. Do not disable host verification
to work around a missing key.

Browse [Swagger](http://127.0.0.1:8000/docs) or [OpenAPI](http://127.0.0.1:8000/openapi.json).
Redis uses a named volume and AOF with `appendfsync always`; its published port
is loopback-only. `backend-down` does not delete this volume. Back it up before
intentional removal; never use `down -v` if you need to retain session metadata.
`GET /health` checks Redis readiness; `GET /health/live` is process liveness only.

## API and Python CLI

```sh
uv run --project backend python client/dave_client.py preflight
uv run --project backend python client/dave_client.py create --json '{"scenario":"uw_lidar"}'
uv run --project backend python client/dave_client.py status JOB_UUID
uv run --project backend python client/dave_client.py logs JOB_UUID
uv run --project backend python client/dave_client.py artifacts JOB_UUID
uv run --project backend python client/dave_client.py download JOB_UUID logs/session.log ./session.log
uv run --project backend python client/dave_client.py control JOB_UUID pause
uv run --project backend python client/dave_client.py desktop JOB_UUID
uv run --project backend python client/dave_client.py cancel JOB_UUID
```

The client tunnels noVNC to `127.0.0.1` using `ssh -J cluster user@compute-node`;
the compute host must be reachable and trusted by SSH, often via a suitable
`Host` entry and known_hosts configuration. API responses contain only a validated
node/user/port descriptor, never VNC credentials. If the runtime requires a
password, retrieve it through the documented private runtime procedure and enter
it directly into noVNC; it is not accepted by this API. Do not expose VNC/noVNC
ports publicly. The browser tunnel remains open until Ctrl-C.

Logs remain available for the latest allocation after a job becomes terminal;
desktop/control operations require the current allocation to be running.
Artifact downloads are restricted to paths returned by the remote listing and
limited to 8 MiB; logs are byte-offset based and limited to 64 KiB per request.
This MVP does not stream larger files. Use the API validation rules and OpenAPI
schema rather than treating it as a general cluster file browser.

Control operations return a sequence number when queued, not a promise that ROS
has already applied them. Poll the job's `runtime.control_results` for that
sequence's eventual `ok`/`error` result. Closing the desktop browser does not
cancel the job; use the cancel endpoint/CLI and wait for confirmed cancellation.

## Setup prerequisites

Remote preflight is lightweight. It does not upload files, build a simulator
image, or run Gazebo. Prepare the remote `dave-cluster` runtime and a complete
prebuilt cluster image explicitly according to the cluster runtime deployment
instructions; never build on a login node. The original Gazebo desktop,
checkpoint restoration, GPU rendering, compute-node SSH routing, and live
cluster compatibility are not verified by local tests.

## Local verification

```sh
cd backend
uv sync --locked
uv run pytest
```

API tests use injected fakes and make no SSH/Slurm calls. `just backend-test`
runs both backend and remote-runtime tests, including optional isolated real
Redis checks when `redis-server` is available. Existing `just setup`, `just run`, and `just stop`
remain dedicated to the local simulation stack.
