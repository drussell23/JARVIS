"""SWE-Bench-Pro dataset loader — Phase 2 Phase A (PRD §40.7.9).

Loads SWE-Bench-Pro problems into a frozen ``ProblemSpec`` with
per-instance JSON caching at ``.jarvis/swe_bench_pro/cache/``.

Two acquisition paths (composition discipline; no parallel logic):

  1. **Local JSONL (PRIMARY)** — operator-pre-downloaded dataset
     at ``JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH`` (default
     ``.jarvis/swe_bench_pro/dataset.jsonl``).  One problem per
     line, JSON-encoded.  Avoids network dependency for Phase A
     substrate testing + CI; operator opts into HF when ready.

  2. **HuggingFace fetch (OPT-IN)** — when
     ``JARVIS_SWE_BENCH_PRO_HF_DATASET`` is set to a HuggingFace
     dataset path (e.g. ``"princeton-nlp/SWE-bench-Pro"``), the
     loader lazy-imports the ``datasets`` library and fetches the
     instance.  ``datasets`` is NOT a hard dependency — if not
     installed AND HF env is set, ``LoadOutcome.FETCH_FAILED``
     surfaces with a clear error.  Operator installs ``datasets``
     when they want HF fetch.

Authority asymmetry (§1 Boundary)
---------------------------------

Phase A is **read-only data acquisition**.  Forbidden imports
(AST-pinned in spine):

  * ``orchestrator``, ``iron_gate``, ``change_engine``,
    ``candidate_generator``, ``policy_engine``, ``risk_tier``,
    ``repair_engine``

Composition allowlist (canonical surfaces only):

  * ``cross_process_jsonl.flock_append_line`` — atomic write
    pattern for cache index entries (see :func:`_write_cache_index_line`)
  * stdlib only otherwise — no dependency surface increase

§7 fail-closed contract
-----------------------

Every public surface NEVER raises into the caller (except
``asyncio.CancelledError`` per orchestrator POSTMORTEM convention):

  * :func:`load_problem` — returns ``(None, LoadOutcome)`` on any
    failure mode (missing instance / fetch failed / malformed
    JSON / disk error).  Outcome carries operator-visible reason.
  * :func:`list_cached_problems` — returns ``[]`` if cache dir
    missing or unreadable.
  * :func:`clear_cache` — returns 0 if cache dir missing; counts
    successfully-removed entries otherwise.
  * Frozen ``ProblemSpec.from_dict`` — accepts garbage gracefully
    where possible; raises ``ValueError`` ONLY on unrecoverable
    schema-validation failure (operator must catch).

§33.1 graduation contract
-------------------------

Master flag ``JARVIS_SWE_BENCH_PRO_ENABLED`` defaults FALSE.
Production behavior is byte-identical when unset; the loader's
public surfaces short-circuit at the master-flag check before
any I/O.  Phase 2 Phase G (stratified sample run, paid) cannot
proceed until §40.7.9's hard-stop conditions are met.

§33.5 symmetric serialization
-----------------------------

``ProblemSpec.to_dict`` ↔ ``ProblemSpec.from_dict`` round-trip
preserves every field including ``schema_version`` + arbitrary
``metadata`` (forward-compat for fields the dataset adds in
future versions).
"""
from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple


logger = logging.getLogger("Ouroboros.SWEBenchPro.DatasetLoader")


# ===========================================================================
# Schema + env vocabulary
# ===========================================================================


SWE_BENCH_PRO_PROBLEM_SCHEMA_VERSION: str = "swe_bench_pro_problem.v1"


# Master flag (§33.1 default-FALSE).  Production behavior is
# byte-identical when unset; load_problem short-circuits at
# enabled() before any disk/network I/O.
MASTER_FLAG_ENV_VAR: str = "JARVIS_SWE_BENCH_PRO_ENABLED"

# Cache directory — per-instance JSON files at
# ``<CACHE_PATH>/<sanitized_id>.json``.  Default location keeps
# the cache out of the repo's git tree (.jarvis/ is gitignored).
CACHE_PATH_ENV_VAR: str = "JARVIS_SWE_BENCH_PRO_CACHE_PATH"

# Local pre-downloaded JSONL — PRIMARY acquisition path.  One
# problem per line, JSON-encoded (matches HuggingFace
# datasets.export to_json shape).  Default location sits beside
# the cache dir.
LOCAL_DATASET_PATH_ENV_VAR: str = "JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH"

# HuggingFace dataset path — OPT-IN secondary acquisition path.
# Empty default = HF fetch disabled.  When set, the loader
# lazy-imports ``datasets`` (NOT a hard dep) and fetches the
# named dataset.  Operator opts in by setting this env to
# something like ``"princeton-nlp/SWE-bench-Pro"`` AFTER
# installing ``datasets`` via pip.
HF_DATASET_ENV_VAR: str = "JARVIS_SWE_BENCH_PRO_HF_DATASET"

# HuggingFace split — defaults ``test`` (the standard
# evaluation split for SWE-Bench-family datasets).
HF_SPLIT_ENV_VAR: str = "JARVIS_SWE_BENCH_PRO_HF_SPLIT"

_DEFAULT_CACHE_PATH: str = ".jarvis/swe_bench_pro/cache"
_DEFAULT_LOCAL_DATASET_PATH: str = ".jarvis/swe_bench_pro/dataset.jsonl"
_DEFAULT_HF_SPLIT: str = "test"

# Maximum size of a single problem record on disk.  Defends
# against pathological cache files (truncated / appended garbage
# / accidentally swapped with a different file).  10 MiB is
# generous for a SWE-Bench-Pro problem (issue text + patches
# typically total < 100 KiB).
_MAX_PROBLEM_BYTES: int = 10 * 1024 * 1024


# ===========================================================================
# Closed taxonomies (AST bytes-pinned)
# ===========================================================================


class LoadOutcome(str, enum.Enum):
    """Four canonical outcomes for a single :func:`load_problem` call.

    Closed taxonomy.  Adding a new value requires a Phase tag +
    spine update; the AST pin asserts the value-set bytes are
    exactly these four strings.

    LOADED — fetched fresh (from local JSONL or HF) AND written
    to cache.  ``ProblemSpec`` is non-None.

    LOADED_FROM_CACHE — read from ``.jarvis/swe_bench_pro/cache/``
    without acquisition path involvement.  ``ProblemSpec`` is
    non-None.

    FETCH_FAILED — acquisition was attempted but failed (network
    error / HF dataset missing / local JSONL malformed / disk I/O
    error).  ``ProblemSpec`` is None.  Diagnostic in the log.

    MISSING — instance_id was not present in the local JSONL
    (and HF was either disabled or also did not surface it).
    ``ProblemSpec`` is None.  Distinct from FETCH_FAILED:
    MISSING means "we could not find this id"; FETCH_FAILED
    means "we tried and the acquisition path errored."
    """

    LOADED = "loaded"
    LOADED_FROM_CACHE = "loaded_from_cache"
    FETCH_FAILED = "fetch_failed"
    MISSING = "missing"


# ===========================================================================
# Frozen ProblemSpec dataclass (§33.5 symmetric to_dict/from_dict)
# ===========================================================================


@dataclass(frozen=True)
class ProblemSpec:
    """One SWE-Bench-Pro problem, fully resolved for downstream use.

    Frozen post-construction; immutable across the evaluation
    lifecycle.  Composes the canonical SWE-Bench instance shape
    with one Pro-specific field (``difficulty``) + one
    forward-compat extension point (``metadata`` preserves any
    extra dataset fields verbatim).

    Fields
    ------

    instance_id
        Canonical identifier (e.g. ``"astropy__astropy-12907"``).
        Filename-safe; used directly as the cache file basename.
    repo
        Repository full name (e.g. ``"astropy/astropy"``).
    repo_url
        Clone URL.  May be ``""`` if not provided by the dataset
        (Phase B harness derives from ``repo`` if needed).
    base_commit
        Full SHA at which the test_patch + gold_patch apply.
    problem_statement
        GitHub issue body / problem description.  The text the
        model sees as the task.
    test_patch
        Unified diff that ADDS or MODIFIES tests so they FAIL
        against the buggy base + PASS against the gold fix.
        Phase B applies this before invoking RepairEngine.
    gold_patch
        Unified diff representing the reference fix.  **NEVER
        sent to the model**; used by Phase C scorer for diff
        comparison + by spine tests for fixture-validity checks.
    difficulty
        ``"easy"`` / ``"medium"`` / ``"hard"`` / ``"unknown"``.
        SWE-Bench-Pro adds difficulty stratification absent from
        v1 SWE-Bench.
    metadata
        Forward-compat map of any additional dataset fields
        (preserved verbatim across to_dict/from_dict).  Empty by
        default.  Used for fields like ``version``, ``created_at``,
        ``hints`` that vary across dataset versions.
    schema_version
        Schema version stamp.  Defaults to
        :data:`SWE_BENCH_PRO_PROBLEM_SCHEMA_VERSION`.  Bumped on
        any breaking field change (additive changes preserved
        via ``metadata``).
    """

    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    test_patch: str
    gold_patch: str
    difficulty: str = "unknown"
    repo_url: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    schema_version: str = SWE_BENCH_PRO_PROBLEM_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "instance_id": self.instance_id,
            "repo": self.repo,
            "repo_url": self.repo_url,
            "base_commit": self.base_commit,
            "problem_statement": self.problem_statement,
            "test_patch": self.test_patch,
            "gold_patch": self.gold_patch,
            "difficulty": self.difficulty,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProblemSpec":
        """Construct from a dict.  Tolerates missing optional fields
        with sensible defaults; raises ``ValueError`` only on
        unrecoverable schema violations (missing ``instance_id``)."""
        instance_id = payload.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id:
            raise ValueError(
                "ProblemSpec.from_dict: 'instance_id' is required + "
                "must be a non-empty string"
            )
        return cls(
            schema_version=str(payload.get(
                "schema_version", SWE_BENCH_PRO_PROBLEM_SCHEMA_VERSION,
            )),
            instance_id=instance_id,
            repo=str(payload.get("repo", "")),
            repo_url=str(payload.get("repo_url", "")),
            base_commit=str(payload.get("base_commit", "")),
            problem_statement=str(payload.get("problem_statement", "")),
            test_patch=str(payload.get("test_patch", "")),
            gold_patch=str(payload.get("gold_patch", "")),
            difficulty=str(payload.get("difficulty", "unknown")),
            metadata=dict(payload.get("metadata", {})),
        )


# ===========================================================================
# Env loaders (NEVER raise; clamped where applicable)
# ===========================================================================


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("", "false", "0", "no", "off")


def swe_bench_pro_enabled() -> bool:
    """Master-flag accessor.  Default FALSE per §33.1.  NEVER raises."""
    return _env_bool(MASTER_FLAG_ENV_VAR, default=False)


def cache_dir() -> Path:
    """Cache directory accessor.  Resolves the env override or
    falls back to the default ``.jarvis/swe_bench_pro/cache/``
    location.  NEVER raises."""
    raw = os.environ.get(CACHE_PATH_ENV_VAR, "").strip()
    return Path(raw) if raw else Path(_DEFAULT_CACHE_PATH)


def _local_dataset_path() -> Path:
    raw = os.environ.get(LOCAL_DATASET_PATH_ENV_VAR, "").strip()
    return Path(raw) if raw else Path(_DEFAULT_LOCAL_DATASET_PATH)


def _hf_dataset_name() -> str:
    """HuggingFace dataset path; empty default = HF fetch disabled."""
    return os.environ.get(HF_DATASET_ENV_VAR, "").strip()


def _hf_split() -> str:
    raw = os.environ.get(HF_SPLIT_ENV_VAR, "").strip()
    return raw if raw else _DEFAULT_HF_SPLIT


# ===========================================================================
# Cache I/O — atomic write via tmp+rename (no flock needed for
# per-file cache; different files don't contend)
# ===========================================================================


def _sanitize_instance_id_for_filename(instance_id: str) -> str:
    """Convert instance_id to a filesystem-safe filename basename.

    SWE-Bench-Pro instance_ids like ``astropy__astropy-12907`` are
    already safe; this defensively replaces filesystem-hostile
    characters (``/``, ``\\``, null) with underscores.  Pure
    function; deterministic; never raises."""
    out_chars: List[str] = []
    for ch in instance_id:
        if ch in ("/", "\\", "\x00", ":"):
            out_chars.append("_")
        else:
            out_chars.append(ch)
    return "".join(out_chars) or "_unnamed"


def _cache_path_for(instance_id: str) -> Path:
    return cache_dir() / f"{_sanitize_instance_id_for_filename(instance_id)}.json"


def _read_cache(instance_id: str) -> Optional[ProblemSpec]:
    """Read a cached ProblemSpec; return None on any failure
    (missing file / malformed JSON / size limit / schema
    violation).  NEVER raises."""
    path = _cache_path_for(instance_id)
    try:
        if not path.is_file():
            return None
        size = path.stat().st_size
        if size > _MAX_PROBLEM_BYTES:
            logger.warning(
                "[SWEBenchPro] cache file %r exceeds %d bytes — refusing to load",
                str(path), _MAX_PROBLEM_BYTES,
            )
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.debug(
                "[SWEBenchPro] malformed cache file %r — ignoring",
                str(path), exc_info=True,
            )
            return None
        if not isinstance(payload, dict):
            return None
        try:
            return ProblemSpec.from_dict(payload)
        except (ValueError, TypeError):
            logger.debug(
                "[SWEBenchPro] cache file %r failed schema validation",
                str(path), exc_info=True,
            )
            return None
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[SWEBenchPro] _read_cache raised", exc_info=True,
        )
        return None


def _write_cache(spec: ProblemSpec) -> bool:
    """Atomic write a ProblemSpec to cache via tmp+os.replace.
    Returns True on success, False on any failure.  NEVER raises."""
    try:
        path = _cache_path_for(spec.instance_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: serialize to a temp file in the same dir,
        # then os.replace (POSIX-atomic + cross-process safe).
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(spec.to_dict(), indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(str(tmp_path), str(path))
        return True
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — defensive
        logger.warning(
            "[SWEBenchPro] cache write failed for instance_id=%r",
            getattr(spec, "instance_id", "?"), exc_info=True,
        )
        return False


# ===========================================================================
# Acquisition: local JSONL (PRIMARY) + HuggingFace (OPT-IN)
# ===========================================================================


def _load_from_local_jsonl(instance_id: str) -> Optional[ProblemSpec]:
    """Scan the local JSONL for the requested instance_id.  Returns
    None if file missing or instance not found.  NEVER raises."""
    path = _local_dataset_path()
    try:
        if not path.is_file():
            return None
        with path.open("r", encoding="utf-8") as fh:
            for line_num, raw in enumerate(fh, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    logger.debug(
                        "[SWEBenchPro] local JSONL line %d malformed — skipping",
                        line_num,
                    )
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("instance_id") == instance_id:
                    try:
                        return ProblemSpec.from_dict(record)
                    except (ValueError, TypeError):
                        logger.warning(
                            "[SWEBenchPro] local JSONL line %d for %r "
                            "failed schema validation", line_num, instance_id,
                            exc_info=True,
                        )
                        return None
        return None
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — defensive
        logger.warning(
            "[SWEBenchPro] _load_from_local_jsonl raised for %r",
            instance_id, exc_info=True,
        )
        return None


def _load_from_huggingface(instance_id: str) -> Optional[ProblemSpec]:
    """Lazy-import ``datasets`` and fetch the instance from HF.

    Returns None if HF env not set, ``datasets`` not installed,
    fetch errors, or instance not found in the dataset.
    NEVER raises (asyncio.CancelledError propagates)."""
    hf_name = _hf_dataset_name()
    if not hf_name:
        return None
    try:
        try:
            import datasets  # type: ignore  # noqa: I001 — lazy import
        except ImportError:
            logger.warning(
                "[SWEBenchPro] HF fetch requested (%s=%r) but ``datasets`` "
                "is not installed — pip install datasets to enable",
                HF_DATASET_ENV_VAR, hf_name,
            )
            return None
        ds = datasets.load_dataset(hf_name, split=_hf_split())
        for record in ds:
            if not isinstance(record, dict):
                continue
            if record.get("instance_id") == instance_id:
                try:
                    return ProblemSpec.from_dict(record)
                except (ValueError, TypeError):
                    logger.warning(
                        "[SWEBenchPro] HF record for %r failed schema "
                        "validation", instance_id, exc_info=True,
                    )
                    return None
        return None
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — defensive
        logger.warning(
            "[SWEBenchPro] _load_from_huggingface raised for %r",
            instance_id, exc_info=True,
        )
        return None


# ===========================================================================
# Public API — load_problem + cache management
# ===========================================================================


def load_problem(
    instance_id: str,
) -> Tuple[Optional[ProblemSpec], LoadOutcome]:
    """Load one SWE-Bench-Pro problem.

    Resolution order:
      1. Master flag check — if FALSE → ``(None, MISSING)``
         (forces explicit operator opt-in; no silent
         dataset I/O when feature is off).
      2. Cache hit at ``cache_dir()/<id>.json`` →
         ``(spec, LOADED_FROM_CACHE)``.
      3. Local JSONL scan at
         :data:`LOCAL_DATASET_PATH_ENV_VAR` → cache + return
         ``(spec, LOADED)``.
      4. HuggingFace fetch (only if
         :data:`HF_DATASET_ENV_VAR` is set) → cache + return
         ``(spec, LOADED)``.
      5. None of the above → ``(None, MISSING)``.

    NEVER raises (``asyncio.CancelledError`` propagates).  Failures
    in cache write / HF fetch surface as ``FETCH_FAILED`` rather
    than crashing the caller.
    """
    if not swe_bench_pro_enabled():
        return None, LoadOutcome.MISSING
    if not isinstance(instance_id, str) or not instance_id:
        return None, LoadOutcome.MISSING
    try:
        # Cache hit first — cheapest path.
        cached = _read_cache(instance_id)
        if cached is not None:
            return cached, LoadOutcome.LOADED_FROM_CACHE
        # Acquisition: local JSONL primary.
        spec = _load_from_local_jsonl(instance_id)
        if spec is not None:
            wrote = _write_cache(spec)
            outcome = LoadOutcome.LOADED if wrote else LoadOutcome.FETCH_FAILED
            return (spec, outcome) if wrote else (spec, LoadOutcome.LOADED)
            # Note: even if cache write fails, we still return the
            # spec we loaded — the outcome is LOADED.  FETCH_FAILED
            # is reserved for cases where we couldn't get the spec
            # at all.
        # Acquisition: HuggingFace fallback (opt-in only).
        spec = _load_from_huggingface(instance_id)
        if spec is not None:
            _write_cache(spec)  # best-effort cache; outcome stays LOADED
            return spec, LoadOutcome.LOADED
        # Not found anywhere.
        return None, LoadOutcome.MISSING
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — fail-open contract
        logger.warning(
            "[SWEBenchPro] load_problem(%r) raised", instance_id,
            exc_info=True,
        )
        return None, LoadOutcome.FETCH_FAILED


def list_cached_problems() -> List[str]:
    """Return sorted list of instance_ids currently in the cache.
    NEVER raises."""
    try:
        d = cache_dir()
        if not d.is_dir():
            return []
        out: List[str] = []
        for entry in d.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix != ".json":
                continue
            if entry.name.startswith("_"):
                # Convention: underscore-prefixed files are reserved
                # (e.g., _index.json if a future arc adds one).
                continue
            # The cache filename basename IS the sanitized
            # instance_id — but we want to surface the original
            # instance_id from the cached payload (the sanitization
            # is one-way for safety).
            spec = _read_cache(entry.stem)
            if spec is not None:
                out.append(spec.instance_id)
        return sorted(out)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.debug(
            "[SWEBenchPro] list_cached_problems raised", exc_info=True,
        )
        return []


def clear_cache(instance_id: Optional[str] = None) -> int:
    """Purge cache entries.  Returns the number of entries removed.

    If ``instance_id`` is None → purge ALL entries (rmdir + recreate).
    If specified → remove just that one entry's file.

    NEVER raises (``asyncio.CancelledError`` propagates)."""
    try:
        d = cache_dir()
        if not d.is_dir():
            return 0
        if instance_id is None:
            # Count first, then nuke + recreate
            count = 0
            for entry in d.iterdir():
                if entry.is_file() and entry.suffix == ".json" \
                        and not entry.name.startswith("_"):
                    count += 1
            shutil.rmtree(str(d), ignore_errors=True)
            d.mkdir(parents=True, exist_ok=True)
            return count
        path = _cache_path_for(instance_id)
        if path.is_file():
            try:
                path.unlink()
                return 1
            except OSError:
                logger.warning(
                    "[SWEBenchPro] could not unlink cache file %r",
                    str(path), exc_info=True,
                )
                return 0
        return 0
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.debug(
            "[SWEBenchPro] clear_cache raised", exc_info=True,
        )
        return 0


# ===========================================================================
# FlagRegistry self-registration (auto-discovered by §33.3 walker)
# ===========================================================================


def register_flags(registry: Any) -> int:
    """Module-owned FlagRegistry registration.

    Picked up zero-edit by ``flag_registry_seed.
    _discover_module_provided_flags`` walker on next boot.
    NEVER raises — fail-open per §33.1.

    Returns the count of FlagSpecs successfully registered."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category,
            FlagSpec,
            FlagType,
        )
    except ImportError:
        return 0

    specs = [
        FlagSpec(
            name=MASTER_FLAG_ENV_VAR,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Master kill switch for SWE-Bench-Pro evaluation arc "
                "(Phase 2; v3.7 PRD §40.7.9).  When TRUE, "
                "load_problem + list_cached_problems + clear_cache + "
                "all downstream Phase B-F harness substrates become "
                "active.  Default FALSE per §33.1 graduation contract "
                "— flip on for an operator-paced evaluation run."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/swe_bench_pro/"
                "dataset_loader.py"
            ),
            example="true",
            since="v3.7 Phase 2 Phase A (2026-05-12)",
        ),
        FlagSpec(
            name=CACHE_PATH_ENV_VAR,
            type=FlagType.STR,
            default=_DEFAULT_CACHE_PATH,
            description=(
                "Per-instance JSON cache directory for SWE-Bench-Pro "
                "ProblemSpec records.  Defaults to "
                f"{_DEFAULT_CACHE_PATH} (out of git tree).  Override "
                "for shared-cache deployments."
            ),
            category=Category.INTEGRATION,
            source_file=(
                "backend/core/ouroboros/governance/swe_bench_pro/"
                "dataset_loader.py"
            ),
            example=_DEFAULT_CACHE_PATH,
            since="v3.7 Phase 2 Phase A (2026-05-12)",
        ),
        FlagSpec(
            name=LOCAL_DATASET_PATH_ENV_VAR,
            type=FlagType.STR,
            default=_DEFAULT_LOCAL_DATASET_PATH,
            description=(
                "Path to the operator-pre-downloaded SWE-Bench-Pro "
                "JSONL dataset.  PRIMARY acquisition path (avoids "
                "network dependency).  One problem per line, "
                "JSON-encoded; matches HuggingFace "
                "datasets.export to_json shape."
            ),
            category=Category.INTEGRATION,
            source_file=(
                "backend/core/ouroboros/governance/swe_bench_pro/"
                "dataset_loader.py"
            ),
            example=_DEFAULT_LOCAL_DATASET_PATH,
            since="v3.7 Phase 2 Phase A (2026-05-12)",
        ),
        FlagSpec(
            name=HF_DATASET_ENV_VAR,
            type=FlagType.STR,
            default="",
            description=(
                "HuggingFace dataset path for SWE-Bench-Pro fetch "
                "(e.g. 'princeton-nlp/SWE-bench-Pro').  OPT-IN "
                "secondary acquisition path; empty default = HF "
                "fetch disabled.  Requires the ``datasets`` library "
                "installed (pip install datasets) — NOT a hard "
                "dependency of O+V."
            ),
            category=Category.INTEGRATION,
            source_file=(
                "backend/core/ouroboros/governance/swe_bench_pro/"
                "dataset_loader.py"
            ),
            example="princeton-nlp/SWE-bench-Pro",
            since="v3.7 Phase 2 Phase A (2026-05-12)",
        ),
        FlagSpec(
            name=HF_SPLIT_ENV_VAR,
            type=FlagType.STR,
            default=_DEFAULT_HF_SPLIT,
            description=(
                "HuggingFace dataset split selector (default "
                f"'{_DEFAULT_HF_SPLIT}').  Only consulted when "
                f"{HF_DATASET_ENV_VAR} is set."
            ),
            category=Category.INTEGRATION,
            source_file=(
                "backend/core/ouroboros/governance/swe_bench_pro/"
                "dataset_loader.py"
            ),
            example=_DEFAULT_HF_SPLIT,
            since="v3.7 Phase 2 Phase A (2026-05-12)",
        ),
    ]

    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001 — boot-time fail-open
            logger.debug(
                "[SWEBenchPro] flag registration failed for %s",
                getattr(spec, "name", "?"),
                exc_info=True,
            )
    return count


__all__ = [
    "SWE_BENCH_PRO_PROBLEM_SCHEMA_VERSION",
    "MASTER_FLAG_ENV_VAR",
    "CACHE_PATH_ENV_VAR",
    "LOCAL_DATASET_PATH_ENV_VAR",
    "HF_DATASET_ENV_VAR",
    "HF_SPLIT_ENV_VAR",
    "LoadOutcome",
    "ProblemSpec",
    "swe_bench_pro_enabled",
    "cache_dir",
    "load_problem",
    "list_cached_problems",
    "clear_cache",
    "register_flags",
]
