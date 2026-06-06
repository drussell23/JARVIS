# Shadow Soak Runbook — Production Evidence Accrual (Slice 107)

**Purpose.** Launch O+V into full **Shadow Mode** so the advanced cognitive
substrates (Entropy Engine, Sleep Daemon, belief learning loop) and the OS-level
Docker runtime cage actively run and **accrue real wall-clock evidence** — while
the fail-closed graduation actuator generates receipts + advisories but **never
flips a master flag**. The human remains the sole actuator (§51.11.2).

> **The Shadow Invariant.** Every flag below is observational/advisory or fail-
> closed. `JARVIS_GRADUATION_SHADOW_MODE` is **TRUE** (the default; do NOT set it
> false). In shadow, AUTO_FLIP receipts go to the *shadow* ledger — the boot
> applier never reads them — so no OS-level master flip occurs. You un-shadow
> manually, later, only after the empirical threshold is undeniable.

---

## 0. One-time: provision the VERIFY sandbox image

The ephemeral VERIFY image is tiny (pytest stack only; the project code is mounted
read-only at run time) and cold-boots fast. Build it multi-arch (arm64 for the M1,
amd64 for GCP Spot VMs):

```bash
cd /Users/djrussell23/Documents/repos/JARVIS-AI-Agent
SBX=backend/core/ouroboros/governance/sandbox_profiles
docker buildx build --platform linux/arm64,linux/amd64 \
  -f "$SBX/Dockerfile.verify-sandbox" \
  -t jarvis-verify-sandbox:latest \
  --label org.jarvis.state-hash="$(python3 -c 'from backend.core.ouroboros.governance.image_provisioner import image_state_hash; print(image_state_hash())')" \
  "$SBX"
```

Or let the **Image Provisioning Daemon** do it automatically at boot (it rebuilds
only when `requirements-sandbox.txt` / `Dockerfile.verify-sandbox` change):

```bash
export JARVIS_IMAGE_PROVISIONER_ENABLED=1
```

> **Containerized VERIFY of the FULL governance suite** (not just self-contained
> probes) additionally needs a **project image** with the repo's import-chain deps
> (`backend.core.ouroboros.__init__` eagerly imports `aiohttp` + the engine chain).
> Build one from the full `requirements.txt` and point the sandbox at it:
> `export JARVIS_RUNTIME_SANDBOX_VERIFY_IMAGE=jarvis-project-sandbox:latest`. The
> generic light image runs arbitrary contained code + self-contained probes (the
> proven 8.8%-residual containment); the project image runs the real scoped suite.

---

## 1. Enable the substrates (shadow / observational)

```bash
# ── Cognitive nervous system (Slice 101) ─────────────────────────────
export JARVIS_COGNITIVE_BUS_ENABLED=1
export JARVIS_BELIEF_REVISION_ENABLED=1
export JARVIS_SLEEP_DAEMON_ENABLED=1            # off-hot-path consolidation + self-audit
export JARVIS_DOMAIN_ENTROPY_ENGINE_ENABLED=1   # proactive Shannon-entropy exploration
export JARVIS_ADVERSARIAL_AUTOBIOGRAPHY_ENABLED=1
export JARVIS_COUNTERFACTUAL_REHEARSAL_ENABLED=1
export JARVIS_PROOF_CARRIER_ENABLED=1
# load-shed stays advisory unless you opt the gate into enforce:
# export JARVIS_INTAKE_COGNITIVE_SHED_MODE=enforce

# ── Recursion bound (Slice 104) — default-ON safety gate, no action needed ──
# JARVIS_RECURSION_DEPTH_GATE_ENABLED defaults TRUE; JARVIS_MAX_RECURSION_DEPTH=3.

# ── OS-level runtime cage (Slice 105/106/107) ────────────────────────
export JARVIS_RUNTIME_SANDBOX_ENABLED=1
export JARVIS_RUNTIME_SANDBOX_BACKEND=container   # use the Docker bridge
# optional defense-in-depth seccomp profile (validate against your image first):
# export JARVIS_RUNTIME_SANDBOX_SECCOMP_PROFILE="$SBX/strict_runtime.seccomp.json"
# the VERIFY containment probe (operator-supplied for your image/suite):
# export JARVIS_RUNTIME_SANDBOX_VERIFY_PROBE="<python payload or test invocation>"

# ── Graduation engine in SHADOW (Slice 102/103) — the fail-closed actuator ──
export JARVIS_AUTONOMOUS_GRADUATION_ENGINE_ENABLED=1
# JARVIS_GRADUATION_SHADOW_MODE defaults TRUE — DO NOT set it false. Receipts go
# to the shadow ledger; NO OS-level flip. Leave the apply gate OFF:
# (do NOT set JARVIS_GRADUATION_OVERRIDE_APPLY_ENABLED)
```

---

## 2. Launch the soak

```bash
python3 scripts/ouroboros_battle_test.py \
  --cost-cap 0.50 --idle-timeout 600 --max-wall-seconds 2400 --headless -v
```

(Drop `--headless` for an interactive SerpentFlow REPL when stdin is a TTY.)

---

## 3. What to watch — the evidence trail

| Signal | Where |
|---|---|
| Shadow graduation receipts (would-flip, NOT flipped) | `.jarvis/graduation_shadow.jsonl` |
| SAFETY-tier advisories (operator-approval-only) | `.jarvis/graduation_advisories.jsonl` |
| Belief learning loop (falsified patterns) | `.jarvis/belief_revision_ledger.jsonl` |
| Self-audit (own-commit cage-bypass scan) | `.jarvis/adversarial_autobiography_ledger.jsonl` |
| Sleep consolidation / meta-prior | `.jarvis/sleep_consolidation_ledger.jsonl`, `.jarvis/meta_prior_ledger.jsonl` |
| Containment breaches at VERIFY | orchestrator log `CONTAINMENT BREACH at VERIFY ... quarantining` |
| Recursion-bound halts | risk-floor log `[RecursionGate] HALT ...` |
| Live decisions (SSE) | `GET /observability/stream` (set `JARVIS_IDE_STREAM_ENABLED=1`) |

---

## 4. The un-shadow decision (operator only — NOT automated)

After the empirical threshold is undeniable (per §41.6 / §51.7 Tier 5 — e.g. N
regression-free cadence sessions per flag, zero containment breaches, zero
recursion halts, clean shadow receipts over a sustained window), the operator —
and only the operator — un-shadows a specific STANDARD flag:

```bash
export JARVIS_GRADUATION_SHADOW_MODE=false          # authorize real overrides
export JARVIS_GRADUATION_OVERRIDE_APPLY_ENABLED=1   # let the boot applier flip
```

SAFETY/governance flags **never** auto-flip regardless — they remain advisory by
construction (the override-ledger tier gate). This is the zero-order-doll boundary:
O+V accrues the mathematical case for graduation; the human pulls the lever.
