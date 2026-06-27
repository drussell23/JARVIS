# Isomorphic Local Sandbox — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** A local harness that runs the full O+V A1 chain (boot → chaos-detect → dispatch → generate → apply → PR) under conditions **mathematically isomorphic** to the GCP soak node, so the "passes-unit-fails-live" integration bugs (the C+ execution blocker) surface locally in minutes for $0 instead of 50-min/$0.25 cloud discovery runs.

**Architecture:** A thin **composition layer** over proven infra (~90% reuse). Three pillars: (A) **path/env isomorphism** — force the live `/opt/trinity/jarvis` absolute path + cwd-mismatch + the live sandbox-prefix policy so path bugs can't hide; (B) **Synthetic Adversary** — a localhost provider proxy that deterministically injects the real DW/Prime failure taxonomy to test failover-trigger wiring in ms; (C) **autonomous telemetry** — on failure, dump FSM phase + memory pressure + causal-parent chain by reusing CommProtocol/MemoryPressureGate/SessionRecorder/autopsy.

**Tech stack:** Python 3.9+ async, aiohttp (already a dep), Docker (optional, via existing `container_sandbox`), no new heavy deps. M1-native: containers optional (a pure-process "isomorphic env" mode is the default; Docker mode is the strict-parity escalation).

## Global Constraints
- **No duplication — compose, don't rebuild.** Every pillar reuses a named existing module (mapped per task). New code is glue + the specific fidelity-forcing + the bug fixes.
- **No hardcoding.** The live root is parametrized via `JARVIS_IAC_REMOTE_ROOT` (default-shape `/opt/trinity/jarvis`, not a literal baked into product code); the parity shape constant `_PARITY_RELATIVE_SHAPE=("opt","trinity","jarvis")` already exists in `test_scoped_verify_parity.py` — reuse it.
- **Fail-soft telemetry, never block teardown** (the autopsy contract).
- **`from __future__ import annotations`**, async-first, env-var-driven, deterministic chaos (FakeClock — no real sleeps).
- **Naming truth:** there is no "Chronoflow" module — `comm_protocol.py` (`causal_parent_seq`) is the causal spine. "pre_oom_autopsy" = the autopsy protocol (`sovereign_sentinel.py` + `local_autopsy`). Build on the real names.

## Reuse Map (recon-confirmed, file:line)
- Live node contract: `scripts/sovereign_iac_hypervisor.py:build_startup_script` (`/opt/trinity/jarvis`, `JARVIS_IAC_REMOTE_ROOT`).
- Repo-root SoT: `backend/core/ouroboros/governance/workspace_resolver.py::resolve_repo_root()` (.git-anchored, cwd-independent). **Bug:** `GovernedLoopConfig.project_root` defaults to `os.getcwd()`.
- Fidelity gap: `test_runner._ALLOWED_SANDBOX_PREFIXES` whitelists `/tmp` → masks the wrong-root rejection. Parity fixture already exists: `tests/integration/test_scoped_verify_parity.py::parity_repo` + `test_hermetic_a1_matrix.py`.
- Container isolation: `backend/core/ouroboros/governance/container_sandbox.py::build_container_argv/run_in_container`.
- Orchestrator entry: `scripts/a1_live_fire_chaos_harness.py` (`--dry-run-local`), driving `chaos_injector_ast.py` + `ouroboros_battle_test.py` + `a1_graduation_auditor.py`.
- Provider seams (env-URL-swap): `DOUBLEWORD_BASE_URL`, `JARVIS_AEGIS_URL`, `REACTOR_CORE_API_URL`, `JARVIS_PRIME_URL`. Claude-no-Aegis → httpx.MockTransport.
- Failover signals: `provider_quarantine.ProviderHealthGradient.record_sweep/is_global_outage` (real, `candidate_generator.py:4431`); `provider_heartbeat.DWHeartbeat.is_degrading` (probe, injectable `probe_fn`).
- Chaos infra: `tests/adversarial/fault_injector.py` (FaultInjector + FaultType); `scripts/chaos_injector.py` (FakeClock + ChaosSchedule); `topology_sentinel.py:429-472` FailureSource taxonomy.
- Telemetry: `comm_protocol.py` (causal_parent_seq), `memory_pressure_gate.snapshot()`, `op_context.OperationPhase` + `a1_trace.py`, `battle_test/session_recorder.py::save_summary`, `a1_live_fire_chaos_harness.local_autopsy()`.

---

### Task 1 — Isomorphic environment context manager (path/env/policy parity)
**Files:** Create `backend/core/ouroboros/battle_test/isomorphic_env.py`; Test `tests/battle_test/test_isomorphic_env.py`.
**Produces:** `class IsomorphicEnv` (context manager) that, on enter, forces the live conditions a process would see on the node: (1) repo materialized/mounted at the parametrized live root (`JARVIS_IAC_REMOTE_ROOT`, default `/opt/trinity/jarvis`) — pure-process mode via a symlink/bind into a temp prefix that is NOT `/tmp`-whitelisted, Docker mode via `container_sandbox` `-v repo:/opt/trinity/jarvis`; (2) **cwd ≠ repo_root** (the live mismatch); (3) the live sandbox-prefix policy — DISABLE the `/tmp` whitelist (`_ALLOWED_SANDBOX_PREFIXES`) so a wrong-root is rejected exactly as live; (4) the node env var set (from `build_startup_script`). `mode: "process" | "container"` (process = fast M1 default; container = strict parity).
**Interfaces — Produces:** `with IsomorphicEnv(repo, mode=...) as env: env.root, env.run(cmd)`.
**TDD:** assert effective abs path == live shape; assert cwd != root inside; assert a path under the (now-un-whitelisted) `/tmp` is rejected by the test-runner policy; assert env vars match the node contract. RED→GREEN.

### Task 2 — Fix the repo_root injection bug the fidelity env now exposes
**Files:** Modify `backend/core/ouroboros/governance/governed_loop_service.py` (the `GovernedLoopConfig.project_root` default) + the scoped-verify `LanguageRouter` construction site; Test: reuse `tests/integration/test_scoped_verify_parity.py` (already reproduces it) + a new assertion.
**Design:** replace the `os.getcwd()` default for `project_root` with `workspace_resolver.resolve_repo_root()` (the .git-anchored, cwd-independent SoT); thread that root into every downstream `repo_root` consumer (the patch_benchmarker post-apply scoped-verify that run #13 found still constructs its own). No new resolver — wire the existing one everywhere.
**TDD:** run `test_scoped_verify_parity.py` under `IsomorphicEnv` (cwd≠root, no `/tmp` whitelist) → the "outside repo root" rejection that killed runs #12/#13 must NOT fire for a valid test file. This is the first live-fidelity bug closed locally.

### Task 3 — Synthetic Adversary proxy (deterministic provider chaos)
**Files:** Create `scripts/synthetic_adversary.py`; Test `tests/adversarial/test_synthetic_adversary.py`.
**Produces:** a localhost aiohttp server the harness points providers at via env-URL-swap. Reuses `tests/adversarial/fault_injector.py::FaultType` (extend with the DW HTTP taxonomy from `topology_sentinel.FailureSource`: LIVE_TRANSPORT/LIVE_HTTP_5XX/LIVE_HTTP_429/LIVE_PARSE_ERROR/LIVE_STREAM_STALL) + `scripts/chaos_injector.py::FakeClock/ChaosSchedule` for a deterministic timeline. Per-route programmable chaos: `adversary.schedule(route="background", at=t, fault=LIVE_TRANSPORT)`. Endpoints for DW (`/chat/completions`, `/models`), Prime, Reactor.
**TDD:** each FailureSource is emitted deterministically (503 storm, malformed JSON, socket-close, SSE stall, 429); the `/models` HeavyProbe path and the `/chat/completions` real-generation path are independently controllable (the crux of Task 4). RED→GREEN.

### Task 4 — Failover-trigger wiring proof + fix (closes run #11's BIG finding)
**Files:** Test `tests/adversarial/test_failover_trigger_wiring.py`; Modify the failover trigger consumer if the proof fails (likely `failover_lifecycle.py` / the awaken condition).
**Design:** drive the adversary so the **HeavyProbe `/models` passes** but **real `/chat/completions` generation dies on LIVE_TRANSPORT** (the exact run #11/#12 condition). Assert `ProviderHealthGradient.is_global_outage(route)` fires (because `record_sweep` saw rate==0) AND the failover awaken consumes THAT signal — not just the probe. **If awaken stays dormant (the documented bug), FIX the trigger to consume the real `record_sweep`/`live_transport` signal.** This is a real bug-fix gated by the existing failover flags.
**TDD:** probe-pass + generation-fail → outage detected + awaken fires; blip (single fail) → no spurious awaken (dormant-safe). Millisecond runtime via FakeClock.

### Task 5 — Autonomous failure telemetry capture
**Files:** Create `backend/core/ouroboros/battle_test/failure_telemetry.py`; Test `tests/battle_test/test_failure_telemetry.py`.
**Produces:** `capture_failure_telemetry(ctx, output_dir) -> Path` that, fail-soft, dumps a single artifact composing (reuse only): current `OperationPhase` + `a1_trace` hops, the `comm_protocol` causal-parent chain for the op, `memory_pressure_gate.snapshot()`, and writes via `SessionRecorder.save_summary(session_outcome="incomplete_kill", failure_telemetry=...)` + the `local_autopsy()` directory pattern. Wired into `IsomorphicEnv`'s failure path + the harness teardown.
**TDD:** force a failure → artifact contains FSM phase, memory snapshot, causal parent sequence; telemetry never raises / never blocks teardown (fail-soft contract). RED→GREEN.

### Task 6 — Full-chain local E2E driver + chaos-sequencing + intervention-lock scoping
**Files:** Create `scripts/isomorphic_a1_local.py` (the top-level driver); Modify `a1_graduation_auditor.py` (intervention-lock scope); Test `tests/integration/test_isomorphic_a1_e2e.py`.
**Design:** compose Tasks 1–5: under `IsomorphicEnv`, drive `a1_live_fire_chaos_harness` logic locally — **inject chaos POST-boot** and **touch the chaos file to fire `fs.changed`** (fixes run #12's pre-soak sequencing so dynamic scoping detects it) → scoped pytest detects the red test → dispatch → generate (Synthetic Adversary as DW, or real) → apply → PR-dry-run. **Scope the auditor's intervention-lock to the chaos-op lineage** (run #13: an unrelated APPROVAL_REQUIRED op must not fail graduation — only a human-gate on the CHAOS-REPAIR op should). On any failure → Task 5 telemetry.
**TDD:** the full chain runs GREEN locally end-to-end (chaos detected → repair dispatched → PR-dry-run produced) under isomorphic conditions; an unrelated Orange op does NOT trip the (now-scoped) intervention-lock. This is the deliverable: the A1 chain provable locally for $0.

---

## Self-Review
- Coverage: pillar A = Tasks 1,2,6; pillar B = Tasks 3,4; pillar C = Task 5. The fidelity gap (`/tmp` whitelist + cwd≠root) is forced in T1 and exercised in T2/T6. The failover-signal-mismatch (run #11) is T4. The chaos-sequencing (run #12) + intervention-lock (run #13) are T6.
- Type consistency: `IsomorphicEnv` (T1) is consumed by T2/T5/T6; `resolve_repo_root` (T2) is the single SoT; `synthetic_adversary` (T3) is driven by T4/T6; `capture_failure_telemetry` (T5) is called by T1/T6.
- After this lands: the disciplined endgame = get T6 GREEN locally, then **one** confirming cloud run → first autonomous PR → A1 gate passes → execution grade moves off C+.
