"""Targeted Locality Bounding + Epistemic Humility for the OperationAdvisor.

Root cause (soak bt-2026-07-21-205755)
--------------------------------------
On a COLD cache in a fresh worktree the Advisor's global importer scan
(``operation_advisor._compute_blast_radius`` and its cooperative async
twin) burned its entire wall-clock budget traversing the tree --
``blast_radius_scan_budget_exhausted ... files_examined=1
elapsed_ms=39189.8`` -- then FABRICATED ``importers =
conservative_cap (50)`` and presented it downstream as evidence.
Three compounding defects:

1. The fabricated cap was written into the shared TTL cache, poisoning
   every subsequent op on the same ``(targets, root)`` key.
2. The cooperative async path injected the fabricated value into
   ``advise()`` WITHOUT ``_blast_is_synthetic=True``, so fabricated
   data could satisfy the hard-BLOCK predicates that Slice 21 Fix B
   explicitly fenced synthetic values out of.
3. Every subsequent cold op re-paid the full 40s traversal burn.

The structural repair (this module + operation_advisor seams)
-------------------------------------------------------------
* **Targeted Locality Bounding** -- when the global O(N) scan exhausts
  its budget, the Advisor pivots to a bounded O(K) localized search:
  the immediate package neighborhoods of the target files, the
  directories of their DIRECT importees (resolved via the canonical
  ``reverse_dep_resolver.extract_module_imports`` -- no duplicated
  graph-walking logic), and the conventional test directories. The
  result is an honest MEASURED LOWER BOUND with explicit provenance.
* **Epistemic Humility** -- when even the localized scan cannot
  resolve (severe cache coldness), the Advisor reports provenance
  ``unknown`` with a NEUTRAL count (0), refuses to fabricate a risk
  payload, and records an escalation in the epistemic ledger below so
  BOTH GATE paths floor the op at NOTIFY_APPLY (operator-visible,
  never an unappealable block).
* **Cold-root memo** -- the first budget exhaustion per scan root is
  remembered (TTL-bounded) so subsequent ops skip straight to the
  cheap localized path instead of re-paying the global burn.

Authority notes: this module carries TELEMETRY + degradation strategy
state only. It never blocks, never mutates, and the GATE floor built
on its ledger is stricter-wins / fail-soft -- Iron Gate, VALIDATE,
SemanticGuardian and the risk tiers keep every real guarantee.

Constraints: Python 3.9+, ASCII-only, ``from __future__ import
annotations``, zero hardcoded models, env-var driven knobs.
"""

from __future__ import annotations

import ast as _ast
import collections
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

# DRY -- reuse the canonical bounded traversal + env-knob primitives.
from backend.core.ouroboros.governance.bounded_walker import (
    _env_float,
    _env_int,
    bounded_read_text,
    default_skip_dirs,
    iter_bounded_files,
)

# DRY -- reuse the canonical import extractor + module<->path grammar
# from the reverse-dependency resolver (Slice 6 factored these out as
# THE single implementation; we only query, we build no parallel graph).
from backend.core.ouroboros.governance.reverse_dep_resolver import (
    _module_from_relpath,
    _test_dir_names,
    extract_module_imports,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Blast-radius provenance vocabulary (closed set)
# ---------------------------------------------------------------------------

#: Full-fidelity evidence: complete global scan, oracle graph, or
#: call-graph BFS. May satisfy hard-BLOCK predicates.
PROVENANCE_MEASURED: str = "measured"

#: Honest partial evidence: a bounded localized scan of the target's
#: dependency neighborhood. A LOWER BOUND on the true importer count --
#: still real measurement, so it may satisfy hard-BLOCK predicates
#: (same-or-sharper: a neighborhood already showing >= N importers is
#: proof of >= N importers globally).
PROVENANCE_LOCAL_LOWER_BOUND: str = "localized_lower_bound"

#: No evidence: both the global and localized scans failed to resolve.
#: The count is a NEUTRAL 0, contributes no risk factor, can never
#: satisfy a BLOCK predicate, and triggers the NOTIFY_APPLY floor.
PROVENANCE_UNKNOWN: str = "unknown"

#: A placeholder injected with no scan performed (Slice 12T
#: background-tier skip, or the legacy exhaustion cap when locality
#: bounding is master-OFF). May contribute caution, never a BLOCK
#: (Slice 21 Fix B discipline).
PROVENANCE_SYNTHETIC: str = "synthetic_cap"

#: Provenances that constitute REAL measurement (hard-BLOCK eligible).
MEASURED_PROVENANCES: Tuple[str, ...] = (
    PROVENANCE_MEASURED,
    PROVENANCE_LOCAL_LOWER_BOUND,
)


# ---------------------------------------------------------------------------
# Env knobs (all defaults overridable; no hardcoded behavior)
# ---------------------------------------------------------------------------

LOCALITY_BOUNDING_ENABLED_ENV_VAR: str = (
    "JARVIS_ADVISOR_LOCALITY_BOUNDING_ENABLED"
)
EPISTEMIC_NOTIFY_ENABLED_ENV_VAR: str = (
    "JARVIS_ADVISOR_EPISTEMIC_NOTIFY_ENABLED"
)


def locality_bounding_enabled() -> bool:
    """Master flag for the localized fallback pivot (default TRUE).

    OFF restores the legacy budget-exhaustion behavior EXCEPT that the
    fabricated cap is now correctly labeled ``synthetic_cap`` so it can
    no longer satisfy hard-BLOCK predicates -- that mislabeling was a
    bug (the Slice 21 Fix B bypass), not behavior worth preserving.
    """
    raw = os.environ.get(
        LOCALITY_BOUNDING_ENABLED_ENV_VAR, "true",
    ).strip().lower()
    return raw not in ("false", "0", "no", "off")


def epistemic_notify_enabled() -> bool:
    """Master flag for the GATE-side NOTIFY_APPLY floor on
    epistemically-unknown blast radius (default TRUE)."""
    raw = os.environ.get(
        EPISTEMIC_NOTIFY_ENABLED_ENV_VAR, "true",
    ).strip().lower()
    return raw not in ("false", "0", "no", "off")


def locality_timeout_s() -> float:
    """``JARVIS_ADVISOR_LOCALITY_TIMEOUT_S`` -- default 5.0. Shared
    wall-clock budget across ALL locality roots (the O(K) bound)."""
    return _env_float("JARVIS_ADVISOR_LOCALITY_TIMEOUT_S", 5.0)


def locality_max_scanned() -> int:
    """``JARVIS_ADVISOR_LOCALITY_MAX_SCANNED`` -- default 4000. Total
    candidate-file yield ceiling across all locality roots."""
    return _env_int("JARVIS_ADVISOR_LOCALITY_MAX_SCANNED", 4000)


def locality_max_roots() -> int:
    """``JARVIS_ADVISOR_LOCALITY_MAX_ROOTS`` -- default 16. Ceiling on
    the number of neighborhood directories derived per op."""
    return _env_int("JARVIS_ADVISOR_LOCALITY_MAX_ROOTS", 16)


def cold_root_ttl_s() -> float:
    """``JARVIS_ADVISOR_COLD_ROOT_TTL_S`` -- default 300. How long a
    budget-exhausted scan root is remembered as cold (subsequent ops
    skip the global scan and go straight to the localized path)."""
    return _env_float("JARVIS_ADVISOR_COLD_ROOT_TTL_S", 300.0)


# ---------------------------------------------------------------------------
# Cold-root memo -- "we already proved this tree is too cold to walk"
# ---------------------------------------------------------------------------

_COLD_ROOTS_LOCK = threading.Lock()
_COLD_ROOTS: "collections.OrderedDict[str, float]" = collections.OrderedDict()
_COLD_ROOTS_MAX_ENTRIES: int = 64


def note_cold_root(scan_root: "Path | str") -> None:
    """Record that a global scan of ``scan_root`` exhausted its budget.

    TTL-bounded + FIFO-capped; thread-safe; NEVER raises.
    """
    try:
        key = str(scan_root)
        now = time.monotonic()
        with _COLD_ROOTS_LOCK:
            _COLD_ROOTS.pop(key, None)
            _COLD_ROOTS[key] = now
            while len(_COLD_ROOTS) > _COLD_ROOTS_MAX_ENTRIES:
                _COLD_ROOTS.popitem(last=False)
    except Exception:  # noqa: BLE001 -- memo is advisory, never fatal
        pass


def is_cold_root(scan_root: "Path | str") -> bool:
    """True when ``scan_root`` recently exhausted a global scan budget
    (within ``cold_root_ttl_s()``). Thread-safe; NEVER raises."""
    try:
        key = str(scan_root)
        now = time.monotonic()
        with _COLD_ROOTS_LOCK:
            stamped = _COLD_ROOTS.get(key)
            if stamped is None:
                return False
            if now - stamped >= cold_root_ttl_s():
                _COLD_ROOTS.pop(key, None)
                return False
            return True
    except Exception:  # noqa: BLE001
        return False


def _reset_cold_roots_for_tests() -> None:
    """Test seam -- clear the memo between test cases."""
    with _COLD_ROOTS_LOCK:
        _COLD_ROOTS.clear()


# ---------------------------------------------------------------------------
# Epistemic ledger -- telemetry consumed by the GATE NOTIFY_APPLY floor
# ---------------------------------------------------------------------------

_EPISTEMIC_LEDGER_LOCK = threading.Lock()
_EPISTEMIC_LEDGER: "collections.OrderedDict[str, Dict[str, Any]]" = (
    collections.OrderedDict()
)
_EPISTEMIC_LEDGER_MAX_ENTRIES: int = 256

#: The only escalation value the GATE floor honors (closed vocabulary).
ESCALATION_NOTIFY_APPLY: str = "notify_apply"


def record_blast_epistemics(
    op_id: str,
    *,
    provenance: str,
    escalation: str = "",
    detail: str = "",
) -> None:
    """Record the epistemic state of an op's blast-radius evidence.

    Written by ``OperationAdvisor.advise`` when provenance is
    ``unknown`` on a mutating op; read (non-destructively) by
    ``_advisor_epistemic_notify_floor`` on BOTH GATE paths. Bounded
    FIFO + thread-safe; NEVER raises. Telemetry only -- carries no
    authority of its own.
    """
    if not op_id:
        return
    try:
        with _EPISTEMIC_LEDGER_LOCK:
            _EPISTEMIC_LEDGER.pop(op_id, None)
            _EPISTEMIC_LEDGER[op_id] = {
                "provenance": provenance,
                "escalation": escalation,
                "detail": detail,
                "ts_monotonic": time.monotonic(),
            }
            while len(_EPISTEMIC_LEDGER) > _EPISTEMIC_LEDGER_MAX_ENTRIES:
                _EPISTEMIC_LEDGER.popitem(last=False)
    except Exception:  # noqa: BLE001 -- ledger is telemetry, never fatal
        pass


def peek_blast_epistemics(op_id: str) -> Optional[Dict[str, Any]]:
    """Non-destructive read of an op's epistemic record (or ``None``).

    Non-destructive so GENERATE retries / both GATE twins observe the
    same state. NEVER raises.
    """
    if not op_id:
        return None
    try:
        with _EPISTEMIC_LEDGER_LOCK:
            rec = _EPISTEMIC_LEDGER.get(op_id)
            return dict(rec) if rec is not None else None
    except Exception:  # noqa: BLE001
        return None


def _reset_epistemic_ledger_for_tests() -> None:
    """Test seam -- clear the ledger between test cases."""
    with _EPISTEMIC_LEDGER_LOCK:
        _EPISTEMIC_LEDGER.clear()


# ---------------------------------------------------------------------------
# Locality-root derivation (the O(K) neighborhood)
# ---------------------------------------------------------------------------

def _resolve_module_to_dir(module: str, scan_root: Path) -> Optional[Path]:
    """Resolve a dotted import target to the DIRECTORY holding it,
    under ``scan_root``. Deterministic path construction -- no rglob.

    ``a.b.c`` -> ``a/b/c.py``'s parent, or package dir ``a/b/c`` when
    ``a/b/c/__init__.py`` exists. Returns ``None`` when neither shape
    exists on disk (stdlib / third-party import -- outside the tree).
    """
    if not module:
        return None
    rel = module.replace(".", "/")
    try:
        as_file = scan_root / (rel + ".py")
        if as_file.is_file():
            return as_file.parent
        as_pkg = scan_root / rel / "__init__.py"
        if as_pkg.is_file():
            return as_pkg.parent
    except OSError:
        return None
    return None


def _direct_importee_dirs(
    target_path: Path,
    rel_path: str,
    scan_root: Path,
    *,
    max_bytes: int,
) -> Set[Path]:
    """Directories of the modules a target file DIRECTLY imports.

    AST-parses the target (bounded read) and resolves each absolute
    dotted import via deterministic path construction. Reuses THE
    canonical ``extract_module_imports`` -- zero duplicated
    graph-walking logic. Fail-soft: any parse/read error -> empty set.
    """
    dirs: Set[Path] = set()
    try:
        source = bounded_read_text(target_path, max_bytes=max_bytes)
        if source is None:
            return dirs
        module = _module_from_relpath(rel_path)
        if not module:
            return dirs
        is_init = rel_path == "__init__.py" or rel_path.endswith("/__init__.py")
        tree = _ast.parse(source, filename=str(target_path))
        for imported in extract_module_imports(tree, module, is_init):
            resolved = _resolve_module_to_dir(imported, scan_root)
            if resolved is not None:
                dirs.add(resolved)
    except (SyntaxError, ValueError, OSError, UnicodeDecodeError):
        return dirs
    except Exception:  # noqa: BLE001 -- locality derivation is fail-soft
        return dirs
    return dirs


def derive_locality_roots(
    target_files: Sequence[str],
    scan_root: Path,
    *,
    max_roots: Optional[int] = None,
    per_file_bytes: int = 65_536,
) -> Tuple[Path, ...]:
    """Derive the bounded O(K) neighborhood for a localized importer scan.

    The neighborhood of a work unit is:

    1. The parent directory of each target file (its package -- where
       sibling importers live),
    2. The directories of each target's DIRECT importees (modules the
       target couples to -- bidirectional coupling means importers
       cluster there too),
    3. The conventional test directories under ``scan_root`` (importers
       of a module are very commonly its tests), including ``tests``
       siblings of each target's package.

    Deterministic: sorted, deduped, ancestor-collapsed (a root nested
    under another kept root is dropped), capped at ``max_roots``.
    ``scan_root`` itself is NEVER a locality root -- that would be the
    global scan again. Returns ``()`` when nothing resolves (the
    caller treats that as epistemically unresolved). NEVER raises.
    """
    cap = max_roots if max_roots is not None else locality_max_roots()
    try:
        root = scan_root.resolve()
    except OSError:
        return ()

    candidates: Set[Path] = set()
    try:
        for tf in target_files:
            raw = str(tf).replace("\\", "/")
            path = Path(raw)
            if not path.is_absolute():
                path = root / raw
            try:
                path = path.resolve()
            except OSError:
                continue
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue  # escapes the scan root -- not our neighborhood
            rel_str = str(rel).replace("\\", "/")
            parent = path.parent
            if parent != root:
                candidates.add(parent)
            # tests sibling of the target's package
            for tdn in sorted(_test_dir_names()):
                sib = parent / tdn
                try:
                    if sib.is_dir() and sib != root:
                        candidates.add(sib)
                except OSError:
                    continue
            if rel_str.endswith(".py"):
                candidates.update(
                    d for d in _direct_importee_dirs(
                        path, rel_str, root, max_bytes=per_file_bytes,
                    )
                    if d != root
                )
        # Conventional top-level test dirs.
        for tdn in sorted(_test_dir_names()):
            tdir = root / tdn
            try:
                if tdir.is_dir():
                    candidates.add(tdir)
            except OSError:
                continue
    except Exception:  # noqa: BLE001 -- fail-soft: partial set is fine
        pass

    if not candidates:
        return ()

    # Ancestor-collapse: drop any root nested under another kept root
    # (a shared-budget walk of the ancestor already covers it).
    ordered = sorted(candidates, key=lambda p: (len(p.parts), str(p)))
    kept: List[Path] = []
    for cand in ordered:
        nested = False
        for k in kept:
            try:
                cand.relative_to(k)
                nested = True
                break
            except ValueError:
                continue
        if not nested:
            kept.append(cand)

    return tuple(kept[: max(1, cap)])


def iter_locality_files(
    roots: Sequence[Path],
    *,
    max_scanned: Optional[int] = None,
    timeout_s: Optional[float] = None,
    skip_dirs: Optional[Set[str]] = None,
) -> Iterator[str]:
    """Yield candidate file paths across ``roots`` under a SHARED budget.

    Composes the canonical ``iter_bounded_files`` per root: one global
    wall-clock ceiling (``timeout_s``) and one global yield ceiling
    (``max_scanned``) span ALL roots, so a pathological first root
    cannot starve the rest by more than the shared budget -- and the
    whole pivot stays O(K). Paths are deduped across overlapping
    roots. Terminates cleanly on budget exhaustion (mirrors the
    ``iter_bounded_files`` contract). NEVER raises.
    """
    budget_scanned = max_scanned if max_scanned is not None else locality_max_scanned()
    budget_timeout = timeout_s if timeout_s is not None else locality_timeout_s()
    eff_skip = skip_dirs if skip_dirs is not None else default_skip_dirs()

    t0 = time.monotonic()
    yielded = 0
    seen: Set[str] = set()
    for root in roots:
        remaining_t = budget_timeout - (time.monotonic() - t0)
        remaining_n = budget_scanned - yielded
        if remaining_t <= 0.0 or remaining_n <= 0:
            return
        for path_str in iter_bounded_files(
            root,
            max_scanned=remaining_n,
            timeout_s=remaining_t,
            skip_dirs=eff_skip,
        ):
            if path_str in seen:
                continue
            seen.add(path_str)
            yielded += 1
            yield path_str
            if yielded >= budget_scanned:
                return
            if (time.monotonic() - t0) > budget_timeout:
                return


__all__ = [
    "ESCALATION_NOTIFY_APPLY",
    "EPISTEMIC_NOTIFY_ENABLED_ENV_VAR",
    "LOCALITY_BOUNDING_ENABLED_ENV_VAR",
    "MEASURED_PROVENANCES",
    "PROVENANCE_LOCAL_LOWER_BOUND",
    "PROVENANCE_MEASURED",
    "PROVENANCE_SYNTHETIC",
    "PROVENANCE_UNKNOWN",
    "cold_root_ttl_s",
    "derive_locality_roots",
    "epistemic_notify_enabled",
    "is_cold_root",
    "iter_locality_files",
    "locality_bounding_enabled",
    "locality_max_roots",
    "locality_max_scanned",
    "locality_timeout_s",
    "note_cold_root",
    "peek_blast_epistemics",
    "record_blast_epistemics",
]
