# Architecture and UML

## Components / class diagram

```mermaid
classDiagram
    class JobCreate {
        +scenario: uw_lidar
        +cpus: int = 16
        +gpus: int = 1
        +memory_gb: int = 32
        +walltime_minutes: int = 30
        +auto_resume: bool = true
        +max_allocations: int = 2
    }
    class JobRecord {
        +id: UUID
        +state: JobState
        +request: JobCreate
        +allocations: list
        +cancel_requested: bool
        +checkpoint: dict
    }
    class AllocationRecord {
        +index: int
        +slurm_job_id: string
        +state: string
        +submission_token: string
    }
    class JobService {
        +create_job(request, idempotency_key)
        +get_job(id)
        +list_jobs()
        +cancel_job(id)
    }
    class RedisStore {
        +create(record)
        +get(id)
        +list()
        +save(record)
        +lock()
    }
    class ClusterController {
        +tick()
        +reconcile(record)
    }
    class SSHTransport {
        +call(operation, payload) dict
    }
    class RemoteDispatcher {
        +submit()
        +inspect()
        +cancel()
        +logs()
        +artifacts()
        +control()
        +preflight()
    }
    class SessionRuntime {
        +launchDesktopAndGazebo()
        +checkpoint()
        +restore()
        +shutdown()
    }
    class SimulatorAdapter {
        +health()
        +pause()
        +resume()
        +reset()
        +saveExplicitState()
        +restoreExplicitState()
    }
    JobRecord *-- JobCreate
    JobRecord *-- AllocationRecord
    JobService --> RedisStore
    ClusterController --> RedisStore
    ClusterController --> SSHTransport
    SSHTransport --> RemoteDispatcher : OpenSSH / JSON
    RemoteDispatcher --> SessionRuntime : sbatch
    SessionRuntime --> SimulatorAdapter : ROS Noetic
```

## Launch and desktop sequence

```mermaid
sequenceDiagram
    actor User
    participant Client as Python client / Swagger
    participant API as FastAPI
    participant R as Redis
    participant W as Controller worker
    participant L as Login dispatcher (SSH cluster)
    participant S as Slurm
    participant N as Compute runtime
    User->>Client: Create RexROV session
    Client->>API: POST /jobs (validated parameters)
    API->>R: Persist logical job (idempotency key)
    API-->>Client: 202 + logical job id
    W->>R: Acquire renewable single-controller lock
    W->>L: JSON submit(logical id, allocation index)
    L->>S: Check user queue, sbatch --parsable
    S-->>L: Slurm job id or busy
    L-->>W: Durable idempotent submission receipt
    W->>R: Persist allocation and pending state
    S->>N: Allocate compute node
    N->>N: Launch container, desktop, ROS, gzserver/gzclient
    N->>N: Validate clock / sensor messages, publish readiness
    W->>L: inspect(logical id, allocation index)
    L-->>W: Scheduler state + runtime readiness
    W->>R: running only after runtime ready
    Client->>API: GET /jobs/id/desktop
    API-->>Client: Allocated node / loopback noVNC port
    Client->>N: SSH tunnel via cluster jump host
    User->>N: Browser noVNC: original Gazebo desktop
```

## Rollover sequence

```mermaid
sequenceDiagram
    participant S as Slurm
    participant N as Allocation n runtime
    participant F as Shared private job storage
    participant W as PC controller
    participant L as Login dispatcher
    participant R as Redis
    S->>N: USR1 before walltime (batch shell)
    N->>N: Pause, stop input, save explicit simulator state
    N->>F: Write versioned checkpoint atomically
    N->>F: Publish completed checkpoint manifest
    N->>N: Stop Gazebo + desktop gracefully
    N->>S: Exit successfully with rollover marker
    W->>L: Reconcile scheduler and runtime manifest
    L-->>W: Terminal allocation + valid checkpoint + rollover
    W->>R: waiting_resume (desktop unavailable)
    alt Resume enabled, not cancelled, allocation budget remains
        W->>L: Submit allocation n+1 after old job left queue
        L->>S: sbatch (same logical id, new allocation index)
        S->>N: Allocate possibly different compute node
        N->>F: Verify and restore checkpoint
        N->>F: Publish fresh desktop readiness
        W->>R: running + new endpoint
    else Budget exhausted or checkpoint invalid
        W->>R: completed or needs_attention with reason
    end
```

### Invariants

- A lost SSH response is not permission to blindly submit again: reconcile with deterministic job name/comment and shared submission receipt.
- Cancellation is durable and dominates automatic renewal.
- `squeue` absence alone does not prove success. Consult `sacct`/`scontrol` and runtime manifests; missing accounting becomes unknown/needs_attention, not completed.
- Never create allocation n+1 while allocation n is active/COMPLETING or any incompatible user job consumes the one-job submit quota.
- Redis/API restart must not erase logical sessions or duplicate a remote allocation.
