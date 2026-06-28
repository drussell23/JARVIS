# A1 Cloud Confirm — Autonomous Launch Matrix

The runbook for the first autonomous-PR cloud confirm. Composes the existing
`a1_live_fire_chaos_harness.py --remote` (provision → real-provider soak →
chaos → A1 audit → black-box → **teardown-always**) with two hardening
gap-closers shipped here.

## Pre-flight gates (in order)

```bash
# 0. $0 wiring dry-run — proves the matrix end-to-end, spends nothing
python3 scripts/a1_live_fire_chaos_harness.py --dry-run-local --stub-soak

# 0.5 GAP-CLOSER Z — Cryptographic Pristine Lock (HARD GATE; rc!=0 ABORTS the launch)
python3 scripts/a1_zero_state_lock.py || { echo "NODE NOT PRISTINE — ABORT"; exit 1; }
#   Sweeps orphaned worktrees (reap_orphans), then computes a deterministic SHA256
#   over the ENTIRE working-tree state (HEAD + porcelain incl. untracked + worktree
#   list + on-disk .worktrees/ orphans + chaos-manifest presence) and asserts it
#   equals the COMPUTED pristine baseline for this HEAD. No hardcoded SHA. Fail-closed.
```

## Fire (only after BOTH PRs merged + gates green)

```bash
# 1. GAP-CLOSER T — start the resilient telemetry bridge in a SIDE terminal first.
#    Async fault-tolerant reconnect loop, byte-offset resume (no loss/dup across drops),
#    multiplexes [A1Trace] (cyan) / [Cortex]+HEDGE GOVERNOR (yellow) / LEDGER_TERMINAL
#    (green=applied, red=else) into one color-coded local stream. Read-only — a local
#    Ctrl-C never touches the remote soak.
python3 scripts/a1_telemetry_bridge.py --node "$A1_NODE" --session-id "$A1_SESSION"

# 2. The real cloud confirm (zero invented flags — all verified in source)
JARVIS_IAC_HYPERVISOR_ENABLED=1 \
JARVIS_PROVIDER_CLAUDE_DISABLED=true \          # DW-primary safety gate #1
JARVIS_DW_PRIMARY_OVERRIDE=a1-cloud-confirm \   # DW-primary safety gate #2
JARVIS_IAC_SOAK_GOLDEN_ENABLED=true \           # boot the pristine golden image
python3 scripts/a1_live_fire_chaos_harness.py \
  --remote --i-understand-this-spends-money \
  --cost-cap 2.00 --max-wall-seconds 2400 --seed 42 --strict
```

## Financial Dead-Man's Switch (already in the harness/hypervisor)

`sovereign_iac_hypervisor.py::burn_node()` runs in the orchestrator `try/finally`
(teardown-always) with four independent kill layers: local `gcloud delete
--delete-disks=all` · node-side dead-man self-delete (sentinel/idle) · Spot
`--delete-on-preempt` · GCP `--max-run-duration`. Plus `--cost-cap` (hard) and
the Slice-47 resource-zero wall-clock hard-kill thread (state-blind by
invariant). Confirm zero zombies after:

```bash
gcloud compute instances list --filter="name~'sovereign-sandbox-'"   # expect empty
```

## Success criterion

`fsm_classify_to_applied=true` + `twelve_flag_audit_passed=true` in the verdict
→ autonomous PR filed → **A1 gate passes** (raises the execution grade).
