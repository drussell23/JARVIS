"""RR Pass B Slice 1 — Order-2 manifest: schema + loader + Body entries.

Per ``memory/project_reverse_russian_doll_pass_b.md`` §3:

  > The Order-2 manifest is a ``(repo, path-glob)`` registry of
  > governance-code paths. Mutating any matched path triggers the
  > ``ORDER_2_GOVERNANCE`` risk class (Slice 2) and routes the op
  > through the operator amendment protocol (Slice 6).

Slice 1 ships the manifest **only** — no enforcement. The schema is
loaded at boot when ``JARVIS_ORDER2_MANIFEST_LOADED`` is truthy; until
Slices 2-6 wire downstream consumers (risk_tier_floor at GATE,
MetaPhaseRunner, REPL amendment surface), loading is **purely
observational**: structure validates, entries enumerate, glob matches
are queryable, but nothing about the FSM changes.

Authority invariants (Pass B §3.4):

  * The manifest is **read** by Slice 2's ``risk_tier_floor.py`` GATE
    classifier hook + Slice 5's ``MetaPhaseRunner`` AST validator.
    Tests pin: any future import of ``Order2Manifest`` outside
    those allowed callers is a CI failure (enforced at the
    grep-pin level once the consumers exist).
  * The manifest is **written** ONLY by the §7 amendment protocol
    (Slice 6) — never directly by O+V, never by APPLY, never by
    AutoCommitter.
  * This module itself: pure data + YAML parse. NO orchestrator /
    policy / iron_gate / risk_tier / change_engine /
    candidate_generator / gate / semantic_guardian imports.
    Allowed: stdlib + ``RepoRegistry`` (for repo lookup typing).
  * The default manifest path
    (``.jarvis/order2_manifest.yaml``) is operator-curated; edits
    are an Order-2 amendment and require the §7 protocol — but
    Slice 1 doesn't enforce that protocol; it just reads what's
    on disk. Slice 6 will land the write-side enforcement.

Default-off behind ``JARVIS_ORDER2_MANIFEST_LOADED`` until Slice 1's
own clean-session graduation. Hot-revert: set the flag to false →
loader returns an empty manifest. Behaviorally indistinguishable
from "manifest not yet shipped" since no other module consumes it
yet.
"""
from __future__ import annotations

import enum
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Schema version frozen at v1 — bumped on any Order2ManifestEntry
# field change so downstream consumers (Slice 2-6) can pin a parser
# version against it.
ORDER2_MANIFEST_SCHEMA_VERSION: int = 1

# Per-entry caps — defends against a malformed YAML pulling in a
# multi-MB blob.
MAX_RATIONALE_CHARS: int = 480
MAX_PATH_GLOB_CHARS: int = 256

# Maximum manifest entries. The Body-only initial set is 9; this cap
# bounds an accidental future commit that bloats the manifest into
# something that pins memory at boot.
MAX_MANIFEST_ENTRIES: int = 256

# Allowed repo names. Extends naturally to Trinity (jarvis-prime,
# jarvis-reactor) once the cross-Trinity integration document lands;
# Slice 1 ships Body-only.
KNOWN_REPOS: FrozenSet[str] = frozenset({
    "jarvis", "jarvis-prime", "jarvis-reactor",
})


def is_loaded() -> bool:
    """Master flag — ``JARVIS_ORDER2_MANIFEST_LOADED`` (default
    TRUE post Q4 Priority #3 graduation, 2026-05-02).

    Operator-authorized graduation: the manifest now loads on boot
    so the Order-2 governance-code path registry is observably
    active. This DOES NOT authorize Order-2 amendments — those
    require the Slice 6.x flags
    (``JARVIS_ORDER2_REVIEW_QUEUE_ENABLED`` /
    ``JARVIS_ORDER2_REPL_ENABLED``) which stay default-false.
    What graduating Slice 1 enables: classifier sees the manifest;
    Slice 2 (when graduated) elevates Order-2 paths to the
    ``ORDER_2_GOVERNANCE`` risk class; Slice 3 AST validator runs
    on candidate runners; Slice 4 shadow replay records.

    When off, :func:`get_default_manifest` returns an **empty**
    manifest regardless of what's on disk. Hot-revert: single env
    knob (``JARVIS_ORDER2_MANIFEST_LOADED=false``)."""
    raw = os.environ.get(
        "JARVIS_ORDER2_MANIFEST_LOADED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated 2026-05-02
    return raw in _TRUTHY


def manifest_path() -> Path:
    """Return the manifest file path. Env-overridable via
    ``JARVIS_ORDER2_MANIFEST_PATH``; defaults to
    ``.jarvis/order2_manifest.yaml`` under the cwd."""
    raw = os.environ.get("JARVIS_ORDER2_MANIFEST_PATH")
    if raw:
        return Path(raw)
    return Path(".jarvis") / "order2_manifest.yaml"


# ---------------------------------------------------------------------------
# Frozen schema
# ---------------------------------------------------------------------------


class ManifestLoadStatus(str, enum.Enum):
    """Outcome of a manifest load attempt. Pinned for telemetry +
    Slice 2 consumers' status checks."""

    LOADED = "LOADED"             # entries parsed; manifest live
    NOT_LOADED = "NOT_LOADED"     # JARVIS_ORDER2_MANIFEST_LOADED off
    FILE_MISSING = "FILE_MISSING"
    FILE_UNREADABLE = "FILE_UNREADABLE"
    SCHEMA_ERROR = "SCHEMA_ERROR"
    EMPTY = "EMPTY"               # file present, parses, zero entries


@dataclass(frozen=True)
class Order2ManifestEntry:
    """One governance-code path entry. Frozen — every field is
    audit-load-bearing.

    Per Pass B §3.1:
      * ``repo`` — RepoRegistry key (``"jarvis"`` for Body-only).
      * ``path_glob`` — POSIX glob relative to repo root.
      * ``rationale`` — operator-readable why-it-is-governance.
      * ``added`` — ISO date the entry landed.
      * ``added_by`` — ``"operator"`` or ``"<commit-sha>"`` for
        provenance.
    """

    repo: str
    path_glob: str
    rationale: str
    added: str
    added_by: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repo": self.repo,
            "path_glob": self.path_glob,
            "rationale": self.rationale,
            "added": self.added,
            "added_by": self.added_by,
        }


@dataclass(frozen=True)
class Order2Manifest:
    """Frozen manifest — entries tuple + schema version + load status.

    Slice 2-6 consumers query via :meth:`matches` and :meth:`entries_for_repo`.
    The manifest itself never mutates — Slice 6's amendment protocol
    rebuilds + reloads via :func:`get_default_manifest` after a
    `reset_default_manifest` call.
    """

    schema_version: int = ORDER2_MANIFEST_SCHEMA_VERSION
    entries: Tuple[Order2ManifestEntry, ...] = field(default_factory=tuple)
    status: ManifestLoadStatus = ManifestLoadStatus.NOT_LOADED
    notes: Tuple[str, ...] = field(default_factory=tuple)

    def matches(self, repo: str, path: str) -> bool:
        """Return True iff any entry's ``(repo, path_glob)`` matches
        the given ``(repo, path)``. Pure-data — no I/O.

        ``path`` is matched via :func:`fnmatch.fnmatchcase` (case-
        sensitive POSIX glob); repo must equal exactly."""
        from fnmatch import fnmatchcase
        for entry in self.entries:
            if entry.repo == repo and fnmatchcase(path, entry.path_glob):
                return True
        return False

    def entries_for_repo(self, repo: str) -> Tuple[Order2ManifestEntry, ...]:
        """Return all entries for a given repo. Used by Slice 5
        MetaPhaseRunner + Slice 6 REPL amendment surface."""
        return tuple(e for e in self.entries if e.repo == repo)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status.value,
            "entries": [e.to_dict() for e in self.entries],
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


_VALID_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_manifest(path: Optional[Path] = None) -> Order2Manifest:
    """Load + validate the manifest from disk.

    Returns an :class:`Order2Manifest` describing the load outcome.
    NEVER raises — every failure path returns a manifest with the
    appropriate :class:`ManifestLoadStatus` and ``notes`` populated
    so Slice 2-6 consumers can render the diagnostic.

    Skip behaviour:
      * Master flag off → ``NOT_LOADED`` with empty entries.
      * File missing → ``FILE_MISSING``.
      * File unreadable / parse error → ``FILE_UNREADABLE`` /
        ``SCHEMA_ERROR``.
      * File present, zero entries → ``EMPTY``.
    """
    if not is_loaded():
        return Order2Manifest(
            status=ManifestLoadStatus.NOT_LOADED,
            notes=("master_flag_off",),
        )
    p = path or manifest_path()
    if not p.exists():
        return Order2Manifest(
            status=ManifestLoadStatus.FILE_MISSING,
            notes=(f"path_missing:{p}",),
        )
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        return Order2Manifest(
            status=ManifestLoadStatus.FILE_UNREADABLE,
            notes=(f"read_failed:{exc}",),
        )
    return _parse_yaml(raw)


def _parse_yaml(raw: str) -> Order2Manifest:
    """Defensive YAML parse + per-entry validation. PyYAML is
    optional; when unavailable, the loader degrades to ``SCHEMA_ERROR``
    with a clear note rather than crashing the boot path."""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return Order2Manifest(
            status=ManifestLoadStatus.SCHEMA_ERROR,
            notes=("yaml_module_missing",),
        )
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return Order2Manifest(
            status=ManifestLoadStatus.SCHEMA_ERROR,
            notes=(f"yaml_parse_failed:{exc}",),
        )
    if doc is None:
        return Order2Manifest(
            status=ManifestLoadStatus.EMPTY,
            notes=("empty_document",),
        )
    if not isinstance(doc, dict):
        return Order2Manifest(
            status=ManifestLoadStatus.SCHEMA_ERROR,
            notes=("doc_not_mapping",),
        )

    declared_version = doc.get("schema_version")
    notes: List[str] = []
    if declared_version != ORDER2_MANIFEST_SCHEMA_VERSION:
        notes.append(
            f"schema_version_mismatch:declared={declared_version},"
            f"expected={ORDER2_MANIFEST_SCHEMA_VERSION}"
        )

    raw_entries = doc.get("entries")
    if not isinstance(raw_entries, list):
        return Order2Manifest(
            status=ManifestLoadStatus.SCHEMA_ERROR,
            notes=tuple(notes + ["entries_key_missing_or_not_list"]),
        )

    entries: List[Order2ManifestEntry] = []
    for i, raw_entry in enumerate(raw_entries):
        if i >= MAX_MANIFEST_ENTRIES:
            notes.append(
                f"entries_truncated_at_max_{MAX_MANIFEST_ENTRIES}"
            )
            break
        coerced = _coerce_entry(raw_entry, notes, idx=i)
        if coerced is not None:
            entries.append(coerced)

    if not entries:
        return Order2Manifest(
            status=ManifestLoadStatus.EMPTY,
            entries=(),
            notes=tuple(notes),
        )

    logger.info(
        "[Order2Manifest] loaded %d entries from %s",
        len(entries), manifest_path(),
    )
    return Order2Manifest(
        schema_version=ORDER2_MANIFEST_SCHEMA_VERSION,
        entries=tuple(entries),
        status=ManifestLoadStatus.LOADED,
        notes=tuple(notes),
    )


def _coerce_entry(
    raw: Any,
    notes: List[str],
    idx: int,
) -> Optional[Order2ManifestEntry]:
    """Defensively coerce one raw YAML entry into an
    :class:`Order2ManifestEntry`. Returns ``None`` on validation
    failure with a structured note."""
    if not isinstance(raw, dict):
        notes.append(f"entry_{idx}_not_mapping")
        return None
    repo = str(raw.get("repo") or "").strip()
    if repo not in KNOWN_REPOS:
        notes.append(f"entry_{idx}_unknown_repo:{repo!r}")
        return None
    path_glob = str(raw.get("path_glob") or "").strip()
    if not path_glob:
        notes.append(f"entry_{idx}_empty_path_glob")
        return None
    if len(path_glob) > MAX_PATH_GLOB_CHARS:
        path_glob = path_glob[:MAX_PATH_GLOB_CHARS]
        notes.append(f"entry_{idx}_path_glob_truncated")
    rationale = str(raw.get("rationale") or "").strip()
    if not rationale:
        notes.append(f"entry_{idx}_empty_rationale")
        return None
    if len(rationale) > MAX_RATIONALE_CHARS:
        rationale = rationale[:MAX_RATIONALE_CHARS]
        notes.append(f"entry_{idx}_rationale_truncated")
    added = str(raw.get("added") or "").strip()
    if not _VALID_DATE_RE.match(added):
        notes.append(f"entry_{idx}_bad_added_date:{added!r}")
        return None
    added_by = str(raw.get("added_by") or "").strip()
    if not added_by:
        notes.append(f"entry_{idx}_empty_added_by")
        return None
    return Order2ManifestEntry(
        repo=repo,
        path_glob=path_glob,
        rationale=rationale,
        added=added,
        added_by=added_by,
    )


# ---------------------------------------------------------------------------
# Default-singleton accessor
# ---------------------------------------------------------------------------


_default_manifest: Optional[Order2Manifest] = None
_default_lock = threading.Lock()


def get_default_manifest() -> Order2Manifest:
    """Process-wide manifest. Lazy-load on first call. The cached
    manifest is reused until :func:`reset_default_manifest` is called
    (Slice 6's amendment protocol will call reset after a successful
    operator-authorized amendment).

    Boot wiring: any module that wants to consult the manifest calls
    this function. Slice 2-6 consumers MUST treat ``status !=
    LOADED`` as "no Order-2 enforcement" (i.e. the pre-Pass-B
    behaviour) so the cage degrades to the existing safety stack
    when the manifest is missing / disabled / malformed."""
    global _default_manifest
    with _default_lock:
        if _default_manifest is None:
            _default_manifest = load_manifest()
    return _default_manifest


def reset_default_manifest() -> None:
    """Reset the cached manifest. Slice 6 amendment protocol calls
    this after writing the YAML. Tests use it for isolation."""
    global _default_manifest
    with _default_lock:
        _default_manifest = None


__all__ = [
    "KNOWN_REPOS",
    "MAX_MANIFEST_ENTRIES",
    "MAX_PATH_GLOB_CHARS",
    "MAX_RATIONALE_CHARS",
    "ManifestLoadStatus",
    "ORDER2_MANIFEST_SCHEMA_VERSION",
    "Order2Manifest",
    "Order2ManifestEntry",
    "get_default_manifest",
    "is_loaded",
    "load_manifest",
    "manifest_path",
    "reset_default_manifest",
]


# ---------------------------------------------------------------------------
# Pass B Graduation Slice 2 — substrate AST pin
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    from backend.core.ouroboros.governance.meta._invariant_helpers import (
        make_pass_b_substrate_invariant,
    )
    inv = make_pass_b_substrate_invariant(
        invariant_name="pass_b_order2_manifest_substrate",
        target_file=(
            "backend/core/ouroboros/governance/meta/order2_manifest.py"
        ),
        description=(
            "Pass B Slice 1 substrate: Order2Manifest + "
            "Order2ManifestEntry (frozen) + is_loaded + manifest_path "
            "+ load_manifest present; no dynamic-code calls."
        ),
        required_funcs=("is_loaded", "manifest_path", "load_manifest"),
        required_classes=("Order2Manifest", "Order2ManifestEntry"),
        frozen_classes=("Order2ManifestEntry",),
    )
    return [inv] if inv is not None else []
