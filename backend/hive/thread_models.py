"""
Hive Thread Data Models

Foundational data layer for the Autonomous Engineering Hive.
Defines message types, thread lifecycle, and cognitive state models
used by every other Hive component (FSM, ThreadManager, HandOff, HUD Relay).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Literal, Optional, Union


# ============================================================================
# ENUMS
# ============================================================================


class CognitiveState(str, Enum):
    """Cognitive state of the system when a thread is created or transitions."""

    BASELINE = "baseline"
    REM = "rem"
    FLOW = "flow"


class ThreadState(str, Enum):
    """Lifecycle state of a Hive thread."""

    OPEN = "open"
    DEBATING = "debating"
    CONSENSUS = "consensus"
    EXECUTING = "executing"
    RESOLVED = "resolved"
    STALE = "stale"


class PersonaIntent(str, Enum):
    """Intent behind a persona's message in a thread."""

    OBSERVE = "observe"
    PROPOSE = "propose"
    CHALLENGE = "challenge"
    SUPPORT = "support"
    VALIDATE = "validate"


# ============================================================================
# HELPERS
# ============================================================================


def _gen_msg_id() -> str:
    """Generate a unique message ID."""
    return f"msg_{uuid.uuid4().hex[:12]}"


def _gen_thread_id() -> str:
    """Generate a unique thread ID."""
    return f"thr_{uuid.uuid4().hex[:12]}"


def _now_utc() -> datetime:
    """Return the current UTC datetime (timezone-aware)."""
    return datetime.now(tz=timezone.utc)


# ============================================================================
# TIER 1 — SPECIALIST TELEMETRY (no LLM)
# ============================================================================


@dataclass
class AgentLogMessage:
    """Tier 1 message: specialist telemetry from any agent. No LLM involved.

    Attributes:
        thread_id: ID of the parent thread.
        agent_name: Name of the emitting agent.
        trinity_parent: Which Trinity persona owns the agent.
        severity: Log severity level.
        category: Freeform category tag (e.g. "build", "test", "lint").
        payload: Arbitrary structured data for the log entry.
    """

    # --- Required fields (caller must supply) ---
    thread_id: str
    agent_name: str
    trinity_parent: Literal["jarvis", "j_prime", "reactor"]
    severity: Literal["info", "warning", "error", "critical"]
    category: str
    payload: Dict = field(default_factory=dict)

    # --- Auto fields ---
    type: str = field(default="agent_log", init=False)
    message_id: str = field(default_factory=_gen_msg_id)
    ts: datetime = field(default_factory=_now_utc)
    monotonic_ns: int = field(default_factory=time.monotonic_ns)

    def to_dict(self) -> Dict:
        """Serialize to a plain dictionary."""
        return {
            "type": self.type,
            "message_id": self.message_id,
            "thread_id": self.thread_id,
            "agent_name": self.agent_name,
            "trinity_parent": self.trinity_parent,
            "severity": self.severity,
            "category": self.category,
            "payload": self.payload,
            "ts": self.ts.isoformat(),
            "monotonic_ns": self.monotonic_ns,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "AgentLogMessage":
        """Deserialize from a plain dictionary."""
        obj = cls(
            thread_id=data["thread_id"],
            agent_name=data["agent_name"],
            trinity_parent=data["trinity_parent"],
            severity=data["severity"],
            category=data["category"],
            payload=data.get("payload", {}),
        )
        obj.message_id = data.get("message_id", obj.message_id)
        obj.ts = (
            datetime.fromisoformat(data["ts"])
            if isinstance(data.get("ts"), str)
            else data.get("ts", obj.ts)
        )
        obj.monotonic_ns = data.get("monotonic_ns", obj.monotonic_ns)
        return obj


# ============================================================================
# TIER 2 — TRINITY DEBATE VOICES
# ============================================================================


@dataclass
class PersonaReasoningMessage:
    """Tier 2 message: a Trinity persona's contribution to a thread debate.

    Attributes:
        thread_id: ID of the parent thread.
        persona: Which Trinity persona is speaking.
        role: Functional role of the persona.
        intent: What the persona intends with this message.
        references: List of file paths, URLs, or IDs referenced.
        reasoning: The actual reasoning text (LLM-generated or template).
        confidence: Confidence score [0.0, 1.0].
        model_used: Model identifier (e.g. "claude-sonnet-4-20250514", "qwen-7b").
        token_cost: Token count consumed for this message.
    """

    # --- Required fields ---
    thread_id: str
    persona: Literal["jarvis", "j_prime", "reactor"]
    role: Literal["body", "mind", "immune_system"]
    intent: PersonaIntent
    references: List[str]
    reasoning: str
    confidence: float
    model_used: str
    token_cost: int

    # --- Auto fields ---
    type: str = field(default="persona_reasoning", init=False)
    message_id: str = field(default_factory=_gen_msg_id)
    ts: datetime = field(default_factory=_now_utc)

    # --- Optional fields ---
    manifesto_principle: Optional[str] = None
    validate_verdict: Optional[Literal["approve", "reject"]] = None

    def to_dict(self) -> Dict:
        """Serialize to a plain dictionary."""
        return {
            "type": self.type,
            "message_id": self.message_id,
            "thread_id": self.thread_id,
            "persona": self.persona,
            "role": self.role,
            "intent": self.intent.value,
            "references": self.references,
            "reasoning": self.reasoning,
            "confidence": self.confidence,
            "model_used": self.model_used,
            "token_cost": self.token_cost,
            "ts": self.ts.isoformat(),
            "manifesto_principle": self.manifesto_principle,
            "validate_verdict": self.validate_verdict,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "PersonaReasoningMessage":
        """Deserialize from a plain dictionary."""
        obj = cls(
            thread_id=data["thread_id"],
            persona=data["persona"],
            role=data["role"],
            intent=PersonaIntent(data["intent"]),
            references=data.get("references", []),
            reasoning=data["reasoning"],
            confidence=data["confidence"],
            model_used=data["model_used"],
            token_cost=data["token_cost"],
        )
        obj.message_id = data.get("message_id", obj.message_id)
        obj.ts = (
            datetime.fromisoformat(data["ts"])
            if isinstance(data.get("ts"), str)
            else data.get("ts", obj.ts)
        )
        obj.manifesto_principle = data.get("manifesto_principle")
        obj.validate_verdict = data.get("validate_verdict")
        return obj


# ============================================================================
# UNION TYPE
# ============================================================================

HiveMessage = Union[AgentLogMessage, PersonaReasoningMessage]


# ============================================================================
# HIVE THREAD
# ============================================================================


@dataclass
class HiveThread:
    """A conversation thread in the Hive.

    Threads track the full lifecycle of a debate or task, from trigger
    to resolution.  They accumulate messages, enforce token budgets,
    and track consensus status.

    Attributes:
        title: Human-readable title for the thread.
        trigger_event: What caused this thread to open.
        cognitive_state: System cognitive state when thread was created.
        token_budget: Maximum tokens the thread may consume.
        debate_deadline_s: Seconds until debate times out.
    """

    # --- Required constructor fields ---
    title: str
    trigger_event: str
    cognitive_state: CognitiveState
    token_budget: int
    debate_deadline_s: float

    # --- Auto fields ---
    thread_id: str = field(default_factory=_gen_thread_id)
    state: ThreadState = field(default=ThreadState.OPEN)
    messages: List[HiveMessage] = field(default_factory=list)
    manifesto_principles: List[str] = field(default_factory=list)
    tokens_consumed: int = 0
    linked_op_id: Optional[str] = None
    linked_pr_url: Optional[str] = None
    created_at: datetime = field(default_factory=_now_utc)
    resolved_at: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Message management
    # ------------------------------------------------------------------

    def add_message(self, msg: HiveMessage) -> None:
        """Append a message and update bookkeeping.

        - Tracks ``token_cost`` from :class:`PersonaReasoningMessage`.
        - Collects any ``manifesto_principle`` into the thread-level list.
        """
        self.messages.append(msg)

        if isinstance(msg, PersonaReasoningMessage):
            self.tokens_consumed += msg.token_cost
            if (
                msg.manifesto_principle
                and msg.manifesto_principle not in self.manifesto_principles
            ):
                self.manifesto_principles.append(msg.manifesto_principle)

    # ------------------------------------------------------------------
    # Consensus helpers
    # ------------------------------------------------------------------

    def has_observe(self) -> bool:
        """True if JARVIS has posted an OBSERVE message."""
        return any(
            isinstance(m, PersonaReasoningMessage)
            and m.persona == "jarvis"
            and m.intent == PersonaIntent.OBSERVE
            for m in self.messages
        )

    def has_propose(self) -> bool:
        """True if J-Prime has posted a PROPOSE message."""
        return any(
            isinstance(m, PersonaReasoningMessage)
            and m.persona == "j_prime"
            and m.intent == PersonaIntent.PROPOSE
            for m in self.messages
        )

    def is_consensus_ready(self) -> bool:
        """True when all three consensus conditions are met:

        1. JARVIS has observed.
        2. J-Prime has proposed.
        3. Reactor has validated with an *approve* verdict.
        """
        has_reactor_approve = any(
            isinstance(m, PersonaReasoningMessage)
            and m.persona == "reactor"
            and m.intent == PersonaIntent.VALIDATE
            and m.validate_verdict == "approve"
            for m in self.messages
        )
        return self.has_observe() and self.has_propose() and has_reactor_approve

    def is_budget_exhausted(self) -> bool:
        """True if consumed tokens >= budget."""
        return self.tokens_consumed >= self.token_budget

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict:
        """Serialize the entire thread to a plain dictionary."""
        return {
            "thread_id": self.thread_id,
            "title": self.title,
            "trigger_event": self.trigger_event,
            "cognitive_state": self.cognitive_state.value,
            "token_budget": self.token_budget,
            "debate_deadline_s": self.debate_deadline_s,
            "state": self.state.value,
            "messages": [m.to_dict() for m in self.messages],
            "manifesto_principles": self.manifesto_principles,
            "tokens_consumed": self.tokens_consumed,
            "linked_op_id": self.linked_op_id,
            "linked_pr_url": self.linked_pr_url,
            "created_at": self.created_at.isoformat(),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "HiveThread":
        """Deserialize a thread from a plain dictionary."""
        thread = cls(
            title=data["title"],
            trigger_event=data["trigger_event"],
            cognitive_state=CognitiveState(data["cognitive_state"]),
            token_budget=data["token_budget"],
            debate_deadline_s=data["debate_deadline_s"],
        )
        thread.thread_id = data.get("thread_id", thread.thread_id)
        thread.state = ThreadState(data.get("state", "open"))
        thread.manifesto_principles = data.get("manifesto_principles", [])
        thread.tokens_consumed = data.get("tokens_consumed", 0)
        thread.linked_op_id = data.get("linked_op_id")
        thread.linked_pr_url = data.get("linked_pr_url")
        thread.created_at = (
            datetime.fromisoformat(data["created_at"])
            if isinstance(data.get("created_at"), str)
            else data.get("created_at", thread.created_at)
        )
        thread.resolved_at = (
            datetime.fromisoformat(data["resolved_at"])
            if isinstance(data.get("resolved_at"), str)
            else data.get("resolved_at")
        )

        # Reconstruct messages from dicts
        for msg_data in data.get("messages", []):
            msg_type = msg_data.get("type")
            if msg_type == "agent_log":
                thread.messages.append(AgentLogMessage.from_dict(msg_data))
            elif msg_type == "persona_reasoning":
                thread.messages.append(PersonaReasoningMessage.from_dict(msg_data))

        return thread
