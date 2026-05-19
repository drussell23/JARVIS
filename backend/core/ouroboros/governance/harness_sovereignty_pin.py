"""HarnessSovereigntyPin — Slice 4 #2: structural §1 closure.

The §1 root cause (documented verbatim in :mod:`ledger_sovereignty`'s
header): ``HarnessConfig.repo_path`` defaults to ``Path(".")`` and
``AutoCommitter`` accepts any ``repo_root``; during scheduled soaks
that resolved to the operator's live checkout → 4× franken-commits
racing operator work.

Runtime defenses already exist and are tested:
  * ``AutoCommitter.commit()`` invokes the OCA autonomous gate
    (``verify_pre_commit(channel="autonomous")``) **before** any
    staging — Slice 3 #4.
  * ``AutoCommitter._assert_commit_target_sovereign`` composes
    ``ledger_sovereignty.assert_ledger_sovereignty`` — Slice 2.
  * The harness boot runs ``_boot_ledger_sovereignty_workspace``
    which stamps an owned worktree + sets
    ``JARVIS_AUTO_COMMIT_WORKSPACE`` so commits never land on the
    ``.``-defaulted operator main.

This module makes those defenses **structurally un-removable**: a
refactor that drops the pre-staging sovereignty gate, or removes
the harness's owned-workspace boot phase, fails the auto-discovered
shipped-code invariant (CI / meta-validation red) — it never
silently regresses to the §1 franken-commit failure mode.

Pure AST analysis. Composes the canonical
:mod:`meta.shipped_code_invariants` registry (auto-discovered by
``module_discovery`` via the ``register_shipped_invariants`` name —
zero registry edits). NEVER raises; ImportError-tolerant.
"""
from __future__ import annotations

import ast as _ast
import logging
from typing import Tuple

logger = logging.getLogger("Ouroboros.HarnessSovereigntyPin")


HARNESS_SOVEREIGNTY_PIN_SCHEMA_VERSION: str = (
    "harness_sovereignty_pin.v1"
)


# ---------------------------------------------------------------------------
# Invariant 1 — AutoCommitter gates BEFORE staging
# ---------------------------------------------------------------------------


def _validate_autocommitter_pre_stage_gate(
    tree: "_ast.Module", source: str,
) -> Tuple[str, ...]:
    """``commit()`` MUST invoke the OCA autonomous verdict gate
    (``verify_pre_commit``) AND the sovereignty assert
    (``_assert_commit_target_sovereign``) BEFORE the first
    git-staging call (``git add`` / a ``"add"`` subprocess arg).

    Source-order is load-bearing: a guard that runs *after*
    staging is no guard at all (the franken-commit already
    touched the index). We assert the gate Call nodes precede the
    first staging token by line number within ``commit``.
    """
    violations = []
    commit_fn = None
    for node in _ast.walk(tree):
        if (
            isinstance(node, _ast.AsyncFunctionDef)
            and node.name == "commit"
        ):
            commit_fn = node
            break
    if commit_fn is None:
        return (
            "auto_committer: async def commit() not found — the "
            "§1 pin cannot verify the pre-stage gate ordering",
        )

    gate_line = None        # first verify_pre_commit / sovereignty assert
    stage_line = None       # first 'add' staging token
    for n in _ast.walk(commit_fn):
        ln = getattr(n, "lineno", None)
        if ln is None:
            continue
        if isinstance(n, _ast.Call):
            fn = n.func
            name = (
                fn.attr if isinstance(fn, _ast.Attribute)
                else fn.id if isinstance(fn, _ast.Name) else ""
            )
            if name in (
                "verify_pre_commit",
                "_assert_commit_target_sovereign",
            ):
                gate_line = ln if gate_line is None else min(
                    gate_line, ln,
                )
        if isinstance(n, _ast.Constant) and n.value == "add":
            stage_line = ln if stage_line is None else min(
                stage_line, ln,
            )

    if gate_line is None:
        violations.append(
            "auto_committer.commit() does NOT compose the "
            "pre-stage sovereignty/OCA gate (verify_pre_commit / "
            "_assert_commit_target_sovereign) — §1 regression"
        )
    if (
        gate_line is not None
        and stage_line is not None
        and gate_line >= stage_line
    ):
        violations.append(
            f"auto_committer.commit(): sovereignty/OCA gate "
            f"(line {gate_line}) does NOT precede the first git "
            f"staging token (line {stage_line}) — a post-stage "
            f"guard cannot prevent a franken-commit"
        )
    # Defense-in-depth: the literal autonomous channel must be the
    # one fed to the gate (never env/resolve — Slice 3 #4).
    if '"autonomous"' not in source and "'autonomous'" not in source:
        violations.append(
            "auto_committer must gate with the LITERAL "
            "channel='autonomous' (never env / resolve_commit_"
            "channel for the autonomous committer)"
        )
    return tuple(violations)


# ---------------------------------------------------------------------------
# Invariant 2 — harness boot stamps an owned workspace
# ---------------------------------------------------------------------------


def _validate_harness_owned_workspace_boot(
    tree: "_ast.Module", source: str,
) -> Tuple[str, ...]:
    """The harness boot sequence MUST invoke
    ``_boot_ledger_sovereignty_workspace`` — the phase that stamps
    a ``ledger_sovereignty`` owned worktree and sets
    ``JARVIS_AUTO_COMMIT_WORKSPACE`` so AutoCommitter never commits
    into the ``Path(".")``-defaulted operator main. Removing that
    phase silently reopens §1.
    """
    violations = []
    called = any(
        (
            isinstance(n, _ast.Call)
            and isinstance(n.func, _ast.Attribute)
            and n.func.attr == "_boot_ledger_sovereignty_workspace"
        )
        for n in _ast.walk(tree)
    )
    defined = any(
        isinstance(n, _ast.AsyncFunctionDef)
        and n.name == "_boot_ledger_sovereignty_workspace"
        for n in _ast.walk(tree)
    )
    if not defined:
        violations.append(
            "harness: _boot_ledger_sovereignty_workspace method "
            "removed — §1 owned-workspace stamping gone"
        )
    if not called:
        violations.append(
            "harness: _boot_ledger_sovereignty_workspace is never "
            "invoked in the boot sequence — AutoCommitter would "
            "commit into the Path('.')-defaulted operator main "
            "(the §1 franken-commit root cause)"
        )
    # The env seam name is the single indirection point — pin it
    # so a rename can't silently sever harness↔AutoCommitter.
    if "JARVIS_AUTO_COMMIT_WORKSPACE" not in source:
        violations.append(
            "harness no longer references "
            "JARVIS_AUTO_COMMIT_WORKSPACE — the owned-worktree "
            "commit-cwd seam is severed"
        )
    return tuple(violations)


# ---------------------------------------------------------------------------
# Auto-discovered registration
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    ac = "backend/core/ouroboros/governance/auto_committer.py"
    hn = "backend/core/ouroboros/battle_test/harness.py"
    return [
        ShippedCodeInvariant(
            invariant_name="autocommitter_pre_stage_sovereignty_gate",
            target_file=ac,
            description=(
                "AutoCommitter.commit() composes the OCA "
                "autonomous gate + sovereignty assert BEFORE the "
                "first git staging token (source-order pinned); "
                "the autonomous channel is the literal string. "
                "Closes §1 structurally — a post-stage or absent "
                "guard fails CI."
            ),
            validate=_validate_autocommitter_pre_stage_gate,
        ),
        ShippedCodeInvariant(
            invariant_name="harness_owned_workspace_boot_phase",
            target_file=hn,
            description=(
                "The harness boot sequence invokes "
                "_boot_ledger_sovereignty_workspace and references "
                "JARVIS_AUTO_COMMIT_WORKSPACE — AutoCommitter "
                "never commits into the Path('.')-defaulted "
                "operator main (§1 root cause)."
            ),
            validate=_validate_harness_owned_workspace_boot,
        ),
    ]


__all__ = [
    "HARNESS_SOVEREIGNTY_PIN_SCHEMA_VERSION",
    "register_shipped_invariants",
]
