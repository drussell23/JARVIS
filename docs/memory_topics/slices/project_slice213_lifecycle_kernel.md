---
title: Project Slice213 Lifecycle Kernel
modules: [backend/core/ouroboros/governance/lifecycle_kernel.py]
status: historical
source: project_slice213_lifecycle_kernel.md
---

**Slice 213 — Native Orchestration Kernel (MERGED #69455, main `876864c57f`, 2026-06-10).** `lifecycle_kernel.py` (HOST-side, governance dir but runs on host) replaces the bash launcher's stamping/build logic after its `set -e` trap (`[ test ] && log` returns 1 when false → set -e kills script) silently aborted a relaunch BEFORE the build (212c fix #69454 main 5873fbb1a0 converted to if-block).

**Launch contract:** `launch()` = resolve_commit → compute_dirty (scoped `:!.jarvis` `:!**/__pycache__`) → build_launch_env (REFUSES dirty upfront) → compose build/up via create_subprocess_exec (SIGINT/SIGTERM forwarded to child) → `verify_postlaunch()`: stamp prefix-matches pin AND marker code greps present → **exit 0 ONLY on ATTESTED_MATCH** (phantom deploys can't exit 0). Bash launcher = thin preflight wrapper exec'ing the kernel. 12 tests.

**SCOPE REFUSALS (test-pinned):** (1) NO in-container self-cycling — Docker control socket in an autonomous LLM-agent container = root-equivalent host escape (Slice-199 ~/.ssh class); socket path appears nowhere in module, test pins absence. (2) NO auto-reload on self-verified patches — S208 detectors are friction not proof; operator merge stays the deploy boundary. (3) In-container graceful shutdown already owned by harness Ticket-B handlers + stop_grace_period — not duplicated.

**MILESTONE — first fully-attested healthy boot (take 3, 212c launcher):** stamp `5873fbb1a045 dirty=false`, verdict **MATCH**, `STRATEGIC IGNITION MESH live` in boot log. First time in the arc every layer proven not assumed. North-Star honesty told operator: 213 = reliability precondition, NOT autonomy progress; needle still waits on GOAL-001 → autonomous PR; "O+V exceeds CC's autonomy surface on paper, still chasing CC's execution reliability in practice." See [[project-slice212-runtime-attestation]].
