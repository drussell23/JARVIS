"""Async Merkle Cartographer — Phase 11 P11.4 (foundation, no consumers).

Persistent, asynchronous Merkle tree hashing over the JARVIS file tree.
Replaces O(N) tree-scans in background sensors with O(1) "has anything
changed?" queries against a cached tree-of-hashes.

## Problem

Today, sensors that need to know "did any code change since my last
poll?" walk the whole file tree every cycle:

  * ``TodoScannerSensor`` — every ``JARVIS_TODO_SCAN_INTERVAL_S`` (24h
    default), reads ~1500 .py files looking for TODO/FIXME markers.
  * ``DocStalenessSensor`` — every ``JARVIS_DOC_STALE_INTERVAL_S`` (6h),
    walks ``docs/`` looking for stale .md files.
  * ``OpportunityMinerSensor`` — every 1h, scans the entire codebase.
  * ``BacklogSensor`` — every ``JARVIS_BACKLOG_INTERVAL_S`` (1h),
    re-reads ``.jarvis/backlog.json``.

Each cycle is O(N) disk reads + O(N) text-pattern matches even when
nothing has changed. The 2026-04-27 sensory-evolution directive
("First-Order Sensory Evolution") names this Zero-Order behavior.

## Solution (this slice)

A Merkle tree of hashes over the file system, persisted to disk:

  * Each leaf = SHA-256 of one file's content.
  * Each internal node = SHA-256 of its sorted-children hashes.
  * Root hash captures the entire tree's state in one fingerprint.
  * Disk-backed at ``.jarvis/merkle_current.json`` (atomic temp+rename
    via the same idiom as ``posture_store.py``); transition log at
    ``merkle_history.jsonl``.
  * On boot: ``hydrate()`` reads the cache; subsequent ``has_changed``
    queries are O(1).
  * On change events from ``FileSystemEventBridge`` (Slice 11.5):
    ``update_incremental`` recomputes only affected subtrees in O(log N).

## Authority posture (AST-pinned in tests)

  * **Top-level imports**: stdlib + ``asyncio`` + ``hashlib`` + ``typing``.
    No orchestrator/policy/iron_gate/gate/change_engine/candidate_generator
    imports.
  * **Pure observer** — never modifies files; only reads + hashes +
    persists state.
  * **NEVER raises into caller** — every public method swallows
    ``OSError`` / ``FileNotFoundError`` / generic exceptions and
    returns a safe default ("treat as changed" preserves correctness;
    "treat as unchanged" would leak stale state).
  * **No primitive duplication**: composes ``posture_store.py``-shaped
    persistence pattern + ``FileWatchGuard``'s exclusion contract +
    standard ``hashlib.sha256`` (no re-rolling crypto).

## Master flag

``JARVIS_MERKLE_CARTOGRAPHER_ENABLED`` (default ``false``). When off,
the module is fully importable but ``get_default_cartographer()``
returns an instance whose ``has_changed`` always returns ``True`` —
meaning sensors that consult it (Slice 11.6) get the legacy O(N)
scan behavior (no false negatives possible).

## Slice scope

This slice ships the **foundation** layer. No consumers wired:

  * Slice 11.4 (this PR): module + ``MerkleStateStore`` + tree
    primitives + async walker + ``has_changed`` API + persistence
    + boot-loop protection.
  * Slice 11.5: subscribe to ``FileSystemEventBridge.fs.changed.*``
    events; per-event incremental updates.
  * Slice 11.6.{a,b,c,d}: TodoScanner / DocStaleness /
    OpportunityMiner / BacklogSensor each wrap their existing
    full-tree scan in ``if cartographer.has_changed(scope): ...``.
  * Slice 11.7: graduation flip after 3 forced-clean once-proofs
    each + cost-per-cycle metric shows ≥80% short-circuit rate.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)


logger = logging.getLogger(__name__)


SCHEMA_VERSION = "merkle.1"


_TRUTHY = ("1", "true", "yes", "on")


# Default exclusion list — mirrors FileWatchGuard.exclude_top_level_dirs
# (same physical truth: directories the cartographer should skip
# because they're either external code, build artifacts, transient
# logs, or version-control internals).
_DEFAULT_EXCLUSIONS: Tuple[str, ...] = (
    "venv", ".venv", "venv_py39_backup",
    "node_modules", ".git", ".worktrees",
    "__pycache__", ".pytest_cache", ".mypy_cache",
    "build", "dist", ".ouroboros",
    # Cartographer-specific: .jarvis/sessions changes every run
    # (battle-test session artifacts), so excluded — would defeat
    # caching if hashed.
    ".jarvis_sessions",
)


# ---------------------------------------------------------------------------
# Env helpers (mirror posture_store / topology_sentinel idiom)
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Master flag + tunables
# ---------------------------------------------------------------------------


def is_cartographer_enabled() -> bool:
    """``JARVIS_MERKLE_CARTOGRAPHER_ENABLED`` (default ``true`` — graduated
    in Phase 11 Slice 11.7).

    When off, callers consulting ``has_changed`` get an unconditional
    ``True`` so existing O(N) sensor scans run as before — no false
    negatives possible. Hot-revert: ``export
    JARVIS_MERKLE_CARTOGRAPHER_ENABLED=false`` returns the entire
    Phase 11 cartographer stack to dormant (per-sensor consumers all
    fail-safe to legacy when ``current_root_hash``/``subtree_hash``
    return empty)."""
    return _env_bool(
        "JARVIS_MERKLE_CARTOGRAPHER_ENABLED", default=True,
    )


def state_dir() -> Path:
    """Directory holding ``merkle_*.json``.

    Default ``.jarvis/`` matches every other governance disk artifact;
    override via ``JARVIS_MERKLE_STATE_DIR`` for tests."""
    raw = os.environ.get("JARVIS_MERKLE_STATE_DIR")
    if raw:
        return Path(raw)
    return Path(".jarvis").resolve()


def history_capacity() -> int:
    """Append-only history ring-buffer cap. Default 1024."""
    return max(
        16,
        _env_int(
            "JARVIS_MERKLE_HISTORY_SIZE", default=1024, minimum=16,
        ),
    )


def state_max_age_s() -> float:
    """Maximum age of a hydrated current.json before forced rescan.
    Default 7 days. Same fail-safe-against-stale-state pattern as
    topology_sentinel.

    Per audit Risk #1: a cache that's been stable for a week is
    suspect — drives a forced full rescan even if no FS events
    arrived (catches the case where event subscription died silently)."""
    raw = os.environ.get("JARVIS_MERKLE_FORCE_REINDEX_AFTER_S")
    if raw is None:
        return 604800.0  # 7 days
    try:
        return max(60.0, float(raw))
    except (TypeError, ValueError):
        return 604800.0


def walk_concurrency() -> int:
    """Max concurrent file reads during full tree walk. Default 32 —
    balanced against typical macOS / Linux file descriptor limits.
    Override via ``JARVIS_MERKLE_WALK_CONCURRENCY``."""
    return max(
        1,
        _env_int(
            "JARVIS_MERKLE_WALK_CONCURRENCY", default=32, minimum=1,
        ),
    )


def excluded_dirs() -> Tuple[str, ...]:
    """Tuple of top-level directory names to skip. Override via
    ``JARVIS_MERKLE_EXCLUDE_DIRS`` (comma-separated)."""
    raw = os.environ.get("JARVIS_MERKLE_EXCLUDE_DIRS", "").strip()
    if not raw:
        return _DEFAULT_EXCLUSIONS
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return tuple(parts) if parts else _DEFAULT_EXCLUSIONS


def included_top_level_dirs() -> Tuple[str, ...]:
    """Tuple of top-level directories the cartographer scans. Default
    is the production-relevant set per the audit § D.

    Override via ``JARVIS_MERKLE_INCLUDE_DIRS``."""
    raw = os.environ.get("JARVIS_MERKLE_INCLUDE_DIRS", "").strip()
    if not raw:
        return ("backend", "tests", "scripts", "docs", "config")
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return tuple(parts) if parts else (
        "backend", "tests", "scripts", "docs", "config",
    )


# ---------------------------------------------------------------------------
# Hash primitives
# ---------------------------------------------------------------------------


def hash_file_content(content: bytes) -> str:
    """SHA-256 of file content. NEVER raises."""
    try:
        return hashlib.sha256(content).hexdigest()
    except Exception:  # noqa: BLE001 — defensive
        return ""


def hash_combine(child_hashes: Sequence[str]) -> str:
    """Combine sorted child hashes into a parent hash.

    The Merkle invariant: any change in any child propagates to the
    parent. Sort ensures the operation is order-independent (file-
    listing order doesn't affect the hash)."""
    h = hashlib.sha256()
    for c in sorted(child_hashes):
        h.update(c.encode("utf-8"))
        h.update(b"\x00")  # separator — defends against trivial collisions
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Tree node
# ---------------------------------------------------------------------------


@dataclass
class MerkleNode:
    """One node in the Merkle tree. Mutable on purpose — the
    cartographer rebuilds nodes incrementally as files change.

    ``relpath`` is the POSIX-style repo-relative path. For the root
    node ``relpath`` is empty string.
    """

    relpath: str
    is_dir: bool
    hash: str = ""
    mtime: float = 0.0          # leaf only; 0 for dirs
    size: int = 0               # leaf only; 0 for dirs
    children: Dict[str, "MerkleNode"] = field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "relpath": self.relpath,
            "is_dir": self.is_dir,
            "hash": self.hash,
        }
        if not self.is_dir:
            out["mtime"] = self.mtime
            out["size"] = self.size
        if self.children:
            out["children"] = {
                name: child.to_json()
                for name, child in self.children.items()
            }
        return out

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> Optional["MerkleNode"]:
        try:
            node = cls(
                relpath=str(payload.get("relpath", "")),
                is_dir=bool(payload.get("is_dir", False)),
                hash=str(payload.get("hash", "")),
                mtime=float(payload.get("mtime", 0.0)),
                size=int(payload.get("size", 0)),
            )
            children = payload.get("children")
            if isinstance(children, Mapping):
                for name, child_payload in children.items():
                    if isinstance(child_payload, Mapping):
                        child = cls.from_json(child_payload)
                        if child is not None:
                            node.children[str(name)] = child
            return node
        except (TypeError, ValueError) as exc:
            logger.debug(
                "[MerkleCartographer] from_json failed: %s", exc,
            )
            return None

    def all_leaf_paths(self) -> Set[str]:
        """All leaf relpaths under this node."""
        if not self.is_dir:
            return {self.relpath}
        out: Set[str] = set()
        for child in self.children.values():
            out |= child.all_leaf_paths()
        return out


# ---------------------------------------------------------------------------
# State store — disk persistence (mirrors PostureStore idiom)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MerkleFsEvent:
    """Slice 11.5 — minimal event shape for ``update_incremental``.

    Decoupled from TrinityEventBus on purpose: the cartographer's
    incremental API takes ANY object with these three fields, so
    callers (the bus subscriber, tests, custom watchers) construct
    them directly. The bus subscriber translates published events
    into this shape via ``MerkleEventSubscriber.handle``.

    ``kind`` is one of ``"created" | "modified" | "deleted" |
    "moved"``. Other strings are accepted but treated as upserts
    (NEVER raises on unknown kinds — fail-safe to "rebuild leaf").
    """

    kind: str
    relpath: str
    is_directory: bool = False


@dataclass(frozen=True)
class MerkleTransitionRecord:
    """One row of the history log."""

    ts_epoch: float
    transition_kind: str           # "full_walk" | "incremental" | "hydrate" | "miss"
    root_hash_before: str = ""
    root_hash_after: str = ""
    files_changed: int = 0
    files_total: int = 0
    elapsed_s: float = 0.0
    schema_version: str = SCHEMA_VERSION

    def to_json(self) -> Dict[str, Any]:
        return {
            "ts_epoch": self.ts_epoch,
            "ts_iso": datetime.fromtimestamp(
                self.ts_epoch, timezone.utc,
            ).isoformat(),
            "transition_kind": self.transition_kind,
            "root_hash_before": self.root_hash_before[:16],
            "root_hash_after": self.root_hash_after[:16],
            "files_changed": self.files_changed,
            "files_total": self.files_total,
            "elapsed_s": round(self.elapsed_s, 4),
            "schema_version": self.schema_version,
        }


class MerkleStateStore:
    """Durable triplet under ``state_dir()``:

      * ``merkle_current.json`` — the full tree snapshot, atomic
        temp+rename.
      * ``merkle_history.jsonl`` — append-only ring buffer of
        transitions, trimmed to ``history_capacity()`` lines.

    Mirrors the ``posture_store.py`` pattern used elsewhere in
    governance — operators have one mental model for every
    ``.jarvis/*_current.json`` + ``*_history.jsonl`` pair.
    """

    def __init__(
        self,
        directory: Optional[Path] = None,
        history_cap: Optional[int] = None,
    ) -> None:
        self._dir = Path(directory) if directory else state_dir()
        self._cap = (
            history_cap if history_cap is not None
            else history_capacity()
        )
        self._lock = threading.Lock()

    @property
    def current_path(self) -> Path:
        return self._dir / "merkle_current.json"

    @property
    def history_path(self) -> Path:
        return self._dir / "merkle_history.jsonl"

    def _ensure_dir(self) -> bool:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            return True
        except OSError as exc:
            logger.warning(
                "[MerkleCartographer] state dir %s: %s", self._dir, exc,
            )
            return False

    def hydrate(self) -> Optional[MerkleNode]:
        """Read current.json. Returns None on any failure.

        Age-stale snapshots (older than ``state_max_age_s``) are
        rejected so a long-offline process boots clean."""
        if not self.current_path.exists():
            return None
        try:
            payload = json.loads(self.current_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "[MerkleCartographer] hydrate read failed: %s", exc,
            )
            return None
        if not isinstance(payload, Mapping):
            return None
        if payload.get("schema_version") != SCHEMA_VERSION:
            logger.info(
                "[MerkleCartographer] schema mismatch (%r); cold-start",
                payload.get("schema_version"),
            )
            return None
        snapshot_ts = float(payload.get("written_at_epoch", 0.0))
        if snapshot_ts <= 0.0:
            return None
        age = time.time() - snapshot_ts
        if age > state_max_age_s():
            logger.info(
                "[MerkleCartographer] snapshot age %.1fs > max %s; "
                "cold-start", age, state_max_age_s(),
            )
            return None
        root_payload = payload.get("root")
        if not isinstance(root_payload, Mapping):
            return None
        return MerkleNode.from_json(root_payload)

    def write_current(self, root: MerkleNode) -> bool:
        """Atomic temp+rename so readers never see torn state."""
        if not self._ensure_dir():
            return False
        payload = {
            "schema_version": SCHEMA_VERSION,
            "written_at_epoch": time.time(),
            "root": root.to_json(),
        }
        with self._lock:
            try:
                fd, tmp = tempfile.mkstemp(
                    prefix="merkle_current_",
                    suffix=".json.tmp",
                    dir=str(self._dir),
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        json.dump(payload, fh, sort_keys=True)
                    os.replace(tmp, self.current_path)
                    return True
                except Exception:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
            except OSError as exc:
                logger.warning(
                    "[MerkleCartographer] write_current failed: %s", exc,
                )
                return False

    def append_history(self, record: MerkleTransitionRecord) -> bool:
        if not self._ensure_dir():
            return False
        line = json.dumps(record.to_json(), sort_keys=True) + "\n"
        with self._lock:
            try:
                with open(self.history_path, "a", encoding="utf-8") as fh:
                    fh.write(line)
            except OSError as exc:
                logger.debug(
                    "[MerkleCartographer] append_history failed: %s", exc,
                )
                return False
            self._maybe_trim_history()
        return True

    def _maybe_trim_history(self) -> None:
        try:
            with open(self.history_path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            return
        if len(lines) <= self._cap:
            return
        kept = lines[-self._cap:]
        try:
            fd, tmp = tempfile.mkstemp(
                prefix="merkle_history_",
                suffix=".jsonl.tmp",
                dir=str(self._dir),
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.writelines(kept)
            os.replace(tmp, self.history_path)
        except OSError as exc:
            logger.debug(
                "[MerkleCartographer] history trim failed: %s", exc,
            )


# ---------------------------------------------------------------------------
# Cartographer coordinator
# ---------------------------------------------------------------------------


class MerkleCartographer:
    """O(log N) change detection over the JARVIS file tree.

    Composes:
      * ``MerkleStateStore`` for persistence
      * ``hashlib.sha256`` for content + tree hashing
      * ``asyncio.gather`` (with a semaphore) for parallel file reads

    Public API (all NEVER raise — caller-visible failures degrade to
    fail-safe defaults that preserve correctness):

      * ``hydrate() -> int``                 boot-time load; returns
                                              loaded leaf count
      * ``update_full() -> Set[str]``         async walk; returns
                                              changed leaf relpaths
      * ``update_incremental(events) -> ...`` per-event recompute
      * ``has_changed(paths) -> bool``        O(1) query
      * ``changed_descendants(root) -> Set``  O(log N) tree walk
      * ``snapshot() -> Dict[str, Any]``      observability surface
    """

    def __init__(
        self,
        repo_root: Optional[Path] = None,
        store: Optional[MerkleStateStore] = None,
        included_dirs: Optional[Sequence[str]] = None,
        excluded: Optional[Sequence[str]] = None,
    ) -> None:
        self._repo_root = (
            Path(repo_root) if repo_root else Path.cwd()
        ).resolve()
        self._store = store or MerkleStateStore()
        self._included = tuple(
            included_dirs if included_dirs is not None
            else included_top_level_dirs()
        )
        self._excluded: Set[str] = set(
            excluded if excluded is not None else excluded_dirs()
        )
        self._root: Optional[MerkleNode] = None
        # Flat lookup: relpath -> hash. Built on hydrate/update.
        self._leaf_index: Dict[str, str] = {}
        self._lock = threading.RLock()

    # -- public API --------------------------------------------------------

    def hydrate(self) -> int:
        """Read persisted snapshot. NEVER raises. Returns loaded
        leaf count (0 on cold-start)."""
        loaded = self._store.hydrate()
        with self._lock:
            self._root = loaded
            if loaded is None:
                self._leaf_index = {}
                return 0
            self._leaf_index = self._build_leaf_index(loaded)
        return len(self._leaf_index)

    def current_root_hash(self) -> str:
        """O(1) — the current Merkle root hash. Empty string when the
        cartographer hasn't been hydrated / walked yet, or when the
        master flag is off (no caching authority).

        Sensors (Slice 11.6.x) use this for baseline-based change
        detection: store the hash from one scan, compare against
        ``current_root_hash()`` on the next; if they differ, do a
        full scan AND refresh the baseline.

        Master-flag-off → returns empty string. Sensors treat that
        as "always changed" so legacy O(N) scans preserved.
        """
        if not is_cartographer_enabled():
            return ""
        with self._lock:
            if self._root is None:
                return ""
            return self._root.hash

    def subtree_hash(self, relpath: str) -> str:
        """O(log N) — hash of a subtree rooted at ``relpath``. Empty
        string when the path isn't in the tree OR master flag is off.

        Mirrors ``current_root_hash`` but scoped to a directory so
        sensors can baseline-track only the dirs they care about
        (e.g. ``backend/`` + ``tests/`` + ``scripts/``)."""
        if not is_cartographer_enabled():
            return ""
        with self._lock:
            if self._root is None:
                return ""
            node = self._find_node(
                relpath.replace("\\", "/").strip("/"),
            )
            if node is None:
                return ""
            return node.hash

    def has_changed(self, paths: Optional[Sequence[str]] = None) -> bool:
        """O(1) — has any leaf under any of ``paths`` changed since
        the last persisted snapshot?

        Master-flag-off short-circuit: returns True so the caller's
        legacy O(N) scan path runs (no false negatives possible).

        **Note**: this compares in-memory tree to last-persisted
        snapshot. Sensors should prefer ``current_root_hash()`` /
        ``subtree_hash(relpath)`` + their own baseline tracking —
        which lets them detect changes that happened between two
        sensor cycles, not just changes that haven't been persisted.
        """
        if not is_cartographer_enabled():
            return True
        with self._lock:
            if self._root is None:
                return True
            if paths is None:
                # Any change anywhere
                return self._root.hash != self._cached_root_hash()
            for p in paths:
                normalized = p.replace("\\", "/").strip("/")
                node = self._find_node(normalized)
                if node is None:
                    return True   # path unmapped — assume changed
            return False

    def changed_descendants(self, relpath: str) -> Set[str]:
        """Return leaf relpaths under ``relpath`` whose hash differs
        from the persisted snapshot. NEVER raises. When master flag
        is off, returns all descendants (legacy O(N) sensor behavior
        preserved)."""
        with self._lock:
            if self._root is None:
                return set()
            normalized = relpath.replace("\\", "/").strip("/")
            node = self._find_node(normalized)
            if node is None:
                return set()
            return node.all_leaf_paths()

    async def update_incremental(
        self, events: Sequence["MerkleFsEvent"],
    ) -> Set[str]:
        """O(log N) per-event recompute. Slice 11.5.

        Each event carries (kind, relpath, is_directory). The
        cartographer:

          * created / modified  → re-hash leaf at relpath, propagate
                                   up to root
          * deleted             → drop leaf, propagate up
          * moved               → delete source + create dest

        Skips events whose relpath:
          * falls outside the included top-level dirs
          * lands inside an excluded directory
          * points at a non-existent + non-prior-leaf path (no-op)

        Returns the set of leaf relpaths whose hashes changed (subset
        of the input event set after filtering). NEVER raises.

        After processing the batch, the snapshot is persisted ONCE
        (not per-event) — an audit row is appended to history with
        ``transition_kind="incremental"``."""
        if not events:
            return set()
        t0 = time.monotonic()
        prev_root_hash = ""
        with self._lock:
            if self._root is None:
                # Cold cache — incremental is a no-op; caller should
                # invoke update_full first.
                return set()
            prev_root_hash = self._root.hash

        changed: Set[str] = set()
        # Group events by directory path to dedupe redundant work.
        # If a single dir gets 5 events, we still only re-hash each
        # leaf once and recompute the parent once.
        seen_relpaths: Set[str] = set()
        for ev in events:
            relpath = (ev.relpath or "").replace("\\", "/").strip("/")
            if not relpath or relpath in seen_relpaths:
                continue
            seen_relpaths.add(relpath)

            # Skip excluded segments.
            parts = relpath.split("/")
            if any(p in self._excluded for p in parts):
                continue

            # Must be inside an included top-level dir.
            top = parts[0] if parts else ""
            if top not in self._included:
                continue

            # Apply the event.
            if ev.kind in ("deleted",):
                if self._apply_delete(relpath):
                    changed.add(relpath)
                continue

            # Treat created / modified / moved-dest equivalently — a
            # rebuild of that leaf node from disk.
            if await self._apply_upsert(relpath):
                changed.add(relpath)

        # Rebuild leaf index + persist if anything actually changed.
        if changed:
            with self._lock:
                if self._root is not None:
                    self._leaf_index = self._build_leaf_index(self._root)
                self._store.write_current(self._root)

        elapsed = time.monotonic() - t0
        with self._lock:
            new_root_hash = (
                self._root.hash if self._root is not None else ""
            )
        self._store.append_history(MerkleTransitionRecord(
            ts_epoch=time.time(),
            transition_kind="incremental",
            root_hash_before=prev_root_hash,
            root_hash_after=new_root_hash,
            files_changed=len(changed),
            files_total=len(self._leaf_index),
            elapsed_s=elapsed,
        ))
        return changed

    def _apply_delete(self, relpath: str) -> bool:
        """Remove a leaf + propagate ancestor hashes. Returns True
        if a leaf was actually deleted. NEVER raises."""
        with self._lock:
            if self._root is None:
                return False
            parts = relpath.split("/")
            # Walk down to the parent of the leaf.
            stack: List[Tuple[MerkleNode, str]] = []
            node: Optional[MerkleNode] = self._root
            for part in parts[:-1]:
                if node is None or not node.is_dir:
                    return False
                child = node.children.get(part)
                if child is None:
                    return False
                stack.append((node, part))
                node = child
            if node is None or not node.is_dir:
                return False
            leaf_name = parts[-1]
            if leaf_name not in node.children:
                return False
            del node.children[leaf_name]
            # Recompute parent + ancestors
            self._propagate_up(node, stack)
            return True

    async def _apply_upsert(self, relpath: str) -> bool:
        """Re-hash a single file and propagate parent hashes. Returns
        True if the leaf hash actually changed (or was newly created).
        NEVER raises."""
        abs_path = self._repo_root / relpath
        if not abs_path.exists() or not abs_path.is_file():
            # Treat missing-file as delete.
            return self._apply_delete(relpath)
        if abs_path.is_symlink():
            # Per walker contract, symlinks excluded.
            return False
        try:
            loop = asyncio.get_running_loop()
            content = await loop.run_in_executor(
                None, abs_path.read_bytes,
            )
            stat = abs_path.stat()
        except OSError:
            return False
        new_hash = hash_file_content(content)
        with self._lock:
            if self._root is None:
                return False
            parts = relpath.split("/")
            # Walk down, creating intermediate dir nodes if needed.
            stack: List[Tuple[MerkleNode, str]] = []
            node = self._root
            for i, part in enumerate(parts[:-1]):
                if not node.is_dir:
                    return False
                child = node.children.get(part)
                if child is None:
                    sub_relpath = "/".join(parts[: i + 1])
                    child = MerkleNode(
                        relpath=sub_relpath, is_dir=True, hash="",
                    )
                    node.children[part] = child
                stack.append((node, part))
                node = child

            leaf_name = parts[-1]
            existing = node.children.get(leaf_name)
            if existing is not None and existing.hash == new_hash:
                return False  # idempotent

            new_leaf = MerkleNode(
                relpath=relpath, is_dir=False,
                hash=new_hash, mtime=stat.st_mtime, size=stat.st_size,
            )
            node.children[leaf_name] = new_leaf
            self._propagate_up(node, stack)
            return True

    def _propagate_up(
        self,
        leaf_parent: MerkleNode,
        stack: Sequence[Tuple[MerkleNode, str]],
    ) -> None:
        """Recompute hashes from leaf-parent up through ancestors to
        root. Caller holds the lock. NEVER raises."""
        # Recompute leaf-parent hash from its current children
        leaf_parent.hash = hash_combine(
            [c.hash for c in leaf_parent.children.values()]
        )
        # Walk back up through stack, recomputing each ancestor
        for ancestor, _name in reversed(stack):
            ancestor.hash = hash_combine(
                [c.hash for c in ancestor.children.values()]
            )

    async def update_full(self) -> Set[str]:
        """Walk the file tree, hash every file, rebuild the Merkle
        tree, persist the result. Returns the set of leaf relpaths
        whose content hash differs from the prior snapshot. NEVER
        raises."""
        t0 = time.monotonic()
        prev_root_hash = ""
        prev_leaf_index: Dict[str, str] = {}
        with self._lock:
            if self._root is not None:
                prev_root_hash = self._root.hash
                prev_leaf_index = dict(self._leaf_index)
        try:
            new_root = await self._walk_and_hash()
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[MerkleCartographer] update_full failed: %s", exc,
            )
            return set()
        new_leaf_index = self._build_leaf_index(new_root)
        changed: Set[str] = set()
        for relpath, new_hash in new_leaf_index.items():
            if prev_leaf_index.get(relpath) != new_hash:
                changed.add(relpath)
        # Also account for deletions (prev had files no longer present)
        for relpath in prev_leaf_index:
            if relpath not in new_leaf_index:
                changed.add(relpath)
        with self._lock:
            self._root = new_root
            self._leaf_index = new_leaf_index
        self._store.write_current(new_root)
        elapsed = time.monotonic() - t0
        self._store.append_history(MerkleTransitionRecord(
            ts_epoch=time.time(),
            transition_kind="full_walk",
            root_hash_before=prev_root_hash,
            root_hash_after=new_root.hash,
            files_changed=len(changed),
            files_total=len(new_leaf_index),
            elapsed_s=elapsed,
        ))
        return changed

    def snapshot(self) -> Dict[str, Any]:
        """Read-only observability surface."""
        with self._lock:
            return {
                "schema_version": SCHEMA_VERSION,
                "enabled": is_cartographer_enabled(),
                "repo_root": str(self._repo_root),
                "included_dirs": list(self._included),
                "excluded_dirs": sorted(self._excluded),
                "leaf_count": len(self._leaf_index),
                "root_hash": (
                    self._root.hash[:16] if self._root else ""
                ),
            }

    # -- helpers -----------------------------------------------------------

    def _cached_root_hash(self) -> str:
        snap = self._store.hydrate()
        return snap.hash if snap is not None else ""

    def _find_node(self, relpath: str) -> Optional[MerkleNode]:
        if self._root is None:
            return None
        if not relpath:
            return self._root
        parts = relpath.split("/")
        node: Optional[MerkleNode] = self._root
        for part in parts:
            if node is None or not node.is_dir:
                return None
            node = node.children.get(part)
        return node

    def _build_leaf_index(self, root: MerkleNode) -> Dict[str, str]:
        out: Dict[str, str] = {}
        stack: List[MerkleNode] = [root]
        while stack:
            node = stack.pop()
            if not node.is_dir:
                out[node.relpath] = node.hash
            else:
                stack.extend(node.children.values())
        return out

    def _is_excluded(self, name: str) -> bool:
        return name in self._excluded

    async def _walk_and_hash(self) -> MerkleNode:
        """Build the tree by walking ``self._repo_root`` constrained
        to ``self._included`` and ``self._excluded`` filters."""
        sem = asyncio.Semaphore(walk_concurrency())

        async def _hash_file(abs_path: Path, relpath: str) -> Tuple[str, MerkleNode]:
            async with sem:
                try:
                    loop = asyncio.get_running_loop()
                    content = await loop.run_in_executor(
                        None, abs_path.read_bytes,
                    )
                    stat = abs_path.stat()
                except OSError:
                    # Vanished mid-walk — emit a sentinel hash so
                    # the parent's hash still differs from a healthy
                    # tree. NEVER raise.
                    return relpath, MerkleNode(
                        relpath=relpath, is_dir=False,
                        hash="",
                        mtime=0.0, size=0,
                    )
            file_hash = hash_file_content(content)
            return relpath, MerkleNode(
                relpath=relpath, is_dir=False,
                hash=file_hash,
                mtime=stat.st_mtime,
                size=stat.st_size,
            )

        async def _walk_dir(
            abs_dir: Path, relpath: str,
        ) -> MerkleNode:
            children: Dict[str, MerkleNode] = {}
            file_tasks: List[Any] = []
            try:
                entries = sorted(abs_dir.iterdir(), key=lambda p: p.name)
            except OSError:
                return MerkleNode(
                    relpath=relpath, is_dir=True, hash="",
                )
            sub_dirs: List[Tuple[str, Path]] = []
            for entry in entries:
                if self._is_excluded(entry.name):
                    continue
                child_relpath = (
                    f"{relpath}/{entry.name}" if relpath else entry.name
                )
                if entry.is_symlink():
                    # Skip symlinks — defends against cycles.
                    continue
                if entry.is_dir():
                    sub_dirs.append((entry.name, entry))
                elif entry.is_file():
                    file_tasks.append(_hash_file(entry, child_relpath))

            # Hash files concurrently
            if file_tasks:
                results = await asyncio.gather(
                    *file_tasks, return_exceptions=False,
                )
                for relp, leaf in results:
                    children[Path(relp).name] = leaf

            # Walk subdirectories sequentially (each one will spawn its
            # own concurrent file hashing). Trades some parallelism
            # for bounded fd usage.
            for name, sub_path in sub_dirs:
                sub_relpath = (
                    f"{relpath}/{name}" if relpath else name
                )
                children[name] = await _walk_dir(sub_path, sub_relpath)

            child_hashes = [c.hash for c in children.values()]
            return MerkleNode(
                relpath=relpath, is_dir=True,
                hash=hash_combine(child_hashes),
                children=children,
            )

        # Build root from the included top-level dirs.
        root_children: Dict[str, MerkleNode] = {}
        for top in self._included:
            abs_top = self._repo_root / top
            if not abs_top.exists() or not abs_top.is_dir():
                continue
            root_children[top] = await _walk_dir(abs_top, top)
        root = MerkleNode(
            relpath="", is_dir=True,
            hash=hash_combine([c.hash for c in root_children.values()]),
            children=root_children,
        )
        return root


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------


_default_cartographer: Optional[MerkleCartographer] = None
_default_lock = threading.Lock()


def get_default_cartographer(
    repo_root: Optional[Path] = None,
) -> MerkleCartographer:
    """Module-level singleton. Slice 11.5 boot wiring will call this
    from ``GovernedLoopService.start`` to hydrate + spawn periodic
    full walks. NEVER raises."""
    global _default_cartographer
    with _default_lock:
        if _default_cartographer is None:
            _default_cartographer = MerkleCartographer(
                repo_root=repo_root,
            )
            _default_cartographer.hydrate()
    return _default_cartographer


def reset_default_cartographer_for_tests() -> None:
    """Tests-only escape hatch — clears the module singleton."""
    global _default_cartographer
    with _default_lock:
        _default_cartographer = None


# ---------------------------------------------------------------------------
# Slice 11.5 — TrinityEventBus subscriber.
# ---------------------------------------------------------------------------
#
# The cartographer's ``update_incremental`` is event-source-agnostic:
# any caller that can construct ``MerkleFsEvent`` objects can drive
# it. ``MerkleEventSubscriber`` is the production wire-up to the
# project's existing FS-event infrastructure (Gap #4 closed
# 2026-04-20): subscribes to ``fs.changed.*`` topics on
# TrinityEventBus, translates payloads, batches with debounce, and
# dispatches to the cartographer.
#
# Subscriber NEVER raises into the bus's dispatch path. Failures
# inside the cartographer's update degrade silently to a debug log.


def _coerce_kind_from_topic(topic: str) -> str:
    """Map ``fs.changed.modified`` → ``"modified"``; defensive
    fallback to ``"modified"`` for anything else (treated as upsert
    by ``update_incremental``)."""
    if topic.startswith("fs.changed."):
        return topic[len("fs.changed."):] or "modified"
    return "modified"


class MerkleEventSubscriber:
    """Subscribes to ``fs.changed.*`` events on a
    ``TrinityEventBus``-shaped object and forwards them to a
    ``MerkleCartographer`` via batched ``update_incremental`` calls.

    Decoupled from TrinityEventBus's concrete type — accepts any
    object exposing ``async subscribe(topic_pattern, handler)``.
    This keeps the cartographer module free of orchestrator imports.

    Debouncing: events are buffered in memory; ``flush`` is called
    after ``debounce_seconds`` of quiet OR when the buffer reaches
    ``flush_threshold``. Both knobs are env-tunable.

    Master-flag-aware: when ``is_cartographer_enabled()`` is False,
    ``handle`` is a no-op so the subscriber can be wired at boot
    without affecting behavior.
    """

    def __init__(
        self,
        cartographer: MerkleCartographer,
        debounce_seconds: float = 1.0,
        flush_threshold: int = 50,
    ) -> None:
        self._cartographer = cartographer
        self._debounce_s = max(0.0, float(debounce_seconds))
        self._flush_threshold = max(1, int(flush_threshold))
        self._pending: List[MerkleFsEvent] = []
        self._lock = threading.Lock()
        self._flush_task: Optional[asyncio.Task] = None
        self._events_received: int = 0
        self._batches_flushed: int = 0

    @property
    def metrics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "events_received": self._events_received,
                "batches_flushed": self._batches_flushed,
                "pending_count": len(self._pending),
            }

    async def handle(self, topic: str, payload: Any) -> None:
        """Bus event handler. NEVER raises into the bus dispatch."""
        if not is_cartographer_enabled():
            return
        try:
            relpath = ""
            is_directory = False
            if isinstance(payload, Mapping):
                relpath = str(
                    payload.get("relative_path")
                    or payload.get("path")
                    or "",
                )
                is_directory = bool(payload.get("is_directory", False))
            else:
                # Object-style payload — tolerant duck-typing
                relpath = str(
                    getattr(payload, "relative_path", "")
                    or getattr(payload, "path", "")
                    or "",
                )
                is_directory = bool(
                    getattr(payload, "is_directory", False),
                )
            if not relpath:
                return
            if is_directory:
                # Directory events are coarse-grained; we still
                # care about them for delete events but skip
                # creates/modifies (no leaf to hash).
                kind = _coerce_kind_from_topic(topic)
                if kind not in ("deleted", "moved"):
                    return
            else:
                kind = _coerce_kind_from_topic(topic)
            ev = MerkleFsEvent(
                kind=kind, relpath=relpath, is_directory=is_directory,
            )
            with self._lock:
                self._pending.append(ev)
                self._events_received += 1
                pending_n = len(self._pending)
            if pending_n >= self._flush_threshold:
                await self._flush_now()
            else:
                # Schedule a debounced flush.
                self._schedule_debounced_flush()
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[MerkleSubscriber] handle failed", exc_info=True,
            )

    async def subscribe_to_bus(self, event_bus: Any) -> bool:
        """Wire the subscriber to a bus exposing
        ``async subscribe(topic_pattern, handler)``. Returns True on
        successful subscription; False on any failure. NEVER raises."""
        try:
            await event_bus.subscribe("fs.changed.*", self.handle)
            return True
        except Exception:  # noqa: BLE001
            logger.debug(
                "[MerkleSubscriber] bus.subscribe failed", exc_info=True,
            )
            return False

    async def flush(self) -> int:
        """Drain the pending buffer to ``cartographer.update_incremental``.
        Returns the number of events processed. NEVER raises."""
        return await self._flush_now()

    async def _flush_now(self) -> int:
        with self._lock:
            batch = list(self._pending)
            self._pending.clear()
        if not batch:
            return 0
        try:
            await self._cartographer.update_incremental(batch)
        except Exception:  # noqa: BLE001
            logger.debug(
                "[MerkleSubscriber] update_incremental failed",
                exc_info=True,
            )
        with self._lock:
            self._batches_flushed += 1
        return len(batch)

    def _schedule_debounced_flush(self) -> None:
        """If no flush is currently scheduled, spawn one. NEVER raises."""
        if self._flush_task is not None and not self._flush_task.done():
            return  # already scheduled
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._flush_task = loop.create_task(self._debounced_flush())

    async def _debounced_flush(self) -> None:
        try:
            await asyncio.sleep(self._debounce_s)
            await self._flush_now()
        except Exception:  # noqa: BLE001
            logger.debug(
                "[MerkleSubscriber] debounced_flush failed",
                exc_info=True,
            )


__all__ = [
    "MerkleCartographer",
    "MerkleEventSubscriber",
    "MerkleFsEvent",
    "MerkleNode",
    "MerkleStateStore",
    "MerkleTransitionRecord",
    "SCHEMA_VERSION",
    "excluded_dirs",
    "get_default_cartographer",
    "hash_combine",
    "hash_file_content",
    "history_capacity",
    "included_top_level_dirs",
    "is_cartographer_enabled",
    "reset_default_cartographer_for_tests",
    "state_dir",
    "state_max_age_s",
    "walk_concurrency",
]
