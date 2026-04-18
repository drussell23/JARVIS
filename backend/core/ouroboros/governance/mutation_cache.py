"""MutationCache — content-hash-keyed cache of enumerated mutants and
per-mutant pytest outcomes. Makes APPLY-phase mutation gating tractable
by amortizing the two expensive steps:

  1. **Catalog enumeration** — ``ast.parse`` + ``copy.deepcopy`` +
     ``ast.unparse`` for every mutation site. For a 400-line file with
     ~30 sites, this is O(100ms) per enumeration. Cacheable by SUT
     content hash.
  2. **Outcome execution** — per-mutant pytest subprocess. For
     Session W's 28 mutants × ~5s pytest = 144s wall time. Cacheable by
     (SUT content hash, test suite content hash) — if neither side
     changed, the outcome can't have changed either.

Two-tier cache:

  * **in-memory LRU** (hot path) — O(1) lookup. Defaults to 10 entries
    per cache to keep RAM bounded (~1MB per catalog worst case).
  * **disk backing** (cross-session) — JSON files under
    ``.jarvis/mutation_cache/catalog/`` and ``.jarvis/mutation_cache/outcomes/``.
    Survives process restart; invalidated automatically by content
    hash mismatch.

**Authority invariant**: this module is a pure cache. It never mutates
governance state, never raises on miss (just returns ``None``), never
blocks a phase. Consumers are free to bypass the cache entirely.

**Scope caveats**:
  * Cache hits depend on byte-level content equality of both SUT and
    test files. A whitespace-only edit invalidates the cache — which
    is correct (the AST walk changes) but can be surprising.
  * The catalog cache is safe across O+V commits (SHA-256 of file
    bytes is stable). The outcome cache assumes pytest is deterministic
    between invocations. Tests that depend on wall-clock, random state
    without fixed seeds, or environment variables will produce
    stale-but-cached verdicts. Callers that need fresh verdicts should
    call ``invalidate_outcomes()``.
  * No cache coherence across concurrent writers. V1 assumes one
    mutation-testing process per file at a time.

Env gates:

    JARVIS_MUTATION_CACHE_DIR       default ".jarvis/mutation_cache"
    JARVIS_MUTATION_CACHE_MAX_RAM   in-memory LRU size, default 10
    JARVIS_MUTATION_CACHE_DISABLED  emergency kill switch, default 0
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from collections import OrderedDict
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from backend.core.ouroboros.governance.mutation_tester import Mutant


logger = logging.getLogger("Ouroboros.MutationCache")

_ENV_DIR = "JARVIS_MUTATION_CACHE_DIR"
_ENV_RAM = "JARVIS_MUTATION_CACHE_MAX_RAM"
_ENV_DISABLED = "JARVIS_MUTATION_CACHE_DISABLED"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _cache_disabled() -> bool:
    return os.environ.get(_ENV_DISABLED, "0").strip().lower() in _TRUTHY


def _cache_dir() -> Path:
    return Path(os.environ.get(_ENV_DIR, ".jarvis/mutation_cache"))


def _ram_limit() -> int:
    try:
        return max(1, min(200, int(os.environ.get(_ENV_RAM, "10"))))
    except (TypeError, ValueError):
        return 10


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def file_hash(path: Path) -> str:
    """Stable SHA-256 of file content. Returns empty string on read error."""
    try:
        data = path.read_bytes()
    except Exception:  # noqa: BLE001
        return ""
    return hashlib.sha256(data).hexdigest()


def files_composite_hash(paths: Iterable[Path]) -> str:
    """Composite hash of a set of files. Order-independent (sorted by str)."""
    parts: List[str] = []
    for p in sorted(paths, key=lambda x: str(x)):
        h = file_hash(p)
        parts.append(f"{p}:{h}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Mutant (de)serialization
# ---------------------------------------------------------------------------


def _mutant_to_dict(m: Mutant) -> Dict:
    return asdict(m)


def _mutant_from_dict(d: Dict) -> Mutant:
    return Mutant(
        op=d["op"],
        source_file=d["source_file"],
        line=int(d["line"]),
        col=int(d["col"]),
        original=d["original"],
        mutated=d["mutated"],
        patched_src=d["patched_src"],
    )


# ---------------------------------------------------------------------------
# Catalog cache — (sut_hash) → List[Mutant]
# ---------------------------------------------------------------------------


class _LRU:
    """Minimal thread-safe LRU on top of OrderedDict."""

    def __init__(self, maxsize: int) -> None:
        self._max = maxsize
        self._data: "OrderedDict[str, object]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            val = self._data.get(key)
            if val is not None:
                self._data.move_to_end(key)
            return val

    def put(self, key: str, val: object) -> None:
        with self._lock:
            self._data[key] = val
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


_catalog_lru = _LRU(_ram_limit())
_outcome_lru = _LRU(_ram_limit())


def _catalog_path(sut_hash: str) -> Path:
    return _cache_dir() / "catalog" / f"{sut_hash}.json"


def _outcome_path(sut_hash: str, tests_hash: str) -> Path:
    return _cache_dir() / "outcomes" / f"{sut_hash}_{tests_hash}.json"


def get_catalog(sut_path: Path) -> Tuple[str, Optional[List[Mutant]]]:
    """Return (sut_hash, cached_catalog_or_None).

    The hash is always computed (needed for the next cache write).
    Callers who get ``None`` should enumerate and call ``put_catalog``.
    """
    sut_hash = file_hash(sut_path)
    if not sut_hash or _cache_disabled():
        return sut_hash, None
    hit = _catalog_lru.get(sut_hash)
    if hit is not None:
        return sut_hash, list(hit)  # type: ignore[arg-type]
    disk = _catalog_path(sut_hash)
    if disk.is_file():
        try:
            payload = json.loads(disk.read_text(encoding="utf-8"))
            mutants = [_mutant_from_dict(d) for d in payload.get("mutants", [])]
            _catalog_lru.put(sut_hash, tuple(mutants))
            return sut_hash, mutants
        except Exception:  # noqa: BLE001
            logger.debug(
                "[MutationCache] catalog read failed %s", disk, exc_info=True,
            )
    return sut_hash, None


def put_catalog(sut_hash: str, mutants: List[Mutant]) -> None:
    if not sut_hash or _cache_disabled():
        return
    _catalog_lru.put(sut_hash, tuple(mutants))
    try:
        path = _catalog_path(sut_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sut_hash": sut_hash,
            "n_mutants": len(mutants),
            "mutants": [_mutant_to_dict(m) for m in mutants],
        }
        path.write_text(
            json.dumps(payload, sort_keys=False), encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[MutationCache] catalog write failed", exc_info=True,
        )


# ---------------------------------------------------------------------------
# Outcome cache — (sut_hash, tests_hash) → {mutant_key → "caught"|"survived"|...}
# ---------------------------------------------------------------------------


def get_outcomes(
    sut_hash: str, tests_hash: str,
) -> Optional[Dict[str, str]]:
    """Return the full outcome map for this (SUT, tests) combination, or None."""
    if not sut_hash or not tests_hash or _cache_disabled():
        return None
    key = f"{sut_hash}:{tests_hash}"
    hit = _outcome_lru.get(key)
    if hit is not None:
        return dict(hit)  # type: ignore[arg-type]
    disk = _outcome_path(sut_hash, tests_hash)
    if disk.is_file():
        try:
            payload = json.loads(disk.read_text(encoding="utf-8"))
            outcomes = dict(payload.get("outcomes", {}))
            _outcome_lru.put(key, outcomes)
            return outcomes
        except Exception:  # noqa: BLE001
            logger.debug(
                "[MutationCache] outcomes read failed %s", disk, exc_info=True,
            )
    return None


def put_outcomes(
    sut_hash: str, tests_hash: str, outcomes: Dict[str, str],
) -> None:
    if not sut_hash or not tests_hash or _cache_disabled():
        return
    key = f"{sut_hash}:{tests_hash}"
    _outcome_lru.put(key, dict(outcomes))
    try:
        path = _outcome_path(sut_hash, tests_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sut_hash": sut_hash,
            "tests_hash": tests_hash,
            "n_outcomes": len(outcomes),
            "outcomes": outcomes,
        }
        path.write_text(
            json.dumps(payload, sort_keys=True), encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[MutationCache] outcomes write failed", exc_info=True,
        )


# ---------------------------------------------------------------------------
# Invalidation helpers (operator-facing)
# ---------------------------------------------------------------------------


def invalidate_catalog(sut_path: Path) -> None:
    sut_hash = file_hash(sut_path)
    _catalog_lru.clear()  # coarse — keeps the code simple
    path = _catalog_path(sut_hash)
    if path.is_file():
        try:
            path.unlink()
        except Exception:  # noqa: BLE001
            logger.debug(
                "[MutationCache] invalidate_catalog unlink failed",
                exc_info=True,
            )


def invalidate_outcomes() -> None:
    """Drop every cached outcome. Catalog entries are preserved."""
    _outcome_lru.clear()
    root = _cache_dir() / "outcomes"
    if root.is_dir():
        for child in root.iterdir():
            try:
                child.unlink()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[MutationCache] outcomes unlink failed %s",
                    child, exc_info=True,
                )


def cache_stats() -> Dict[str, int]:
    """For /status-style telemetry."""
    return {
        "catalog_ram": len(_catalog_lru),
        "outcomes_ram": len(_outcome_lru),
    }


__all__ = [
    "cache_stats",
    "file_hash",
    "files_composite_hash",
    "get_catalog",
    "get_outcomes",
    "invalidate_catalog",
    "invalidate_outcomes",
    "put_catalog",
    "put_outcomes",
]
