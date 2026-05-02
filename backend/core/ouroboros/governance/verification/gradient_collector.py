"""Priority #5 Slice 2 — CIGW async metric collectors + on-APPLY hook.

The collection layer for CIGW. Slice 1 shipped pure data + closed-
taxonomy decisions. Slice 2 (this module) ships:

  1. **MetricCollector Protocol** — sync interface taking a target
     file path + returning an InvariantSample. Tests inject
     capturing fakes; production wires the 5 default concrete
     collectors registered at module load.

  2. **5 default concrete collectors** (one per MeasurementKind from
     Slice 1's closed taxonomy):
       * LINE_COUNT — `len(file.read().splitlines())`
       * FUNCTION_COUNT — AST walk for FunctionDef + AsyncFunctionDef
       * IMPORT_COUNT — AST walk for Import + ImportFrom
       * BANNED_TOKEN_COUNT — substring count for env-tunable banned
         token list (default: the 14 banned governance imports —
         the same list used as cost-contract pin across SBT/Replay/
         Coherence/Postmortem)
       * BRANCH_COMPLEXITY — AST walk for If/For/While/Try/ExceptHandler

  3. **Dynamic collector registry** — operators register custom
     collectors via ``register_collector(kind, fn)`` at runtime.
     Default 5 registered at module load. The registry is per-kind;
     re-registration replaces with an info-level log.

  4. **Async public API** wrapping all disk + ast.parse work via
     ``asyncio.to_thread`` so the harness event loop is never
     blocked:
       * ``async sample_target(path, kinds=None) -> Tuple[Sample, ...]``
       * ``async sample_targets(paths, kinds=None) -> ...`` (bounded
         concurrency via env knob)
       * ``async sample_on_apply(op_id, target_files, *,
         enabled_override=None) -> ...`` — production wire-up
         surface for orchestrator's post-APPLY hook

ZERO LLM cost on the collection path — every metric is computed
via stdlib ``ast`` + ``file.read()``. No tool calls, no provider
invocations.

Direct-solve principles:

  * **Asynchronous** — every public API is async; sync collector
    calls wrap in ``asyncio.to_thread``. Bounded concurrency cap
    via env knob (default 4); ``asyncio.Semaphore`` enforces.

  * **Dynamic** — collector registry is mutable at runtime. Banned
    token list is env-tunable. Concurrency cap is env-tunable.
    NO hardcoded magic constants in collection logic.

  * **Adaptive** — degraded inputs (missing file, syntax error,
    permission denied) all map to InvariantSample with value=0.0
    + detail explaining (caller treats as "no signal" via Slice 1's
    severity-NONE classification on a stable-zero series).

  * **Intelligent** — caches ast.parse output across multiple
    collectors hitting the same file via per-call dict (computed
    once per target per sample_target call). Saves the parse cost
    when 5 collectors all want to walk the same module.

  * **Robust** — every public function NEVER raises out. Per-target
    failures isolated; bundle returns whatever samples succeeded.

  * **No hardcoding** — banned token list overridable via env
    (JARVIS_CIGW_BANNED_TOKENS); concurrency overridable
    (JARVIS_CIGW_COLLECTOR_CONCURRENCY); collector implementations
    overridable via register_collector. Default banned tokens
    mirror the SBT/Replay/Coherence/Postmortem cost-contract pins.

Authority invariants (AST-pinned by Slice 5):

  * NEVER imports orchestrator / phase_runners / iron_gate /
    change_engine / policy / candidate_generator / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor / semantic_guardian /
    semantic_firewall / risk_engine.

  * Read-only over source files — never writes a file, never
    executes code (defense-in-depth: ``ast.parse(mode='exec')`` is
    purely structural; compiled code is never executed).

  * No exec / eval / compile (mirrors Slice 1 + Move 6 + Priority
    #1-4 critical safety pin — tested via AST walk, not substring
    scan).

  * Reuses Slice 1 primitives (InvariantSample / MeasurementKind)
    — does NOT re-implement schema.

Master flag (Slice 1): ``JARVIS_CIGW_ENABLED``. Collector sub-flag
(this module): ``JARVIS_CIGW_COLLECTOR_ENABLED`` (default-false
until Slice 5; gates the loader path even if Slice 1's master is on
— operators can keep schemas live while disabling collection for
a cost-cap rollback).
"""
from __future__ import annotations

import asyncio
import ast as _ast
import logging
import os
import time
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

# Slice 1 reuse — pure-stdlib primitives.
from backend.core.ouroboros.governance.verification.gradient_watcher import (
    InvariantSample,
    MeasurementKind,
    cigw_enabled,
)

logger = logging.getLogger(__name__)


CIGW_COLLECTOR_SCHEMA_VERSION: str = "gradient_collector.1"


# ---------------------------------------------------------------------------
# Sub-flag — independent rollback knob from Slice 1's master
# ---------------------------------------------------------------------------


def collector_enabled() -> bool:
    """``JARVIS_CIGW_COLLECTOR_ENABLED`` — collector-loader gate.

    Asymmetric env semantics — empty/whitespace = unset = current
    default; explicit truthy/falsy overrides at call time.

    Default ``true`` — graduated 2026-05-02 in Priority #5 Slice 5.
    Both flags must be ``true`` for the collector to actually sample;
    if either is off the public surface short-circuits to empty
    tuple. Hot-revert via ``export
    JARVIS_CIGW_COLLECTOR_ENABLED=false``."""
    raw = os.environ.get(
        "JARVIS_CIGW_COLLECTOR_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default (Slice 5, 2026-05-02)
    return raw in ("1", "true", "yes", "on")


def collector_concurrency() -> int:
    """``JARVIS_CIGW_COLLECTOR_CONCURRENCY`` — max parallel
    sample_target calls in a sample_targets batch. Default 4,
    clamped [1, 16]. Higher values speed up large batches but
    contend on disk I/O."""
    try:
        raw = os.environ.get(
            "JARVIS_CIGW_COLLECTOR_CONCURRENCY", "",
        ).strip()
        if not raw:
            return 4
        return max(1, min(16, int(raw)))
    except (TypeError, ValueError):
        return 4


# ---------------------------------------------------------------------------
# Default banned-token list — mirrors SBT/Replay/Coherence/Postmortem
# cost-contract pin. Operators override via env.
# ---------------------------------------------------------------------------


_DEFAULT_BANNED_TOKENS: Tuple[str, ...] = (
    "doubleword_provider", "urgency_router", "candidate_generator",
    "orchestrator", "tool_executor", "phase_runner", "iron_gate",
    "change_engine", "auto_action_router", "subagent_scheduler",
    "semantic_guardian", "semantic_firewall", "risk_engine",
    "providers",
)


def banned_tokens() -> FrozenSet[str]:
    """``JARVIS_CIGW_BANNED_TOKENS`` — comma-separated list of
    banned substrings to count per file. Default mirrors the 14
    banned governance imports pinned across SBT/Replay/Coherence/
    Postmortem cost-contract invariants.

    Operators override via env to track domain-specific bans
    (e.g., a deprecated legacy module). Empty/whitespace tokens
    silently dropped."""
    raw = os.environ.get(
        "JARVIS_CIGW_BANNED_TOKENS", "",
    ).strip()
    if not raw:
        return frozenset(_DEFAULT_BANNED_TOKENS)
    try:
        parsed = tuple(
            t.strip() for t in raw.split(",")
            if t.strip()
        )
        if not parsed:
            return frozenset(_DEFAULT_BANNED_TOKENS)
        return frozenset(parsed)
    except Exception:  # noqa: BLE001 — defensive
        return frozenset(_DEFAULT_BANNED_TOKENS)


# ---------------------------------------------------------------------------
# Collector context — per-target shared state for the per-kind callbacks
# ---------------------------------------------------------------------------


class _CollectorContext:
    """Per-target sample state shared across the per-kind collector
    callbacks. Caches the file source + parsed AST so the 5 default
    collectors don't each re-parse the same module.

    NOT exposed in the public API — this is internal optimization."""

    __slots__ = ("path", "source", "tree", "_parse_attempted")

    def __init__(self, path: Path) -> None:
        self.path = path
        self.source: Optional[str] = None
        self.tree: Optional[_ast.Module] = None
        self._parse_attempted: bool = False

    def load_source(self) -> Optional[str]:
        """Read file contents once; cached for subsequent calls.
        NEVER raises — missing/permission-denied returns None."""
        if self.source is not None:
            return self.source
        try:
            self.source = self.path.read_text(encoding="utf-8")
            return self.source
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug(
                "[cigw_collector] read_text failed for %s: %s",
                self.path, exc,
            )
            return None

    def parse_ast(self) -> Optional[_ast.Module]:
        """Parse file's AST once; cached for subsequent calls.
        NEVER raises — syntax errors return None."""
        if self.tree is not None or self._parse_attempted:
            return self.tree
        self._parse_attempted = True
        source = self.load_source()
        if source is None:
            return None
        try:
            self.tree = _ast.parse(source)
            return self.tree
        except SyntaxError as exc:
            logger.debug(
                "[cigw_collector] ast.parse SyntaxError for %s: %s",
                self.path, exc,
            )
            return None
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[cigw_collector] ast.parse exc for %s: %s",
                self.path, exc,
            )
            return None


# ---------------------------------------------------------------------------
# MetricCollector Protocol + registry
# ---------------------------------------------------------------------------


class MetricCollector(Protocol):
    """Per-kind collector. Production wires the 5 default
    implementations registered at module load; tests inject
    capturing fakes via ``register_collector``.

    Sync — the runner wraps each call in ``asyncio.to_thread``.
    NEVER raises (defensive contract is implementation-owned;
    runner catches anyway)."""

    def __call__(self, ctx: "_CollectorContext") -> float:
        """Return the metric value for this kind. Empty/missing
        files → 0.0 (caller treats as STABLE-zero series via
        Slice 1's NONE-severity classification)."""
        ...


_collector_registry: Dict[MeasurementKind, MetricCollector] = {}


def register_collector(
    kind: MeasurementKind, fn: MetricCollector,
) -> None:
    """Register a per-kind collector. Idempotent — re-registering
    the same key with the same fn is a no-op; re-registering with
    a different fn logs an info-level message and replaces.
    NEVER raises."""
    if not isinstance(kind, MeasurementKind):
        return
    existing = _collector_registry.get(kind)
    if existing is not None and existing is not fn:
        logger.info(
            "[cigw_collector] collector for %s replaced",
            kind.value,
        )
    _collector_registry[kind] = fn


def get_collector(
    kind: MeasurementKind,
) -> Optional[MetricCollector]:
    """Return registered collector for ``kind`` or None.
    NEVER raises."""
    if not isinstance(kind, MeasurementKind):
        return None
    return _collector_registry.get(kind)


def reset_registry_for_tests() -> None:
    """Drop all registered collectors. Production code MUST NOT
    call this. Tests use it to isolate registration between test
    functions."""
    _collector_registry.clear()


# ---------------------------------------------------------------------------
# Default collectors — one per MeasurementKind
# ---------------------------------------------------------------------------


def _line_count_collector(ctx: _CollectorContext) -> float:
    """LINE_COUNT — count of logical lines (splitlines)."""
    try:
        source = ctx.load_source()
        if source is None:
            return 0.0
        return float(len(source.splitlines()))
    except Exception:  # noqa: BLE001 — defensive
        return 0.0


def _function_count_collector(ctx: _CollectorContext) -> float:
    """FUNCTION_COUNT — count of FunctionDef + AsyncFunctionDef."""
    try:
        tree = ctx.parse_ast()
        if tree is None:
            return 0.0
        count = 0
        for node in _ast.walk(tree):
            if isinstance(
                node, (_ast.FunctionDef, _ast.AsyncFunctionDef),
            ):
                count += 1
        return float(count)
    except Exception:  # noqa: BLE001 — defensive
        return 0.0


def _import_count_collector(ctx: _CollectorContext) -> float:
    """IMPORT_COUNT — count of Import + ImportFrom."""
    try:
        tree = ctx.parse_ast()
        if tree is None:
            return 0.0
        count = 0
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.Import, _ast.ImportFrom)):
                count += 1
        return float(count)
    except Exception:  # noqa: BLE001 — defensive
        return 0.0


def _banned_token_count_collector(ctx: _CollectorContext) -> float:
    """BANNED_TOKEN_COUNT — substring count for the env-tunable
    banned token list. Counts UNIQUE banned tokens present (not
    total occurrences) — operators care about whether a banned
    module is referenced AT ALL, not how often."""
    try:
        source = ctx.load_source()
        if source is None:
            return 0.0
        tokens = banned_tokens()
        if not tokens:
            return 0.0
        present = sum(1 for t in tokens if t in source)
        return float(present)
    except Exception:  # noqa: BLE001 — defensive
        return 0.0


def _branch_complexity_collector(ctx: _CollectorContext) -> float:
    """BRANCH_COMPLEXITY — count of If/For/While/Try/ExceptHandler.

    Cyclomatic-ish proxy. Drift signals control-flow complexity
    growth (e.g., a refactor that adds many error-handling
    branches)."""
    try:
        tree = ctx.parse_ast()
        if tree is None:
            return 0.0
        count = 0
        for node in _ast.walk(tree):
            if isinstance(
                node,
                (_ast.If, _ast.For, _ast.While,
                 _ast.Try, _ast.ExceptHandler),
            ):
                count += 1
        return float(count)
    except Exception:  # noqa: BLE001 — defensive
        return 0.0


# Register the 5 default collectors at module load. Operators
# override via ``register_collector`` if they need custom semantics.
register_collector(MeasurementKind.LINE_COUNT, _line_count_collector)
register_collector(
    MeasurementKind.FUNCTION_COUNT, _function_count_collector,
)
register_collector(
    MeasurementKind.IMPORT_COUNT, _import_count_collector,
)
register_collector(
    MeasurementKind.BANNED_TOKEN_COUNT,
    _banned_token_count_collector,
)
register_collector(
    MeasurementKind.BRANCH_COMPLEXITY,
    _branch_complexity_collector,
)


# ---------------------------------------------------------------------------
# Per-target sync collection — the unit asyncio.to_thread wraps
# ---------------------------------------------------------------------------


def _collect_sync(
    target_path: Path,
    kinds: Tuple[MeasurementKind, ...],
    *,
    op_id: str,
    monotonic_ts: float,
) -> Tuple[InvariantSample, ...]:
    """Sync per-target collection. Builds a _CollectorContext (which
    caches file source + AST), invokes each registered collector
    once, returns the InvariantSample tuple.

    NEVER raises. Per-collector failures yield value=0.0; missing
    file or parse error yields all collectors at 0.0."""
    try:
        ctx = _CollectorContext(target_path)
        target_id = str(target_path)
        result: List[InvariantSample] = []
        for kind in kinds:
            collector = get_collector(kind)
            if collector is None:
                continue
            try:
                value = float(collector(ctx))
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[cigw_collector] %s raised on %s: %s",
                    kind.value, target_path, exc,
                )
                value = 0.0
            result.append(InvariantSample(
                target_id=target_id,
                measurement_kind=kind,
                value=value,
                monotonic_ts=monotonic_ts,
                op_id=op_id,
            ))
        return tuple(result)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[cigw_collector] _collect_sync failed for %s: %s",
            target_path, exc,
        )
        return ()


# ---------------------------------------------------------------------------
# Async public surface — sample_target / sample_targets / sample_on_apply
# ---------------------------------------------------------------------------


def _resolve_kinds(
    kinds: Optional[Sequence[MeasurementKind]],
) -> Tuple[MeasurementKind, ...]:
    """Resolve the kinds tuple. None → all registered kinds (in
    closed-taxonomy order). Caller-supplied → filtered to the
    closed taxonomy + deduplicated preserving order."""
    if kinds is None:
        # Use closed-taxonomy enum order for stable output across
        # calls. Filter to currently-registered kinds.
        return tuple(
            k for k in MeasurementKind
            if get_collector(k) is not None
        )
    seen: set = set()
    result: List[MeasurementKind] = []
    for k in kinds:
        if not isinstance(k, MeasurementKind):
            continue
        if k in seen:
            continue
        if get_collector(k) is None:
            continue
        seen.add(k)
        result.append(k)
    return tuple(result)


def _resolve_target_path(
    target: Any,
) -> Optional[Path]:
    """Coerce a string / Path / anything-else to Path. NEVER raises.
    Returns None on garbage."""
    try:
        if isinstance(target, Path):
            return target
        if isinstance(target, str):
            return Path(target)
        return None
    except Exception:  # noqa: BLE001 — defensive
        return None


async def sample_target(
    target: Any,
    *,
    kinds: Optional[Sequence[MeasurementKind]] = None,
    op_id: str = "",
    enabled_override: Optional[bool] = None,
) -> Tuple[InvariantSample, ...]:
    """Sample one file across the configured measurement kinds.

    Wraps the sync ``_collect_sync`` in ``asyncio.to_thread`` so
    the harness event loop is never blocked on disk + AST parse.

    Resolution order:
      1. ``enabled_override is False`` → empty tuple (no-op)
      2. ``not cigw_enabled()`` (when override is None) → empty
      3. ``not collector_enabled()`` (when override is None) → empty
      4. Garbage target path → empty
      5. Empty kinds set → empty
      6. Sync collect off-thread → return tuple

    NEVER raises."""
    if enabled_override is False:
        return ()
    if enabled_override is None:
        if not cigw_enabled():
            return ()
        if not collector_enabled():
            return ()

    path = _resolve_target_path(target)
    if path is None:
        return ()

    resolved_kinds = _resolve_kinds(kinds)
    if not resolved_kinds:
        return ()

    monotonic_ts = time.monotonic()

    try:
        return await asyncio.to_thread(
            _collect_sync,
            path,
            resolved_kinds,
            op_id=op_id,
            monotonic_ts=monotonic_ts,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[cigw_collector] sample_target failed for %s: %s",
            path, exc,
        )
        return ()


async def sample_targets(
    targets: Sequence[Any],
    *,
    kinds: Optional[Sequence[MeasurementKind]] = None,
    op_id: str = "",
    enabled_override: Optional[bool] = None,
    concurrency: Optional[int] = None,
) -> Tuple[InvariantSample, ...]:
    """Sample N files in parallel under a bounded concurrency cap.

    Concurrency cap defaults to ``collector_concurrency()`` env
    knob. Callers may override per-call.

    Per-target failures isolated — bundle returns whatever samples
    succeeded across all targets.

    NEVER raises."""
    if enabled_override is False:
        return ()
    if enabled_override is None:
        if not cigw_enabled():
            return ()
        if not collector_enabled():
            return ()

    if not targets:
        return ()
    try:
        target_list = list(targets)
    except TypeError:
        return ()
    if not target_list:
        return ()

    cap = (
        max(1, min(16, int(concurrency)))
        if concurrency is not None
        else collector_concurrency()
    )
    semaphore = asyncio.Semaphore(cap)

    async def _process_one(t: Any) -> Tuple[InvariantSample, ...]:
        async with semaphore:
            return await sample_target(
                t, kinds=kinds, op_id=op_id,
                enabled_override=True,  # we already gated above
            )

    try:
        results = await asyncio.gather(
            *(_process_one(t) for t in target_list),
            return_exceptions=False,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[cigw_collector] sample_targets gather: %s", exc,
        )
        return ()

    flat: List[InvariantSample] = []
    for batch in results:
        if isinstance(batch, tuple):
            flat.extend(batch)
    return tuple(flat)


async def sample_on_apply(
    op_id: str,
    target_files: Sequence[Any],
    *,
    kinds: Optional[Sequence[MeasurementKind]] = None,
    enabled_override: Optional[bool] = None,
) -> Tuple[InvariantSample, ...]:
    """On-APPLY hook for orchestrator wire-up.

    Production callers invoke this after every successful APPLY
    phase with the op_id + the list of files just modified. Returns
    the per-file structural-metric samples that downstream Slice 4
    persists + Slice 3 aggregates.

    Empty / garbage op_id → empty tuple. Empty target_files →
    empty tuple. Otherwise → batch ``sample_targets`` with the
    op_id stamped on every sample.

    NEVER raises."""
    sid = str(op_id or "").strip()
    if not sid:
        return ()
    if not target_files:
        return ()
    return await sample_targets(
        target_files, kinds=kinds, op_id=sid,
        enabled_override=enabled_override,
    )


# ---------------------------------------------------------------------------
# Cost-contract authority constant (AST-pin target for Slice 5)
# ---------------------------------------------------------------------------


COST_CONTRACT_PRESERVED_BY_CONSTRUCTION: bool = True


__all__ = [
    "CIGW_COLLECTOR_SCHEMA_VERSION",
    "COST_CONTRACT_PRESERVED_BY_CONSTRUCTION",
    "MetricCollector",
    "banned_tokens",
    "collector_concurrency",
    "collector_enabled",
    "get_collector",
    "register_collector",
    "reset_registry_for_tests",
    "sample_on_apply",
    "sample_target",
    "sample_targets",
]
