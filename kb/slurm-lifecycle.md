# Slurm lifecycle and honest recovery

## Sources

- https://slurm.schedmd.com/sbatch.html (`--signal`, `--requeue`, `--time`, dependencies, `--parsable`). Online docs currently describe a newer Slurm than the cluster: prefer installed 24.11.1 manpages for supported flags.
- https://slurm.schedmd.com/scontrol.html (`requeue`, `show job`).
- https://slurm.schedmd.com/sacct.html (terminal state and exit code).
- Cluster's installed `man sbatch`, inspected read-only via SSH on 2026-10-23: `B:` signals only batch shell; default targets job steps, not shell; warning can arrive up to 60 seconds early.

## Strategy

Use `#SBATCH --signal=B:USR1@180` and `--time=00:30:00` (or shorter user limit), a shell trap and an independent compute-runtime safety timer. Shell must launch the runtime in background and wait so traps can run; do not block traps behind a foreground command. Send the signal to the supervised runtime, wait for checkpoint/shutdown and clean up child processes. Also save periodically (default 60 s) so abrupt node loss loses bounded recent progress.

`--requeue` does not save application state: the batch script runs again from its beginning. Do not assume time-limit exhaustion automatically requeues. MVP uses new allocations managed by PC controller, not automatic requeue. Each attempt is traceable under a stable logical job id.

Submit successor only after terminal scheduler state and a complete validated rollover checkpoint. A checkpoint alone may be periodic and must not cause renewal after normal completion or cancellation. Respect MaxSubmit=1: do not pre-submit dependent jobs while old job is active. Re-query all the user's jobs before submission. A queue wait is expected, never promise uninterrupted desktop continuity.

Budget: default at most 2 allocations, at most 4 configurable. Cancel sets durable desired state before scancel, disables renewal immediately. Once budget is exhausted retain checkpoint/artifacts and explain completion; unexpected timeout/node failure/missing checkpoint becomes needs_attention rather than silently looping. Redis outages, SSH loss and controller restart must not fabricate terminal states or duplicate submissions.

## Application vs process checkpoint

Slurm cannot automatically serialize Gazebo, X11, OpenGL, ROS graph and controller memory into a portable resumable snapshot. CRIU/DMTCP GPU/desktop checkpointing is outside scope. A Gazebo world log/rosbag is not a complete simulation checkpoint.

MVP recovery launches the same scene/container on a new node, pauses simulation, restores explicit RexROV pose/twist/joint configuration, validates sensors and reopens a new desktop. Exact simulation time, plugin internal RNG/integrators, dynamic object state and GUI layout may differ; all recovery metadata must report partial fidelity. Extend the adapter for manipulated objects if interaction requires it. PPO checkpoints later must include model, optimizer, normalization, RNG and iteration counters; training is not implemented now.
