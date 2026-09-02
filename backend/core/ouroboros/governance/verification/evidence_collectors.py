"""Priority F — Evidence collector extension.

Closes the verification loop end-to-end. Pre-F, soak #4 produced
postmortems with `total_claims=3` (good — Priority A working) but
ALL claims evaluated to `INSUFFICIENT_EVIDENCE` because the existing
`ctx_evidence_collector` (Slice 2.4) only knows about the ORIGINAL
claim kinds (`test_passes`, `key_present`) — not the three Priority A
default kinds:

  * `file_parses_after_change` (needs `target_files_post`)
  * `test_set_hash_stable` (needs `test_files_pre` + `test_files_post`)
  * `no_new_credential_shapes` (needs `diff_text`)

This module ships the canonical surface for evidence gathering:

  * `EvidenceGatherer` — frozen, hashable spec (kind + description +
    async gather function).
  * Registry pattern (mirrors A2 default_claims, B1 dormancy
    detectors, C test strategies, E shipped-code invariants):
    `register_evidence_gatherer` / `unregister` / `list` /
    `reset_for_tests`. Idempotent on identical re-register; rejects
    different-callable without `overwrite=True`.
  * `dispatch_evidence_gather(claim, ctx)` — pure async dispatcher:
    looks up the registered gatherer for `claim.property.kind`,
    invokes it, returns the evidence mapping. Falls back to empty
    mapping for unregistered kinds (caller's existing fallback chain
    runs).
  * Three default gatherers for Priority A kinds, each with a
    self-gathering fallback when ctx attrs aren't pre-stamped:
    - `file_parses_after_change` reads `ctx.target_files_post` if
      stamped; falls back to reading `ctx.target_files` from disk
    - `test_set_hash_stable` reads `ctx.test_files_pre` /
      `ctx.test_files_post` if stamped; falls back to globbing
      `tests/**/*.py` (post only, no pre fallback — pre-state must
      be captured at PLAN time)
    - `no_new_credential_shapes` reads `ctx.diff_text` if stamped;
      no self-gather fallback (diff is unavailable post-APPLY
      without explicit capture)

Authority invariants (AST-pinned by tests):
  * No imports of orchestrator / phase_runner / candidate_generator /
    iron_gate / change_engine / policy / semantic_guardian.
  * Pure stdlib + verification.* (own slice family).
  * NEVER raises out of any public method.
  * Read-only over the filesystem — never writes back.

Master flag `JARVIS_EVIDENCE_COLLECTORS_ENABLED` (default `true`).
When off, `dispatch_evidence_gather` returns `{}` for every claim
and the legacy ctx_evidence_collector hardcoded paths run.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Coroutine,
    Dict,
    List,
    Mapping,
    Optional,
    Tuple,
)

logger = logging.getLogger(__name__)


EVIDENCE_COLLECTOR_SCHEMA_VERSION: str = "evidence_collector.1"


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def evidence_collectors_enabled() -> bool:
    """``JARVIS_EVIDENCE_COLLECTORS_ENABLED`` (default ``true``).

    When off, ``dispatch_evidence_gather`` returns an empty mapping
    for every claim and the legacy ctx_evidence_collector hardcoded
    paths run. Hot-revert: a single env knob."""
    raw = os.environ.get(
        "JARVIS_EVIDENCE_COLLECTORS_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# EvidenceGatherer — registry value type
# ---------------------------------------------------------------------------


# An evidence gatherer takes (claim, ctx) and returns an evidence
# mapping (claim.property.evidence_required keys → values). NEVER
# raises — gatherers must catch their own errors and return {} on
# failure (the caller's INSUFFICIENT_EVIDENCE path handles missing
# evidence cleanly).
EvidenceGatherFn = Callable[
    [Any, Any],
    "Coroutine[Any, Any, Mapping[str, Any]]",
]


@dataclass(frozen=True)
class EvidenceGatherer:
    """One evidence-gathering spec. Frozen + hashable for safe
    registry storage."""

    kind: str
    description: str
    gather: EvidenceGatherFn
    schema_version: str = EVIDENCE_COLLECTOR_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_REGISTRY: Dict[str, EvidenceGatherer] = {}
_REGISTRY_LOCK = threading.RLock()


def register_evidence_gatherer(
    gatherer: EvidenceGatherer, *, overwrite: bool = False,
) -> None:
    """Install an evidence gatherer. NEVER raises. Idempotent on
    identical re-register; rejects different-callable without
    overwrite=True."""
    if not isinstance(gatherer, EvidenceGatherer):
        return
    safe_kind = (
        str(gatherer.kind).strip() if gatherer.kind else ""
    )
    if not safe_kind:
        return
    with _REGISTRY_LOCK:
        existing = _REGISTRY.get(safe_kind)
        if existing is not None:
            if existing == gatherer:
                return
            if not overwrite:
                logger.info(
                    "[EvidenceCollectors] gatherer %r already registered",
                    safe_kind,
                )
                return
        _REGISTRY[safe_kind] = gatherer


def unregister_evidence_gatherer(kind: str) -> bool:
    """Remove a gatherer. Returns True if removed. NEVER raises."""
    safe_kind = str(kind).strip() if kind else ""
    if not safe_kind:
        return False
    with _REGISTRY_LOCK:
        return _REGISTRY.pop(safe_kind, None) is not None


def list_evidence_gatherers() -> Tuple[EvidenceGatherer, ...]:
    """Return all gatherers in stable alphabetical order."""
    with _REGISTRY_LOCK:
        return tuple(_REGISTRY[k] for k in sorted(_REGISTRY.keys()))


def is_kind_registered(kind: str) -> bool:
    """True iff a gatherer is registered for ``kind``."""
    safe_kind = str(kind).strip() if kind else ""
    if not safe_kind:
        return False
    with _REGISTRY_LOCK:
        return safe_kind in _REGISTRY


def reset_registry_for_tests() -> None:
    """Test isolation."""
    with _REGISTRY_LOCK:
        _REGISTRY.clear()
    _register_seed_gatherers()


# ---------------------------------------------------------------------------
# Dispatcher — pure async; gates on master flag + claim shape
# ---------------------------------------------------------------------------


async def dispatch_evidence_gather(
    claim: Any, ctx: Any,
) -> Mapping[str, Any]:
    """Dispatch evidence gathering for ``claim`` via the registered
    gatherer for ``claim.property.kind``. NEVER raises.

    Returns:
      * Empty mapping when the master flag is off
      * Empty mapping when the claim or its property is malformed
      * Empty mapping when no gatherer is registered for the kind
        (caller falls back to legacy hardcoded paths)
      * Whatever the gatherer returns on success (also a mapping;
        gatherers swallow their own errors and return {} on failure)
    """
    if not evidence_collectors_enabled():
        return {}
    if claim is None:
        return {}
    prop = getattr(claim, "property", None)
    if prop is None:
        return {}
    kind = getattr(prop, "kind", "")
    if not kind:
        return {}
    safe_kind = str(kind).strip()
    with _REGISTRY_LOCK:
        gatherer = _REGISTRY.get(safe_kind)
    if gatherer is None:
        return {}
    try:
        result = await gatherer.gather(claim, ctx)
    except Exception:  # noqa: BLE001 — defensive (gatherer should
        # itself never raise, but defense-in-depth)
        logger.debug(
            "[EvidenceCollectors] gatherer %r raised", safe_kind,
            exc_info=True,
        )
        return {}
    if not isinstance(result, Mapping):
        return {}
    return dict(result)


# ---------------------------------------------------------------------------
# Default gatherers for Priority A claim kinds
# ---------------------------------------------------------------------------


async def _gather_file_parses_after_change(
    claim: Any, ctx: Any,
) -> Mapping[str, Any]:
    """Gather evidence for the ``file_parses_after_change`` claim.

    Resolution chain:
      1. If ``ctx.target_files_post`` is already stamped (by future
         APPLY-phase enrichment), pass through as-is.
      2. Else, self-gather: read each file in ``ctx.target_files``
         from disk and produce ``[{path, content}, ...]``.

    NEVER raises. On any I/O failure, returns ``{}`` (Oracle returns
    INSUFFICIENT_EVIDENCE — honest about the gap)."""
    try:
        # Priority 1 — pre-stamped (future Slice F2 wiring)
        stamped = _from_ctx_or_ledger(
            ctx, "target_files_post").get("target_files_post")
        if stamped is not None:
            return {"target_files_post": list(stamped)}

        # Priority 2 — self-gather from disk
        targets = getattr(ctx, "target_files", None)
        if not targets:
            return {}
        # Same wedge class as the tests walk above, one order of
        # magnitude smaller: exists() + is_file() + read_text() is three
        # DrvFS round trips per target, serialised in the coroutine.
        # `_stat_and_read` collapses the two stats into one and the
        # whole per-file step runs on the advisor-blast executor; the
        # targets are then read CONCURRENTLY, because this cost is
        # latency-dominated and the executor -- not this loop -- is what
        # bounds the fan-out.
        out: List[Dict[str, Any]] = []
        gathered = await _read_targets_offloaded(
            [str(t) for t in targets],
        )
        for item in gathered:
            if item is not None:
                out.append(item)
        return {"target_files_post": out}
    except Exception:  # noqa: BLE001 — defensive
        return {}


def _stat_and_read(path_str: str) -> Optional[Dict[str, Any]]:
    """Classify one target and read it. Executor-side; NEVER raises.

    Returns the ``{path, content}`` record, or ``None`` for a target
    that must be SKIPPED. The three outcomes are exactly the ones the
    inline version produced, preserved deliberately:

      * missing  -> ``{"path": ..., "content": ""}``. Absence is
        evidence: the evaluator treats an absent ``.py`` as the
        SyntaxError-equivalent for ``file_parses_after_change``.
      * not a regular file (a directory, a socket) -> ``None``. Not a
        parse failure, not evidence of anything; skipped.
      * unreadable -> ``None``. Honest gap, never a fabricated "".

    One ``os.stat`` replaces ``exists()`` + ``is_file()``: those are two
    syscalls answering one question, and on DrvFS each is a round trip.
    The read is deliberately UNBOUNDED -- this evidence is fed to an AST
    parse, and a truncated file does not parse, so a byte cap would
    manufacture the exact SyntaxError the claim exists to detect.
    """
    import os  # noqa: PLC0415
    import stat as _stat  # noqa: PLC0415

    try:
        p = Path(path_str)
        try:
            st = os.stat(p)
        except (FileNotFoundError, NotADirectoryError):
            return {"path": str(p), "content": ""}
        except OSError:
            return None
        if not _stat.S_ISREG(st.st_mode):
            return None
        return {
            "path": str(p),
            "content": p.read_text(encoding="utf-8", errors="replace"),
        }
    except Exception:  # noqa: BLE001 — executor payloads never raise out
        return None


async def _read_targets_offloaded(
    paths: List[str],
) -> List[Optional[Dict[str, Any]]]:
    """Read every target off-loop, concurrently. NEVER raises.

    Falls back to in-line synchronous reads when the substrate is
    unavailable, so behaviour is preserved on a bare checkout rather
    than silently losing evidence. A per-target failure yields ``None``
    for that target only -- one unreadable file must not discard the
    others.
    """
    try:
        from backend.core.ouroboros.governance.cooperative_fs_io import (  # noqa: E501,PLC0415
            is_offload_error,
            offload,
        )
    except Exception:  # noqa: BLE001
        return [_stat_and_read(p) for p in paths]
    import asyncio  # noqa: PLC0415

    try:
        settled = await asyncio.gather(
            *(offload(_stat_and_read, p, cpu_bound=False) for p in paths),
            return_exceptions=True,
        )
    except Exception:  # noqa: BLE001 — gather itself must not propagate
        logger.debug(
            "[EvidenceCollectors] offloaded target reads failed",
            exc_info=True,
        )
        return [None] * len(paths)
    out: List[Optional[Dict[str, Any]]] = []
    for item in settled:
        if isinstance(item, BaseException) or is_offload_error(item):
            out.append(None)
        elif item is None or isinstance(item, dict):
            out.append(item)
        else:  # pragma: no cover — an executor cannot return this shape
            out.append(None)
    return out


def _from_ctx_or_ledger(ctx: Any, *keys: str) -> Dict[str, Any]:
    """Resolve evidence keys from the ctx first, then from the ledger.

    The ctx is the fast path — same object, no I/O — and it is also the one
    that does not survive. ``test_files_pre``, ``test_files_post``,
    ``diff_text`` and ``target_files_post`` are stamped with
    ``object.__setattr__`` and are not declared fields of the frozen
    ``OperationContext``, so ``dataclasses.replace(ctx, ...)`` — called at
    more than ten sites for ordinary reasons — rebuilds without them.

    That single fact accounts for 13,911 of 18,414 claims returning
    INSUFFICIENT_EVIDENCE, and for why exactly one of the three Priority A
    gatherers worked: ``file_parses_after_change`` falls back to
    ``ctx.target_files``, which IS declared.

    Reading the ledger second is not a fallback in the "try something worse"
    sense — it is the durable copy, written beside the claim it settles and
    keyed by the same op_id the claim is read back by. NEVER raises; a key
    that resolves nowhere is simply absent, which the Oracle reports as
    INSUFFICIENT_EVIDENCE, honestly.
    """
    out: Dict[str, Any] = {}
    missing = []
    for key in keys:
        got = getattr(ctx, key, None)
        if got is None:
            missing.append(key)
        else:
            out[key] = got
    if not missing:
        return out
    op_id = str(getattr(ctx, "op_id", "") or "").strip()
    if not op_id:
        return out
    try:
        from backend.core.ouroboros.governance.verification.evidence_ledger import (
            recorded_evidence,
        )
        recorded = recorded_evidence(op_id=op_id)
    except Exception:  # noqa: BLE001 — reader unavailable; ctx-only
        logger.debug("[EvidenceCollectors] ledger read failed", exc_info=True)
        return out
    for key in missing:
        got = recorded.get(key)
        if got is not None:
            out[key] = got
    return out


async def _gather_cost_contract_bg_op_did_not_use_claude(
    claim: Any, ctx: Any,
) -> Mapping[str, Any]:
    """Evidence for the cost-contract claim, which had no gatherer at all.

    It is attached to every op as ``must_hold`` and returned
    INSUFFICIENT_EVIDENCE every single time — 4,637 of them — because its
    kind was never registered, so the dispatcher fell through to the legacy
    hardcoded paths, which know only ``test_passes`` and ``key_present``.

    Nothing had to be built to settle it. Two of its three required keys,
    ``provider_route`` and ``is_read_only``, are DECLARED fields of the
    context and therefore survive every copy. The third, ``providers_used``,
    appears nowhere in the codebase — but ``provider_selection`` records
    carrying ``provider_name`` have been written on every GENERATE all along;
    2,220 of them were sitting on this repository's ledger while the claim
    that needed them went unjudged.

    Absent evidence stays absent. A route that was never assigned is not the
    same as a BG route, and guessing one would let a claim about cost
    discipline pass on an op whose dispatch nobody recorded.

    NEVER raises.
    """
    try:
        out: Dict[str, Any] = {}
        route = getattr(ctx, "provider_route", None)
        if route is not None and str(route).strip():
            out["provider_route"] = str(route).strip()
        read_only = getattr(ctx, "is_read_only", None)
        if isinstance(read_only, bool):
            out["is_read_only"] = read_only

        op_id = str(getattr(ctx, "op_id", "") or "").strip()
        if op_id:
            try:
                from backend.core.ouroboros.governance.verification.evidence_ledger import (  # noqa: E501
                    recorded_providers_used,
                )
                # An op that dispatched to nobody legitimately used no
                # providers, and the empty tuple is the correct evidence for
                # that — distinct from the key being absent, which means the
                # ledger could not be read.
                out["providers_used"] = list(
                    recorded_providers_used(op_id=op_id))
            except Exception:  # noqa: BLE001
                logger.debug("[EvidenceCollectors] providers_used unavailable",
                             exc_info=True)
        return out
    except Exception:  # noqa: BLE001
        return {}


async def _gather_test_set_hash_stable(
    claim: Any, ctx: Any,
) -> Mapping[str, Any]:
    """Gather evidence for the ``test_set_hash_stable`` claim.

    Resolution chain:
      1. If both ``ctx.test_files_pre`` AND ``ctx.test_files_post``
         are stamped, pass through.
      2. Else, self-gather post-state by globbing ``tests/**/*.py``
         under ``ctx.target_dir`` (or project root). Pre-state has
         no self-gather fallback — without a PLAN-time snapshot the
         claim correctly evaluates to INSUFFICIENT_EVIDENCE.

    NEVER raises."""
    try:
        resolved = _from_ctx_or_ledger(ctx, "test_files_pre", "test_files_post")
        pre = resolved.get("test_files_pre")
        post = resolved.get("test_files_post")
        if pre is not None and post is not None:
            return {
                "test_files_pre": list(pre),
                "test_files_post": list(post),
            }

        # Pre-state cannot be self-gathered post-APPLY (the original
        # state is gone). Honest INSUFFICIENT_EVIDENCE.
        if pre is None:
            return {}

        # Post can self-gather — OFF the event loop.
        #
        # This walk used to be `base.glob("tests/**/*.py")` with a
        # `p.is_file()` per hit, run inline in this coroutine. `glob`
        # stats every entry and `is_file()` stats each one again; on a
        # /mnt/c (DrvFS) tree each stat is a round trip to the Windows
        # filesystem driver. Soak bt-2026-09-02-003459 measured the
        # result: the main loop blocked 46.9 s here during POSTMORTEM,
        # and repeated stalls tripped the out-of-process heartbeat
        # watchdog, which SIGKILLed the session and lost every
        # in-flight trajectory. It is the same class of wedge Slice 12U
        # was built to eradicate -- `predictive_engine._fragility`
        # doing rglob+read_text on the loop -- so it composes that
        # substrate rather than growing a private thread hop.
        target_dir = getattr(ctx, "target_dir", None) or "."
        try:
            base = Path(str(target_dir))
        except Exception:  # noqa: BLE001
            base = Path(".")
        result = await _walk_tests_offloaded(base)
        if result is None:
            return {}
        # A TRUNCATED walk is not a smaller answer, it is a WRONG one:
        # this claim compares a set hash, so a post-set missing files
        # the budget never reached reads as "the test set changed" and
        # fails a candidate that changed nothing. Budget exhaustion is
        # an absence of evidence, and this module says so rather than
        # guessing -- the same choice the pre-state branch above makes.
        if result.truncated:
            logger.debug(
                "[EvidenceCollectors] test_set_hash_stable walk %s "
                "(%s, scanned=%d) — reporting INSUFFICIENT_EVIDENCE "
                "rather than a partial set",
                base, result.truncation_reason(), result.scanned_count,
            )
            return {}
        return {
            "test_files_pre": list(pre),
            "test_files_post": list(result.matches),
        }
    except Exception:  # noqa: BLE001
        return {}


async def _walk_tests_offloaded(base: Path) -> Any:
    """Bounded walk of ``base/tests`` for ``*.py``, off the event loop.

    Returns the canonical ``BoundedWalkResult`` (so the caller can see
    ``truncated``), or ``None`` when the walk could not be performed at
    all — a missing substrate, a pool fault, or a root that is not a
    directory. NEVER raises.

    Composition, not reimplementation:
      * ``bounded_walker.bounded_glob`` already owns the walk, the
        skip-dirs pruning, the budget contract and the truncation
        vocabulary. Its budgets come from the operator's own
        ``JARVIS_BLAST_RADIUS_*`` knobs -- passing ``None`` here means
        "whatever this deployment configured for filesystem scans",
        which is why nothing in this function is a literal.
      * ``cooperative_fs_io.offload(..., cpu_bound=False)`` puts it on
        the dedicated ``advisor-blast`` executor. Not
        ``asyncio.to_thread``: that targets the contested default pool
        shared with sensors, the Oracle and DreamEngine -- the
        antipattern Slice 12S introduced and 12T reverted.

    The pattern is a FILENAME pattern (``bounded_glob`` fnmatches
    ``entry.name``), so the recursion is expressed by rooting the walk
    at ``base/tests`` rather than by a ``tests/**/`` path glob. The
    walker also yields regular files only -- directories are traversed,
    never matched -- which is what the discarded ``is_file()`` call was
    for.
    """
    try:
        from backend.core.ouroboros.governance.bounded_walker import (  # noqa: E501,PLC0415
            bounded_glob,
        )
        from backend.core.ouroboros.governance.cooperative_fs_io import (  # noqa: E501,PLC0415
            is_offload_error,
            offload,
        )
    except Exception:  # noqa: BLE001 — substrate optional; degrade honestly
        logger.debug(
            "[EvidenceCollectors] cooperative FS substrate unavailable",
            exc_info=True,
        )
        return None
    try:
        result = await offload(
            bounded_glob, base / "tests", "*.py", cpu_bound=False,
        )
    except Exception:  # noqa: BLE001 — offload itself must never propagate
        logger.debug(
            "[EvidenceCollectors] offloaded tests walk failed for %s",
            base, exc_info=True,
        )
        return None
    # `offload` reports failure by RETURNING a sentinel, so a plain
    # try/except would sail past it and hand a sentinel to the caller
    # as if it were a walk result.
    if is_offload_error(result) or not hasattr(result, "matches"):
        return None
    return result


async def _gather_no_new_credential_shapes(
    claim: Any, ctx: Any,
) -> Mapping[str, Any]:
    """Gather evidence for the ``no_new_credential_shapes`` claim.

    Resolution chain:
      1. If ``ctx.diff_text`` is stamped (by future APPLY-phase
         enrichment), pass through as-is.
      2. No self-gather fallback — without an explicit diff we
         can't faithfully detect "newly introduced" credentials
         (full file content would flag pre-existing credentials).
         Honest INSUFFICIENT_EVIDENCE.

    NEVER raises."""
    try:
        diff = _from_ctx_or_ledger(ctx, "diff_text").get("diff_text")
        if diff is None:
            return {}
        # Coerce to string defensively
        if isinstance(diff, bytes):
            diff_str = diff.decode("utf-8", errors="replace")
        else:
            diff_str = str(diff)
        return {"diff_text": diff_str}
    except Exception:  # noqa: BLE001
        return {}


def _register_seed_gatherers() -> None:
    """Module-load: register the three Priority A seed gatherers.
    Idempotent — re-registering the same callable is a silent no-op."""
    register_evidence_gatherer(
        EvidenceGatherer(
            kind="file_parses_after_change",
            description=(
                "Gathers target_files_post from ctx (pre-stamped by "
                "APPLY) or self-gathers by reading ctx.target_files "
                "from disk."
            ),
            gather=_gather_file_parses_after_change,
        ),
    )
    register_evidence_gatherer(
        EvidenceGatherer(
            kind="cost_contract_bg_op_did_not_use_claude",
            description=(
                "Gathers provider_route + is_read_only from the ctx's "
                "DECLARED fields and providers_used from the "
                "provider_selection records already on the ledger. Was "
                "never registered, so the claim evaluated "
                "INSUFFICIENT_EVIDENCE on every op it was attached to."
            ),
            gather=_gather_cost_contract_bg_op_did_not_use_claude,
        ),
    )
    register_evidence_gatherer(
        EvidenceGatherer(
            kind="test_set_hash_stable",
            description=(
                "Gathers test_files_pre + test_files_post from ctx; "
                "post can self-gather via tests/**/*.py glob, pre "
                "must be PLAN-time stamped."
            ),
            gather=_gather_test_set_hash_stable,
        ),
    )
    register_evidence_gatherer(
        EvidenceGatherer(
            kind="no_new_credential_shapes",
            description=(
                "Gathers diff_text from ctx; no self-gather fallback "
                "because full-file scan would flag pre-existing "
                "credentials."
            ),
            gather=_gather_no_new_credential_shapes,
        ),
    )


_register_seed_gatherers()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "EVIDENCE_COLLECTOR_SCHEMA_VERSION",
    "EvidenceGatherer",
    "EvidenceGatherFn",
    "dispatch_evidence_gather",
    "evidence_collectors_enabled",
    "is_kind_registered",
    "list_evidence_gatherers",
    "register_evidence_gatherer",
    "reset_registry_for_tests",
    "unregister_evidence_gatherer",
]
