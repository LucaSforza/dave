---
name: project-kb
description: Maintain the DAVE Cluster Control knowledge base, architecture, Mermaid UML, interface contracts, and evidence. Use when changing backend, cluster runtime, desktop access, checkpoint lifecycle or client behaviour.
---

# Maintain the project KB

1. Read `kb/README.md`, the relevant requirements and `kb/contracts.md` before editing code. Use the KB for intended behaviour; use tests and source to establish implemented behaviour.
2. Inspect `git status --short` and the KB baseline diff with `git diff <baseline>^ <baseline> -- kb .agents/skills/project-kb`. Never discard unrelated local simulation work.
3. For an interface/lifecycle change, update the contract and relevant Mermaid class/sequence diagrams together with the implementation. Keep names aligned and diagrams valid Mermaid fences.
4. Separate verified evidence, design intent and untested assumptions. Mocked Slurm/Redis/ROS tests do not prove real GPU rendering, desktop connectivity or full checkpoint fidelity.
5. Record commands/tests and limitations under `kb/implementation.md`; link reports instead of duplicating mutable observations. Never include SSH keys, passwords, Redis dumps or desktop secrets.
6. Respect single-user local-only defaults, bounded allocation renewal, explicit cancellation precedence and the compute-only policy. No cluster job submission merely to update docs.
7. Before committing, run relevant tests and `git diff --check`, inspect staged files, and stage only the requested scope. Commit KB design before delegated implementation when that workflow is requested.
8. Delegated implementation prompts must identify the KB commit, exact owned paths, integration contracts and validation expectations. Ask agents to read the commit diff and report uncertainties instead of inventing live verification.
