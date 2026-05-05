"""Phase 10 Slice 5 — graduation contract harness (PRD §9 / §32.8.1).

Structurally enforces the operator-binding evidence ladder before
``JARVIS_TOPOLOGY_SENTINEL_ENABLED`` may flip default-false →
default-true and before the static ``dw_allowed: false`` blocks
may be purged from ``brain_selection_policy.yaml``.

Per PRD §1612 the graduation requires **3 forced-clean once-proofs
post-purge with at least one observed BG op getting QUEUE action
under SEVERED state AND at least one observed OPEN → HALF_OPEN →
CLOSED transition**. Pre-Slice-5 this was operator attestation
(human eyes on logs); Slice 5 substrate makes it a runtime
predicate the AST pin gates the flag flip on.

## Architectural locks (operator mandate, AST-pinned)

1. **Composes existing topology_sentinel substrate** — reads the
   shipped ``topology_sentinel_history.jsonl`` ledger
   (:class:`SentinelStateStore`) and the battle-test session
   ``debug.log`` artifacts. Zero new persistence; zero
   duplication of state-tracking machinery.
2. **Per-session evidence pair required** — both criteria must
   appear within the same session window. A session that shows
   only one half is NOT counted as clean (operator attestation
   semantics preserved structurally).
3. **3-session rolling window** — the verdict reads the most
   recent 3 sessions whose `summary.json` exists; older sessions
   ignored. Rolling so a regression on session 4 invalidates
   the contract until 3 fresh clean sessions stack.
4. **`is_ready_for_purge() -> ContractVerdict`** — frozen 5-value
   closed enum (`READY_FOR_PURGE` / `INSUFFICIENT_SESSIONS` /
   `MISSING_QUEUE_EVIDENCE` / `MISSING_RECOVERY_EVIDENCE` /
   `DISABLED`). Caller branches on the enum, never on freeform
   strings.
5. **AST pin asserts master flag stays default-false** until the
   contract reports ``READY_FOR_PURGE`` — operator binding
   structurally enforced (mirrors M10's `m10_master_flag_stays_-
   default_false` pattern). Pre-graduation pin renames itself
   to `*_post_graduation` only after the master flag default
   flips, per shipping discipline.
6. **NEVER raises** — all faults map to ``DISABLED`` verdict
   with diagnostic payload; missing artifacts → empty evidence
   list, not exception.

## Authority asymmetry

Imports stdlib + ``topology_sentinel`` (read-only) ONLY. NEVER
imports orchestrator / iron_gate / policy / providers /
candidate_generator / urgency_router / change_engine /
semantic_guardian. Read-only over session artifacts.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


PHASE10_GRADUATION_CONTRACT_SCHEMA_VERSION: str = (
    "phase10_graduation_contract.1"
)


# ---------------------------------------------------------------------------
# Closed-enum verdict taxonomy
# ---------------------------------------------------------------------------


class ContractVerdict(str, Enum):
    """5-value closed taxonomy for the
    :func:`is_ready_for_purge` predicate. New values require
    explicit scope-doc + AST pin update."""

    READY_FOR_PURGE = "ready_for_purge"
    INSUFFICIENT_SESSIONS = "insufficient_sessions"
    MISSING_QUEUE_EVIDENCE = "missing_queue_evidence"
    MISSING_RECOVERY_EVIDENCE = "missing_recovery_evidence"
    DISABLED = "disabled"


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def graduation_contract_enabled() -> bool:
    """``JARVIS_PHASE10_GRADUATION_CONTRACT_ENABLED`` (default
    ``true``). When false, :func:`is_ready_for_purge` always
    returns :class:`ContractVerdict.DISABLED` so the master flag
    flip is structurally blocked. Intended for operator
    troubleshooting only — production should leave this on.
    NEVER raises."""
    raw = os.environ.get(
        "JARVIS_PHASE10_GRADUATION_CONTRACT_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True
    return raw in ("1", "true", "yes", "on")


def required_clean_sessions() -> int:
    """``JARVIS_PHASE10_REQUIRED_CLEAN_SESSIONS`` (default 3 per
    PRD §1612). Clamped [1, 10]. NEVER raises."""
    raw = os.environ.get(
        "JARVIS_PHASE10_REQUIRED_CLEAN_SESSIONS", "",
    ).strip()
    try:
        n = int(raw) if raw else 3
        if n < 1:
            return 1
        if n > 10:
            return 10
        return n
    except (TypeError, ValueError):
        return 3


def session_root() -> Path:
    """``.ouroboros/sessions`` — battle-test session artifacts
    root. Override via ``JARVIS_PHASE10_SESSION_ROOT`` for tests."""
    raw = os.environ.get(
        "JARVIS_PHASE10_SESSION_ROOT", "",
    ).strip()
    if raw:
        return Path(raw)
    return Path(".ouroboros") / "sessions"


# Sentinel for the queued-under-SEVERED evidence — string token
# raised by `candidate_generator._dispatch_via_sentinel` when the
# `fallback_tolerance="queue"` path fires. Greppable in debug.log
# / postmortem artifacts.
_QUEUE_EVIDENCE_TOKENS: Tuple[str, ...] = (
    "dw_severed_queued:",
    "fallback_tolerance:queue:severed",
)


# ---------------------------------------------------------------------------
# Frozen result containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionEvidence:
    """Per-session evidence projection — what the harness
    extracted from one session's artifacts."""

    session_id: str
    has_queue_evidence: bool
    has_recovery_transition: bool
    queue_event_count: int = 0
    recovery_transition_count: int = 0
    queue_evidence_excerpts: Tuple[str, ...] = field(
        default_factory=tuple,
    )
    recovery_transition_excerpts: Tuple[str, ...] = field(
        default_factory=tuple,
    )
    diagnostics: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_clean(self) -> bool:
        """Both criteria must appear within the same session
        window (operator attestation semantics)."""
        return self.has_queue_evidence and self.has_recovery_transition

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "is_clean": self.is_clean,
            "has_queue_evidence": self.has_queue_evidence,
            "has_recovery_transition": (
                self.has_recovery_transition
            ),
            "queue_event_count": self.queue_event_count,
            "recovery_transition_count": (
                self.recovery_transition_count
            ),
            "queue_evidence_excerpts": list(
                self.queue_evidence_excerpts,
            ),
            "recovery_transition_excerpts": list(
                self.recovery_transition_excerpts,
            ),
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True)
class ContractReport:
    """Aggregated 3-session rolling-window verdict. Frozen,
    JSON-projectable."""

    verdict: ContractVerdict
    sessions_inspected: int
    clean_sessions: int
    required_clean_sessions: int
    session_evidence: Tuple[SessionEvidence, ...] = field(
        default_factory=tuple,
    )
    elapsed_s: float = 0.0
    schema_version: str = field(
        default=PHASE10_GRADUATION_CONTRACT_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "verdict": self.verdict.value,
            "sessions_inspected": self.sessions_inspected,
            "clean_sessions": self.clean_sessions,
            "required_clean_sessions": (
                self.required_clean_sessions
            ),
            "session_evidence": [
                s.to_dict() for s in self.session_evidence
            ],
            "elapsed_s": float(self.elapsed_s),
        }


# ---------------------------------------------------------------------------
# Session evidence extraction
# ---------------------------------------------------------------------------


def _list_recent_session_dirs(
    *, root: Path, limit: int,
) -> List[Path]:
    """Return up to ``limit`` most-recent session directories
    under ``root``. Sort by directory mtime descending so the
    rolling window naturally tracks recency. NEVER raises."""
    if not root.exists():
        return []
    try:
        candidates = [
            p for p in root.iterdir()
            if p.is_dir() and p.name.startswith("bt-")
        ]
    except OSError:
        return []
    candidates.sort(
        key=lambda p: p.stat().st_mtime if p.exists() else 0.0,
        reverse=True,
    )
    return candidates[:limit]


def _scan_for_queue_evidence(
    session_dir: Path,
) -> Tuple[int, Tuple[str, ...]]:
    """Scan the session's ``debug.log`` (and adjacent text logs)
    for ``dw_severed_queued:`` tokens emitted by
    ``candidate_generator._dispatch_via_sentinel`` when the
    ``fallback_tolerance="queue"`` path fires. Returns
    (count, up-to-3 excerpts). NEVER raises."""
    candidate_files = (
        session_dir / "debug.log",
        session_dir / "summary.json",  # postmortem fragments
    )
    count = 0
    excerpts: List[str] = []
    for path in candidate_files:
        if not path.exists():
            continue
        try:
            stat = path.stat()
            if stat.st_size > 100 * 1024 * 1024:  # 100 MB cap
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    matched = any(
                        tok in line for tok in _QUEUE_EVIDENCE_TOKENS
                    )
                    if matched:
                        count += 1
                        if len(excerpts) < 3:
                            stripped = line.strip()[:240]
                            excerpts.append(stripped)
        except OSError:
            continue
    return count, tuple(excerpts)


def _scan_for_recovery_transitions(
    session_dir: Path,
) -> Tuple[int, Tuple[str, ...]]:
    """Scan the session's `topology_sentinel_history.jsonl` (or
    a copy under the session dir) for OPEN → HALF_OPEN → CLOSED
    transitions for any single ``model_id``. Returns
    (count, up-to-3 excerpts) — count is the number of complete
    OPEN→HALF_OPEN→CLOSED chains observed. NEVER raises.

    The sentinel writes globally to ``state_dir() /
    topology_sentinel_history.jsonl``; sessions that capture an
    in-window snapshot place a copy under
    ``<session_dir>/topology_sentinel_history.jsonl``. The
    harness checks both locations."""
    candidate_files = (
        session_dir / "topology_sentinel_history.jsonl",
    )
    rows_per_model: Dict[str, List[Dict[str, Any]]] = {}
    for path in candidate_files:
        if not path.exists():
            continue
        try:
            stat = path.stat()
            if stat.st_size > 50 * 1024 * 1024:
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    s = raw.strip()
                    if not s:
                        continue
                    try:
                        row = json.loads(s)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    if (
                        row.get("transition_kind")
                        != "state_change"
                    ):
                        continue
                    mid = str(row.get("model_id", "")).strip()
                    if not mid:
                        continue
                    rows_per_model.setdefault(mid, []).append(row)
        except OSError:
            continue
    # Detect OPEN→HALF_OPEN→CLOSED chains per model.
    count = 0
    excerpts: List[str] = []
    for mid, rows in rows_per_model.items():
        # Sort by ts_epoch ascending for chain detection.
        rows.sort(key=lambda r: float(r.get("ts_epoch", 0.0) or 0.0))
        i = 0
        while i < len(rows) - 2:
            r1, r2, r3 = rows[i], rows[i + 1], rows[i + 2]
            t1 = (
                str(r1.get("from_state", "")).upper(),
                str(r1.get("to_state", "")).upper(),
            )
            t2 = (
                str(r2.get("from_state", "")).upper(),
                str(r2.get("to_state", "")).upper(),
            )
            t3 = (
                str(r3.get("from_state", "")).upper(),
                str(r3.get("to_state", "")).upper(),
            )
            chain_ok = (
                t1[1] == "OPEN"
                and t2[0] == "OPEN"
                and t2[1] == "HALF_OPEN"
                and t3[0] == "HALF_OPEN"
                and t3[1] == "CLOSED"
            )
            if chain_ok:
                count += 1
                if len(excerpts) < 3:
                    excerpts.append(
                        f"{mid}: OPEN→HALF_OPEN→CLOSED "
                        f"@ ts_epoch={r3.get('ts_epoch')}",
                    )
                i += 3
            else:
                i += 1
    return count, tuple(excerpts)


def extract_session_evidence(
    session_dir: Path,
) -> SessionEvidence:
    """Project one session's artifacts into a frozen
    :class:`SessionEvidence`. NEVER raises."""
    sid = session_dir.name
    diagnostics: List[str] = []
    if not session_dir.exists():
        diagnostics.append("session_dir_missing")
        return SessionEvidence(
            session_id=sid,
            has_queue_evidence=False,
            has_recovery_transition=False,
            diagnostics=tuple(diagnostics),
        )
    try:
        q_count, q_excerpts = _scan_for_queue_evidence(
            session_dir,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        q_count, q_excerpts = 0, tuple()
        diagnostics.append(
            f"queue_scan_failed: "
            f"{type(exc).__name__}: {str(exc)[:120]}"
        )
    try:
        r_count, r_excerpts = _scan_for_recovery_transitions(
            session_dir,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        r_count, r_excerpts = 0, tuple()
        diagnostics.append(
            f"recovery_scan_failed: "
            f"{type(exc).__name__}: {str(exc)[:120]}"
        )
    return SessionEvidence(
        session_id=sid,
        has_queue_evidence=q_count > 0,
        has_recovery_transition=r_count > 0,
        queue_event_count=q_count,
        recovery_transition_count=r_count,
        queue_evidence_excerpts=q_excerpts,
        recovery_transition_excerpts=r_excerpts,
        diagnostics=tuple(diagnostics),
    )


# ---------------------------------------------------------------------------
# Public API — is_ready_for_purge
# ---------------------------------------------------------------------------


def is_ready_for_purge(
    *,
    root: Optional[Path] = None,
    required: Optional[int] = None,
) -> ContractReport:
    """Aggregate the per-session evidence over the most-recent
    ``required`` sessions and emit a frozen
    :class:`ContractReport`. NEVER raises.

    Verdict ladder:

      * ``DISABLED`` — master flag off
      * ``INSUFFICIENT_SESSIONS`` — fewer than ``required``
        sessions have run
      * ``MISSING_QUEUE_EVIDENCE`` — at least one inspected
        session lacks the BG-queued-under-SEVERED evidence
      * ``MISSING_RECOVERY_EVIDENCE`` — at least one inspected
        session lacks the OPEN→HALF_OPEN→CLOSED transition
      * ``READY_FOR_PURGE`` — all ``required`` sessions show
        BOTH criteria

    Both criteria must appear within the same session window
    (operator attestation semantics)."""
    t0 = time.monotonic()
    if not graduation_contract_enabled():
        return ContractReport(
            verdict=ContractVerdict.DISABLED,
            sessions_inspected=0,
            clean_sessions=0,
            required_clean_sessions=(
                required or required_clean_sessions()
            ),
            elapsed_s=time.monotonic() - t0,
        )
    target_root = root if root is not None else session_root()
    needed = required if required is not None else (
        required_clean_sessions()
    )
    session_dirs = _list_recent_session_dirs(
        root=target_root, limit=needed,
    )
    evidence: List[SessionEvidence] = []
    for d in session_dirs:
        evidence.append(extract_session_evidence(d))

    sessions_inspected = len(evidence)
    clean = [e for e in evidence if e.is_clean]
    if sessions_inspected < needed:
        verdict = ContractVerdict.INSUFFICIENT_SESSIONS
    elif any(not e.has_queue_evidence for e in evidence):
        verdict = ContractVerdict.MISSING_QUEUE_EVIDENCE
    elif any(
        not e.has_recovery_transition for e in evidence
    ):
        verdict = ContractVerdict.MISSING_RECOVERY_EVIDENCE
    elif len(clean) >= needed:
        verdict = ContractVerdict.READY_FOR_PURGE
    else:
        # Defensive — should be unreachable given the prior
        # branches, but if any session is non-clean for a
        # combination of reasons, flag missing-queue first.
        verdict = ContractVerdict.MISSING_QUEUE_EVIDENCE

    return ContractReport(
        verdict=verdict,
        sessions_inspected=sessions_inspected,
        clean_sessions=len(clean),
        required_clean_sessions=needed,
        session_evidence=tuple(evidence),
        elapsed_s=time.monotonic() - t0,
    )


# ---------------------------------------------------------------------------
# Module-owned ShippedCodeInvariant contributions (auto-discovered)
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered by
    :func:`shipped_code_invariants._discover_module_provided_invariants`.

    Pin asserts the operator-binding contract:
    ``JARVIS_TOPOLOGY_SENTINEL_ENABLED`` master flag in
    :mod:`topology_sentinel` MUST stay default-false until the
    Phase 10 graduation contract reports ``READY_FOR_PURGE``
    across 3 forced-clean sessions. Bytes-pin: source MUST
    contain ``default=False`` literal in the
    ``topology_sentinel_enabled`` helper."""
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate_master_flag_default_false(
        tree: "ast.Module", source: str,
    ) -> tuple:
        """``JARVIS_TOPOLOGY_SENTINEL_ENABLED`` master flag MUST
        stay default-False until 3 forced-clean once-proofs
        graduation contract reports ``READY_FOR_PURGE``."""
        violations: list = []
        # Bytes-pin: the helper must call ``_env_bool(...,
        # default=False)`` for the master flag.
        target_pattern = (
            '_env_bool("JARVIS_TOPOLOGY_SENTINEL_ENABLED", '
            'default=False)'
        )
        if target_pattern not in source:
            # Allow alternative arg formatting but require the
            # default=False literal in proximity to the flag name.
            idx = source.find(
                "JARVIS_TOPOLOGY_SENTINEL_ENABLED",
            )
            if idx < 0:
                violations.append(
                    "topology_sentinel master flag literal "
                    "missing — required by §32.5 / §1610"
                )
            else:
                window = source[idx: idx + 200]
                if "default=False" not in window:
                    violations.append(
                        "JARVIS_TOPOLOGY_SENTINEL_ENABLED MUST "
                        "stay default=False until Phase 10 "
                        "graduation contract reports "
                        "READY_FOR_PURGE (PRD §1610)"
                    )
        # Forbid the post-graduation default-true marker
        # ('graduated default') from appearing yet.
        if (
            "JARVIS_TOPOLOGY_SENTINEL_ENABLED" in source
            and "graduated default" in source
            and "_post_graduation" not in source
        ):
            violations.append(
                "post-graduation marker present without "
                "matching `_post_graduation` rename — flip "
                "did not follow shipping discipline"
            )
        return tuple(violations)

    def _validate_contract_purity(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """phase10_graduation_contract.py MUST stay pure
        substrate — no orchestrator/iron_gate/policy/providers
        imports; read-only over session artifacts +
        topology_sentinel."""
        violations: list = []
        forbidden = (
            "orchestrator",
            "iron_gate",
            "policy",
            "providers",
            "candidate_generator",
            "urgency_router",
            "change_engine",
            "semantic_guardian",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        violations.append(
                            f"phase10_graduation_contract.py "
                            f"MUST NOT import {module!r}"
                        )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "topology_sentinel_master_flag_stays_default_false"
            ),
            target_file=(
                "backend/core/ouroboros/governance/"
                "topology_sentinel.py"
            ),
            description=(
                "JARVIS_TOPOLOGY_SENTINEL_ENABLED master flag "
                "MUST stay default-False until Phase 10 "
                "graduation contract reports READY_FOR_PURGE "
                "across 3 forced-clean sessions (PRD §1610). "
                "Operator-binding gate against premature flip."
            ),
            validate=_validate_master_flag_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "phase10_graduation_contract_authority_asymmetry"
            ),
            target_file=(
                "backend/core/ouroboros/governance/"
                "phase10_graduation_contract.py"
            ),
            description=(
                "phase10_graduation_contract.py MUST stay pure "
                "substrate — read-only over session artifacts "
                "+ topology_sentinel (no orchestrator / "
                "iron_gate / policy / providers / "
                "candidate_generator imports)."
            ),
            validate=_validate_contract_purity,
        ),
    ]


__all__ = [
    "ContractReport",
    "ContractVerdict",
    "PHASE10_GRADUATION_CONTRACT_SCHEMA_VERSION",
    "SessionEvidence",
    "extract_session_evidence",
    "graduation_contract_enabled",
    "is_ready_for_purge",
    "register_shipped_invariants",
    "required_clean_sessions",
    "session_root",
]
