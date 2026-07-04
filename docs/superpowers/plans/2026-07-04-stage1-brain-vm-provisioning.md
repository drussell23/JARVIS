# Stage 1 — Brain-VM Provisioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provision the GCP CPU Brain VM as a warm standby per the approved relocation design (`docs/superpowers/specs/2026-07-03-gcp-orchestrator-relocation-design.md` §Staging item 1): a golden-image CPU node hosting the organism runtime at `/opt/trinity/jarvis`, booting the battle-test harness headless, reachable via the Stage-0 WS transport, discovered dynamically (no hardcoded IP). The Mac keeps running everything locally — Stage 1 proves the Brain CAN run there; Stage 2 moves the sensors/FSM.

**Architecture:** Reuse the entire J-Prime provisioning stack with a CPU variant: `gcp_compute_rest` (node insert/reap, SA/ADC token mint, scatter-gather zones), the golden-image bake pattern (`bake_jprime_golden_image.py` → new CPU-brain variant script sharing its helpers), `failover_lifecycle._race_node_ready` for discovery, mTLS from Stage 0 (`transport_security`) + the ephemeral `/32` firewall pattern from the J-Prime mesh. Lifecycle: ON-DEMAND-PER-SESSION default (`JARVIS_BRAIN_VM_PERSISTENT=false`), idle-shutdown + hard teardown at session end; every threshold/machine-size env-resolved.

**Tech Stack:** Python 3.9+ asyncio, existing `gcp_compute_rest.py` / `failover_lifecycle.py` / `transport/` subpackage / `bake_jprime_golden_image.py` helpers; gcloud golden image; `scripts/ouroboros_battle_test.py --production-soak --headless` as the Brain's runtime entry.

## User Mandates (verbatim constraints)

1. Root-Cause Only — this fixes the capacity/GIL/memory class structurally (BG pool scaled to VM cores on the Brain; local Mac caps untouched).
2. Architectural Purity — on-demand lifecycle default with `JARVIS_BRAIN_VM_PERSISTENT` opt-in; ALL sizing (`JARVIS_BRAIN_VM_MACHINE_TYPE`, default `e2-highmem-4`), idle timeout, runtime caps, tokens, endpoints resolved from env at runtime. Zero baked assumptions.
3. DRY — Stage-0 `StreamEventBroker`/`DistributedEventBus` transport + `IsomorphicEnv` config + `gcp_compute_rest` + golden-image bake + Reachability Racer. No new provisioning framework.
4. Bulletproof — $0-orphan guarantee (the failover lifecycle's reap discipline: teardown on every exit path incl. SIGTERM; reap-confirm attempts); mTLS-required WS (certless rejected); discovery re-resolves on every reconnect.

---

## Task 1: CPU-brain golden-image bake variant

**Files:** Create `scripts/bake_brain_golden_image.py` (imports/shares `bake_jprime_golden_image.py` helpers: `_run`, `_log`, `build_startup_script` pattern, `parse_validation_verdict` shape). Test: `tests/infra/test_brain_bake_script.py` (unit: startup-script content assertions only — no live GCP in unit tests).
**Shape:** CPU-only image (no NVIDIA/Ollama): base Debian/Ubuntu image + python3.11 + git + repo clone at `/opt/trinity/jarvis` (the IsomorphicEnv canonical path — REAL this time) + `pip install -r requirements` + a systemd unit `jarvis-brain.service` running `python3 scripts/ouroboros_battle_test.py --production-soak --headless --cost-cap <env> --max-wall-seconds 0` with env from `/etc/jarvis/brain.env` (written from instance metadata at boot — the J-Prime metadata pattern). Startup script asserts `/opt/trinity/jarvis/.git` exists and the transport module imports before marking ready (sentinel file, the bake's validation verdict pattern).
**Pins:** no literal project IDs/zones (env: `JARVIS_GCP_PROJECT`, `JARVIS_BRAIN_VM_ZONES`); image name `jarvis-brain-golden-<stamp>`.

## Task 2: Brain node lifecycle in `gcp_compute_rest`

**Files:** Modify `backend/core/ouroboros/governance/gcp_compute_rest.py` (add brain-node spec builder beside the existing L4 spec: machine type env `JARVIS_BRAIN_VM_MACHINE_TYPE` default `e2-highmem-4`, boot disk env-typed via existing `_boot_disk_type`, NO GPU/accelerator, labels `jarvis-role=brain`), reusing insert/poll/reap verbatim. Test: `tests/governance/test_brain_node_spec.py` (spec-dict assertions: machine type env-driven, no accelerators, label present, image family from env).
**Lifecycle:** `JARVIS_BRAIN_VM_PERSISTENT` (default false) + `JARVIS_BRAIN_VM_IDLE_SHUTDOWN_S` (default 1800) — idle shutdown implemented VM-side in the systemd unit (Task 1: self-shutdown when no WS peer for N seconds, N from metadata), harness-side hard teardown in Task 4.

## Task 3: Discovery + mTLS WS reachability

**Files:** Create `backend/core/ouroboros/governance/brain_discovery.py` (thin: `discover_brain_endpoint()` = `gcp_compute_rest` instance-list filtered by `jarvis-role=brain` label → candidates → `failover_lifecycle._race_node_ready`-style concurrent probe of the WS health surface → first healthy; re-resolve on every reconnect — NO caching of IPs beyond a single connection's lifetime). Ephemeral `/32` firewall: reuse the existing J-Prime mesh helper (`resolve_local_public_ip` + the firewall-rule create/delete pair in `gcp_compute_rest`/`isomorphic_a1_local.py:268` region — grep `delete_firewall_rule` for the create/delete API). mTLS material via instance metadata (server) + local env (client) using Stage-0 `transport_security` builders unchanged.
**Test:** unit — discovery returns raced-first-healthy from fakes; re-resolve on reconnect; no IP literal anywhere (extend the Task-8-style AST/no-hardcoded-endpoint invariant to `brain_discovery.py`).

## Task 4: Ignition driver — `scripts/ignite_brain_vm.py`

**Files:** Create `scripts/ignite_brain_vm.py` (mirrors `isomorphic_a1_local.py`'s teardown discipline): provision (or reuse if `JARVIS_BRAIN_VM_PERSISTENT` and one exists) → await ready sentinel → open `/32` firewall for the Mac's public IP → connect a `DistributedEventBus` client (Stage 0) over mTLS → run a validation exchange (publish N events Mac→Brain, assert echo/subscription both ways, Last-Event-ID replay across a forced reconnect) → teardown: firewall rule deleted + VM stopped/deleted per lifecycle flag, on EVERY exit path (finally + SIGTERM handler; reap-confirm via existing `_reap_confirm_attempts`).
**The load-bearing live test (Stage-1 acceptance):** the cross-host equivalent of Stage 0's loopback proof — publish on Mac, observe on Brain (and reverse), sever the WS mid-stream, reconnect via re-discovery, assert exact Last-Event-ID replay. Plus: certless client rejected (mTLS-required, cross-host). Plus: $0 check — `gcloud compute instances list` empty post-teardown.

## Task 5: Live-fire validation + ledger

Run `ignite_brain_vm.py` for real (operator-visible, cost-capped, on-demand lifecycle): capture the acceptance evidence (both-direction events, replay-exact, mTLS-required, teardown-$0), append to `.superpowers/sdd/progress.md`, update the relocation design doc's Stage-1 status. Post-merge follow-up ticket: Stage 2 (sensor/FSM migration) planning.

## Self-Review
- Mandate 2 (on-demand default, env-resolved): Tasks 1/2/4 knobs all env-driven, persistent opt-in. ✓
- Mandate 3 (DRY): zero new frameworks — bake helpers, compute REST, racer, Stage-0 transport, iso env path all reused. ✓
- Mandate 4: teardown-on-every-exit + reap-confirm + mTLS-required + re-discovery pinned in Task 4's acceptance. ✓
- Placeholders: Tasks specify exact files/envs/defaults; implementers ground-truth exact helper signatures per the established grep-first pattern (bake helpers at `bake_jprime_golden_image.py:92-298`, spec builder near the L4 spec, firewall pair near `isomorphic_a1_local.py:268`).
