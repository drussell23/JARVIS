"""DomainMapMemory -- Slice 3 of ClusterIntelligence-CrossSession arc.
======================================================================

Cross-session typed memory mapping
``cluster.centroid_hash8 -> {discovered_files, architectural_role,
exploration_count, ...}``. The structural close for the
"sovereign architect" gap: when ProactiveExploration emits a
cluster_coverage envelope for a previously-explored cluster
(stable centroid_hash8 across rebuilds), the bridge consults
this store and threads prior context into the envelope so the
model picks up where it left off instead of re-deriving the
domain from scratch every session.

Slice 3 ships READ + WRITE APIs as a primitive. Slice 4 wires
the cascade observer to call ``record_exploration`` on
``on_verify_completed`` for cluster_coverage ops, and wires
ProactiveExploration to consult ``lookup_by_centroid_hash8``
when building envelopes.

Reuse contract (no duplication)
-------------------------------

* :func:`flock_critical_section` from
  :mod:`cross_process_jsonl` -- the same flock primitive
  InvariantDriftStore + ApprovalStore + AdaptationLedger use.
  Per-entry JSON file persistence with cross-process safe
  read-modify-write.
* Storage shape mirrors :mod:`user_preference_memory` discipline:
  per-entry files (one ``.json`` per centroid_hash8), atomic
  write via tempfile+rename, defensive parse on read, NEVER-raise
  IO contract.
* Master flag pattern mirrors every other Slice 1 primitive in
  the arc family (asymmetric env semantics, default-false until
  Slice 5 graduation).
* No new authority surfaces. Read-only over disk + frozen
  dataclasses out.

Persistence layout
------------------

::

    <project_root>/.jarvis/domain_map/
      <centroid_hash8>.json       # one per cluster
      <centroid_hash8>.json.lock  # flock companion (created
                                  # on first read-modify-write)

Each ``.json`` file holds a single ``DomainMapEntry`` rendered
via :meth:`to_dict`. Atomic write via tempfile + ``os.replace``
ensures partial-write corruption can't be observed by a
concurrent reader.

Reverse-Russian-Doll posture
----------------------------

* O+V (the inner doll) gains episodic memory of its own
  exploration history -- the substrate that lets the model
  build on prior sessions instead of reinventing each time.
* Antivenom (the constraint, the immune system) scales
  proportionally:
    - Frozen :class:`DomainMapEntry` dataclass + closed-set
      schema_version pin
    - Pure-stdlib at hot path (only governance import is
      ``cross_process_jsonl.flock_critical_section``)
    - Defensive at every IO boundary -- corrupt JSON returns
      ``None`` from lookup, never raises into callers
    - Master flag default false until Slice 5 graduation
    - Atomic write via tempfile+rename: a crashed write leaves
      either the prior valid JSON or no file (never partial)
    - Cross-process flock for read-modify-write -- two
      concurrent ``record_exploration`` calls on the same
      cluster cannot interleave their merges
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from backend.core.ouroboros.governance.cross_process_jsonl import (
    flock_critical_section,
)

logger = logging.getLogger("Ouroboros.DomainMapMemory")


DOMAIN_MAP_SCHEMA_VERSION: str = "domain_map.v1"

# Per-entry filename suffix (the centroid_hash8 stem is provided
# by the caller).
_ENTRY_SUFFIX: str = ".json"

# Default subdirectory under project_root.
_DEFAULT_DIR_NAME: str = ".jarvis/domain_map"

# Hard cap on discovered_files per entry -- prevents a pathological
# exploration from ballooning a single entry file. Independent of
# Slice 1's K knob.
_DEFAULT_FILES_CAP: int = 64

# Hard cap on architectural_role string length -- prevents the
# (Slice 4 optional) Venom one-liner from exceeding sane bounds.
_DEFAULT_ROLE_MAX_CHARS: int = 500


# ---------------------------------------------------------------------------
# Master flag + env knobs
# ---------------------------------------------------------------------------


def domain_map_enabled() -> bool:
    """``JARVIS_DOMAIN_MAP_ENABLED`` (default ``true`` post Slice
    5 graduation, 2026-05-03).

    Asymmetric env semantics -- empty/whitespace = unset = current
    default; explicit truthy/falsy overrides. Re-read on every
    call so flag flips hot-revert without restart.

    When off, every public method short-circuits to no-op-safe
    defaults: ``lookup_by_centroid_hash8 -> None``,
    ``list_all -> []``, ``record_exploration -> None``. No disk
    IO is performed on any path.
    """
    raw = os.environ.get(
        "JARVIS_DOMAIN_MAP_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default
    return raw in ("1", "true", "yes", "on")


def domain_map_files_cap() -> int:
    """``JARVIS_DOMAIN_MAP_FILES_CAP`` (default 64, floor 1,
    ceiling 256). Hard cap on ``discovered_files`` per entry."""
    raw = os.environ.get("JARVIS_DOMAIN_MAP_FILES_CAP", "").strip()
    try:
        n = int(raw) if raw else _DEFAULT_FILES_CAP
    except ValueError:
        n = _DEFAULT_FILES_CAP
    return max(1, min(256, n))


def domain_map_role_max_chars() -> int:
    """``JARVIS_DOMAIN_MAP_ROLE_MAX_CHARS`` (default 500, floor
    32, ceiling 4000). Hard cap on the ``architectural_role``
    string length at write time."""
    raw = os.environ.get(
        "JARVIS_DOMAIN_MAP_ROLE_MAX_CHARS", "",
    ).strip()
    try:
        n = int(raw) if raw else _DEFAULT_ROLE_MAX_CHARS
    except ValueError:
        n = _DEFAULT_ROLE_MAX_CHARS
    return max(32, min(4000, n))


def domain_map_lock_timeout_s() -> float:
    """``JARVIS_DOMAIN_MAP_LOCK_TIMEOUT_S`` (default 5.0, floor
    0.1, ceiling 30.0). Cross-process flock acquisition timeout
    for record_exploration's read-modify-write critical
    section."""
    raw = os.environ.get(
        "JARVIS_DOMAIN_MAP_LOCK_TIMEOUT_S", "",
    ).strip()
    try:
        n = float(raw) if raw else 5.0
    except ValueError:
        n = 5.0
    return max(0.1, min(30.0, n))


# ---------------------------------------------------------------------------
# Frozen dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DomainMapEntry:
    """One persisted memory of a cluster exploration.

    Frozen so callers can pass instances around without risk of
    mutation. ``record_exploration`` produces NEW instances via
    the merge logic (existing entry + new fragment -> merged
    entry), never mutates in place.

    ``centroid_hash8`` is the load-bearing key -- it's the same
    hash :class:`semantic_index.ClusterInfo` exposes, so a cluster
    that survives a rebuild (same shape -> same hash) keeps its
    DomainMap entry across sessions.

    ``discovered_files`` is a tuple of repository-relative paths
    accumulated across explorations. Bounded by
    :func:`domain_map_files_cap`. Order preserved (most-recently-
    discovered first when merged).

    ``architectural_role`` is a short free-form description --
    in Slice 4 this is optionally populated by a single Venom
    round asking "in 1 sentence what's the architectural role of
    these files?" Empty when unknown / role-inference disabled.

    ``confidence`` is a 0.0-1.0 self-reported quality score.
    Slice 4's cascade observer sets this from the
    SkillInvocationOutcome / verify outcome of the originating
    op. 0.0 when unknown.

    ``exploration_count`` increments on each
    ``record_exploration`` call for the same centroid_hash8 --
    operators see how mature the entry is.

    ``populated_by_op_id`` is the most-recent op_id that
    contributed to this entry; preserves audit trail for §8.
    """

    centroid_hash8: str
    cluster_id: int = -1  # -1 = unknown (cluster_id is unstable
                          # across rebuilds; centroid_hash8 is
                          # the stable key)
    theme_label: str = ""
    discovered_files: Tuple[str, ...] = ()
    architectural_role: str = ""
    confidence: float = 0.0
    last_updated_at: float = 0.0  # Unix epoch
    populated_by_op_id: str = ""
    exploration_count: int = 0
    schema_version: str = DOMAIN_MAP_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "centroid_hash8": self.centroid_hash8,
            "cluster_id": int(self.cluster_id),
            "theme_label": self.theme_label,
            "discovered_files": list(self.discovered_files),
            "architectural_role": self.architectural_role,
            "confidence": float(self.confidence),
            "last_updated_at": float(self.last_updated_at),
            "populated_by_op_id": self.populated_by_op_id,
            "exploration_count": int(self.exploration_count),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Optional["DomainMapEntry"]:
        """Defensive parse. Returns ``None`` when ``d`` is
        malformed (missing centroid_hash8 / wrong types). NEVER
        raises -- the corrupt-file path returns None so the store
        skips the entry."""
        try:
            if not isinstance(d, Mapping):
                return None
            hash8 = str(d.get("centroid_hash8", "") or "").strip()
            if not hash8:
                return None
            files_raw = d.get("discovered_files", []) or []
            if not isinstance(files_raw, (list, tuple)):
                files_raw = []
            files = tuple(
                str(p) for p in files_raw
                if isinstance(p, str) and p
            )
            return cls(
                centroid_hash8=hash8,
                cluster_id=int(d.get("cluster_id", -1) or -1),
                theme_label=str(d.get("theme_label", "") or ""),
                discovered_files=files,
                architectural_role=str(
                    d.get("architectural_role", "") or "",
                ),
                confidence=float(d.get("confidence", 0.0) or 0.0),
                last_updated_at=float(
                    d.get("last_updated_at", 0.0) or 0.0,
                ),
                populated_by_op_id=str(
                    d.get("populated_by_op_id", "") or "",
                ),
                exploration_count=int(
                    d.get("exploration_count", 0) or 0,
                ),
                schema_version=str(
                    d.get("schema_version", DOMAIN_MAP_SCHEMA_VERSION),
                ),
            )
        except (ValueError, TypeError) as exc:
            logger.debug(
                "[DomainMap] from_dict degraded: %s", exc,
            )
            return None
        except Exception as exc:  # noqa: BLE001 -- last-resort
            logger.debug(
                "[DomainMap] from_dict last-resort degraded: %s",
                exc,
            )
            return None


# ---------------------------------------------------------------------------
# Hash validation -- pin the centroid_hash8 shape
# ---------------------------------------------------------------------------


def _is_valid_hash8(hash8: str) -> bool:
    """centroid_hash8 must be a non-empty alphanumeric string of
    bounded length. Filename-safe by construction."""
    if not isinstance(hash8, str):
        return False
    h = hash8.strip()
    if not h or len(h) > 64:
        return False
    return all(c.isalnum() for c in h)


# ---------------------------------------------------------------------------
# DomainMapStore
# ---------------------------------------------------------------------------


class DomainMapStore:
    """Cross-session per-centroid_hash8 entry store. Thread-safe
    intra-process via ``self._lock``; cross-process safe via
    flock_critical_section on per-entry .lock files.

    Read API (lookup_by_centroid_hash8 / list_all) does NOT take
    flocks -- the worst case is reading a stale-but-consistent
    entry (atomic-write guarantees no torn reads), which is
    acceptable for episodic memory. Write API
    (record_exploration) takes a per-entry flock.
    """

    def __init__(
        self,
        project_root: Path,
        *,
        dir_name: str = _DEFAULT_DIR_NAME,
    ) -> None:
        self._project_root = Path(project_root)
        self._dir = self._project_root / dir_name
        self._lock = threading.Lock()  # in-process serialization

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def lookup_by_centroid_hash8(
        self, centroid_hash8: str,
    ) -> Optional[DomainMapEntry]:
        """Return the entry for ``centroid_hash8`` or ``None``.

        Returns None when:
          * Master flag is off (graduation gate)
          * hash8 fails validation (empty / non-alnum / too long)
          * On-disk file does not exist
          * On-disk file is malformed JSON
          * On-disk file's schema fails validation

        NEVER raises.
        """
        try:
            if not domain_map_enabled():
                return None
            if not _is_valid_hash8(centroid_hash8):
                return None
            path = self._dir / f"{centroid_hash8}{_ENTRY_SUFFIX}"
            if not path.exists():
                return None
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.debug(
                    "[DomainMap] read %s degraded: %s", path, exc,
                )
                return None
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                logger.debug(
                    "[DomainMap] corrupt JSON at %s: %s", path, exc,
                )
                return None
            return DomainMapEntry.from_dict(data)
        except Exception as exc:  # noqa: BLE001 -- last-resort
            logger.debug(
                "[DomainMap] lookup last-resort degraded: %s", exc,
            )
            return None

    def list_all(self) -> List[DomainMapEntry]:
        """Return every valid entry in the store, sorted by
        ``last_updated_at`` descending. Skips malformed files
        silently. NEVER raises."""
        out: List[DomainMapEntry] = []
        try:
            if not domain_map_enabled():
                return out
            if not self._dir.is_dir():
                return out
            for entry_path in sorted(self._dir.glob(f"*{_ENTRY_SUFFIX}")):
                try:
                    raw = entry_path.read_text(encoding="utf-8")
                    data = json.loads(raw)
                except (OSError, json.JSONDecodeError) as exc:
                    logger.debug(
                        "[DomainMap] list_all skipped %s: %s",
                        entry_path, exc,
                    )
                    continue
                entry = DomainMapEntry.from_dict(data)
                if entry is not None:
                    out.append(entry)
            out.sort(
                key=lambda e: e.last_updated_at, reverse=True,
            )
        except Exception as exc:  # noqa: BLE001 -- last-resort
            logger.debug(
                "[DomainMap] list_all last-resort degraded: %s",
                exc,
            )
        return out

    # ------------------------------------------------------------------
    # Write API -- idempotent merge
    # ------------------------------------------------------------------

    def record_exploration(
        self,
        centroid_hash8: str,
        *,
        theme_label: str = "",
        discovered_files: Tuple[str, ...] = (),
        architectural_role: str = "",
        confidence: float = 0.0,
        cluster_id: int = -1,
        op_id: str = "",
    ) -> Optional[DomainMapEntry]:
        """Idempotent merge: existing entry (if any) + this
        fragment -> merged entry persisted atomically.

        Merge rules:
          * ``theme_label``: caller wins if non-empty (clusters
            theme_label can drift slightly across rebuilds; the
            most recent one is the most relevant).
          * ``discovered_files``: dedup-preserving-order union of
            existing + new (new files prepended), capped at
            :func:`domain_map_files_cap`.
          * ``architectural_role``: caller wins if non-empty;
            existing preserved otherwise (don't overwrite a
            known role with empty just because Slice 4 didn't
            invoke role-inference this time).
          * ``confidence``: max(existing, new) -- monotonic
            tightening.
          * ``cluster_id``: caller wins if >=0; existing
            preserved otherwise.
          * ``exploration_count``: existing + 1 (every call
            counts as one exploration).
          * ``last_updated_at``: now.
          * ``populated_by_op_id``: caller wins if non-empty.

        Returns the persisted merged entry, or ``None`` when:
          * Master flag is off
          * hash8 fails validation
          * Cross-process flock could not be acquired in time
          * Atomic write failed (disk full / permission denied)

        NEVER raises.
        """
        try:
            if not domain_map_enabled():
                return None
            if not _is_valid_hash8(centroid_hash8):
                return None
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.debug(
                    "[DomainMap] mkdir %s degraded: %s",
                    self._dir, exc,
                )
                return None
            path = self._dir / f"{centroid_hash8}{_ENTRY_SUFFIX}"
            timeout_s = domain_map_lock_timeout_s()
            files_cap = domain_map_files_cap()
            role_cap = domain_map_role_max_chars()
            now = time.time()

            with self._lock:
                with flock_critical_section(
                    path, timeout_s=timeout_s,
                ) as acquired:
                    if not acquired:
                        logger.debug(
                            "[DomainMap] flock acquisition timed "
                            "out for %s", path,
                        )
                        return None
                    # Read current state inside the lock.
                    existing: Optional[DomainMapEntry] = None
                    if path.exists():
                        try:
                            raw = path.read_text(encoding="utf-8")
                            existing = DomainMapEntry.from_dict(
                                json.loads(raw),
                            )
                        except (OSError, json.JSONDecodeError) as exc:
                            logger.debug(
                                "[DomainMap] existing entry "
                                "unreadable, replacing: %s", exc,
                            )
                            existing = None

                    # Merge.
                    merged = self._merge_entry(
                        existing=existing,
                        centroid_hash8=centroid_hash8,
                        theme_label=theme_label,
                        new_files=discovered_files,
                        architectural_role=architectural_role,
                        confidence=confidence,
                        cluster_id=cluster_id,
                        op_id=op_id,
                        now=now,
                        files_cap=files_cap,
                        role_cap=role_cap,
                    )

                    # Atomic write via tempfile + rename.
                    if not self._atomic_write(path, merged):
                        return None
                    return merged
        except Exception as exc:  # noqa: BLE001 -- last-resort
            logger.debug(
                "[DomainMap] record_exploration last-resort "
                "degraded: %s", exc,
            )
            return None

    # ------------------------------------------------------------------
    # Merge logic (pure, NEVER raises)
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_entry(
        *,
        existing: Optional[DomainMapEntry],
        centroid_hash8: str,
        theme_label: str,
        new_files: Tuple[str, ...],
        architectural_role: str,
        confidence: float,
        cluster_id: int,
        op_id: str,
        now: float,
        files_cap: int,
        role_cap: int,
    ) -> DomainMapEntry:
        # theme_label: caller wins if non-empty
        merged_theme = theme_label.strip() if theme_label else (
            existing.theme_label if existing else ""
        )
        # discovered_files: dedup-preserving-order union with new prepended
        merged_files: List[str] = []
        seen: set = set()
        for p in (new_files or ()):
            if not isinstance(p, str) or not p:
                continue
            if p in seen:
                continue
            seen.add(p)
            merged_files.append(p)
        if existing is not None:
            for p in existing.discovered_files:
                if p in seen:
                    continue
                seen.add(p)
                merged_files.append(p)
        merged_files = merged_files[:files_cap]
        # architectural_role: caller wins if non-empty
        if architectural_role and architectural_role.strip():
            merged_role = architectural_role.strip()[:role_cap]
        else:
            merged_role = existing.architectural_role if existing else ""
        # confidence: max(existing, new) -- monotonic
        new_conf = max(0.0, min(1.0, float(confidence or 0.0)))
        existing_conf = existing.confidence if existing else 0.0
        merged_conf = max(existing_conf, new_conf)
        # cluster_id: caller wins if >=0
        merged_cid = (
            cluster_id if cluster_id >= 0
            else (existing.cluster_id if existing else -1)
        )
        # exploration_count: existing + 1
        merged_count = (
            (existing.exploration_count if existing else 0) + 1
        )
        # populated_by_op_id: caller wins if non-empty
        merged_op_id = op_id.strip() if op_id else (
            existing.populated_by_op_id if existing else ""
        )
        return DomainMapEntry(
            centroid_hash8=centroid_hash8,
            cluster_id=merged_cid,
            theme_label=merged_theme,
            discovered_files=tuple(merged_files),
            architectural_role=merged_role,
            confidence=merged_conf,
            last_updated_at=now,
            populated_by_op_id=merged_op_id,
            exploration_count=merged_count,
        )

    # ------------------------------------------------------------------
    # Atomic write helper
    # ------------------------------------------------------------------

    @staticmethod
    def _atomic_write(path: Path, entry: DomainMapEntry) -> bool:
        """Write ``entry`` to ``path`` atomically via tempfile +
        os.replace. Returns True on success. NEVER raises."""
        try:
            payload = json.dumps(
                entry.to_dict(), ensure_ascii=True, sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            logger.debug(
                "[DomainMap] entry serialization failed: %s", exc,
            )
            return False
        tmp_fd = -1
        tmp_path: Optional[str] = None
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp",
                dir=str(path.parent),
            )
            try:
                os.write(tmp_fd, payload.encode("utf-8"))
            finally:
                os.close(tmp_fd)
                tmp_fd = -1
            os.replace(tmp_path, str(path))
            tmp_path = None
            return True
        except OSError as exc:
            logger.debug(
                "[DomainMap] atomic write to %s degraded: %s",
                path, exc,
            )
            return False
        finally:
            # Cleanup orphan tempfile if rename never happened.
            if tmp_fd != -1:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def clear(self) -> int:
        """Test helper. Removes every entry file + lock file
        under the store directory. Returns count removed.
        NEVER raises."""
        removed = 0
        try:
            if not self._dir.is_dir():
                return 0
            with self._lock:
                for entry_path in self._dir.iterdir():
                    if not entry_path.is_file():
                        continue
                    name = entry_path.name
                    if not (
                        name.endswith(_ENTRY_SUFFIX)
                        or name.endswith(".lock")
                    ):
                        continue
                    try:
                        entry_path.unlink()
                        if name.endswith(_ENTRY_SUFFIX):
                            removed += 1
                    except OSError:
                        continue
        except Exception as exc:  # noqa: BLE001 -- defensive
            logger.debug(
                "[DomainMap] clear degraded: %s", exc,
            )
        return removed


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------


_default_store: Optional[DomainMapStore] = None
_default_store_lock = threading.Lock()


def get_default_store(
    project_root: Optional[Path] = None,
) -> Optional[DomainMapStore]:
    """Lazy singleton. First call must supply ``project_root``;
    subsequent calls return the existing instance regardless of
    later root args (project_root is fixed for the process).

    Returns ``None`` only if a first-time construction is
    attempted without ``project_root``."""
    global _default_store
    with _default_store_lock:
        if _default_store is not None:
            return _default_store
        if project_root is None:
            return None
        _default_store = DomainMapStore(project_root=project_root)
        return _default_store


def reset_default_store() -> None:
    """Test helper. Clears the singleton without touching disk."""
    global _default_store
    with _default_store_lock:
        _default_store = None


__all__ = [
    "DOMAIN_MAP_SCHEMA_VERSION",
    "DomainMapEntry",
    "DomainMapStore",
    "domain_map_enabled",
    "domain_map_files_cap",
    "domain_map_lock_timeout_s",
    "domain_map_role_max_chars",
    "get_default_store",
    "register_flags",
    "register_shipped_invariants",
    "reset_default_store",
]


# ---------------------------------------------------------------------------
# Slice 5 -- Module-owned FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry) -> int:  # noqa: ANN001
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except Exception as exc:  # noqa: BLE001 -- defensive
        logger.warning(
            "[DomainMap] register_flags degraded: %s", exc,
        )
        return 0
    target = (
        "backend/core/ouroboros/governance/domain_map_memory.py"
    )
    specs = [
        FlagSpec(
            name="JARVIS_DOMAIN_MAP_ENABLED",
            type=FlagType.BOOL, default=True,
            category=Category.SAFETY,
            source_file=target,
            example="JARVIS_DOMAIN_MAP_ENABLED=true",
            description=(
                "Master switch for cross-session DomainMap "
                "persistence. When off, every public method on "
                "DomainMapStore short-circuits to no-op-safe "
                "defaults. Graduated default-true 2026-05-03 in "
                "Slice 5."
            ),
        ),
        FlagSpec(
            name="JARVIS_DOMAIN_MAP_FILES_CAP",
            type=FlagType.INT, default=64,
            category=Category.CAPACITY,
            source_file=target,
            example="JARVIS_DOMAIN_MAP_FILES_CAP=128",
            description=(
                "Hard cap on discovered_files per DomainMap "
                "entry. Floor 1, ceiling 256. Prevents pathological "
                "explorations from ballooning a single entry."
            ),
        ),
        FlagSpec(
            name="JARVIS_DOMAIN_MAP_ROLE_MAX_CHARS",
            type=FlagType.INT, default=500,
            category=Category.CAPACITY,
            source_file=target,
            example="JARVIS_DOMAIN_MAP_ROLE_MAX_CHARS=1000",
            description=(
                "Hard cap on architectural_role string length at "
                "write time. Floor 32, ceiling 4000. Bounds the "
                "Slice 4 stub + future Venom-round role inference."
            ),
        ),
        FlagSpec(
            name="JARVIS_DOMAIN_MAP_LOCK_TIMEOUT_S",
            type=FlagType.FLOAT, default=5.0,
            category=Category.TIMING,
            source_file=target,
            example="JARVIS_DOMAIN_MAP_LOCK_TIMEOUT_S=10.0",
            description=(
                "Cross-process flock acquisition timeout for "
                "record_exploration's read-modify-write critical "
                "section. Floor 0.1s, ceiling 30.0s."
            ),
        ),
    ]
    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception as exc:  # noqa: BLE001 -- defensive
            logger.debug(
                "[DomainMap] register_flags spec %s skipped: %s",
                spec.name, exc,
            )
    return count


# ---------------------------------------------------------------------------
# Slice 5 -- Module-owned shipped_code_invariants
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Slice 3 invariants: authority allowlist (only
    cross_process_jsonl + the registration contract) + frozen
    DomainMapEntry contract + NEVER-raise IO discipline."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    _ALLOWED = {
        "cross_process_jsonl",
        # Registration contract.
        "flag_registry",
        "shipped_code_invariants",
    }
    _FORBIDDEN = {
        "orchestrator", "phase_runner", "iron_gate",
        "change_engine", "candidate_generator", "providers",
        "doubleword_provider", "urgency_router",
        "auto_action_router", "subagent_scheduler",
        "tool_executor", "semantic_guardian",
        "semantic_firewall", "risk_engine",
    }

    def _validate(
        tree: "_ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                module = node.module or ""
                if "backend." not in module and "governance" not in module:
                    continue
                tail = module.rsplit(".", 1)[-1]
                if tail in _FORBIDDEN:
                    violations.append(
                        f"line {getattr(node, 'lineno', '?')}: "
                        f"forbidden module {module!r}"
                    )
                elif tail not in _ALLOWED:
                    violations.append(
                        f"line {getattr(node, 'lineno', '?')}: "
                        f"unexpected governance import {module!r}"
                    )
            if isinstance(node, _ast.Call):
                if isinstance(node.func, _ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        violations.append(
                            f"line {getattr(node, 'lineno', '?')}: "
                            f"Slice 3 MUST NOT {node.func.id}()"
                        )
        # DomainMapEntry must remain frozen.
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef) and (
                node.name == "DomainMapEntry"
            ):
                # Walk decorators looking for @dataclass(frozen=True)
                frozen_seen = False
                for dec in node.decorator_list:
                    if isinstance(dec, _ast.Call):
                        for kw in dec.keywords:
                            if (
                                kw.arg == "frozen"
                                and isinstance(kw.value, _ast.Constant)
                                and kw.value.value is True
                            ):
                                frozen_seen = True
                if not frozen_seen:
                    violations.append(
                        "DomainMapEntry must be @dataclass"
                        "(frozen=True)"
                    )
        return tuple(violations)

    target = (
        "backend/core/ouroboros/governance/domain_map_memory.py"
    )
    return [
        ShippedCodeInvariant(
            invariant_name="domain_map_memory_authority",
            target_file=target,
            description=(
                "Slice 3 DomainMap authority: imports only "
                "cross_process_jsonl + the registration contract. "
                "DomainMapEntry stays @dataclass(frozen=True). "
                "No exec/eval/compile."
            ),
            validate=_validate,
        ),
    ]
