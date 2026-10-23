# DAVE Cluster Control — knowledge base

This KB is the design contract for a single-user FastAPI/Redis service running on the user's Linux PC. It submits DAVE/RexROV sessions through the existing OpenSSH alias `cluster`, never executes compute workloads on the login node, and exposes the original Gazebo desktop, not a new renderer.

## Read in order

1. [Requirements and decisions](requirements.md)
2. [Architecture and UML](architecture.md)
3. [API and integration contracts](contracts.md)
4. [Slurm lifecycle and checkpoint semantics](slurm-lifecycle.md)
5. [Implementation and validation plan](implementation.md)
6. [Guida ai concetti e ai primi esperimenti](learning-guide.md)

## Evidence and scope

- Existing local tests: `docker/TEST_REPORT.md`.
- Previous read-only cluster inventory: `docker/CLUSTER_ASSESSMENT.md`.
- Slurm 24.11.1 `sbatch --help` and `man sbatch` were checked over SSH on 2026-10-23; no jobs were submitted for this design.
- No cluster desktop, NVIDIA renderer, or simulator checkpoint restore has yet been verified live.
- Keep evidence separate from design intent. Passing mocked tests does not establish live cluster compatibility.

## Updating the KB

Use `.agents/skills/project-kb/SKILL.md`. Update the relevant contract and diagrams whenever behaviour changes; record verification evidence and unresolved limits. Implementation agents must first inspect the KB commit with `git show --stat <commit>` and `git diff <commit>^ <commit> -- kb .agents/skills/project-kb`.
