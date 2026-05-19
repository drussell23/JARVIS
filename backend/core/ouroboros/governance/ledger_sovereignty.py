"""Ledger Sovereignty — structural commit-target ownership boundary.

**Why this exists** — the v18 SWE arc observed 4× franken-commits to
the operator's main checkout (e.g. ``377cc230eb``, 997-line ghost
commits racing operator work). Root cause: ``AutoCommitter`` accepts
any ``repo_root`` Path; during scheduled soaks that path resolved to
the operator's live checkout because ``HarnessConfig.repo_path``
defaulted to ``"."`` and was never overridden to an isolated work
area. The autonomous loop is therefore *structurally capable* of
committing to a tree it doesn't own.

**The structural fix** — pure-add typed ownership marker:

  1. When a controlled work-area is created (e.g. by
     :class:`WorktreeManager`), it stamps a sovereignty marker at
     ``<root>/.jarvis/ledger_ownership.json`` carrying
     ``session_id``, ``branch_name``, ``creator_pid``, and a
     monotonic ``schema_version``.
  2. Any path that lacks a valid marker is, by definition, not owned
     by the autonomous loop.
  3. :func:`assert_ledger_sovereignty` is a fail-closed predicate —
     raises :exc:`LedgerSovereigntyError` when the master flag is on
     and the target is not owned. Off-master is byte-identical
     (returns silently).

This module is *substrate only*. It does not import — and must not
import — ``AutoCommitter``, the orchestrator, or any policy module.
The dependency direction is one-way: ``AutoCommitter`` and
``WorktreeManager`` (Slice 2 wiring) compose this module's public
surface; this module composes nothing from governance.

Master flag: ``JARVIS_LEDGER_SOVEREIGNTY_ENABLED`` (default
**FALSE**, §33.1). When off, every entry point is a no-op or
pure-data accessor — the soak boots byte-identically to the
pre-substrate world.

Slice 1 ships only the marker substrate + the assertion. Slice 2
wires :class:`WorktreeManager.create` to stamp the marker and
:class:`AutoCommitter` to assert before any ``git commit`` call.
Slice 3 adds the singleton scheduler lock.
"""
from __future__ import annotations

import ast as _ast
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


LEDGER_SOVEREIGNTY_SCHEMA_VERSION = "1.0"
"""Bumped only when :class:`OwnershipRecord` gains/removes a
load-bearing field; current consumers (Slice 2 ``AutoCommitter``)
must update in lock-step. Additive fields stay at the same
version (forward-compatible decode)."""


_MARKER_RELATIVE_PATH = (".jarvis", "ledger_ownership.json")
"""Marker landing path relative to the owned work-area root.

Deliberately under ``.jarvis/`` so existing tooling that already
respects ``.jarvis`` (config writes, debug logs, REPL history)
treats this consistently. Tuple-of-segments rather than a string
so ``Path(*marker)`` stays OS-portable."""


# ---------------------------------------------------------------------------
# Master flag (§33.1 default-FALSE)
# ---------------------------------------------------------------------------


_MASTER_FLAG = "JARVIS_LEDGER_SOVEREIGNTY_ENABLED"


def master_enabled() -> bool:
    """Return ``True`` iff the sovereignty gate is master-ON.

    Two independent enable sources (OR-composed):

      1. ``JARVIS_LEDGER_SOVEREIGNTY_ENABLED=true`` env (byte-
         identical legacy behavior — when set, returns True
         without touching disk).
      2. A signed, out-of-repo persistent enable record (composes
         :mod:`persistent_master`). This is the ONLY way the gate
         can be ON for a Cursor/VS Code GUI-git subprocess, which
         inherits no shell env — the same structural reason OCA
         needed its persistent enable. Tamper-evident, fail-closed.

    Default remains ``False`` per §33.1 (no env + no signed
    record). The persistent path NEVER raises and degrades to
    env-only if the substrate is unavailable.
    """
    if os.environ.get(_MASTER_FLAG, "false").lower() == "true":
        return True
    try:
        from backend.core.ouroboros.governance.persistent_master import (
            is_persistently_enabled,
        )
        return is_persistently_enabled("ledger_sovereignty")
    except Exception:  # noqa: BLE001 — fail closed to env-only
        return False


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class LedgerSovereigntyError(RuntimeError):
    """Raised by :func:`assert_ledger_sovereignty` when master is
    on and the target path is not a registered, owned work-area.

    Subclasses :class:`RuntimeError` rather than a typed governance
    exception so existing ``except Exception`` defenders catch it
    naturally. Carries structured fields for telemetry.
    """

    def __init__(
        self,
        path: Path,
        reason: str,
        *,
        expected_session_id: Optional[str] = None,
        actual_session_id: Optional[str] = None,
    ) -> None:
        self.path = Path(path)
        self.reason = reason
        self.expected_session_id = expected_session_id
        self.actual_session_id = actual_session_id
        suffix = ""
        if expected_session_id and actual_session_id:
            suffix = (
                f" (expected session={expected_session_id!r}, "
                f"actual={actual_session_id!r})"
            )
        super().__init__(
            f"Ledger sovereignty violation at {self.path}: "
            f"{reason}{suffix}"
        )


# ---------------------------------------------------------------------------
# OwnershipRecord
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OwnershipRecord:
    """Marker payload — what gets written to disk.

    Frozen so consumers can't mutate the read-back snapshot.
    ``to_dict`` / ``from_dict`` give §33.5 lossless roundtrip for
    forward compatibility — Slice 2+ may add fields without
    breaking Slice 1 readers.
    """

    session_id: str
    branch_name: str
    creator_pid: int
    created_at: float = field(default_factory=time.time)
    schema_version: str = LEDGER_SOVEREIGNTY_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "OwnershipRecord":
        """Build a record from a previously-serialized dict.

        Unknown keys are ignored (forward-compat). Missing
        ``schema_version`` defaults to the current — older markers
        predating this field are accepted but flagged as legacy
        via the default value.
        """
        return cls(
            session_id=str(payload.get("session_id", "")),
            branch_name=str(payload.get("branch_name", "")),
            creator_pid=int(payload.get("creator_pid", 0)),
            created_at=float(payload.get("created_at", 0.0)),
            schema_version=str(
                payload.get(
                    "schema_version",
                    LEDGER_SOVEREIGNTY_SCHEMA_VERSION,
                )
            ),
        )


# ---------------------------------------------------------------------------
# Marker path
# ---------------------------------------------------------------------------


def marker_path(work_area_root: Path) -> Path:
    """Return the absolute marker path for a given work-area root.

    Pure function — does not touch the filesystem. Callers use this
    to locate (existing or future) markers consistently across the
    write side (:func:`mark_owned`) and the read side
    (:func:`read_ownership`).
    """
    return Path(work_area_root, *_MARKER_RELATIVE_PATH)


# ---------------------------------------------------------------------------
# Mark owned (write side)
# ---------------------------------------------------------------------------


def mark_owned(
    work_area_root: Path,
    *,
    session_id: str,
    branch_name: str,
    creator_pid: Optional[int] = None,
) -> Optional[OwnershipRecord]:
    """Stamp a sovereignty marker at ``<root>/.jarvis/ledger_ownership.json``.

    Returns the :class:`OwnershipRecord` on success, ``None`` on
    any I/O failure. NEVER raises — the caller (Slice 2
    ``WorktreeManager.create``) is fail-closed on missing markers,
    so a write failure is naturally surfaced by the downstream
    assertion. We log loud + return ``None``.

    The work-area root MUST already exist (this function does not
    create it). The ``.jarvis/`` subdirectory IS created with
    ``parents=True`` for convenience.
    """
    root = Path(work_area_root)
    pid = (
        int(creator_pid) if creator_pid is not None
        else int(os.getpid())
    )
    record = OwnershipRecord(
        session_id=str(session_id),
        branch_name=str(branch_name),
        creator_pid=pid,
    )
    target = marker_path(root)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Atomic-ish write via tmp + replace — avoids torn reads if
        # the assertion path races the write.
        tmp = target.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(record.to_dict(), sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, target)
    except Exception as err:  # noqa: BLE001 — fail-closed surface
        logger.warning(
            "[ledger_sovereignty] mark_owned write failed at %s: "
            "%r — downstream assertion will refuse this path",
            target, err,
        )
        return None
    logger.info(
        "[ledger_sovereignty] marked %s as owned "
        "(session=%s, branch=%s, pid=%d)",
        root, session_id, branch_name, pid,
    )
    return record


# ---------------------------------------------------------------------------
# Read ownership (read side)
# ---------------------------------------------------------------------------


def read_ownership(
    work_area_root: Path,
) -> Optional[OwnershipRecord]:
    """Return the parsed :class:`OwnershipRecord` if a valid marker
    exists at ``<root>/.jarvis/ledger_ownership.json``, else ``None``.

    Treats missing files, unreadable files, invalid JSON, and
    malformed payloads identically: returns ``None``. NEVER raises.
    The downstream assertion treats ``None`` as "not owned" — the
    safest possible posture.
    """
    target = marker_path(work_area_root)
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception as err:  # noqa: BLE001
        logger.debug(
            "[ledger_sovereignty] marker unreadable at %s: %r",
            target, err,
        )
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return OwnershipRecord.from_dict(payload)
    except Exception as err:  # noqa: BLE001
        logger.debug(
            "[ledger_sovereignty] marker malformed at %s: %r",
            target, err,
        )
        return None


def is_owned(work_area_root: Path) -> bool:
    """Convenience predicate — ``read_ownership(root) is not None``.

    Use this for boolean checks where the caller doesn't need the
    record fields. Cheap (single ``Path.exists`` + JSON parse).
    """
    return read_ownership(work_area_root) is not None


# ---------------------------------------------------------------------------
# Assertion (the structural boundary)
# ---------------------------------------------------------------------------


def assert_ledger_sovereignty(
    work_area_root: Path,
    *,
    expected_session_id: Optional[str] = None,
) -> None:
    """Raise :exc:`LedgerSovereigntyError` iff the master flag is
    on and ``work_area_root`` is not an owned work-area.

    This is the load-bearing predicate Slice 2 wires into
    ``AutoCommitter``. When master is **off** (default), this is a
    pure no-op — byte-identical pre-substrate behavior.

    When master is **on**, the check is fail-closed:

      * No marker present → raise (the operator's main checkout)
      * Marker present but unparseable → raise (corrupted /
        partial write)
      * Marker present AND ``expected_session_id`` supplied AND
        doesn't match → raise (cross-session contamination — a
        stale worktree from an earlier soak)

    The optional ``expected_session_id`` lets callers tighten the
    check to "I want THIS session's work-area, not any owned one."
    Most callers will pass it for defense-in-depth.
    """
    if not master_enabled():
        return  # §33.1 master-FALSE byte-identical path

    root = Path(work_area_root)
    record = read_ownership(root)
    if record is None:
        raise LedgerSovereigntyError(
            root,
            reason="no ownership marker found at "
            f"{marker_path(root)}",
        )
    if (
        expected_session_id is not None
        and record.session_id != expected_session_id
    ):
        raise LedgerSovereigntyError(
            root,
            reason="session_id mismatch",
            expected_session_id=expected_session_id,
            actual_session_id=record.session_id,
        )


# ---------------------------------------------------------------------------
# §33.3 register_shipped_invariants — auto-discovered AST pins
# ---------------------------------------------------------------------------


_TARGET_FILE = (
    "backend/core/ouroboros/governance/ledger_sovereignty.py"
)


def register_shipped_invariants() -> list:
    """AST pins — auto-discovered by the §33.3 meta runner."""
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate_master_default_false(
        tree: _ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        """master_enabled() default arg literal MUST be 'false'.

        This is the §33.1 pin — drift to 'true' would silently flip
        the gate on for every operator. Substrate guarantees the
        master flag stays FALSE until graduation.
        """
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                # Walk body for the Call to os.environ.get with
                # default arg.
                for sub in _ast.walk(node):
                    if (
                        isinstance(sub, _ast.Call)
                        and len(sub.args) >= 2
                        and isinstance(sub.args[1], _ast.Constant)
                    ):
                        if sub.args[1].value != "false":
                            return (
                                "master_enabled() default arg "
                                f"drift: {sub.args[1].value!r} "
                                "(expected 'false')",
                            )
                        return ()
                return (
                    "master_enabled() body missing default-arg "
                    "literal",
                )
        return ("master_enabled() not found",)

    def _validate_authority_asymmetry(
        tree: _ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        """ledger_sovereignty is the substrate — it MUST NOT import
        the modules that compose it. Dependency direction is
        one-way; reversing it would create a cycle and let policy
        modules redefine ownership semantics."""
        forbidden = {
            "backend.core.ouroboros.governance.auto_committer",
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            "backend.core.ouroboros.governance.policy_engine",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.worktree_manager",
            (
                "backend.core.ouroboros.governance.autonomy."
                "worktree_manager"
            ),
            "backend.core.ouroboros.governance.semantic_guardian",
            "backend.core.ouroboros.governance.tool_executor",
        }
        violations = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                mod = node.module or ""
                if mod in forbidden:
                    violations.append(f"forbidden import: {mod}")
            elif isinstance(node, _ast.Import):
                for alias in node.names:
                    if alias.name in forbidden:
                        violations.append(
                            f"forbidden import: {alias.name}"
                        )
        return tuple(violations)

    def _validate_assert_raises_typed(
        tree: _ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        """``assert_ledger_sovereignty`` MUST raise
        :exc:`LedgerSovereigntyError` (typed). Drift to bare
        ``RuntimeError`` or ``Exception`` would defeat downstream
        ``except LedgerSovereigntyError`` clauses that distinguish
        sovereignty violations from arbitrary errors."""
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.FunctionDef)
                and node.name == "assert_ledger_sovereignty"
            ):
                raised_types = set()
                for sub in _ast.walk(node):
                    if isinstance(sub, _ast.Raise) and isinstance(
                        sub.exc, _ast.Call
                    ):
                        if isinstance(sub.exc.func, _ast.Name):
                            raised_types.add(sub.exc.func.id)
                if not raised_types:
                    return (
                        "assert_ledger_sovereignty has no "
                        "Raise statements",
                    )
                if raised_types != {"LedgerSovereigntyError"}:
                    return (
                        "assert_ledger_sovereignty raises "
                        f"{sorted(raised_types)} (expected only "
                        "LedgerSovereigntyError)",
                    )
                return ()
        return ("assert_ledger_sovereignty not found",)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "ledger_sovereignty_master_default_false"
            ),
            target_file=_TARGET_FILE,
            description=(
                "§33.1 substrate canonical shape — master flag "
                "default-FALSE. Drift to 'true' would silently "
                "flip the gate on for every operator before "
                "graduation."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "ledger_sovereignty_authority_asymmetry"
            ),
            target_file=_TARGET_FILE,
            description=(
                "Substrate purity — sovereignty MUST NOT import "
                "auto_committer / orchestrator / worktree_manager "
                "/ iron_gate / policy / change_engine / etc. "
                "Dependency direction is one-way: consumers "
                "compose substrate, never vice versa."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "ledger_sovereignty_assert_raises_typed"
            ),
            target_file=_TARGET_FILE,
            description=(
                "Structural assertion MUST raise typed "
                "LedgerSovereigntyError — drift to bare "
                "RuntimeError / Exception would defeat the "
                "downstream `except LedgerSovereigntyError` "
                "branch in AutoCommitter that distinguishes "
                "sovereignty violations from arbitrary failures."
            ),
            validate=_validate_assert_raises_typed,
        ),
    ]


__all__ = [
    "LEDGER_SOVEREIGNTY_SCHEMA_VERSION",
    "LedgerSovereigntyError",
    "OwnershipRecord",
    "assert_ledger_sovereignty",
    "is_owned",
    "mark_owned",
    "marker_path",
    "master_enabled",
    "read_ownership",
    "register_shipped_invariants",
]
