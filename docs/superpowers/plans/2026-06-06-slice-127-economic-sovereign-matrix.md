# Slice 127 — Economic Sovereign Matrix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the provider routing matrix un-killable under single-lane economic/transport failure: an economic exhaustion of ONE lane (Claude "credit balance too low" / DW 402) must never sticky-brick an op or the organism — it must isolate the broke lane, self-heal it after a recovery window, and route volume to the working lane.

**Architecture:** Compose existing components (do NOT rebuild). The classifier (`provider_retry_classifier.py`) learns to recognize economic blocks via the canonical `economic_router.is_hard_economic_block()` detector and maps them to a recoverable decision instead of permanent `TERMINAL_CONFIG`. The EXISTING per-Anthropic-lane self-healing breaker (`ClaudeCircuitBreaker` — CLOSED/OPEN/HALF_OPEN with recovery window) is taught to trip on economic exhaustion so future ops route around a broke lane and probe it back. The EXISTING Slice 83 DW live-transport streak/sever logic is composed with a recovery window so a severed DW lane auto-probes back instead of staying severed for the whole session.

**Tech Stack:** Python 3.11 (3.9+ compatible), asyncio, pytest/unittest, env-var-driven config (§33.1 default-FALSE masters), FlagRegistry registration, structured logging.

---

## CRITICAL CONTEXT — Verify-First Root-Cause Correction (READ FIRST)

The locked PRD blueprint (§51.11.D) **misdiagnosed** the root cause. Ground truth from the canonical `debug.log` of session `bt-2026-06-07-040933` (line 1592):

```
fallback_err_class=BadRequestError
fallback_err_msg=Error code: 400 - 'Your credit balance is too low to access the Anthropic API...'
slice7e_decision=terminal_config
```

- The breaker tripped **16×** on Claude's **HTTP 400 "credit balance too low"** (class `BadRequestError`, which is in `_TERMINAL_CONFIG_CLASSES`) → classified `TERMINAL_CONFIG` → per-op breaker sticky-bricks the op. It is **per-op** (`escalated_to_global=False`), not a GLOBAL breaker.
- The `/v1/models` **401** (blueprint's claimed cause) is a benign `SemanticTriage` warning (line 46). It NEVER reaches the generation breaker. The blueprint's directive #2 (quarantine the 401) addresses a non-problem and is **descoped**.
- DW independently failed background ops with `live_transport:RuntimeError` stream ruptures (Slice 83/84 issue).

**Operator authorization:** Option 1 (Corrected Superset). Build the real root fix (economic reclassification + per-provider isolation + self-healing) AND the sound structural directives, composing existing files.

**Divergence from blueprint, with rationale (compose, don't duplicate — operator's explicit mandate):**
- Blueprint directive #1 says "add `CircuitScope.PER_PROVIDER` to `circuit_breaker.py` + a new per-provider breaker registry." But a per-provider self-healing breaker **already exists** (`claude_circuit_breaker.py::ClaudeCircuitBreaker`). We **compose it** (teach it about economics) rather than build a parallel registry. We DO add the `PER_PROVIDER` enum value as the canonical lane tag (cheap, satisfies the letter), but the actual lane-health logic reuses the existing breaker.
- Blueprint directive #2 (401 health-probe quarantine): **descoped** (non-problem per ground truth). May be added later as belt-and-suspenders.
- Blueprint directive #3 (transient recovery + `EMPTY_RESPONSE`→`RETRY_TRANSIENT`): **kept** — the 0-token empty-response IS real (`stream_renderer.py:303`, `tokens=0 first_token_ms=-1`).

---

## Key Anchors (verified against the live tree)

| Component | File:Line | Role |
|---|---|---|
| Classifier `classify()` | `provider_retry_classifier.py:219` | maps failure → RetryDecision (4-value closed) |
| `_TERMINAL_CONFIG_CLASSES` | `provider_retry_classifier.py:144` | contains `BadRequestError` (the bug) |
| `_FAILURE_MODE_DEFAULT` | `provider_retry_classifier.py:203` | FailureMode.name → decision |
| Economic detector | `economic_router.py:66` `is_hard_economic_block()` | canonical "balance too low"/insufficient detector |
| Slice 7 breaker | `circuit_breaker.py` | per-op FSM, `CircuitScope` (L175), trip table (L795) |
| Claude lane breaker | `claude_circuit_breaker.py` | per-Anthropic-lane self-healing CLOSED/OPEN/HALF_OPEN |
| Classify call site | `candidate_generator.py:5676` | passes class+mode only (NOT message) |
| `should_allow_request` gate | `candidate_generator.py:4325` | dispatcher consults Claude breaker pre-call |
| DW Slice 83 isolation | `candidate_generator.py:~3383-3420` | live_transport streak + sever DW lane |
| DW rupture raise | `doubleword_provider.py:2377` `StreamRuptureError` | source of live_transport RuntimeError |
| Empty-response producer | `stream_renderer.py:303` | `tokens=0 first_token_ms=-1` log |
| §33.1 flag pattern | `autonomy_command_bus_bridge.py:120,725` | master_enabled + register_flags |

---

## File Structure

- **Modify** `backend/core/ouroboros/governance/provider_retry_classifier.py` — add economic recognition (Phase 1).
- **Modify** `backend/core/ouroboros/governance/candidate_generator.py` — pass message to classifier (Phase 1); feed economic signal to Claude breaker (Phase 2); DW lane recovery (Phase 3).
- **Modify** `backend/core/ouroboros/governance/claude_circuit_breaker.py` — economic-exhaustion trip + recovery (Phase 2).
- **Modify** `backend/core/ouroboros/governance/circuit_breaker.py` — add `CircuitScope.PER_PROVIDER` enum value (Phase 2).
- **Test** `tests/governance/test_provider_retry_classifier.py`, `tests/governance/test_economic_reclassification.py` (new), `tests/governance/test_claude_circuit_breaker_economic.py` (new), `tests/governance/test_circuit_breaker.py`, `tests/governance/test_dw_lane_recovery.py` (new).

---

## PHASE 1 — Economic Reclassification (the root fix) — EXECUTE NOW

**Why first:** Lowest blast radius (classifier is pure-data), highest value (it is THE root-cause fix for the 16 trips). Self-contained, independently shippable.

**Master flag:** `JARVIS_ECONOMIC_RECLASSIFY_ENABLED` — default **FALSE** per §33.1. When off, `classify()` is byte-identical to today (economic 400 → TERMINAL_CONFIG, the legacy path). The ignition soak (Phase 4) enables it. RetryDecision stays a 4-value closed taxonomy (economic → existing `TERMINAL_QUOTA`, **no new enum member** → the AST pin stays green).

**Decision mapping rationale:** Economic block → `TERMINAL_QUOTA` (not a new member). `TERMINAL_QUOTA` semantically IS "quota/economic refusal, may recover after window." In the Slice 7 breaker trip table, `TERMINAL_QUOTA` on the 1st hit → `RETRY_AFTER_BACKOFF` (recoverable), Nth-in-window → `OPEN_TERMINAL`. This is strictly better than `TERMINAL_CONFIG`'s immediate 1× sticky brick, and it is the signal Phase 2 escalates to the per-provider Claude breaker.

### Task 1.1: Economic reclassification helper (pure data)

**Files:**
- Modify: `backend/core/ouroboros/governance/provider_retry_classifier.py`
- Test: `tests/governance/test_economic_reclassification.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/test_economic_reclassification.py
"""Slice 127 Phase 1 — economic reclassification (root fix).

Ground truth: bt-2026-06-07-040933 line 1592 — Claude HTTP 400
"credit balance too low" (class BadRequestError) was classified
TERMINAL_CONFIG and sticky-bricked 16 ops. With the economic
reclassifier ENABLED, that exact message must classify as the
recoverable TERMINAL_QUOTA, never TERMINAL_CONFIG.
"""
from __future__ import annotations

import os
import unittest

from backend.core.ouroboros.governance.provider_retry_classifier import (
    RetryDecision,
    classify,
    economic_reclassify_enabled,
)

_CLAUDE_CREDIT_400 = (
    "Error code: 400 - {'type': 'error', 'error': {'type': "
    "'invalid_request_error', 'message': 'Your credit balance is too "
    "low to access the Anthropic API. Please go to Plans & Billing to "
    "upgrade or purchase credits.'}}"
)


class TestEconomicReclassification(unittest.TestCase):
    def setUp(self) -> None:
        self._prev = os.environ.get("JARVIS_ECONOMIC_RECLASSIFY_ENABLED")
        os.environ["JARVIS_ECONOMIC_RECLASSIFY_ENABLED"] = "true"

    def tearDown(self) -> None:
        if self._prev is None:
            os.environ.pop("JARVIS_ECONOMIC_RECLASSIFY_ENABLED", None)
        else:
            os.environ["JARVIS_ECONOMIC_RECLASSIFY_ENABLED"] = self._prev

    def test_claude_credit_400_is_quota_not_config_when_enabled(self) -> None:
        # The exact bt-2026-06-07-040933 signature: BadRequestError + the
        # credit-balance message. WITHOUT the message it (correctly) stays
        # TERMINAL_CONFIG; WITH the message + flag on, it's recoverable.
        decision = classify(
            "BadRequestError", "TIMEOUT",
            failure_message=_CLAUDE_CREDIT_400,
        )
        self.assertEqual(decision, RetryDecision.TERMINAL_QUOTA)

    def test_non_economic_badrequest_still_terminal_config(self) -> None:
        decision = classify(
            "BadRequestError", "TIMEOUT",
            failure_message="Error code: 400 - malformed tool schema",
        )
        self.assertEqual(decision, RetryDecision.TERMINAL_CONFIG)

    def test_disabled_flag_preserves_legacy_terminal_config(self) -> None:
        os.environ["JARVIS_ECONOMIC_RECLASSIFY_ENABLED"] = "false"
        decision = classify(
            "BadRequestError", "TIMEOUT",
            failure_message=_CLAUDE_CREDIT_400,
        )
        self.assertEqual(decision, RetryDecision.TERMINAL_CONFIG)

    def test_message_param_is_optional_byte_identical(self) -> None:
        # No failure_message → identical to the legacy 3-arg signature.
        self.assertEqual(
            classify("RateLimitError"), RetryDecision.TERMINAL_QUOTA,
        )
        self.assertEqual(
            classify(None, None, http_status=401),
            RetryDecision.TERMINAL_CONFIG,
        )

    def test_economic_reclassify_enabled_default_false(self) -> None:
        os.environ.pop("JARVIS_ECONOMIC_RECLASSIFY_ENABLED", None)
        self.assertFalse(economic_reclassify_enabled())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/test_economic_reclassification.py -q`
Expected: FAIL — `ImportError: cannot import name 'economic_reclassify_enabled'` (and `classify()` has no `failure_message` kwarg).

- [ ] **Step 3: Write minimal implementation**

In `provider_retry_classifier.py`:
- Add the master-flag reader (default-FALSE per §33.1):

```python
import os  # add to imports

_ECONOMIC_RECLASSIFY_ENV = "JARVIS_ECONOMIC_RECLASSIFY_ENABLED"


def economic_reclassify_enabled() -> bool:
    """Slice 127 — master gate for economic reclassification.
    Default FALSE per §33.1. When OFF, ``classify`` ignores
    ``failure_message`` economics → byte-identical to pre-Slice-127.
    NEVER raises."""
    try:
        raw = os.environ.get(_ECONOMIC_RECLASSIFY_ENV, "").strip().lower()
        return raw in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001
        return False
```

- Extend `classify()` signature with `failure_message: Optional[str] = None` and add a **priority-0** economic check (before the failure_class registry). Compose the canonical detector via lazy import to avoid cycles:

```python
def classify(
    failure_class: Optional[str],
    failure_mode: Optional[str] = None,
    *,
    http_status: Optional[int] = None,
    failure_message: Optional[str] = None,
) -> RetryDecision:
    # Priority 0 (Slice 127): economic block recognition. A provider's
    # "credit balance too low" / "insufficient funds" 400 is an ECONOMIC
    # refusal (recoverable after credits/window), NOT a permanent config
    # fault. Misclassifying it as TERMINAL_CONFIG sticky-bricked 16 ops
    # in bt-2026-06-07-040933. Gated; compose economic_router's canonical
    # detector (no duplicate marker table). NEVER raises.
    if failure_message and economic_reclassify_enabled():
        try:
            from backend.core.ouroboros.governance.economic_router import (
                is_hard_economic_block,
            )
            if is_hard_economic_block(failure_message) is not None:
                return RetryDecision.TERMINAL_QUOTA
        except Exception:  # noqa: BLE001 — failure-soft, fall through
            pass
    # ... existing Priority 1-4 unchanged ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/test_economic_reclassification.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Run the classifier regression suite (AST pins must stay green)**

Run: `python3 -m pytest tests/governance/test_provider_retry_classifier.py -q`
Expected: PASS (RetryDecision still 4 members; no new enum member added).

- [ ] **Step 6: Add `economic_reclassify_enabled` to `__all__` + register the flag**

Add `"economic_reclassify_enabled"` to the module `__all__`. Add a `register_flags(registry)` seeding `JARVIS_ECONOMIC_RECLASSIFY_ENABLED` (type bool, default "false", category "Routing"/"Resilience", posture_relevance RELEVANT) following the `autonomy_command_bus_bridge.py:725` pattern, wrapped in defensive try/except.

- [ ] **Step 7: Commit**

```bash
git add backend/core/ouroboros/governance/provider_retry_classifier.py tests/governance/test_economic_reclassification.py
git commit -m "feat(ouroboros): Slice 127 P1 — economic reclassification (credit-balance 400 → recoverable, not sticky config)"
```

### Task 1.2: Wire the error message into the breaker classify call site

**Files:**
- Modify: `backend/core/ouroboros/governance/candidate_generator.py:5676`
- Test: `tests/governance/test_economic_reclassification.py` (extend — integration shape)

- [ ] **Step 1: Write the failing test** — assert that when the fallback raises a credit-balance `BadRequestError`, the per-op breaker (with the flag on) does NOT go straight to `OPEN_TERMINAL` on the 1st hit (it backs off / retries instead). Use a focused unit harness around a `CircuitBreaker` fed `classify(..., failure_message=<credit msg>)`. (Detailed test authored at execution time against the real breaker API.)

- [ ] **Step 2: Verify it fails.**

- [ ] **Step 3: Minimal change** at `candidate_generator.py:5676`:

```python
_slice7e_decision = _slice7e_classify(
    failure_class=type(inner_exc).__name__,
    failure_mode=_inner_mode.name,
    failure_message=str(inner_exc),  # Slice 127 — economic recognition
)
```

- [ ] **Step 4: Verify it passes.**

- [ ] **Step 5: Run the candidate_generator-adjacent governance suite** to confirm no regression.

- [ ] **Step 6: Commit.**

---

## PHASE 2 — Per-Provider Economic Breaker (compose ClaudeCircuitBreaker)

**Goal:** Once a lane is economically exhausted, future ops route around it and it self-heals after a recovery window — composing the EXISTING `ClaudeCircuitBreaker` rather than building a parallel registry.

### Task 2.1: `CircuitScope.PER_PROVIDER` enum value (canonical lane tag)
- TDD: update `tests/governance/test_circuit_breaker.py` CircuitScope cardinality pin from 2→3 members; add `PER_PROVIDER = "per_provider"` to `circuit_breaker.py:175`. Verify the AST pin + full breaker suite green. Commit.

### Task 2.2: Economic-exhaustion trip on `ClaudeCircuitBreaker`
- TDD (`tests/governance/test_claude_circuit_breaker_economic.py`): add `record_economic_exhaustion(detail)` that trips OPEN after `JARVIS_CLAUDE_ECONOMIC_TRIP_THRESHOLD` (default 1 — economic block is immediately actionable) and self-heals via the existing `should_allow_request()` recovery-window → HALF_OPEN → `record_success()` path. Distinct counter from transport so the two health signals don't cross-contaminate. Default-FALSE master `JARVIS_CLAUDE_ECONOMIC_BREAKER_ENABLED`. Commit.

### Task 2.3: Wire the economic signal at the fallback site
- At `candidate_generator.py` fallback exception handling, when `classify(...)` returns `TERMINAL_QUOTA` AND `is_hard_economic_block(str(inner_exc))` is truthy for the Claude lane, call `get_claude_circuit_breaker().record_economic_exhaustion(...)`. The EXISTING `should_allow_request()` gate at `:4325` then routes future ops to DW. TDD the wiring with an injected breaker; commit.

---

## PHASE 3 — DW Transport Rupture Self-Healing

**Honest scoping (verify-first):** LLM completion streams CANNOT be byte-resumed mid-chunk — there is no provider API to resume a severed SSE completion from an offset. "Preserve context and resume the chunk" is therefore implemented as **request-level stateful retry + lane self-healing**, NOT byte-stream resumption. This will be stated plainly in the PRD; claiming mid-stream resume would be a false attestation.

### Task 3.1: Severed-DW-lane recovery window
- Today (Slice 83) a 3-streak `live_transport` failure **severs** the DW lane and stamps `dw_surface_health` TRANSPORT_DEGRADED so subsequent ops cascade straight to Claude (`candidate_generator.py:~3406`, `_note_dw_live_transport_degraded` at `:945`). The gap: once degraded, it can stay degraded for the session. TDD a recovery window (`JARVIS_DW_LANE_RECOVERY_S`, default e.g. 300s) after which the DW lane is probed back to CLOSED (one canary op), composing the existing `dw_surface_health` ledger — symmetric to `ClaudeCircuitBreaker`'s HALF_OPEN. Default-FALSE master. Commit.

### Task 3.2: `EMPTY_RESPONSE` → `RETRY_TRANSIENT` (blueprint directive #3, kept)
- The 0-token empty response (`stream_renderer.py:303`, `tokens=0 first_token_ms=-1`) currently becomes a silent empty-candidates failure. TDD: a producer-side `ProviderEmptyResponse` signal where 0-token/`first_token_ms=-1` is detected, classified `RETRY_TRANSIENT` (recoverable), routed into the existing OPEN_TRANSIENT→HALF_OPEN recovery. Add the mapping in `_FAILURE_MODE_DEFAULT` (dict key, not a new enum member → AST pin safe). Default-FALSE master. Commit.

---

## PHASE 4 — PRD Update + Ignition Soak

### Task 4.1: PRD §51.11.27
- Add §51.11.27 documenting the corrected Slice 127 (Economic Sovereign Matrix), the verify-first root-cause correction (blueprint §51.11.D was misdiagnosed — Claude 400 credit, not /v1/models 401), the compose-first divergence, and the honest "no byte-resume" scoping. Bump the PRD banner version. PR + squash.

### Task 4.2: Ignition soak
- Launch a production soak with `JARVIS_ECONOMIC_RECLASSIFY_ENABLED=1 JARVIS_CLAUDE_ECONOMIC_BREAKER_ENABLED=1` (+ DW lane recovery + empty-response masters), with an injected economic failure on one lane. **Verification gate:** the loop survives the single-lane economic failure (no sticky brick), routes to the working lane, and reaches a real `LAYER4:` line. Record the session id + receipt. (Requires a funded key on the working lane — operator-provisioned; honest caveat if unavailable in-session.)

---

## Self-Review

- **Spec coverage:** Phase 1 = economic reclassification (real root fix) ✓; Phase 2 = PER_PROVIDER isolation + self-healing (directive #1, composed) ✓; Phase 3 = transient recovery + EMPTY_RESPONSE (directive #3) + DW transport resilience (operator phase 2) ✓; Phase 4 = PRD + soak (operator phase 4) ✓. Directive #2 (401 quarantine) explicitly descoped with rationale ✓.
- **Closed-taxonomy safety:** economic → existing `TERMINAL_QUOTA` (RetryDecision stays 4-value, AST pin green); `CircuitScope` 2→3 requires the pin update (called out in Task 2.1).
- **No-duplication:** composes `economic_router.is_hard_economic_block`, `ClaudeCircuitBreaker`, Slice 83 `dw_surface_health` — no parallel detectors or registries.
- **§33.1:** every new master default-FALSE, byte-identical when off, FlagRegistry-registered.
- **Invariants:** secrets never logged (we only match on public error text, never keys); no hardcoded models (failover model from env); never bypass Aegis/the cage.
