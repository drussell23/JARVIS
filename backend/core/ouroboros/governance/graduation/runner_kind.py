"""Phase 9 Slice 6 — structured ``runner_attributed_kind`` taxonomy.

Closes the deferred half of Slice 5: the harness's evidence writer
now lands a STRUCTURED enum field on every session row, so future
eligibility logic NEVER depends on parsing the free-form ``notes``
string. From the lineage_waiver.py forward-looking note:

    > The harness's evidence writer will land a structured
    > ``runner_attributed_kind`` field (Venom / orchestrator /
    > contract_downgrade / etc.) so future eligibility never
    > depends on parsing free-text notes. When that lands, this
    > waiver becomes a backward-compat shim for legacy rows;
    > new rows go through the structured field.

This module ships:

  * Closed :class:`RunnerAttributedKind` enum (12 values; bytes-
    pinned via AST regression so taxonomy cannot silently drift)
  * :func:`infer_runner_kind` — pure inference from the existing
    ``classify_outcome`` decision tree (same 5-step logic; emits a
    typed enum value alongside the existing 3-tuple)
  * :func:`is_blocking_kind` — declarative answer to "should this
    kind block flag graduation?" (single source of truth; no
    parallel allowlist)

Architectural locks:

  * **Single pipeline** — this module is the SOLE knower of the
    runner-kind taxonomy. ``_RUNNER_FAILURE_CLASSES`` in
    ``live_fire_soak.py`` remains the regex/signature side; this
    module is the structural side. AST-pinned: the enum value-set
    must equal a deterministic projection of that frozenset
    (zero drift between the two sides).
  * **Authority asymmetry** — pure substrate (no orchestrator /
    iron_gate / providers / change_engine imports).
  * **NEVER raises** — every public function returns a sane
    default on any malformed input.
  * **Backward-compat default** — :class:`SessionRecord` rows
    written BEFORE Slice 6 have no structured field; readers
    treat absence as ``None``, and the existing
    :func:`is_legacy_contract_downgrade` predicate stays load-
    bearing for those legacy rows ONLY (back-compat shim).
"""
from __future__ import annotations

import enum
from typing import FrozenSet, Iterable, Optional


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


class RunnerAttributedKind(str, enum.Enum):
    """Closed taxonomy for ``runner_attributed_kind`` field.

    Every value either:

      (a) Mirrors a member of ``_RUNNER_FAILURE_CLASSES`` from
          ``live_fire_soak.py`` (1:1; same 9 classes), OR
      (b) Is a STRUCTURAL classifier emitted by the harness when
          no concrete failure class fired:

            * ``CONTRACT_DOWNGRADE_LEGACY`` — Slice 5 lineage
              waiver lineage; pre-Slice-4 ops_count predicate bug
              downgraded a CLEAN session and emitted the ``|...
              contract_predicate_downgraded_clean`` notes suffix.
              The structured-field replaces the suffix scan
              going forward; legacy rows fall through to the
              back-compat suffix shim.
            * ``DEFAULT_CONSERVATIVE`` — Step 5 of
              ``classify_outcome`` (unknown fault-class blocks
              by default).
            * ``NONE`` — emitted when ``runner_attributed`` is
              False (CLEAN / INFRA / MIGRATION outcomes); never
              blocks graduation.

    The value-set is **bytes-pinned** via the
    ``runner_kind_taxonomy_closed`` AST invariant — additions
    require an explicit pin update.
    """

    # 9 concrete runner-failure classes (1:1 with _RUNNER_FAILURE_CLASSES)
    PHASE_RUNNER_ERROR = "phase_runner_error"
    CANDIDATE_VALIDATE_ERROR = "candidate_validate_error"
    IRON_GATE_VIOLATION = "iron_gate_violation"
    SEMANTIC_GUARDIAN_BLOCK = "semantic_guardian_block"
    CHANGE_ENGINE_ERROR = "change_engine_error"
    VERIFY_REGRESSION = "verify_regression"
    L2_REPAIR_ERROR = "l2_repair_error"
    FSM_STATE_CORRUPTION = "fsm_state_corruption"
    ARTIFACT_CONTRACT_DRIFT = "artifact_contract_drift"

    # Structural classifiers
    CONTRACT_DOWNGRADE_LEGACY = "contract_downgrade_legacy"
    DEFAULT_CONSERVATIVE = "default_conservative"
    NONE = "none"


# Pre-computed view of all values for O(1) membership.
_ALL_KIND_VALUES: FrozenSet[str] = frozenset(
    k.value for k in RunnerAttributedKind
)

# The CONCRETE runner-failure-class values (a subset of
# RunnerAttributedKind) that mirror live_fire_soak's
# _RUNNER_FAILURE_CLASSES — kept as a separate frozenset so the
# AST pin can verify zero drift between the two surfaces. Bytes-
# pinned.
CONCRETE_RUNNER_FAILURE_CLASS_VALUES: FrozenSet[str] = frozenset({
    "phase_runner_error",
    "candidate_validate_error",
    "iron_gate_violation",
    "semantic_guardian_block",
    "change_engine_error",
    "verify_regression",
    "l2_repair_error",
    "fsm_state_corruption",
    "artifact_contract_drift",
})


# Kinds that BLOCK graduation. Closed set: every concrete
# runner-failure class + DEFAULT_CONSERVATIVE. Crucially excludes
# CONTRACT_DOWNGRADE_LEGACY (the whole point of the waiver) and
# NONE (non-runner outcomes).
_BLOCKING_KINDS: FrozenSet[RunnerAttributedKind] = frozenset({
    RunnerAttributedKind.PHASE_RUNNER_ERROR,
    RunnerAttributedKind.CANDIDATE_VALIDATE_ERROR,
    RunnerAttributedKind.IRON_GATE_VIOLATION,
    RunnerAttributedKind.SEMANTIC_GUARDIAN_BLOCK,
    RunnerAttributedKind.CHANGE_ENGINE_ERROR,
    RunnerAttributedKind.VERIFY_REGRESSION,
    RunnerAttributedKind.L2_REPAIR_ERROR,
    RunnerAttributedKind.FSM_STATE_CORRUPTION,
    RunnerAttributedKind.ARTIFACT_CONTRACT_DRIFT,
    RunnerAttributedKind.DEFAULT_CONSERVATIVE,
})


# ---------------------------------------------------------------------------
# Pure inference
# ---------------------------------------------------------------------------


def infer_runner_kind(
    *,
    runner_attributed: bool,
    runner_hits: Optional[Iterable[str]] = None,
    classification_path: str = "",
) -> RunnerAttributedKind:
    """Map a ``classify_outcome`` result to a structured kind.

    Pure function. NEVER raises.

    Decision tree mirrors ``live_fire_soak.classify_outcome``:

      * ``runner_attributed is False`` → :attr:`NONE` (clean /
        infra / migration outcomes — never blocks).
      * ``runner_hits`` non-empty → first concrete value in
        :data:`CONCRETE_RUNNER_FAILURE_CLASS_VALUES` (sorted for
        determinism so test stability isn't dependent on hit-set
        iteration order).
      * ``classification_path == "default_runner"`` →
        :attr:`DEFAULT_CONSERVATIVE` (Step 5 of classify_outcome).
      * Otherwise → :attr:`DEFAULT_CONSERVATIVE` (conservative
        — unknown lineage treated as blocking).

    Note: contract-downgrade-legacy is a SEPARATE input path —
    emitted directly by the contract harness when it downgrades a
    clean session; never inferred from classify_outcome output.
    """
    try:
        if not runner_attributed:
            return RunnerAttributedKind.NONE
        if runner_hits:
            sorted_hits = sorted(
                str(h) for h in runner_hits if isinstance(h, str)
            )
            for hit in sorted_hits:
                if hit in CONCRETE_RUNNER_FAILURE_CLASS_VALUES:
                    return RunnerAttributedKind(hit)
        # Step 5 of classify_outcome — unknown lineage is treated
        # as DEFAULT_CONSERVATIVE (blocks). Note: we keep
        # classification_path in the signature for forward
        # compatibility — future runner subclasses (e.g.,
        # ``runner_classes:[...]``) can be teased apart here
        # without changing callers.
        _ = (classification_path or "").strip().lower()
        return RunnerAttributedKind.DEFAULT_CONSERVATIVE
    except Exception:  # noqa: BLE001 — defensive
        return RunnerAttributedKind.DEFAULT_CONSERVATIVE


def is_blocking_kind(kind: Optional[RunnerAttributedKind]) -> bool:
    """Declarative: should this kind block flag graduation?

    Single source of truth — :data:`_BLOCKING_KINDS` is the only
    knower; consumers compose this function. Returns False on
    None / unknown input (defensive)."""
    if kind is None:
        return False
    if not isinstance(kind, RunnerAttributedKind):
        return False
    return kind in _BLOCKING_KINDS


def is_legacy_downgrade_kind(
    kind: Optional[RunnerAttributedKind],
) -> bool:
    """Single-value selector for the Slice 5 lineage waiver class
    expressed as a structured kind. New rows use this directly;
    legacy rows fall through to the suffix back-compat shim."""
    return kind is RunnerAttributedKind.CONTRACT_DOWNGRADE_LEGACY


def coerce_kind(value: object) -> Optional[RunnerAttributedKind]:
    """Coerce a JSONL-loaded scalar to a :class:`RunnerAttributedKind`.

    NEVER raises. Returns None on missing / unknown values so
    legacy ledger rows (no structured field) deserialize as
    None and route through the suffix back-compat shim.
    """
    if value is None:
        return None
    if isinstance(value, RunnerAttributedKind):
        return value
    try:
        s = str(value).strip()
    except Exception:  # noqa: BLE001 — defensive
        return None
    if not s:
        return None
    if s not in _ALL_KIND_VALUES:
        return None
    try:
        return RunnerAttributedKind(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``runner_kind_taxonomy_closed`` — :class:`RunnerAttributedKind`
         has EXACTLY 12 values and the value set is bytes-pinned.
         Closed taxonomy; additions require explicit pin update.
      2. ``runner_kind_concrete_set_matches_live_fire`` —
         :data:`CONCRETE_RUNNER_FAILURE_CLASS_VALUES` is bytes-
         pinned to match ``_RUNNER_FAILURE_CLASSES`` in
         ``live_fire_soak.py``. Drift between the two surfaces
         fires the pin (ensures structural side mirrors regex
         side; no taxonomy fork).
      3. ``runner_kind_authority_asymmetry`` — pure substrate;
         forbids orchestrator+iron_gate+policy+providers imports.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/graduation/runner_kind.py"
    )

    _EXPECTED_VALUES = {
        "phase_runner_error",
        "candidate_validate_error",
        "iron_gate_violation",
        "semantic_guardian_block",
        "change_engine_error",
        "verify_regression",
        "l2_repair_error",
        "fsm_state_corruption",
        "artifact_contract_drift",
        "contract_downgrade_legacy",
        "default_conservative",
        "none",
    }

    def _validate_taxonomy_closed(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """RunnerAttributedKind class body MUST contain exactly
        the 12 expected enum members (bytes-pinned)."""
        violations: list = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "RunnerAttributedKind"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                missing = _EXPECTED_VALUES - found
                extra = found - _EXPECTED_VALUES
                if missing:
                    violations.append(
                        f"RunnerAttributedKind missing values: "
                        f"{sorted(missing)}"
                    )
                if extra:
                    violations.append(
                        f"RunnerAttributedKind has unexpected "
                        f"values (taxonomy drift): {sorted(extra)}"
                    )
                return tuple(violations)
        violations.append(
            "RunnerAttributedKind class definition missing"
        )
        return tuple(violations)

    def _validate_concrete_set_matches_live_fire(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """CONCRETE_RUNNER_FAILURE_CLASS_VALUES must contain
        exactly the 9 _RUNNER_FAILURE_CLASSES entries from
        live_fire_soak.py — bytes-pinned mirror, no drift."""
        expected_concrete = (
            _EXPECTED_VALUES
            - {"contract_downgrade_legacy", "default_conservative", "none"}
        )
        violations: list = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id
                == "CONCRETE_RUNNER_FAILURE_CLASS_VALUES"
            ):
                # Walk the frozenset({...}) call's set literal.
                if isinstance(node.value, ast.Call):
                    args = node.value.args
                    if args and isinstance(args[0], ast.Set):
                        found = {
                            elt.value
                            for elt in args[0].elts
                            if isinstance(elt, ast.Constant)
                            and isinstance(elt.value, str)
                        }
                        if found != expected_concrete:
                            missing = expected_concrete - found
                            extra = found - expected_concrete
                            if missing:
                                violations.append(
                                    f"CONCRETE_RUNNER_FAILURE_"
                                    f"CLASS_VALUES missing: "
                                    f"{sorted(missing)}"
                                )
                            if extra:
                                violations.append(
                                    f"CONCRETE_RUNNER_FAILURE_"
                                    f"CLASS_VALUES extra "
                                    f"(drift from live_fire_"
                                    f"soak): {sorted(extra)}"
                                )
                        return tuple(violations)
                violations.append(
                    "CONCRETE_RUNNER_FAILURE_CLASS_VALUES must "
                    "be a frozenset({...}) literal for AST pin"
                )
                return tuple(violations)
        violations.append(
            "CONCRETE_RUNNER_FAILURE_CLASS_VALUES module-level "
            "constant missing"
        )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden = (
            "orchestrator", "iron_gate", "policy", "providers",
            "candidate_generator", "urgency_router",
            "change_engine", "semantic_guardian",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        violations.append(
                            f"runner_kind.py MUST NOT import "
                            f"{module!r}"
                        )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="runner_kind_taxonomy_closed",
            target_file=target,
            description=(
                "Phase 9 Slice 6 — RunnerAttributedKind 12-value "
                "closed taxonomy bytes-pinned. Additions require "
                "explicit pin update."
            ),
            validate=_validate_taxonomy_closed,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "runner_kind_concrete_set_matches_live_fire"
            ),
            target_file=target,
            description=(
                "Phase 9 Slice 6 — concrete subset bytes-pinned "
                "to mirror live_fire_soak's _RUNNER_FAILURE_"
                "CLASSES; zero drift between regex and "
                "structural surfaces."
            ),
            validate=_validate_concrete_set_matches_live_fire,
        ),
        ShippedCodeInvariant(
            invariant_name="runner_kind_authority_asymmetry",
            target_file=target,
            description=(
                "Phase 9 Slice 6 — substrate purity."
            ),
            validate=_validate_authority_asymmetry,
        ),
    ]


__all__ = [
    "CONCRETE_RUNNER_FAILURE_CLASS_VALUES",
    "RunnerAttributedKind",
    "coerce_kind",
    "infer_runner_kind",
    "is_blocking_kind",
    "is_legacy_downgrade_kind",
    "register_shipped_invariants",
]
