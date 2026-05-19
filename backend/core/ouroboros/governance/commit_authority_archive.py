"""CommitAuthorityArchive — OCA observability ring (Slice 3 #2).

Read-side observability for Operator Commit Authority decisions.
Mirrors :class:`permission_decision_archive.BoundedDecisionArchive`
EXACTLY — the single canonical ring shape across the cross-substrate
``/expand <ref>`` family (``t-N``/``d-N``/``o-N``/``n-N``/``p-N``);
this arc adds ``c-N``.

Two layers, composed (zero parallel logic):

  * **In-memory ring** — bounded FIFO, monotonic ``c-N`` refs,
    drop-oldest, ``threading.RLock``. Powers ``/commit recent``.
  * **Durable JSONL ledger** — every record is also appended via
    the canonical :func:`cross_process_jsonl.flock_append_line`
    (NO parallel ``fcntl``; the single cross-process-safe append
    primitive). Survives process restarts; cross-soak audit.

Recorded event kinds (closed taxonomy, AST-pinned):
``grant_issue`` / ``revoke`` / ``verify_verdict`` / ``consume`` /
``bypass_suspected`` / ``enable``.

This is OBSERVABILITY ONLY. It records projections of authority
decisions; it has zero say in any verdict. Authority asymmetry
(AST-pinned): no orchestrator / iron_gate / providers /
change_engine / candidate_generator import. Master-flag-gated
(default-FALSE per §33.1 — recording is a no-op when off). NEVER
raises into the authority path.
"""
from __future__ import annotations

import enum
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Ouroboros.CommitAuthorityArchive")


COMMIT_AUTHORITY_ARCHIVE_SCHEMA_VERSION: str = (
    "commit_authority_archive.v1"
)

MASTER_FLAG_ENV_VAR: str = "JARVIS_COMMIT_AUTHORITY_ARCHIVE_ENABLED"
ARCHIVE_SIZE_ENV_VAR: str = "JARVIS_COMMIT_AUTHORITY_ARCHIVE_SIZE"
LEDGER_PATH_ENV_VAR: str = "JARVIS_COMMIT_AUTHORITY_ARCHIVE_PATH"

_DEFAULT_ARCHIVE_SIZE: int = 50
_MIN_ARCHIVE_SIZE: int = 1
_MAX_ARCHIVE_SIZE: int = 10_000

# Exposed publicly so REPL parsers / tests build refs without
# string-munging this module's literals (mirrors REF_PREFIX
# convention in permission_decision_archive).
REF_PREFIX: str = "c-"

_DEFAULT_LEDGER_RELATIVE = ".jarvis/commit_authority/archive.jsonl"


class CommitAuthorityEventKind(str, enum.Enum):
    """Closed taxonomy of archivable OCA events (AST-pinned)."""

    GRANT_ISSUE = "grant_issue"
    REVOKE = "revoke"
    VERIFY_VERDICT = "verify_verdict"
    CONSUME = "consume"
    BYPASS_SUSPECTED = "bypass_suspected"
    ENABLE = "enable"

    @classmethod
    def parse(cls, value: object) -> Optional["CommitAuthorityEventKind"]:
        try:
            return cls(str(value).strip().lower())
        except Exception:  # noqa: BLE001
            return None


# ---------------------------------------------------------------------------
# Master flag + env knobs
# ---------------------------------------------------------------------------


def commit_authority_archive_enabled() -> bool:
    """Master switch. Default-FALSE per §33.1 graduation contract —
    recording is a no-op when off (telemetry, never authority).
    Re-read every call so a flip hot-reverts."""
    return os.environ.get(
        MASTER_FLAG_ENV_VAR, "false",
    ).strip().lower() in ("1", "true", "yes", "on")


def _capacity_from_env() -> int:
    raw = os.environ.get(ARCHIVE_SIZE_ENV_VAR, "").strip()
    if not raw:
        return _DEFAULT_ARCHIVE_SIZE
    try:
        return max(_MIN_ARCHIVE_SIZE, min(_MAX_ARCHIVE_SIZE, int(raw)))
    except (TypeError, ValueError):
        return _DEFAULT_ARCHIVE_SIZE


def _ledger_path() -> Path:
    raw = os.environ.get(LEDGER_PATH_ENV_VAR, "").strip()
    if raw:
        try:
            return Path(raw).expanduser()
        except Exception:  # noqa: BLE001
            pass
    return Path(_DEFAULT_LEDGER_RELATIVE)


def _safe_str(raw: object) -> str:
    try:
        return str(raw)
    except Exception:  # noqa: BLE001
        return "<unprintable>"


# ---------------------------------------------------------------------------
# Frozen record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommitAuthorityRecord:
    """One archived OCA event. ``ref`` is a stable monotonic
    ``c-N``. ``detail`` is a shallow projection dict (read-only —
    consumers MUST NOT mutate)."""

    ref: str
    kind: str
    detail: Dict[str, Any]
    inserted_at: float
    schema_version: str = COMMIT_AUTHORITY_ARCHIVE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ref": self.ref,
            "kind": self.kind,
            "detail": dict(self.detail),
            "inserted_at": self.inserted_at,
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# Bounded ring
# ---------------------------------------------------------------------------


class BoundedCommitAuthorityArchive:
    """Thread-safe bounded FIFO of OCA events. Mirrors
    :class:`permission_decision_archive.BoundedDecisionArchive`
    semantics: drop-oldest, monotonic ``c-N`` refs that never
    reuse, reentrant lock. NEVER raises."""

    def __init__(self, *, capacity: Optional[int] = None) -> None:
        if capacity is None:
            cap = _capacity_from_env()
        else:
            try:
                cap = max(
                    _MIN_ARCHIVE_SIZE,
                    min(_MAX_ARCHIVE_SIZE, int(capacity)),
                )
            except (TypeError, ValueError):
                cap = _DEFAULT_ARCHIVE_SIZE
        self._capacity = cap
        self._items: "OrderedDict[str, CommitAuthorityRecord]" = (
            OrderedDict()
        )
        self._next_seq = 1
        self._lock = threading.RLock()

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def record(
        self, *, kind: object, detail: Optional[dict] = None,
    ) -> Optional[CommitAuthorityRecord]:
        """Park one event. Master-gated → ``None`` when off.
        Unknown kind (outside the closed taxonomy) → ``None``
        (never pollute the ring). NEVER raises into the caller."""
        if not commit_authority_archive_enabled():
            return None
        parsed = CommitAuthorityEventKind.parse(kind)
        if parsed is None:
            logger.debug(
                "[CommitAuthorityArchive] unknown kind %r — skipped",
                kind,
            )
            return None
        safe_detail: Dict[str, Any] = {}
        if isinstance(detail, dict):
            for k, v in detail.items():
                safe_detail[_safe_str(k)] = (
                    v if isinstance(v, (str, int, float, bool))
                    or v is None else _safe_str(v)
                )
        try:
            with self._lock:
                ref = f"{REF_PREFIX}{self._next_seq}"
                self._next_seq += 1
                rec = CommitAuthorityRecord(
                    ref=ref,
                    kind=parsed.value,
                    detail=safe_detail,
                    inserted_at=time.time(),
                )
                self._items[ref] = rec
                while len(self._items) > self._capacity:
                    self._items.popitem(last=False)  # drop oldest
            _append_ledger(rec)
            _publish_sse(rec)
            return rec
        except Exception as exc:  # noqa: BLE001 — never into authority
            logger.debug(
                "[CommitAuthorityArchive] record failed: %s", exc,
            )
            return None

    def lookup(self, ref: str) -> Optional[CommitAuthorityRecord]:
        with self._lock:
            return self._items.get(ref)

    def recent(self, n: int = 10) -> List[Dict[str, Any]]:
        """Return up to ``n`` newest records (chronological,
        newest last) as ``to_dict`` projections. NEVER raises."""
        try:
            cnt = max(1, min(_MAX_ARCHIVE_SIZE, int(n)))
        except (TypeError, ValueError):
            cnt = 10
        with self._lock:
            vals = list(self._items.values())
        return [r.to_dict() for r in vals[-cnt:]]


def _append_ledger(rec: CommitAuthorityRecord) -> None:
    """Durable append via the canonical cross-process primitive.
    Best-effort; ledger failure never affects the in-memory ring
    or the authority path. NEVER raises."""
    try:
        import json
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_append_line,
        )
        path = _ledger_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        flock_append_line(path, json.dumps(rec.to_dict(), sort_keys=True))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[CommitAuthorityArchive] ledger append: %s", exc)


def _publish_sse(rec: CommitAuthorityRecord) -> None:
    """Emit the ``commit_authority_decision_recorded`` SSE frame.
    Best-effort, fail-silent — mirrors the git_index_guard
    on_anomaly seam. Stream absence/disable never affects the ring
    or the authority path. NEVER raises. (ide_observability_stream
    is observability, not a decision-side module — no
    authority-asymmetry violation.)"""
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            publish_commit_authority_decision,
        )
        publish_commit_authority_decision(rec.to_dict())
    except Exception as exc:  # noqa: BLE001
        logger.debug("[CommitAuthorityArchive] sse publish: %s", exc)


# ---------------------------------------------------------------------------
# Module-level singleton (mirrors get_default_broker / store pattern)
# ---------------------------------------------------------------------------


_default_archive: Optional[BoundedCommitAuthorityArchive] = None
_default_lock = threading.Lock()


def get_default_archive() -> BoundedCommitAuthorityArchive:
    global _default_archive
    with _default_lock:
        if _default_archive is None:
            _default_archive = BoundedCommitAuthorityArchive()
        return _default_archive


def reset_default_archive_for_tests() -> None:
    global _default_archive
    with _default_lock:
        _default_archive = None


def record(
    *, kind: object, detail: Optional[dict] = None,
) -> Optional[CommitAuthorityRecord]:
    """Module-level convenience — record onto the default archive.
    NEVER raises."""
    try:
        return get_default_archive().record(kind=kind, detail=detail)
    except Exception:  # noqa: BLE001
        return None


def recent(n: int = 10) -> List[Dict[str, Any]]:
    """Module-level convenience — newest N from the default
    archive. NEVER raises."""
    try:
        return get_default_archive().recent(n)
    except Exception:  # noqa: BLE001
        return []


__all__ = [
    "COMMIT_AUTHORITY_ARCHIVE_SCHEMA_VERSION",
    "MASTER_FLAG_ENV_VAR",
    "ARCHIVE_SIZE_ENV_VAR",
    "LEDGER_PATH_ENV_VAR",
    "REF_PREFIX",
    "CommitAuthorityEventKind",
    "CommitAuthorityRecord",
    "BoundedCommitAuthorityArchive",
    "commit_authority_archive_enabled",
    "get_default_archive",
    "reset_default_archive_for_tests",
    "record",
    "recent",
    "register_flags",
    "register_shipped_invariants",
]


# ---------------------------------------------------------------------------
# Module-owned FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry) -> int:  # noqa: ANN001
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[CommitAuthorityArchive] register_flags degraded: %s", exc,
        )
        return 0
    tgt = (
        "backend/core/ouroboros/governance/commit_authority_archive.py"
    )
    specs = [
        FlagSpec(
            name=MASTER_FLAG_ENV_VAR, type=FlagType.BOOL,
            default=False, category=Category.OBSERVABILITY,
            source_file=tgt,
            example=f"{MASTER_FLAG_ENV_VAR}=true",
            description=(
                "Master switch for the OCA observability ring + "
                "durable JSONL ledger. Default-FALSE per §33.1 — "
                "recording is a no-op when off (telemetry, never "
                "authority)."
            ),
        ),
        FlagSpec(
            name=ARCHIVE_SIZE_ENV_VAR, type=FlagType.INT,
            default=_DEFAULT_ARCHIVE_SIZE, category=Category.CAPACITY,
            source_file=tgt,
            example=f"{ARCHIVE_SIZE_ENV_VAR}=100",
            description=(
                "In-memory c-N ring capacity (drop-oldest). Floor "
                f"{_MIN_ARCHIVE_SIZE}, ceiling {_MAX_ARCHIVE_SIZE}."
            ),
        ),
    ]
    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[CommitAuthorityArchive] seed %s skipped: %s",
                spec.name, exc,
            )
    return count


# ---------------------------------------------------------------------------
# Module-owned shipped_code_invariants (AST pins)
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Pins: authority asymmetry (no decision-side imports), the
    closed event-kind taxonomy, composes the canonical
    flock_append_line (no parallel fcntl)."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate(tree: "_ast.Module", source: str) -> tuple:
        violations: list = []
        forbidden = (
            "orchestrator", "iron_gate", "providers",
            "change_engine", "candidate_generator",
            "semantic_guardian", "urgency_router",
        )
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                mod = node.module or ""
                for f in forbidden:
                    if f in mod:
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"authority-asymmetry violation — "
                            f"archive must not import {f!r}"
                        )
                if (node.module or "").endswith("fcntl"):
                    violations.append(
                        "must NOT import fcntl — compose the "
                        "canonical flock_append_line"
                    )
            if isinstance(node, _ast.Import):
                for a in node.names:
                    if a.name == "fcntl":
                        violations.append(
                            "must NOT import fcntl — compose "
                            "flock_append_line"
                        )
        if "flock_append_line" not in source:
            violations.append(
                "durable ledger must compose the canonical "
                "cross_process_jsonl.flock_append_line"
            )
        required = {
            "GRANT_ISSUE", "REVOKE", "VERIFY_VERDICT",
            "CONSUME", "BYPASS_SUSPECTED", "ENABLE",
        }
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef) and (
                node.name == "CommitAuthorityEventKind"
            ):
                seen = {
                    t.id
                    for stmt in node.body
                    if isinstance(stmt, _ast.Assign)
                    for t in stmt.targets
                    if isinstance(t, _ast.Name)
                }
                if required - seen:
                    violations.append(
                        f"CommitAuthorityEventKind missing "
                        f"{sorted(required - seen)}"
                    )
                if seen - required:
                    violations.append(
                        f"CommitAuthorityEventKind unexpected "
                        f"(closed-taxonomy) {sorted(seen - required)}"
                    )
        return tuple(violations)

    tgt = (
        "backend/core/ouroboros/governance/commit_authority_archive.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name="commit_authority_archive_asymmetry_taxonomy",
            target_file=tgt,
            description=(
                "OCA observability ring stays authority-asymmetric "
                "(no orchestrator/iron_gate/providers/... import), "
                "composes the canonical flock_append_line (no "
                "parallel fcntl), and CommitAuthorityEventKind is "
                "the closed 6-value taxonomy."
            ),
            validate=_validate,
        ),
    ]
