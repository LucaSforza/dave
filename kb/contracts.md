# Integration contracts (implementation agents)

## Python core (`backend/dave_backend/`)

Core owns `models.py`, `settings.py`, `store.py`, `transport.py`, `controller.py`, `service.py`, `worker.py`, `__init__.py`.

- `Settings()` uses `DAVE_` prefixed environment, including `redis_url`, `ssh_host` (default `cluster`), `remote_root` (default safe home-relative `dave-cluster`), `ssh_config`, optional `ssh_identity`, `poll_seconds`, command timeout. Expose settings for composition; no automatic SSH or submissions on import.
- `JobCreate` is a Pydantic model: scenario fixed `uw_lidar`, x/y/z/yaw (4,4,-93,-1.8), cpus 1..16 default16, gpus 0..2 default1, memory_gb positive default32, walltime_minutes 5..30 default30, auto_resume defaulttrue, max_allocations 1..4 default2. Forbid extra fields and nonfinite coordinates. `JobRecord` serializable via `model_dump(mode="json")`.
- `RedisStore(settings.redis_url)` synchronously persists records; `get(id)`, `list()` newest first, `save(record)`, `create(record)` (atomic, no overwrite), idempotency support and renewable controller locking. Cancellation must not be lost to controller writes.
- `JobService(store)` exposes `create_job(JobCreate, idempotency_key=None) -> JobRecord`, `get_job(id) -> JobRecord|None`, `list_jobs() -> list[JobRecord]`, `cancel_job(id) -> JobRecord|None`. Creation only queues locally; it does not block on Slurm.
- `SSHTransport(settings).call(operation: str, payload: dict) -> dict`: bounded synchronous subprocess; JSON via stdin to `python3 <root>/runtime/dispatch.py <operation> --root <root>`. Strict operation allowlist, shell quoting, timeouts, meaningful exceptions. No automatic deployment or arbitrary execution endpoint.
- `ClusterController(store, transport, settings).tick()` processes bounded work and reconciles state. `python -m dave_backend.worker` runs the periodic loop.

## API composition (`backend/dave_backend/api.py`)

- `create_app()` with injectable/testable store/transport wiring. `app = create_app()` for Uvicorn; no SSH at import/startup.
- `GET /health`, `GET /cluster` (lightweight `preflight`), `POST /jobs` (202, optional `Idempotency-Key`), `GET /jobs`, `GET /jobs/{uuid}`, `POST /jobs/{uuid}/cancel`.
- `GET /jobs/{uuid}/logs?offset=0&limit=...`, `/artifacts`, `/desktop` (only current running allocation), `POST /jobs/{uuid}/control` (pause/resume/reset/checkpoint, bounded structured command).
- Artifact download by opaque artifact identifier/listed relative path only; no arbitrary cluster paths. Bound transfers for MVP, document limits.
- API uses `JobService` for mutations, SSH transport for bounded interactive operations. Worker is the only submission owner. Invalid input => 422, not found =>404, unavailable runtime=>409, cluster communication failure=>503, no traceback/secrets in responses.
- OpenAPI docs are the initial learning UI. Python client CLI uses HTTP, not direct Slurm submission.

## Remote JSON protocol (`cluster/runtime/dispatch.py`)

One operation argument plus `--root`; read one JSON object on stdin. Emit exactly one JSON response on stdout. Diagnostics to stderr. Use only stdlib on login. All paths confined beneath private job root; validate canonical UUID job IDs, positive allocation indices, numeric Slurm IDs and allowlisted controls.

Common payload: `{"job_id":"<uuid>","allocation_index":1}`. Store under `<root>/jobs/<uuid>/allocations/1/`. `request` contains `JobCreate` fields; shared image path configured in remote private `<root>/config.json`, never supplied as executable client input.

Operations:

- `preflight {}` => `{ "ok":bool, "checks":{}, "errors":[] }`; lightweight only, report image/runtime prerequisites, never build or run Gazebo.
- `submit {job_id, allocation_index, request, checkpoint?}` => `{ "status":"submitted"|"busy", "slurm_job_id":"123", "reason"?:str }`. Use `fcntl` lock, atomic receipt, deterministic job name `dave-<uuid>-<index>`; recover ambiguous submissions by querying current user jobs before retry. Refuse over-quota submission if any user job is still queued/running/completing. Submission script uses fixed executable/runtime paths; no user shell fragments.
- `inspect {job_id, allocation_index}` => `{ "scheduler_state":str, "slurm_job_id":str|null, "runtime":{ "ready":bool, "rollover":bool, "checkpoint":dict|null, "error":str|null, "desktop":dict|null } }`. Absence of allocation => NOT_SUBMITTED; absent queue with unavailable accounting => UNKNOWN. Read manifests boundedly.
- `cancel {job_id, allocation_index}` => `{ "status":"cancel_requested"|"not_found" }`; operate only on receipts owned by this logical job.
- `logs {job_id,allocation_index,offset,limit}` => `{ "text":str,"next_offset":int }`, byte offset, max 64 KiB/call.
- `artifacts {job_id}` => `{ "artifacts":[{"path":str,"size":int}] }` (bounded). `download {job_id,path}` => `{ "data_base64":str,"path":str }`, max 8 MiB/call; no symlink escapes or secrets.
- `control {job_id,allocation_index,action}` => validated action in pause/resume/reset/checkpoint, atomically queue command for compute runtime; no ROS imports/execution on login.
- `desktop {job_id,allocation_index}` => `{ "ready":bool,"node":str,"user":str,"port":int }`; only current running runtime descriptor. Tunnel SSH uses alias cluster as jump host, target `user@node`, forwarded endpoint compute loopback; do not bypass host verification.

Treat errors as `{"error":"...","code":"..."}` with nonzero process exit; transport must turn these into explicit failures, never fake success.

## Runtime manifest and checkpoints

- Runtime atomically writes `runtime.json` inside allocation directory: ready, rollover, checkpoint, desktop, error, timestamp. No credentials in public manifests/artifacts.
- Checkpoints live in `<job>/checkpoints/`; versioned complete manifests identify original job, scenario, allocation index, creation time, snapshot file/hash and `fidelity:"explicit_state_only"`.
- Restore only validated completed matching checkpoints. File writes temp+fsync+rename. Incomplete/unsupported/corrupt checkpoints must not silently restart from scratch.
- Runtime catches shell USR1/TERM and has a walltime safety timer; checkpointing runs on compute, independent of PC connectivity. No compute SSH calls from API callbacks or login dispatcher.
- ROS adapter initially snapshots RexROV pose/twist and available joint positions, pause state and configuration. It documents hidden state not covered (solver, controller integrators, plugin RNG, contacts, clock); reset/restore is not bitwise deterministic continuation.
