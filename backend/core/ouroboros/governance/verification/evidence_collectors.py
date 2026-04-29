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
        stamped = getattr(ctx, "target_files_post", None)
        if stamped is not None:
            return {"target_files_post": list(stamped)}

        # Priority 2 — self-gather from disk
        targets = getattr(ctx, "target_files", None)
        if not targets:
            return {}
        out: List[Dict[str, Any]] = []
        for path_str in targets:
            try:
                p = Path(str(path_str))
                if not p.exists():
                    # File doesn't exist post-op — record without
                    # content; the evaluator will treat absent .py
                    # as a SyntaxError-equivalent if relevant.
                    out.append({"path": str(p), "content": ""})
                    continue
                if not p.is_file():
                    continue
                content = p.read_text(
                    encoding="utf-8", errors="replace",
                )
                out.append({"path": str(p), "content": content})
            except OSError:
                continue
        return {"target_files_post": out}
    except Exception:  # noqa: BLE001 — defensive
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
        pre = getattr(ctx, "test_files_pre", None)
        post = getattr(ctx, "test_files_post", None)
        if pre is not None and post is not None:
            return {
                "test_files_pre": list(pre),
                "test_files_post": list(post),
            }

        # Pre-state cannot be self-gathered post-APPLY (the original
        # state is gone). Honest INSUFFICIENT_EVIDENCE.
        if pre is None:
            return {}

        # Post can self-gather.
        target_dir = getattr(ctx, "target_dir", None) or "."
        try:
            base = Path(str(target_dir))
        except Exception:  # noqa: BLE001
            base = Path(".")
        if not base.exists() or not base.is_dir():
            return {}
        post_set: List[str] = []
        try:
            for p in base.glob("tests/**/*.py"):
                if p.is_file():
                    post_set.append(str(p))
        except (OSError, ValueError):
            pass
        return {
            "test_files_pre": list(pre),
            "test_files_post": post_set,
        }
    except Exception:  # noqa: BLE001
        return {}


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
        diff = getattr(ctx, "diff_text", None)
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
