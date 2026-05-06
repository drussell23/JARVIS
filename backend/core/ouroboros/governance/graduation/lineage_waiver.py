"""Phase 9 Slice 5 — Lineage waiver predicate.

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


# ---------------------------------------------------------------------------
# Pure-function predicate
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
    ]


__all__ = [
    "LEGACY_CONTRACT_DOWNGRADE_NOTE_SUFFIX",
    "is_legacy_contract_downgrade",
    "register_shipped_invariants",
]
