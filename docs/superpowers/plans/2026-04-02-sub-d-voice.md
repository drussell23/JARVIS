# Sub-project D: The Voice — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire `say_fn` into GovernedLoopService so internal narrators speak, extend CommProtocol and VoiceNarrator to narrate Sub-project C gate events, and add narration templates for duplication/similarity/verify regression.

**Architecture:** Phase 1 adds `say_fn` parameter to GLS constructor and updates all production call sites. Phase 2 extends `emit_decision`/`emit_postmortem` with optional `target_files`, extends `VoiceNarrator._map_phase` routing, and adds narration templates. Phase 3 emits gate events from the orchestrator at the three Sub-project C insertion points.

**Tech Stack:** Python 3.12, asyncio, unittest.mock, pytest

**Spec:** `docs/superpowers/specs/2026-04-02-sub-d-voice-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `backend/core/ouroboros/governance/governed_loop_service.py` | Modify | Add `say_fn` parameter to `__init__` |
| `backend/core/ouroboros/governance/comm_protocol.py` | Modify | Add `target_files` to `emit_decision` + `emit_postmortem` |
| `backend/core/ouroboros/governance/comms/voice_narrator.py` | Modify | Extend `_map_phase` for gate routing |
| `backend/core/ouroboros/governance/comms/narrator_script.py` | Modify | Add 3 gate templates + required keys |
| `backend/core/ouroboros/governance/orchestrator.py` | Modify | Emit gate events at 3 insertion points |
| `unified_supervisor.py` | Modify | Pass `say_fn=safe_say` at 2 GLS construction sites |
| `trigger_ignition.py` | Modify | Import `safe_say`, pass to GLS |
| `tests/governance/test_daemon_narrator_wiring.py` | Create | All tests |

---

### Task 1: Wire `say_fn` into GovernedLoopService

**Files:**
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py`
- Create: `tests/governance/test_daemon_narrator_wiring.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/governance/test_daemon_narrator_wiring.py`:

```python
"""Tests for Sub-project D: DaemonNarrator wiring."""
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Task 1: GovernedLoopService say_fn wiring
# ---------------------------------------------------------------------------

def test_gls_accepts_say_fn():
    """GLS constructor accepts and stores say_fn."""
    from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopService
    mock_say = AsyncMock(return_value=True)
    gls = GovernedLoopService(say_fn=mock_say)
    assert gls._say_fn is mock_say


def test_gls_say_fn_default_none():
    """GLS defaults say_fn to None for tests/headless."""
    from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopService
    gls = GovernedLoopService()
    assert gls._say_fn is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/test_daemon_narrator_wiring.py -k "test_gls" -v`
Expected: FAIL — `__init__() got an unexpected keyword argument 'say_fn'`

- [ ] **Step 3: Add `say_fn` parameter to GLS `__init__`**

In `backend/core/ouroboros/governance/governed_loop_service.py`, find `GovernedLoopService.__init__` (search for `def __init__` inside `class GovernedLoopService`). The current signature is:

```python
    def __init__(
        self,
        stack: Any = None,
        prime_client: Any = None,
        config: Optional[GovernedLoopConfig] = None,
        active_brain_set: FrozenSet[str] = frozenset(),
    ) -> None:
```

Change to:

```python
    def __init__(
        self,
        stack: Any = None,
        prime_client: Any = None,
        config: Optional[GovernedLoopConfig] = None,
        active_brain_set: FrozenSet[str] = frozenset(),
        say_fn: Optional[Any] = None,
    ) -> None:
```

Then add `self._say_fn = say_fn` after `self._prime_client = prime_client` (around line 690):

```python
        self._stack = stack
        self._prime_client = prime_client
        self._say_fn = say_fn
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/test_daemon_narrator_wiring.py -k "test_gls" -v`
Expected: Both PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/governed_loop_service.py tests/governance/test_daemon_narrator_wiring.py
git commit -m "feat(governance): add say_fn parameter to GovernedLoopService

Internal narrators (ReasoningNarrator, PreActionNarrator, EmergencyProtocol)
use getattr(self, '_say_fn', None) which now finds the injected callable
instead of always getting None.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Update production call sites to pass `say_fn`

**Files:**
- Modify: `unified_supervisor.py` (2 sites)
- Modify: `trigger_ignition.py` (1 site)

- [ ] **Step 1: Update unified_supervisor.py — fallback path**

Search `unified_supervisor.py` for the first `GovernedLoopService(` call (around line 85020). It currently looks like:

```python
                self._governed_loop = GovernedLoopService(
                    stack=self._governance_stack,
                    prime_client=self._kernel_prime_client,
                    config=_loop_config,
                    active_brain_set=frozenset(),  # No handshake — gate disabled
                )
```

Before this block, add the `safe_say` import (with try/except):

```python
                _say_fn = None
                try:
                    from backend.core.supervisor.unified_voice_orchestrator import safe_say
                    _say_fn = safe_say
                except ImportError:
                    pass
```

Then add `say_fn=_say_fn` to the constructor call:

```python
                self._governed_loop = GovernedLoopService(
                    stack=self._governance_stack,
                    prime_client=self._kernel_prime_client,
                    config=_loop_config,
                    active_brain_set=frozenset(),
                    say_fn=_say_fn,
                )
```

- [ ] **Step 2: Update unified_supervisor.py — handshake path**

Search for the second `GovernedLoopService(` call (around line 87273). Apply the same pattern: import `safe_say` with try/except before the call, add `say_fn=_say_fn`.

The second site looks like:

```python
                                    self._governed_loop = GovernedLoopService(
                                        stack=self._governance_stack,
                                        prime_client=self._kernel_prime_client,
                                        config=_loop_config,
                                        active_brain_set=_handshake_brain_set,
                                    )
```

Add `say_fn=_say_fn` (reuse the same `_say_fn` variable if it's in scope, or re-import):

```python
                                    _say_fn_hs = None
                                    try:
                                        from backend.core.supervisor.unified_voice_orchestrator import safe_say as _safe_say_hs
                                        _say_fn_hs = _safe_say_hs
                                    except ImportError:
                                        pass
                                    self._governed_loop = GovernedLoopService(
                                        stack=self._governance_stack,
                                        prime_client=self._kernel_prime_client,
                                        config=_loop_config,
                                        active_brain_set=_handshake_brain_set,
                                        say_fn=_say_fn_hs,
                                    )
```

- [ ] **Step 3: Update trigger_ignition.py**

Search `trigger_ignition.py` for `GovernedLoopService(` (around line 264). Before it, add:

```python
    _say_fn = None
    try:
        from backend.core.supervisor.unified_voice_orchestrator import safe_say
        _say_fn = safe_say
    except ImportError:
        log.debug("safe_say not available — GLS narrators will be silent")
```

Then add `say_fn=_say_fn` to the constructor:

```python
    gls = GovernedLoopService(stack=stack, prime_client=prime_client, config=loop_config, say_fn=_say_fn)
```

- [ ] **Step 4: Commit**

```bash
git add unified_supervisor.py trigger_ignition.py
git commit -m "feat(boot): pass safe_say to GovernedLoopService at all production sites

Updates 2 sites in unified_supervisor.py (fallback + handshake paths)
and 1 site in trigger_ignition.py. Import is try/except so headless
environments gracefully degrade to silent narrators.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Extend CommProtocol with `target_files` parameter

**Files:**
- Modify: `backend/core/ouroboros/governance/comm_protocol.py`
- Modify: `tests/governance/test_daemon_narrator_wiring.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/governance/test_daemon_narrator_wiring.py`:

```python
# ---------------------------------------------------------------------------
# Task 3: CommProtocol target_files extension
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_emit_decision_target_files():
    """emit_decision includes target_files in payload when provided."""
    from backend.core.ouroboros.governance.comm_protocol import CommProtocol
    cp = CommProtocol(transports=[])
    # Spy on _emit
    emitted = []
    async def spy_emit(msg):
        emitted.append(msg)
    cp._emit = spy_emit

    await cp.emit_decision(
        op_id="op-1",
        outcome="blocked",
        reason_code="duplication",
        target_files=["module.py"],
    )
    assert len(emitted) == 1
    assert emitted[0].payload["target_files"] == ["module.py"]


@pytest.mark.asyncio
async def test_emit_postmortem_target_files():
    """emit_postmortem includes target_files in payload when provided."""
    from backend.core.ouroboros.governance.comm_protocol import CommProtocol
    cp = CommProtocol(transports=[])
    emitted = []
    async def spy_emit(msg):
        emitted.append(msg)
    cp._emit = spy_emit

    await cp.emit_postmortem(
        op_id="op-1",
        root_cause="verify_regression: pass_rate=0.85",
        failed_phase="VERIFY",
        target_files=["module.py"],
    )
    assert len(emitted) == 1
    assert emitted[0].payload["target_files"] == ["module.py"]


@pytest.mark.asyncio
async def test_emit_decision_without_target_files():
    """emit_decision without target_files does not include it in payload."""
    from backend.core.ouroboros.governance.comm_protocol import CommProtocol
    cp = CommProtocol(transports=[])
    emitted = []
    async def spy_emit(msg):
        emitted.append(msg)
    cp._emit = spy_emit

    await cp.emit_decision(
        op_id="op-1",
        outcome="applied",
        reason_code="success",
    )
    assert "target_files" not in emitted[0].payload
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/test_daemon_narrator_wiring.py -k "emit_decision or emit_postmortem" -v`
Expected: FAIL — `emit_decision() got an unexpected keyword argument 'target_files'`

- [ ] **Step 3: Add `target_files` to both emit methods**

In `backend/core/ouroboros/governance/comm_protocol.py`, modify `emit_decision` (search for `async def emit_decision`):

```python
    async def emit_decision(
        self,
        op_id: str,
        outcome: str,
        reason_code: str,
        diff_summary: Optional[str] = None,
        target_files: Optional[List[str]] = None,
    ) -> None:
```

Add to payload construction (after `if diff_summary is not None:`):

```python
        if target_files is not None:
            payload["target_files"] = target_files
```

Add `List` to the imports at the top of the file if not already present.

Similarly, modify `emit_postmortem` (search for `async def emit_postmortem`):

```python
    async def emit_postmortem(
        self,
        op_id: str,
        root_cause: str,
        failed_phase: Optional[str],
        next_safe_action: Optional[str] = None,
        target_files: Optional[List[str]] = None,
    ) -> None:
```

Add to payload construction (after `if next_safe_action is not None:`):

```python
        if target_files is not None:
            payload["target_files"] = target_files
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/test_daemon_narrator_wiring.py -k "emit" -v`
Expected: All 3 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/comm_protocol.py tests/governance/test_daemon_narrator_wiring.py
git commit -m "feat(comm): add optional target_files to emit_decision and emit_postmortem

Backward compatible — existing callers unaffected. VoiceNarrator uses
target_files[0] to populate {file} in narration templates.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Extend VoiceNarrator routing + narration templates

**Files:**
- Modify: `backend/core/ouroboros/governance/comms/voice_narrator.py`
- Modify: `backend/core/ouroboros/governance/comms/narrator_script.py`
- Modify: `tests/governance/test_daemon_narrator_wiring.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/governance/test_daemon_narrator_wiring.py`:

```python
# ---------------------------------------------------------------------------
# Task 4: VoiceNarrator routing + narration templates
# ---------------------------------------------------------------------------

def test_map_phase_duplication():
    """reason_code=duplication → 'duplication_blocked'."""
    from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator
    from backend.core.ouroboros.governance.comm_protocol import CommMessage, MessageType
    msg = CommMessage(
        msg_type=MessageType.DECISION,
        op_id="op-1",
        seq=1,
        causal_parent_seq=None,
        payload={"outcome": "blocked", "reason_code": "duplication"},
    )
    assert VoiceNarrator._map_phase(msg) == "duplication_blocked"


def test_map_phase_similarity():
    """reason_code=similarity_escalation → 'similarity_escalated'."""
    from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator
    from backend.core.ouroboros.governance.comm_protocol import CommMessage, MessageType
    msg = CommMessage(
        msg_type=MessageType.DECISION,
        op_id="op-1",
        seq=1,
        causal_parent_seq=None,
        payload={"outcome": "escalated", "reason_code": "similarity_escalation"},
    )
    assert VoiceNarrator._map_phase(msg) == "similarity_escalated"


def test_map_phase_verify_regression():
    """root_cause starting with 'verify_regression' → 'verify_regression'."""
    from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator
    from backend.core.ouroboros.governance.comm_protocol import CommMessage, MessageType
    msg = CommMessage(
        msg_type=MessageType.POSTMORTEM,
        op_id="op-1",
        seq=1,
        causal_parent_seq=None,
        payload={"root_cause": "verify_regression: pass_rate=0.85", "failed_phase": "VERIFY"},
    )
    assert VoiceNarrator._map_phase(msg) == "verify_regression"


def test_map_phase_generic_decision_unchanged():
    """Existing outcome routing still works for non-gate decisions."""
    from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator
    from backend.core.ouroboros.governance.comm_protocol import CommMessage, MessageType
    msg = CommMessage(
        msg_type=MessageType.DECISION,
        op_id="op-1",
        seq=1,
        causal_parent_seq=None,
        payload={"outcome": "applied", "reason_code": "success"},
    )
    assert VoiceNarrator._map_phase(msg) == "applied"


def test_narration_template_duplication():
    """duplication_blocked template renders with file context."""
    from backend.core.ouroboros.governance.comms.narrator_script import format_narration
    result = format_narration("duplication_blocked", {"file": "module.py", "op_id": "op-1"})
    assert result is not None
    assert "module.py" in result
    assert "duplicat" in result.lower()


def test_narration_template_verify():
    """verify_regression template renders with file and root_cause."""
    from backend.core.ouroboros.governance.comms.narrator_script import format_narration
    result = format_narration("verify_regression", {
        "file": "module.py",
        "root_cause": "pass_rate=0.85 < threshold=1.00",
        "op_id": "op-1",
    })
    assert result is not None
    assert "module.py" in result
    assert "pass_rate" in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/test_daemon_narrator_wiring.py -k "map_phase or narration_template" -v`
Expected: FAIL — `_map_phase` returns wrong values, templates don't exist

- [ ] **Step 3: Extend `_map_phase` in `voice_narrator.py`**

In `backend/core/ouroboros/governance/comms/voice_narrator.py`, find the `_map_phase` static method (search for `def _map_phase`). Replace the entire method:

```python
    @staticmethod
    def _map_phase(msg: CommMessage) -> str:
        """Map CommMessage type + payload to narrator script phase."""
        if msg.msg_type == MessageType.INTENT:
            return "signal_detected"
        elif msg.msg_type == MessageType.POSTMORTEM:
            root_cause = msg.payload.get("root_cause", "")
            if isinstance(root_cause, str) and root_cause.startswith("verify_regression"):
                return "verify_regression"
            return "postmortem"
        elif msg.msg_type == MessageType.DECISION:
            reason = msg.payload.get("reason_code", "")
            if reason == "duplication":
                return "duplication_blocked"
            if reason == "similarity_escalation":
                return "similarity_escalated"
            outcome = msg.payload.get("outcome", "")
            if outcome in ("applied", "validated"):
                return "applied"
            elif outcome == "blocked":
                return "approve"
            else:
                return "applied"
        return "signal_detected"
```

- [ ] **Step 4: Add templates to `narrator_script.py`**

In `backend/core/ouroboros/governance/comms/narrator_script.py`, add to the `SCRIPTS` dict (before the closing `}`):

```python
    "duplication_blocked": (
        "I blocked a code change for {file}. "
        "The generated code duplicated existing logic."
    ),
    "similarity_escalated": (
        "A code change for {file} has high overlap with existing code. "
        "Escalating for your review."
    ),
    "verify_regression": (
        "I rolled back a change to {file}. "
        "Post-apply verification failed: {root_cause}."
    ),
```

Add to `_REQUIRED_KEYS` dict:

```python
    "duplication_blocked":   ("file",),
    "similarity_escalated":  ("file",),
    "verify_regression":     ("file", "root_cause"),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/test_daemon_narrator_wiring.py -k "map_phase or narration_template" -v`
Expected: All 6 PASS

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/comms/voice_narrator.py backend/core/ouroboros/governance/comms/narrator_script.py tests/governance/test_daemon_narrator_wiring.py
git commit -m "feat(narrator): extend VoiceNarrator routing and templates for gate events

_map_phase now routes reason_code=duplication and similarity_escalation
to dedicated templates. POSTMORTEM with root_cause prefix 'verify_regression'
routes to verify_regression template. Three new narration templates added.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Emit gate events from orchestrator

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py` (3 insertion points)

- [ ] **Step 1: Emit DECISION for similarity escalation in GATE**

In `orchestrator.py`, find the similarity gate block (search for `GATE similarity escalation`). After `risk_tier = RiskTier.APPROVAL_REQUIRED`, add the emission:

Currently:
```python
                        if risk_tier is not RiskTier.APPROVAL_REQUIRED:
                            risk_tier = RiskTier.APPROVAL_REQUIRED
            except Exception:
```

Change to:
```python
                        if risk_tier is not RiskTier.APPROVAL_REQUIRED:
                            risk_tier = RiskTier.APPROVAL_REQUIRED
                        # Emit gate event for VoiceNarrator
                        try:
                            await self._stack.comm.emit_decision(
                                op_id=ctx.op_id,
                                outcome="escalated",
                                reason_code="similarity_escalation",
                                target_files=list(ctx.target_files),
                            )
                        except Exception:
                            pass
            except Exception:
```

- [ ] **Step 2: Emit POSTMORTEM for verify regression in VERIFY**

In `orchestrator.py`, find the verify regression block (search for `VERIFY regression gate fired`). Before `if _serpent: _serpent.update_phase("POSTMORTEM")`, add the emission:

```python
            # Emit gate event for VoiceNarrator
            try:
                _root_cause_display = _verify_error.replace("verify_regression: ", "", 1) if _verify_error.startswith("verify_regression:") else _verify_error
                await self._stack.comm.emit_postmortem(
                    op_id=ctx.op_id,
                    root_cause=f"verify_regression: {_root_cause_display}",
                    failed_phase="VERIFY",
                    target_files=list(ctx.target_files),
                )
            except Exception:
                pass

            if _serpent: _serpent.update_phase("POSTMORTEM")
```

- [ ] **Step 3: Emit DECISION for duplication block in VALIDATE caller**

Duplication failures return from `_run_validation` with `failure_class="duplication"`. The caller in the VALIDATE phase checks `validation.failure_class`. Find the block that handles non-retryable failures (search for `failure_class == "infra"`). There should be a similar check area. Add a specific duplication emission.

Find the block where validation fails and is NOT retried (the main candidate loop). After the ledger entry for a failed candidate (search for `candidate_validated` and `validation_outcome`), add:

```python
                # Emit gate event for duplication blocks
                if validation.failure_class == "duplication":
                    try:
                        await self._stack.comm.emit_decision(
                            op_id=ctx.op_id,
                            outcome="blocked",
                            reason_code="duplication",
                            target_files=list(ctx.target_files),
                        )
                    except Exception:
                        pass
```

Place this AFTER the ledger entry (the `await self._record_ledger(ctx, OperationState.GATING, {...})` block) and BEFORE the `if validation.passed:` check.

- [ ] **Step 4: Run all tests**

Run: `python3 -m pytest tests/governance/test_daemon_narrator_wiring.py tests/governance/test_duplication_checker.py tests/governance/test_similarity_gate.py tests/governance/test_verify_gate.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/orchestrator.py
git commit -m "feat(governance): emit CommProtocol events from Sub-project C gates

Similarity escalation emits DECISION(outcome=escalated, reason_code=similarity_escalation).
Verify regression emits POSTMORTEM(root_cause=verify_regression: ..., failed_phase=VERIFY).
Duplication block emits DECISION(outcome=blocked, reason_code=duplication).
All include target_files for VoiceNarrator {file} context.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Full regression and verify

**Files:** None (verification only)

- [ ] **Step 1: Run all Sub-project D tests**

Run: `python3 -m pytest tests/governance/test_daemon_narrator_wiring.py -v`
Expected: All tests PASS

- [ ] **Step 2: Run all governance tests**

Run: `python3 -m pytest tests/governance/ -v --timeout=30 2>&1 | tail -20`
Expected: No new failures

- [ ] **Step 3: Run intake tests**

Run: `python3 -m pytest tests/governance/intake/ -v --timeout=30 2>&1 | tail -10`
Expected: 114+ pass

- [ ] **Step 4: Final commit if fixups needed**

```bash
git add -u
git commit -m "fix(governance): address regression findings from narrator wiring

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```
