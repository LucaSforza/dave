# Implementation and validation plan

## Work packages

1. **Core (Luna):** backend models/settings, Redis persistence, safe SSH adapter, controller, worker, service, core mocked tests. Own only core paths defined in contracts plus `backend/tests/test_core*.py`.
2. **Remote runtime (Luna):** `cluster/` dispatcher, sbatch supervision, ROS checkpoint adapter, desktop startup, separate complete cluster image recipe, remote unit tests. No live submissions/builds, no edits to existing local `docker/`.
3. **API/deployment/client (Luna):** FastAPI composition, `backend/pyproject.toml` and `uv.lock`, backend Dockerfile/compose/environment docs using uv, API tests, Python client/CLI, new Justfile recipes while preserving existing ones. Consume core contracts, do not edit core files.
4. **Integration (coordinator):** review interfaces/security, run all tests locally, fix edge cases, update KB evidence and report actual limits. Do not claim successful remote desktop until a real allocated test has passed.

All implementation agents: use model Luna, inspect the committed KB diff before writing, coordinate cross-package issues through the parent, do not commit or overwrite another agent's files. No cluster configuration changes, no remote compute jobs.

## Test gates

- Validation bounds and command injection/path traversal/secret exposure tests.
- Create/read/list/cancel/idempotency API; persistent Redis tested with fake plus optional disposable real Redis.
- Controller lifecycle pending→running→checkpoint→terminal→successor; budget exhaustion; cancellation during reconciliation; unknown accounting; external job quota; duplicate worker/ambiguous SSH submission protection.
- Remote dispatcher uses mocked Slurm; checkpoints round-trip via injected/fake simulator adapter; corrupt snapshot rejection and signal-safe shutdown.
- Desktop endpoint unavailable before readiness/after cancellation; tunnel descriptor validated, bound to localhost, no credentials leaked.
- Docker/Compose validation/build when available, no DAVE simulator launched on host as part of tests.
- Use `uv sync --locked` and `uv run` for backend/client tests and commands; commit a reproducible uv lockfile.
- Existing local `just run` unchanged, static checks preserved.

## Getting started experience

Document `just backend-up`, OpenAPI `/docs`, Python CLI examples for preflight/create/status/logs/desktop/control/cancel, configuration of existing SSH alias inside containers, and explicit upload/build/image prerequisites. Provide a local mocked workflow or test suite that requires no cluster, with unmistakable non-live labels. Mount OpenSSH config read-only and/or agent socket explicitly; preserve known_hosts verification and user's identity. Never mount Docker socket into backend containers.

## Verification log

- Design stage: installed cluster Slurm help/manpage verified, QoS MaxJobs=1 and MaxSubmit=1 reconfirmed. No job submitted.
- KB baseline committed before delegation: `ca7a64c`. Three Luna subagents implemented core, remote runtime and API/deployment/client; coordinator reviewed integration separately.
- Full suite: `TMPDIR=/tmp/opencode just backend-test` — **108 tests passed**, including backend/client, one-command launcher, stdlib remote dispatcher/runtime, and optional isolated real-Redis checks. One upstream Starlette/httpx deprecation warning; no failed tests.
- Actual Redis integration found and fixed Lua cjson's conversion of empty arrays to objects. Real-Redis tests cover cancellation, CAS writes, checkpoint arrays and an API→controller rollover→worker restart→cancellation flow with a mocked scheduler.
- AOF restart test preserves job/idempotency state across two actual Redis processes. A cross-component test uses the real dispatcher, checkpoint files, restore validator and Redis, faking only Slurm and the ROS adapter.
- Backend OCI image built locally with Podman (`podman build -t localhost/dave-control:dev backend`), using Python 3.12 and `uv sync --locked --no-dev --no-editable`. It includes OpenSSH client and CA certificates.
- Container smoke test: image API + isolated real Redis; health, OpenAPI docs, creation, idempotency, listing and cancellation passed. No worker started, no credentials mounted, no cluster job submitted; test processes/containers stopped afterward.
- `just backend-client --help`, `uv lock --project backend --check`, shell syntax, `git diff --check` and unchanged `just --dry-run run` passed.
- One-command entry point `just run-cluster` added: initializes only a missing default `.env` (0600/current UID/GID), starts the local backend, checks API/Redis health and remote preflight, reuses a matching active session or creates one via the API. `--new` explicitly permits an additional session; `--desktop` waits for readiness and delegates to the existing SSH/noVNC client. A resolved custom environment-file path is passed consistently to Compose. Failed preflight never creates a new session; HTTP transport retries retain the same idempotency key; timeout/interrupt does not cancel work.
- Launcher validation: 19 mocked tests cover environment preservation, local API selection, readiness/retries, preflight failure, session reuse, explicit creation, uncertain-creation recovery and desktop handling. `just run-cluster --help` and recipe dry-run passed. The real `run-cluster` startup/submission command was deliberately not executed against the cluster.
- Actual container desktop-component smoke test (Noetic/Focal apt stage): private Xauthority, Xvfb, Openbox, x11vnc requiring RFB password authentication and noVNC HTTP serving passed. No Gazebo/simulation started, no host browser opened, no SSH connection made. This exposed and fixed the absence of modern `novnc_proxy` in Focal: use its packaged foreground `websockify` instead.
- Rootless Podman mode-0600 bind-mount test passed using a dummy non-secret file. The Compose wrapper selects keep-id when Docker is emulated by rootless Podman; `DAVE_FORWARD_AGENT=1` explicitly opts into the SSH agent socket. No actual private keys were read during this test.
- Cluster image build: **passed**, catkin 100%, local-only with CPU quota 4, memory 8 GiB and catkin parallelism 2. Image `localhost/dave-cluster:dev`, ID `304eb31a3b9935c7a71825a2a60ff248bc2eeabda5330f8639ec68d867029b8a`, approximately 5.92 GB uncompressed. Initial vcstool HTTP/ref probing failures were resolved with exact-SHA shallow imports; an unnecessary NPS `nvidia-cuda` rosdep declaration was excluded for this OpenGL-only sensor path.
- Complete **real local OCI runtime smoke test passed twice**: original Gazebo GUI supervised inside Xvfb, real RGB/PointCloud2 health validation, pause/checkpoint/resume controls, planned USR1 checkpoint/shutdown, and explicit-state restore into a second fresh container. No host browser/window opened. The test exposed a real ROS name collision: the Gazebo client wrapper must receive `__name:=gazebo_gui` or it unregisters `/gazebo`; a regression test covers this.
- Smoke reports: `/tmp/opencode/dave-runtime-smoke-15d42a17-8f55-46ac-9f7a-d62a8d6c9f9c-{1,2}.log`. Shared smoke storage and both temporary containers were removed after validation. `cluster/tests/smoke_session.py` is the explicit opt-in test driver, not part of ordinary unit test discovery.
- Still **not verified**: OCI→SIF conversion under site policy; a real Slurm allocation and its signal delivery; compute-node SSH/ProxyJump desktop access; NVIDIA rendering and performance. No real cluster job was submitted. Local OCI tests do not establish those capabilities, nor bitwise checkpoint fidelity.
