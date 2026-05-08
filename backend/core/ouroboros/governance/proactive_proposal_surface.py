"""§38.11-E — Proactive Proposal Surface
(PRD v2.68 to v2.69, 2026-05-08).

Merged with §39 #11 per §38.11.5a reconciliation. ONE
substrate unifies 4 proactive-proposal producers via the
canonical ``signal_source`` field:

  * ``proactive_curiosity_reader.rank_curious_clusters``
  * :class:`CapabilityGapSensor`   (intake/sensors)
  * :class:`OpportunityMinerSensor` (intake/sensors)
  * M10 ``ArchitectureProposer``   (long-horizon)

Authority asymmetry: this module is the proposal LEDGER +
RENDERER + OPERATOR-DECISION RECORDER. It NEVER:

  * decides which proposal to act on
  * mutates orchestrator/risk-tier state
  * spawns ops directly

When the operator accepts a proposal, the act of acceptance
is recorded; downstream consumers (the actual op-spawning
path) read the decision via the canonical accessors.

§33 patterns invoked:

  * §33.1 graduation contract — master default-FALSE.
  * §33.2 producer-bridge — :func:`emit_proposal` is the
    producer-side hook (lazy-importable; NEVER raises).
  * §33.3 naming-cage — ``proposals_repl.py`` (sibling
    module) auto-discovers via §32.11 Slice 4.
  * §33.4 per-cluster flock'd JSONL — optional persistence
    layer (sub-flag) at ``.jarvis/proactive_proposals.jsonl``
    so decisions survive across sessions.
  * §33.5 versioned artifact — frozen
    :class:`ProactiveProposal` (schema_version + symmetric
    to_dict / from_dict).
"""
from __future__ import annotations

import enum
import hashlib
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


PROACTIVE_PROPOSAL_SCHEMA_VERSION: str = "proactive_proposal.1"


_ENV_MASTER = "JARVIS_PROACTIVE_PROPOSAL_ENABLED"
_ENV_SUB_PANEL = "JARVIS_PROACTIVE_PROPOSAL_PANEL_ENABLED"
_ENV_SUB_PERSIST = (
    "JARVIS_PROACTIVE_PROPOSAL_PERSISTENCE_ENABLED"
)
_ENV_RING_SIZE = "JARVIS_PROACTIVE_PROPOSAL_RING_SIZE"
_ENV_EXPIRY_SECONDS = (
    "JARVIS_PROACTIVE_PROPOSAL_EXPIRY_SECONDS"
)

_DEFAULT_RING_SIZE = 64
_MIN_RING = 8
_MAX_RING = 512
_DEFAULT_EXPIRY_S = 24 * 3600  # 24 hours


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 graduation contract — master default-FALSE."""
    return _flag(_ENV_MASTER, default=False)


def panel_enabled() -> bool:
    if not master_enabled():
        return False
    return _flag(_ENV_SUB_PANEL, default=True)


def persistence_enabled() -> bool:
    if not master_enabled():
        return False
    return _flag(_ENV_SUB_PERSIST, default=False)


def _read_ring_size() -> int:
    raw = os.environ.get(_ENV_RING_SIZE, "").strip()
    if not raw:
        return _DEFAULT_RING_SIZE
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_RING_SIZE
    return max(_MIN_RING, min(_MAX_RING, n))


def _read_expiry_s() -> int:
    raw = os.environ.get(_ENV_EXPIRY_SECONDS, "").strip()
    if not raw:
        return _DEFAULT_EXPIRY_S
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_EXPIRY_S
    return max(60, n)  # never less than 1 minute


# ===========================================================================
# Closed taxonomies
# ===========================================================================


class ProposalKind(str, enum.Enum):
    """Closed 4-value vocabulary mapping to the 4 canonical
    producers per §38.11-E reconciliation. Each value is
    the ``signal_source`` field on the canonical
    IntentSignal envelope (string-interoperable via StrEnum).
    """

    CURIOSITY = "curiosity"           # proactive_curiosity_reader
    CAPABILITY_GAP = "capability_gap"  # CapabilityGapSensor
    OPPORTUNITY = "opportunity"       # OpportunityMinerSensor
    ARCHITECTURE = "architecture"     # M10 ArchitectureProposer

    @classmethod
    def coerce(cls, raw: object) -> "ProposalKind":
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, str):
            s = raw.strip().lower()
            for m in cls:
                if m.value == s:
                    return m
        return cls.OPPORTUNITY  # most generic default


class ProposalDecision(str, enum.Enum):
    """Closed 4-value lifecycle.

    PENDING is the only mutable state; ACCEPTED / REJECTED /
    EXPIRED are terminal.
    """

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXPIRED = "expired"

    @classmethod
    def coerce(cls, raw: object) -> "ProposalDecision":
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, str):
            s = raw.strip().lower()
            for m in cls:
                if m.value == s:
                    return m
        return cls.PENDING

    @property
    def is_terminal(self) -> bool:
        return self is not ProposalDecision.PENDING


# ===========================================================================
# Frozen §33.5 versioned artifact
# ===========================================================================


@dataclass(frozen=True)
class ProactiveProposal:
    """One proactive proposal awaiting operator decision.

    Frozen + hashable. Symmetric to_dict / from_dict per
    §33.5 versioned-artifact contract.

    ``proposal_id`` — deterministic sha256[:12] of
    ``kind|signal_source|summary|emitted_at_unix`` (collision
    risk negligible at session-scale; not cryptographic).
    """

    proposal_id: str
    kind: ProposalKind
    signal_source: str
    summary: str
    rationale: str = ""
    priority_hint: float = 0.5
    emitted_at_unix: float = field(default_factory=time.time)
    decision: ProposalDecision = ProposalDecision.PENDING
    decided_at_unix: Optional[float] = None
    decision_note: str = ""
    schema_version: str = PROACTIVE_PROPOSAL_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "kind": self.kind.value,
            "signal_source": self.signal_source,
            "summary": self.summary,
            "rationale": self.rationale,
            "priority_hint": self.priority_hint,
            "emitted_at_unix": self.emitted_at_unix,
            "decision": self.decision.value,
            "decided_at_unix": self.decided_at_unix,
            "decision_note": self.decision_note,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: object) -> Optional["ProactiveProposal"]:
        """Reverse of :meth:`to_dict`. Returns None on any
        defensive parse failure (NEVER raises)."""
        if not isinstance(d, dict):
            return None
        try:
            return cls(
                proposal_id=str(d.get("proposal_id", "")),
                kind=ProposalKind.coerce(d.get("kind")),
                signal_source=str(d.get("signal_source", "")),
                summary=str(d.get("summary", "")),
                rationale=str(d.get("rationale", "")),
                priority_hint=_clamp_float(
                    d.get("priority_hint", 0.5),
                ),
                emitted_at_unix=float(
                    d.get("emitted_at_unix", time.time()),
                ),
                decision=ProposalDecision.coerce(
                    d.get("decision"),
                ),
                decided_at_unix=(
                    float(d["decided_at_unix"])
                    if d.get("decided_at_unix") is not None
                    else None
                ),
                decision_note=str(d.get("decision_note", "")),
            )
        except Exception:  # noqa: BLE001
            return None


def _clamp_float(raw: object, *, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return 0.5
    if f < lo:
        return lo
    if f > hi:
        return hi
    return f


def _truncate(s: object, *, max_chars: int) -> str:
    try:
        text = str(s or "")
    except Exception:  # noqa: BLE001
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)] + "…"


def _compute_proposal_id(
    *,
    kind: ProposalKind,
    signal_source: str,
    summary: str,
    emitted_at_unix: float,
) -> str:
    seed = (
        f"{kind.value}|{signal_source}|{summary}|"
        f"{emitted_at_unix:.6f}"
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


# ===========================================================================
# ProactiveProposalLedger — singleton, thread-safe
# ===========================================================================


class ProactiveProposalLedger:
    """Bounded ring of proposals indexed by proposal_id.

    Drop-oldest eviction at capacity. Operator-decision
    state machine (PENDING → ACCEPTED/REJECTED/EXPIRED) via
    `accept_proposal` / `reject_proposal` / `expire_stale`.
    """

    def __init__(self, *, capacity: Optional[int] = None) -> None:
        cap = capacity if capacity is not None else _read_ring_size()
        self._capacity: int = max(_MIN_RING, min(_MAX_RING, cap))
        self._items: "OrderedDict[str, ProactiveProposal]" = (
            OrderedDict()
        )
        self._lock = threading.RLock()

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    # ---- write API (always best-effort; NEVER raises) ----------------

    def record(
        self, proposal: ProactiveProposal,
    ) -> Optional[ProactiveProposal]:
        """Insert proposal. Idempotent on duplicate
        proposal_id (returns existing record). Returns the
        recorded proposal or None on master/sub-flag off."""
        if not master_enabled():
            return None
        try:
            with self._lock:
                existing = self._items.get(proposal.proposal_id)
                if existing is not None:
                    return existing
                self._items[proposal.proposal_id] = proposal
                self._evict_if_needed()
        except Exception:  # noqa: BLE001
            return None
        _publish_proposal_emitted(proposal)
        if persistence_enabled():
            _persist_proposal(proposal)
        return proposal

    def accept(
        self, proposal_id: str, *, note: str = "",
    ) -> bool:
        return self._decide(
            proposal_id,
            decision=ProposalDecision.ACCEPTED,
            note=note,
        )

    def reject(
        self, proposal_id: str, *, note: str = "",
    ) -> bool:
        return self._decide(
            proposal_id,
            decision=ProposalDecision.REJECTED,
            note=note,
        )

    def _decide(
        self,
        proposal_id: str,
        *,
        decision: ProposalDecision,
        note: str,
    ) -> bool:
        if not master_enabled():
            return False
        with self._lock:
            existing = self._items.get(proposal_id)
            if existing is None:
                return False
            if existing.decision.is_terminal:
                # Already decided; idempotent no-op.
                return existing.decision is decision
            updated = replace(
                existing,
                decision=decision,
                decided_at_unix=time.time(),
                decision_note=_truncate(note, max_chars=200),
            )
            self._items[proposal_id] = updated
        if persistence_enabled():
            _persist_proposal(updated)
        return True

    def expire_stale(
        self, *, expiry_s: Optional[int] = None,
    ) -> int:
        """Sweep PENDING proposals older than ``expiry_s``
        and mark them EXPIRED. Returns the count expired."""
        if not master_enabled():
            return 0
        ttl = expiry_s if expiry_s is not None else _read_expiry_s()
        cutoff = time.time() - ttl
        n = 0
        with self._lock:
            for pid, p in list(self._items.items()):
                if (
                    p.decision is ProposalDecision.PENDING
                    and p.emitted_at_unix < cutoff
                ):
                    self._items[pid] = replace(
                        p,
                        decision=ProposalDecision.EXPIRED,
                        decided_at_unix=time.time(),
                    )
                    n += 1
        return n

    # ---- read API (pure; NEVER raises) -------------------------------

    def get(self, proposal_id: str) -> Optional[ProactiveProposal]:
        with self._lock:
            return self._items.get(proposal_id)

    def all_proposals(
        self, *, limit: int = 64,
    ) -> Tuple[ProactiveProposal, ...]:
        try:
            n = max(1, min(int(limit), _MAX_RING))
        except (TypeError, ValueError):
            n = 64
        with self._lock:
            items = list(self._items.values())
        if n >= len(items):
            return tuple(items)
        return tuple(items[-n:])

    def pending_proposals(
        self, *, limit: int = 16,
    ) -> Tuple[ProactiveProposal, ...]:
        try:
            n = max(1, min(int(limit), _MAX_RING))
        except (TypeError, ValueError):
            n = 16
        with self._lock:
            pending = [
                p for p in self._items.values()
                if p.decision is ProposalDecision.PENDING
            ]
        if n >= len(pending):
            return tuple(pending)
        return tuple(pending[-n:])

    def reset_for_tests(self) -> None:
        with self._lock:
            self._items.clear()

    # ---- internals ---------------------------------------------------

    def _evict_if_needed(self) -> None:
        while len(self._items) > self._capacity:
            self._items.popitem(last=False)


# ---- module singleton --------------------------------------------------

_default_ledger: Optional[ProactiveProposalLedger] = None
_singleton_lock = threading.Lock()


def get_default_ledger() -> ProactiveProposalLedger:
    global _default_ledger
    with _singleton_lock:
        if _default_ledger is None:
            _default_ledger = ProactiveProposalLedger()
        return _default_ledger


def reset_ledger_for_tests() -> None:
    global _default_ledger
    with _singleton_lock:
        if _default_ledger is not None:
            _default_ledger.reset_for_tests()
        _default_ledger = None


# ===========================================================================
# Producer-bridge §33.2 — emit_proposal
# ===========================================================================


def emit_proposal(
    *,
    kind: object,
    signal_source: str,
    summary: str,
    rationale: str = "",
    priority_hint: float = 0.5,
) -> Optional[str]:
    """Producer-side hook used by the 4 canonical producers.
    Returns the proposal_id on success, ``None`` on any
    failure (master/sub-flag off, etc.). NEVER raises.
    """
    try:
        kind_enum = ProposalKind.coerce(kind)
        emitted_at = time.time()
        summary_safe = _truncate(summary, max_chars=200)
        rationale_safe = _truncate(rationale, max_chars=2000)
        proposal = ProactiveProposal(
            proposal_id=_compute_proposal_id(
                kind=kind_enum,
                signal_source=str(signal_source or ""),
                summary=summary_safe,
                emitted_at_unix=emitted_at,
            ),
            kind=kind_enum,
            signal_source=str(signal_source or ""),
            summary=summary_safe,
            rationale=rationale_safe,
            priority_hint=_clamp_float(priority_hint),
            emitted_at_unix=emitted_at,
        )
        recorded = get_default_ledger().record(proposal)
        if recorded is None:
            return None
        return recorded.proposal_id
    except Exception:  # noqa: BLE001
        logger.debug(
            "proactive_proposal: emit failed", exc_info=True,
        )
        return None


# ===========================================================================
# Operator-decision API (proxies through canonical ledger)
# ===========================================================================


def accept_proposal(
    proposal_id: str, *, note: str = "",
) -> bool:
    return get_default_ledger().accept(
        proposal_id, note=note,
    )


def reject_proposal(
    proposal_id: str, *, note: str = "",
) -> bool:
    return get_default_ledger().reject(
        proposal_id, note=note,
    )


# ===========================================================================
# SSE composition — uses canonical broker ONLY
# ===========================================================================


def _publish_proposal_emitted(
    proposal: ProactiveProposal,
) -> None:
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_PROACTIVE_PROPOSAL_EMITTED,
            get_default_broker,
        )
        broker = get_default_broker()
        if broker is None:
            return
        broker.publish(
            EVENT_TYPE_PROACTIVE_PROPOSAL_EMITTED,
            proposal.proposal_id,
            proposal.to_dict(),
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "proactive_proposal: SSE publish failed",
            exc_info=True,
        )


# ===========================================================================
# §33.4 flock'd JSONL persistence layer (sub-flag opt-in)
# ===========================================================================


def _persist_proposal(proposal: ProactiveProposal) -> None:
    """Append-only journal at ``.jarvis/proactive_proposals.jsonl``.

    Each new event (record / accept / reject / expire)
    appends a line; latest line wins for a given proposal_id
    on read. Composes canonical
    ``cross_process_jsonl.flock_append_line`` per §33.4.
    Best-effort; NEVER raises.
    """
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_append_line,
        )
    except Exception:  # noqa: BLE001
        return
    try:
        import json as _json
        from pathlib import Path as _Path
        path = _Path(".jarvis") / "proactive_proposals.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = _json.dumps(
            proposal.to_dict(),
            ensure_ascii=False, sort_keys=True,
        )
        flock_append_line(path=path, line=line)
    except Exception:  # noqa: BLE001
        logger.debug(
            "proactive_proposal: persist failed",
            exc_info=True,
        )


# ===========================================================================
# Renderer — pure, NEVER raises
# ===========================================================================


_KIND_GLYPHS = {
    ProposalKind.CURIOSITY: "🔭",
    ProposalKind.CAPABILITY_GAP: "🧩",
    ProposalKind.OPPORTUNITY: "💡",
    ProposalKind.ARCHITECTURE: "🏛",
}


_DECISION_GLYPHS = {
    ProposalDecision.PENDING: "○",
    ProposalDecision.ACCEPTED: "✓",
    ProposalDecision.REJECTED: "✗",
    ProposalDecision.EXPIRED: "⌛",
}


def format_proposal_panel(
    *,
    proposals: Optional[Tuple[ProactiveProposal, ...]] = None,
    limit: int = 8,
    pending_only: bool = True,
) -> str:
    if not panel_enabled():
        return ""
    if proposals is None:
        ledger = get_default_ledger()
        proposals = (
            ledger.pending_proposals(limit=limit)
            if pending_only
            else ledger.all_proposals(limit=limit)
        )
    if not proposals:
        return ""
    parts = ["[bright_yellow]🌱 Pending proposals:[/]"]
    for p in proposals[-limit:]:
        kg = _KIND_GLYPHS.get(p.kind, "•")
        dg = _DECISION_GLYPHS.get(p.decision, "?")
        priority = (
            f" [dim]p={p.priority_hint:.2f}[/]"
        )
        src = (
            f" [dim]({p.signal_source})[/]"
            if p.signal_source else ""
        )
        decision_extra = ""
        if p.decision is not ProposalDecision.PENDING:
            decision_extra = (
                f" [dim]→ {p.decision.value}[/]"
            )
        parts.append(
            f"  {dg} {kg} [{p.proposal_id}] {p.summary}"
            f"{priority}{src}{decision_extra}"
        )
        if p.rationale:
            parts.append(
                f"      [dim italic]› {_truncate(p.rationale, max_chars=140)}[/]"
            )
    parts.append(
        "[dim]Operator: /proposals accept <id> | "
        "/proposals reject <id>[/]"
    )
    return "\n".join(parts)


# ===========================================================================
# FlagRegistry seeds
# ===========================================================================


def register_flags(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    n = 0
    specs = (
        (
            _ENV_MASTER, "bool",
            "§38.11-E proactive proposal master switch "
            "(graduation contract per §33.1; default FALSE).",
            "false",
        ),
        (
            _ENV_SUB_PANEL, "bool",
            "Enable proactive proposal panel render. "
            "Default TRUE when master on.",
            "true",
        ),
        (
            _ENV_SUB_PERSIST, "bool",
            "Persist proposal lifecycle to "
            ".jarvis/proactive_proposals.jsonl (§33.4 flock'd "
            "JSONL). Default FALSE — opt-in.",
            "false",
        ),
        (
            _ENV_RING_SIZE, "int",
            "Bounded ring size for proposal ledger "
            "(default 64; clamped 8..512).",
            "64",
        ),
        (
            _ENV_EXPIRY_SECONDS, "int",
            "Pending-proposal expiry seconds (default 24h; "
            "min 60s).",
            "86400",
        ),
    )
    for name, typ, desc, ex in specs:
        try:
            registry.register(
                name=name,
                type=typ,
                category="ux",
                description=desc,
                example=ex,
                source_file=(
                    "backend/core/ouroboros/governance/"
                    "proactive_proposal_surface.py"
                ),
            )
            n += 1
        except Exception:  # noqa: BLE001
            pass
    return n


# ===========================================================================
# AST pins
# ===========================================================================


def register_shipped_invariants() -> list:
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        ShippedCodeInvariant,
    )
    import ast

    pins = []

    # ---- Pin 1: master_default_false -------------------------------------

    def _master_default_false(tree: ast.AST, src: str):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                ok = False
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_flag"
                    ):
                        for kw in sub.keywords:
                            if (
                                kw.arg == "default"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is False
                            ):
                                ok = True
                if not ok:
                    return [
                        "master_enabled() must call _flag(...) "
                        "with default=False"
                    ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_38_11e_master_default_false"
        ),
        description=(
            "§33.1 graduation contract — master flag stays "
            "default-False until evidence ladder closes."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "proactive_proposal_surface.py"
        ),
        validate=_master_default_false,
    ))

    # ---- Pin 2: authority_asymmetry --------------------------------------

    def _authority_asymmetry(tree: ast.AST, src: str):
        bad = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.risk_tier_floor",
            "backend.core.ouroboros.governance.candidate_generator",
        )
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod.startswith(b) for b in bad):
                    violations.append(
                        f"forbidden authority import: {mod}"
                    )
        return violations

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_38_11e_authority_asymmetry"
        ),
        description=(
            "Substrate purity — module is a ledger + "
            "renderer + decision recorder; no orchestrator "
            "/risk-tier authority."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "proactive_proposal_surface.py"
        ),
        validate=_authority_asymmetry,
    ))

    # ---- Pin 3: proposal_kind_taxonomy_4_values --------------------------

    def _kind_taxonomy(tree: ast.AST, src: str):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "ProposalKind"
            ):
                names = {
                    a.targets[0].id
                    for a in node.body
                    if isinstance(a, ast.Assign)
                    and isinstance(a.targets[0], ast.Name)
                }
                expected = {
                    "CURIOSITY", "CAPABILITY_GAP",
                    "OPPORTUNITY", "ARCHITECTURE",
                }
                missing = expected - names
                if missing:
                    return [
                        f"ProposalKind missing values: "
                        f"{sorted(missing)} — these are the "
                        "canonical 4 producer slots"
                    ]
                return []
        return ["ProposalKind class not found"]

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_38_11e_proposal_kind_taxonomy_4_values"
        ),
        description=(
            "Closed 4-value ProposalKind taxonomy maps to "
            "the 4 canonical producers per §38.11-E "
            "reconciliation. Adding a kind requires a slice "
            "+ a new producer."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "proactive_proposal_surface.py"
        ),
        validate=_kind_taxonomy,
    ))

    # ---- Pin 4: proposal_decision_taxonomy_4_values ---------------------

    def _decision_taxonomy(tree: ast.AST, src: str):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "ProposalDecision"
            ):
                names = {
                    a.targets[0].id
                    for a in node.body
                    if isinstance(a, ast.Assign)
                    and isinstance(a.targets[0], ast.Name)
                }
                expected = {
                    "PENDING", "ACCEPTED",
                    "REJECTED", "EXPIRED",
                }
                missing = expected - names
                if missing:
                    return [
                        f"ProposalDecision missing values: "
                        f"{sorted(missing)}"
                    ]
                return []
        return ["ProposalDecision class not found"]

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_38_11e_proposal_decision_taxonomy_4_values"
        ),
        description=(
            "Closed 4-value ProposalDecision lifecycle. "
            "PENDING is the only mutable state; the 3 "
            "terminal values are operator/sweep-decided."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "proactive_proposal_surface.py"
        ),
        validate=_decision_taxonomy,
    ))

    # ---- Pin 5: composes_canonical_signal_source -------------------------

    def _composes_signal_source(tree: ast.AST, src: str):
        """The proposal artifact MUST carry a canonical
        ``signal_source`` field matching the IntentSignal
        envelope shape (`intent.signals.SignalSource`'s
        StrEnum value space). Bytes-pin via the dataclass
        field name."""
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "ProactiveProposal"
            ):
                fields = {
                    a.target.id
                    for a in node.body
                    if isinstance(a, ast.AnnAssign)
                    and isinstance(a.target, ast.Name)
                }
                if "signal_source" not in fields:
                    return [
                        "ProactiveProposal missing "
                        "'signal_source' field — required by "
                        "§38.11.5a row 5 reconciliation "
                        "(canonical envelope shape)"
                    ]
                return []
        return ["ProactiveProposal class not found"]

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_38_11e_composes_canonical_signal_source"
        ),
        description=(
            "ProactiveProposal carries the canonical "
            "signal_source field so 4 producers can write "
            "via one envelope shape (per §38.11.5a row 5)."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "proactive_proposal_surface.py"
        ),
        validate=_composes_signal_source,
    ))

    return pins


__all__ = [
    "PROACTIVE_PROPOSAL_SCHEMA_VERSION",
    "ProposalKind",
    "ProposalDecision",
    "ProactiveProposal",
    "ProactiveProposalLedger",
    "master_enabled",
    "panel_enabled",
    "persistence_enabled",
    "get_default_ledger",
    "reset_ledger_for_tests",
    "emit_proposal",
    "accept_proposal",
    "reject_proposal",
    "format_proposal_panel",
    "register_flags",
    "register_shipped_invariants",
]
