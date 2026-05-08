"""Phase 4 (A6) — L3 merge-conflict audit recorder.

Operator binding 2026-05-07 (verbatim — non-negotiable):

  "git_conflict_handler behind worktree / subagent path
   only; audit trail to existing recorder patterns
   (cross_op_semantic_recorder style) only if you already
   have a flock'd sink — no new parallel ledger without
   §33.4 discipline."

L3's :class:`MergeCoordinator` (in
``saga/merge_coordinator.py``) currently raises
``RuntimeError`` on 3 conflict shapes — operators see a
crash but no auditable forensic record. This module ships
the **minimum viable audit recorder** that captures the
conflict to a §33.4 flock'd JSONL ledger BEFORE the
RuntimeError raises, so post-incident analysis has data
instead of just a stack trace.

## Lift discipline (operator binding "lift only the pieces
   you need")

What this module **does NOT** do:

  * **No auto-resolution** — does NOT apply ours/theirs/
    union strategies. Conflict resolution is operator
    decision; the substrate produces forensics, not action.
    AST-pinned via ``merge_conflict_audit_no_auto_resolution``.
  * **No worktree mutation** — does NOT call ``git``,
    ``subprocess``, ``shutil``, or filesystem-mutation
    primitives. Pure-stdlib audit recorder. AST-pinned via
    ``merge_conflict_audit_no_worktree_mutation``.
  * **No replacement of MergeCoordinator failure
    semantics** — RuntimeError still raises after the audit
    record persists. Operator binding "L3 silent failure"
    closes via observability, not by changing failure
    classes.
  * **No new parallel ledger** — composes canonical
    :func:`cross_process_jsonl.flock_append_line` (§33.4
    pattern shared with Move 6.5 Slice 4 + Phase 3 A1/A2/A3
    + cross_op_semantic_recorder). AST-pinned via
    ``merge_conflict_audit_composes_canonical_jsonl``.
  * **No coding_council imports** — Phase 0 cross-kingdom
    boundary holds (covered automatically by
    ``governance_no_coding_council_imports`` AST pin).

What this module **DOES** ship:

  1. **Closed 3-value :class:`MergeConflictKind`** —
     mirrors the 3 RuntimeError branches in MergeCoordinator
     (OWNED_PATH / DUPLICATE_FILE / DUPLICATE_NEW_CONTENT).
     AST-pinned.
  2. **Frozen §33.5 :class:`MergeConflictRecord`** —
     audit row persisted to JSONL; carries graph_id, repo,
     barrier_id, conflict_units, detail message, ts_unix.
  3. **Pure :func:`record_merge_conflict`** —
     caller-invoked from MergeCoordinator's pre-raise path.
     NEVER raises (audit failure cannot prevent the
     RuntimeError from raising; that's the canonical
     escalation path).
  4. **Read API** :func:`read_recent_records` — for
     post-incident inspection + Slice 5+ observability
     extensions.

## Authority asymmetry

No orchestrator / iron_gate / providers / candidate_generator
/ change_engine / semantic_guardian / plan_generator /
urgency_router / direction_inferrer / policy imports.
Pure substrate. AST-pinned.

## Master flag

``JARVIS_MERGE_CONFLICT_AUDIT_ENABLED`` default-FALSE per
§33.1. When OFF, :func:`record_merge_conflict` is a no-op
(zero filesystem touch). MergeCoordinator behavior remains
byte-identical pre-Phase-4 — RuntimeError still raises;
just no audit row. Operator opts in once the canonical
ledger surface graduates.

## NEVER raises

Audit failure CANNOT prevent the canonical RuntimeError
from raising — that's the existing operator-escalation
path. Every code path defensive (record build / JSONL
persistence failures swallowed). The audit recorder is a
forensic surface; it cannot itself become a failure mode.
"""
from __future__ import annotations

import enum
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any, Dict, FrozenSet, Mapping, Optional, Tuple,
)


logger = logging.getLogger(
    "Ouroboros.MergeConflictAudit",
)


MERGE_CONFLICT_AUDIT_SCHEMA_VERSION: str = (
    "merge_conflict_audit.1"
)


_TRUTHY: FrozenSet[str] = frozenset(
    {"1", "true", "yes", "on"},
)


_DEFAULT_LEDGER_FILENAME: str = (
    "merge_conflict_audit.jsonl"
)
_DEFAULT_LEDGER_SIZE_CAP_BYTES: int = 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# Closed 3-value taxonomy — mirrors MergeCoordinator's 3 raise sites
# ---------------------------------------------------------------------------


class MergeConflictKind(str, enum.Enum):
    """Closed 3-value taxonomy of L3 patch-merge conflict
    shapes. Each value mirrors one of the 3 RuntimeError
    branches in :class:`MergeCoordinator`.

    AST-pinned. Adding a new conflict shape requires:
      1. Updating MergeCoordinator with a new raise site
      2. Adding the enum value here
      3. Updating the taxonomy AST pin's required set

    ``OWNED_PATH``           — Two units in the same
                               (repo, barrier) batch claim
                               the same path in their
                               ``effective_owned_paths``.
                               Mirrors
                               ``merge_coordinator:owned_path_conflict``.
    ``DUPLICATE_FILE``       — Two units patch the same
                               file path during merge.
                               Mirrors
                               ``merge_coordinator:duplicate_file_path``.
    ``DUPLICATE_NEW_CONTENT``— Two units create the same
                               new file path during merge.
                               Mirrors
                               ``merge_coordinator:duplicate_new_content``.
    """

    OWNED_PATH = "owned_path"
    DUPLICATE_FILE = "duplicate_file"
    DUPLICATE_NEW_CONTENT = "duplicate_new_content"


# ---------------------------------------------------------------------------
# Master flag + tunable knobs
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_MERGE_CONFLICT_AUDIT_ENABLED`` master switch.
    Default-FALSE per §33.1: when OFF,
    :func:`record_merge_conflict` is a no-op (zero filesystem
    touch). NEVER raises."""
    raw = os.environ.get(
        "JARVIS_MERGE_CONFLICT_AUDIT_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False
    return raw in _TRUTHY


def ledger_path() -> Path:
    """Resolve the canonical ledger path. NEVER raises."""
    raw = os.environ.get(
        "JARVIS_MERGE_CONFLICT_AUDIT_LEDGER_PATH", "",
    ).strip()
    if raw:
        return Path(raw)
    return Path(".jarvis") / _DEFAULT_LEDGER_FILENAME


# ---------------------------------------------------------------------------
# Frozen artifact (§33.5 versioned)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergeConflictRecord:
    """One audit row. §33.5 versioned-artifact contract.

    Captures the L3 patch-merge conflict's essentials:
    which units conflicted on which (repo, barrier_id) at
    what time. The ``detail`` field carries the original
    MergeCoordinator RuntimeError message verbatim so post-
    incident analysis has the exact escalation context."""

    kind: str
    graph_id: str
    repo: str
    barrier_id: str
    conflict_units: Tuple[str, ...]
    paths: Tuple[str, ...] = field(default_factory=tuple)
    detail: str = ""
    ts_unix: float = 0.0
    schema_version: str = field(
        default=MERGE_CONFLICT_AUDIT_SCHEMA_VERSION,
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": str(self.kind),
            "graph_id": str(self.graph_id),
            "repo": str(self.repo),
            "barrier_id": str(self.barrier_id),
            "conflict_units": list(self.conflict_units),
            "paths": list(self.paths),
            "detail": str(self.detail)[:1024],
            "ts_unix": float(self.ts_unix),
            "schema_version": str(self.schema_version),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any],
    ) -> Optional["MergeConflictRecord"]:
        try:
            schema = payload.get("schema_version")
            if schema != (
                MERGE_CONFLICT_AUDIT_SCHEMA_VERSION
            ):
                return None
            return cls(
                kind=str(payload["kind"]),
                graph_id=str(
                    payload.get("graph_id", ""),
                ),
                repo=str(payload.get("repo", "")),
                barrier_id=str(
                    payload.get("barrier_id", ""),
                ),
                conflict_units=tuple(
                    str(u) for u in (
                        payload.get("conflict_units", [])
                        or []
                    )
                ),
                paths=tuple(
                    str(p) for p in (
                        payload.get("paths", []) or []
                    )
                ),
                detail=str(payload.get("detail", "")),
                ts_unix=float(payload["ts_unix"]),
            )
        except (KeyError, TypeError, ValueError):
            return None


# ---------------------------------------------------------------------------
# Public recorder — caller-invoked from MergeCoordinator pre-raise path
# ---------------------------------------------------------------------------


def record_merge_conflict(
    *,
    kind: MergeConflictKind,
    graph_id: str,
    repo: str,
    barrier_id: str,
    conflict_units: Tuple[str, ...] = (),
    paths: Tuple[str, ...] = (),
    detail: str = "",
    ledger_path_override: Optional[Path] = None,
) -> Optional[MergeConflictRecord]:
    """Audit one L3 patch-merge conflict. Composes:
      1. Build §33.5 versioned record.
      2. Append flock'd JSONL row via §33.4
         :func:`cross_process_jsonl.flock_append_line`.

    Returns the record on success, None when:
      * Master flag off (no-op)
      * Record build / persistence failed defensively

    NEVER raises. The audit recorder MUST NOT block or
    delay the canonical RuntimeError escalation path —
    operator binding "no replacement of failure semantics"."""
    if not master_enabled():
        return None
    try:
        record = MergeConflictRecord(
            kind=(
                kind.value
                if isinstance(kind, MergeConflictKind)
                else str(kind)
            ),
            graph_id=str(graph_id or ""),
            repo=str(repo or ""),
            barrier_id=str(barrier_id or ""),
            conflict_units=tuple(
                str(u) for u in (conflict_units or ())
            ),
            paths=tuple(
                str(p) for p in (paths or ())
            ),
            detail=str(detail or "")[:1024],
            ts_unix=time.time(),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[MergeConflictAudit] record build failed",
            exc_info=True,
        )
        return None
    persisted = _flock_persist(
        record=record,
        target=(
            ledger_path_override
            if ledger_path_override is not None
            else ledger_path()
        ),
    )
    return record if persisted else None


def _flock_persist(
    *,
    record: MergeConflictRecord,
    target: Path,
) -> bool:
    """Append one row via canonical
    :func:`cross_process_jsonl.flock_append_line` (§33.4
    pattern). NEVER raises."""
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_append_line,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[MergeConflictAudit] flock primitive "
            "unavailable: %s", exc,
        )
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001 — defensive
        return False
    try:
        import json
        line = json.dumps(
            record.to_dict(), ensure_ascii=True,
            separators=(",", ":"),
        )
    except Exception:  # noqa: BLE001 — defensive
        return False
    try:
        return bool(flock_append_line(target, line))
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[MergeConflictAudit] flock_append_line "
            "raised: %s", exc,
        )
        return False


# ---------------------------------------------------------------------------
# Read API — for post-incident inspection + Slice 5+ extensions
# ---------------------------------------------------------------------------


def read_recent_records(
    *,
    limit: int = 50,
    path: Optional[Path] = None,
) -> Tuple[MergeConflictRecord, ...]:
    """Read recent audit records via canonical
    :func:`cross_process_jsonl.flock_critical_section`.
    NEVER raises; empty tuple on missing file / I/O /
    schema-mismatch."""
    target = path if path is not None else ledger_path()
    if not target.exists():
        return ()
    try:
        size = target.stat().st_size
    except OSError:
        return ()
    if size > _DEFAULT_LEDGER_SIZE_CAP_BYTES:
        return ()
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_critical_section,
        )
    except Exception:  # noqa: BLE001
        return ()
    rows_raw: list = []
    try:
        with flock_critical_section(target) as acquired:
            if not acquired:
                return ()
            try:
                with target.open(
                    "r", encoding="utf-8",
                ) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            rows_raw.append(line)
            except OSError:
                return ()
    except Exception:  # noqa: BLE001 — defensive
        return ()
    if limit > 0 and len(rows_raw) > limit:
        rows_raw = rows_raw[-limit:]
    out: list = []
    import json as _json
    for raw in rows_raw:
        try:
            payload = _json.loads(raw)
        except (TypeError, ValueError):
            continue
        rec = MergeConflictRecord.from_dict(payload)
        if rec is not None:
            out.append(rec)
    return tuple(out)


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> None:
    """Auto-discovered. Seeds 2 flags."""
    try:
        registry.register(
            name="JARVIS_MERGE_CONFLICT_AUDIT_ENABLED",
            type_="bool",
            default="false",
            description=(
                "Master switch for Phase 4 A6 — L3 "
                "patch-merge conflict audit recorder. "
                "Default-FALSE per §33.1; when off, "
                "record_merge_conflict is a no-op + "
                "MergeCoordinator behavior is byte-"
                "identical pre-Phase-4."
            ),
            category="Observability",
            posture_relevance="RELEVANT",
            source_file=(
                "backend/core/ouroboros/governance/saga/"
                "merge_conflict_audit.py"
            ),
            example=(
                "JARVIS_MERGE_CONFLICT_AUDIT_ENABLED=true"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[MergeConflictAudit] master-flag seeding "
            "failed (non-fatal)", exc_info=True,
        )
    try:
        registry.register(
            name=(
                "JARVIS_MERGE_CONFLICT_AUDIT_LEDGER_PATH"
            ),
            type_="path",
            default=str(
                Path(".jarvis") / _DEFAULT_LEDGER_FILENAME
            ),
            description=(
                "JSONL ledger path for Phase 4 A6 audit "
                "records (§33.4 flock'd persistence)."
            ),
            category="Observability",
            posture_relevance="IGNORED",
            source_file=(
                "backend/core/ouroboros/governance/saga/"
                "merge_conflict_audit.py"
            ),
            example=(
                "JARVIS_MERGE_CONFLICT_AUDIT_LEDGER_PATH="
                ".jarvis/merge_audit.jsonl"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[MergeConflictAudit] ledger-path seeding "
            "failed (non-fatal)", exc_info=True,
        )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``merge_conflict_audit_master_default_false``
      2. ``merge_conflict_audit_authority_asymmetry``
      3. ``merge_conflict_audit_taxonomy_3_values`` —
         closed enum (mirrors 3 RuntimeError branches in
         MergeCoordinator).
      4. ``merge_conflict_audit_composes_canonical_jsonl``
         — §33.4 flock primitives only.
      5. ``merge_conflict_audit_no_auto_resolution`` —
         module MUST NOT call any function whose name
         suggests resolution (resolve_*, apply_resolution,
         merge_files, write_*).
      6. ``merge_conflict_audit_no_worktree_mutation`` —
         module MUST NOT import ``subprocess``,
         ``shutil``, or call ``os.remove`` / ``Path.unlink``
         / ``Path.write_*``. Pure-stdlib audit recorder.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/saga/"
        "merge_conflict_audit.py"
    )

    def _validate_master_default_false(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        target_func = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                target_func = node
                break
        if target_func is None:
            violations.append("master_enabled() missing")
            return tuple(violations)
        empty_returns_false = False
        for sub in ast.walk(target_func):
            if not isinstance(sub, ast.If):
                continue
            for cmp_node in ast.walk(sub.test):
                if not isinstance(cmp_node, ast.Compare):
                    continue
                if not cmp_node.ops or not isinstance(
                    cmp_node.ops[0], ast.Eq,
                ):
                    continue
                operand_empty = False
                for operand in (
                    cmp_node.left, *cmp_node.comparators,
                ):
                    if (
                        isinstance(operand, ast.Constant)
                        and operand.value == ""
                    ):
                        operand_empty = True
                        break
                if not operand_empty:
                    continue
                for stmt in sub.body:
                    if isinstance(stmt, ast.Return) and (
                        isinstance(stmt.value, ast.Constant)
                        and stmt.value.value is False
                    ):
                        empty_returns_false = True
                        break
                if empty_returns_false:
                    break
            if empty_returns_false:
                break
        if not empty_returns_false:
            violations.append(
                "master_enabled() MUST return False on "
                "empty env-var string per §33.1"
            )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden_substring = (
            "iron_gate", "providers", "candidate_generator",
            "urgency_router", "change_engine",
            "semantic_guardian", "plan_generator",
            "direction_inferrer",
        )
        forbidden_exact = {"orchestrator", "policy"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                segments = module.split(".")
                if any(
                    "merge_conflict_audit" in s
                    for s in segments
                ):
                    continue
                for seg in segments:
                    if seg in forbidden_exact:
                        violations.append(
                            f"merge_conflict_audit.py "
                            f"MUST NOT import {module!r} "
                            f"(forbidden segment {seg!r})"
                        )
                        break
                for f in forbidden_substring:
                    if any(f in seg for seg in segments):
                        violations.append(
                            f"merge_conflict_audit.py "
                            f"MUST NOT import {module!r} "
                            f"(forbidden token {f!r})"
                        )
                        break
        return tuple(violations)

    def _validate_taxonomy(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {
            "OWNED_PATH",
            "DUPLICATE_FILE",
            "DUPLICATE_NEW_CONTENT",
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "MergeConflictKind"
            ):
                seen: set = set()
                for stmt in node.body:
                    if isinstance(stmt, ast.Assign):
                        for tgt in stmt.targets:
                            if isinstance(tgt, ast.Name):
                                seen.add(tgt.id)
                missing = required - seen
                extra = seen - required
                if missing:
                    violations.append(
                        f"MergeConflictKind missing "
                        f"{sorted(missing)}"
                    )
                if extra:
                    violations.append(
                        f"MergeConflictKind has extra "
                        f"{sorted(extra)} — closed at 3 "
                        f"values mirroring MergeCoordinator's "
                        f"3 RuntimeError branches"
                    )
                return tuple(violations)
        violations.append(
            "MergeConflictKind class missing"
        )
        return tuple(violations)

    def _validate_composes_canonical_jsonl(
        tree: "ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        for fn_name in (
            "flock_append_line",
            "flock_critical_section",
        ):
            if fn_name not in source:
                violations.append(
                    f"composes-canonical-jsonl: source "
                    f"MUST use cross_process_jsonl."
                    f"{fn_name} (§33.4 pattern)"
                )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Name)
                and func.id == "open"
            ):
                mode_arg: Optional[str] = None
                if len(node.args) >= 2 and isinstance(
                    node.args[1], ast.Constant,
                ):
                    mode_arg = str(node.args[1].value)
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(
                        kw.value, ast.Constant,
                    ):
                        mode_arg = str(kw.value.value)
                if mode_arg and mode_arg.startswith("a"):
                    violations.append(
                        f"composes-canonical-jsonl: raw "
                        f"open(..., {mode_arg!r}) "
                        f"forbidden — use flock_append_line "
                        f"(line {node.lineno})"
                    )
        return tuple(violations)

    def _validate_no_auto_resolution(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Module MUST NOT define or call any
        resolution-shaped function. Operator binding
        'no auto-resolution; substrate produces forensics
        not action'."""
        violations: list = []
        forbidden_name_substrings = (
            "resolve_conflict", "apply_resolution",
            "merge_files", "auto_resolve",
            "resolve_ours", "resolve_theirs",
            "resolve_union",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                lower = node.name.lower()
                for sub in forbidden_name_substrings:
                    if sub in lower:
                        violations.append(
                            f"no-auto-resolution: function "
                            f"{node.name!r} forbidden "
                            f"(line {node.lineno}) — "
                            f"substrate is forensics-only"
                        )
                        break
            if isinstance(node, ast.Call):
                func = node.func
                fname = (
                    func.id
                    if isinstance(func, ast.Name)
                    else (
                        func.attr
                        if isinstance(func, ast.Attribute)
                        else None
                    )
                )
                if fname:
                    lower = fname.lower()
                    for sub in forbidden_name_substrings:
                        if sub in lower:
                            violations.append(
                                f"no-auto-resolution: call "
                                f"to {fname!r} forbidden "
                                f"(line {node.lineno})"
                            )
                            break
        return tuple(violations)

    def _validate_no_worktree_mutation(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """Module MUST NOT import ``subprocess``,
        ``shutil``, ``os.remove``, etc. Pure-stdlib audit
        recorder. Operator binding 'no worktree
        mutation'."""
        violations: list = []
        forbidden_imports = {
            "subprocess", "shutil", "asyncio.subprocess",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name or ""
                    if name in forbidden_imports:
                        violations.append(
                            f"no-worktree-mutation: import "
                            f"{name!r} forbidden — "
                            f"audit recorder is read-only"
                        )
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module in forbidden_imports:
                    violations.append(
                        f"no-worktree-mutation: from "
                        f"{module!r} forbidden"
                    )
            # Forbid Path.write_text / Path.unlink /
            # Path.write_bytes / os.remove calls.
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    forbidden_methods = {
                        "write_text", "write_bytes",
                        "unlink", "rmdir", "rmtree",
                        "remove",
                    }
                    if func.attr in forbidden_methods:
                        # Allow .mkdir() — needed for parent
                        # dir creation before flock_append.
                        # Specifically forbid mutation methods.
                        violations.append(
                            f"no-worktree-mutation: call "
                            f"to .{func.attr}() forbidden "
                            f"(line {node.lineno})"
                        )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "merge_conflict_audit_"
                "master_default_false"
            ),
            target_file=target,
            description=(
                "Phase 4 A6 — §33.1 master flag stays "
                "default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "merge_conflict_audit_"
                "authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Phase 4 A6 — substrate purity: no "
                "orchestrator-tier imports."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "merge_conflict_audit_taxonomy_3_values"
            ),
            target_file=target,
            description=(
                "Phase 4 A6 — MergeConflictKind closed at "
                "3 values mirroring MergeCoordinator's 3 "
                "RuntimeError branches."
            ),
            validate=_validate_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "merge_conflict_audit_"
                "composes_canonical_jsonl"
            ),
            target_file=target,
            description=(
                "Phase 4 A6 — §33.4 Per-Cluster Flock'd "
                "JSONL: persistence composes "
                "flock_append_line + flock_critical_section."
            ),
            validate=_validate_composes_canonical_jsonl,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "merge_conflict_audit_no_auto_resolution"
            ),
            target_file=target,
            description=(
                "Phase 4 A6 — operator binding 'no auto-"
                "resolution; substrate produces forensics "
                "not action'. No resolve_* / apply_resolution "
                "/ merge_files / auto_resolve / *_ours / "
                "*_theirs / *_union calls."
            ),
            validate=_validate_no_auto_resolution,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "merge_conflict_audit_no_worktree_mutation"
            ),
            target_file=target,
            description=(
                "Phase 4 A6 — pure-stdlib audit recorder. "
                "No subprocess / shutil imports; no "
                ".write_*() / .unlink() / .rmdir() / .remove() "
                "calls."
            ),
            validate=_validate_no_worktree_mutation,
        ),
    ]


__all__ = [
    "MERGE_CONFLICT_AUDIT_SCHEMA_VERSION",
    "MergeConflictKind",
    "MergeConflictRecord",
    "ledger_path",
    "master_enabled",
    "read_recent_records",
    "record_merge_conflict",
    "register_flags",
    "register_shipped_invariants",
]
