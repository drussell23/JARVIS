---
title: Sovereign Ephemeral Self-Termination Matrix (PR #69636 MERGED, main ee0e5d8de5, 2026-06-21)
modules: []
status: merged
source: project_sovereign_self_termination.md
---

# Sovereign Ephemeral Self-Termination Matrix (PR #69636 MERGED, main ee0e5d8de5, 2026-06-21)

**Why:** Operator: "an external watcher script that kills the node is a brittle crutch; the organism must manage its own death." Replace external/manual teardown with in-organism programmatic cost control.

**`sovereign_self_termination.py` (NEW):** `trigger_self_termination(pr_url)` runs the self-destruct sequence the instant a [SOVEREIGN GRADUATION] PR opens:
1. `mark_terminal_success(pr_url)` — atomic `.jarvis/SOVEREIGN_TERMINAL_SUCCESS` sentinel (state=TERMINAL_SUCCESS + pr_url + ts).
2. `flush_state_vault()` — final sync GCS push (reuses `state_persistence_daemon._gcs_push_blocking(_src(),_target())` — immortalizes the victory).
3. `sever_compute()` — DELETE this GCE VM via **metadata-server SA token + Compute REST API over stdlib urllib** (lean image has NO gcloud CLI; no new deps). Resolves project/zone/name from `http://metadata.google.internal/computeMetadata/v1/instance/...`, gets SA OAuth token, `DELETE https://compute.googleapis.com/compute/v1/projects/{p}/zones/{z}/instances/{n}`.

Gated **default-OFF** `JARVIS_SOVEREIGN_SELF_TERMINATE_ENABLED` (self-deleting the host is destructive — only the ephemeral crucible overlay opts in). Fires ONLY on a genuine pr_url (never errors). Idempotent (sentinel + `_fired` process guard). Fail-soft — NEVER raises, NEVER undoes a graduation; off-GCE/no-IAM → Spot max-lifetime backstop. Grace sleep (`JARVIS_SOVEREIGN_SELF_TERMINATE_GRACE_S` default 5s) before the delete so logs/flush settle.

**Wired:** `autonomous_graduation_engine._maybe_propose_source_pr._go()` captures the ProposalResult; on `proposed + pr_url` → `await asyncio.to_thread(trigger_self_termination, pr_url)`. compose `docker-compose.crucible.yml` opts the ephemeral node in. 8 tests (gate, idempotency, fail-soft off-GCE, sentinel write, exact self-delete REST URL/method/Bearer shape).

**IAM/scope confirmed sufficient (no infra change):** node SA `888774109345-compute@developer.gserviceaccount.com` has `roles/editor` (includes compute.instances.delete) + instance OAuth scope `cloud-platform` → the metadata-token Compute DELETE is authorized.

**Demo nuance:** a brand-new autonomous graduation in 1 cycle wasn't available (only JARVIS_COMMAND_BUS_BRIDGE_ENABLED eligible + it already has PR #69632; new flags need 3 clean soaks). So the self-termination was demonstrated by invoking the DEPLOYED matrix with the real PR #69632 URL (node flushed GCS + self-deleted via its own code) — the autonomous wiring fires unaided on future graduations. See [[project_gitops_identity_matrix]], [[project_cognitive_graduation_crucible]].
