"""§32.5 Cleanup Arc — archive-only structural pins.

Auto-discovered by
:func:`shipped_code_invariants._discover_module_provided_invariants`
via the package walker. Owns the structural invariants that prevent
accidentally re-importing archived modules from production code.

Pinned modules (PRD §32.5.1):

  * ``graduation_orchestrator`` — superseded by M10 (PRD §32.4); the
    *design* (15-phase FSM + Bayesian AdaptiveThreshold + H1-H6
    hardening + 5-layer validation) was lifted into ``m10/`` while the
    code was archived to ``archive/legacy/`` per §32.5.
  * ``graduation_tracker`` — companion module with zero importers;
    archived alongside the orchestrator.

Invariants:

  1. ``graduation_orchestrator_archived_only_harness`` — verifies
     ``backend/core/ouroboros/battle_test/harness.py`` does NOT
     re-introduce the ``boot_graduation`` wiring or import
     ``GraduationOrchestrator``.
  2. ``graduation_orchestrator_archived_only_runtime_task`` — same
     for ``backend/core/runtime_task_orchestrator.py`` (used to host
     a structurally-unreachable graduation gate).
  3. ``graduation_orchestrator_archived_only_governed_loop`` — same
     for ``backend/core/ouroboros/governance/governed_loop_service.py``
     (used to host the always-None graduation_tracker hook).
  4. ``graduation_orchestrator_module_archived`` — sentinel pin
     targeting this module itself; asserts the archived files
     physically exist under ``archive/legacy/`` and the production
     locations do NOT re-introduce them. Future regressions hit this
     pin first.

Authority invariant: stdlib + the ``ShippedCodeInvariant``
registration contract ONLY. Zero orchestrator / iron_gate / policy /
providers / change_engine imports.
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Archive enforcement constants
# ---------------------------------------------------------------------------


_ARCHIVED_MODULE_NAMES: tuple = (
    "graduation_orchestrator",
    "graduation_tracker",
)

_ARCHIVED_DOTTED_PATHS: tuple = (
    "backend.core.ouroboros.governance.graduation_orchestrator",
    "backend.core.ouroboros.governance.graduation_tracker",
)

# Repo-relative paths the archived files MUST live at post-cleanup.
_EXPECTED_ARCHIVE_PATHS: tuple = (
    "archive/legacy/graduation_orchestrator_2026_04_06.py",
    "archive/legacy/graduation_tracker_2026_04_06.py",
    "archive/legacy/test_graduation_orchestrator_2026_04_06.py",
)

# Repo-relative paths where the modules MUST NOT live post-cleanup
# (regression catch — if a refactor accidentally restores them).
_FORBIDDEN_PRODUCTION_PATHS: tuple = (
    "backend/core/ouroboros/governance/graduation_orchestrator.py",
    "backend/core/ouroboros/governance/graduation_tracker.py",
)


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def _scan_for_archived_imports(
    tree: "ast.Module", *, target_label: str,
) -> list:
    """Walk ``tree`` for any ``import`` / ``from``-import that
    references the archived modules. Returns a list of human-
    readable violation strings."""
    violations: list = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in _ARCHIVED_DOTTED_PATHS:
                violations.append(
                    f"line {getattr(node, 'lineno', '?')}: "
                    f"{target_label} MUST NOT import "
                    f"{module!r} (archived per §32.5)"
                )
            # Also catch ``from .graduation_orchestrator import X``
            # style intra-package relative imports.
            for short in _ARCHIVED_MODULE_NAMES:
                if module.endswith(f".{short}") or module == short:
                    if module not in _ARCHIVED_DOTTED_PATHS:
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"{target_label} MUST NOT import "
                            f"{module!r} (archived per §32.5)"
                        )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name or ""
                if name in _ARCHIVED_DOTTED_PATHS:
                    violations.append(
                        f"line {getattr(node, 'lineno', '?')}: "
                        f"{target_label} MUST NOT import "
                        f"{name!r} (archived per §32.5)"
                    )
    return violations


def _validate_harness(
    tree: "ast.Module", source: str,
) -> tuple:
    """harness.py MUST NOT re-introduce ``boot_graduation`` /
    ``_graduation_orchestrator`` wiring."""
    violations: list = []
    violations.extend(
        _scan_for_archived_imports(
            tree, target_label="harness.py",
        ),
    )
    # AST scan for ``boot_graduation`` definition — should NOT
    # re-appear after cleanup.
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "boot_graduation":
                violations.append(
                    f"line {getattr(node, 'lineno', '?')}: "
                    f"harness.py MUST NOT re-introduce "
                    f"``boot_graduation`` (removed per §32.5; "
                    f"orchestrator instance was always-unreachable)"
                )
    # bytes-pin: instance attribute MUST stay removed.
    if "self._graduation_orchestrator" in source:
        violations.append(
            "harness.py MUST NOT re-introduce "
            "``self._graduation_orchestrator`` attribute "
            "(removed per §32.5)"
        )
    return tuple(violations)


def _validate_runtime_task_orchestrator(
    tree: "ast.Module", source: str,
) -> tuple:
    """runtime_task_orchestrator.py MUST NOT re-introduce the
    structurally-unreachable graduation gate (``_graduation_tracker``
    + ``_graduation_orchestrator`` chained calls)."""
    violations: list = []
    violations.extend(
        _scan_for_archived_imports(
            tree, target_label="runtime_task_orchestrator.py",
        ),
    )
    # bytes-pin: the always-None gate strings must stay removed.
    forbidden_strings = (
        "_graduation_tracker",
        "_graduation_orchestrator",
        "evaluate_graduation",
    )
    for s in forbidden_strings:
        if s in source:
            violations.append(
                f"runtime_task_orchestrator.py MUST NOT "
                f"re-introduce {s!r} (gate was structurally "
                f"unreachable; removed per §32.5)"
            )
    return tuple(violations)


def _validate_governed_loop_service(
    tree: "ast.Module", source: str,
) -> tuple:
    """governed_loop_service.py MUST NOT re-introduce the
    always-None graduation_tracker hook."""
    violations: list = []
    violations.extend(
        _scan_for_archived_imports(
            tree, target_label="governed_loop_service.py",
        ),
    )
    if "_graduation_tracker" in source:
        violations.append(
            "governed_loop_service.py MUST NOT re-introduce "
            "``_graduation_tracker`` hook (gate was always "
            "None; removed per §32.5)"
        )
    return tuple(violations)


def _repo_root() -> Path:
    """Resolve repo root from this module's location.
    ``backend/core/ouroboros/governance/cleanup_invariants.py`` →
    4 ``parent`` calls reach ``backend/`` parent = repo root."""
    return Path(__file__).resolve().parents[4]


def _validate_archive_provenance(
    tree: "ast.Module", source: str,  # noqa: ARG001
) -> tuple:
    """Sentinel pin targeting this module itself. Asserts:

      * Each archived file physically exists at its expected
        ``archive/legacy/`` path.
      * Each forbidden production path is absent.

    A regression that restores production paths fails here; a
    regression that deletes the archive provenance also fails
    here. NEVER raises."""
    violations: list = []
    try:
        root = _repo_root()
    except Exception as exc:  # noqa: BLE001
        violations.append(
            f"could not resolve repo root for archive "
            f"verification: {exc}"
        )
        return tuple(violations)

    for rel in _EXPECTED_ARCHIVE_PATHS:
        path = root / rel
        if not path.exists():
            violations.append(
                f"archived file missing: {rel} (expected "
                f"after §32.5 cleanup; restore from "
                f"git history if accidentally deleted)"
            )

    for rel in _FORBIDDEN_PRODUCTION_PATHS:
        path = root / rel
        if path.exists():
            violations.append(
                f"forbidden production path re-introduced: "
                f"{rel} (archived per §32.5; remove and "
                f"replace with M10 substrate)"
            )

    # Ensure provenance README exists so future operators know
    # WHY these files moved.
    readme = root / "archive" / "legacy" / "README.md"
    if not readme.exists():
        violations.append(
            "archive/legacy/README.md missing — required by "
            "§32.5 for design-lineage documentation"
        )

    return tuple(violations)


# ---------------------------------------------------------------------------
# Public API — register_shipped_invariants
# ---------------------------------------------------------------------------


def _validate_consumer_uses_primitive(
    tree: "ast.Module", source: str,
) -> tuple:
    """Slice 2 (Slice 5b consolidation arc) — every consumer
    of the discovery pattern MUST delegate to
    :func:`module_discovery.discover_module_provided_callable`
    rather than reimplementing the walk. This pin enforces
    that contract structurally on the three known consumers:
    flag_registry_seed / shipped_code_invariants /
    help_dispatcher.

    Detection: source MUST import
    ``discover_module_provided_callable`` AND MUST NOT contain
    ``pkgutil.iter_modules`` outside import-statement context."""
    violations: list = []
    has_primitive_import = False
    has_legacy_walk = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if "module_discovery" in module:
                for alias in node.names:
                    if (
                        alias.name
                        == "discover_module_provided_callable"
                    ):
                        has_primitive_import = True
        if isinstance(node, ast.Attribute):
            if (
                isinstance(node.value, ast.Name)
                and node.value.id == "pkgutil"
                and node.attr == "iter_modules"
            ):
                has_legacy_walk = True
    if not has_primitive_import:
        violations.append(
            "consumer MUST import "
            "discover_module_provided_callable from "
            "module_discovery (Slice 5b consolidation Slice 2 — "
            "no parallel walkers)"
        )
    if has_legacy_walk:
        violations.append(
            "consumer MUST NOT call pkgutil.iter_modules "
            "directly — delegate to module_discovery primitive"
        )
    return tuple(violations)


def register_shipped_invariants() -> list:
    """Module-owned ShippedCodeInvariant contributions for §32.5
    cleanup arc + Slice 5b consolidation Slice 2.
    Auto-discovered by
    :func:`shipped_code_invariants._discover_module_provided_invariants`.

    Returns 7 pins:

      1. ``graduation_orchestrator_archived_only_harness``
      2. ``graduation_orchestrator_archived_only_runtime_task``
      3. ``graduation_orchestrator_archived_only_governed_loop``
      4. ``graduation_orchestrator_module_archived`` (sentinel)
      5. ``module_discovery_consumer_flag_registry_seed`` —
         enforces flag_registry_seed delegates to the primitive
      6. ``module_discovery_consumer_shipped_code_invariants`` —
         same for shipped_code_invariants
      7. ``module_discovery_consumer_help_dispatcher`` —
         same for help_dispatcher
    """
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "graduation_orchestrator_archived_only_harness"
            ),
            target_file=(
                "backend/core/ouroboros/battle_test/harness.py"
            ),
            description=(
                "harness.py MUST NOT import the archived "
                "graduation_orchestrator / graduation_tracker "
                "modules and MUST NOT re-introduce the dead "
                "``boot_graduation`` wiring (§32.5 cleanup)."
            ),
            validate=_validate_harness,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "graduation_orchestrator_archived_only_runtime_task"
            ),
            target_file="backend/core/runtime_task_orchestrator.py",
            description=(
                "runtime_task_orchestrator.py MUST NOT "
                "re-introduce the structurally-unreachable "
                "graduation gate (``_graduation_tracker`` was "
                "never assigned anywhere; the chained "
                "evaluate_graduation call was dead code)."
            ),
            validate=_validate_runtime_task_orchestrator,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "graduation_orchestrator_archived_only_"
                "governed_loop"
            ),
            target_file=(
                "backend/core/ouroboros/governance/"
                "governed_loop_service.py"
            ),
            description=(
                "governed_loop_service.py MUST NOT re-introduce "
                "the always-None graduation_tracker hook in "
                "the op-completion finally block (§32.5 cleanup)."
            ),
            validate=_validate_governed_loop_service,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "graduation_orchestrator_module_archived"
            ),
            target_file=(
                "backend/core/ouroboros/governance/"
                "cleanup_invariants.py"
            ),
            description=(
                "Sentinel pin: archived files exist at expected "
                "``archive/legacy/`` paths; forbidden production "
                "paths are absent; provenance README is present "
                "(§32.5 cleanup integrity)."
            ),
            validate=_validate_archive_provenance,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "module_discovery_consumer_flag_registry_seed"
            ),
            target_file=(
                "backend/core/ouroboros/governance/"
                "flag_registry_seed.py"
            ),
            description=(
                "flag_registry_seed._discover_module_provided_"
                "flags MUST delegate to module_discovery."
                "discover_module_provided_callable (no parallel "
                "walker). Slice 5b consolidation Slice 2."
            ),
            validate=_validate_consumer_uses_primitive,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "module_discovery_consumer_shipped_code_invariants"
            ),
            target_file=(
                "backend/core/ouroboros/governance/meta/"
                "shipped_code_invariants.py"
            ),
            description=(
                "shipped_code_invariants._discover_module_-"
                "provided_invariants MUST delegate to "
                "module_discovery.discover_module_provided_-"
                "callable (no parallel walker). Slice 5b "
                "consolidation Slice 2."
            ),
            validate=_validate_consumer_uses_primitive,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "module_discovery_consumer_help_dispatcher"
            ),
            target_file=(
                "backend/core/ouroboros/governance/"
                "help_dispatcher.py"
            ),
            description=(
                "help_dispatcher._discover_module_provided_verbs "
                "MUST delegate to module_discovery."
                "discover_module_provided_callable (no parallel "
                "walker). Slice 5b consolidation Slice 2."
            ),
            validate=_validate_consumer_uses_primitive,
        ),
    ]


__all__ = ["register_shipped_invariants"]
