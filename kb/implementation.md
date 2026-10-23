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
- Implementation results: pending.
