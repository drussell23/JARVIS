"""Phase 9 Slice 5 / 7 / 7c — Lineage waiver predicates.

Closes the structural-mismatch problem surfaced by the 2026-05-05
green-soak proof: pre-Slice-4 cadence soaks recorded ``outcome=runner``
rows whose notes mark them as **contract downgrades**, not actual
runner-class failures (Venom / orchestrator / iron-gate / change-engine
errors). Treating those rows as blocking ``runner != 0`` forever
conflates "stored label" with "semantic runner failure" — and would
permanently lock out flags whose only "runner" history is the
already-fixed ops_count contract bug.

Operator binding 2026-05-05 (verbatim):

  > "Two rows are outcome=runner in the append-only file, but their
  > notes mark them as contract downgrade / false eligibility, not
  > Venom/orchestrator runner-class failures. Treating them forever
  > as blocking conflates 'stored label' with 'semantic runner
  > failure.' Fixing that refines eligibility, it does not weaken
  > 'no runner failures.'"

This module ships ONE named, tested predicate. It is the SOLE place
in the codebase that knows about the legacy contract-downgrade note
suffix. AST-pinned: the suffix string literal must not appear
anywhere outside this module + its tests + the contract that emits
it. Future code that wants to filter on this lineage MUST compose
:func:`is_legacy_contract_downgrade` — never re-grep the suffix.

Operator-binding tightness clauses (verbatim):

  * "Module-level constant + AST/regression tests so it cannot
    silently broaden."
  * "Notes must equal / endswith rather than loose ``in``."

Implementation honors both. Hardening:

  * Match via ``endswith`` of the canonical suffix (NOT ``in``).
    The canonical note shape emitted by the harness is
    ``complete_no_runner_failures|contract_predicate_downgraded_clean``;
    waiver fires only on rows whose notes END with the suffix.
  * Predicate accepts only ``RUNNER`` outcome — never widens to
    other outcomes. CLEAN/INFRA/MIGRATION rows pass through
    untouched even if their notes happened to share a suffix.
  * Pure function. NEVER raises. Returns False on any malformed
    input.

Forward-looking (deferred — separate slice, not in scope here):

  * The harness's evidence writer will land a structured
    ``runner_attributed_kind`` field (Venom / orchestrator /
    contract_downgrade / etc.) so future eligibility never depends
    on parsing free-text notes. When that lands, this waiver
    becomes a backward-compat shim for legacy rows; new rows go
    through the structured field.
"""
from __future__ import annotations

import os

import re


# ---------------------------------------------------------------------------
# Canonical waiver constant — module-level, AST-pinned
# ---------------------------------------------------------------------------


# The exact suffix the contract-downgrade path emits in the EvidenceRow
# notes field. Pre-Slice-4 (2026-05-05), this was the load-bearing
# marker that distinguished a runner-class FAILURE from a CLEAN session
# downgraded by the (now-fixed) ops_count predicate bug.
#
# Bytes-pinned. AST regression test asserts:
#   1. This constant exists at module level
#   2. The string value is exactly "contract_predicate_downgraded_clean"
#   3. No OTHER module in backend/core/ouroboros/governance/ references
#      the literal string outside its emit site (graduation_contract.py
#      via _maybe_apply_contract → notes append) and this waiver module
#      + its tests.
LEGACY_CONTRACT_DOWNGRADE_NOTE_SUFFIX: str = (
    "contract_predicate_downgraded_clean"
)


# Slice 7 (2026-05-07): empty-summary runner-attribution lineage.
#
# Pre-fix `live_fire_soak.classify_outcome` Step 5 default conservatively
# attributed `outcome="runner"` even when the summary had ZERO observable
# signal (session_outcome="" AND stop_reason="" AND failure_class_counts
# empty). The May 7 23:40 EXPLORATION_LEDGER_LOAD_ADAPTED_CATEGORY_WEIGHTS
# row is the canonical example: emitted with `session_id="unknown"` AND
# notes `"default_runner:outcome=|stop="` (exact bytes).
#
# Forward fix (`live_fire_soak.classify_outcome` Step 5 NEW): empty-
# summary signature routes to INFRA, not RUNNER. Future soaks emit
# `outcome="infra"` with `notes="summary_incomplete:no_observable_signal"`
# and `runner_attributed_kind=NONE` — non-blocking by construction.
#
# Backward fix (THIS lineage waiver): existing bad rows (kind=
# default_conservative + notes matching the canonical bytes) re-route
# at progress() aggregation time to `runner_incomplete_summary_waived`
# audit-visible bucket — same pattern as `runner_legacy_downgrade`
# from Slice 5.
#
# Bytes-pinned. AST regression test asserts:
#   1. Constant exists at module level.
#   2. String value is exactly "default_runner:outcome=|stop=".
#   3. Predicate uses `==` exact-equality (NEVER `in` / `endswith`
#      — operator-mandated tightness; loose match could collide
#      with legitimate runner-class rows whose notes mention the
#      prefix in passing).
INCOMPLETE_SUMMARY_RUNNER_NOTES: str = (
    "default_runner:outcome=|stop="
)


# ---------------------------------------------------------------------------
# Pure-function predicates
# ---------------------------------------------------------------------------


def is_legacy_contract_downgrade(
    *,
    outcome: str,
    notes: str,
) -> bool:
    """Return True iff ``(outcome, notes)`` matches the legacy
    contract-downgrade lineage — i.e., a row that was labeled
    ``outcome=runner`` ONLY because the pre-Slice-4 ops_count
    predicate bug downgraded a CLEAN session.

    Tightness contract (operator-binding 2026-05-05):

      * Outcome MUST be exactly the string ``"runner"`` (matches
        ``SessionOutcome.RUNNER.value``). CLEAN / INFRA / MIGRATION
        rows are NEVER waived even if their notes share a suffix.
      * Notes MUST END WITH the canonical suffix (not contain).
        Loose ``in`` matching could accidentally collide with a
        future note string that mentions the suffix in passing.

    The canonical note shape emitted by the harness's contract path
    is::

        complete_no_runner_failures|contract_predicate_downgraded_clean

    The waiver fires on this exact shape — ``endswith`` matches the
    suffix as a complete trailing token after the ``|`` separator.

    Pure function. NEVER raises. Defensive on type mismatches:
    non-string inputs return False.
    """
    if not isinstance(outcome, str):
        return False
    if not isinstance(notes, str):
        return False
    if outcome != "runner":
        return False
    return notes.endswith(
        LEGACY_CONTRACT_DOWNGRADE_NOTE_SUFFIX,
    )


_DEFAULT_RUNNER_NOTES_RE = re.compile(
    r"^default_runner:outcome=([^|]*)\|stop=(.*)$",
)
"""Slice 7c (2026-05-07) — canonical regex for parsing the
``classify_outcome`` Step 6 default conservative notes shape:
``default_runner:outcome=<X>|stop=<Y>``. Used by
:func:`is_pre_slice_7c_shutdown_misclassification` to detect
rows misclassified BEFORE the Slice 7c forward fix landed.

Capture groups:
  1. The session_outcome value (may be empty / ``incomplete_kill`` / etc.)
  2. The stop_reason value (may be empty / composite like
     ``wall_clock_cap+atexit_fallback`` / etc.)

Bytes-pinned via AST regression."""


# Sovereign Temporal Lineage Waiver (2026-06-21) — legacy infra-latency downgrade.
# A soak downgraded ONLY by the contract metrics predicate (TTFT/cognitive) while
# having NO actual runner failures was conservatively bucketed outcome=runner
# (kind=default_conservative), which permanently blocks graduation under the
# runner==0 gate. Those downgrades were caused by DW batch LATENCY — fixed by the
# Infinite-Horizon Batch Matrix. This waiver forgives them, but ONLY before the
# architectural fix landed (temporal bound), so any metric downgrade AFTER the fix
# is still a ruthless hard runner failure. Forgiven rows route to the audit-visible
# `waived_legacy_infra_latency` bucket, distinct from clean ops and hard FSM faults.
LEGACY_INFRA_LATENCY_NOTE_SUFFIX = "contract_metrics_predicate_downgraded"
LEGACY_INFRA_LATENCY_NO_RUNNER_TOKEN = "complete_no_runner_failures"
# Cutoff = the Infinite-Horizon Batch Matrix merge commit epoch (558111b,
# 2026-06-21T06:04:48Z). Env-overridable. Downgrades recorded strictly BEFORE this
# are legacy infra-latency (waivable); at/after it are hard failures.
_LATENCY_WAIVER_CUTOFF_DEFAULT = 1782021888.0


def latency_waiver_cutoff_epoch() -> float:
    """Resolve the temporal cutoff (unix epoch) before which a metrics-predicate
    downgrade is forgiven as legacy infra-latency. Env:
    ``JARVIS_GRADUATION_LATENCY_WAIVER_CUTOFF_EPOCH``. NEVER raises."""
    raw = (os.environ.get("JARVIS_GRADUATION_LATENCY_WAIVER_CUTOFF_EPOCH", "") or "").strip()
    try:
        return float(raw) if raw else _LATENCY_WAIVER_CUTOFF_DEFAULT
    except (TypeError, ValueError):
        return _LATENCY_WAIVER_CUTOFF_DEFAULT


def is_legacy_infra_latency_downgrade(
    *,
    outcome: str,
    notes: str,
    recorded_at_epoch: float,
    cutoff_epoch: float = None,  # type: ignore[assignment]
) -> bool:
    """True iff ``(outcome, notes, recorded_at_epoch)`` is a PRE-fix metrics-
    predicate downgrade with no actual runner failures — waivable as legacy
    infra-latency. Tight + temporally bounded:

      * outcome MUST be exactly ``"runner"``.
      * notes MUST contain ``complete_no_runner_failures`` (zero real faults) AND
        end with ``contract_metrics_predicate_downgraded`` (metrics-only downgrade).
      * recorded_at_epoch MUST be > 0 AND strictly BEFORE the cutoff (the
        Infinite-Horizon fix). A downgrade at/after the fix is NOT waived.

    Pure function. NEVER raises. Non-string / non-positive epoch → False."""
    if not isinstance(outcome, str) or outcome != "runner":
        return False
    if not isinstance(notes, str):
        return False
    if LEGACY_INFRA_LATENCY_NO_RUNNER_TOKEN not in notes:
        return False
    if not notes.endswith(LEGACY_INFRA_LATENCY_NOTE_SUFFIX):
        return False
    try:
        epoch = float(recorded_at_epoch)
    except (TypeError, ValueError):
        return False
    if epoch <= 0.0:
        return False
    cutoff = latency_waiver_cutoff_epoch() if cutoff_epoch is None else cutoff_epoch
    return epoch < cutoff


def is_pre_slice_7c_shutdown_misclassification(
    *,
    outcome: str,
    notes: str,
) -> bool:
    """Return True iff ``(outcome, notes)`` matches the
    pre-Slice-7c shutdown misclassification lineage —
    Slice 7c backward fix (2026-05-07).

    Real cadence soak ``bt-2026-05-08-022312`` (May 8 03:10
    UTC, first cron-fired soak in repo history that actually
    completed) hit the 40min wall-clock cap and wrote a
    composite stop_reason ``wall_clock_cap+atexit_fallback`` +
    session_outcome ``incomplete_kill``. ``classify_outcome``'s
    Step 4 used exact set membership on
    ``_SHUTDOWN_NOISE_STOP_REASONS`` (no composite-prefix
    handling) and didn't recognize ``incomplete_kill`` as an
    INFRA signal — both gaps. The row landed as
    ``outcome=runner runner_attributed_kind=default_conservative``
    when it should have been INFRA.

    Slice 7c forward-fix in :mod:`live_fire_soak` prevents
    future rows from this misclassification. THIS predicate is
    the backward-compat shim for rows already on disk.

    Tightness contract:

      * Outcome MUST be exactly the string ``"runner"``.
      * Notes MUST match :data:`_DEFAULT_RUNNER_NOTES_RE` (the
        canonical Step 6 default-runner notes shape).
      * The captured session_outcome MUST be in
        :data:`_INCOMPLETE_OUTCOME_VALUES` (e.g.,
        ``incomplete_kill``) OR the captured stop_reason's
        leading segment MUST be in
        :data:`_SHUTDOWN_NOISE_STOP_REASONS_LIVE_FIRE` (e.g.,
        ``wall_clock_cap`` from ``wall_clock_cap+atexit_fallback``).

    Pure function. NEVER raises. Defensive on type mismatches:
    non-string inputs return False.
    """
    if not isinstance(outcome, str):
        return False
    if not isinstance(notes, str):
        return False
    if outcome != "runner":
        return False
    m = _DEFAULT_RUNNER_NOTES_RE.match(notes)
    if not m:
        return False
    captured_outcome = m.group(1).strip()
    captured_stop = m.group(2).strip()
    # Compose canonical sets from live_fire_soak — single source
    # of truth. Lazy-import to avoid startup cycle. Defensive: any
    # ImportError → fall back to literal frozenset (preserves
    # backward-fix coverage even if substrate is partially loaded).
    try:
        from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
            _INCOMPLETE_OUTCOME_VALUES,
            _SHUTDOWN_NOISE_STOP_REASONS,
        )
        incomplete_values = _INCOMPLETE_OUTCOME_VALUES
        shutdown_noise = _SHUTDOWN_NOISE_STOP_REASONS
    except ImportError:
        incomplete_values = frozenset({"incomplete_kill"})
        shutdown_noise = frozenset({
            "sigterm", "sighup", "sigint",
            "wall_clock_cap", "harness_idle_timeout",
        })
    if captured_outcome in incomplete_values:
        return True
    # Composite stop-reason prefix match — same logic as
    # live_fire_soak._is_shutdown_noise_stop.
    head = captured_stop.split("+", 1)[0].strip()
    return head in shutdown_noise


def is_incomplete_summary_runner_lineage(
    *,
    outcome: str,
    notes: str,
) -> bool:
    """Return True iff ``(outcome, notes)`` matches the empty-
    summary runner-attribution lineage — Slice 7 backward fix
    (2026-05-07).

    Tightness contract:

      * Outcome MUST be exactly the string ``"runner"`` (matches
        ``SessionOutcome.RUNNER.value``). CLEAN / INFRA /
        MIGRATION rows pass through untouched.
      * Notes MUST equal :data:`INCOMPLETE_SUMMARY_RUNNER_NOTES`
        EXACTLY — operator-mandated tightness. ``endswith`` /
        ``in`` matching is forbidden because the canonical
        empty-summary bytes (``default_runner:outcome=|stop=``)
        could appear as a substring of legitimate runner rows
        whose notes carry additional diagnostic suffix.

    Pure function. NEVER raises. Defensive on type mismatches:
    non-string inputs return False.
    """
    if not isinstance(outcome, str):
        return False
    if not isinstance(notes, str):
        return False
    if outcome != "runner":
        return False
    return notes == INCOMPLETE_SUMMARY_RUNNER_NOTES


# ---------------------------------------------------------------------------
# AST pins — predicate signature + constant + sole-path enforcement
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``lineage_waiver_constant_value_pinned`` — module-level
         ``LEGACY_CONTRACT_DOWNGRADE_NOTE_SUFFIX`` exists and equals
         the exact bytes ``"contract_predicate_downgraded_clean"``.
      2. ``lineage_waiver_uses_endswith_not_in`` — predicate body
         uses ``str.endswith`` (operator-mandated tightness; ``in``
         matching is forbidden).
      3. ``lineage_waiver_outcome_check_pinned`` — predicate
         requires ``outcome == "runner"`` literal check (no widening
         to ``in (RUNNER, INFRA, ...)`` etc.).
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/graduation/"
        "lineage_waiver.py"
    )

    def _validate_constant_value(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in tree.body:
            if isinstance(node, ast.AnnAssign):
                if (
                    isinstance(node.target, ast.Name)
                    and node.target.id
                    == "LEGACY_CONTRACT_DOWNGRADE_NOTE_SUFFIX"
                ):
                    if (
                        isinstance(node.value, ast.Constant)
                        and node.value.value
                        == "contract_predicate_downgraded_clean"
                    ):
                        return ()
                    violations.append(
                        "LEGACY_CONTRACT_DOWNGRADE_NOTE_SUFFIX "
                        "must equal literal "
                        "'contract_predicate_downgraded_clean'"
                    )
                    return tuple(violations)
        violations.append(
            "LEGACY_CONTRACT_DOWNGRADE_NOTE_SUFFIX "
            "module-level constant missing"
        )
        return tuple(violations)

    def _validate_endswith_not_in(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Operator-mandated tightness: predicate MUST use
        ``str.endswith``, NEVER ``in`` operator on the note suffix.
        Catches accidental loosening regressions."""
        violations: list = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name != "is_legacy_contract_downgrade":
                    continue
                has_endswith = False
                for sub in ast.walk(node):
                    # Detect `notes.endswith(...)` call.
                    if isinstance(sub, ast.Call):
                        func = sub.func
                        if (
                            isinstance(func, ast.Attribute)
                            and func.attr == "endswith"
                        ):
                            has_endswith = True
                    # Reject `<suffix> in notes` shape (Compare
                    # node with ast.In op + Name on left matching
                    # the suffix constant).
                    if isinstance(sub, ast.Compare):
                        for op in sub.ops:
                            if isinstance(op, ast.In):
                                left = sub.left
                                if (
                                    isinstance(left, ast.Name)
                                    and left.id
                                    == "LEGACY_CONTRACT_DOWNGRADE_"
                                       "NOTE_SUFFIX"
                                ):
                                    violations.append(
                                        "predicate uses `in` "
                                        "operator on suffix — "
                                        "operator-mandated "
                                        "tightness requires "
                                        "endswith"
                                    )
                if not has_endswith:
                    violations.append(
                        "predicate must use str.endswith on "
                        "the suffix constant (tight match)"
                    )
                return tuple(violations)
        return tuple(violations)

    def _validate_outcome_check_pinned(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Predicate body MUST contain a literal equality check
        ``outcome != "runner"`` (or ``outcome == "runner"``) so
        no other outcome class is silently waived."""
        violations: list = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if node.name != "is_legacy_contract_downgrade":
                    continue
                has_runner_check = False
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Compare):
                        # Look for `outcome != "runner"` or
                        # `outcome == "runner"`.
                        if (
                            isinstance(sub.left, ast.Name)
                            and sub.left.id == "outcome"
                            and len(sub.comparators) == 1
                            and isinstance(
                                sub.comparators[0], ast.Constant,
                            )
                            and sub.comparators[0].value
                            == "runner"
                        ):
                            has_runner_check = True
                if not has_runner_check:
                    violations.append(
                        "predicate must contain literal "
                        "outcome == 'runner' (or !=) check — "
                        "waiver MUST NOT widen to other "
                        "outcome classes"
                    )
                return tuple(violations)
        return tuple(violations)

    def _validate_incomplete_summary_constant(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Slice 7 — module-level
        :data:`INCOMPLETE_SUMMARY_RUNNER_NOTES` constant exists
        and equals the canonical bytes
        ``"default_runner:outcome=|stop="`` (matches the bytes
        emitted by ``classify_outcome``'s Step 6 default
        conservative fallback when both outcome and stop_reason
        are empty)."""
        violations: list = []
        for node in tree.body:
            if isinstance(node, ast.AnnAssign):
                if (
                    isinstance(node.target, ast.Name)
                    and node.target.id
                    == "INCOMPLETE_SUMMARY_RUNNER_NOTES"
                ):
                    if (
                        isinstance(node.value, ast.Constant)
                        and node.value.value
                        == "default_runner:outcome=|stop="
                    ):
                        return ()
                    violations.append(
                        "INCOMPLETE_SUMMARY_RUNNER_NOTES must "
                        "equal literal "
                        "'default_runner:outcome=|stop='"
                    )
                    return tuple(violations)
        violations.append(
            "INCOMPLETE_SUMMARY_RUNNER_NOTES module-level "
            "constant missing"
        )
        return tuple(violations)

    def _validate_incomplete_summary_exact_match(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Slice 7 — predicate body MUST use ``==`` exact-match
        on :data:`INCOMPLETE_SUMMARY_RUNNER_NOTES`. ``endswith`` /
        ``startswith`` / ``in`` are FORBIDDEN — the canonical
        empty-summary bytes are a strict prefix of any
        non-empty-summary runner row, so loose match would
        accidentally waive legitimate runner-class failures."""
        violations: list = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if (
                    node.name
                    != "is_incomplete_summary_runner_lineage"
                ):
                    continue
                has_eq_check = False
                for sub in ast.walk(node):
                    # Reject endswith / startswith / __contains__
                    # / `in` shapes targeting the constant.
                    if isinstance(sub, ast.Call):
                        func = sub.func
                        if isinstance(func, ast.Attribute):
                            if func.attr in (
                                "endswith",
                                "startswith",
                                "__contains__",
                            ):
                                violations.append(
                                    f"predicate uses "
                                    f"{func.attr} — Slice 7 "
                                    f"requires == exact-match"
                                )
                    if isinstance(sub, ast.Compare):
                        # Reject `<const> in notes`
                        for op in sub.ops:
                            if isinstance(op, ast.In):
                                left = sub.left
                                if (
                                    isinstance(left, ast.Name)
                                    and left.id
                                    == "INCOMPLETE_SUMMARY_"
                                       "RUNNER_NOTES"
                                ):
                                    violations.append(
                                        "predicate uses `in` "
                                        "operator on "
                                        "incomplete-summary "
                                        "constant — Slice 7 "
                                        "requires == "
                                        "exact-match"
                                    )
                        # Look for `notes == <constant>` shape.
                        for op in sub.ops:
                            if isinstance(op, ast.Eq):
                                # Either side may be the Name.
                                comp = sub.comparators[0] if sub.comparators else None
                                left = sub.left
                                names = []
                                if isinstance(left, ast.Name):
                                    names.append(left.id)
                                if isinstance(comp, ast.Name):
                                    names.append(comp.id)
                                if (
                                    "INCOMPLETE_SUMMARY_RUNNER_NOTES"
                                    in names
                                    and "notes" in names
                                ):
                                    has_eq_check = True
                if not has_eq_check:
                    violations.append(
                        "predicate must use `notes == "
                        "INCOMPLETE_SUMMARY_RUNNER_NOTES` "
                        "exact-match"
                    )
                return tuple(violations)
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="lineage_waiver_constant_value_pinned",
            target_file=target,
            description=(
                "Phase 9 Slice 5 — module-level constant equals "
                "the exact legacy-downgrade note suffix bytes."
            ),
            validate=_validate_constant_value,
        ),
        ShippedCodeInvariant(
            invariant_name="lineage_waiver_uses_endswith_not_in",
            target_file=target,
            description=(
                "Phase 9 Slice 5 — operator-mandated tightness: "
                "predicate uses str.endswith, never `in`. "
                "Catches loosening regressions."
            ),
            validate=_validate_endswith_not_in,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "lineage_waiver_outcome_check_pinned"
            ),
            target_file=target,
            description=(
                "Phase 9 Slice 5 — predicate restricts to "
                "outcome=='runner'; never widens to other "
                "outcome classes."
            ),
            validate=_validate_outcome_check_pinned,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "lineage_waiver_incomplete_summary_constant"
            ),
            target_file=target,
            description=(
                "Slice 7 — module-level constant "
                "INCOMPLETE_SUMMARY_RUNNER_NOTES equals the "
                "canonical bytes emitted by classify_outcome's "
                "Step 6 default conservative fallback when "
                "both outcome and stop_reason are empty."
            ),
            validate=_validate_incomplete_summary_constant,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "lineage_waiver_incomplete_summary_exact_match"
            ),
            target_file=target,
            description=(
                "Slice 7 — operator-mandated tightness: "
                "is_incomplete_summary_runner_lineage MUST "
                "use == exact-match on the constant. "
                "endswith/startswith/in/__contains__ are "
                "FORBIDDEN — the canonical bytes are a strict "
                "prefix of any non-empty-summary runner row, "
                "so loose match would accidentally waive "
                "legitimate failures."
            ),
            validate=_validate_incomplete_summary_exact_match,
        ),
    ]


__all__ = [
    "INCOMPLETE_SUMMARY_RUNNER_NOTES",
    "LEGACY_CONTRACT_DOWNGRADE_NOTE_SUFFIX",
    "is_incomplete_summary_runner_lineage",
    "is_legacy_contract_downgrade",
    "register_shipped_invariants",
]
