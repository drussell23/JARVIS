"""P5 Slice 2 — AdversarialReviewerService + cost budget + audit ledger.

Per OUROBOROS_VENOM_PRD.md §9 Phase 5 P5 acceptance criteria:

  > * New AdversarialReviewerService calls a Claude side-stream
  > * Findings in JSON: [{severity, category, description,
  >   mitigation_hint}]
  > * Cost-budgeted (default $0.05/op)
  > * Skipped for trivial / SAFE_AUTO ops
  > * Telemetry: ``[AdversarialReviewer] op=X raised N findings
  >   (severity high=A, med=B, low=C)``

Edge cases (PRD spec):
  > * Reviewer hallucinations — findings must reference specific
  >   files / patterns; ungrounded findings filtered
  > * Reviewer disagreement with PLAN — use as warning, not gate
  >   (PLAN still authoritative; findings inform GENERATE)
  > * Cost budget exceeded — reviewer skipped silently with INFO log

This module composes Slice 1's primitives (prompt builder, response
parser, hallucination filter) into one orchestrating service that:

  1. Decides whether to skip (master_off / safe_auto / empty_plan /
     budget_exhausted) — all skip paths return an
     :class:`AdversarialReview` with ``skip_reason`` set; no LLM
     call made.
  2. Calls a caller-injected :class:`ReviewProvider` (Claude side-
     stream); failure non-propagating (returns
     ``skip_reason="provider_error"``).
  3. Parses + filters the response.
  4. Writes one JSONL row to ``.jarvis/adversarial_review_audit.jsonl``
     (best-effort, never raises).
  5. Emits the §8 telemetry log line.

Authority invariants (PRD §12.2):
  * Allowed: ``adversarial_reviewer`` (own slice). NO imports of
    orchestrator / policy / iron_gate / risk_tier / change_engine /
    candidate_generator / gate / semantic_guardian. The risk-tier
    skip decision uses a STRING name (caller stringifies the enum).
  * Allowed I/O: the JSONL audit ledger path ONLY. No subprocess /
    env mutation / network. The Claude call is delegated to the
    injected :class:`ReviewProvider` so this module's I/O surface
    stays narrow.
  * Best-effort throughout — every failure (provider error, ledger
    write, parser exception) is swallowed; the service NEVER raises
    into the orchestrator.
  * Reviewer is **advisory only** — service produces an
    :class:`AdversarialReview`; the orchestrator decides whether to
    inject findings into GENERATE. Per PRD edge case "PLAN still
    authoritative."

Default-off behind ``JARVIS_ADVERSARIAL_REVIEWER_ENABLED`` (Slice 1).
Slice 5 graduation flips the default + wires the orchestrator hook.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, Sequence

from backend.core.ouroboros.governance.adversarial_reviewer import (
    AdversarialReview,
    build_review_prompt,
    filter_findings,
    is_enabled,
    parse_review_response,
)

logger = logging.getLogger(__name__)


# Per PRD spec — default cost ceiling per op. Env-overridable via
# ``JARVIS_ADVERSARIAL_REVIEWER_COST_BUDGET_USD``. Negative values
# clamp to 0 (which means "always skip on cost" — operator-explicit
# disable without flipping the master flag).
DEFAULT_COST_BUDGET_USD: float = 0.05

# Risk-tier names that bypass the reviewer per PRD spec ("Skipped for
# trivial / SAFE_AUTO ops"). Compared against the caller-supplied
# ``risk_tier_name`` (string, not enum — keeps authority cage intact).
SAFE_RISK_TIER_NAMES = frozenset({"SAFE_AUTO"})

# Audit ledger schema version. Bumped on any field shape change so
# Slice 4 REPL + IDE GET parsers can pin a version.
AUDIT_LEDGER_SCHEMA_VERSION: int = 1

# Per-line byte ceiling on JSONL writes. Anything fatter is dropped
# at write time (partial JSONL rows would break the reader).
MAX_LINE_BYTES: int = 32 * 1024  # 32 KiB


def audit_ledger_path() -> Path:
    """Return the JSONL audit ledger path. Env-overridable via
    ``JARVIS_ADVERSARIAL_REVIEWER_AUDIT_PATH``; defaults to
    ``.jarvis/adversarial_review_audit.jsonl``."""
    raw = os.environ.get("JARVIS_ADVERSARIAL_REVIEWER_AUDIT_PATH")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "adversarial_review_audit.jsonl"


def cost_budget_per_op_usd() -> float:
    """Per-op cost budget. Reads
    ``JARVIS_ADVERSARIAL_REVIEWER_COST_BUDGET_USD``; defaults to
    :data:`DEFAULT_COST_BUDGET_USD`. Negative / unparseable values
    fall back to the default."""
    raw = os.environ.get("JARVIS_ADVERSARIAL_REVIEWER_COST_BUDGET_USD")
    if raw is None:
        return DEFAULT_COST_BUDGET_USD
    try:
        v = float(raw)
        return max(0.0, v)
    except (TypeError, ValueError):
        return DEFAULT_COST_BUDGET_USD


# ---------------------------------------------------------------------------
# Provider Protocol
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewProviderResult:
    """One side-stream call's response. Frozen — passed to the parser
    verbatim, audited verbatim."""

    raw_response: str
    cost_usd: float
    model_used: str = ""


class ReviewProvider(Protocol):
    """Protocol for the LLM side-stream caller.

    Slice 5 graduation will wire a concrete implementation against
    the Claude provider; until then, callers (tests + Slice 3-4
    integration) inject fakes."""

    def review(self, prompt: str) -> ReviewProviderResult: ...


# ---------------------------------------------------------------------------
# Audit ledger
# ---------------------------------------------------------------------------


class _AdversarialAuditLedger:
    """Append-only JSONL writer. Best-effort: warn-once on I/O
    failure, never raises. Mirrors the P3 inline-approval audit
    ledger pattern."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or audit_ledger_path()
        self._lock = threading.Lock()
        self._io_warned = False

    @property
    def path(self) -> Path:
        return self._path

    def append(self, review: AdversarialReview) -> bool:
        """Write one review as a JSONL row. Returns True on success,
        False on serialize / size / I/O failure."""
        try:
            payload = {
                "schema_version": AUDIT_LEDGER_SCHEMA_VERSION,
                "wrote_at_unix": time.time(),
                **review.to_dict(),
            }
            line = json.dumps(payload, default=str)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AdversarialReviewerLedger] serialize failed: %s", exc,
            )
            return False
        encoded = line.encode("utf-8", errors="replace")
        if len(encoded) > MAX_LINE_BYTES:
            logger.warning(
                "[AdversarialReviewerLedger] review op=%s exceeds "
                "MAX_LINE_BYTES=%d (was %d) — dropped",
                review.op_id, MAX_LINE_BYTES, len(encoded),
            )
            return False
        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            return True
        except OSError as exc:
            if not self._io_warned:
                logger.warning(
                    "[AdversarialReviewerLedger] write failed at %s: "
                    "%s (further failures suppressed)",
                    self._path, exc,
                )
                self._io_warned = True
            return False

    def reset_warned_for_tests(self) -> None:
        self._io_warned = False


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AdversarialReviewerService:
    """Orchestrates one reviewer pass over a plan.

    Composition:
      * Slice 1 primitives for prompt + parse + filter.
      * Caller-injected :class:`ReviewProvider` for the LLM call.
      * :class:`_AdversarialAuditLedger` for the §8 audit row.
      * Telemetry log line per PRD spec.

    Skip decisions (NO LLM call made; returns immediately):
      * ``master_off``        — ``JARVIS_ADVERSARIAL_REVIEWER_ENABLED``
                                falsy.
      * ``safe_auto``         — ``risk_tier_name in SAFE_RISK_TIER_NAMES``.
      * ``empty_plan``        — plan_text is None / blank.
      * ``no_provider``       — caller did not inject a provider AND
                                the default-singleton path has no
                                graduated wiring (Slice 5 future).
      * ``budget_exhausted``  — provider reports cost >= budget. Note:
                                the budget is enforced **after** the
                                call as a post-check (per PRD spec
                                "exceeded → skipped silently with
                                INFO log") — the reviewer's findings
                                are still discarded so they don't
                                influence GENERATE.
      * ``provider_error``    — provider raised; failure non-propagating.

    Each skip path returns an :class:`AdversarialReview` with
    ``skip_reason`` set + ``findings=()``. Caller (Slice 5
    orchestrator wiring) treats ``was_skipped`` as "no Reviewer
    raised: section to inject."
    """

    def __init__(
        self,
        provider: Optional[ReviewProvider] = None,
        audit_ledger: Optional[_AdversarialAuditLedger] = None,
        cost_budget_usd: Optional[float] = None,
        clock=time.time,
    ) -> None:
        self._provider = provider
        self._audit = audit_ledger if audit_ledger is not None else _AdversarialAuditLedger()
        self._cost_budget_override = cost_budget_usd
        self._clock = clock

    # ---- public entry ----

    def review_plan(
        self,
        *,
        op_id: str,
        plan_text: str,
        target_files: Sequence[str] = (),
        risk_tier_name: Optional[str] = None,
    ) -> AdversarialReview:
        """Run the reviewer over one plan. Returns an
        :class:`AdversarialReview`; never raises.

        ``risk_tier_name`` is a string (e.g. ``"SAFE_AUTO"`` /
        ``"NOTIFY_APPLY"`` / ``"APPROVAL_REQUIRED"``) so this module
        avoids importing the risk_tier enum (authority cage).
        """
        # 1. Master-off short-circuit.
        if not is_enabled():
            return self._skip(op_id, "master_off")

        # 2. SAFE_AUTO bypass (PRD: "Skipped for trivial / SAFE_AUTO").
        if risk_tier_name and risk_tier_name.upper() in SAFE_RISK_TIER_NAMES:
            return self._skip(op_id, "safe_auto")

        # 3. Empty plan — nothing to review.
        if not plan_text or not str(plan_text).strip():
            return self._skip(op_id, "empty_plan")

        # 4. Provider check.
        provider = self._provider
        if provider is None:
            # Slice 5 graduation will wire a default Claude provider
            # via build_adversarial_reviewer_service(). Until then,
            # skip silently so the orchestrator wiring stays harmless.
            return self._skip(op_id, "no_provider")

        # 5. Build prompt + call provider.
        prompt = build_review_prompt(plan_text, target_files)
        try:
            result = provider.review(prompt)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AdversarialReviewer] op=%s provider raised: %s",
                op_id, exc,
            )
            return self._skip(op_id, "provider_error", model_used="")

        if not isinstance(result, ReviewProviderResult):
            logger.warning(
                "[AdversarialReviewer] op=%s provider returned "
                "non-ReviewProviderResult (%s) — skipping",
                op_id, type(result).__name__,
            )
            return self._skip(op_id, "provider_error")

        # 6. Cost budget post-check (PRD: "exceeded → skipped silently
        #    with INFO log"). If the provider exceeded the budget, we
        #    discard its findings so they cannot influence GENERATE.
        budget = self._budget()
        if result.cost_usd > budget:
            logger.info(
                "[AdversarialReviewer] op=%s cost_usd=%.4f > "
                "budget=%.4f — review discarded",
                op_id, result.cost_usd, budget,
            )
            return self._skip(
                op_id, "budget_exhausted",
                cost_usd=result.cost_usd, model_used=result.model_used,
            )

        # 7. Parse + filter.
        parsed, parse_notes = parse_review_response(result.raw_response)
        kept, drop_notes = filter_findings(parsed, target_files)
        notes = tuple(parse_notes) + tuple(drop_notes)

        review = AdversarialReview(
            op_id=op_id,
            findings=tuple(kept),
            raw_findings_count=len(parsed),
            filtered_findings_count=len(kept),
            cost_usd=result.cost_usd,
            model_used=result.model_used,
            skip_reason="",
            notes=notes,
        )

        # 8. Audit + telemetry. Both best-effort.
        self._audit_review(review)
        self._emit_telemetry(review)
        return review

    # ---- internals ----

    def _budget(self) -> float:
        if self._cost_budget_override is not None:
            return max(0.0, self._cost_budget_override)
        return cost_budget_per_op_usd()

    def _skip(
        self,
        op_id: str,
        reason: str,
        *,
        cost_usd: float = 0.0,
        model_used: str = "",
    ) -> AdversarialReview:
        review = AdversarialReview(
            op_id=op_id,
            findings=(),
            raw_findings_count=0,
            filtered_findings_count=0,
            cost_usd=cost_usd,
            model_used=model_used,
            skip_reason=reason,
            notes=(),
        )
        # Audit skips too (so /adversarial history shows them) but
        # don't emit the verbose telemetry line for skips.
        self._audit_review(review)
        logger.info(
            "[AdversarialReviewer] op=%s skipped reason=%s",
            op_id, reason,
        )
        return review

    def _audit_review(self, review: AdversarialReview) -> None:
        try:
            self._audit.append(review)
        except Exception as exc:  # noqa: BLE001
            # Defensive: ledger.append is already best-effort + never
            # raises, but wrap defensively in case someone injects a
            # broken ledger.
            logger.warning(
                "[AdversarialReviewer] op=%s audit append swallowed: %s",
                review.op_id, exc,
            )

    @staticmethod
    def _emit_telemetry(review: AdversarialReview) -> None:
        """§8 telemetry line per PRD spec:
        ``[AdversarialReviewer] op=X raised N findings (severity
        high=A, med=B, low=C)``"""
        hist = review.severity_histogram()
        logger.info(
            "[AdversarialReviewer] op=%s raised %d findings "
            "(severity high=%d, med=%d, low=%d) cost_usd=%.4f model=%s",
            review.op_id,
            len(review.findings),
            hist["HIGH"], hist["MEDIUM"], hist["LOW"],
            review.cost_usd, review.model_used or "?",
        )


# ---------------------------------------------------------------------------
# Default-singleton accessor
# ---------------------------------------------------------------------------


_default_service: Optional[AdversarialReviewerService] = None
_default_lock = threading.Lock()


def get_default_service() -> AdversarialReviewerService:
    """Process-wide service. Lazy-construct on first call. No master
    flag on the accessor — service is callable when reverted (its
    ``review_plan`` short-circuits with ``skip_reason="master_off"``)."""
    global _default_service
    with _default_lock:
        if _default_service is None:
            _default_service = AdversarialReviewerService()
    return _default_service


def reset_default_service() -> None:
    """Reset the singleton — for tests."""
    global _default_service
    with _default_lock:
        _default_service = None


__all__ = [
    "AUDIT_LEDGER_SCHEMA_VERSION",
    "AdversarialReviewerService",
    "DEFAULT_COST_BUDGET_USD",
    "MAX_LINE_BYTES",
    "ReviewProvider",
    "ReviewProviderResult",
    "SAFE_RISK_TIER_NAMES",
    "audit_ledger_path",
    "cost_budget_per_op_usd",
    "get_default_service",
    "reset_default_service",
]
