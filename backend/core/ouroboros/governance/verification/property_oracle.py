"""Phase 2 Slice 2.1 — PropertyOracle primitive.

Architectural foundation for Closed-Loop Self-Verification (PRD
§24.10 Critical Path #2). Pure-function dispatcher that answers
the question every op should be asked but isn't:

  > Did this op actually achieve what it claimed?

ROOT PROBLEM:

Phase 1 gave us deterministic replay (we can prove WHAT happened).
Phase 2 must give us closed-loop verification (we can prove a
change WORKED). Today, every APPLY phase commits a code change;
every VERIFY phase runs tests; but neither closes the loop by
asking whether the CLAIMED property holds.

Symptoms:
  * Op X claims "fixes flaky test Y" → VERIFY runs Y once and
    passes → op closes → Y still flakes 5% in the wild.
  * Op Z claims "improves test runtime by 20%" → VERIFY runs once
    and gets 18% → op closes → but the 18% was variance.
  * Refactor ops claim "no behavior change" → VERIFY runs the
    test suite → but tests don't cover the affected path.

THE ORACLE PRIMITIVE:

A ``PropertyOracle`` is a pure-function dispatcher. Given a
``Property`` (the claim) + evidence (post-APPLY observations),
it produces a ``PropertyVerdict`` — passed/failed/insufficient/
error with a confidence score and structured diagnostic.

Phase 2.1 ships only the dispatcher. Slice 2.2 adds the
``RepeatRunner`` that calls the Oracle N times to compute Bayesian
confidence. Slice 2.3 wires PLAN-time claim recording. Slice 2.4
ties verification failure to POSTMORTEM. Slice 2.5 graduates.

DESIGN PATTERNS (mirrors SemanticGuardian):

  * Module-level ``_EVALUATORS`` dict, free-form string keys
  * ``register_evaluator(...)`` for dynamic kind registration
  * Frozen ``Property`` + ``PropertyVerdict`` dataclasses
  * ``PropertyOracle`` is a thin stateless wrapper over the registry
  * Six SEED evaluators registered at module load — operators
    register more dynamically (no hardcoded enum)

OPERATOR'S DESIGN CONSTRAINTS APPLIED:

  * **Asynchronous** — evaluators are sync (cheap deterministic
    checks); Slice 2.2 will lift this to async repeat-runs. The
    Oracle itself is sync because it dispatches to evaluators.
  * **Dynamic** — property kinds registered at runtime via
    ``register_evaluator``; no enum.
  * **Adaptive** — missing evidence → INSUFFICIENT_EVIDENCE
    verdict (not raise). Schema mismatch → EVALUATOR_ERROR
    (also not raise). Operators see the diagnostic.
  * **Intelligent** — evidence canonically hashed via Antigravity's
    ``canonical_hash`` so semantically-identical evidence collapses
    cleanly across replay sessions.
  * **Robust** — every public method NEVER raises; evaluator
    exceptions become EVALUATOR_ERROR verdicts with the trace
    captured in ``reason``.
  * **No hardcoding** — every evaluator registered dynamically;
    confidence thresholds env-tunable; six seed kinds are
    examples, not canon.
  * **Leverages existing** — Antigravity's canonical_hash,
    semantic_guardian's registry pattern. ZERO duplication.

AUTHORITY INVARIANTS (pinned by tests):
  * NEVER imports orchestrator / phase_runner / candidate_generator.
  * NEVER raises out of any public method.
  * Pure stdlib + canonical_hash adapter only.
"""
from __future__ import annotations

import logging
import os
import time as _time
import traceback
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def oracle_enabled() -> bool:
    """``JARVIS_VERIFICATION_ORACLE_ENABLED`` (default ``true`` —
    graduated in Phase 2 Slice 2.5).

    Re-read at call time so monkeypatch works in tests + operators
    can flip live without re-init. Hot-revert path: ``export
    JARVIS_VERIFICATION_ORACLE_ENABLED=false`` returns the Oracle
    to advisory-only mode. The dispatcher itself always works
    regardless — the flag governs whether downstream consumers
    treat oracle output as authoritative."""
    raw = os.environ.get(
        "JARVIS_VERIFICATION_ORACLE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Verdict schema
# ---------------------------------------------------------------------------


class VerdictKind(str, Enum):
    """Four-valued verdict outcome.

    PASSED / FAILED are deterministic property results.
    INSUFFICIENT_EVIDENCE means the evaluator couldn't produce a
    determination (required evidence keys missing). EVALUATOR_ERROR
    means the evaluator raised during dispatch — operators see the
    traceback in ``reason``.

    Note: the dispatcher never returns EVALUATOR_ERROR for properties
    whose ``kind`` isn't registered — that's INSUFFICIENT_EVIDENCE
    (the evaluator is missing, not erroring). EVALUATOR_ERROR is
    strictly for runtime exceptions inside a registered evaluator."""
    PASSED = "passed"
    FAILED = "failed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    EVALUATOR_ERROR = "evaluator_error"


# Schema version for the Verdict shape — bump when fields change
PROPERTY_VERDICT_SCHEMA_VERSION = "property_verdict.1"


@dataclass(frozen=True)
class Property:
    """A claim that can be verified.

    Properties are FROZEN so they can be hashed for the decision
    runtime ledger + safely shared across threads / async tasks.

    Fields:
      * ``kind``: the registered evaluator key (free-form string).
      * ``name``: human-readable identifier for this specific
        property instance (e.g., "test_users_login_works").
      * ``evidence_required``: tuple of keys the evaluator needs in
        the evidence mapping. Missing keys → INSUFFICIENT_EVIDENCE.
      * ``metadata``: property-specific config (e.g., the threshold
        for ``numeric_below_threshold``).

    Two properties are equal iff all four fields match."""
    kind: str
    name: str
    evidence_required: Tuple[str, ...] = ()
    metadata: Tuple[Tuple[str, Any], ...] = field(default_factory=tuple)

    @classmethod
    def make(
        cls,
        *,
        kind: str,
        name: str,
        evidence_required: Optional[Tuple[str, ...]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> "Property":
        """Convenience factory — coerces metadata dict → frozen tuple
        of pairs so the dataclass stays hashable. NEVER raises on
        bad input — coerces what it can."""
        try:
            md_pairs = tuple(
                (str(k), v) for k, v in
                (sorted(metadata.items()) if metadata else ())
            )
        except (AttributeError, TypeError):
            md_pairs = ()
        try:
            ev_req = tuple(
                str(e) for e in (evidence_required or ())
            )
        except TypeError:
            ev_req = ()
        return cls(
            kind=str(kind or "").strip() or "unknown",
            name=str(name or "").strip() or "unnamed",
            evidence_required=ev_req,
            metadata=md_pairs,
        )

    def metadata_dict(self) -> Dict[str, Any]:
        """Read metadata as a dict. NEVER raises."""
        try:
            return dict(self.metadata)
        except (TypeError, ValueError):
            return {}


@dataclass(frozen=True)
class PropertyVerdict:
    """The Oracle's judgment on a single Property.

    Frozen + hashable — safe for cross-thread sharing + serialization
    into the decision-runtime ledger.

    ``confidence`` is on [0.0, 1.0]. Slice 2.1 evaluators return
    1.0 for deterministic decisions and 0.0 for missing-evidence /
    error verdicts. Slice 2.2's RepeatRunner will compute true
    Bayesian confidence after N runs."""
    property_name: str
    kind: str
    verdict: VerdictKind
    confidence: float = 0.0
    reason: str = ""
    evidence_hash: str = ""
    evaluation_ts_unix: float = 0.0
    schema_version: str = PROPERTY_VERDICT_SCHEMA_VERSION

    @property
    def passed(self) -> bool:
        """Convenience: True iff verdict is PASSED."""
        return self.verdict is VerdictKind.PASSED

    @property
    def is_terminal(self) -> bool:
        """True iff the verdict is a definitive answer (PASSED or
        FAILED). INSUFFICIENT_EVIDENCE / EVALUATOR_ERROR are
        non-terminal — operators may want to retry or escalate."""
        return self.verdict in (VerdictKind.PASSED, VerdictKind.FAILED)


# ---------------------------------------------------------------------------
# Evaluator schema
# ---------------------------------------------------------------------------


# An evaluator is a callable: (Property, evidence_dict) → PropertyVerdict.
# It is responsible for:
#   1. Checking evidence_required keys are present (or returning
#      INSUFFICIENT_EVIDENCE).
#   2. Computing the verdict from the evidence.
#   3. NEVER raising — failures become EVALUATOR_ERROR verdicts via
#      the dispatcher's defensive try/except wrapper.
#
# Evaluators ARE allowed to be expensive — Slice 2.1 runs them
# synchronously. Slice 2.2 will lift to async + parallel execution.
EvaluatorFn = Callable[
    ["Property", Mapping[str, Any]], PropertyVerdict,
]


@dataclass(frozen=True)
class PropertyEvaluator:
    """Bidirectional adapter — evaluator function + diagnostic name."""
    kind: str
    evaluate: EvaluatorFn
    description: str = ""


# ---------------------------------------------------------------------------
# Module-level evaluator registry (mirrors semantic_guardian._PATTERNS)
# ---------------------------------------------------------------------------


_EVALUATORS: Dict[str, PropertyEvaluator] = {}


def register_evaluator(
    *,
    kind: str,
    evaluate: EvaluatorFn,
    description: str = "",
    overwrite: bool = False,
) -> None:
    """Register a property evaluator. NEVER raises.

    Idempotent: re-registering the same (kind, callable) tuple is a
    silent no-op. Re-registering a kind with a DIFFERENT callable
    requires ``overwrite=True`` (defensive — prevents accidental
    silent override during module reloads in test suites)."""
    safe_kind = (str(kind).strip() if kind else "")
    if not safe_kind or evaluate is None:
        return
    existing = _EVALUATORS.get(safe_kind)
    if existing is not None:
        if existing.evaluate is evaluate:
            return  # silent no-op on identical re-register
        if not overwrite:
            logger.info(
                "[verification] evaluator for kind=%r already "
                "registered (existing=%s); use overwrite=True to "
                "replace", safe_kind, existing.description or "<noname>",
            )
            return
    _EVALUATORS[safe_kind] = PropertyEvaluator(
        kind=safe_kind,
        evaluate=evaluate,
        description=description,
    )


def is_kind_registered(kind: str) -> bool:
    return (str(kind).strip() if kind else "") in _EVALUATORS


def known_kinds() -> Tuple[str, ...]:
    """Snapshot of registered kinds. Sorted for deterministic
    diagnostic output."""
    return tuple(sorted(_EVALUATORS.keys()))


def reset_registry_for_tests() -> None:
    """Test hook — clears the registry entirely. Production code
    MUST NOT call this. Tests use it to isolate evaluator
    registration between test functions."""
    _EVALUATORS.clear()
    _register_seed_evaluators()


# ---------------------------------------------------------------------------
# PropertyOracle — stateless dispatcher
# ---------------------------------------------------------------------------


class PropertyOracle:
    """Stateless dispatcher over the module-level evaluator registry.

    Construction is cheap (just a thin wrapper). The Oracle is
    immutable + safe to share across threads / async tasks. All
    actual logic lives in the registered evaluators."""

    def evaluate(
        self,
        *,
        prop: Property,
        evidence: Mapping[str, Any],
    ) -> PropertyVerdict:
        """Dispatch to the registered evaluator. NEVER raises.

        Behavior:
          * Unregistered kind → INSUFFICIENT_EVIDENCE (the operator
            registered a property but no evaluator handles it).
          * Missing evidence keys → INSUFFICIENT_EVIDENCE (evaluator
            does its own check; the dispatcher's pre-check is a
            fast-fail).
          * Evaluator raises → EVALUATOR_ERROR with the traceback
            in ``reason`` (operators see what blew up).
          * Evaluator returns non-Verdict → EVALUATOR_ERROR.

        Parameters
        ----------
        prop : Property
            The claim. Must have a registered ``kind``.
        evidence : Mapping[str, Any]
            Post-APPLY observations the evaluator examines. The
            ``prop.evidence_required`` tuple declares which keys
            are mandatory.

        Returns
        -------
        PropertyVerdict (always — never raises)."""
        ts = _time.time()
        if prop is None:
            return _verdict_error(
                prop_name="<None>", kind="<None>",
                reason="property is None", ts=ts,
            )

        # Compute evidence hash up-front for the verdict (replay-safe
        # collapse of semantically-identical evidence).
        evidence_hash = _canonical_hash_safely(dict(evidence or {}))

        # Step 1: dispatch
        evaluator = _EVALUATORS.get(prop.kind)
        if evaluator is None:
            return PropertyVerdict(
                property_name=prop.name,
                kind=prop.kind,
                verdict=VerdictKind.INSUFFICIENT_EVIDENCE,
                confidence=0.0,
                reason=(
                    f"no evaluator registered for kind={prop.kind!r} "
                    f"(known kinds: {', '.join(known_kinds()) or '<none>'})"
                ),
                evidence_hash=evidence_hash,
                evaluation_ts_unix=ts,
            )

        # Step 2: pre-check evidence_required (defensive — even though
        # the evaluator should also check, the dispatcher's cheap
        # pre-check gives a more specific diagnostic).
        if prop.evidence_required:
            missing = [
                k for k in prop.evidence_required
                if k not in (evidence or {})
            ]
            if missing:
                return PropertyVerdict(
                    property_name=prop.name,
                    kind=prop.kind,
                    verdict=VerdictKind.INSUFFICIENT_EVIDENCE,
                    confidence=0.0,
                    reason=(
                        f"missing required evidence keys: "
                        f"{', '.join(missing)}"
                    ),
                    evidence_hash=evidence_hash,
                    evaluation_ts_unix=ts,
                )

        # Step 3: invoke evaluator under defensive try/except.
        try:
            verdict = evaluator.evaluate(prop, evidence or {})
        except Exception as exc:  # noqa: BLE001 — by-design defensive
            tb = traceback.format_exc(limit=4)
            return _verdict_error(
                prop_name=prop.name, kind=prop.kind,
                reason=f"evaluator raised {type(exc).__name__}: {exc}\n{tb}",
                ts=ts, evidence_hash=evidence_hash,
            )

        # Step 4: validate evaluator output shape.
        if not isinstance(verdict, PropertyVerdict):
            return _verdict_error(
                prop_name=prop.name, kind=prop.kind,
                reason=(
                    f"evaluator returned {type(verdict).__name__} "
                    f"instead of PropertyVerdict"
                ),
                ts=ts, evidence_hash=evidence_hash,
            )

        # Step 5: ensure evidence_hash + ts populated even if the
        # evaluator forgot to set them (defensive against hand-rolled
        # evaluators).
        if not verdict.evidence_hash:
            verdict = _replace(
                verdict, evidence_hash=evidence_hash,
            )
        if not verdict.evaluation_ts_unix:
            verdict = _replace(
                verdict, evaluation_ts_unix=ts,
            )

        return verdict


# ---------------------------------------------------------------------------
# Singleton accessor (mirrors get_default_ledger pattern)
# ---------------------------------------------------------------------------


_default_oracle = PropertyOracle()


def get_default_oracle() -> PropertyOracle:
    """Public accessor for the module-level Oracle. Construction is
    cheap; the singleton exists for cache locality + test mocking
    convenience."""
    return _default_oracle


# ---------------------------------------------------------------------------
# Defensive helpers
# ---------------------------------------------------------------------------


def _canonical_hash_safely(obj: Any) -> str:
    """Lazy import of Antigravity's canonical_hash — falls back to a
    repr-based pseudo-hash if the canonical module is unavailable.
    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.observability.determinism_substrate import (
            canonical_hash,
        )
        return canonical_hash(obj)
    except Exception:  # noqa: BLE001 — defensive
        try:
            return f"fallback:{hash(repr(sorted((obj or {}).items()))) & 0xFFFF_FFFF_FFFF_FFFF:016x}"
        except Exception:  # noqa: BLE001 — defensive
            return "error:unhashable"


def _verdict_error(
    *,
    prop_name: str,
    kind: str,
    reason: str,
    ts: float,
    evidence_hash: str = "",
) -> PropertyVerdict:
    """Helper: build an EVALUATOR_ERROR verdict with consistent
    shape."""
    return PropertyVerdict(
        property_name=prop_name,
        kind=kind,
        verdict=VerdictKind.EVALUATOR_ERROR,
        confidence=0.0,
        reason=reason,
        evidence_hash=evidence_hash,
        evaluation_ts_unix=ts,
    )


def _replace(verdict: PropertyVerdict, **changes: Any) -> PropertyVerdict:
    """Frozen-dataclass replacer (since dataclasses.replace adds
    a TypeError surface we want to avoid here)."""
    return PropertyVerdict(
        property_name=changes.get("property_name", verdict.property_name),
        kind=changes.get("kind", verdict.kind),
        verdict=changes.get("verdict", verdict.verdict),
        confidence=changes.get("confidence", verdict.confidence),
        reason=changes.get("reason", verdict.reason),
        evidence_hash=changes.get(
            "evidence_hash", verdict.evidence_hash,
        ),
        evaluation_ts_unix=changes.get(
            "evaluation_ts_unix", verdict.evaluation_ts_unix,
        ),
        schema_version=changes.get(
            "schema_version", verdict.schema_version,
        ),
    )


# ---------------------------------------------------------------------------
# Seed evaluators (six examples — operators register more)
# ---------------------------------------------------------------------------


def _eval_test_passes(
    prop: Property, evidence: Mapping[str, Any],
) -> PropertyVerdict:
    """Verify a test passed.

    Required evidence keys: ``exit_code``.
    Verdict: PASSED iff ``exit_code == 0``."""
    code = evidence.get("exit_code")
    try:
        passed = int(code) == 0
    except (TypeError, ValueError):
        return PropertyVerdict(
            property_name=prop.name, kind=prop.kind,
            verdict=VerdictKind.INSUFFICIENT_EVIDENCE,
            confidence=0.0,
            reason=f"exit_code is not an int: {code!r}",
        )
    return PropertyVerdict(
        property_name=prop.name, kind=prop.kind,
        verdict=VerdictKind.PASSED if passed else VerdictKind.FAILED,
        confidence=1.0,
        reason=f"exit_code={int(code)}",
    )


def _eval_key_present(
    prop: Property, evidence: Mapping[str, Any],
) -> PropertyVerdict:
    """Verify a value is present (truthy) in evidence.

    Required evidence keys: ``present``.
    Verdict: PASSED iff ``bool(evidence['present'])``."""
    present = evidence.get("present", False)
    return PropertyVerdict(
        property_name=prop.name, kind=prop.kind,
        verdict=(
            VerdictKind.PASSED if bool(present)
            else VerdictKind.FAILED
        ),
        confidence=1.0,
        reason=f"present={bool(present)}",
    )


def _eval_numeric_below_threshold(
    prop: Property, evidence: Mapping[str, Any],
) -> PropertyVerdict:
    """Verify ``observed`` < ``threshold``. Use case: latency / error
    rate / cost claims.

    Required evidence keys: ``observed``, ``threshold``."""
    try:
        observed = float(evidence["observed"])
        threshold = float(evidence["threshold"])
    except (KeyError, TypeError, ValueError) as exc:
        return PropertyVerdict(
            property_name=prop.name, kind=prop.kind,
            verdict=VerdictKind.INSUFFICIENT_EVIDENCE,
            confidence=0.0,
            reason=f"missing/non-numeric observed/threshold: {exc}",
        )
    passed = observed < threshold
    return PropertyVerdict(
        property_name=prop.name, kind=prop.kind,
        verdict=VerdictKind.PASSED if passed else VerdictKind.FAILED,
        confidence=1.0,
        reason=f"observed={observed} < threshold={threshold}: {passed}",
    )


def _eval_numeric_above_threshold(
    prop: Property, evidence: Mapping[str, Any],
) -> PropertyVerdict:
    """Symmetric: ``observed`` > ``threshold``. Use case: throughput,
    test coverage %, etc."""
    try:
        observed = float(evidence["observed"])
        threshold = float(evidence["threshold"])
    except (KeyError, TypeError, ValueError) as exc:
        return PropertyVerdict(
            property_name=prop.name, kind=prop.kind,
            verdict=VerdictKind.INSUFFICIENT_EVIDENCE,
            confidence=0.0,
            reason=f"missing/non-numeric observed/threshold: {exc}",
        )
    passed = observed > threshold
    return PropertyVerdict(
        property_name=prop.name, kind=prop.kind,
        verdict=VerdictKind.PASSED if passed else VerdictKind.FAILED,
        confidence=1.0,
        reason=f"observed={observed} > threshold={threshold}: {passed}",
    )


def _eval_string_matches(
    prop: Property, evidence: Mapping[str, Any],
) -> PropertyVerdict:
    """Verify ``actual == expected`` (exact string match).

    Required evidence keys: ``actual``, ``expected``."""
    try:
        actual = str(evidence["actual"])
        expected = str(evidence["expected"])
    except KeyError as exc:
        return PropertyVerdict(
            property_name=prop.name, kind=prop.kind,
            verdict=VerdictKind.INSUFFICIENT_EVIDENCE,
            confidence=0.0,
            reason=f"missing key: {exc}",
        )
    passed = actual == expected
    return PropertyVerdict(
        property_name=prop.name, kind=prop.kind,
        verdict=VerdictKind.PASSED if passed else VerdictKind.FAILED,
        confidence=1.0,
        reason=(
            "match"
            if passed
            else f"diff: actual={actual[:80]!r} expected={expected[:80]!r}"
        ),
    )


def _eval_set_subset(
    prop: Property, evidence: Mapping[str, Any],
) -> PropertyVerdict:
    """Verify ``actual ⊆ allowed`` (every element in actual is in
    allowed). Use case: import allowlist, function-call allowlist.

    Required evidence keys: ``actual``, ``allowed``. Both treated
    as sequences/iterables; coerced to sets."""
    try:
        actual = set(evidence["actual"])
        allowed = set(evidence["allowed"])
    except (KeyError, TypeError) as exc:
        return PropertyVerdict(
            property_name=prop.name, kind=prop.kind,
            verdict=VerdictKind.INSUFFICIENT_EVIDENCE,
            confidence=0.0,
            reason=f"missing/non-iterable actual/allowed: {exc}",
        )
    extras = actual - allowed
    passed = not extras
    return PropertyVerdict(
        property_name=prop.name, kind=prop.kind,
        verdict=VerdictKind.PASSED if passed else VerdictKind.FAILED,
        confidence=1.0,
        reason=(
            "subset"
            if passed
            else f"extras: {sorted(str(e) for e in extras)[:5]}"
        ),
    )


# ---------------------------------------------------------------------------
# Default-claim evaluators (Priority A — mandatory claim density)
# ---------------------------------------------------------------------------
#
# These three evaluators back the default claims that EVERY op captures
# at PLAN exit. They are intentionally:
#   - Pure stdlib (ast / hashlib / re) — zero LLM, zero network
#   - Deterministic — same evidence → same verdict (replay-stable)
#   - Defensive — never raise; missing evidence → INSUFFICIENT_EVIDENCE
#
# Per PRD §25.5.1 — without these, every verification_postmortem record
# has total_claims=0 and Phase 2 is theatrical.


def _eval_file_parses_after_change(
    prop: Property, evidence: Mapping[str, Any],
) -> PropertyVerdict:
    """Verify each file in ``target_files_post`` is syntactically
    valid Python after APPLY. Pure-stdlib AST parse — no execution,
    no import side-effects.

    Required evidence keys:
      * ``target_files_post`` — sequence of {path, content} mappings
        captured post-APPLY. Each entry must carry the file's actual
        on-disk content (caller reads it). Non-Python files (extension
        not ``.py``) are silently skipped (no false-fail on YAML/JSON).

    Verdict:
      * PASSED iff every Python file in ``target_files_post`` parses
        cleanly via ``ast.parse``.
      * FAILED with the first SyntaxError (line + offset) on any
        parse failure.
      * INSUFFICIENT_EVIDENCE if the key is missing or not iterable."""
    try:
        files = list(evidence["target_files_post"])
    except (KeyError, TypeError) as exc:
        return PropertyVerdict(
            property_name=prop.name, kind=prop.kind,
            verdict=VerdictKind.INSUFFICIENT_EVIDENCE,
            confidence=0.0,
            reason=f"missing/non-iterable target_files_post: {exc}",
        )
    import ast as _ast
    parsed_count = 0
    for entry in files:
        try:
            path = str(entry.get("path", "") or "")
            content = entry.get("content", "")
        except AttributeError:
            continue  # malformed entry — skip silently
        if not path or not isinstance(content, (str, bytes)):
            continue
        # Slice A1: only Python files are subject to AST parse.
        # Other text files (YAML / JSON / Markdown) bypass cleanly.
        if not path.endswith(".py"):
            continue
        text = (
            content.decode("utf-8", errors="replace")
            if isinstance(content, bytes) else content
        )
        try:
            _ast.parse(text, filename=path)
            parsed_count += 1
        except SyntaxError as exc:
            return PropertyVerdict(
                property_name=prop.name, kind=prop.kind,
                verdict=VerdictKind.FAILED,
                confidence=1.0,
                reason=(
                    f"SyntaxError in {path}:{exc.lineno or 0}:"
                    f"{exc.offset or 0}: {(exc.msg or '')[:120]}"
                ),
            )
    return PropertyVerdict(
        property_name=prop.name, kind=prop.kind,
        verdict=VerdictKind.PASSED,
        confidence=1.0,
        reason=f"parsed_ok={parsed_count}",
    )


def _eval_test_set_hash_stable(
    prop: Property, evidence: Mapping[str, Any],
) -> PropertyVerdict:
    """Verify the test-file inventory is unchanged across the op.

    The op shouldn't silently delete or rename existing test files.
    Adding new tests is fine (the post-set is a superset of the pre-
    set). Removing tests fails the claim — operators must explicitly
    re-classify the op as test-touching to do so.

    Required evidence keys:
      * ``test_files_pre`` — sequence of test file paths captured
        before APPLY (sorted, normalized).
      * ``test_files_post`` — sequence of test file paths post-APPLY.

    Verdict:
      * PASSED iff every entry in pre is present in post (additions OK,
        deletions/renames flagged).
      * FAILED with the first missing path (truncated to 5).
      * INSUFFICIENT_EVIDENCE if either key is missing."""
    try:
        pre = list(evidence["test_files_pre"])
        post = set(evidence["test_files_post"])
    except (KeyError, TypeError) as exc:
        return PropertyVerdict(
            property_name=prop.name, kind=prop.kind,
            verdict=VerdictKind.INSUFFICIENT_EVIDENCE,
            confidence=0.0,
            reason=f"missing/non-iterable test_files_pre/post: {exc}",
        )
    missing = [p for p in pre if p not in post]
    if missing:
        return PropertyVerdict(
            property_name=prop.name, kind=prop.kind,
            verdict=VerdictKind.FAILED,
            confidence=1.0,
            reason=(
                f"removed {len(missing)} test file(s); first 5: "
                f"{sorted(str(p) for p in missing)[:5]}"
            ),
        )
    # Hash both sets and report the digest pair so replay can verify
    # the same observation produced the same verdict.
    import hashlib as _hashlib
    pre_digest = _hashlib.sha256(
        ("\n".join(sorted(str(p) for p in pre))).encode("utf-8"),
    ).hexdigest()[:16]
    post_digest = _hashlib.sha256(
        ("\n".join(sorted(str(p) for p in post))).encode("utf-8"),
    ).hexdigest()[:16]
    return PropertyVerdict(
        property_name=prop.name, kind=prop.kind,
        verdict=VerdictKind.PASSED,
        confidence=1.0,
        reason=f"pre_sha={pre_digest} post_sha={post_digest} additions={max(0, len(post) - len(pre))}",
    )


def _eval_no_new_credential_shapes(
    prop: Property, evidence: Mapping[str, Any],
) -> PropertyVerdict:
    """Verify the diff text does NOT introduce any credential-shape
    secret. Reuses the canonical ``_CREDENTIAL_SHAPE_PATTERNS`` tuple
    from ``semantic_firewall.py`` — single source of truth.

    Required evidence keys:
      * ``diff_text`` — the unified diff (or full new-file content)
        produced by APPLY. Empty string is acceptable (no diff = no
        credentials added).

    Verdict:
      * PASSED iff none of the 5 credential regexes match.
      * FAILED with the first matching pattern's name (truncated
        match preview, no actual secret echoed back to logs).
      * INSUFFICIENT_EVIDENCE if the key is missing."""
    if "diff_text" not in evidence:
        return PropertyVerdict(
            property_name=prop.name, kind=prop.kind,
            verdict=VerdictKind.INSUFFICIENT_EVIDENCE,
            confidence=0.0,
            reason="missing key: diff_text",
        )
    diff_text = evidence.get("diff_text") or ""
    if not isinstance(diff_text, (str, bytes)):
        return PropertyVerdict(
            property_name=prop.name, kind=prop.kind,
            verdict=VerdictKind.INSUFFICIENT_EVIDENCE,
            confidence=0.0,
            reason=f"diff_text is not str/bytes: {type(diff_text).__name__}",
        )
    if isinstance(diff_text, bytes):
        diff_text = diff_text.decode("utf-8", errors="replace")
    # Late import — semantic_firewall pulls in heavier deps; keep
    # property_oracle lightweight at module load.
    try:
        from backend.core.ouroboros.governance.semantic_firewall import (
            _CREDENTIAL_SHAPE_PATTERNS,
        )
    except ImportError as exc:
        return PropertyVerdict(
            property_name=prop.name, kind=prop.kind,
            verdict=VerdictKind.EVALUATOR_ERROR,
            confidence=0.0,
            reason=f"semantic_firewall unavailable: {exc}",
        )
    for pattern in _CREDENTIAL_SHAPE_PATTERNS:
        match = pattern.search(diff_text)
        if match is not None:
            # Do NOT echo the matched secret back. Report the pattern
            # source (its source string) and the match offset only.
            return PropertyVerdict(
                property_name=prop.name, kind=prop.kind,
                verdict=VerdictKind.FAILED,
                confidence=1.0,
                reason=(
                    f"credential shape detected at offset "
                    f"{match.start()}; pattern={pattern.pattern[:48]!r}"
                ),
            )
    return PropertyVerdict(
        property_name=prop.name, kind=prop.kind,
        verdict=VerdictKind.PASSED,
        confidence=1.0,
        reason=f"no credential shapes in {len(diff_text)} chars",
    )


def _register_seed_evaluators() -> None:
    """Module-load: register the seed evaluators. Idempotent —
    re-registering the same callable is a silent no-op."""
    register_evaluator(
        kind="test_passes", evaluate=_eval_test_passes,
        description="Verify a test passed (exit_code == 0).",
    )
    register_evaluator(
        kind="key_present", evaluate=_eval_key_present,
        description="Verify a value/path/identifier is present.",
    )
    register_evaluator(
        kind="numeric_below_threshold",
        evaluate=_eval_numeric_below_threshold,
        description="Verify observed < threshold (latency, error rate).",
    )
    register_evaluator(
        kind="numeric_above_threshold",
        evaluate=_eval_numeric_above_threshold,
        description="Verify observed > threshold (throughput, coverage).",
    )
    register_evaluator(
        kind="string_matches", evaluate=_eval_string_matches,
        description="Verify actual == expected (exact match).",
    )
    register_evaluator(
        kind="set_subset", evaluate=_eval_set_subset,
        description="Verify actual ⊆ allowed (allowlist check).",
    )
    # Priority A — Mandatory Claim Density evaluators (Slice A1).
    register_evaluator(
        kind="file_parses_after_change",
        evaluate=_eval_file_parses_after_change,
        description=(
            "Verify every Python file in target_files_post parses "
            "cleanly via ast.parse (default must_hold claim)."
        ),
    )
    register_evaluator(
        kind="test_set_hash_stable",
        evaluate=_eval_test_set_hash_stable,
        description=(
            "Verify the existing test file inventory is preserved "
            "across the op (additions OK; deletions flagged)."
        ),
    )
    register_evaluator(
        kind="no_new_credential_shapes",
        evaluate=_eval_no_new_credential_shapes,
        description=(
            "Verify the diff_text contains no credential/secret "
            "regex shape (5 canonical patterns from semantic_firewall)."
        ),
    )


_register_seed_evaluators()


__all__ = [
    "PROPERTY_VERDICT_SCHEMA_VERSION",
    "Property",
    "PropertyEvaluator",
    "PropertyOracle",
    "PropertyVerdict",
    "VerdictKind",
    "get_default_oracle",
    "is_kind_registered",
    "known_kinds",
    "oracle_enabled",
    "register_evaluator",
    "reset_registry_for_tests",
]
