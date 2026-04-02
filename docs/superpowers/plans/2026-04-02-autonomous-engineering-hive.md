# Autonomous Engineering Hive — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an agent-to-agent social network (the "Hive") where Trinity Personas debate system improvements, surfaced through the JARVIS HUD, with Ouroboros executing consensus into PRs.

**Architecture:** Backend-first — the `AgentCommunicationBus` is the town square. A 3-state Cognitive FSM (BASELINE/REM/FLOW) governs intelligence spend. Hierarchical messaging: specialist agents post telemetry (Tier 1), Trinity Personas debate solutions (Tier 2). Reactor's `validate(approve)` is the consensus gate. Thread consensus serializes into `OperationContext` for Ouroboros execution. The HUD Relay Agent projects messages to the native Swift HUD via IPC (port 8742).

**Tech Stack:** Python 3.12, asyncio, dataclasses, pytest + pytest-asyncio, Doubleword batch API (Qwen 35B/397B), SwiftUI (JARVISHUD target), Next.js (jarvis-cloud dashboard)

**Spec:** `docs/superpowers/specs/2026-04-02-autonomous-engineering-hive-design.md`

---

## File Structure

### New Files (backend/hive/)

| File | Responsibility |
|------|----------------|
| `backend/hive/__init__.py` | Package init, exports |
| `backend/hive/thread_models.py` | Data models: `HiveThread`, `AgentLogMessage`, `PersonaReasoningMessage`, enums |
| `backend/hive/cognitive_fsm.py` | `CognitiveStateMachine` — 3-state FSM with pure `decide()` + durable executor |
| `backend/hive/thread_manager.py` | Thread lifecycle, storage, consensus detection |
| `backend/hive/persona_engine.py` | Trinity Persona reasoning orchestrator (Doubleword LLM calls) |
| `backend/hive/model_router.py` | Cognitive-state-aware Doubleword model selection |
| `backend/hive/hud_relay_agent.py` | `BaseNeuralMeshAgent` bridging bus → IPC (8742) |
| `backend/hive/ouroboros_handoff.py` | Thread consensus → `OperationContext` serialization |
| `backend/hive/hive_service.py` | Top-level orchestrator wiring all components, boot/shutdown lifecycle |

### New Files (tests/)

| File | Responsibility |
|------|----------------|
| `tests/test_hive_thread_models.py` | Data model serialization, validation, enums |
| `tests/test_hive_cognitive_fsm.py` | FSM transitions, safety invariants, crash recovery |
| `tests/test_hive_thread_manager.py` | Thread lifecycle, consensus detection, storage |
| `tests/test_hive_persona_engine.py` | Persona reasoning orchestration, model routing |
| `tests/test_hive_hud_relay.py` | IPC message projection, ordering, batching |
| `tests/test_hive_ouroboros_handoff.py` | OperationContext serialization, field mapping |
| `tests/test_hive_integration.py` | End-to-end: agent_log → persona debate → consensus → handoff |

### Modified Files

| File | Change |
|------|--------|
| `backend/neural_mesh/data_models.py` | Add `HIVE_AGENT_LOG`, `HIVE_PERSONA_REASONING`, `HIVE_THREAD_LIFECYCLE`, `HIVE_COGNITIVE_TRANSITION` to `MessageType` enum |
| `backend/hud/ipc_server.py` | No changes needed — already handles arbitrary `{"event_type", "data"}` JSON |

### Future Files (not this plan)

| File | Responsibility |
|------|----------------|
| `JARVIS-Apple/JARVISHUD/Views/HiveView.swift` | Native Hive tab (SwiftUI) — separate plan |
| `JARVIS-Apple/JARVISHUD/Services/HiveStore.swift` | @Observable store — separate plan |
| `jarvis-cloud/app/dashboard/hive/page.tsx` | Web dashboard — separate plan |

---

## Task 1: Thread Data Models

**Files:**
- Create: `backend/hive/__init__.py`
- Create: `backend/hive/thread_models.py`
- Create: `tests/test_hive_thread_models.py`
- Modify: `backend/neural_mesh/data_models.py` (add MessageType entries)

### Step 1: Add Hive MessageType entries

- [ ] **1.1: Write failing test for new MessageType values**

```python
# tests/test_hive_thread_models.py
"""Tests for Hive data models."""
import pytest
from backend.neural_mesh.data_models import MessageType


def test_hive_message_types_exist():
    """Hive message types must be registered in the MessageType enum."""
    assert hasattr(MessageType, "HIVE_AGENT_LOG")
    assert hasattr(MessageType, "HIVE_PERSONA_REASONING")
    assert hasattr(MessageType, "HIVE_THREAD_LIFECYCLE")
    assert hasattr(MessageType, "HIVE_COGNITIVE_TRANSITION")
```

- [ ] **1.2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_hive_thread_models.py::test_hive_message_types_exist -v`
Expected: FAIL with `AttributeError`

- [ ] **1.3: Add MessageType entries**

In `backend/neural_mesh/data_models.py`, add after the existing `TIER_DECISION` entry (near end of MessageType enum):

```python
    # Hive (Autonomous Engineering Hive — agent-to-agent social network)
    HIVE_AGENT_LOG = auto()
    HIVE_PERSONA_REASONING = auto()
    HIVE_THREAD_LIFECYCLE = auto()
    HIVE_COGNITIVE_TRANSITION = auto()
```

- [ ] **1.4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_hive_thread_models.py::test_hive_message_types_exist -v`
Expected: PASS

### Step 2: Create thread data models

- [ ] **2.1: Write failing tests for data models**

Append to `tests/test_hive_thread_models.py`:

```python
import time
from datetime import datetime, timezone

from backend.hive.thread_models import (
    AgentLogMessage,
    CognitiveState,
    HiveThread,
    PersonaIntent,
    PersonaReasoningMessage,
    ThreadState,
)


def test_cognitive_state_enum():
    assert CognitiveState.BASELINE.value == "baseline"
    assert CognitiveState.REM.value == "rem"
    assert CognitiveState.FLOW.value == "flow"


def test_thread_state_enum():
    assert ThreadState.OPEN.value == "open"
    assert ThreadState.DEBATING.value == "debating"
    assert ThreadState.CONSENSUS.value == "consensus"
    assert ThreadState.EXECUTING.value == "executing"
    assert ThreadState.RESOLVED.value == "resolved"
    assert ThreadState.STALE.value == "stale"


def test_persona_intent_enum():
    assert PersonaIntent.OBSERVE.value == "observe"
    assert PersonaIntent.PROPOSE.value == "propose"
    assert PersonaIntent.CHALLENGE.value == "challenge"
    assert PersonaIntent.SUPPORT.value == "support"
    assert PersonaIntent.VALIDATE.value == "validate"


def test_agent_log_message_creation():
    msg = AgentLogMessage(
        thread_id="thr_test1",
        agent_name="health_monitor_agent",
        trinity_parent="jarvis",
        severity="warning",
        category="memory_pressure",
        payload={"metric": "ram_percent", "value": 87.3},
    )
    assert msg.type == "agent_log"
    assert msg.message_id.startswith("msg_")
    assert msg.monotonic_ns > 0
    assert msg.ts is not None


def test_agent_log_to_dict_roundtrip():
    msg = AgentLogMessage(
        thread_id="thr_test1",
        agent_name="vision_agent",
        trinity_parent="jarvis",
        severity="info",
        category="observation",
        payload={"frames": 47},
    )
    d = msg.to_dict()
    assert d["type"] == "agent_log"
    assert d["agent_name"] == "vision_agent"
    assert d["payload"]["frames"] == 47
    restored = AgentLogMessage.from_dict(d)
    assert restored.thread_id == msg.thread_id
    assert restored.agent_name == msg.agent_name


def test_persona_reasoning_message_creation():
    msg = PersonaReasoningMessage(
        thread_id="thr_test1",
        persona="j_prime",
        role="mind",
        intent=PersonaIntent.PROPOSE,
        references=["msg_log_001"],
        manifesto_principle="$3 Spinal Cord",
        reasoning="Add TTL eviction to FramePipeline.",
        confidence=0.87,
        model_used="Qwen/Qwen3.5-397B-A17B-FP8",
        token_cost=1847,
    )
    assert msg.type == "persona_reasoning"
    assert msg.message_id.startswith("msg_")
    assert msg.validate_verdict is None


def test_persona_reasoning_validate_with_verdict():
    msg = PersonaReasoningMessage(
        thread_id="thr_test1",
        persona="reactor",
        role="immune_system",
        intent=PersonaIntent.VALIDATE,
        references=["msg_proposal_001"],
        reasoning="AST clean. Low risk.",
        confidence=0.95,
        model_used="Qwen/Qwen3.5-397B-A17B-FP8",
        token_cost=923,
        validate_verdict="approve",
    )
    assert msg.validate_verdict == "approve"


def test_persona_reasoning_to_dict_roundtrip():
    msg = PersonaReasoningMessage(
        thread_id="thr_test1",
        persona="jarvis",
        role="body",
        intent=PersonaIntent.OBSERVE,
        references=[],
        reasoning="RAM pressure rising.",
        confidence=0.9,
        model_used="Qwen/Qwen3.5-35B-A3B-FP8",
        token_cost=500,
    )
    d = msg.to_dict()
    assert d["type"] == "persona_reasoning"
    restored = PersonaReasoningMessage.from_dict(d)
    assert restored.persona == "jarvis"
    assert restored.intent == PersonaIntent.OBSERVE


def test_hive_thread_creation():
    thread = HiveThread(
        title="Memory Pressure in Vision Loop",
        trigger_event="health_monitor_agent:memory_pressure",
        cognitive_state=CognitiveState.FLOW,
        token_budget=50000,
        debate_deadline_s=900.0,
    )
    assert thread.thread_id.startswith("thr_")
    assert thread.state == ThreadState.OPEN
    assert thread.tokens_consumed == 0
    assert thread.messages == []
    assert thread.linked_op_id is None


def test_hive_thread_add_message():
    thread = HiveThread(
        title="Test Thread",
        trigger_event="test",
        cognitive_state=CognitiveState.FLOW,
        token_budget=50000,
        debate_deadline_s=900.0,
    )
    log = AgentLogMessage(
        thread_id=thread.thread_id,
        agent_name="health_monitor_agent",
        trinity_parent="jarvis",
        severity="warning",
        category="memory_pressure",
        payload={"value": 87.3},
    )
    thread.add_message(log)
    assert len(thread.messages) == 1
    assert thread.messages[0].message_id == log.message_id


def test_hive_thread_token_tracking():
    thread = HiveThread(
        title="Test",
        trigger_event="test",
        cognitive_state=CognitiveState.FLOW,
        token_budget=1000,
        debate_deadline_s=900.0,
    )
    msg = PersonaReasoningMessage(
        thread_id=thread.thread_id,
        persona="j_prime",
        role="mind",
        intent=PersonaIntent.PROPOSE,
        references=[],
        reasoning="test",
        confidence=0.9,
        model_used="test-model",
        token_cost=500,
    )
    thread.add_message(msg)
    assert thread.tokens_consumed == 500

    msg2 = PersonaReasoningMessage(
        thread_id=thread.thread_id,
        persona="reactor",
        role="immune_system",
        intent=PersonaIntent.VALIDATE,
        references=[],
        reasoning="approved",
        confidence=0.95,
        model_used="test-model",
        token_cost=300,
    )
    thread.add_message(msg2)
    assert thread.tokens_consumed == 800


def test_hive_thread_to_dict_roundtrip():
    thread = HiveThread(
        title="Test Thread",
        trigger_event="test",
        cognitive_state=CognitiveState.FLOW,
        token_budget=50000,
        debate_deadline_s=900.0,
    )
    d = thread.to_dict()
    assert d["state"] == "open"
    restored = HiveThread.from_dict(d)
    assert restored.thread_id == thread.thread_id
    assert restored.title == "Test Thread"
```

- [ ] **2.2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_hive_thread_models.py -v -k "not test_hive_message_types"`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.hive'`

- [ ] **2.3: Create package init**

```python
# backend/hive/__init__.py
"""Autonomous Engineering Hive — agent-to-agent social network for the Trinity ecosystem."""
```

- [ ] **2.4: Implement thread_models.py**

```python
# backend/hive/thread_models.py
"""Data models for the Autonomous Engineering Hive.

Defines the two-tier message schema:
  - Tier 1: AgentLogMessage (deterministic specialist telemetry, no LLM)
  - Tier 2: PersonaReasoningMessage (LLM-powered Trinity debate voices)

Plus HiveThread (scoped conversation unit with lifecycle).

All models support to_dict/from_dict for IPC serialization (JSON over TCP port 8742).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CognitiveState(str, Enum):
    """Dynamic Cognitive State Machine states (spec $1)."""
    BASELINE = "baseline"
    REM = "rem"
    FLOW = "flow"


class ThreadState(str, Enum):
    """Thread lifecycle states (spec $2)."""
    OPEN = "open"
    DEBATING = "debating"
    CONSENSUS = "consensus"
    EXECUTING = "executing"
    RESOLVED = "resolved"
    STALE = "stale"


class PersonaIntent(str, Enum):
    """Persona reasoning intents (spec $3)."""
    OBSERVE = "observe"
    PROPOSE = "propose"
    CHALLENGE = "challenge"
    SUPPORT = "support"
    VALIDATE = "validate"


def _gen_msg_id() -> str:
    return f"msg_{uuid.uuid4().hex[:12]}"


def _gen_thread_id() -> str:
    return f"thr_{uuid.uuid4().hex[:12]}"


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Tier 1: Agent Log (specialist telemetry — no LLM)
# ---------------------------------------------------------------------------


@dataclass
class AgentLogMessage:
    """Structured telemetry from a specialist sub-agent.

    These are deterministic, structured, and never involve LLM calls.
    Published by sub-agents and routed to their Trinity parent.
    """
    thread_id: str
    agent_name: str
    trinity_parent: Literal["jarvis", "j_prime", "reactor"]
    severity: Literal["info", "warning", "error", "critical"]
    category: str
    payload: Dict[str, Any]
    type: Literal["agent_log"] = "agent_log"
    message_id: str = field(default_factory=_gen_msg_id)
    ts: datetime = field(default_factory=_now_utc)
    monotonic_ns: int = field(default_factory=time.monotonic_ns)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "thread_id": self.thread_id,
            "message_id": self.message_id,
            "agent_name": self.agent_name,
            "trinity_parent": self.trinity_parent,
            "severity": self.severity,
            "category": self.category,
            "payload": self.payload,
            "ts": self.ts.isoformat(),
            "monotonic_ns": self.monotonic_ns,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> AgentLogMessage:
        ts = data.get("ts")
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        return cls(
            thread_id=data["thread_id"],
            agent_name=data["agent_name"],
            trinity_parent=data["trinity_parent"],
            severity=data["severity"],
            category=data["category"],
            payload=data["payload"],
            message_id=data.get("message_id", _gen_msg_id()),
            ts=ts or _now_utc(),
            monotonic_ns=data.get("monotonic_ns", time.monotonic_ns()),
        )


# ---------------------------------------------------------------------------
# Tier 2: Persona Reasoning (Trinity debate voices — LLM-powered)
# ---------------------------------------------------------------------------


@dataclass
class PersonaReasoningMessage:
    """LLM-powered reasoning from a Trinity Persona.

    Only JARVIS (body), J-Prime (mind), and Reactor Core (immune_system)
    emit these messages. Reactor's validate intent with approve/reject
    verdict is the consensus gate (spec $2, $7).
    """
    thread_id: str
    persona: Literal["jarvis", "j_prime", "reactor"]
    role: Literal["body", "mind", "immune_system"]
    intent: PersonaIntent
    references: List[str]
    reasoning: str
    confidence: float
    model_used: str
    token_cost: int
    type: Literal["persona_reasoning"] = "persona_reasoning"
    message_id: str = field(default_factory=_gen_msg_id)
    manifesto_principle: Optional[str] = None
    validate_verdict: Optional[Literal["approve", "reject"]] = None
    ts: datetime = field(default_factory=_now_utc)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "thread_id": self.thread_id,
            "message_id": self.message_id,
            "persona": self.persona,
            "role": self.role,
            "intent": self.intent.value,
            "references": self.references,
            "manifesto_principle": self.manifesto_principle,
            "reasoning": self.reasoning,
            "confidence": self.confidence,
            "model_used": self.model_used,
            "token_cost": self.token_cost,
            "validate_verdict": self.validate_verdict,
            "ts": self.ts.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PersonaReasoningMessage:
        ts = data.get("ts")
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        return cls(
            thread_id=data["thread_id"],
            persona=data["persona"],
            role=data["role"],
            intent=PersonaIntent(data["intent"]),
            references=data.get("references", []),
            reasoning=data["reasoning"],
            confidence=data["confidence"],
            model_used=data["model_used"],
            token_cost=data["token_cost"],
            message_id=data.get("message_id", _gen_msg_id()),
            manifesto_principle=data.get("manifesto_principle"),
            validate_verdict=data.get("validate_verdict"),
            ts=ts or _now_utc(),
        )


# Union type for any message in a thread
HiveMessage = Union[AgentLogMessage, PersonaReasoningMessage]


# ---------------------------------------------------------------------------
# HiveThread — scoped conversation unit
# ---------------------------------------------------------------------------


@dataclass
class HiveThread:
    """A scoped conversation unit with lifecycle.

    Each thread maps to one capability gap / improvement proposal.
    Thread history becomes the Ouroboros context payload at CONSENSUS.

    Consensus rule (spec $2, $7): Reactor's validate(approve) is the gate.
    Prerequisites: at least one observe from JARVIS and one propose from
    J-Prime must exist before Reactor can validate.
    """
    title: str
    trigger_event: str
    cognitive_state: CognitiveState
    token_budget: int
    debate_deadline_s: float
    thread_id: str = field(default_factory=_gen_thread_id)
    state: ThreadState = ThreadState.OPEN
    messages: List[HiveMessage] = field(default_factory=list)
    manifesto_principles: List[str] = field(default_factory=list)
    tokens_consumed: int = 0
    linked_op_id: Optional[str] = None
    linked_pr_url: Optional[str] = None
    created_at: datetime = field(default_factory=_now_utc)
    resolved_at: Optional[datetime] = None

    def add_message(self, msg: HiveMessage) -> None:
        """Append a message and update token tracking."""
        self.messages.append(msg)
        if isinstance(msg, PersonaReasoningMessage):
            self.tokens_consumed += msg.token_cost
            if msg.manifesto_principle and msg.manifesto_principle not in self.manifesto_principles:
                self.manifesto_principles.append(msg.manifesto_principle)

    def has_observe(self) -> bool:
        """Check if JARVIS has posted at least one observe."""
        return any(
            isinstance(m, PersonaReasoningMessage)
            and m.persona == "jarvis"
            and m.intent == PersonaIntent.OBSERVE
            for m in self.messages
        )

    def has_propose(self) -> bool:
        """Check if J-Prime has posted at least one propose."""
        return any(
            isinstance(m, PersonaReasoningMessage)
            and m.persona == "j_prime"
            and m.intent == PersonaIntent.PROPOSE
            for m in self.messages
        )

    def is_consensus_ready(self) -> bool:
        """Check if consensus prerequisites are met.

        Requires: JARVIS observed, J-Prime proposed, Reactor validated (approve).
        """
        if not (self.has_observe() and self.has_propose()):
            return False
        return any(
            isinstance(m, PersonaReasoningMessage)
            and m.persona == "reactor"
            and m.intent == PersonaIntent.VALIDATE
            and m.validate_verdict == "approve"
            for m in self.messages
        )

    def is_budget_exhausted(self) -> bool:
        """Check if token budget is exhausted."""
        return self.tokens_consumed >= self.token_budget

    def to_dict(self) -> Dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "title": self.title,
            "state": self.state.value,
            "cognitive_state": self.cognitive_state.value,
            "trigger_event": self.trigger_event,
            "messages": [m.to_dict() for m in self.messages],
            "manifesto_principles": self.manifesto_principles,
            "token_budget": self.token_budget,
            "tokens_consumed": self.tokens_consumed,
            "debate_deadline_s": self.debate_deadline_s,
            "linked_op_id": self.linked_op_id,
            "linked_pr_url": self.linked_pr_url,
            "created_at": self.created_at.isoformat(),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> HiveThread:
        messages: List[HiveMessage] = []
        for md in data.get("messages", []):
            if md.get("type") == "agent_log":
                messages.append(AgentLogMessage.from_dict(md))
            elif md.get("type") == "persona_reasoning":
                messages.append(PersonaReasoningMessage.from_dict(md))

        created_at = data.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        resolved_at = data.get("resolved_at")
        if isinstance(resolved_at, str):
            resolved_at = datetime.fromisoformat(resolved_at)

        thread = cls(
            title=data["title"],
            trigger_event=data["trigger_event"],
            cognitive_state=CognitiveState(data["cognitive_state"]),
            token_budget=data["token_budget"],
            debate_deadline_s=data["debate_deadline_s"],
            thread_id=data.get("thread_id", _gen_thread_id()),
            state=ThreadState(data.get("state", "open")),
            manifesto_principles=data.get("manifesto_principles", []),
            tokens_consumed=data.get("tokens_consumed", 0),
            linked_op_id=data.get("linked_op_id"),
            linked_pr_url=data.get("linked_pr_url"),
            created_at=created_at or _now_utc(),
            resolved_at=resolved_at,
        )
        thread.messages = messages
        return thread
```

- [ ] **2.5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_hive_thread_models.py -v`
Expected: ALL PASS

- [ ] **2.6: Commit**

```bash
git add backend/hive/__init__.py backend/hive/thread_models.py backend/neural_mesh/data_models.py tests/test_hive_thread_models.py
git commit -m "feat(hive): add thread data models and MessageType entries"
```

---

## Task 2: Cognitive State Machine

**Files:**
- Create: `backend/hive/cognitive_fsm.py`
- Create: `tests/test_hive_cognitive_fsm.py`

This follows the `PreemptionFsmEngine` pattern: pure `decide()` function (no I/O), separate executor for side effects, crash recovery via persisted state.

- [ ] **1: Write failing tests for the FSM**

```python
# tests/test_hive_cognitive_fsm.py
"""Tests for the Dynamic Cognitive State Machine."""
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.hive.cognitive_fsm import (
    CognitiveEvent,
    CognitiveFsm,
    CognitiveTransition,
)
from backend.hive.thread_models import CognitiveState


class TestCognitiveFsmTransitions:
    """Test pure decide() transitions — no I/O."""

    def setup_method(self):
        self.fsm = CognitiveFsm()

    def test_initial_state_is_baseline(self):
        assert self.fsm.state == CognitiveState.BASELINE

    def test_baseline_to_rem_on_rem_trigger(self):
        decision = self.fsm.decide(
            CognitiveEvent.REM_TRIGGER,
            idle_seconds=6 * 3600 + 1,
            system_load_pct=20.0,
            graduation_candidates=1,
        )
        assert decision.to_state == CognitiveState.REM
        assert decision.reason_code == "T1_REM_TRIGGER"

    def test_baseline_to_rem_blocked_by_high_load(self):
        decision = self.fsm.decide(
            CognitiveEvent.REM_TRIGGER,
            idle_seconds=7 * 3600,
            system_load_pct=50.0,
            graduation_candidates=1,
        )
        assert decision.to_state == CognitiveState.BASELINE
        assert decision.reason_code == "T1_BLOCKED_HIGH_LOAD"

    def test_baseline_to_rem_blocked_by_low_idle(self):
        decision = self.fsm.decide(
            CognitiveEvent.REM_TRIGGER,
            idle_seconds=3600,
            system_load_pct=10.0,
            graduation_candidates=1,
        )
        assert decision.to_state == CognitiveState.BASELINE
        assert decision.reason_code == "T1_BLOCKED_LOW_IDLE"

    def test_baseline_to_flow_on_flow_trigger(self):
        decision = self.fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        assert decision.to_state == CognitiveState.FLOW
        assert decision.reason_code == "T2_FLOW_TRIGGER"

    def test_rem_to_flow_on_council_escalation(self):
        self.fsm._state = CognitiveState.REM
        decision = self.fsm.decide(CognitiveEvent.COUNCIL_ESCALATION)
        assert decision.to_state == CognitiveState.FLOW
        assert decision.reason_code == "T2B_COUNCIL_ESCALATION"

    def test_rem_to_baseline_on_council_complete(self):
        self.fsm._state = CognitiveState.REM
        decision = self.fsm.decide(CognitiveEvent.COUNCIL_COMPLETE)
        assert decision.to_state == CognitiveState.BASELINE
        assert decision.reason_code == "T3B_COUNCIL_COMPLETE"

    def test_flow_to_baseline_on_pr_merged(self):
        self.fsm._state = CognitiveState.FLOW
        decision = self.fsm.decide(CognitiveEvent.SPINDOWN, spindown_reason="pr_merged")
        assert decision.to_state == CognitiveState.BASELINE
        assert decision.reason_code == "T3_SPINDOWN_PR_MERGED"

    def test_flow_to_baseline_on_debate_timeout(self):
        self.fsm._state = CognitiveState.FLOW
        decision = self.fsm.decide(CognitiveEvent.SPINDOWN, spindown_reason="debate_timeout")
        assert decision.to_state == CognitiveState.BASELINE
        assert decision.reason_code == "T3_SPINDOWN_DEBATE_TIMEOUT"

    def test_flow_to_baseline_on_budget_exhausted(self):
        self.fsm._state = CognitiveState.FLOW
        decision = self.fsm.decide(CognitiveEvent.SPINDOWN, spindown_reason="token_budget_exhausted")
        assert decision.to_state == CognitiveState.BASELINE
        assert decision.reason_code == "T3_SPINDOWN_TOKEN_BUDGET_EXHAUSTED"

    def test_flow_to_baseline_on_user_override(self):
        self.fsm._state = CognitiveState.FLOW
        decision = self.fsm.decide(CognitiveEvent.USER_SPINDOWN)
        assert decision.to_state == CognitiveState.BASELINE
        assert decision.reason_code == "USER_MANUAL_SPINDOWN"

    def test_rem_to_baseline_on_user_override(self):
        self.fsm._state = CognitiveState.REM
        decision = self.fsm.decide(CognitiveEvent.USER_SPINDOWN)
        assert decision.to_state == CognitiveState.BASELINE
        assert decision.reason_code == "USER_MANUAL_SPINDOWN"

    def test_baseline_ignores_spindown(self):
        decision = self.fsm.decide(CognitiveEvent.SPINDOWN, spindown_reason="pr_merged")
        assert decision.to_state == CognitiveState.BASELINE
        assert decision.noop

    def test_flow_trigger_from_flow_is_noop(self):
        self.fsm._state = CognitiveState.FLOW
        decision = self.fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        assert decision.to_state == CognitiveState.FLOW
        assert decision.noop


class TestCognitiveFsmSafety:
    """Safety invariant tests."""

    def test_no_state_stacking(self):
        """Only one state at a time."""
        fsm = CognitiveFsm()
        fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        fsm.apply_last_decision()
        assert fsm.state == CognitiveState.FLOW
        # Cannot enter REM while in FLOW
        decision = fsm.decide(
            CognitiveEvent.REM_TRIGGER,
            idle_seconds=25000,
            system_load_pct=10.0,
            graduation_candidates=1,
        )
        assert decision.to_state == CognitiveState.FLOW
        assert decision.noop

    def test_crash_recovery_to_baseline(self, tmp_path):
        """After crash, state recovers to BASELINE."""
        state_file = tmp_path / "cognitive_state.json"
        # Simulate crash: write FLOW state
        state_file.write_text(json.dumps({"state": "flow", "entered_at": "2026-04-02T00:00:00+00:00"}))
        fsm = CognitiveFsm(state_file=state_file, crash_recovery=True)
        assert fsm.state == CognitiveState.BASELINE

    def test_state_persistence(self, tmp_path):
        """State is persisted to disk after apply."""
        state_file = tmp_path / "cognitive_state.json"
        fsm = CognitiveFsm(state_file=state_file)
        fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        fsm.apply_last_decision()
        data = json.loads(state_file.read_text())
        assert data["state"] == "flow"

    def test_user_spindown_from_any_state(self):
        """USER_SPINDOWN exits any non-BASELINE state."""
        for start_state in [CognitiveState.REM, CognitiveState.FLOW]:
            fsm = CognitiveFsm()
            fsm._state = start_state
            decision = fsm.decide(CognitiveEvent.USER_SPINDOWN)
            assert decision.to_state == CognitiveState.BASELINE
```

- [ ] **2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_hive_cognitive_fsm.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **3: Implement cognitive_fsm.py**

```python
# backend/hive/cognitive_fsm.py
"""Dynamic Cognitive State Machine for the Autonomous Engineering Hive.

3-state FSM: BASELINE -> REM CYCLE -> FLOW STATE.

Follows the PreemptionFsmEngine pattern:
  - decide() is a PURE function: no I/O, no awaits, no mutations.
  - apply_last_decision() persists state and emits side effects.
  - Crash recovery always returns to BASELINE (default-safe).

Transition triggers are documented in spec $1.
Budget caps configured via environment variables (spec $9).
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

from backend.hive.thread_models import CognitiveState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (all env-driven, spec $9)
# ---------------------------------------------------------------------------

_REM_INTERVAL_H = float(os.environ.get("JARVIS_HIVE_REM_INTERVAL_H", "6"))
_REM_LOAD_THRESHOLD = float(os.environ.get("JARVIS_HIVE_REM_LOAD_THRESHOLD", "30"))
_REM_MAX_CALLS = int(os.environ.get("JARVIS_HIVE_REM_MAX_CALLS", "50"))
_FLOW_DEBATE_TIMEOUT_M = float(os.environ.get("JARVIS_HIVE_FLOW_DEBATE_TIMEOUT_M", "15"))
_FLOW_TOKEN_CEILING = int(os.environ.get("JARVIS_HIVE_FLOW_TOKEN_CEILING", "50000"))
_STATE_DIR = Path(os.environ.get("JARVIS_HIVE_STATE_DIR", str(Path.home() / ".jarvis" / "hive")))


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class CognitiveEvent(str, Enum):
    """Events that drive FSM transitions."""
    REM_TRIGGER = "rem_trigger"
    FLOW_TRIGGER = "flow_trigger"
    COUNCIL_ESCALATION = "council_escalation"
    COUNCIL_COMPLETE = "council_complete"
    SPINDOWN = "spindown"
    USER_SPINDOWN = "user_spindown"


# ---------------------------------------------------------------------------
# Decision (pure output of decide())
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CognitiveTransition:
    """Immutable decision produced by CognitiveFsm.decide()."""
    from_state: CognitiveState
    to_state: CognitiveState
    event: CognitiveEvent
    reason_code: str
    noop: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    decided_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


# ---------------------------------------------------------------------------
# FSM (pure logic)
# ---------------------------------------------------------------------------


class CognitiveFsm:
    """Pure-logic FSM for cognitive state transitions.

    decide() returns a CognitiveTransition without mutating state.
    apply_last_decision() commits the transition.
    """

    def __init__(
        self,
        state_file: Optional[Path] = None,
        crash_recovery: bool = False,
    ) -> None:
        self._state_file = state_file or (_STATE_DIR / "cognitive_state.json")
        self._state = CognitiveState.BASELINE
        self._entered_at = datetime.now(tz=timezone.utc)
        self._last_decision: Optional[CognitiveTransition] = None

        if crash_recovery and self._state_file.exists():
            # Crash recovery: always reset to BASELINE (spec $1 safety invariant)
            logger.warning(
                "[CognitiveFsm] Crash recovery: resetting to BASELINE from persisted state"
            )
            self._state = CognitiveState.BASELINE
        elif not crash_recovery and self._state_file.exists():
            self._load_state()

    @property
    def state(self) -> CognitiveState:
        return self._state

    @property
    def entered_at(self) -> datetime:
        return self._entered_at

    def decide(
        self,
        event: CognitiveEvent,
        *,
        idle_seconds: float = 0.0,
        system_load_pct: float = 0.0,
        graduation_candidates: int = 0,
        spindown_reason: str = "",
    ) -> CognitiveTransition:
        """Pure transition function. No I/O, no mutations.

        Returns a CognitiveTransition describing the decision.
        Call apply_last_decision() to commit it.
        """
        current = self._state

        # USER_SPINDOWN overrides everything (spec $1 safety)
        if event == CognitiveEvent.USER_SPINDOWN:
            if current == CognitiveState.BASELINE:
                decision = self._noop(current, event, "USER_SPINDOWN_ALREADY_BASELINE")
            else:
                decision = CognitiveTransition(
                    from_state=current,
                    to_state=CognitiveState.BASELINE,
                    event=event,
                    reason_code="USER_MANUAL_SPINDOWN",
                )
            self._last_decision = decision
            return decision

        if current == CognitiveState.BASELINE:
            decision = self._from_baseline(event, idle_seconds, system_load_pct, graduation_candidates)
        elif current == CognitiveState.REM:
            decision = self._from_rem(event, spindown_reason)
        elif current == CognitiveState.FLOW:
            decision = self._from_flow(event, spindown_reason)
        else:
            decision = self._noop(current, event, "UNKNOWN_STATE")

        self._last_decision = decision
        return decision

    def apply_last_decision(self) -> Optional[CognitiveTransition]:
        """Commit the last decision: mutate state and persist."""
        if self._last_decision is None or self._last_decision.noop:
            return self._last_decision

        d = self._last_decision
        self._state = d.to_state
        self._entered_at = d.decided_at
        self._persist_state()
        logger.info(
            "[CognitiveFsm] %s -> %s (%s)",
            d.from_state.value, d.to_state.value, d.reason_code,
        )
        return d

    # --- Per-state handlers (pure) ---

    def _from_baseline(
        self,
        event: CognitiveEvent,
        idle_seconds: float,
        system_load_pct: float,
        graduation_candidates: int,
    ) -> CognitiveTransition:
        if event == CognitiveEvent.REM_TRIGGER:
            min_idle = _REM_INTERVAL_H * 3600
            if idle_seconds < min_idle:
                return self._noop(CognitiveState.BASELINE, event, "T1_BLOCKED_LOW_IDLE")
            if system_load_pct > _REM_LOAD_THRESHOLD:
                return self._noop(CognitiveState.BASELINE, event, "T1_BLOCKED_HIGH_LOAD")
            return CognitiveTransition(
                from_state=CognitiveState.BASELINE,
                to_state=CognitiveState.REM,
                event=event,
                reason_code="T1_REM_TRIGGER",
                metadata={
                    "idle_seconds": idle_seconds,
                    "system_load_pct": system_load_pct,
                    "graduation_candidates": graduation_candidates,
                },
            )
        if event == CognitiveEvent.FLOW_TRIGGER:
            return CognitiveTransition(
                from_state=CognitiveState.BASELINE,
                to_state=CognitiveState.FLOW,
                event=event,
                reason_code="T2_FLOW_TRIGGER",
            )
        return self._noop(CognitiveState.BASELINE, event, f"BASELINE_IGNORES_{event.value.upper()}")

    def _from_rem(self, event: CognitiveEvent, spindown_reason: str) -> CognitiveTransition:
        if event == CognitiveEvent.COUNCIL_ESCALATION:
            return CognitiveTransition(
                from_state=CognitiveState.REM,
                to_state=CognitiveState.FLOW,
                event=event,
                reason_code="T2B_COUNCIL_ESCALATION",
            )
        if event == CognitiveEvent.COUNCIL_COMPLETE:
            return CognitiveTransition(
                from_state=CognitiveState.REM,
                to_state=CognitiveState.BASELINE,
                event=event,
                reason_code="T3B_COUNCIL_COMPLETE",
            )
        if event == CognitiveEvent.SPINDOWN:
            return CognitiveTransition(
                from_state=CognitiveState.REM,
                to_state=CognitiveState.BASELINE,
                event=event,
                reason_code=f"T3_SPINDOWN_{spindown_reason.upper()}",
            )
        return self._noop(CognitiveState.REM, event, f"REM_IGNORES_{event.value.upper()}")

    def _from_flow(self, event: CognitiveEvent, spindown_reason: str) -> CognitiveTransition:
        if event == CognitiveEvent.SPINDOWN:
            return CognitiveTransition(
                from_state=CognitiveState.FLOW,
                to_state=CognitiveState.BASELINE,
                event=event,
                reason_code=f"T3_SPINDOWN_{spindown_reason.upper()}",
            )
        return self._noop(CognitiveState.FLOW, event, f"FLOW_IGNORES_{event.value.upper()}")

    # --- Helpers ---

    def _noop(self, state: CognitiveState, event: CognitiveEvent, reason: str) -> CognitiveTransition:
        return CognitiveTransition(
            from_state=state,
            to_state=state,
            event=event,
            reason_code=reason,
            noop=True,
        )

    def _persist_state(self) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(json.dumps({
            "state": self._state.value,
            "entered_at": self._entered_at.isoformat(),
        }))

    def _load_state(self) -> None:
        try:
            data = json.loads(self._state_file.read_text())
            self._state = CognitiveState(data["state"])
            self._entered_at = datetime.fromisoformat(data["entered_at"])
        except (json.JSONDecodeError, KeyError, ValueError):
            logger.warning("[CognitiveFsm] Corrupt state file, resetting to BASELINE")
            self._state = CognitiveState.BASELINE
```

- [ ] **4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_hive_cognitive_fsm.py -v`
Expected: ALL PASS

- [ ] **5: Commit**

```bash
git add backend/hive/cognitive_fsm.py tests/test_hive_cognitive_fsm.py
git commit -m "feat(hive): add Dynamic Cognitive State Machine (3-state FSM)"
```

---

## Task 3: Thread Manager

**Files:**
- Create: `backend/hive/thread_manager.py`
- Create: `tests/test_hive_thread_manager.py`

The ThreadManager owns thread lifecycle, consensus detection, storage, and the debate timeout watchdog.

- [ ] **1: Write failing tests**

```python
# tests/test_hive_thread_manager.py
"""Tests for Hive ThreadManager — lifecycle, consensus, storage."""
import json
from pathlib import Path

import pytest
import pytest_asyncio

from backend.hive.thread_manager import ThreadManager
from backend.hive.thread_models import (
    AgentLogMessage,
    CognitiveState,
    HiveThread,
    PersonaIntent,
    PersonaReasoningMessage,
    ThreadState,
)


@pytest.fixture
def storage_dir(tmp_path):
    return tmp_path / "threads"


@pytest.fixture
def manager(storage_dir):
    return ThreadManager(storage_dir=storage_dir, debate_timeout_s=10.0, token_ceiling=5000)


class TestThreadLifecycle:

    def test_create_thread(self, manager):
        thread = manager.create_thread(
            title="Memory Pressure",
            trigger_event="health_monitor_agent:memory_pressure",
            cognitive_state=CognitiveState.FLOW,
        )
        assert thread.state == ThreadState.OPEN
        assert thread.thread_id in manager.active_threads

    def test_transition_to_debating(self, manager):
        thread = manager.create_thread(
            title="Test", trigger_event="test", cognitive_state=CognitiveState.FLOW,
        )
        manager.transition(thread.thread_id, ThreadState.DEBATING)
        assert manager.get_thread(thread.thread_id).state == ThreadState.DEBATING

    def test_invalid_transition_raises(self, manager):
        thread = manager.create_thread(
            title="Test", trigger_event="test", cognitive_state=CognitiveState.FLOW,
        )
        with pytest.raises(ValueError, match="Invalid transition"):
            manager.transition(thread.thread_id, ThreadState.EXECUTING)

    def test_add_agent_log(self, manager):
        thread = manager.create_thread(
            title="Test", trigger_event="test", cognitive_state=CognitiveState.FLOW,
        )
        log = AgentLogMessage(
            thread_id=thread.thread_id,
            agent_name="health_monitor_agent",
            trinity_parent="jarvis",
            severity="warning",
            category="memory_pressure",
            payload={"value": 87.3},
        )
        manager.add_message(thread.thread_id, log)
        assert len(manager.get_thread(thread.thread_id).messages) == 1


class TestConsensusDetection:

    def _build_full_debate(self, manager) -> str:
        """Helper: create thread with observe + propose + validate(approve)."""
        thread = manager.create_thread(
            title="Test", trigger_event="test", cognitive_state=CognitiveState.FLOW,
        )
        tid = thread.thread_id
        manager.transition(tid, ThreadState.DEBATING)

        manager.add_message(tid, AgentLogMessage(
            thread_id=tid, agent_name="health_monitor", trinity_parent="jarvis",
            severity="warning", category="mem", payload={},
        ))
        manager.add_message(tid, PersonaReasoningMessage(
            thread_id=tid, persona="jarvis", role="body",
            intent=PersonaIntent.OBSERVE, references=[], reasoning="RAM high",
            confidence=0.9, model_used="test", token_cost=100,
        ))
        manager.add_message(tid, PersonaReasoningMessage(
            thread_id=tid, persona="j_prime", role="mind",
            intent=PersonaIntent.PROPOSE, references=[], reasoning="Add TTL eviction",
            confidence=0.87, model_used="test", token_cost=200,
        ))
        manager.add_message(tid, PersonaReasoningMessage(
            thread_id=tid, persona="reactor", role="immune_system",
            intent=PersonaIntent.VALIDATE, references=[], reasoning="AST clean",
            confidence=0.95, model_used="test", token_cost=150,
            validate_verdict="approve",
        ))
        return tid

    def test_consensus_detected(self, manager):
        tid = self._build_full_debate(manager)
        assert manager.check_consensus(tid) is True

    def test_consensus_not_reached_without_observe(self, manager):
        thread = manager.create_thread(
            title="Test", trigger_event="test", cognitive_state=CognitiveState.FLOW,
        )
        tid = thread.thread_id
        manager.transition(tid, ThreadState.DEBATING)
        # Skip JARVIS observe, go straight to propose + validate
        manager.add_message(tid, PersonaReasoningMessage(
            thread_id=tid, persona="j_prime", role="mind",
            intent=PersonaIntent.PROPOSE, references=[], reasoning="Fix",
            confidence=0.87, model_used="test", token_cost=200,
        ))
        manager.add_message(tid, PersonaReasoningMessage(
            thread_id=tid, persona="reactor", role="immune_system",
            intent=PersonaIntent.VALIDATE, references=[], reasoning="OK",
            confidence=0.95, model_used="test", token_cost=150,
            validate_verdict="approve",
        ))
        assert manager.check_consensus(tid) is False

    def test_consensus_not_reached_on_reject(self, manager):
        thread = manager.create_thread(
            title="Test", trigger_event="test", cognitive_state=CognitiveState.FLOW,
        )
        tid = thread.thread_id
        manager.transition(tid, ThreadState.DEBATING)
        manager.add_message(tid, PersonaReasoningMessage(
            thread_id=tid, persona="jarvis", role="body",
            intent=PersonaIntent.OBSERVE, references=[], reasoning="RAM high",
            confidence=0.9, model_used="test", token_cost=100,
        ))
        manager.add_message(tid, PersonaReasoningMessage(
            thread_id=tid, persona="j_prime", role="mind",
            intent=PersonaIntent.PROPOSE, references=[], reasoning="Fix",
            confidence=0.87, model_used="test", token_cost=200,
        ))
        manager.add_message(tid, PersonaReasoningMessage(
            thread_id=tid, persona="reactor", role="immune_system",
            intent=PersonaIntent.VALIDATE, references=[], reasoning="Too risky",
            confidence=0.3, model_used="test", token_cost=150,
            validate_verdict="reject",
        ))
        assert manager.check_consensus(tid) is False

    def test_transition_to_consensus_on_detection(self, manager):
        tid = self._build_full_debate(manager)
        manager.check_and_advance(tid)
        assert manager.get_thread(tid).state == ThreadState.CONSENSUS


class TestBudgetEnforcement:

    def test_budget_exhaustion_marks_stale(self, manager):
        thread = manager.create_thread(
            title="Test", trigger_event="test", cognitive_state=CognitiveState.FLOW,
        )
        tid = thread.thread_id
        manager.transition(tid, ThreadState.DEBATING)
        # Consume entire budget (5000 tokens)
        manager.add_message(tid, PersonaReasoningMessage(
            thread_id=tid, persona="j_prime", role="mind",
            intent=PersonaIntent.PROPOSE, references=[], reasoning="big proposal",
            confidence=0.9, model_used="test", token_cost=5000,
        ))
        manager.check_and_advance(tid)
        assert manager.get_thread(tid).state == ThreadState.STALE


class TestStorage:

    def test_persist_and_load(self, manager, storage_dir):
        thread = manager.create_thread(
            title="Persist Test", trigger_event="test", cognitive_state=CognitiveState.FLOW,
        )
        tid = thread.thread_id
        manager.persist_thread(tid)
        assert (storage_dir / f"{tid}.json").exists()

        # Create new manager, load from disk
        manager2 = ThreadManager(storage_dir=storage_dir, debate_timeout_s=10.0, token_ceiling=5000)
        manager2.load_threads()
        loaded = manager2.get_thread(tid)
        assert loaded is not None
        assert loaded.title == "Persist Test"
```

- [ ] **2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_hive_thread_manager.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **3: Implement thread_manager.py**

```python
# backend/hive/thread_manager.py
"""Thread lifecycle manager for the Autonomous Engineering Hive.

Owns thread creation, state transitions, consensus detection, budget
enforcement, and persistent storage. Each thread is a scoped conversation
unit that maps 1:1 to a potential Ouroboros pipeline run.

Consensus rule (spec $2, $7): Reactor's validate(approve) is the gate.
Prerequisites: at least one observe (JARVIS) + one propose (J-Prime).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict, Optional

from backend.hive.thread_models import (
    AgentLogMessage,
    CognitiveState,
    HiveMessage,
    HiveThread,
    PersonaReasoningMessage,
    ThreadState,
    _now_utc,
)

logger = logging.getLogger(__name__)

# Legal thread transitions
_TRANSITIONS: Dict[ThreadState, set] = {
    ThreadState.OPEN: {ThreadState.DEBATING, ThreadState.STALE},
    ThreadState.DEBATING: {ThreadState.CONSENSUS, ThreadState.STALE},
    ThreadState.CONSENSUS: {ThreadState.EXECUTING},
    ThreadState.EXECUTING: {ThreadState.RESOLVED, ThreadState.STALE},
    ThreadState.RESOLVED: set(),
    ThreadState.STALE: set(),
}


class ThreadManager:
    """Manages Hive thread lifecycle, consensus detection, and storage."""

    def __init__(
        self,
        storage_dir: Optional[Path] = None,
        debate_timeout_s: float = 900.0,
        token_ceiling: int = 50000,
    ) -> None:
        self._storage_dir = storage_dir or Path(
            os.environ.get("JARVIS_HIVE_STATE_DIR", str(Path.home() / ".jarvis" / "hive"))
        ) / "threads"
        self._debate_timeout_s = debate_timeout_s
        self._token_ceiling = token_ceiling
        self._threads: Dict[str, HiveThread] = {}

    @property
    def active_threads(self) -> Dict[str, HiveThread]:
        return self._threads

    def create_thread(
        self,
        title: str,
        trigger_event: str,
        cognitive_state: CognitiveState,
    ) -> HiveThread:
        """Create a new thread in OPEN state."""
        thread = HiveThread(
            title=title,
            trigger_event=trigger_event,
            cognitive_state=cognitive_state,
            token_budget=self._token_ceiling,
            debate_deadline_s=self._debate_timeout_s,
        )
        self._threads[thread.thread_id] = thread
        logger.info("[ThreadManager] Created thread %s: %s", thread.thread_id, title)
        return thread

    def get_thread(self, thread_id: str) -> Optional[HiveThread]:
        return self._threads.get(thread_id)

    def transition(self, thread_id: str, new_state: ThreadState) -> None:
        """Transition a thread to a new state with validation."""
        thread = self._threads.get(thread_id)
        if thread is None:
            raise KeyError(f"Thread {thread_id} not found")

        legal = _TRANSITIONS.get(thread.state, set())
        if new_state not in legal:
            raise ValueError(
                f"Invalid transition: {thread.state.value} -> {new_state.value} "
                f"(legal: {[s.value for s in legal]})"
            )

        old = thread.state
        thread.state = new_state
        if new_state in (ThreadState.RESOLVED, ThreadState.STALE):
            thread.resolved_at = _now_utc()
        logger.info(
            "[ThreadManager] Thread %s: %s -> %s", thread_id, old.value, new_state.value
        )

    def add_message(self, thread_id: str, msg: HiveMessage) -> None:
        """Add a message to a thread."""
        thread = self._threads.get(thread_id)
        if thread is None:
            raise KeyError(f"Thread {thread_id} not found")
        thread.add_message(msg)

    def check_consensus(self, thread_id: str) -> bool:
        """Check if consensus prerequisites are met for a thread."""
        thread = self._threads.get(thread_id)
        if thread is None:
            return False
        return thread.is_consensus_ready()

    def check_and_advance(self, thread_id: str) -> Optional[ThreadState]:
        """Check thread state and advance if conditions are met.

        Returns the new state if a transition occurred, None otherwise.
        """
        thread = self._threads.get(thread_id)
        if thread is None:
            return None

        if thread.state != ThreadState.DEBATING:
            return None

        # Budget exhaustion -> STALE
        if thread.is_budget_exhausted():
            logger.warning(
                "[ThreadManager] Thread %s: budget exhausted (%d/%d tokens)",
                thread_id, thread.tokens_consumed, thread.token_budget,
            )
            self.transition(thread_id, ThreadState.STALE)
            return ThreadState.STALE

        # Consensus detection
        if thread.is_consensus_ready():
            self.transition(thread_id, ThreadState.CONSENSUS)
            return ThreadState.CONSENSUS

        return None

    def persist_thread(self, thread_id: str) -> None:
        """Persist a thread to disk as JSON."""
        thread = self._threads.get(thread_id)
        if thread is None:
            return
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        path = self._storage_dir / f"{thread_id}.json"
        path.write_text(json.dumps(thread.to_dict(), indent=2))

    def load_threads(self) -> int:
        """Load all persisted threads from disk. Returns count loaded."""
        if not self._storage_dir.exists():
            return 0
        count = 0
        for path in self._storage_dir.glob("thr_*.json"):
            try:
                data = json.loads(path.read_text())
                thread = HiveThread.from_dict(data)
                self._threads[thread.thread_id] = thread
                count += 1
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("[ThreadManager] Failed to load %s: %s", path, e)
        logger.info("[ThreadManager] Loaded %d threads from disk", count)
        return count
```

- [ ] **4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_hive_thread_manager.py -v`
Expected: ALL PASS

- [ ] **5: Commit**

```bash
git add backend/hive/thread_manager.py tests/test_hive_thread_manager.py
git commit -m "feat(hive): add ThreadManager with lifecycle, consensus detection, storage"
```

---

## Task 4: Model Router

**Files:**
- Create: `backend/hive/model_router.py`
- Create: `tests/test_hive_model_router.py`

Cognitive-state-aware model selection using verified Doubleword model IDs.

- [ ] **1: Write failing tests**

```python
# tests/test_hive_model_router.py
"""Tests for cognitive-state-aware model routing."""
import pytest

from backend.hive.model_router import HiveModelRouter
from backend.hive.thread_models import CognitiveState


def test_baseline_returns_none():
    router = HiveModelRouter()
    assert router.get_model(CognitiveState.BASELINE) is None


def test_rem_returns_35b():
    router = HiveModelRouter()
    assert router.get_model(CognitiveState.REM) == "Qwen/Qwen3.5-35B-A3B-FP8"


def test_flow_returns_397b():
    router = HiveModelRouter()
    assert router.get_model(CognitiveState.FLOW) == "Qwen/Qwen3.5-397B-A17B-FP8"


def test_embedding_model():
    router = HiveModelRouter()
    assert router.embedding_model == "Qwen/Qwen3-Embedding-8B"


def test_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_HIVE_REM_MODEL", "custom/rem-model")
    router = HiveModelRouter()
    assert router.get_model(CognitiveState.REM) == "custom/rem-model"


def test_get_config_returns_all_info():
    router = HiveModelRouter()
    cfg = router.get_config(CognitiveState.FLOW)
    assert cfg["model"] == "Qwen/Qwen3.5-397B-A17B-FP8"
    assert cfg["max_tokens"] > 0
    assert cfg["temperature"] >= 0
```

- [ ] **2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_hive_model_router.py -v`
Expected: FAIL

- [ ] **3: Implement model_router.py**

```python
# backend/hive/model_router.py
"""Cognitive-state-aware Doubleword model selection.

Routes to verified-live model IDs based on the current cognitive state.
All model IDs verified against Doubleword /v1/models on 2026-04-02.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from backend.hive.thread_models import CognitiveState

# Verified live model IDs (2026-04-02)
_DEFAULT_REM_MODEL = "Qwen/Qwen3.5-35B-A3B-FP8"
_DEFAULT_FLOW_MODEL = "Qwen/Qwen3.5-397B-A17B-FP8"
_DEFAULT_EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-8B"


class HiveModelRouter:
    """Selects the Doubleword model based on cognitive state."""

    def __init__(self) -> None:
        self._rem_model = os.environ.get("JARVIS_HIVE_REM_MODEL", _DEFAULT_REM_MODEL)
        self._flow_model = os.environ.get("JARVIS_HIVE_FLOW_MODEL", _DEFAULT_FLOW_MODEL)
        self._embedding_model = os.environ.get("JARVIS_HIVE_EMBEDDING_MODEL", _DEFAULT_EMBEDDING_MODEL)

    @property
    def embedding_model(self) -> str:
        return self._embedding_model

    def get_model(self, state: CognitiveState) -> Optional[str]:
        """Return the model ID for the given cognitive state, or None for BASELINE."""
        if state == CognitiveState.BASELINE:
            return None
        if state == CognitiveState.REM:
            return self._rem_model
        if state == CognitiveState.FLOW:
            return self._flow_model
        return None

    def get_config(self, state: CognitiveState) -> Dict[str, Any]:
        """Return full model config for a cognitive state."""
        model = self.get_model(state)
        if model is None:
            return {"model": None, "max_tokens": 0, "temperature": 0}

        if state == CognitiveState.REM:
            return {"model": model, "max_tokens": 4000, "temperature": 0.3}
        return {"model": model, "max_tokens": 10000, "temperature": 0.2}
```

- [ ] **4: Run tests**

Run: `python3 -m pytest tests/test_hive_model_router.py -v`
Expected: ALL PASS

- [ ] **5: Commit**

```bash
git add backend/hive/model_router.py tests/test_hive_model_router.py
git commit -m "feat(hive): add cognitive-state-aware model router"
```

---

## Task 5: Ouroboros Handoff

**Files:**
- Create: `backend/hive/ouroboros_handoff.py`
- Create: `tests/test_hive_ouroboros_handoff.py`

Serializes thread consensus into `OperationContext` using existing fields. Note: `OperationContext.create()` does not accept `strategic_memory_prompt` or `causal_trace_id` — we use `dataclasses.replace()` after creation.

- [ ] **1: Write failing tests**

```python
# tests/test_hive_ouroboros_handoff.py
"""Tests for thread consensus -> OperationContext serialization."""
import json

import pytest

from backend.core.ouroboros.governance.op_context import OperationContext, OperationPhase
from backend.hive.ouroboros_handoff import serialize_consensus
from backend.hive.thread_models import (
    AgentLogMessage,
    CognitiveState,
    HiveThread,
    PersonaIntent,
    PersonaReasoningMessage,
    ThreadState,
)


def _make_consensus_thread() -> HiveThread:
    """Build a thread that has reached CONSENSUS."""
    thread = HiveThread(
        title="Memory Pressure in Vision Loop",
        trigger_event="health_monitor_agent:memory_pressure",
        cognitive_state=CognitiveState.FLOW,
        token_budget=50000,
        debate_deadline_s=900.0,
    )
    thread.state = ThreadState.CONSENSUS

    thread.add_message(AgentLogMessage(
        thread_id=thread.thread_id,
        agent_name="health_monitor_agent",
        trinity_parent="jarvis",
        severity="warning",
        category="memory_pressure",
        payload={"metric": "ram_percent", "value": 87.3},
    ))
    thread.add_message(PersonaReasoningMessage(
        thread_id=thread.thread_id,
        persona="jarvis", role="body",
        intent=PersonaIntent.OBSERVE, references=[],
        reasoning="RAM pressure from vision loop SHM segments.",
        confidence=0.9, model_used="test", token_cost=100,
        manifesto_principle="$3 Spinal Cord",
    ))
    thread.add_message(PersonaReasoningMessage(
        thread_id=thread.thread_id,
        persona="j_prime", role="mind",
        intent=PersonaIntent.PROPOSE, references=[],
        reasoning="Add TTL eviction to FramePipeline (30s max age).",
        confidence=0.87, model_used="test", token_cost=200,
        manifesto_principle="$3 Spinal Cord",
    ))
    thread.add_message(PersonaReasoningMessage(
        thread_id=thread.thread_id,
        persona="reactor", role="immune_system",
        intent=PersonaIntent.VALIDATE, references=[],
        reasoning="AST clean. Low risk. Approved.",
        confidence=0.95, model_used="test", token_cost=150,
        validate_verdict="approve",
    ))
    return thread


def test_serialize_consensus_returns_operation_context():
    thread = _make_consensus_thread()
    ctx = serialize_consensus(thread, target_files=("backend/vision/frame_pipeline.py",))
    assert isinstance(ctx, OperationContext)
    assert ctx.phase == OperationPhase.CLASSIFY


def test_serialize_maps_description():
    thread = _make_consensus_thread()
    ctx = serialize_consensus(thread, target_files=("backend/vision/frame_pipeline.py",))
    assert "AST clean" in ctx.description
    assert "Approved" in ctx.description


def test_serialize_maps_target_files():
    thread = _make_consensus_thread()
    ctx = serialize_consensus(thread, target_files=("backend/vision/frame_pipeline.py",))
    assert ctx.target_files == ("backend/vision/frame_pipeline.py",)


def test_serialize_maps_causal_trace_id():
    thread = _make_consensus_thread()
    ctx = serialize_consensus(thread, target_files=("x.py",))
    assert ctx.causal_trace_id == thread.thread_id


def test_serialize_maps_correlation_id():
    thread = _make_consensus_thread()
    ctx = serialize_consensus(thread, target_files=("x.py",))
    assert ctx.correlation_id == thread.thread_id


def test_serialize_maps_strategic_memory_prompt():
    thread = _make_consensus_thread()
    ctx = serialize_consensus(thread, target_files=("x.py",))
    # strategic_memory_prompt contains serialized thread history
    assert "agent_log" in ctx.strategic_memory_prompt
    assert "persona_reasoning" in ctx.strategic_memory_prompt
    parsed = json.loads(ctx.strategic_memory_prompt)
    assert len(parsed["messages"]) == 4


def test_serialize_maps_human_instructions():
    thread = _make_consensus_thread()
    ctx = serialize_consensus(thread, target_files=("x.py",))
    assert "$3 Spinal Cord" in ctx.human_instructions


def test_serialize_rejects_non_consensus_thread():
    thread = _make_consensus_thread()
    thread.state = ThreadState.DEBATING
    with pytest.raises(ValueError, match="not in CONSENSUS"):
        serialize_consensus(thread, target_files=("x.py",))


def test_hash_chain_valid():
    thread = _make_consensus_thread()
    ctx = serialize_consensus(thread, target_files=("x.py",))
    assert ctx.context_hash  # Non-empty hash
    assert ctx.previous_hash is None  # Initial context
```

- [ ] **2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_hive_ouroboros_handoff.py -v`
Expected: FAIL

- [ ] **3: Implement ouroboros_handoff.py**

```python
# backend/hive/ouroboros_handoff.py
"""Thread consensus -> OperationContext serialization.

Maps HiveThread fields to existing OperationContext fields (spec $6):
  - description <- Reactor's validate message reasoning
  - target_files <- files referenced in debate
  - strategic_memory_prompt <- serialized thread history (JSON)
  - causal_trace_id <- thread_id
  - correlation_id <- thread_id
  - human_instructions <- Manifesto principles cited in thread
"""
from __future__ import annotations

import dataclasses
import json
from typing import Tuple

from backend.core.ouroboros.governance.op_context import OperationContext
from backend.hive.thread_models import (
    HiveThread,
    PersonaIntent,
    PersonaReasoningMessage,
    ThreadState,
)


def serialize_consensus(
    thread: HiveThread,
    *,
    target_files: Tuple[str, ...],
) -> OperationContext:
    """Serialize a CONSENSUS thread into an OperationContext for Ouroboros.

    Parameters
    ----------
    thread:
        HiveThread in CONSENSUS state.
    target_files:
        Files to target in the Ouroboros pipeline.

    Returns
    -------
    OperationContext in CLASSIFY phase, ready for GovernedLoopService.submit().

    Raises
    ------
    ValueError
        If thread is not in CONSENSUS state.
    """
    if thread.state != ThreadState.CONSENSUS:
        raise ValueError(
            f"Thread {thread.thread_id} is not in CONSENSUS state "
            f"(current: {thread.state.value})"
        )

    # Extract Reactor's approval reasoning as description
    description = _extract_consensus_description(thread)

    # Serialize thread history as JSON for strategic_memory_prompt
    thread_history = json.dumps({
        "thread_id": thread.thread_id,
        "title": thread.title,
        "trigger_event": thread.trigger_event,
        "messages": [m.to_dict() for m in thread.messages],
    })

    # Collect Manifesto principles
    principles = thread.manifesto_principles
    human_instructions = (
        f"Hive thread consensus — Manifesto principles: {', '.join(principles)}"
        if principles else ""
    )

    # Create base context via factory
    ctx = OperationContext.create(
        target_files=target_files,
        description=description,
        correlation_id=thread.thread_id,
    )

    # Stamp additional fields via replace (create() doesn't accept these)
    ctx = dataclasses.replace(
        ctx,
        causal_trace_id=thread.thread_id,
        strategic_memory_prompt=thread_history,
        human_instructions=human_instructions,
    )

    # Recompute hash since we mutated fields after create()
    from backend.core.ouroboros.governance.op_context import _compute_hash
    fields_dict = dataclasses.asdict(ctx)
    fields_dict.pop("context_hash", None)
    new_hash = _compute_hash(fields_dict)
    ctx = dataclasses.replace(ctx, context_hash=new_hash)

    return ctx


def _extract_consensus_description(thread: HiveThread) -> str:
    """Extract description from Reactor's approve message."""
    for msg in reversed(thread.messages):
        if (
            isinstance(msg, PersonaReasoningMessage)
            and msg.persona == "reactor"
            and msg.intent == PersonaIntent.VALIDATE
            and msg.validate_verdict == "approve"
        ):
            return msg.reasoning
    return thread.title
```

- [ ] **4: Run tests**

Run: `python3 -m pytest tests/test_hive_ouroboros_handoff.py -v`
Expected: ALL PASS

- [ ] **5: Commit**

```bash
git add backend/hive/ouroboros_handoff.py tests/test_hive_ouroboros_handoff.py
git commit -m "feat(hive): add Ouroboros handoff (thread consensus -> OperationContext)"
```

---

## Task 6: HUD Relay Agent

**Files:**
- Create: `backend/hive/hud_relay_agent.py`
- Create: `tests/test_hive_hud_relay.py`

Bridges the `AgentCommunicationBus` to IPC (port 8742) for native HUD rendering. Subscribes to Hive message types, batches, and projects as newline-delimited JSON.

- [ ] **1: Write failing tests**

```python
# tests/test_hive_hud_relay.py
"""Tests for HUD Relay Agent — bus to IPC projection."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from backend.hive.hud_relay_agent import HudRelayAgent
from backend.hive.thread_models import (
    AgentLogMessage,
    CognitiveState,
    PersonaIntent,
    PersonaReasoningMessage,
)
from backend.neural_mesh.data_models import AgentMessage, MessageType, MessagePriority


@pytest.fixture
def relay():
    agent = HudRelayAgent()
    agent._ipc_send = AsyncMock()
    return agent


@pytest.mark.asyncio
async def test_relay_projects_agent_log(relay):
    log = AgentLogMessage(
        thread_id="thr_test1",
        agent_name="health_monitor_agent",
        trinity_parent="jarvis",
        severity="warning",
        category="memory_pressure",
        payload={"value": 87.3},
    )
    await relay.project_message(log)
    relay._ipc_send.assert_called_once()
    call_data = relay._ipc_send.call_args[0][0]
    assert call_data["event_type"] == "agent_log"
    assert call_data["data"]["agent_name"] == "health_monitor_agent"


@pytest.mark.asyncio
async def test_relay_projects_persona_reasoning(relay):
    msg = PersonaReasoningMessage(
        thread_id="thr_test1",
        persona="j_prime", role="mind",
        intent=PersonaIntent.PROPOSE, references=[],
        reasoning="Add TTL eviction.",
        confidence=0.87, model_used="test", token_cost=200,
    )
    await relay.project_message(msg)
    relay._ipc_send.assert_called_once()
    call_data = relay._ipc_send.call_args[0][0]
    assert call_data["event_type"] == "persona_reasoning"


@pytest.mark.asyncio
async def test_relay_projects_thread_lifecycle(relay):
    await relay.project_lifecycle("thr_test1", "debating", {"thread_id": "thr_test1"})
    relay._ipc_send.assert_called_once()
    call_data = relay._ipc_send.call_args[0][0]
    assert call_data["event_type"] == "thread_lifecycle"
    assert call_data["data"]["state"] == "debating"


@pytest.mark.asyncio
async def test_relay_projects_cognitive_transition(relay):
    await relay.project_cognitive_transition("baseline", "flow", "T2_FLOW_TRIGGER")
    relay._ipc_send.assert_called_once()
    call_data = relay._ipc_send.call_args[0][0]
    assert call_data["event_type"] == "cognitive_transition"
    assert call_data["data"]["from_state"] == "baseline"
    assert call_data["data"]["to_state"] == "flow"


@pytest.mark.asyncio
async def test_relay_handles_ipc_failure_gracefully(relay):
    relay._ipc_send = AsyncMock(side_effect=ConnectionError("IPC down"))
    log = AgentLogMessage(
        thread_id="thr_test1",
        agent_name="test",
        trinity_parent="jarvis",
        severity="info",
        category="test",
        payload={},
    )
    # Should not raise
    await relay.project_message(log)


@pytest.mark.asyncio
async def test_relay_includes_monotonic_sequence(relay):
    log1 = AgentLogMessage(
        thread_id="thr_test1", agent_name="a", trinity_parent="jarvis",
        severity="info", category="test", payload={},
    )
    log2 = AgentLogMessage(
        thread_id="thr_test1", agent_name="b", trinity_parent="jarvis",
        severity="info", category="test", payload={},
    )
    await relay.project_message(log1)
    await relay.project_message(log2)
    seq1 = relay._ipc_send.call_args_list[0][0][0]["data"]["_seq"]
    seq2 = relay._ipc_send.call_args_list[1][0][0]["data"]["_seq"]
    assert seq2 > seq1
```

- [ ] **2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_hive_hud_relay.py -v`
Expected: FAIL

- [ ] **3: Implement hud_relay_agent.py**

```python
# backend/hive/hud_relay_agent.py
"""HUD Relay Agent — bridges AgentCommunicationBus to IPC (port 8742).

v1 projection path (spec $5): IPC only. The brainstem bridges IPC -> Vercel SSE
via command_sender.py. Hive events use the new event types:
  - agent_log
  - persona_reasoning
  - thread_lifecycle
  - cognitive_transition

Message ordering: monotonic sequence numbers per relay instance.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Coroutine, Dict, Optional

from backend.hive.thread_models import (
    AgentLogMessage,
    HiveMessage,
    PersonaReasoningMessage,
)

logger = logging.getLogger(__name__)


class HudRelayAgent:
    """Projects Hive messages to the native HUD via IPC.

    In production, _ipc_send is wired to the brainstem TCP connection.
    In tests, it is mocked.
    """

    def __init__(
        self,
        ipc_send: Optional[Callable[[Dict[str, Any]], Coroutine]] = None,
    ) -> None:
        self._ipc_send: Callable[[Dict[str, Any]], Coroutine] = ipc_send or self._noop_send
        self._seq = 0

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def _noop_send(self, data: Dict[str, Any]) -> None:
        """Default no-op sender when IPC not connected."""
        pass

    async def project_message(self, msg: HiveMessage) -> None:
        """Project a Tier 1 or Tier 2 message to the HUD."""
        try:
            payload = msg.to_dict()
            payload["_seq"] = self._next_seq()
            envelope = {
                "event_type": msg.type,
                "data": payload,
            }
            await self._ipc_send(envelope)
        except Exception:
            logger.debug("[HudRelay] Failed to project message", exc_info=True)

    async def project_lifecycle(
        self, thread_id: str, state: str, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Project a thread lifecycle event."""
        try:
            data = {"thread_id": thread_id, "state": state, "_seq": self._next_seq()}
            if metadata:
                data.update(metadata)
            await self._ipc_send({"event_type": "thread_lifecycle", "data": data})
        except Exception:
            logger.debug("[HudRelay] Failed to project lifecycle", exc_info=True)

    async def project_cognitive_transition(
        self, from_state: str, to_state: str, reason_code: str
    ) -> None:
        """Project a cognitive state transition event."""
        try:
            await self._ipc_send({
                "event_type": "cognitive_transition",
                "data": {
                    "from_state": from_state,
                    "to_state": to_state,
                    "reason_code": reason_code,
                    "_seq": self._next_seq(),
                },
            })
        except Exception:
            logger.debug("[HudRelay] Failed to project cognitive transition", exc_info=True)
```

- [ ] **4: Run tests**

Run: `python3 -m pytest tests/test_hive_hud_relay.py -v`
Expected: ALL PASS

- [ ] **5: Commit**

```bash
git add backend/hive/hud_relay_agent.py tests/test_hive_hud_relay.py
git commit -m "feat(hive): add HUD Relay Agent (bus -> IPC projection)"
```

---

## Task 7: Integration Test

**Files:**
- Create: `tests/test_hive_integration.py`

End-to-end test: agent_log -> persona debate -> consensus -> Ouroboros handoff.

- [ ] **1: Write integration test**

```python
# tests/test_hive_integration.py
"""Integration test: full Hive pipeline from agent_log to Ouroboros handoff."""
import pytest

from backend.core.ouroboros.governance.op_context import OperationContext, OperationPhase
from backend.hive.cognitive_fsm import CognitiveEvent, CognitiveFsm
from backend.hive.hud_relay_agent import HudRelayAgent
from backend.hive.model_router import HiveModelRouter
from backend.hive.ouroboros_handoff import serialize_consensus
from backend.hive.thread_manager import ThreadManager
from backend.hive.thread_models import (
    AgentLogMessage,
    CognitiveState,
    PersonaIntent,
    PersonaReasoningMessage,
    ThreadState,
)


class TestHiveIntegration:
    """Full pipeline: detect gap -> debate -> consensus -> handoff."""

    def test_full_pipeline(self, tmp_path):
        # 1. FSM starts in BASELINE
        fsm = CognitiveFsm(state_file=tmp_path / "fsm.json")
        assert fsm.state == CognitiveState.BASELINE

        # 2. Model router confirms BASELINE = no model
        router = HiveModelRouter()
        assert router.get_model(fsm.state) is None

        # 3. FLOW_TRIGGER fires (critical gap detected)
        decision = fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        assert decision.to_state == CognitiveState.FLOW
        fsm.apply_last_decision()
        assert fsm.state == CognitiveState.FLOW

        # 4. Model router now returns 397B
        assert router.get_model(fsm.state) == "Qwen/Qwen3.5-397B-A17B-FP8"

        # 5. ThreadManager creates thread
        mgr = ThreadManager(
            storage_dir=tmp_path / "threads",
            debate_timeout_s=900.0,
            token_ceiling=50000,
        )
        thread = mgr.create_thread(
            title="Memory Pressure in Vision Loop",
            trigger_event="health_monitor_agent:memory_pressure",
            cognitive_state=CognitiveState.FLOW,
        )
        tid = thread.thread_id

        # 6. Transition to DEBATING
        mgr.transition(tid, ThreadState.DEBATING)

        # 7. Specialist log arrives
        mgr.add_message(tid, AgentLogMessage(
            thread_id=tid,
            agent_name="health_monitor_agent",
            trinity_parent="jarvis",
            severity="warning",
            category="memory_pressure",
            payload={"metric": "ram_percent", "value": 87.3},
        ))

        # 8. JARVIS observes
        mgr.add_message(tid, PersonaReasoningMessage(
            thread_id=tid, persona="jarvis", role="body",
            intent=PersonaIntent.OBSERVE, references=[],
            reasoning="RAM pressure from stale SHM segments.",
            confidence=0.9, model_used="Qwen/Qwen3.5-397B-A17B-FP8", token_cost=500,
            manifesto_principle="$3 Spinal Cord",
        ))

        # 9. J-Prime proposes
        mgr.add_message(tid, PersonaReasoningMessage(
            thread_id=tid, persona="j_prime", role="mind",
            intent=PersonaIntent.PROPOSE, references=[],
            reasoning="Add TTL eviction to FramePipeline with 30s max age.",
            confidence=0.87, model_used="Qwen/Qwen3.5-397B-A17B-FP8", token_cost=1200,
            manifesto_principle="$3 Spinal Cord",
        ))

        # 10. Reactor validates (approve)
        mgr.add_message(tid, PersonaReasoningMessage(
            thread_id=tid, persona="reactor", role="immune_system",
            intent=PersonaIntent.VALIDATE, references=[],
            reasoning="AST clean. Low risk. Approved for synthesis.",
            confidence=0.95, model_used="Qwen/Qwen3.5-397B-A17B-FP8", token_cost=800,
            validate_verdict="approve",
        ))

        # 11. Consensus detected, thread advances
        new_state = mgr.check_and_advance(tid)
        assert new_state == ThreadState.CONSENSUS
        assert mgr.get_thread(tid).state == ThreadState.CONSENSUS

        # 12. Ouroboros handoff
        ctx = serialize_consensus(
            mgr.get_thread(tid),
            target_files=("backend/vision/frame_pipeline.py",),
        )
        assert isinstance(ctx, OperationContext)
        assert ctx.phase == OperationPhase.CLASSIFY
        assert ctx.causal_trace_id == tid
        assert ctx.target_files == ("backend/vision/frame_pipeline.py",)
        assert "AST clean" in ctx.description

        # 13. Link thread to operation
        mgr.get_thread(tid).linked_op_id = ctx.op_id
        mgr.transition(tid, ThreadState.EXECUTING)
        assert mgr.get_thread(tid).state == ThreadState.EXECUTING
        assert mgr.get_thread(tid).linked_op_id == ctx.op_id

        # 14. Persist and verify
        mgr.persist_thread(tid)
        assert (tmp_path / "threads" / f"{tid}.json").exists()

        # 15. FSM spins down
        spindown = fsm.decide(CognitiveEvent.SPINDOWN, spindown_reason="pr_merged")
        assert spindown.to_state == CognitiveState.BASELINE
        fsm.apply_last_decision()
        assert fsm.state == CognitiveState.BASELINE

    def test_budget_exhaustion_pipeline(self, tmp_path):
        """Thread goes STALE when token budget is exceeded."""
        fsm = CognitiveFsm(state_file=tmp_path / "fsm.json")
        fsm.decide(CognitiveEvent.FLOW_TRIGGER)
        fsm.apply_last_decision()

        mgr = ThreadManager(
            storage_dir=tmp_path / "threads",
            debate_timeout_s=900.0,
            token_ceiling=1000,  # Very low budget
        )
        thread = mgr.create_thread(
            title="Budget Test", trigger_event="test",
            cognitive_state=CognitiveState.FLOW,
        )
        tid = thread.thread_id
        mgr.transition(tid, ThreadState.DEBATING)

        # Consume entire budget in one message
        mgr.add_message(tid, PersonaReasoningMessage(
            thread_id=tid, persona="j_prime", role="mind",
            intent=PersonaIntent.PROPOSE, references=[],
            reasoning="Very expensive proposal",
            confidence=0.9, model_used="test", token_cost=1001,
        ))

        new_state = mgr.check_and_advance(tid)
        assert new_state == ThreadState.STALE

        # Handoff should fail on STALE thread
        with pytest.raises(ValueError, match="not in CONSENSUS"):
            serialize_consensus(mgr.get_thread(tid), target_files=("x.py",))
```

- [ ] **2: Run integration tests**

Run: `python3 -m pytest tests/test_hive_integration.py -v`
Expected: ALL PASS

- [ ] **3: Run full test suite**

Run: `python3 -m pytest tests/test_hive_*.py -v`
Expected: ALL PASS

- [ ] **4: Commit**

```bash
git add tests/test_hive_integration.py
git commit -m "test(hive): add end-to-end integration test (agent_log -> consensus -> handoff)"
```

---

## Summary

| Task | Component | Tests | Dependencies |
|------|-----------|-------|-------------|
| 1 | Thread Data Models + MessageType entries | 14 | None |
| 2 | Cognitive State Machine (FSM) | 15 | Task 1 (CognitiveState enum) |
| 3 | Thread Manager | 11 | Task 1 (thread models) |
| 4 | Model Router | 6 | Task 1 (CognitiveState enum) |
| 5 | Ouroboros Handoff | 9 | Task 1 + Task 3 |
| 6 | HUD Relay Agent | 6 | Task 1 |
| 7 | Integration Test | 2 | Tasks 1-6 |

**Total: 7 tasks, ~63 tests**

Tasks 2, 3, 4, and 6 depend only on Task 1 and can be parallelized after Task 1 completes. Task 5 depends on Tasks 1 and 3. Task 7 depends on all.

**Not in this plan (separate plans):**
- `persona_engine.py` — Trinity Persona reasoning orchestrator (requires Doubleword API integration patterns, prompt engineering)
- `hive_service.py` — Top-level orchestrator (wires all components, boot/shutdown, bus subscriptions)
- SwiftUI `HiveView.swift` + `HiveStore.swift` — Native HUD tab
- `jarvis-cloud/app/dashboard/hive/page.tsx` — Web dashboard
