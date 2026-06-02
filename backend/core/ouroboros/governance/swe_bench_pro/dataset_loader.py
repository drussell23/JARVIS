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
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple


logger = logging.getLogger("Ouroboros.SWEBenchPro.DatasetLoader")


# ===========================================================================
# Schema + env vocabulary
# ===========================================================================


SWE_BENCH_PRO_PROBLEM_SCHEMA_VERSION: str = "swe_bench_pro_problem.v1"

# Reference-fix field aliases.  The upstream ``ScaleAI/SWE-bench_Pro``
# dataset names the gold fix ``patch`` (classic SWE-Bench
# convention); our ProblemSpec calls it ``gold_patch``.  Explicit
# ``gold_patch`` wins over ``patch`` when both are present (defensive
# — a derivative dataset may carry both).  Ordered: first non-empty
# match wins.
_GOLD_PATCH_FIELD_ALIASES: tuple = ("gold_patch", "patch")

# The canonical ProblemSpec field names.  Any payload key NOT in
# this set is folded into ``metadata`` verbatim by
# :meth:`ProblemSpec.from_dict` (forward-compat — preserves Scale AI
# extensions without a hardcoded extension list).  ``patch`` is
# listed so the gold-patch alias source is not ALSO duplicated into
# metadata.
_CANONICAL_PROBLEM_FIELDS: frozenset = frozenset({
    "schema_version", "instance_id", "repo", "repo_url",
    "base_commit", "problem_statement", "test_patch", "gold_patch",
    "patch", "difficulty", "metadata",
})


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
        """Construct from a dict, normalizing the canonical
        SWE-Bench-Pro / Scale AI dataset schema.

        Tolerates missing optional fields with sensible defaults;
        raises ``ValueError`` only on unrecoverable schema
        violations (missing ``instance_id``).

        Schema normalization (single seam — every acquisition path
        composes this so the loader, Phase B/C, and the geometric
        sampler all see ONE canonical shape):

          * **gold_patch** — the upstream ``ScaleAI/SWE-bench_Pro``
            dataset names the reference fix ``patch`` (classic
            SWE-Bench convention); our schema calls it
            ``gold_patch``.  Explicit ``gold_patch`` wins if
            present, else ``patch`` (:data:`_GOLD_PATCH_FIELD_ALIASES`).
          * **repo_url** — derived ``https://github.com/<repo>.git``
            when absent (the Scale AI dataset omits it; Phase B
            needs a clone URL).
          * **metadata** — every payload key that is NOT a canonical
            ``ProblemSpec`` field is folded into ``metadata``
            verbatim (forward-compat: preserves Scale AI extensions
            like ``fail_to_pass`` / ``pass_to_pass`` / ``interface``
            for downstream without a hardcoded field list).
        """
        instance_id = payload.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id:
            raise ValueError(
                "ProblemSpec.from_dict: 'instance_id' is required + "
                "must be a non-empty string"
            )

        gold_patch = ""
        for _alias in _GOLD_PATCH_FIELD_ALIASES:
            _val = payload.get(_alias)
            if isinstance(_val, str) and _val:
                gold_patch = _val
                break

        repo = str(payload.get("repo", ""))
        repo_url = str(payload.get("repo_url", "") or "")
        if not repo_url and repo:
            repo_url = f"https://github.com/{repo}.git"

        # Fold unknown payload keys into metadata (structural —
        # "everything not a canonical field", no hardcoded Scale AI
        # key list).  Explicit ``metadata`` mapping takes precedence.
        extra: Dict[str, Any] = {}
        for _k, _v in payload.items():
            if _k in _CANONICAL_PROBLEM_FIELDS:
                continue
            extra[_k] = _v
        explicit_meta = payload.get("metadata", {})
        if isinstance(explicit_meta, Mapping):
            extra.update(explicit_meta)

        return cls(
            schema_version=str(payload.get(
                "schema_version", SWE_BENCH_PRO_PROBLEM_SCHEMA_VERSION,
            )),
            instance_id=instance_id,
            repo=repo,
            repo_url=repo_url,
            base_commit=str(payload.get("base_commit", "")),
            problem_statement=str(payload.get("problem_statement", "")),
            test_patch=str(payload.get("test_patch", "")),
            gold_patch=gold_patch,
            difficulty=str(payload.get("difficulty", "unknown")),
            metadata=extra,
        )


# ===========================================================================
# Env loaders (NEVER raise; clamped where applicable)
# ===========================================================================


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("", "false", "0", "no", "off")


def _env_int(name: str, *, default: int, minimum: int) -> int:
    """Read an int env var, clamped to ``>= minimum``.  Invalid /
    unset → ``default``.  NEVER raises (mirrors :func:`_env_bool`
    fail-soft contract — config errors must not crash the loader)."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except (ValueError, TypeError):
        logger.warning(
            "[SWEBenchPro] invalid %s=%r — using default %d",
            name, raw, default,
        )
        return default
    return value if value >= minimum else minimum


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


class HFTokenPlaceholderError(ValueError):
    """Slice 62 — raised when an HF dataset is configured but the ambient
    Hugging Face token is an un-substituted placeholder.

    A CONFIG error (not transient): the recurring 2026-06-02 misconfig where
    auth used a literal placeholder (``hf_YOUR_REAL_TOKEN_HERE`` /
    ``your_actual_token_here`` / ``<paste_your_huggingface_token_here>``),
    which otherwise 401s deep inside huggingface_hub with a cryptic traceback.
    Aborts loudly with an actionable message BEFORE the network call.
    """


# Obvious un-substituted-placeholder fingerprints (lower-cased substring
# match). Closed, additive list — covers the example strings handed out in
# the runbooks + the generic "<paste …>" / "…token_here" shapes.
_HF_TOKEN_PLACEHOLDER_MARKERS: tuple = (
    "paste_your", "your_actual_token", "your_real_token", "your_token",
    "your_huggingface_token", "yourtoken", "hf_your", "<paste",
    "real_token_here", "token_here", "your_hf_token",
)


def hf_token_appears_placeholder(token: Optional[str]) -> bool:
    """True iff a NON-EMPTY ``token`` is an obvious un-substituted placeholder.

    Pure + deterministic. Empty/blank/None returns False — an unset HF_TOKEN
    is valid (a disk-cached ``hf auth login`` token resolves ambiently), so
    only a non-empty bogus value is a misconfiguration worth aborting on.
    """
    t = (token or "").strip().lower()
    if not t:
        return False
    return any(marker in t for marker in _HF_TOKEN_PLACEHOLDER_MARKERS)


def _ambient_hf_token() -> str:
    """The Hugging Face token huggingface_hub will resolve from the env, in
    its precedence order. Empty string when none is set in the environment
    (a disk-cached login token is not visible here — and that's fine, it is
    never a placeholder)."""
    for var in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_TOKEN"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    return ""


def _assert_hf_token_not_placeholder(hf_name: str) -> None:
    """Slice 62 sentinel — abort with a clear, actionable error when an HF
    dataset is requested but the ambient token is a placeholder. Logs at
    ERROR (unmissable even if a fail-open caller later swallows the raise)
    then raises :class:`HFTokenPlaceholderError`."""
    tok = _ambient_hf_token()
    if not hf_token_appears_placeholder(tok):
        return
    logger.error(
        "[SWEBenchPro] HF dataset %r requested but the Hugging Face token "
        "looks like an un-substituted placeholder (%r). Put your REAL token "
        "in .env (gitignored + loaded at boot + wins over shell exports): "
        "HF_TOKEN=hf_xxxxx — and accept the gated license at "
        "https://huggingface.co/datasets/%s . Aborting before the 401.",
        hf_name, tok[:16], hf_name,
    )
    raise HFTokenPlaceholderError(
        f"HF_TOKEN is a placeholder ({tok[:16]!r}); set a REAL Hugging Face "
        f"token in .env to access {hf_name}"
    )


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


# Bounded scan ceiling for the local JSONL — SWE-Bench-Pro's full
# upstream dataset is 1,865 problems; 10,000 leaves headroom for
# derivative datasets without unbounded scans. Both
# :func:`_iter_local_jsonl_records` (used by enumeration) and the
# per-id scan in :func:`_load_from_local_jsonl` honor this cap.
_LOCAL_JSONL_MAX_ROWS: int = 10000

# Bounded scan ceiling for the FULL-dataset enumeration consumed by
# the geometric instance sampler (local JSONL ∪ HF). Same headroom
# rationale as _LOCAL_JSONL_MAX_ROWS over the 1,865-problem upstream
# dataset.
_DATASET_SCAN_MAX_RECORDS_DEFAULT: int = 10000
SAMPLER_MAX_SCAN_ENV_VAR: str = "JARVIS_SWE_BENCH_PRO_SAMPLER_MAX_SCAN"


def _dataset_scan_max_records() -> int:
    """Resolve the full-dataset scan cap at CALL time (env-overridable
    via :data:`SAMPLER_MAX_SCAN_ENV_VAR`) so the sampler cannot run
    unbounded on a pathological / derivative dataset and tests can
    monkey-patch the env.  NEVER raises."""
    return _env_int(
        SAMPLER_MAX_SCAN_ENV_VAR,
        default=_DATASET_SCAN_MAX_RECORDS_DEFAULT,
        minimum=1,
    )


def _iter_local_jsonl_records(
    *, max_rows: Optional[int] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield each parsed dict record from the LOCAL_DATASET_PATH
    JSONL, bounded by ``max_rows`` (default
    :data:`_LOCAL_JSONL_MAX_ROWS`, read at CALL time so tests can
    monkey-patch the module constant).

    Single source of truth for local-JSONL scanning — both
    :func:`_load_from_local_jsonl` (per-id load) and
    :func:`list_cached_problems` (enumeration) compose this
    iterator so the line-parsing + dedup-key logic stays in one
    place. Malformed rows + non-dict records are skipped with a
    DEBUG log. Missing file → empty iterator (no raise). NEVER
    raises (asyncio.CancelledError propagates).
    """
    effective_cap = (
        max_rows if max_rows is not None else _LOCAL_JSONL_MAX_ROWS
    )
    path = _local_dataset_path()
    try:
        if not path.is_file():
            return
        with path.open("r", encoding="utf-8") as fh:
            for line_num, raw in enumerate(fh, start=1):
                if line_num > effective_cap:
                    logger.debug(
                        "[SWEBenchPro] local JSONL bounded scan capped "
                        "at %d rows — additional rows ignored. "
                        "Bump _LOCAL_JSONL_MAX_ROWS if needed.",
                        effective_cap,
                    )
                    return
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    logger.debug(
                        "[SWEBenchPro] local JSONL line %d malformed — skipping",
                        line_num,
                    )
                    continue
                if not isinstance(record, dict):
                    continue
                yield record
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — defensive (fail-open scan)
        logger.debug(
            "[SWEBenchPro] _iter_local_jsonl_records raised",
            exc_info=True,
        )
        return


def _load_from_local_jsonl(instance_id: str) -> Optional[ProblemSpec]:
    """Scan the local JSONL for the requested instance_id.  Returns
    None if file missing or instance not found.  NEVER raises.

    Composes :func:`_iter_local_jsonl_records` so the line-parsing
    + bounded-scan + dedup logic stays in one place. Schema-validation
    failures on the matched record produce a WARNING + None.
    """
    try:
        for record in _iter_local_jsonl_records():
            if record.get("instance_id") != instance_id:
                continue
            try:
                return ProblemSpec.from_dict(record)
            except (ValueError, TypeError):
                logger.warning(
                    "[SWEBenchPro] local JSONL record for %r failed "
                    "schema validation", instance_id, exc_info=True,
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


def _iter_hf_records() -> Iterator[Dict[str, Any]]:
    """Yield every dict record from the opt-in HuggingFace dataset.

    Single source of truth for the ``datasets.load_dataset`` call
    shape — both :func:`_load_from_huggingface` (per-id load) and
    :func:`iter_all_dataset_records` (full enumeration for the
    geometric sampler) compose this iterator so the lazy-import +
    load + dict-filter logic stays in ONE place (mirrors the
    :func:`_iter_local_jsonl_records` discipline).

    Empty iterator (no yield) if HF env not set, ``datasets`` not
    installed, or fetch errors. NEVER raises (asyncio.CancelledError
    propagates)."""
    hf_name = _hf_dataset_name()
    if not hf_name:
        return
    # Slice 62 — placeholder-token sentinel BEFORE any import/network so a
    # fake token aborts with a clear message instead of a cryptic 401. Placed
    # outside the fail-open try below so a CONFIG error propagates (transient
    # fetch errors below stay fail-open).
    _assert_hf_token_not_placeholder(hf_name)
    try:
        try:
            import datasets  # type: ignore  # noqa: I001 — lazy import
        except ImportError:
            logger.warning(
                "[SWEBenchPro] HF fetch requested (%s=%r) but ``datasets`` "
                "is not installed — pip install datasets to enable",
                HF_DATASET_ENV_VAR, hf_name,
            )
            return
        ds = datasets.load_dataset(hf_name, split=_hf_split())
        for record in ds:
            if isinstance(record, dict):
                yield record
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — defensive (fail-open scan)
        logger.warning(
            "[SWEBenchPro] _iter_hf_records raised (dataset=%r)",
            hf_name, exc_info=True,
        )
        return


def _load_from_huggingface(instance_id: str) -> Optional[ProblemSpec]:
    """Lazy-import ``datasets`` and fetch the instance from HF.

    Returns None if HF env not set, ``datasets`` not installed,
    fetch errors, or instance not found in the dataset.
    NEVER raises (asyncio.CancelledError propagates).

    Composes :func:`_iter_hf_records` (single source of truth for
    the HF load call)."""
    try:
        for record in _iter_hf_records():
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
    except HFTokenPlaceholderError:
        # Slice 62 — config error: surface it, don't fail-open to None.
        raise
    except Exception:  # noqa: BLE001 — defensive
        logger.warning(
            "[SWEBenchPro] _load_from_huggingface raised for %r",
            instance_id, exc_info=True,
        )
        return None


def iter_all_dataset_records(
    *, max_scan: Optional[int] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield raw dict records across the full dataset — the union
    of the local JSONL (PRIMARY) and the opt-in HuggingFace dataset.

    Composes the two canonical single-source iterators
    (:func:`_iter_local_jsonl_records` + :func:`_iter_hf_records`)
    so there is NO parallel dataset-scan logic — the geometric
    sampler reads exactly what :func:`load_problem` would resolve.

    Master-flag gated: yields nothing when
    :func:`swe_bench_pro_enabled` is False (no dataset I/O when the
    feature is off — mirrors :func:`load_problem`'s short-circuit).

    Dedup by ``instance_id``: a record present in BOTH local JSONL
    and HF is yielded once (local wins — it is the PRIMARY path).
    Bounded by ``max_scan`` (default
    :func:`_dataset_scan_max_records`, env-overridable, read at
    CALL time so tests can monkey-patch). NEVER raises
    (asyncio.CancelledError propagates)."""
    if not swe_bench_pro_enabled():
        return
    cap = (
        max_scan if max_scan is not None else _dataset_scan_max_records()
    )
    seen: set = set()
    emitted = 0
    try:
        for record in _iter_local_jsonl_records(max_rows=cap):
            iid = record.get("instance_id")
            if isinstance(iid, str) and iid:
                seen.add(iid)
            yield record
            emitted += 1
            if emitted >= cap:
                return
        for record in _iter_hf_records():
            iid = record.get("instance_id")
            if isinstance(iid, str) and iid and iid in seen:
                continue  # local PRIMARY already covered this id
            yield record
            emitted += 1
            if emitted >= cap:
                return
    except asyncio.CancelledError:
        raise
    except HFTokenPlaceholderError:
        # Slice 62 — config error: the sampler must see the misconfig, not a
        # silently-empty distribution.
        raise
    except Exception:  # noqa: BLE001 — defensive (fail-open scan)
        logger.warning(
            "[SWEBenchPro] iter_all_dataset_records raised", exc_info=True,
        )
        return


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
    """Single source of truth for "which instance_ids can
    :func:`load_problem` resolve right now."

    Returns the sorted union of:

      1. instance_ids materialized in the on-disk cache
         (``cache_dir()/<sanitized-id>.json``); AND
      2. instance_ids declared in the local-dataset JSONL pointed
         at by :data:`LOCAL_DATASET_PATH_ENV_VAR` (when the env var
         is set AND the file exists AND is readable).

    Both sources are composed via the SAME line-parsing helper
    :func:`_iter_local_jsonl_records` (single-source-of-truth for
    local JSONL scanning) so consumers cannot disagree about
    "available problems" — historically a workaround-inducing
    bug surface for the harness boot hook.

    The JSONL scan is bounded by :data:`_LOCAL_JSONL_MAX_ROWS`
    (default 10,000 — comfortable headroom over the upstream
    SWE-Bench-Pro 1,865-problem dataset). Malformed rows + records
    missing an ``instance_id`` field are silently skipped with a
    DEBUG log.

    Dedup semantics: the union collapses cache + JSONL duplicates;
    callers see each instance_id exactly once. NEVER raises;
    failures in either source produce an empty list from that
    source rather than tearing down the whole enumeration.
    """
    ids: set = set()
    try:
        d = cache_dir()
        if d.is_dir():
            for entry in d.iterdir():
                if not entry.is_file():
                    continue
                if entry.suffix != ".json":
                    continue
                if entry.name.startswith("_"):
                    # Convention: underscore-prefixed files are
                    # reserved (e.g., _index.json if a future arc
                    # adds one).
                    continue
                # The cache filename basename IS the sanitized
                # instance_id — but we want the original
                # instance_id from the cached payload (the
                # sanitization is one-way for safety).
                spec = _read_cache(entry.stem)
                if spec is not None:
                    ids.add(spec.instance_id)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — defensive (fail-open per-source)
        logger.debug(
            "[SWEBenchPro] list_cached_problems cache scan raised",
            exc_info=True,
        )

    try:
        for record in _iter_local_jsonl_records():
            iid = record.get("instance_id")
            if isinstance(iid, str) and iid:
                ids.add(iid)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — defensive (fail-open per-source)
        logger.debug(
            "[SWEBenchPro] list_cached_problems JSONL scan raised",
            exc_info=True,
        )

    return sorted(ids)


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
        FlagSpec(
            name=SAMPLER_MAX_SCAN_ENV_VAR,
            type=FlagType.INT,
            default=_DATASET_SCAN_MAX_RECORDS_DEFAULT,
            description=(
                "Bounded ceiling on the FULL-dataset enumeration "
                "(local JSONL union HF) consumed by "
                "iter_all_dataset_records / the GeometricInstanceSampler. "
                "Clamped to >= 1; invalid/unset -> default "
                f"{_DATASET_SCAN_MAX_RECORDS_DEFAULT} (headroom over the "
                "1,865-problem upstream dataset). Prevents an unbounded "
                "scan on a pathological / derivative dataset."
            ),
            category=Category.CAPACITY,
            source_file=(
                "backend/core/ouroboros/governance/swe_bench_pro/"
                "dataset_loader.py"
            ),
            example=str(_DATASET_SCAN_MAX_RECORDS_DEFAULT),
            since="v3.7 Stage 2 geometric-sampler (2026-05-16)",
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
    "SAMPLER_MAX_SCAN_ENV_VAR",
    "LoadOutcome",
    "ProblemSpec",
    "swe_bench_pro_enabled",
    "cache_dir",
    "load_problem",
    "list_cached_problems",
    "iter_all_dataset_records",
    "clear_cache",
    "register_flags",
]
