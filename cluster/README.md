# Remote runtime setup

The login dispatcher uses only Python 3.8+ stdlib. Upload `cluster/runtime/` and `cluster/image/` to a private shared directory, then copy `config.sample.json` to `<root>/config.json` and set the absolute prebuilt SIF path. Keep root/config/job data private (0700); never place VNC passwords, SSH credentials, or tokens in manifests or artifacts.

Build locally from the repository root or inside an allocated compute job, then
convert the OCI image to SIF locally or on an allocated node. Never build/convert
on login. The recipe fixes the Noetic base digest, DAVE source commit
`e54ea16bdaf7d6dbb466cf141846a03f284a44dd` and external commits in
`cluster/image/ros-noetic.repos`, and compiles a complete catkin workspace.
Individual apt versions are not all pinned (notably the Ubuntu repositories).

Exact-SHA shallow fetches avoid vcstool's failing remote-ref probes. The pinned
NPS package declares `nvidia-cuda` but builds no CUDA targets; this rosdep key is
explicitly skipped because SL3 uses the installed Velodyne OpenGL plugin, not the
CUDA toolkit. This is not a claim that NVIDIA rendering works on the cluster.
`use_nvidia` defaults false and software rendering is explicit; `--nv` is opt-in.
See `kb/implementation.md` for actual build/test evidence and remaining limits.

Requests default to 32 GiB; both API and dispatcher cap requests at 1024 GiB.
Actual schedulable memory depends on the selected node and site policy.

Configure backend remote root to this tree and install dispatcher/runtime scripts
there. `preflight` checks config, SIF, Slurm tools and container runtime; it does
not build or start Gazebo. Missing prerequisites/readiness fail visibly.

- ROS, Gazebo, Xvfb, WM, VNC, noVNC and teleop share one Singularity execution
  using `--contain`, a private per-allocation HOME and the shared runtime-root
  bind. The contained `/tmp` and X sockets are private to that container.
- Xvfb requires a private mode-0600 MIT cookie (`-auth`, `-nolisten tcp`), never
  `-ac`. All GUI processes receive `XAUTHORITY`; the cookie is excluded from
  artifacts. Focal's packaged `websockify` serves noVNC directly.
- Dynamic loopback ROS/Gazebo master ports avoid fixed-port collisions. ROS1
  listeners may still bind more broadly: this is a trusted-cluster-network
  assumption, not a multi-user security boundary. No firewall is modified.
- Desktop listeners bind loopback and require SSH ProxyJump through `cluster`,
  verified host keys and compute-node SSH permission. Remote port allocation can
  race; startup fails if a port is taken and never kills unrelated processes.
- The mode-0600 `.vnc-secret` password is read by x11vnc via `-passwdfile`, never
  placed in command-line arguments. Retrieve it only through authenticated SSH
  as owner; it is excluded from manifests, artifacts, downloads and API responses.

## Play controls

Open the forwarded noVNC URL to use the original Gazebo GUI. No joystick is required. The image opens an xterm teleop shortcut on the desktop. Alternatively, from an authenticated compute-node shell in the running allocation, run `source /opt/ros/noetic/setup.bash && source /opt/dave/devel/setup.bash && python3 /opt/dave/keyboard_teleop.py`. W/S translates forward/back, A/D yaws, R/F changes vertical velocity, space sends zero, and q exits. Commands are speed-bounded; the node publishes zero after 0.35 seconds without a key and again on exit. `/rexrov/cmd_vel` (`geometry_msgs/Twist`) was observed in the repository's local DAVE test report; no cluster motion test has been made.

Desktop shortcut: the image opens an xterm keyboard-teleop terminal on the original Gazebo desktop. Use backend pause/resume/reset controls; stop/cancel with the API's cancel operation. Command results (including failures) appear in the allocation runtime control-results manifest. These controls do not expose shell access through the API. Do not paste the private VNC password into tickets/logs. Artifact downloads are restricted to public `checkpoints/` and `artifacts/` files and reject dotfiles/symlinks. noVNC/VNC forwarding needs compute-node SSH access through `ProxyJump cluster`; otherwise desktop is unavailable.

Tests use fake Slurm commands and temporary directories only. They do not run ROS, Slurm, Singularity, Gazebo or test graphics/checkpoint fidelity.

In addition to those unit tests, the coordinator built the complete OCI image
locally and ran two real local-container smoke sessions (RGB/LiDAR health,
original Gazebo GUI in Xvfb, controls, checkpoint and restore). These passed;
they are not Slurm/SIF/NVIDIA validation. The explicit opt-in driver is
`cluster/tests/smoke_session.py`; details and evidence are in
`kb/implementation.md`. The GUI is a supervised process with its own ROS name;
closing the browser only detaches, but closing Gazebo itself ends the runtime.

## Deployment steps (explicit user operations)

Run these from the repository root. They are instructions, not operations
automatically performed by the API. First build the complete image **locally**:

```bash
docker build --build-arg BUILD_JOBS=2 -f cluster/image/Dockerfile -t dave-cluster:noetic .
docker save -o /tmp/opencode/dave-cluster.tar dave-cluster:noetic
```

Allow adequate local disk for the image and archive. The Python backend image is
separate and does not include Gazebo. If using Podman, the equivalent export is
`podman save --format docker-archive -o /tmp/opencode/dave-cluster.tar IMAGE_NAME`.

Provision a private directory and transfer files (no simulation/build on login):

```bash
ssh cluster 'umask 077; mkdir -p "$HOME/dave-cluster/runtime" "$HOME/dave-cluster/images"; chmod 700 "$HOME/dave-cluster"'
scp cluster/runtime/*.py cluster/runtime/*.sh cluster:~/dave-cluster/runtime/
scp cluster/config.sample.json cluster:~/dave-cluster/config.json
scp /tmp/opencode/dave-cluster.tar cluster:~/dave-cluster/images/
```

Set the `image` field in the remote private `config.json` to the absolute SIF path
under your own home directory. Do not overwrite an existing runtime/config that
has active sessions without first stopping them. The initial copy above is for a
new deployment; preserve existing config when upgrading.

If Singularity is unavailable locally, convert the archive only **inside an
explicit CPU allocation**, for example:

```bash
ssh cluster 'srun --partition=students --nodes=1 --ntasks=1 --cpus-per-task=4 --mem=8G --time=00:20:00 bash -lc '\''singularity build "$HOME/dave-cluster/images/dave-noetic.sif" "docker-archive://$HOME/dave-cluster/images/dave-cluster.tar"'\'''
```

This is a real compute job when executed. It requests no GPU and may wait in the
queue. Check site container-build policy and available space first; do not run
the conversion directly on login. Keep `use_nvidia=false` for the initial
software-rendered smoke test. `--nv` with Xvfb is **not** proof of hardware
acceleration; NVIDIA graphics support needs a separate validated rendering setup.

Once the runtime/image are prepared, start the local backend per
`backend/README.md`, run `just backend-client preflight`, and explicitly create a
single-allocation session before testing automatic renewal:

```bash
just backend-client create --json '{"gpus":0,"walltime_minutes":10,"max_allocations":1}'
just backend-client status JOB_UUID
just backend-client logs JOB_UUID
just backend-client desktop JOB_UUID
```

The safety timer reserves the last four minutes for saving/teardown, so a short
allocation has less interactive time. When ready, obtain the VNC password through
your configured SSH connection and enter it in noVNC, never in API parameters:

```bash
ssh cluster 'cat "$HOME/dave-cluster/jobs/JOB_UUID/allocations/1/.vnc-secret"'
```

Closing the browser or SSH desktop tunnel does not stop Slurm. Always cancel the
logical job when finished and confirm its terminal state. Desktop access also
requires compute-node SSH host-key trust and permission during the allocation;
if the site forbids this path, keep the API working and ask the administrator for
an approved desktop gateway instead of exposing ports publicly.
