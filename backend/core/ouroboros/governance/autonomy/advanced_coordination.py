"""Advanced Autonomy Service — L4 Advanced Coordination.

Hosts cross-repo saga persistence, consensus voting, dynamic tier
recommendations, and provenance-gated strategic memory for long-horizon
intent modeling.

Single-writer invariant: this module NEVER mutates op_context, ledger,
filesystem, or trust tiers directly. Saga state and memory state are
internal to L4 and only influence L1 through advisory context or commands.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

from backend.core.ouroboros.governance.autonomy.autonomy_types import (
    CommandEnvelope,
    CommandType,
)
from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus

logger = logging.getLogger("Ouroboros.AdvancedCoordination")

_NS_PER_DAY = 24 * 60 * 60 * 1_000_000_000
_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _short_hash(payload: Dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _normalize_tokens(*parts: str) -> FrozenSet[str]:
    tokens = set()
    for part in parts:
        tokens.update(_TOKEN_RE.findall((part or "").lower()))
    return frozenset(tokens)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class AdvancedCoordinationConfig:
    """Configuration for the L4 Advanced Coordination layer."""

    state_dir: Path = field(
        default_factory=lambda: Path.home() / ".jarvis" / "ouroboros" / "saga_state"
    )
    saga_timeout_s: float = 600.0
    max_concurrent_sagas: int = 1
    consensus_timeout_per_brain_s: float = 120.0
    min_fact_confidence_for_injection: float = 0.3
    min_reactor_quality_for_write: float = 0.7
    default_intent_decay_rate_per_day: float = 0.05
    max_injected_facts: int = 4
    prompt_char_budget: int = 2000


# ---------------------------------------------------------------------------
# SagaState — value object persisted to disk
# ---------------------------------------------------------------------------


@dataclass
class SagaState:
    """Represents the durable state of a cross-repo saga."""

    saga_id: str
    repos: List[str]
    patches: Dict[str, str]
    phase: str = "CREATED"  # CREATED | IN_PROGRESS | COMPLETED | FAILED
    repos_applied: List[str] = field(default_factory=list)
    repos_failed: List[str] = field(default_factory=list)
    idempotency_key: str = ""
    checksum: str = ""

    def __post_init__(self) -> None:
        if not self.idempotency_key:
            self.idempotency_key = self.saga_id
        self._update_checksum()

    def _update_checksum(self) -> None:
        raw = {
            "saga_id": self.saga_id,
            "phase": self.phase,
            "repos_applied": sorted(self.repos_applied),
            "repos_failed": sorted(self.repos_failed),
        }
        self.checksum = _short_hash(raw)


# ---------------------------------------------------------------------------
# Strategic memory types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryFact:
    """Persisted memory fact with provenance and confidence."""

    fact_id: str
    content: str
    provenance: str
    confidence: float
    created_at_ns: int
    expires_at_ns: Optional[int]
    tags: FrozenSet[str] = field(default_factory=frozenset)
    schema_version: str = "fact.v1"
    checksum: str = ""

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("MemoryFact.content is required")
        if not self.provenance.strip():
            raise ValueError("MemoryFact.provenance is required")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("MemoryFact.confidence must be within [0.0, 1.0]")
        if not self.fact_id:
            object.__setattr__(
                self,
                "fact_id",
                hashlib.sha256(
                    f"{self.content}\n{self.provenance}".encode("utf-8")
                ).hexdigest()[:16],
            )
        if not self.checksum:
            object.__setattr__(self, "checksum", self.compute_checksum())

    def compute_checksum(self) -> str:
        return _short_hash(
            {
                "fact_id": self.fact_id,
                "content": self.content,
                "provenance": self.provenance,
                "confidence": round(self.confidence, 6),
                "created_at_ns": self.created_at_ns,
                "expires_at_ns": self.expires_at_ns,
                "tags": sorted(self.tags),
                "schema_version": self.schema_version,
            }
        )

    def is_expired(self, now_ns: Optional[int] = None) -> bool:
        if self.expires_at_ns is None:
            return False
        current_ns = now_ns if now_ns is not None else time.monotonic_ns()
        return current_ns >= self.expires_at_ns


@dataclass(frozen=True)
class IntentNode:
    """Persistent intent node derived from explicit user goals."""

    intent_id: str
    description: str
    supporting_facts: Tuple[str, ...]
    confidence: float
    parent_intent_id: Optional[str]
    created_at_ns: int
    last_confirmed_at_ns: int
    decay_rate_per_day: float
    schema_version: str = "intent.v1"
    checksum: str = ""

    def __post_init__(self) -> None:
        if not self.description.strip():
            raise ValueError("IntentNode.description is required")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("IntentNode.confidence must be within [0.0, 1.0]")
        if self.decay_rate_per_day < 0.0:
            raise ValueError("IntentNode.decay_rate_per_day must be >= 0.0")
        if not self.intent_id:
            parent = self.parent_intent_id or ""
            object.__setattr__(
                self,
                "intent_id",
                hashlib.sha256(
                    f"{self.description}\n{parent}".encode("utf-8")
                ).hexdigest()[:16],
            )
        if not self.checksum:
            object.__setattr__(self, "checksum", self.compute_checksum())

    def compute_checksum(self) -> str:
        return _short_hash(
            {
                "intent_id": self.intent_id,
                "description": self.description,
                "supporting_facts": sorted(self.supporting_facts),
                "confidence": round(self.confidence, 6),
                "parent_intent_id": self.parent_intent_id or "",
                "created_at_ns": self.created_at_ns,
                "last_confirmed_at_ns": self.last_confirmed_at_ns,
                "decay_rate_per_day": round(self.decay_rate_per_day, 6),
                "schema_version": self.schema_version,
            }
        )


@dataclass(frozen=True)
class StrategicMemoryContext:
    """Frozen strategic context injected additively into generation prompts."""

    intent_id: str = ""
    fact_ids: Tuple[str, ...] = ()
    prompt_block: str = ""
    context_digest: str = ""

    def __post_init__(self) -> None:
        if not self.context_digest:
            object.__setattr__(
                self,
                "context_digest",
                hashlib.sha256(
                    (
                        self.intent_id
                        + "\n"
                        + "\n".join(self.fact_ids)
                        + "\n"
                        + self.prompt_block
                    ).encode("utf-8")
                ).hexdigest()[:16],
            )


# ---------------------------------------------------------------------------
# Consensus result
# ---------------------------------------------------------------------------


@dataclass
class ConsensusResult:
    """Result of a multi-brain consensus vote."""

    op_id: str
    votes: Dict[str, str]
    majority: bool
    approved_count: int
    total_count: int


# ---------------------------------------------------------------------------
# AdvancedAutonomyService — L4 advisory coordinator
# ---------------------------------------------------------------------------


class AdvancedAutonomyService:
    """L4 — Advanced Coordination. Advisory only."""

    def __init__(
        self,
        command_bus: CommandBus,
        config: Optional[AdvancedCoordinationConfig] = None,
    ) -> None:
        self._bus = command_bus
        self._config = config or AdvancedCoordinationConfig()
        self._config.state_dir.mkdir(parents=True, exist_ok=True)
        self._facts_dir.mkdir(parents=True, exist_ok=True)
        self._intents_dir.mkdir(parents=True, exist_ok=True)
        self._sagas: Dict[str, SagaState] = {}
        self._facts: Dict[str, MemoryFact] = {}
        self._intents: Dict[str, IntentNode] = {}
        self._load_persisted_sagas()
        self._load_persisted_facts()
        self._load_persisted_intents()

    @property
    def _facts_dir(self) -> Path:
        return self._config.state_dir / "facts"

    @property
    def _intents_dir(self) -> Path:
        return self._config.state_dir / "intents"

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _saga_path(self, saga_id: str) -> Path:
        return self._config.state_dir / f"saga_{saga_id}.json"

    def _fact_path(self, fact_id: str) -> Path:
        return self._facts_dir / f"fact_{fact_id}.json"

    def _intent_path(self, intent_id: str) -> Path:
        return self._intents_dir / f"intent_{intent_id}.json"

    @staticmethod
    def _persist_json(path: Path, data: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.stem}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.remove(tmp_name)
                except OSError:
                    pass

    def _persist_saga(self, state: SagaState) -> None:
        state._update_checksum()
        self._persist_json(
            self._saga_path(state.saga_id),
            {
                "saga_id": state.saga_id,
                "repos": state.repos,
                "patches": state.patches,
                "phase": state.phase,
                "repos_applied": state.repos_applied,
                "repos_failed": state.repos_failed,
                "idempotency_key": state.idempotency_key,
                "checksum": state.checksum,
            },
        )

    def _persist_fact(self, fact: MemoryFact) -> None:
        self._persist_json(
            self._fact_path(fact.fact_id),
            {
                "fact_id": fact.fact_id,
                "content": fact.content,
                "provenance": fact.provenance,
                "confidence": fact.confidence,
                "created_at_ns": fact.created_at_ns,
                "expires_at_ns": fact.expires_at_ns,
                "tags": sorted(fact.tags),
                "schema_version": fact.schema_version,
                "checksum": fact.checksum,
            },
        )

    def _persist_intent(self, intent: IntentNode) -> None:
        self._persist_json(
            self._intent_path(intent.intent_id),
            {
                "intent_id": intent.intent_id,
                "description": intent.description,
                "supporting_facts": list(intent.supporting_facts),
                "confidence": intent.confidence,
                "parent_intent_id": intent.parent_intent_id,
                "created_at_ns": intent.created_at_ns,
                "last_confirmed_at_ns": intent.last_confirmed_at_ns,
                "decay_rate_per_day": intent.decay_rate_per_day,
                "schema_version": intent.schema_version,
                "checksum": intent.checksum,
            },
        )

    def _load_persisted_sagas(self) -> None:
        for path in self._config.state_dir.glob("saga_*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                state = SagaState(
                    saga_id=data["saga_id"],
                    repos=data["repos"],
                    patches=data.get("patches", {}),
                    phase=data.get("phase", "CREATED"),
                    repos_applied=data.get("repos_applied", []),
                    repos_failed=data.get("repos_failed", []),
                    idempotency_key=data.get("idempotency_key", data["saga_id"]),
                )
                expected = data.get("checksum", "")
                if expected and state.checksum != expected:
                    logger.warning(
                        "[AdvancedCoord] Saga %s checksum mismatch — state may have been tampered with",
                        state.saga_id,
                    )
                self._sagas[state.saga_id] = state
            except Exception as exc:
                logger.warning("[AdvancedCoord] Failed to load %s: %s", path.name, exc)

    def _load_persisted_facts(self) -> None:
        for path in self._facts_dir.glob("fact_*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                fact = MemoryFact(
                    fact_id=str(data.get("fact_id", "")),
                    content=str(data["content"]),
                    provenance=str(data["provenance"]),
                    confidence=float(data["confidence"]),
                    created_at_ns=int(data["created_at_ns"]),
                    expires_at_ns=(
                        None if data.get("expires_at_ns") is None else int(data["expires_at_ns"])
                    ),
                    tags=frozenset(str(tag) for tag in data.get("tags", [])),
                    schema_version=str(data.get("schema_version", "fact.v1")),
                    checksum=str(data.get("checksum", "")),
                )
                self._facts[fact.fact_id] = fact
            except Exception as exc:
                logger.warning("[AdvancedCoord] Failed to load %s: %s", path.name, exc)

    def _load_persisted_intents(self) -> None:
        for path in self._intents_dir.glob("intent_*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                intent = IntentNode(
                    intent_id=str(data.get("intent_id", "")),
                    description=str(data["description"]),
                    supporting_facts=tuple(str(fid) for fid in data.get("supporting_facts", [])),
                    confidence=float(data["confidence"]),
                    parent_intent_id=(
                        None
                        if data.get("parent_intent_id") in (None, "")
                        else str(data["parent_intent_id"])
                    ),
                    created_at_ns=int(data["created_at_ns"]),
                    last_confirmed_at_ns=int(data["last_confirmed_at_ns"]),
                    decay_rate_per_day=float(
                        data.get(
                            "decay_rate_per_day",
                            self._config.default_intent_decay_rate_per_day,
                        )
                    ),
                    schema_version=str(data.get("schema_version", "intent.v1")),
                    checksum=str(data.get("checksum", "")),
                )
                self._intents[intent.intent_id] = intent
            except Exception as exc:
                logger.warning("[AdvancedCoord] Failed to load %s: %s", path.name, exc)

    # ------------------------------------------------------------------
    # Strategic memory API
    # ------------------------------------------------------------------

    def record_memory_fact(
        self,
        *,
        content: str,
        provenance: str,
        confidence: float,
        tags: Sequence[str] = (),
        expires_at_ns: Optional[int] = None,
        user_confirmed: bool = False,
        verified_source: bool = False,
        reactor_score: Optional[float] = None,
    ) -> Optional[MemoryFact]:
        """Persist a memory fact if provenance/quality rules allow it."""
        content = (content or "").strip()
        provenance = (provenance or "").strip()
        if not content or not provenance:
            return None

        trust_granted = (
            user_confirmed
            or provenance.startswith("user:")
            or verified_source
            or ((reactor_score or 0.0) >= self._config.min_reactor_quality_for_write)
        )
        if not trust_granted:
            logger.warning(
                "[AdvancedCoord] Rejecting memory fact without trusted provenance: provenance=%s",
                provenance,
            )
            return None

        fact = MemoryFact(
            fact_id="",
            content=content,
            provenance=provenance,
            confidence=max(0.0, min(1.0, confidence)),
            created_at_ns=time.monotonic_ns(),
            expires_at_ns=expires_at_ns,
            tags=frozenset(tag for tag in tags if tag),
        )
        existing = self._facts.get(fact.fact_id)
        if existing is not None:
            return existing

        self._facts[fact.fact_id] = fact
        self._persist_fact(fact)
        return fact

    def upsert_intent(
        self,
        *,
        description: str,
        supporting_fact_ids: Sequence[str] = (),
        confidence: float = 1.0,
        parent_intent_id: Optional[str] = None,
        confirmed_at_ns: Optional[int] = None,
        decay_rate_per_day: Optional[float] = None,
    ) -> IntentNode:
        """Create or update a persistent intent node."""
        now_ns = confirmed_at_ns if confirmed_at_ns is not None else time.monotonic_ns()
        description = (description or "").strip()
        existing_id = hashlib.sha256(
            f"{description}\n{parent_intent_id or ''}".encode("utf-8")
        ).hexdigest()[:16]
        valid_fact_ids = tuple(
            sorted(fid for fid in supporting_fact_ids if fid in self._facts)
        )
        existing = self._intents.get(existing_id)
        if existing is None:
            intent = IntentNode(
                intent_id=existing_id,
                description=description,
                supporting_facts=valid_fact_ids,
                confidence=max(0.0, min(1.0, confidence)),
                parent_intent_id=parent_intent_id,
                created_at_ns=now_ns,
                last_confirmed_at_ns=now_ns,
                decay_rate_per_day=(
                    self._config.default_intent_decay_rate_per_day
                    if decay_rate_per_day is None
                    else decay_rate_per_day
                ),
            )
        else:
            merged_facts = tuple(sorted(set(existing.supporting_facts) | set(valid_fact_ids)))
            intent = IntentNode(
                intent_id=existing.intent_id,
                description=existing.description,
                supporting_facts=merged_facts,
                confidence=max(existing.confidence, max(0.0, min(1.0, confidence))),
                parent_intent_id=existing.parent_intent_id,
                created_at_ns=existing.created_at_ns,
                last_confirmed_at_ns=max(existing.last_confirmed_at_ns, now_ns),
                decay_rate_per_day=existing.decay_rate_per_day,
            )

        self._intents[intent.intent_id] = intent
        self._persist_intent(intent)
        return intent

    def remember_user_intent(
        self,
        *,
        op_id: str,
        description: str,
        target_files: Sequence[str] = (),
        repo_scope: Sequence[str] = (),
    ) -> IntentNode:
        """Persist the user's explicit goal for future operations."""
        path_tags = {
            part.lower()
            for raw_path in target_files
            for part in Path(raw_path).parts[:3]
            if part not in ("", ".", "..")
        }
        fact = self.record_memory_fact(
            content=description,
            provenance=f"user:{op_id}",
            confidence=1.0,
            tags=tuple(sorted({"goal", "user", *repo_scope, *path_tags})),
            user_confirmed=True,
        )
        supporting = (fact.fact_id,) if fact is not None else ()
        return self.upsert_intent(
            description=description,
            supporting_fact_ids=supporting,
            confidence=1.0,
            confirmed_at_ns=time.monotonic_ns(),
        )

    def build_strategic_memory_context(
        self,
        *,
        goal: str,
        target_files: Sequence[str] = (),
        max_facts: Optional[int] = None,
        include_low_confidence: bool = False,
        now_ns: Optional[int] = None,
    ) -> StrategicMemoryContext:
        """Build a bounded, additive strategic-memory prompt block."""
        current_ns = now_ns if now_ns is not None else time.monotonic_ns()
        query_tokens = _normalize_tokens(
            goal,
            " ".join(target_files),
            " ".join(part for raw_path in target_files for part in Path(raw_path).parts),
        )
        max_fact_count = max_facts or self._config.max_injected_facts

        intent_candidates: List[Tuple[float, IntentNode]] = []
        for intent in self._intents.values():
            if intent.description.strip().lower() == goal.strip().lower():
                continue
            effective_confidence = self._effective_intent_confidence(intent, current_ns)
            if effective_confidence <= 0.0:
                continue
            overlap = self._overlap_score(query_tokens, _normalize_tokens(intent.description))
            score = overlap * 0.7 + effective_confidence * 0.3
            if score > 0.0:
                intent_candidates.append((score, intent))
        intent_candidates.sort(key=lambda item: (-item[0], item[1].intent_id))

        primary_intent: Optional[IntentNode] = intent_candidates[0][1] if intent_candidates else None
        primary_intent_confidence = (
            self._effective_intent_confidence(primary_intent, current_ns)
            if primary_intent is not None
            else 0.0
        )

        fact_candidates: List[Tuple[float, MemoryFact]] = []
        supported_facts = (
            set(primary_intent.supporting_facts)
            if primary_intent is not None
            else set()
        )
        for fact in self._facts.values():
            if fact.content.strip().lower() == goal.strip().lower():
                continue
            if fact.is_expired(current_ns):
                continue
            low_confidence = fact.confidence < self._config.min_fact_confidence_for_injection
            if low_confidence and not include_low_confidence:
                continue

            fact_tokens = _normalize_tokens(fact.content, " ".join(sorted(fact.tags)))
            overlap = self._overlap_score(query_tokens, fact_tokens)
            support_boost = 0.15 if fact.fact_id in supported_facts else 0.0
            score = overlap * 0.65 + fact.confidence * 0.2 + support_boost
            if score > 0.0:
                fact_candidates.append((score, fact))
        fact_candidates.sort(key=lambda item: (-item[0], item[1].fact_id))

        selected_facts: List[MemoryFact] = []
        for _, fact in fact_candidates:
            selected_facts.append(fact)
            if len(selected_facts) >= max_fact_count:
                break

        prompt_block = self._render_memory_prompt(
            primary_intent=primary_intent,
            primary_intent_confidence=primary_intent_confidence,
            facts=selected_facts,
            include_low_confidence=include_low_confidence,
        )
        return StrategicMemoryContext(
            intent_id=primary_intent.intent_id if primary_intent is not None else "",
            fact_ids=tuple(fact.fact_id for fact in selected_facts),
            prompt_block=prompt_block,
        )

    def get_memory_fact(self, fact_id: str) -> Optional[MemoryFact]:
        return self._facts.get(fact_id)

    def get_intent(self, intent_id: str) -> Optional[IntentNode]:
        return self._intents.get(intent_id)

    def memory_stats(self) -> Dict[str, Any]:
        return {
            "fact_count": len(self._facts),
            "intent_count": len(self._intents),
            "state_dir": str(self._config.state_dir),
            "min_fact_confidence_for_injection": self._config.min_fact_confidence_for_injection,
            "max_injected_facts": self._config.max_injected_facts,
        }

    def _effective_intent_confidence(
        self,
        intent: IntentNode,
        now_ns: Optional[int] = None,
    ) -> float:
        current_ns = now_ns if now_ns is not None else time.monotonic_ns()
        elapsed_ns = max(0, current_ns - intent.last_confirmed_at_ns)
        elapsed_days = elapsed_ns / _NS_PER_DAY
        return max(0.0, intent.confidence - (intent.decay_rate_per_day * elapsed_days))

    @staticmethod
    def _overlap_score(query_tokens: FrozenSet[str], candidate_tokens: FrozenSet[str]) -> float:
        if not query_tokens or not candidate_tokens:
            return 0.0
        overlap = query_tokens & candidate_tokens
        return len(overlap) / max(len(query_tokens), 1)

    def _render_memory_prompt(
        self,
        *,
        primary_intent: Optional[IntentNode],
        primary_intent_confidence: float,
        facts: Sequence[MemoryFact],
        include_low_confidence: bool,
    ) -> str:
        if primary_intent is None and not facts:
            return ""

        lines = [
            "## Strategic Memory (advisory context only)",
            "Use this as background context only. Do not override the explicit task, source snapshot, validation output, or governance rules.",
        ]
        if primary_intent is not None:
            lines.append(
                f"Relevant intent: {primary_intent.description} "
                f"[confidence={primary_intent_confidence:.2f}]"
            )
        if facts:
            lines.append("Relevant facts:")

        budget = self._config.prompt_char_budget
        current = "\n".join(lines)
        for fact in facts:
            tags = ",".join(sorted(fact.tags)) if fact.tags else "none"
            low_conf_prefix = ""
            if include_low_confidence and fact.confidence < self._config.min_fact_confidence_for_injection:
                low_conf_prefix = "[LOW_CONFIDENCE] "
            candidate_line = (
                f"- {low_conf_prefix}[confidence={fact.confidence:.2f} "
                f"| provenance={fact.provenance} | tags={tags}] {fact.content}"
            )
            tentative = current + "\n" + candidate_line
            if len(tentative) > budget:
                break
            current = tentative
        return current

    # ------------------------------------------------------------------
    # Saga API
    # ------------------------------------------------------------------

    def create_saga(
        self,
        repos: List[str],
        patches: Dict[str, str],
    ) -> str:
        saga_id = str(uuid.uuid4())[:12]
        state = SagaState(saga_id=saga_id, repos=repos, patches=patches)
        self._sagas[saga_id] = state
        self._persist_saga(state)
        logger.info("[AdvancedCoord] Created saga %s for repos %s", saga_id, repos)
        return saga_id

    def advance_saga(self, saga_id: str, repo: str, success: bool) -> None:
        state = self._sagas.get(saga_id)
        if state is None:
            logger.warning("[AdvancedCoord] Unknown saga: %s", saga_id)
            return

        if success:
            if repo not in state.repos_applied:
                state.repos_applied.append(repo)
        else:
            if repo not in state.repos_failed:
                state.repos_failed.append(repo)

        if set(state.repos_applied) >= set(state.repos):
            state.phase = "COMPLETED"
        elif state.repos_failed:
            state.phase = "FAILED"
        else:
            state.phase = "IN_PROGRESS"

        self._persist_saga(state)
        logger.info(
            "[AdvancedCoord] Saga %s advanced: repo=%s success=%s phase=%s",
            saga_id,
            repo,
            success,
            state.phase,
        )

    def get_saga_state(self, saga_id: str) -> Optional[SagaState]:
        return self._sagas.get(saga_id)

    def request_saga_submit(self, saga_id: str) -> None:
        state = self._sagas.get(saga_id)
        if state is None:
            logger.warning("[AdvancedCoord] Cannot submit unknown saga: %s", saga_id)
            return

        cmd = CommandEnvelope(
            source_layer="L4",
            target_layer="L1",
            command_type=CommandType.REQUEST_SAGA_SUBMIT,
            payload={
                "saga_id": saga_id,
                "repo_patches": state.patches,
                "idempotency_key": state.idempotency_key,
            },
            ttl_s=300.0,
        )
        self._bus.try_put(cmd)
        logger.info("[AdvancedCoord] Submitted saga %s to command bus", saga_id)

    # ------------------------------------------------------------------
    # Consensus and trust recommendations
    # ------------------------------------------------------------------

    def record_vote(
        self,
        op_id: str,
        candidates: List[str],
        votes: Dict[str, str],
    ) -> ConsensusResult:
        approved = sum(1 for vote in votes.values() if vote == "approve")
        total = len(votes)
        majority = approved > total / 2

        result = ConsensusResult(
            op_id=op_id,
            votes=votes,
            majority=majority,
            approved_count=approved,
            total_count=total,
        )

        cmd = CommandEnvelope(
            source_layer="L4",
            target_layer="L1",
            command_type=CommandType.REPORT_CONSENSUS,
            payload={
                "op_id": op_id,
                "candidates": candidates,
                "votes": votes,
                "majority": majority,
            },
            ttl_s=300.0,
        )
        self._bus.try_put(cmd)
        logger.info(
            "[AdvancedCoord] Consensus for op %s: %d/%d approve, majority=%s",
            op_id,
            approved,
            total,
            majority,
        )
        return result

    def recommend_tier_change(
        self,
        repo: str,
        canary_slice: str,
        recommended_tier: str,
        evidence: Dict[str, Any],
    ) -> bool:
        if not evidence:
            logger.warning("[AdvancedCoord] Tier recommendation rejected: empty evidence")
            return False

        cmd = CommandEnvelope(
            source_layer="L4",
            target_layer="L1",
            command_type=CommandType.RECOMMEND_TIER_CHANGE,
            payload={
                "trigger_source": "l4_dynamic_override",
                "repo": repo,
                "canary_slice": canary_slice,
                "recommended_tier": recommended_tier,
                "evidence": evidence,
            },
            ttl_s=300.0,
        )
        return self._bus.try_put(cmd)
