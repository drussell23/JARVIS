"""Phase 9 cadence health — pre-invocation capability probe +
append-only health ledger.

Closes the EPERM-before-Python failure mode: cron fires, macOS
TCC denies file access before the harness imports anything,
:file:`.jarvis/live_fire_graduation_history.jsonl` never gets
appended → invisible failure. This module ships the upstream
witness that records ``preflight_failure`` rows BEFORE the
harness runs, so :mod:`cadence_status` (Slice 3) can answer
"did the schedule fire and die before Python?"

Operator binding 2026-05-06 (verbatim):

  > "Pre-invocation capability probe ... persists a structured
  > non-soak row or append-only cadence health JSONL with
  > failure_class=os_policy / errno, so status / /graduate /
  > future observability can answer 'did the schedule fire and
  > die before Python?'"

This module ships:

  * :class:`CadenceHealthRow` — frozen §33.5 versioned artifact
    (schema_version + symmetric to_dict / from_dict). Captures
    ``kind`` (preflight_ok | preflight_failure), ``failure_class``
    (ok | os_policy | missing_path | unexpected), ``errno`` /
    ``errno_name`` (POSIX errno when applicable), ``subject``
    (what was being probed: repo_root | jarvis_dir |
    log_dir_write | manifest_read), ``detail`` (bounded
    string), ``cadence_kind`` (cron | launchd | adhoc).
  * :func:`run_preflight` — pure-function probe with caller-
    injected paths. Returns a :class:`CadenceHealthRow` ready
    to record. NEVER raises.
  * :func:`record_health_row` — append via §33.4
    ``flock_critical_section`` so concurrent fires (rare but
    possible) cannot interleave partial JSON.
  * :func:`read_recent` / :func:`most_recent_preflight_ok_epoch`
    / :func:`most_recent_preflight_failure` — read API for
    Slice 3 overdue detector + future operator surfaces.

Architectural locks:

  * **Authority asymmetry** — pure stdlib substrate; no
    orchestrator / iron_gate / policy / providers imports
    (AST-pinned).
  * **Composes §33.4** — append uses
    ``cross_process_jsonl.flock_append_line`` (no parallel
    flock impl) (AST-pinned).
  * **Versioned-artifact-contract (§33.5)** — row carries
    explicit ``schema_version``.
  * **NEVER raises** across all public surfaces.
"""
from __future__ import annotations

import errno as _errno_mod
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


CADENCE_HEALTH_SCHEMA_VERSION: str = "cadence_health.1"


# Bounded ledger reads to defend against pathological growth.
_MAX_LEDGER_FILE_BYTES: int = 4 * 1024 * 1024
_MAX_RECORDS_LOADED: int = 10_000
_DETAIL_MAX_CHARS: int = 256


# ---------------------------------------------------------------------------
# Closed taxonomies — bytes-pinned via AST regression
# ---------------------------------------------------------------------------


# Probe outcome kinds. Bytes-pinned.
KIND_PREFLIGHT_OK: str = "preflight_ok"
KIND_PREFLIGHT_FAILURE: str = "preflight_failure"


# Failure classes. Bytes-pinned.
FAILURE_CLASS_OK: str = "ok"
FAILURE_CLASS_OS_POLICY: str = "os_policy"  # EPERM / EACCES — TCC, sandbox, perm bits
FAILURE_CLASS_MISSING_PATH: str = "missing_path"  # ENOENT
FAILURE_CLASS_UNEXPECTED: str = "unexpected"  # other OSError + non-OSError


# Subjects. Bytes-pinned.
SUBJECT_REPO_ROOT: str = "repo_root"
SUBJECT_JARVIS_DIR: str = "jarvis_dir"
SUBJECT_LOG_DIR_WRITE: str = "log_dir_write"
SUBJECT_MANIFEST_READ: str = "manifest_read"
SUBJECT_NONE: str = ""


# ---------------------------------------------------------------------------
# Health-ledger path resolution
# ---------------------------------------------------------------------------


def health_path() -> Path:
    """Canonical path. Env-overridable for tests:
    ``JARVIS_CADENCE_HEALTH_PATH``."""
    raw = os.environ.get("JARVIS_CADENCE_HEALTH_PATH", "")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "cadence_health.jsonl"


# ---------------------------------------------------------------------------
# Versioned health-row artifact (§33.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CadenceHealthRow:
    """One cadence health observation — frozen §33.5 versioned
    artifact."""

    schema_version: str
    ts_iso: str
    ts_epoch: float
    kind: str  # KIND_*
    failure_class: str  # FAILURE_CLASS_*
    errno: Optional[int]
    errno_name: Optional[str]
    subject: str  # SUBJECT_*
    detail: str  # ≤256 chars
    cadence_kind: str  # cron | launchd | adhoc

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "ts_iso": self.ts_iso,
            "ts_epoch": float(self.ts_epoch),
            "kind": self.kind,
            "failure_class": self.failure_class,
            "errno": (
                int(self.errno) if self.errno is not None else None
            ),
            "errno_name": (
                str(self.errno_name)
                if self.errno_name is not None else None
            ),
            "subject": self.subject,
            "detail": self.detail[:_DETAIL_MAX_CHARS],
            "cadence_kind": self.cadence_kind,
        }

    @classmethod
    def from_dict(
        cls, payload: Dict[str, Any],
    ) -> Optional["CadenceHealthRow"]:
        try:
            if not isinstance(payload, dict):
                return None
            kind = str(payload.get("kind") or "")
            if kind not in (
                KIND_PREFLIGHT_OK, KIND_PREFLIGHT_FAILURE,
            ):
                return None
            errno_raw = payload.get("errno")
            errno_int = (
                int(errno_raw) if errno_raw is not None else None
            )
            return cls(
                schema_version=str(
                    payload.get("schema_version")
                    or CADENCE_HEALTH_SCHEMA_VERSION,
                ),
                ts_iso=str(payload.get("ts_iso") or ""),
                ts_epoch=float(payload.get("ts_epoch") or 0.0),
                kind=kind,
                failure_class=str(
                    payload.get("failure_class") or "",
                ),
                errno=errno_int,
                errno_name=(
                    str(payload.get("errno_name"))
                    if payload.get("errno_name") is not None
                    else None
                ),
                subject=str(payload.get("subject") or ""),
                detail=str(payload.get("detail") or "")[
                    :_DETAIL_MAX_CHARS
                ],
                cadence_kind=str(
                    payload.get("cadence_kind") or "",
                ),
            )
        except Exception:  # noqa: BLE001 — defensive
            return None


# ---------------------------------------------------------------------------
# Errno classification — pure function
# ---------------------------------------------------------------------------


_OS_POLICY_ERRNOS = frozenset({
    _errno_mod.EPERM,
    _errno_mod.EACCES,
})


def classify_errno(err: Optional[int]) -> str:
    """Map a POSIX errno to a closed failure_class taxonomy.
    Pure function. NEVER raises."""
    try:
        if err is None:
            return FAILURE_CLASS_OK
        e = int(err)
    except (TypeError, ValueError):
        return FAILURE_CLASS_UNEXPECTED
    if e == 0:
        return FAILURE_CLASS_OK
    if e in _OS_POLICY_ERRNOS:
        return FAILURE_CLASS_OS_POLICY
    if e == _errno_mod.ENOENT:
        return FAILURE_CLASS_MISSING_PATH
    return FAILURE_CLASS_UNEXPECTED


def errno_name(err: Optional[int]) -> Optional[str]:
    if err is None:
        return None
    try:
        return _errno_mod.errorcode.get(int(err))
    except (TypeError, ValueError, AttributeError):
        return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ",
    )


# ---------------------------------------------------------------------------
# Pure-function preflight probe
# ---------------------------------------------------------------------------


def run_preflight(
    *,
    repo_root: Path,
    jarvis_dir: Path,
    log_dir: Path,
    cadence_kind: str = "adhoc",
    now_epoch: Optional[float] = None,
) -> CadenceHealthRow:
    """Run the capability probe. Pure-function — caller injects
    paths. Returns a :class:`CadenceHealthRow` ready to record.

    Probe steps (first failure wins so the row pinpoints the
    blocker):

      1. ``repo_root.is_dir()`` — read access to repo root.
      2. ``jarvis_dir.is_dir()`` (creating if absent — a
         missing ``.jarvis/`` is recoverable) — read access to
         Phase 9 evidence dir.
      3. Write a tiny probe file in ``log_dir`` then unlink it
         — verifies cron / launchd has write access to the
         logs directory under macOS TCC.

    On any OSError, classify via :func:`classify_errno` and
    return a ``preflight_failure`` row pinpointing the subject.
    On success return a ``preflight_ok`` row with detail
    "all_three_subjects_passed".

    NEVER raises.
    """
    now = float(now_epoch) if now_epoch is not None else time.time()
    iso = _utc_now_iso()
    cadence = (cadence_kind or "adhoc").strip().lower() or "adhoc"
    # Step 1 — repo root readable + dir-shaped.
    try:
        if not repo_root.exists():
            return _failure(
                iso, now, _errno_mod.ENOENT,
                SUBJECT_REPO_ROOT,
                f"repo_root_missing:{repo_root}",
                cadence,
            )
        if not repo_root.is_dir():
            return _failure(
                iso, now, _errno_mod.ENOTDIR,
                SUBJECT_REPO_ROOT,
                f"repo_root_not_dir:{repo_root}",
                cadence,
            )
    except OSError as exc:
        return _failure(
            iso, now, _coerce_errno(exc),
            SUBJECT_REPO_ROOT,
            f"repo_root_oserror:{type(exc).__name__}:{exc}",
            cadence,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return _failure(
            iso, now, None, SUBJECT_REPO_ROOT,
            f"repo_root_unexpected:{type(exc).__name__}:{exc}",
            cadence,
        )
    # Step 2 — .jarvis/ readable + creatable.
    try:
        if not jarvis_dir.exists():
            try:
                jarvis_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return _failure(
                    iso, now, _coerce_errno(exc),
                    SUBJECT_JARVIS_DIR,
                    (
                        f"jarvis_dir_mkdir_failed:"
                        f"{type(exc).__name__}:{exc}"
                    ),
                    cadence,
                )
        if not jarvis_dir.is_dir():
            return _failure(
                iso, now, _errno_mod.ENOTDIR,
                SUBJECT_JARVIS_DIR,
                f"jarvis_dir_not_dir:{jarvis_dir}",
                cadence,
            )
    except OSError as exc:
        return _failure(
            iso, now, _coerce_errno(exc),
            SUBJECT_JARVIS_DIR,
            (
                f"jarvis_dir_oserror:{type(exc).__name__}:"
                f"{exc}"
            ),
            cadence,
        )
    # Step 3 — log_dir writable (probe-touch + unlink).
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        probe_file = log_dir / f".cadence_probe.{int(now)}.tmp"
        probe_file.write_text("probe", encoding="utf-8")
        probe_file.unlink(missing_ok=True)
    except OSError as exc:
        return _failure(
            iso, now, _coerce_errno(exc),
            SUBJECT_LOG_DIR_WRITE,
            (
                f"log_dir_write_oserror:{type(exc).__name__}:"
                f"{exc}"
            ),
            cadence,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return _failure(
            iso, now, None, SUBJECT_LOG_DIR_WRITE,
            (
                f"log_dir_write_unexpected:"
                f"{type(exc).__name__}:{exc}"
            ),
            cadence,
        )
    # All three subjects passed.
    return CadenceHealthRow(
        schema_version=CADENCE_HEALTH_SCHEMA_VERSION,
        ts_iso=iso,
        ts_epoch=now,
        kind=KIND_PREFLIGHT_OK,
        failure_class=FAILURE_CLASS_OK,
        errno=None,
        errno_name=None,
        subject=SUBJECT_NONE,
        detail="all_three_subjects_passed",
        cadence_kind=cadence,
    )


def _coerce_errno(exc: OSError) -> Optional[int]:
    try:
        return int(exc.errno) if exc.errno is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def _failure(
    iso: str,
    epoch: float,
    err: Optional[int],
    subject: str,
    detail: str,
    cadence: str,
) -> CadenceHealthRow:
    return CadenceHealthRow(
        schema_version=CADENCE_HEALTH_SCHEMA_VERSION,
        ts_iso=iso,
        ts_epoch=epoch,
        kind=KIND_PREFLIGHT_FAILURE,
        failure_class=classify_errno(err),
        errno=err,
        errno_name=errno_name(err),
        subject=subject,
        detail=detail[:_DETAIL_MAX_CHARS],
        cadence_kind=cadence,
    )


# ---------------------------------------------------------------------------
# Append (§33.4 flock-protected) + read API
# ---------------------------------------------------------------------------


def record_health_row(
    row: CadenceHealthRow,
    *,
    path: Optional[Path] = None,
) -> Tuple[bool, str]:
    """Append one health row via §33.4 canonical flock primitive.
    NEVER raises. Returns ``(ok, detail)``."""
    try:
        target = Path(path) if path is not None else health_path()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return (False, f"mkdir_failed:{exc}")
        try:
            line = json.dumps(row.to_dict(), separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            return (False, f"serialize_failed:{exc}")
        try:
            from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
                flock_append_line,
            )
        except ImportError:
            # Substrate unavailable (rollback branch) — fall
            # through to direct append. Acceptable because this
            # module is best-effort by design.
            try:
                with target.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
                return (True, "ok_legacy_path")
            except OSError as exc:
                return (False, f"legacy_write_failed:{exc}")
        ok = flock_append_line(target, line)
        return (
            (True, "ok") if ok
            else (False, "flock_append_failed")
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return (False, f"unexpected:{exc}")


def read_recent(
    *,
    path: Optional[Path] = None,
    limit: Optional[int] = None,
) -> List[CadenceHealthRow]:
    """Defensive read. Returns rows in file order (oldest →
    newest). ``limit`` clamps to the most-recent N. NEVER
    raises."""
    target = Path(path) if path is not None else health_path()
    try:
        if not target.exists():
            return []
        try:
            size = target.stat().st_size
        except OSError:
            return []
        if size > _MAX_LEDGER_FILE_BYTES:
            logger.warning(
                "[cadence_health] ledger %s exceeds max bytes "
                "(%d>%d) — returning empty",
                target, size, _MAX_LEDGER_FILE_BYTES,
            )
            return []
        try:
            text = target.read_text(encoding="utf-8")
        except OSError:
            return []
        out: List[CadenceHealthRow] = []
        for line in text.splitlines():
            if len(out) >= _MAX_RECORDS_LOADED:
                break
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            row = CadenceHealthRow.from_dict(payload)
            if row is not None:
                out.append(row)
        if limit is not None:
            try:
                lim = max(1, int(limit))
                if lim < len(out):
                    return out[-lim:]
            except (TypeError, ValueError):
                return out
        return out
    except Exception:  # noqa: BLE001 — defensive
        return []


def most_recent_preflight_ok_epoch(
    *, path: Optional[Path] = None,
) -> Optional[float]:
    """Read API for Slice 3. Returns the epoch of the most
    recent ``preflight_ok`` row, or None if none exist."""
    rows = read_recent(path=path)
    for row in reversed(rows):
        if row.kind == KIND_PREFLIGHT_OK:
            return row.ts_epoch
    return None


def most_recent_preflight_failure(
    *, path: Optional[Path] = None,
) -> Optional[CadenceHealthRow]:
    """Read API for Slice 3. Returns the most recent
    ``preflight_failure`` row, or None if none exist."""
    rows = read_recent(path=path)
    for row in reversed(rows):
        if row.kind == KIND_PREFLIGHT_FAILURE:
            return row
    return None


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``cadence_health_authority_asymmetry`` — substrate
         purity.
      2. ``cadence_health_composes_canonical_flock`` — append
         path imports flock_append_line from cross_process_jsonl
         (§33.4); no parallel locking.
      3. ``cadence_health_versioned_artifact_compliance`` —
         CadenceHealthRow §33.5 contract.
      4. ``cadence_health_kind_taxonomy_closed`` — the 2 kind
         + 4 failure_class + 5 subject constants are bytes-
         pinned at module level.
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
        "cadence_health.py"
    )

    _EXPECTED_KIND_VALUES = {"preflight_ok", "preflight_failure"}
    _EXPECTED_FAILURE_CLASS_VALUES = {
        "ok", "os_policy", "missing_path", "unexpected",
    }
    _EXPECTED_SUBJECT_VALUES = {
        "repo_root", "jarvis_dir", "log_dir_write",
        "manifest_read", "",
    }

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
                            f"cadence_health.py MUST NOT "
                            f"import {module!r}"
                        )
        return tuple(violations)

    def _validate_composes_canonical_flock(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        found_canonical = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if (
                    node.module
                    and "cross_process_jsonl" in node.module
                ):
                    for alias in node.names:
                        if alias.name == "flock_append_line":
                            found_canonical = True
        if not found_canonical:
            violations.append(
                "cadence_health.py MUST compose "
                "cross_process_jsonl.flock_append_line "
                "(§33.4); no parallel flock impl"
            )
        # Forbid raw fcntl import (would imply parallel impl).
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "fcntl":
                        violations.append(
                            "cadence_health.py MUST NOT "
                            "import fcntl directly — compose "
                            "cross_process_jsonl primitive"
                        )
        return tuple(violations)

    def _validate_versioned_artifact_compliance(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "CadenceHealthRow"
            ):
                method_names = {
                    sub.name
                    for sub in node.body
                    if isinstance(
                        sub, (ast.FunctionDef, ast.AsyncFunctionDef),
                    )
                }
                field_names = {
                    sub.target.id
                    for sub in node.body
                    if (
                        isinstance(sub, ast.AnnAssign)
                        and isinstance(sub.target, ast.Name)
                    )
                }
                if "schema_version" not in field_names:
                    violations.append(
                        "CadenceHealthRow MUST declare "
                        "schema_version (§33.5)"
                    )
                if "to_dict" not in method_names:
                    violations.append(
                        "CadenceHealthRow MUST expose to_dict "
                        "(§33.5)"
                    )
                if "from_dict" not in method_names:
                    violations.append(
                        "CadenceHealthRow MUST expose from_dict "
                        "(§33.5)"
                    )
                return tuple(violations)
        violations.append(
            "CadenceHealthRow class definition missing"
        )
        return tuple(violations)

    def _validate_taxonomy_closed(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        # Module-level constants: KIND_* + FAILURE_CLASS_* + SUBJECT_*
        kinds: set = set()
        classes: set = set()
        subjects: set = set()
        for node in tree.body:
            if isinstance(node, ast.AnnAssign):
                if (
                    isinstance(node.target, ast.Name)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    name = node.target.id
                    val = node.value.value
                    if name.startswith("KIND_"):
                        kinds.add(val)
                    elif name.startswith("FAILURE_CLASS_"):
                        classes.add(val)
                    elif name.startswith("SUBJECT_"):
                        subjects.add(val)
        if kinds != _EXPECTED_KIND_VALUES:
            violations.append(
                f"KIND_* drift: {sorted(kinds)} != "
                f"{sorted(_EXPECTED_KIND_VALUES)}"
            )
        if classes != _EXPECTED_FAILURE_CLASS_VALUES:
            violations.append(
                f"FAILURE_CLASS_* drift: {sorted(classes)} "
                f"!= {sorted(_EXPECTED_FAILURE_CLASS_VALUES)}"
            )
        if subjects != _EXPECTED_SUBJECT_VALUES:
            violations.append(
                f"SUBJECT_* drift: {sorted(subjects)} != "
                f"{sorted(_EXPECTED_SUBJECT_VALUES)}"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "cadence_health_authority_asymmetry"
            ),
            target_file=target,
            description="Cadence Slice 2 — substrate purity.",
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cadence_health_composes_canonical_flock"
            ),
            target_file=target,
            description=(
                "Cadence Slice 2 — append composes §33.4 "
                "flock_append_line; no parallel impl."
            ),
            validate=_validate_composes_canonical_flock,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cadence_health_versioned_artifact_compliance"
            ),
            target_file=target,
            description=(
                "Cadence Slice 2 — §33.5 versioned-artifact "
                "contract."
            ),
            validate=_validate_versioned_artifact_compliance,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cadence_health_kind_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "Cadence Slice 2 — closed taxonomies for KIND_/"
                "FAILURE_CLASS_/SUBJECT_ module-level constants."
            ),
            validate=_validate_taxonomy_closed,
        ),
    ]


__all__ = [
    "CADENCE_HEALTH_SCHEMA_VERSION",
    "CadenceHealthRow",
    "FAILURE_CLASS_MISSING_PATH",
    "FAILURE_CLASS_OK",
    "FAILURE_CLASS_OS_POLICY",
    "FAILURE_CLASS_UNEXPECTED",
    "KIND_PREFLIGHT_FAILURE",
    "KIND_PREFLIGHT_OK",
    "SUBJECT_JARVIS_DIR",
    "SUBJECT_LOG_DIR_WRITE",
    "SUBJECT_MANIFEST_READ",
    "SUBJECT_NONE",
    "SUBJECT_REPO_ROOT",
    "classify_errno",
    "errno_name",
    "health_path",
    "most_recent_preflight_failure",
    "most_recent_preflight_ok_epoch",
    "read_recent",
    "record_health_row",
    "register_shipped_invariants",
    "run_preflight",
]
