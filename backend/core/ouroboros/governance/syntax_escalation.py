"""Syntax-failure escalation engine — DW → J-Prime provider cascade.

When the Tier 0 provider (DoubleWord) produces persistent
``all_candidates_syntax_error`` failures for the same op, this module
detects the thrashing pattern and signals the ``CandidateGenerator`` to
escalate to J-Prime (Tier 2) with full failure context — the failed
candidate previews, AST rejection details, and an anti-hallucination
directive so the escalation target avoids the same structural mistakes.

Design constraints (Manifesto §5 + user mandates):
- **No hardcoded retry count**: threshold is env-driven
  (``JARVIS_SYNTAX_ESCALATION_THRESHOLD``).
- **Error-class-aware**: only persistent AST/syntax failures trigger
  escalation — timeouts, schema mismatches, and provider exhaustion do
  NOT escalate (they have their own recovery paths).
- **DRY**: reuses ``truncation_retry.is_truncation_failure`` for error
  classification.
- **Process-lifetime state**: the tracker is a plain dict (matches the
  ``_failfast_exhaust_consec`` pattern in the orchestrator). TTL-based
  cleanup prevents unbounded growth.
- **Env-gated master switch**: ``JARVIS_SYNTAX_ESCALATION_ENABLED``
  (default ``true``).
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _escalation_enabled() -> bool:
    """Master switch — default ON."""
    return os.environ.get(
        "JARVIS_SYNTAX_ESCALATION_ENABLED", "true",
    ).strip().lower() not in _FALSE


def _escalation_threshold() -> int:
    """Consecutive syntax failures before J-Prime cascade. Env-driven."""
    try:
        v = int(os.environ.get("JARVIS_SYNTAX_ESCALATION_THRESHOLD", "2"))
        return max(1, v)
    except (ValueError, TypeError):
        return 2


def _escalation_ttl_s() -> float:
    """Stale entry cleanup TTL (seconds). Entries older than this are reaped."""
    try:
        return max(60.0, float(
            os.environ.get("JARVIS_SYNTAX_ESCALATION_TTL_S", "600"),
        ))
    except (ValueError, TypeError):
        return 600.0


# ---------------------------------------------------------------------------
# Failure record
# ---------------------------------------------------------------------------

@dataclass
class SyntaxFailureRecord:
    """One recorded syntax failure for an op."""
    timestamp: float
    error_msg: str
    candidate_preview: str  # first N chars of the failed candidate
    target_file: str


@dataclass
class _OpEscalationState:
    """Per-op escalation tracking state."""
    failures: List[SyntaxFailureRecord] = field(default_factory=list)
    escalated: bool = False
    last_activity: float = field(default_factory=time.monotonic)


# ---------------------------------------------------------------------------
# Escalation context (forwarded to J-Prime)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EscalationContext:
    """Failure dossier assembled for the escalation target (J-Prime).

    Contains everything the target needs to avoid repeating the Tier 0
    provider's mistakes: the specific AST rejections, candidate previews,
    and an anti-hallucination directive.
    """
    op_id: str
    consecutive_failures: int
    target_file: str
    failure_records: Tuple[SyntaxFailureRecord, ...]
    anti_hallucination_directive: str


# ---------------------------------------------------------------------------
# SyntaxExhaustionEscalator (singleton-safe, process-lifetime)
# ---------------------------------------------------------------------------

class SyntaxExhaustionEscalator:
    """Per-op rolling tracker of consecutive syntax failures.

    Thread-safe for single-event-loop async usage (no locks needed —
    all access is from the same event loop via the CandidateGenerator).

    Usage::

        escalator = get_escalator()
        escalator.record_attempt(op_id, error_msg, preview, target_file)
        if escalator.should_escalate(op_id):
            ctx = escalator.build_escalation_context(op_id, op_context)
            # ... route to J-Prime ...
            escalator.clear(op_id)
    """

    def __init__(self) -> None:
        self._ops: Dict[str, _OpEscalationState] = {}

    # ── Recording ──

    def record_attempt(
        self,
        op_id: str,
        error_msg: str,
        candidate_preview: str = "",
        target_file: str = "",
    ) -> None:
        """Record a syntax failure for *op_id*.

        Only records if the error is classified as a syntax/truncation
        failure (reuses ``truncation_retry.is_truncation_failure``).
        Non-syntax failures are silently ignored.
        """
        if not _escalation_enabled():
            return
        if not _is_syntax_class_failure(error_msg):
            return

        self._reap_stale()

        state = self._ops.get(op_id)
        if state is None:
            state = _OpEscalationState()
            self._ops[op_id] = state

        state.failures.append(SyntaxFailureRecord(
            timestamp=time.monotonic(),
            error_msg=str(error_msg)[:500],
            candidate_preview=str(candidate_preview)[:800],
            target_file=str(target_file),
        ))
        state.last_activity = time.monotonic()

        logger.debug(
            "[SyntaxEscalator] recorded op=%s failures=%d threshold=%d "
            "target_file=%s",
            op_id[:16], len(state.failures), _escalation_threshold(),
            target_file,
        )

    # ── Decision ──

    def should_escalate(self, op_id: str) -> bool:
        """True iff *op_id* has met the escalation threshold.

        Checks:
        1. Master switch is ON
        2. Consecutive syntax failures ≥ threshold
        3. Not already escalated for this op (prevent double-escalation)
        """
        if not _escalation_enabled():
            return False

        state = self._ops.get(op_id)
        if state is None:
            return False
        if state.escalated:
            return False

        return len(state.failures) >= _escalation_threshold()

    # ── Context builder ──

    def build_escalation_context(
        self,
        op_id: str,
        op_context: Optional[Any] = None,
    ) -> EscalationContext:
        """Assemble the full failure dossier for J-Prime.

        The dossier contains:
        - All recorded failure records (AST rejections + candidate previews)
        - Anti-hallucination directive derived from the failure pattern
        - Op metadata from *op_context* (target file, description)
        """
        state = self._ops.get(op_id, _OpEscalationState())

        # Derive the target file from the most recent failure record
        target_file = ""
        if state.failures:
            target_file = state.failures[-1].target_file
        if not target_file and op_context is not None:
            # Fallback to op context
            _tf = getattr(op_context, "target_files", None) or []
            if _tf:
                target_file = str(_tf[0]) if _tf else ""

        # Build the anti-hallucination directive from failure patterns
        directive = _build_anti_hallucination_directive(
            failures=state.failures,
            target_file=target_file,
        )

        return EscalationContext(
            op_id=op_id,
            consecutive_failures=len(state.failures),
            target_file=target_file,
            failure_records=tuple(state.failures),
            anti_hallucination_directive=directive,
        )

    # ── Lifecycle ──

    def mark_escalated(self, op_id: str) -> None:
        """Mark *op_id* as escalated (prevents double-escalation)."""
        state = self._ops.get(op_id)
        if state is not None:
            state.escalated = True

    def clear(self, op_id: str) -> None:
        """Clear tracking state for *op_id* (on success or terminal)."""
        self._ops.pop(op_id, None)

    def get_failure_count(self, op_id: str) -> int:
        """Current consecutive failure count for *op_id*."""
        state = self._ops.get(op_id)
        return len(state.failures) if state else 0

    # ── Internal ──

    def _reap_stale(self) -> None:
        """Remove entries older than TTL to prevent unbounded growth."""
        ttl = _escalation_ttl_s()
        now = time.monotonic()
        stale_keys = [
            k for k, v in self._ops.items()
            if (now - v.last_activity) > ttl
        ]
        for k in stale_keys:
            self._ops.pop(k, None)
        if stale_keys:
            logger.debug(
                "[SyntaxEscalator] reaped %d stale entries", len(stale_keys),
            )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_ESCALATOR: Optional[SyntaxExhaustionEscalator] = None


def get_escalator() -> SyntaxExhaustionEscalator:
    """Return the process-lifetime singleton escalator."""
    global _ESCALATOR  # noqa: PLW0603
    if _ESCALATOR is None:
        _ESCALATOR = SyntaxExhaustionEscalator()
    return _ESCALATOR


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

def _is_syntax_class_failure(error_msg: str) -> bool:
    """True iff *error_msg* indicates a syntax/AST failure class.

    Reuses ``truncation_retry.is_truncation_failure`` as the primary
    classifier, with a direct substring check as fallback to avoid
    import failures breaking the escalator.
    """
    if not error_msg:
        return False
    try:
        from backend.core.ouroboros.governance.truncation_retry import (
            is_truncation_failure,
        )
        if is_truncation_failure(error_msg):
            return True
    except Exception:  # noqa: BLE001 — fail-soft
        pass
    # Direct fallback — the canonical marker
    low = str(error_msg).lower()
    return "all_candidates_syntax_error" in low


# ---------------------------------------------------------------------------
# Anti-hallucination directive builder
# ---------------------------------------------------------------------------

def _build_anti_hallucination_directive(
    failures: List[SyntaxFailureRecord],
    target_file: str,
) -> str:
    """Build a targeted directive that tells J-Prime what NOT to do.

    The directive is injected into the generation prompt's system
    context so J-Prime structurally avoids the DW model's failure modes.
    """
    n = len(failures)
    if n == 0:
        return ""

    # Extract unique error patterns from the failures
    error_patterns: List[str] = []
    for f in failures[-3:]:  # Last 3 failures (most recent)
        preview = f.candidate_preview.strip()
        if preview and preview not in error_patterns:
            error_patterns.append(preview[:300])

    parts = [
        f"PROVIDER ESCALATION: The previous provider attempted {n} "
        f"generation(s) for '{target_file}', ALL of which failed "
        f"AST syntax validation.",
        "",
        "CRITICAL CONSTRAINTS:",
        "1. Your output MUST be valid Python that passes ast.parse().",
        "2. Do NOT truncate or elide any part of the file with comments "
        "like '# ... rest of file ...' or '# existing code here'.",
        "3. Return the COMPLETE file content — every function, every import, "
        "every docstring.",
        "4. The file is small — return it in full without abbreviation.",
    ]

    if error_patterns:
        parts.append("")
        parts.append(
            "The previous provider's failed candidate started with "
            "(DO NOT repeat this pattern if it contains structural errors):"
        )
        for i, pat in enumerate(error_patterns, 1):
            parts.append(f"  Attempt {i}: {pat!r}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Context enrichment helper (used by CandidateGenerator)
# ---------------------------------------------------------------------------

def enrich_context_for_escalation(
    context: Any,
    escalation: EscalationContext,
) -> Any:
    """Return a copy of *context* with the escalation directive injected.

    Uses ``dataclasses.replace`` to produce a new OperationContext with
    the anti-hallucination directive prepended to the description. This
    gives J-Prime full visibility into WHY it's being called and WHAT
    to avoid. Fail-soft: returns *context* unchanged on any error.
    """
    import dataclasses as _dc
    try:
        # Prepend the directive to the existing description
        existing_desc = getattr(context, "description", "") or ""
        enriched_desc = (
            escalation.anti_hallucination_directive
            + "\n\n--- ORIGINAL TASK ---\n\n"
            + existing_desc
        )
        return _dc.replace(context, description=enriched_desc)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — fail-soft
        logger.debug(
            "[SyntaxEscalator] context enrichment failed (non-fatal); "
            "proceeding with original context",
            exc_info=True,
        )
        return context
