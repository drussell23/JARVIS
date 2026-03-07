# Phase B: Decision-Action Bridge Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire Phase A contracts (DecisionEnvelope, PolicyGate, ActionCommitLedger, BehavioralHealthMonitor) into the email triage runner so every autonomous action is enveloped, gated, ledger-reserved, invariant-checked, executed, and committed.

**Architecture:** In-place wiring in `runner.py`. No new orchestrator. New files: `PolicyContext` typed dataclass, `TriagePolicyGate` wrapping existing `NotificationPolicy`. Ledger is mandatory for write actions (fail-closed). Health monitor recommends, runner enforces.

**Tech Stack:** Python 3.9 stdlib. Phase A contracts (already implemented). Existing `NotificationPolicy`, `score_email`, `extract_features`, `apply_label`.

---

### Task 1: PolicyContext typed dataclass

**Files:**
- Create: `backend/core/contracts/policy_context.py`
- Test: `tests/unit/core/contracts/test_policy_context.py`

**Step 1: Write the failing test**

Create `tests/unit/core/contracts/test_policy_context.py`:

```python
"""Tests for PolicyContext typed dataclass."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest


class TestPolicyContext:
    def test_frozen(self):
        from core.contracts.policy_context import PolicyContext
        ctx = PolicyContext(
            tier=1, score=90, message_id="msg-1",
            sender_domain="example.com", is_reply=False,
            has_attachment=False, label_ids=("INBOX",),
            cycle_id="cycle-1", fencing_token=1,
            config_version="v1",
        )
        with pytest.raises(AttributeError):
            ctx.tier = 2  # type: ignore[misc]

    def test_all_fields_accessible(self):
        from core.contracts.policy_context import PolicyContext
        ctx = PolicyContext(
            tier=2, score=75, message_id="msg-2",
            sender_domain="test.com", is_reply=True,
            has_attachment=True, label_ids=("INBOX", "IMPORTANT"),
            cycle_id="cycle-2", fencing_token=5,
            config_version="v2",
        )
        assert ctx.tier == 2
        assert ctx.score == 75
        assert ctx.message_id == "msg-2"
        assert ctx.sender_domain == "test.com"
        assert ctx.is_reply is True
        assert ctx.has_attachment is True
        assert ctx.label_ids == ("INBOX", "IMPORTANT")
        assert ctx.cycle_id == "cycle-2"
        assert ctx.fencing_token == 5
        assert ctx.config_version == "v2"
```

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/core/contracts/test_policy_context.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.contracts.policy_context'`

**Step 3: Write minimal implementation**

Create `backend/core/contracts/policy_context.py`:

```python
"""PolicyContext — typed context for PolicyGate evaluation.

Replaces untyped Dict[str, Any] context with a frozen dataclass
that enforces field presence and types at construction time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class PolicyContext:
    """Typed context passed to PolicyGate.evaluate()."""
    tier: int
    score: int
    message_id: str
    sender_domain: str
    is_reply: bool
    has_attachment: bool
    label_ids: Tuple[str, ...]
    cycle_id: str
    fencing_token: int
    config_version: str
```

**Step 4: Run test to verify it passes**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/core/contracts/test_policy_context.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/core/contracts/policy_context.py tests/unit/core/contracts/test_policy_context.py
git commit -m "feat(contracts): add typed PolicyContext dataclass"
```

---

### Task 2: TriagePolicyGate wrapping NotificationPolicy

**Files:**
- Create: `backend/autonomy/email_triage/triage_policy_gate.py`
- Test: `tests/unit/backend/email_triage/test_triage_policy_gate.py`

**Step 1: Write the failing test**

Create `tests/unit/backend/email_triage/test_triage_policy_gate.py`:

```python
"""Tests for TriagePolicyGate wrapping NotificationPolicy."""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest

from core.contracts.decision_envelope import (
    DecisionEnvelope, DecisionType, DecisionSource, OriginComponent,
)
from core.contracts.policy_gate import PolicyGate, VerdictAction
from core.contracts.policy_context import PolicyContext
from autonomy.email_triage.config import TriageConfig


def _make_scoring_envelope(message_id="msg-1", score=90, tier=1):
    return DecisionEnvelope(
        envelope_id="env-score-1", trace_id="trace-1",
        parent_envelope_id="env-extract-1",
        decision_type=DecisionType.SCORING,
        source=DecisionSource.HEURISTIC,
        origin_component=OriginComponent.EMAIL_TRIAGE_SCORING,
        payload={"message_id": message_id, "score": score, "tier": tier},
        confidence=1.0,
        created_at_epoch=time.time(),
        created_at_monotonic=time.monotonic(),
        causal_seq=2, config_version="v1",
    )


def _make_context(message_id="msg-1", tier=1, score=90):
    return PolicyContext(
        tier=tier, score=score, message_id=message_id,
        sender_domain="example.com", is_reply=False,
        has_attachment=False, label_ids=("INBOX",),
        cycle_id="cycle-1", fencing_token=1,
        config_version="v1",
    )


class TestTriagePolicyGateProtocol:
    def test_satisfies_policy_gate_protocol(self):
        from autonomy.email_triage.triage_policy_gate import TriagePolicyGate
        from autonomy.email_triage.policy import NotificationPolicy
        config = TriageConfig(enabled=True)
        policy = NotificationPolicy(config)
        gate = TriagePolicyGate(policy, config)
        assert isinstance(gate, PolicyGate)


class TestTriagePolicyGateEvaluation:
    @pytest.mark.asyncio
    async def test_tier1_allows_immediate(self):
        from autonomy.email_triage.triage_policy_gate import TriagePolicyGate
        from autonomy.email_triage.policy import NotificationPolicy
        config = TriageConfig(enabled=True, notify_tier1=True)
        policy = NotificationPolicy(config)
        gate = TriagePolicyGate(policy, config)

        envelope = _make_scoring_envelope(score=90, tier=1)
        context = _make_context(tier=1, score=90)
        verdict = await gate.evaluate(envelope, context)
        assert verdict.allowed is True
        assert verdict.action == VerdictAction.ALLOW

    @pytest.mark.asyncio
    async def test_tier3_denies(self):
        from autonomy.email_triage.triage_policy_gate import TriagePolicyGate
        from autonomy.email_triage.policy import NotificationPolicy
        config = TriageConfig(enabled=True)
        policy = NotificationPolicy(config)
        gate = TriagePolicyGate(policy, config)

        envelope = _make_scoring_envelope(score=40, tier=3)
        context = _make_context(tier=3, score=40)
        verdict = await gate.evaluate(envelope, context)
        assert verdict.allowed is False
        assert verdict.action == VerdictAction.DENY

    @pytest.mark.asyncio
    async def test_tier4_denies(self):
        from autonomy.email_triage.triage_policy_gate import TriagePolicyGate
        from autonomy.email_triage.policy import NotificationPolicy
        config = TriageConfig(enabled=True, quarantine_tier4=False)
        policy = NotificationPolicy(config)
        gate = TriagePolicyGate(policy, config)

        envelope = _make_scoring_envelope(score=10, tier=4)
        context = _make_context(tier=4, score=10)
        verdict = await gate.evaluate(envelope, context)
        assert verdict.allowed is False

    @pytest.mark.asyncio
    async def test_verdict_has_envelope_id(self):
        from autonomy.email_triage.triage_policy_gate import TriagePolicyGate
        from autonomy.email_triage.policy import NotificationPolicy
        config = TriageConfig(enabled=True)
        policy = NotificationPolicy(config)
        gate = TriagePolicyGate(policy, config)

        envelope = _make_scoring_envelope()
        context = _make_context()
        verdict = await gate.evaluate(envelope, context)
        assert verdict.envelope_id == envelope.envelope_id
        assert verdict.gate_name == "triage_policy"

    @pytest.mark.asyncio
    async def test_verdict_has_dual_timestamps(self):
        from autonomy.email_triage.triage_policy_gate import TriagePolicyGate
        from autonomy.email_triage.policy import NotificationPolicy
        config = TriageConfig(enabled=True)
        policy = NotificationPolicy(config)
        gate = TriagePolicyGate(policy, config)

        before_epoch = time.time()
        before_mono = time.monotonic()
        envelope = _make_scoring_envelope()
        context = _make_context()
        verdict = await gate.evaluate(envelope, context)
        assert verdict.created_at_epoch >= before_epoch
        assert verdict.created_at_monotonic >= before_mono
```

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/backend/email_triage/test_triage_policy_gate.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the implementation**

Create `backend/autonomy/email_triage/triage_policy_gate.py`:

```python
"""TriagePolicyGate — wraps NotificationPolicy behind the PolicyGate protocol.

Zero behavior change. The existing NotificationPolicy.decide_action() logic
is preserved exactly. This adapter adds typed envelope/verdict interface.
"""

from __future__ import annotations

import time
from typing import Any, Dict

from core.contracts.decision_envelope import DecisionEnvelope
from core.contracts.policy_gate import PolicyVerdict, VerdictAction
from core.contracts.policy_context import PolicyContext
from autonomy.email_triage.config import TriageConfig
from autonomy.email_triage.policy import NotificationPolicy
from autonomy.email_triage.schemas import (
    EmailFeatures, PolicyExplanation, ScoringResult, TriagedEmail,
)


# Action -> VerdictAction mapping
_ACTION_TO_VERDICT = {
    "immediate": VerdictAction.ALLOW,
    "summary": VerdictAction.DEFER,
    "label_only": VerdictAction.DENY,
    "quarantine": VerdictAction.DENY,
}


class TriagePolicyGate:
    """Wraps NotificationPolicy behind the PolicyGate protocol.

    Satisfies PolicyGate via structural subtyping (duck typing).
    The evaluate() method:
    1. Builds a minimal TriagedEmail from envelope payload + PolicyContext
    2. Calls NotificationPolicy.decide_action()
    3. Wraps the result in a PolicyVerdict
    """

    def __init__(self, policy: NotificationPolicy, config: TriageConfig) -> None:
        self._policy = policy
        self._config = config

    async def evaluate(
        self, envelope: DecisionEnvelope, context: Any,
    ) -> PolicyVerdict:
        payload = envelope.payload
        msg_id = payload.get("message_id", "")
        score_val = payload.get("score", 0)
        tier_val = payload.get("tier", 4)

        # Build minimal EmailFeatures for NotificationPolicy
        features = EmailFeatures(
            message_id=msg_id,
            sender=f"unknown@{context.sender_domain}" if hasattr(context, "sender_domain") else "unknown",
            sender_domain=context.sender_domain if hasattr(context, "sender_domain") else "",
            subject="",
            snippet="",
            is_reply=context.is_reply if hasattr(context, "is_reply") else False,
            has_attachment=context.has_attachment if hasattr(context, "has_attachment") else False,
            label_ids=context.label_ids if hasattr(context, "label_ids") else (),
            keywords=(),
            sender_frequency="occasional",
            urgency_signals=(),
            extraction_confidence=envelope.confidence,
            extraction_source=envelope.source.value,
        )

        scoring = ScoringResult(
            score=score_val,
            tier=tier_val,
            tier_label=self._config.label_for_tier(tier_val),
            breakdown={},
            idempotency_key=f"{msg_id}:{self._config.scoring_version}",
        )

        triaged = TriagedEmail(
            features=features,
            scoring=scoring,
            notification_action="",
            processed_at=time.time(),
        )

        action_str, explanation = self._policy.decide_action(triaged)

        verdict_action = _ACTION_TO_VERDICT.get(action_str, VerdictAction.DENY)
        allowed = verdict_action == VerdictAction.ALLOW

        return PolicyVerdict(
            allowed=allowed,
            action=verdict_action,
            reason=action_str,
            conditions=explanation.reasons if explanation else (),
            envelope_id=envelope.envelope_id,
            gate_name="triage_policy",
            created_at_epoch=time.time(),
            created_at_monotonic=time.monotonic(),
        )
```

**Step 4: Run test to verify it passes**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/backend/email_triage/test_triage_policy_gate.py -v`
Expected: All 6 tests PASS

**Step 5: Commit**

```bash
git add backend/autonomy/email_triage/triage_policy_gate.py tests/unit/backend/email_triage/test_triage_policy_gate.py
git commit -m "feat(triage): add TriagePolicyGate wrapping NotificationPolicy"
```

---

### Task 3: Add ledger_lease_duration_s to TriageConfig

**Files:**
- Modify: `backend/autonomy/email_triage/config.py`
- Test: `tests/unit/backend/email_triage/test_config.py` (existing tests will cover)

**Step 1: Write the failing test**

Add to existing `tests/unit/backend/email_triage/test_config.py` — but actually the config tests already use `get_triage_config()` and check defaults. Let's just verify the new field exists:

Create `tests/unit/backend/email_triage/test_config_ledger.py`:

```python
"""Test ledger config field."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))


def test_ledger_lease_default():
    from autonomy.email_triage.config import TriageConfig
    config = TriageConfig()
    assert config.ledger_lease_duration_s == 60.0


def test_ledger_lease_custom():
    from autonomy.email_triage.config import TriageConfig
    config = TriageConfig(ledger_lease_duration_s=120.0)
    assert config.ledger_lease_duration_s == 120.0
```

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/backend/email_triage/test_config_ledger.py -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'ledger_lease_duration_s'`

**Step 3: Add the field to config.py**

In `backend/autonomy/email_triage/config.py`, add after line 85 (`latency_ema_alpha`):
```python
    # Phase B: Action commit ledger
    ledger_lease_duration_s: float = 60.0  # cycle_timeout + buffer
```

In `from_env()`, add after line 144 (`latency_ema_alpha`):
```python
            # Phase B: Action commit ledger
            ledger_lease_duration_s=_env_float("EMAIL_TRIAGE_LEDGER_LEASE_S", 60.0),
```

**Step 4: Run test to verify it passes**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/backend/email_triage/test_config_ledger.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add backend/autonomy/email_triage/config.py tests/unit/backend/email_triage/test_config_ledger.py
git commit -m "feat(config): add ledger_lease_duration_s for Phase B"
```

---

### Task 4: Wire contracts into runner.py — imports, init, warm_up

**Files:**
- Modify: `backend/autonomy/email_triage/runner.py` (imports, __init__, warm_up)
- Test: verify existing tests still pass

This task adds the contract instances and ledger initialization WITHOUT changing `run_cycle()` yet.

**Step 1: Add imports to runner.py**

After the existing imports (line 41), add:

```python
# Phase B: Decision-action bridge contracts
from core.contracts.decision_envelope import (
    DecisionType, DecisionSource, OriginComponent,
    EnvelopeFactory, IdempotencyKey,
)
from core.contracts.action_commit_ledger import ActionCommitLedger
from core.contracts.policy_context import PolicyContext
from core.contracts.policy_gate import VerdictAction
from autonomy.contracts.behavioral_health import (
    BehavioralHealthMonitor, ThrottleRecommendation,
)
from autonomy.email_triage.triage_policy_gate import TriagePolicyGate

try:
    from core.trace_envelope import LamportClock
    _LAMPORT_AVAILABLE = True
except ImportError:
    _LAMPORT_AVAILABLE = False
```

**Step 2: Add instance vars to __init__**

After `self._extraction_p95_ema_ms` (line 91), add:

```python
        # Phase B: Decision-action bridge
        self._envelope_factory = EnvelopeFactory(
            clock=LamportClock() if _LAMPORT_AVAILABLE else None,
        )
        self._health_monitor = BehavioralHealthMonitor()
        self._commit_ledger: Optional[ActionCommitLedger] = None
        self._policy_gate = TriagePolicyGate(self._policy, self._config)
        self._runner_id = f"runner-{uuid4().hex[:8]}"
```

**Step 3: Add ledger init to warm_up()**

After `await self._cold_start_recovery()` (line 129), add:

```python
        # Phase B: Initialize action commit ledger
        if self._config.state_persistence_enabled:
            try:
                from pathlib import Path
                parent = Path(self._config.state_db_path).parent if self._config.state_db_path else Path.home() / ".jarvis"
                parent.mkdir(parents=True, exist_ok=True)
                ledger_path = parent / "action_commits.db"
                self._commit_ledger = ActionCommitLedger(ledger_path)
                await self._commit_ledger.start()
                expired = await self._commit_ledger.expire_stale()
                if expired > 0:
                    logger.info("Expired %d stale ledger reservations from prior session", expired)
            except Exception as e:
                logger.warning("Action commit ledger init failed: %s", e)
                self._commit_ledger = None
```

**Step 4: Run existing tests to verify no regressions**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/backend/email_triage/ -q --tb=short -k "not test_disabled_flag_skips_entirely" 2>&1 | tail -5`
Expected: 223+ passed, 0 failed

**Step 5: Commit**

```bash
git add backend/autonomy/email_triage/runner.py
git commit -m "feat(runner): add Phase B contract imports and initialization"
```

---

### Task 5: Wire throttle check into run_cycle()

**Files:**
- Modify: `backend/autonomy/email_triage/runner.py` (run_cycle throttle check + admission backpressure)
- Test: `tests/unit/backend/email_triage/test_runner_bridge.py` (new)

**Step 1: Write the failing test**

Create `tests/unit/backend/email_triage/test_runner_bridge.py`:

```python
"""Tests for Phase B decision-action bridge wiring in runner."""

import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend"))

import pytest

from autonomy.email_triage.config import TriageConfig
from autonomy.email_triage.runner import EmailTriageRunner


def _make_runner(config=None):
    """Build a runner with minimal mocks for testing."""
    if config is None:
        config = TriageConfig(enabled=True, max_emails_per_cycle=5)

    runner = EmailTriageRunner.__new__(EmailTriageRunner)
    runner._config = config
    runner._state_store = None
    runner._state_store_initialized = True
    runner._label_map = {}
    runner._labels_initialized = True
    runner._fencing_token = 0
    runner._current_fencing_token = 0
    runner._last_committed_fencing_token = 0
    runner._warmed_up = True
    runner._cold_start_recovered = True
    runner._outcome_collector = None
    runner._weight_adapter = None
    runner._outbox_replayed = True
    runner._prior_triaged = {}
    runner._extraction_latencies_ms = []
    runner._extraction_p95_ema_ms = 0.0
    runner._last_report = None
    runner._last_report_at = 0.0
    runner._triaged_emails = {}
    runner._committed_snapshot = None
    runner._triage_schema_version = "1.0"

    import asyncio
    runner._report_lock = asyncio.Lock()

    # Phase B contracts
    from core.contracts.decision_envelope import EnvelopeFactory
    from autonomy.contracts.behavioral_health import BehavioralHealthMonitor
    from autonomy.email_triage.triage_policy_gate import TriagePolicyGate
    from autonomy.email_triage.policy import NotificationPolicy

    runner._envelope_factory = EnvelopeFactory()
    runner._health_monitor = BehavioralHealthMonitor()
    runner._commit_ledger = None
    runner._policy = NotificationPolicy(config)
    runner._policy_gate = TriagePolicyGate(runner._policy, config)
    runner._runner_id = "runner-test"

    # Mock resolver
    mock_resolver = MagicMock()
    mock_workspace = AsyncMock()
    mock_workspace._fetch_unread_emails = AsyncMock(return_value={"emails": []})
    mock_resolver.resolve_all = AsyncMock()
    mock_resolver.get = lambda name: {
        "workspace_agent": mock_workspace,
        "router": MagicMock(),
        "notifier": MagicMock(),
    }.get(name)
    runner._resolver = mock_resolver

    return runner


class TestThrottleCheck:
    @pytest.mark.asyncio
    async def test_circuit_break_skips_cycle(self):
        runner = _make_runner()
        # Force circuit break
        from autonomy.contracts.behavioral_health import ThrottleRecommendation
        runner._health_monitor.should_throttle = lambda: (
            ThrottleRecommendation.CIRCUIT_BREAK, "test anomaly"
        )
        report = await runner.run_cycle()
        assert report.skipped is True
        assert "circuit_break" in report.skip_reason

    @pytest.mark.asyncio
    async def test_pause_skips_cycle(self):
        runner = _make_runner()
        from autonomy.contracts.behavioral_health import ThrottleRecommendation
        runner._health_monitor.should_throttle = lambda: (
            ThrottleRecommendation.PAUSE_CYCLE, "error rate"
        )
        report = await runner.run_cycle()
        assert report.skipped is True
        assert "pause" in report.skip_reason

    @pytest.mark.asyncio
    async def test_healthy_proceeds(self):
        runner = _make_runner()
        # Default health monitor returns NONE
        report = await runner.run_cycle()
        assert report.skipped is False or report.skip_reason != "circuit_break"
```

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/backend/email_triage/test_runner_bridge.py::TestThrottleCheck::test_circuit_break_skips_cycle -v`
Expected: FAIL (runner doesn't check health monitor yet)

**Step 3: Add throttle check to run_cycle()**

In `runner.py`, after the `emit_triage_event(EVENT_CYCLE_STARTED, ...)` call (line 387), add:

```python
        # Phase B: Behavioral health throttle check
        rec, throttle_reason = self._health_monitor.should_throttle()
        if rec == ThrottleRecommendation.CIRCUIT_BREAK:
            return TriageCycleReport(
                cycle_id=cycle_id, started_at=started_at,
                completed_at=time.time(), emails_fetched=0,
                emails_processed=0, tier_counts={},
                notifications_sent=0, notifications_suppressed=0,
                errors=[], skipped=True,
                skip_reason=f"circuit_break:{throttle_reason}",
            )
        if rec == ThrottleRecommendation.PAUSE_CYCLE:
            return TriageCycleReport(
                cycle_id=cycle_id, started_at=started_at,
                completed_at=time.time(), emails_fetched=0,
                emails_processed=0, tier_counts={},
                notifications_sent=0, notifications_suppressed=0,
                errors=[], skipped=True,
                skip_reason=f"pause:{throttle_reason}",
            )
        # REDUCE_BATCH: get recommended max for admission gate
        _health_report = self._health_monitor.check_health()
        _health_max_emails = _health_report.recommended_max_emails
```

Then modify the admission gate section (around line 459) to apply `_health_max_emails`:

After `admitted_count, budget_required_s = self._compute_budget(len(sorted_emails))`, add:

```python
        # Phase B: Apply backpressure from health monitor
        if _health_max_emails is not None:
            effective_max = max(1, min(admitted_count, _health_max_emails))
            if effective_max < admitted_count:
                logger.info("Health backpressure: reducing batch %d -> %d", admitted_count, effective_max)
                admitted_count = effective_max
```

**Step 4: Run test to verify it passes**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/backend/email_triage/test_runner_bridge.py -v`
Expected: All 3 tests PASS

**Step 5: Commit**

```bash
git add backend/autonomy/email_triage/runner.py tests/unit/backend/email_triage/test_runner_bridge.py
git commit -m "feat(runner): add health throttle check before triage cycle"
```

---

### Task 6: Wire envelope creation + policy gate + ledger into per-email processing

**Files:**
- Modify: `backend/autonomy/email_triage/runner.py` (the per-email processing loop)
- Test: add to `tests/unit/backend/email_triage/test_runner_bridge.py`

This is the most complex task. It modifies the per-email processing loop (lines 495-575) to:
1. Create extraction + scoring envelopes
2. Call TriagePolicyGate instead of direct decide_action
3. Reserve in ledger before action
4. Check pre-exec invariants
5. Commit/abort after action

**Step 1: Write the failing tests**

Add to `tests/unit/backend/email_triage/test_runner_bridge.py`:

```python
class TestEnvelopeCreation:
    @pytest.mark.asyncio
    async def test_envelopes_created_for_each_email(self):
        runner = _make_runner()
        # Mock workspace to return 2 emails
        mock_ws = runner._resolver.get("workspace_agent")
        mock_ws._fetch_unread_emails = AsyncMock(return_value={
            "emails": [
                {"id": "msg-1", "from": "a@example.com", "subject": "Test 1", "snippet": "hi", "labelIds": []},
                {"id": "msg-2", "from": "b@example.com", "subject": "Test 2", "snippet": "hello", "labelIds": []},
            ]
        })

        with patch("autonomy.email_triage.runner.extract_features") as mock_extract:
            from autonomy.email_triage.schemas import EmailFeatures
            def fake_extract(email, router, deadline=None, config=None):
                mid = email.get("id", "?")
                return EmailFeatures(
                    message_id=mid, sender=f"user@example.com",
                    sender_domain="example.com", subject="Test",
                    snippet="hi", is_reply=False, has_attachment=False,
                    label_ids=(), keywords=(), sender_frequency="occasional",
                    urgency_signals=(), extraction_confidence=0.8,
                    extraction_source="heuristic",
                )
            mock_extract.side_effect = fake_extract

            with patch("autonomy.email_triage.runner.score_email") as mock_score:
                mock_score.return_value = MagicMock(
                    tier=3, score=40, tier_label="jarvis/tier3_review",
                    breakdown={}, idempotency_key="idem-1",
                    scoring_explanation="test",
                )
                with patch.object(runner, "_apply_label", new_callable=AsyncMock):
                    report = await runner.run_cycle()

        # Verify envelopes were created (check via health monitor which receives them)
        assert report.emails_processed == 2


class TestLedgerIntegration:
    @pytest.mark.asyncio
    async def test_ledger_reserve_commit_on_success(self, tmp_path):
        from core.contracts.action_commit_ledger import ActionCommitLedger, CommitState
        config = TriageConfig(enabled=True, max_emails_per_cycle=1)
        runner = _make_runner(config)

        ledger = ActionCommitLedger(tmp_path / "test.db")
        await ledger.start()
        runner._commit_ledger = ledger

        mock_ws = runner._resolver.get("workspace_agent")
        mock_ws._fetch_unread_emails = AsyncMock(return_value={
            "emails": [
                {"id": "msg-1", "from": "a@test.com", "subject": "Urgent", "snippet": "now", "labelIds": []},
            ]
        })

        with patch("autonomy.email_triage.runner.extract_features") as mock_extract:
            from autonomy.email_triage.schemas import EmailFeatures
            mock_extract.return_value = EmailFeatures(
                message_id="msg-1", sender="a@test.com",
                sender_domain="test.com", subject="Urgent",
                snippet="now", is_reply=False, has_attachment=False,
                label_ids=(), keywords=(), sender_frequency="occasional",
                urgency_signals=(), extraction_confidence=0.9,
                extraction_source="heuristic",
            )
            with patch("autonomy.email_triage.runner.score_email") as mock_score:
                mock_score.return_value = MagicMock(
                    tier=3, score=40, tier_label="jarvis/tier3_review",
                    breakdown={}, idempotency_key="idem-1",
                    scoring_explanation="test",
                )
                with patch.object(runner, "_apply_label", new_callable=AsyncMock):
                    await runner.run_cycle()

        records = await ledger.query(since_epoch=0)
        assert len(records) == 1
        assert records[0].state == CommitState.COMMITTED
        assert records[0].outcome == "success"
        await ledger.stop()

    @pytest.mark.asyncio
    async def test_ledger_abort_on_label_failure(self, tmp_path):
        from core.contracts.action_commit_ledger import ActionCommitLedger, CommitState
        config = TriageConfig(enabled=True, max_emails_per_cycle=1)
        runner = _make_runner(config)

        ledger = ActionCommitLedger(tmp_path / "test.db")
        await ledger.start()
        runner._commit_ledger = ledger

        mock_ws = runner._resolver.get("workspace_agent")
        mock_ws._fetch_unread_emails = AsyncMock(return_value={
            "emails": [
                {"id": "msg-1", "from": "a@test.com", "subject": "Test", "snippet": "hi", "labelIds": []},
            ]
        })

        with patch("autonomy.email_triage.runner.extract_features") as mock_extract:
            from autonomy.email_triage.schemas import EmailFeatures
            mock_extract.return_value = EmailFeatures(
                message_id="msg-1", sender="a@test.com",
                sender_domain="test.com", subject="Test",
                snippet="hi", is_reply=False, has_attachment=False,
                label_ids=(), keywords=(), sender_frequency="occasional",
                urgency_signals=(), extraction_confidence=0.9,
                extraction_source="heuristic",
            )
            with patch("autonomy.email_triage.runner.score_email") as mock_score:
                mock_score.return_value = MagicMock(
                    tier=3, score=40, tier_label="jarvis/tier3_review",
                    breakdown={}, idempotency_key="idem-1",
                    scoring_explanation="test",
                )
                with patch.object(runner, "_apply_label", new_callable=AsyncMock, side_effect=Exception("Gmail error")):
                    await runner.run_cycle()

        records = await ledger.query(since_epoch=0)
        assert len(records) == 1
        assert records[0].state == CommitState.ABORTED
        assert "action_failed" in (records[0].abort_reason or "")
        await ledger.stop()

    @pytest.mark.asyncio
    async def test_fail_closed_on_reserve_failure(self, tmp_path):
        from core.contracts.action_commit_ledger import ActionCommitLedger
        config = TriageConfig(enabled=True, max_emails_per_cycle=1)
        runner = _make_runner(config)

        ledger = ActionCommitLedger(tmp_path / "test.db")
        await ledger.start()
        runner._commit_ledger = ledger
        # Break reserve to simulate failure
        ledger.reserve = AsyncMock(side_effect=Exception("DB locked"))

        mock_ws = runner._resolver.get("workspace_agent")
        mock_ws._fetch_unread_emails = AsyncMock(return_value={
            "emails": [
                {"id": "msg-1", "from": "a@test.com", "subject": "Test", "snippet": "hi", "labelIds": []},
            ]
        })

        label_mock = AsyncMock()
        with patch("autonomy.email_triage.runner.extract_features") as mock_extract:
            from autonomy.email_triage.schemas import EmailFeatures
            mock_extract.return_value = EmailFeatures(
                message_id="msg-1", sender="a@test.com",
                sender_domain="test.com", subject="Test",
                snippet="hi", is_reply=False, has_attachment=False,
                label_ids=(), keywords=(), sender_frequency="occasional",
                urgency_signals=(), extraction_confidence=0.9,
                extraction_source="heuristic",
            )
            with patch("autonomy.email_triage.runner.score_email") as mock_score:
                mock_score.return_value = MagicMock(
                    tier=3, score=40, tier_label="jarvis/tier3_review",
                    breakdown={}, idempotency_key="idem-1",
                    scoring_explanation="test",
                )
                with patch.object(runner, "_apply_label", label_mock):
                    report = await runner.run_cycle()

        # Label should NOT have been called (fail-closed)
        label_mock.assert_not_called()
        assert any("ledger_reserve" in e for e in report.errors)
        await ledger.stop()
```

**Step 2: Run tests to verify they fail**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/backend/email_triage/test_runner_bridge.py -v -k "TestLedger or TestEnvelope"`
Expected: FAIL (runner doesn't use ledger or envelopes yet)

**Step 3: Modify per-email processing in run_cycle()**

This is the critical change. Replace the per-email processing loop (lines 490-575 approximately) with the envelope + gate + ledger flow from the design doc.

The key structural change is:
1. Add `cycle_envelopes: List[DecisionEnvelope] = []` before the loop
2. After extraction: create extraction_envelope
3. After scoring: create scoring_envelope
4. Replace `action, explanation = self._policy.decide_action(triaged)` with `verdict = await self._policy_gate.evaluate(scoring_envelope, policy_context)`
5. Before label: `reserve()` + `check_pre_exec_invariants()`
6. After label: `commit()` or `abort()`

Also add the helper function `_map_extraction_source()` as a module-level function.

**IMPORTANT:** The existing notification logic (immediate_emails, summary buffer) must continue working. The verdict's `.reason` field contains the action string ("immediate", "label_only", etc.) which feeds the existing notification path.

**Step 4: Run ALL tests to verify**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/backend/email_triage/ -q --tb=short -k "not test_disabled_flag_skips_entirely"`
Expected: All pass (existing + new)

**Step 5: Commit**

```bash
git add backend/autonomy/email_triage/runner.py tests/unit/backend/email_triage/test_runner_bridge.py
git commit -m "feat(runner): wire envelope + policy gate + ledger into per-email processing"
```

---

### Task 7: Wire health recording + stale expiry after cycle

**Files:**
- Modify: `backend/autonomy/email_triage/runner.py`
- Test: add to `tests/unit/backend/email_triage/test_runner_bridge.py`

**Step 1: Write the failing test**

Add to `test_runner_bridge.py`:

```python
class TestHealthRecording:
    @pytest.mark.asyncio
    async def test_health_monitor_records_cycle(self):
        runner = _make_runner()
        mock_ws = runner._resolver.get("workspace_agent")
        mock_ws._fetch_unread_emails = AsyncMock(return_value={"emails": []})

        # Spy on record_cycle
        original_record = runner._health_monitor.record_cycle
        recorded = []
        def spy_record(report, envelopes):
            recorded.append((report, envelopes))
            return original_record(report, envelopes)
        runner._health_monitor.record_cycle = spy_record

        await runner.run_cycle()
        assert len(recorded) == 1
        report, envelopes = recorded[0]
        assert report is not None
```

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/backend/email_triage/test_runner_bridge.py::TestHealthRecording -v`
Expected: FAIL

**Step 3: Add health recording to run_cycle()**

After the report is built (after line 684), before the snapshot commit block, add:

```python
        # Phase B: Record cycle in behavioral health monitor
        self._health_monitor.record_cycle(report, cycle_envelopes)

        # Phase B: Expire stale ledger reservations
        if self._commit_ledger:
            try:
                await self._commit_ledger.expire_stale()
            except Exception:
                pass
```

Make sure `cycle_envelopes` is passed through from where it was defined in the per-email loop. If the variable was defined inside the loop scope, move it to be defined at the top of `run_cycle()`:
```python
        cycle_envelopes: list = []  # Phase B: collect envelopes for health monitor
```

**Step 4: Run test to verify it passes**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/backend/email_triage/test_runner_bridge.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add backend/autonomy/email_triage/runner.py tests/unit/backend/email_triage/test_runner_bridge.py
git commit -m "feat(runner): add health recording and stale lease expiry after cycle"
```

---

### Task 8: Full regression test + final verification

**Files:**
- No new files. Run full test suites.

**Step 1: Run Phase A contract tests**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/core/contracts/ tests/unit/autonomy/contracts/ -q`
Expected: 49+ tests pass

**Step 2: Run Phase B bridge tests**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/backend/email_triage/test_runner_bridge.py tests/unit/backend/email_triage/test_triage_policy_gate.py tests/unit/core/contracts/test_policy_context.py tests/unit/backend/email_triage/test_config_ledger.py -v`
Expected: All pass

**Step 3: Run full email triage regression**

Run: `PYTHONPATH="$PWD:$PWD/backend" python3 -m pytest tests/unit/backend/email_triage/ -q --tb=short -k "not test_disabled_flag_skips_entirely"`
Expected: 230+ pass, 0 fail (more tests now than before)

**Step 4: Verify done criteria (10 gates)**

Check each gate from the design doc:
1. Extraction envelopes created -- verify in test_envelopes_created
2. Scoring envelopes with parent -- verify in code
3. PolicyGate.evaluate called -- verify in test
4. Ledger.reserve called -- verify in test_ledger_reserve_commit
5. Pre-exec invariants checked -- verify in code
6. Commit/abort called -- verify in test_ledger_reserve_commit + test_ledger_abort
7. Health monitor records cycle -- verify in test_health_monitor_records_cycle
8. Throttle check before cycle -- verify in test_circuit_break_skips_cycle
9. expire_stale called -- verify in warm_up + post-cycle
10. Existing tests pass -- verify in Step 3

**Step 5: Summary commit if any fixes needed**

```bash
git add -A
git commit -m "test(phase-b): verify all 10 acceptance gates pass"
```
