# Requirements and decisions

## User requirements

- Backend on the user's PC; FastAPI, Redis, worker and development tools managed with Docker.
- Use `uv` for Python dependency management, lockfile, tests and application commands (including backend Docker image). ROS Noetic runtime keeps distro Python/ROS packages for compatibility; do not replace its interpreter with modern Python.
- Existing `ssh cluster "command"` configuration supplies identity. No passwords/private keys in API requests, Redis, images or the repository.
- Original Gazebo GUI via remote desktop (no bespoke simulator rendering).
- First deliverable: learn, launch and interact with RexROV; inspect state/logs/artifacts; stop/cancel safely. PPO is a later adapter, not implemented training in this milestone.
- One logical session can span several Slurm allocations: save before walltime, restore in a successor allocation.
- Initial default: one node, one task, 16 Slurm CPUs, one GPU, 32 GiB RAM, 30 minutes; GPU maximum two. CPU counts are scheduler CPUs, not guaranteed physical cores. RAM configurable within physical/site limits; do not use `--mem=0` by default.
- Auto-resume bounded by `max_allocations` (default 2, maximum 4), configurable per request. No infinite re-submission, no attempts to bypass fair-share or site policy.
- Preserve existing `docker/` simulation files and `just run` behaviour.

## Safety and limitations

- Run workloads and build/conversion only on allocated compute nodes or locally. Login operations: lightweight probes, bounded metadata reads/writes, submission/status/cancellation and streaming file transfer only.
- Phase 1 implementation tests are local/mocked. No live cluster job submission or shared configuration changes by implementation agents.
- Serve API and Redis only on loopback; no unauthenticated public service. API defaults are single trusted local user, not a multi-tenant security boundary. Disable permissive CORS.
- API accepts structured validated operations, never arbitrary shell commands, launch filenames, or user-controlled Slurm directives.
- SSH uses subprocess argument lists, BatchMode, strict host verification, explicit timeouts and bounded output. JSON over stdin; quote trusted fixed remote executable paths separately. Never `shell=True` locally.
- Redis persists to a named volume with AOF. Persist metadata/state, not huge log streams or simulation artifacts. Logs/downloads are bounded and paths confined to the job root.
- QoS previously observed: one submitted/running job. Account-wide coordination must also detect external jobs and wait instead of over-submitting.
- Remote desktop and resume involve disconnections and queue delay, not a seamless 24/7 allocation.

## Deployment choices

- `backend/`: independent Python 3.11+ package `dave_backend`, tests and container stack.
- `cluster/`: standard-library remote dispatcher plus ROS/container runtime, separate from existing local setup.
- `client/`: lightweight Python HTTP client/CLI, optionally opens browser and SSH desktop tunnel. No new 3D renderer.
- Remote runtime defaults to SingularityCE with a prebuilt complete SIF and explicit shared job storage. No implicit image building on login.
- Gazebo desktop: Xvfb + lightweight window manager + x11vnc + noVNC inside a cluster-specific image. Software rendering is an explicit baseline, not claimed NVIDIA acceleration. Hardware rendering is an opt-in deployment prerequisite to validate later.
- noVNC/VNC bind loopback on compute node. Access via SSH jump host to the allocated compute node. Compute SSH permission/routing/host verification is a prerequisite; if unavailable, report the blocker, do not expose an unauthenticated public port.
