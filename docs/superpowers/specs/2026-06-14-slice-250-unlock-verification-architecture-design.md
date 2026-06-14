# Slice 250 — Deterministic Hardware-Boundary Provider & Split-Brain CI

**Date:** 2026-06-14
**Author:** Derek J. Russell (design facilitated by Claude)
**Branch:** `topology/slice-250-unlock-verification`
**Status:** DESIGN — awaiting spec review before implementation
**Program context:** Step 1 of the sequenced "rebuild unlock verification, then harden runtime" program. This slice rebuilds the *verification architecture* so it produces an honest signal. Runtime hardening is explicitly deferred to later slices, after honest tests exist to drive it.

---

## 1. Problem Statement

`github.com/drussell23/JARVIS` (this repo; the local working dir is named `JARVIS-AI-Agent` but its git remote is `drussell23/JARVIS`) has **227 open issues**, ~225 of which are a single recurring auto-generated failure: `🚨 Critical: Unlock Test Suite Failed`, filed daily by CI from late April 2026 onward. They are one bug wearing a trench coat.

### Root cause (forensic)

The daily `complete-unlock-test-suite.yml` (cron `0 4 * * *`) runs in `integration` mode and calls two reusable workflows. In that mode the verification is **theater, not signal**:

1. The only job that runs real assertions (`test-mock-mode`) is gated `if test-mode == 'mock'` → **skipped** on the scheduled `integration` path.
2. The job that *does* run (`test-integration-mode`, `macos-latest`) contains a step literally commented **`# Script content would be same as above`** — it creates **no test script**, then runs `python3 <nonexistent> || true`. No unlock test executes. Pass/fail is incidentally driven by things like `pip install -r backend/requirements.txt` (the full ML stack) succeeding on a hosted runner.
3. It is conceptually impossible as written: "unlock my Mac screen" needs a real GUI login session, a microphone, an enrolled voiceprint, and a Keychain secret — **none exist on GitHub-hosted runners**. The workflow half-knows this (`can-test-real` gates a `self-hosted` job that never runs on schedule).
4. The failure handler calls `github.rest.issues.create(...)` **with no deduplication**, every day → ~225 phantom issues.

### What is genuinely real (do not disturb)

- `#33911` — OpportunityMinerSensor merkle integration gap (legitimate bug).
- `#26101` — Pass B graduation soak tracking issue (legitimate tracker).
- The `backend/voice_unlock/` runtime itself — a real, substantial 112-file subsystem. Its correctness is **not** assumed by this slice; honest tests built here will surface real defects for later hardening slices.

---

## 2. Goals & Non-Goals

### Goals

- **G1.** Establish a clean **dependency-inversion boundary** over every hardware/OS-coupled capability the *primary e2e-tested unlock path* touches, with a single config-driven composition root. No hardcoded `if CI:` branches.
- **G2.** Ship two providers: a *thin* `RealHardwareProvider` wrapping existing code, and a deterministic pure-software `MockHardwareProvider` whose `AudioCapture` is a **Deterministic Vector Injector** (runs the real ML pipeline against synthetic fixtures — no logic-gate bypass).
- **G3.** Replace the hollow workflows with an honest **split-brain CI**: Track A (cloud logic, `provider=mock`, real exit codes) live; Track B (sovereign hardware, `provider=real`, `self-hosted`+macOS) fully built but **dormant**.
- **G4.** **Eradicate** the phantom issues (purge-all-but-newest, repurpose newest as a Canonical Ledger), strip the blind shells, and replace the cron-blind issue creator with a **dedup-aware reporter**.
- **G5.** Prove the dual-case biometric math in CI: authorized probe **accepts**, imposter probe **rejects**.

### Non-Goals (discipline boundary)

- **N1.** Do **not** rewrite every hardware call site across all 8 files. Slice 250 wires the boundary into the *primary e2e path* and the call sites the e2e suite exercises; exhaustive migration is follow-on slices.
- **N2.** Do **not** harden / refactor the unlock *runtime* logic (anti-spoofing tuning, model swaps, etc.). That is the sequenced Phase 2 of the program.
- **N3.** Do **not** introduce any human biometric data into the repo.
- **N4.** No single bloated PR. Phases land as independent, reviewable commits.

---

## 3. Architecture

### 3.1 The seam — interface-segregated role Protocols

Define small `typing.Protocol` interfaces (one capability each — Interface Segregation), located in a new package `backend/voice_unlock/hardware/`:

| Protocol | Responsibility | Real impl wraps | Mock impl |
|---|---|---|---|
| `AudioCapture` | Acquire an audio frame/stream for verification | real mic capture path | **Deterministic Vector Injector** (streams fixture `.wav` bytes) |
| `PasswordStore` | Read/write the unlock secret | `KeychainService` (`keyring`) | in-memory keyring backend |
| `ScreenLockSensor` | Report locked/unlocked state | `screen_lock_detector.is_screen_locked()` | programmable state machine |
| `ScreenWaker` | Wake the display | `caffeinate -u` path | no-op recorder |
| `KeyEventSink` | Emit keystrokes (password typing) | `SecurePasswordTyper` CGEvent layer | no-op recorder (captures keystrokes for assertions) |
| `SubprocessExecutor` | Run an external command | `ScreensaverIntegration.AsyncSubprocessRunner` | scripted-response fake |

> **Reuse, do not duplicate.** Each real impl is a *thin adapter* over code that already exists (cited above). We are consolidating today's scattered `@patch` mocking (`test_integration.py` patches keyring, audio capture, liveness, subprocess, screen-state ad-hoc) into one coherent injectable object.

### 3.2 The bundle and composition root

```python
@dataclass(frozen=True)
class HardwareProvider:
    audio: AudioCapture
    passwords: PasswordStore
    screen_lock: ScreenLockSensor
    screen_waker: ScreenWaker
    key_events: KeyEventSink
    subprocess: SubprocessExecutor

def resolve(config: ProviderConfig | None = None) -> HardwareProvider: ...
```

`resolve()` is the **single decision point**. Selection is config-driven via `JARVIS_UNLOCK_HARDWARE_PROVIDER`:

- `real` → `RealHardwareProvider`
- `mock` → `MockHardwareProvider`
- `auto` (default) → `mock` when a CI/headless signal is present (`CI` env truthy, or no audio device + no display), else `real`.

`auto`-detection lives in **one** helper (`_detect_environment()`), not scattered. Env var overrides detection. Default behavior on a live Mac session is byte-for-byte unchanged (resolves to `real`, wrapping the existing code paths), so this slice is inert in production until the runtime is migrated to consume the provider.

### 3.3 The Deterministic Vector Injector (the "beefed-up" mock)

The `MockHardwareProvider.audio` implementation is **forbidden from short-circuiting any logic gate**. It does not return `None`, does not stub `verify_speaker`, does not fake a confidence score. It injects the raw bytes of a synthetic `.wav` fixture into the **real** feature-extraction → embedding → distance-matrix → threshold pipeline. The pipeline's genuine math decides accept/reject.

**Synthetic fixture matrix** (committed under `backend/voice_unlock/tests/fixtures/ci_voice/`, generated by a committed, reproducible generator script — TTS or public-domain/CC source, **no human biometric**):

| Fixture | Role | Speaker | Expected outcome |
|---|---|---|---|
| A — enrolled anchor | enrollment seed in the test profile DB | `ci-fixture-speaker` | n/a (baseline) |
| B — authorized probe | verification input, **same** synth speaker | `ci-fixture-speaker` | distance < threshold → **Unlock=True** |
| C — imposter probe | verification input, **different** synth speaker | `ci-imposter-speaker` | distance ≥ threshold → **Unlock=False** |

A test that only proves "B accepts" is insufficient — it could pass with an always-yes pipeline. The reject case (C) is mandatory.

### 3.4 Adaptive Quantization Execution Matrix (encoder parity under CI)

The injector feeds bytes into the **real** encoder. On a hosted runner there is no GPU/MPS and limited memory, so loading the full-precision production encoder would OOM or be unavailably slow. We do **not** answer this with an undefined "lightweight fallback" or a *different* model — that would decouple CI math from production math. Instead, a deterministic quantization matrix selects a **compressed derivative of the same encoder architecture**:

- **Same topology, compressed weights.** Track A loads an ONNX-compiled and/or `float16`-scaled derivative of the *primary* speaker encoder — identical layer topology, identical feature-extraction front-end, identical embedding dimensionality, identical distance metric. Only the numeric precision / runtime is reduced. The foundational architecture is never swapped.
- **One decision point, config-driven.** Selection lives in the same composition root as the provider: a `ModelExecutionConfig` resolved from `JARVIS_UNLOCK_ENCODER_EXECUTION ∈ {full, quantized, auto}`, default `auto` → `quantized` when CI/no-accelerator is detected (reusing `_detect_environment()`), else `full`. No scattered branching; production default is `full`.
- **Decision-equivalent parity, not bit-exactness.** Quantization introduces bounded numerical deviation; bit-identical output is neither claimed nor required. The contract is: the quantized encoder's embeddings stay within a **documented cosine-distance tolerance `τ`** of the full-precision encoder on the fixture set, AND the **accept/reject verdicts on fixtures B and C are identical** under both execution modes. A parity test asserts both (`cosine_drift(full, quantized, fixture) ≤ τ` and verdict equality). If a `full` baseline cannot be materialized in CI, the parity test is computed offline once and the reference embeddings/`τ` are committed as a fixture.
- **Quantized artifact provenance.** The compressed encoder is produced by a committed, reproducible export script (fp32 → ONNX/`float16`) from the named primary encoder, checked in under `backend/voice_unlock/tests/fixtures/ci_encoder/`. It is a derived artifact, not hand-authored weights.

---

## 4. Split-Brain CI

Replace `complete-unlock-test-suite.yml`, `unlock-integration-e2e.yml`, `biometric-voice-unlock-e2e.yml` with two honest tracks. The `.bak` files (107KB / 61KB) are deleted — they are dead weight.

### Track A — Cloud Logic Verification (LIVE)

- Triggers: `pull_request`, `push` to `main` (unlock paths), and the daily `schedule`.
- Runners: `ubuntu-latest` (+ optionally `macos-latest` for OS-specific code paths).
- Forces `JARVIS_UNLOCK_HARDWARE_PROVIDER=mock` (and `CI=true`).
- **Actually runs** the rehabilitated suites:
  - `backend/voice_unlock/tests/` unit suite (migrated to the provider).
  - The rehabilitated `unlock_integration_e2e_test.py` and `biometric_voice_e2e_test.py` (they exist at `.github/workflows/scripts/`; wire them to import the real modules under `provider=mock`).
  - The dual-case biometric matrix (§3.3).
- Real exit code. A genuine failure → the dedup reporter (§5), not a fresh issue.

### Track B — Sovereign Hardware Validation (DORMANT)

- Runners: `runs-on: [self-hosted, macOS]`.
- Forces `JARVIS_UNLOCK_HARDWARE_PROVIDER=real`.
- **Gating so it can never fail when no runner exists:** the job is conditioned on an explicit opt-in signal (repo variable, e.g. `vars.JARVIS_SELFHOSTED_MAC == 'true'`) **and** `runs-on: [self-hosted, macOS]`. With no self-hosted runner registered and the variable unset/false, the job's `if` evaluates false → it is **skipped**, not queued-and-failing. It therefore never blocks, never files issues. Attaching a Mac + setting the variable lights it up with zero workflow edits.
- Real Keychain entry + real screen lock/unlock; never runs on GitHub-hosted instances.

---

## 5. Issue Eradication & Truthful Telemetry

### 5.1 Purge (one-time, API-driven, after the filer fix is committed)

- Close **all but the newest** `🚨 Critical: Unlock Test Suite Failed` auto-issues with a short comment pointing to Slice 250. Performed via the GitHub MCP API (gh CLI has a TLS failure in this sandbox). Done **silently** (single batched comment, no @-mentions) to avoid notification flooding.
- **Repurpose the newest** such issue into the single **Canonical Hardware Integration Ledger** — retitle/relabel it; this becomes the rolling issue the dedup reporter updates.
- Leave `#33911` and `#26101` untouched.
- Ordering: fix the filer first (so no new phantom spawns mid-purge), then purge.

### 5.2 Dedup-aware reporter (replaces the cron-blind creator)

- Fires **only** on a real Track-A failure (not on schedule-by-default).
- Instead of `issues.create`, it **finds the Canonical Ledger issue by a stable marker** (label `unlock-ci-ledger` + a hidden HTML marker in the body), then **updates/reopens** it with the latest failing run link + timestamp. One issue, ever.
- The blind `python3 <nonexistent> || true` shells and the naive creator block are removed entirely.

---

## 6. Phased Commit Plan (no bloated PR)

Each phase is an independent, reviewable commit on `topology/slice-250-unlock-verification`. Earlier phases must be green before the next starts.

- **250.1 — Telemetry purge & filer teardown.** Add the dedup-aware reporter + Canonical Ledger plumbing; remove blind shells and the cron-blind creator. Then run the one-time API purge. *(Filer committed before purge executes.)*
- **250.2 — Hardware-boundary provider.** New `backend/voice_unlock/hardware/` package: Protocols, `HardwareProvider`, `resolve()`/`_detect_environment()`, `RealHardwareProvider` (thin adapters), `MockHardwareProvider` (incl. Deterministic Vector Injector). Synthetic fixture generator + fixtures A/B/C. Unit tests consolidated onto the provider. **Production default path unchanged** (`auto`→`real`).
- **250.3 — Split-brain workflows.** Replace the three workflows with Track A (live) + Track B (dormant); delete `.bak` files. Wire/rehabilitate the e2e scripts to the provider.
- **250.4 — Verification.** Prove Track A executes real assertions, the dual-case matrix passes (B accept / C reject), exit code is honest, and no phantom issue is created. Capture evidence.

---

## 7. Verification Criteria (definition of done)

- **V1.** `resolve()` returns `real` on a live Mac (no env override) and `mock` under `CI=true`; covered by unit tests. No production behavior change when the provider is unused.
- **V2.** `MockHardwareProvider.audio` injects fixtures through the real pipeline; the test asserts B → Unlock=True and C → Unlock=False. No mock returns a hardcoded verdict.
- **V3.** Track A runs on a PR and returns a real non-zero exit on an injected regression, and zero on green — verified by a deliberate temporary break.
- **V4.** Track B is **skipped** (not failed, not queued) on hosted runners with no self-hosted Mac + variable unset.
- **V5.** After the purge: only the Canonical Ledger (+ `#33911`, `#26101`, and any unrelated issues) remain among the formerly-phantom set; no new `🚨 Critical: Unlock Test Suite Failed` issue appears on the next scheduled run.
- **V6.** No human biometric data anywhere in the tree or git history.
- **V7.** Encoder parity: under `quantized` execution the cosine drift vs. the `full` reference stays ≤ `τ` on the fixture set, and fixtures B/C produce identical accept/reject verdicts under both `full` and `quantized` modes (computed in CI, or offline with committed reference if `full` can't be materialized on the runner).

---

## 8. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Biometric leak into a public repo | Synthetic fixtures only (§3.3); V6 gate; generator is reproducible and non-personal. |
| Mock becomes "theater 2.0" (hollow pass) | Injector forbidden from bypassing gates; mandatory reject-case (C); V2. |
| Track B silently failing when no runner | Skipped-by-`if`, not queued; V4. |
| Migrating call sites balloons the slice | N1 scope cap — only the primary e2e path this slice; `resolve()` defaults to `real` so unmigrated code is untouched. |
| Purge is irreversible | Done after filer fix; close (not delete) issues, so reversible by reopening; newest preserved as Ledger. |
| Full-precision encoder OOMs / is too slow on hosted runners | Adaptive Quantization Execution Matrix (§3.4): same-architecture ONNX/`float16` derivative, config-selected via `JARVIS_UNLOCK_ENCODER_EXECUTION`, with a committed parity test (cosine drift ≤ `τ` + identical B/C verdicts). No model-architecture swap. |
| Quantized CI encoder silently drifts from production math | Parity gate (V7): bounded cosine tolerance `τ` + verdict equality on fixtures B/C; derived artifact produced by a committed, reproducible export script. |

---

## 9. Reuse Map (build on, don't duplicate)

- `backend/voice_unlock/services/keychain_service.py` — `keyring`-backed; mock swaps the keyring backend in-memory.
- `backend/voice_unlock/services/screensaver_integration.py::AsyncSubprocessRunner` — reuse as the real `SubprocessExecutor` (already has timeout + circuit breaker).
- `backend/voice_unlock/secure_password_typer.py` (`TypingConfig`, CGEvent layer) — real `KeyEventSink`.
- `backend/voice_unlock/objc/server/screen_lock_detector.py::is_screen_locked` — real `ScreenLockSensor`.
- `backend/voice_unlock/tests/test_integration.py` synthetic-audio helpers — seed the fixture generator.
- `.github/workflows/scripts/{unlock_integration_e2e_test.py,biometric_voice_e2e_test.py,ci_auto_pr_manager.py}` — rehabilitate, don't rewrite.
- Env-flag convention `JARVIS_*` consistent with the repo's existing config culture.

---

## 10. Open Questions (resolved during brainstorming)

- Scope → **Both, sequenced**; this slice = verification rebuild only.
- Self-hosted runner → **none today**; Track B built but dormant.
- Issue purge → **close all but newest**; newest → Canonical Ledger; keep `#33911`, `#26101`.
- Fixture provenance → **synthetic** dual-case (A/B/C), no human biometric.
- Track B gating → external repo variable `vars.JARVIS_SELFHOSTED_MAC == 'true'` + `runs-on: [self-hosted, macOS]` → skipped (not queued) when absent.
- Encoder cloud execution → **Adaptive Quantization Execution Matrix** (§3.4): same-architecture compressed derivative, config-selected, decision-equivalent parity within tolerance `τ`. No model swap.

---

## 11. Implementation Status

- **250.1 — Telemetry Eradication & Canonical Ledger — CODE COMPLETE** (branch `topology/slice-250-unlock`).
  - `backend/voice_unlock/ci/`: `IssueClient` seam, pure `ledger.py` (selection + idempotent purge plan + structured schema w/ emoji state tags + Track A/B matrix), dedup-aware `ledger_reporter.py`, `GitHubRestClient` (injectable opener), dry-run-first `purge_phantom_issues.py`. 25 unit tests green.
  - Orchestrator workflow: blind `issues.create` replaced by the canonical-ledger reporter (no new phantoms spawn).
  - Independent final review: approved, no Critical/Important defects.
  - **Pending (operator):** one-time live purge of 217 phantom issues (keep newest #69488 → Canonical Ledger), run from a host terminal where outbound TLS works. V5 post-state verification follows the purge.
- **250.2 — Hardware-Boundary Provider** — next plan.
- **250.3 — Split-brain workflows** — pending.
- **250.4 — Verification** — pending.
