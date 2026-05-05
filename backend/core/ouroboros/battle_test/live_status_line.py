"""LiveStatusLineRenderer — wires StatusLineBuilder into the REPL's
``bottom_toolbar`` callable so phase / cost / route / risk surface live
to operators while they type.
========================================================================

Slice 1 of the **Gap #1 + Gap #5 closure arc**.

Root problem
------------

The REPL's existing ``_bottom_toolbar`` callable (``serpent_flow.py:4043``)
shows only the **swarm digest** (active op count + lens + last event).
The operator-load-bearing live data — current phase + sub-detail, cost
spent vs budget, route badge, op-id, risk tier — is computed by the
fully-built :class:`StatusLineBuilder` (``status_line.py``) but never
rendered anywhere persistent. The harness already constructs and
registers the builder with the right providers (``harness.py:1389``);
the consumer side (this module) is the missing link.

The original audit also flagged "Gap #5 — REPL is blocking, no live
background updates." Closer reading shows ``patch_stdout(raw=True)``
(``serpent_flow.py:4163``) already interleaves concurrent output above
the prompt — concurrent prints DO appear during typing. What's missing
is a *fixed* status surface, which is exactly Gap #1. So this slice
closes the operator-visible aspect of both gaps.

Architectural reuse — zero duplication
---------------------------------------

* :class:`StatusLineBuilder` (``status_line.py``) — the entire
  rendering machinery: pull-model snapshot, color gradient, compact-
  mode gate, TTY gate, kill switch. We just call ``.render()``.
* :func:`get_status_line_builder` / ``register_status_line_builder``
  — already wired by ``harness.py``. We consult, never replace.
* ``_bottom_toolbar`` callable in ``serpent_flow.py`` — Slice 1 edit
  appends our line to its existing swarm-digest output. One layout
  primitive, two information slots.

Authority boundary
------------------

* §1 deterministic — pure read-only render; no LLM, no I/O on the hot path
* §7 fail-closed — every pull has a documented degradation path
  (builder unregistered → empty; TTY gate false → empty; render raise →
  empty). The bottom-toolbar callable NEVER raises.
* §8 observable — the builder's existing :class:`StatusSnapshot`
  projection covers SSE / observability; this module only adds the
  *display* path.

What this module does NOT do
----------------------------

* Construct ``StatusLineBuilder`` — that's the harness's job
  (``harness.py:1389``).
* Manage refresh ticks — the existing ``refresh_interval`` on the
  ``PromptSession`` (``_REPL_REFRESH_INTERVAL_S = 0.10``) drives
  redraws. We don't add a second cadence.
* Mutate any subsystem state — pure read-only.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger("Ouroboros.LiveStatusLine")


# ===========================================================================
# Schema + master flag
# ===========================================================================


LIVE_STATUS_LINE_SCHEMA_VERSION: str = "live_status_line.v1"


MASTER_FLAG_ENV_VAR: str = "JARVIS_LIVE_STATUS_LINE_ENABLED"


def is_master_flag_enabled() -> bool:
    """Read :data:`MASTER_FLAG_ENV_VAR`. **Default true** post Slice 5
    graduation (2026-05-04). Operators flip ``=false`` for instant
    rollback to swarm-digest-only legacy behavior. NEVER raises."""
    raw = os.environ.get(MASTER_FLAG_ENV_VAR, "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


# ===========================================================================
# Frozen render result — exposed to tests + observability
# ===========================================================================


@dataclass(frozen=True)
class LiveStatusLineRender:
    """Structured output of one render pass.

    Fields
    ------
    * ``swarm_segment`` — original ``_bottom_toolbar`` content
      (active op count + lens + last event). Always present when the
      caller passes one.
    * ``status_segment`` — :meth:`StatusLineBuilder.render` output
      (phase / cost / route / risk). Empty when builder is
      unregistered / disabled / render raised.
    * ``combined`` — newline-joined view ready for prompt_toolkit
      ``ANSI(...)`` wrapping. Empty segments are dropped from the join.
    """

    swarm_segment: str
    status_segment: str
    combined: str
    schema_version: str = LIVE_STATUS_LINE_SCHEMA_VERSION


# ===========================================================================
# StatusLineBuilder consumer — defensive lookup + render
# ===========================================================================


def render_status_segment() -> str:
    """Pull-and-render the registered :class:`StatusLineBuilder`.

    Returns the rendered status line, or an empty string when:

      * The Slice 1 master flag is off (graduation gate).
      * No builder is registered (no harness has booted).
      * ``StatusLineBuilder.should_render()`` returns False (TTY gate /
        kill-switch — already implemented in ``status_line.py``).
      * ``snapshot()`` or ``render()`` raised.

    NEVER raises. The bottom-toolbar callable is allowed to assume
    this always returns a string.
    """
    if not is_master_flag_enabled():
        return ""
    try:
        from backend.core.ouroboros.battle_test.status_line import (
            get_status_line_builder,
            should_render,
        )
    except ImportError:
        return ""
    try:
        if not should_render():
            return ""
        builder = get_status_line_builder()
        if builder is None:
            return ""
        # ``StatusLineBuilder`` exposes ``render_plain()`` which
        # internally snapshots + formats. We use that (rather than
        # snapshot() + a custom render) so the existing kill-switch /
        # compact-mode / TTY-gate paths inside the builder all stay
        # honored — zero parallel rendering surface, zero duplication.
        rendered = builder.render_plain()
        return rendered if isinstance(rendered, str) else ""
    except Exception:  # noqa: BLE001
        logger.debug(
            "[LiveStatusLine] render_status_segment defensive catch",
            exc_info=True,
        )
        return ""


# ===========================================================================
# Compose — merge swarm digest with status segment
# ===========================================================================


def compose(swarm_segment: object) -> LiveStatusLineRender:
    """Merge the swarm-digest segment (caller-provided) with the
    status-line segment (pulled from the registered builder).

    The two are joined with a newline so prompt_toolkit's
    ``bottom_toolbar`` renders them as two stacked lines. Either may
    be empty:

      * Swarm digest empty → operator sees only the status line.
      * Status segment empty → operator sees only the swarm digest
        (legacy behavior preserved).
      * Both empty → caller can short-circuit by checking
        ``combined == ""``.

    NEVER raises — non-string ``swarm_segment`` coerces to empty.
    """
    swarm_safe = swarm_segment if isinstance(swarm_segment, str) else ""
    status_safe = render_status_segment()
    parts = [s for s in (swarm_safe, status_safe) if s]
    combined = "\n".join(parts)
    return LiveStatusLineRender(
        swarm_segment=swarm_safe,
        status_segment=status_safe,
        combined=combined,
    )


# ===========================================================================
# Convenience: prompt_toolkit-friendly callable factory
# ===========================================================================


def make_bottom_toolbar_callable(
    swarm_callable: Callable[[], object],
) -> Callable[[], object]:
    """Build a ``bottom_toolbar`` callable that merges ``swarm_callable``'s
    output with the registered status segment.

    Returns a function suitable for direct use as ``PromptSession(
    bottom_toolbar=...)``. The wrapped function returns the
    prompt_toolkit ``ANSI`` wrapper when both segments are non-empty,
    or the original swarm output (passed through unchanged) when the
    master flag is off OR no builder is registered.

    The caller in ``serpent_flow.py`` can replace its existing
    ``bottom_toolbar=_bottom_toolbar`` argument with
    ``bottom_toolbar=make_bottom_toolbar_callable(_bottom_toolbar)``
    and get the merged behavior with zero other changes — Slice 1's
    backwards-compat contract.
    """
    def _wrapped() -> object:
        # Pull the legacy swarm-digest output (returns ANSI / str /
        # whatever the caller's wrapper produces).
        try:
            raw = swarm_callable()
        except Exception:  # noqa: BLE001
            logger.debug(
                "[LiveStatusLine] swarm_callable raised; "
                "passing through empty",
                exc_info=True,
            )
            raw = ""

        # Master flag off OR no status segment → pass through unchanged
        # (preserves byte-identical legacy behavior).
        status_safe = render_status_segment()
        if not status_safe:
            return raw

        # Merge: convert ``raw`` to a plain string for joining. The
        # original ANSI wrapper (if any) is preserved by re-wrapping
        # with ANSI at the join site so escape codes survive.
        try:
            from prompt_toolkit.formatted_text import ANSI
        except ImportError:
            # No prompt_toolkit available — return joined plain text.
            raw_str = _to_plain_str(raw)
            return f"{raw_str}\n{status_safe}" if raw_str else status_safe

        raw_str = _to_plain_str(raw)
        if not raw_str:
            return ANSI(status_safe)
        return ANSI(f"{raw_str}\n{status_safe}")

    return _wrapped


def _to_plain_str(raw: object) -> str:
    """Coerce a prompt_toolkit ``ANSI(...)``, ``HTML(...)``, or plain
    str into a plain string. Non-string-coercible inputs return
    empty. NEVER raises."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    # Prompt-toolkit ``ANSI`` / ``HTML`` carry the raw text on
    # ``.value``. Defensive getattr to avoid coupling to private API.
    inner = getattr(raw, "value", None)
    if isinstance(inner, str):
        return inner
    try:
        return str(raw)
    except Exception:  # noqa: BLE001
        return ""


__all__ = [
    "LIVE_STATUS_LINE_SCHEMA_VERSION",
    "LiveStatusLineRender",
    "MASTER_FLAG_ENV_VAR",
    "SpinnerInvalidator",
    "compose",
    "invalidate_app",
    "is_auto_refresh_enabled",
    "is_master_flag_enabled",
    "make_bottom_toolbar_callable",
    "make_cached_bottom_toolbar",
    "register_flags",
    "register_shipped_invariants",
    "render_status_segment",
]


# ===========================================================================
# Event-driven invalidation — replaces refresh_interval-based redraws
# ===========================================================================
#
# Root cause of typing lag (deeper diagnosis): prompt_toolkit's
# ``refresh_interval`` schedules a redraw on the asyncio event loop
# every N seconds regardless of whether anything changed. Each redraw
# runs the diff/render pipeline. When the operator presses a key,
# their key event waits its turn on the event loop behind in-progress
# redraws — observable as keystroke batching.
#
# CC has no refresh_interval. It redraws only on input events + on
# explicit ``app.invalidate()`` calls. We adopt the same model:
#
#   * ``refresh_interval=None`` (no background ticks)
#   * :class:`SpinnerInvalidator` — fires ``app.invalidate()`` at
#     spinner cadence ONLY while spinner state is active. When idle,
#     no invalidations, no redraws, fully responsive typing.
#   * :func:`invalidate_app` — best-effort one-shot for op state
#     transitions (op_started / completed / failed) where the toolbar
#     content changes and the operator should see it immediately.


_AUTO_REFRESH_ENV_VAR: str = "JARVIS_REPL_AUTO_REFRESH_ENABLED"


def is_auto_refresh_enabled() -> bool:
    """``JARVIS_REPL_AUTO_REFRESH_ENABLED``. Default **false** (event-
    driven model). Operators can set ``=true`` to restore the legacy
    refresh_interval-driven redraws if some downstream mechanic
    breaks under event-driven mode. NEVER raises."""
    raw = os.environ.get(_AUTO_REFRESH_ENV_VAR, "")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def invalidate_app() -> bool:
    """Defensive ``prompt_toolkit.application.get_app().invalidate()``.

    Returns ``True`` on success, ``False`` when prompt_toolkit isn't
    importable / no app is running / invalidate raised. NEVER raises
    into the caller.

    Use at op state transitions (op_started / op_completed / op_failed
    / cost_tick / posture_change) so the operator sees the new state
    in the toolbar without waiting for the next refresh tick.
    """
    try:
        from prompt_toolkit.application import get_app
    except ImportError:
        return False
    try:
        app = get_app()
    except Exception:  # noqa: BLE001 — defensive
        return False
    try:
        app.invalidate()
        return True
    except Exception:  # noqa: BLE001
        return False


class SpinnerInvalidator:
    """Background asyncio task that fires ``app.invalidate()`` at a
    configurable cadence WHILE a caller-supplied predicate returns
    True (typically: while a Rich spinner is animating).

    Replaces ``PromptSession(refresh_interval=...)`` for the
    spinner-animation case so background ticks don't run during
    idle typing.

    Lifecycle
    ---------
      * :meth:`start` — schedule the background task on the running
        event loop. Idempotent: starting twice is a no-op.
      * :meth:`stop` — cancel the task. Idempotent.
      * :meth:`is_running` — observability hook.

    Defensive contract
    -------------------
      * The predicate raising → treated as False (no invalidate this tick)
      * ``app.invalidate()`` raising → swallowed (next tick retries)
      * ``stop()`` always succeeds — cancellation is best-effort
      * NEVER raises into the caller's hot path
    """

    def __init__(
        self,
        *,
        get_active: Callable[[], bool],
        cadence_s: float = 0.10,
    ) -> None:
        self._get_active = get_active
        self._cadence_s = max(0.01, min(5.0, float(cadence_s)))
        self._task: object = None  # asyncio.Task when running
        self._stop_flag: bool = False

    def is_running(self) -> bool:
        if self._task is None:
            return False
        try:
            return not self._task.done()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            return False

    def start(self) -> bool:
        """Schedule the loop. Returns ``True`` on success, ``False``
        when no event loop is available."""
        if self.is_running():
            return True
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            if not loop.is_running():
                return False
            self._stop_flag = False
            self._task = loop.create_task(self._loop())
            return True
        except Exception:  # noqa: BLE001
            logger.debug(
                "[SpinnerInvalidator] start failed", exc_info=True,
            )
            return False

    def stop(self) -> None:
        """Cancel the loop. Idempotent."""
        self._stop_flag = True
        if self._task is None:
            return
        try:
            self._task.cancel()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
        self._task = None

    async def _loop(self) -> None:
        import asyncio
        while not self._stop_flag:
            try:
                if self._predicate_true():
                    invalidate_app()
            except Exception:  # noqa: BLE001
                pass
            try:
                await asyncio.sleep(self._cadence_s)
            except asyncio.CancelledError:
                break

    def _predicate_true(self) -> bool:
        try:
            return bool(self._get_active())
        except Exception:  # noqa: BLE001
            return False


# ===========================================================================
# Typing-responsiveness fix — state-hash cache on the toolbar callable
# ===========================================================================
#
# Root cause of perceived typing lag: prompt_toolkit calls the
# bottom_toolbar callable on every keystroke AND on every
# ``refresh_interval`` tick. With Gap #1+5's wrapper the call now
# does multiple state pulls + Rich formatting per invocation. A
# single keystroke contends with the in-progress toolbar redraw —
# operators see the freeze.
#
# Fix: wrap the callable with a state-hash cache. The state hash is
# a cheap tuple of primitives sampled per call. When the hash matches
# the previous call (typical: rapid keystrokes during idle), return
# the cached ANSI/string result in microseconds. When the hash
# changes (op started, cost ticked, spinner frame advanced), recompute.
#
# Spinner animation still works because the spinner glyph is part of
# the hash — every spinner-frame change invalidates. When the spinner
# is INACTIVE (operator-typing scenario), the hash is stable across
# keystrokes and the cache dominates.


def make_cached_bottom_toolbar(
    inner: Callable[[], object],
    state_fetcher: Callable[[], object],
) -> Callable[[], object]:
    """Wrap ``inner`` with a state-hash-based cache.

    On each invocation:

      1. Call ``state_fetcher()`` — should return a small hashable
         object (typically a tuple of primitives).
      2. Compute its hash. If unchanged from the previous call AND
         a cached result exists, return the cached result.
      3. Otherwise call ``inner()``, cache, and return.

    Defensive: ``state_fetcher`` raising → uncached pass-through to
    ``inner`` (NEVER raises into prompt_toolkit's render path). Same
    for ``inner`` raising — caller's existing exception handling
    applies. The cache is single-threaded by construction (toolbar
    runs on the asyncio event loop, no concurrent invocation).

    NEVER raises.
    """
    cache: dict = {"hash": None, "result": None, "primed": False}

    def _cached() -> object:
        # Fetch state first; on failure, degrade to uncached call
        try:
            state = state_fetcher()
            new_hash = hash(state)
        except Exception:  # noqa: BLE001
            try:
                return inner()
            except Exception:  # noqa: BLE001
                return ""
        # Cache hit — return previous result without re-computing
        if cache["primed"] and new_hash == cache["hash"]:
            return cache["result"]
        # Cache miss — compute, store, return
        try:
            result = inner()
        except Exception:  # noqa: BLE001
            return ""
        cache["hash"] = new_hash
        cache["result"] = result
        cache["primed"] = True
        return result

    return _cached


# ===========================================================================
# Slice 5 — FlagRegistry self-registration (auto-discovered via
# battle_test entry in ``_FLAG_PROVIDER_PACKAGES``)
# ===========================================================================


def register_flags(registry) -> int:
    """Module-owned FlagRegistry registration for the Gap #1+3+5 arc.
    Returns count of FlagSpecs added. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except ImportError:
        return 0

    specs = [
        FlagSpec(
            name=MASTER_FLAG_ENV_VAR,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Master kill switch for the live status-line callable "
                "(Gap #1+5). When false, the SerpentREPL bottom_toolbar "
                "shows only the swarm digest (legacy behavior). "
                "Default TRUE post graduation 2026-05-04."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/battle_test/live_status_line.py"
            ),
            example="true",
            since="Gap #1+3+5 Slice 5 (2026-05-04)",
        ),
        FlagSpec(
            name="JARVIS_OP_COLLAPSE_ENABLED",
            type=FlagType.BOOL,
            default=True,
            description=(
                "Master kill switch for per-op buffered rendering and "
                "``/expand <op-id>`` recovery (Gap #3). When false, "
                "every op's lines emit straight to the console with no "
                "buffer record (operator can't /expand later). "
                "Default TRUE post graduation 2026-05-04."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/battle_test/serpent_flow.py"
            ),
            example="true",
            since="Gap #1+3+5 Slice 5 (2026-05-04)",
        ),
        FlagSpec(
            name="JARVIS_OP_BLOCK_BUFFER_SIZE",
            type=FlagType.INT,
            default=50,
            description=(
                "Capacity of the OpBlockBuffer (Gap #3 Slice 2) — the "
                "session-scoped ring of per-op buffered render lines. "
                "Drop-oldest eviction; clamped to [1, 5000]. Backs the "
                "``/expand <op-id>`` recovery path."
            ),
            category=Category.TUNING,
            source_file=(
                "backend/core/ouroboros/battle_test/op_block_buffer.py"
            ),
            example="50",
            since="Gap #1+3+5 Slice 5 (2026-05-04)",
        ),
    ]

    count = 0
    for spec in specs:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            logger.debug(
                "[LiveStatusLine] flag registration failed for %s",
                getattr(spec, "name", "?"), exc_info=True,
            )
    return count


# ===========================================================================
# Slice 5 — shipped_code_invariants self-registration
# ===========================================================================


def register_shipped_invariants() -> list:
    """Module-owned shipped-code AST pins for the Gap #1+3+5 arc.

    Four structural invariants:

      1. ``status_line_callable_wired_into_prompt_async`` — the
         ``make_bottom_toolbar_callable`` wrapper MUST be invoked at
         the ``PromptSession(bottom_toolbar=...)`` call site. THIS IS
         THE BUG-FIX REGRESSION PIN — without it, status line silently
         regresses.
      2. ``op_block_state_taxonomy_frozen`` — :class:`OpBlockState`
         3-value taxonomy frozen against silent expansion.
      3. ``serpent_flow_op_lifecycle_buffer_hooks`` — ``op_started``,
         ``_op_line``, ``op_completed``, ``op_failed`` MUST call their
         respective ``_maybe_buffer_*`` helpers. Without these hooks
         the buffer is never populated.
      4. ``handle_expand_dispatches_three_prefixes`` — ``_handle_expand``
         must route ``t-`` / ``d-`` / ``o-`` prefixes; losing any
         silently breaks expansion for that artifact kind.

    NEVER raises (returns ``[]`` on import failure)."""
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    import ast as _ast

    def _validate_status_line_wired(_tree, source) -> tuple:
        del _tree
        if "make_bottom_toolbar_callable" not in source:
            return (
                "serpent_flow.py missing make_bottom_toolbar_callable "
                "invocation — Gap #1 status-line wiring regressed",
            )
        if "bottom_toolbar=_live_bottom_toolbar" not in source:
            return (
                "serpent_flow.py PromptSession does not pass the "
                "wrapped bottom_toolbar — Gap #1 status-line not surfaced",
            )
        return ()

    def _validate_op_block_state_frozen(tree, _source) -> tuple:
        del _source
        seen: set = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef) and node.name == "OpBlockState":
                for stmt in node.body:
                    if isinstance(stmt, _ast.Assign):
                        for target in stmt.targets:
                            if isinstance(target, _ast.Name):
                                seen.add(target.id)
        required = {"BUFFERING", "COMMITTED", "EXPANDED"}
        missing = required - seen
        if missing:
            return (
                f"OpBlockState lost values: {sorted(missing)} — "
                "the closed taxonomy is frozen by Gap #3 Slice 5",
            )
        return ()

    def _validate_lifecycle_buffer_hooks(tree, _source) -> tuple:
        del _source
        violations = []
        required_hooks = {
            "op_started": "_maybe_buffer_op_start",
            "_op_line": "_maybe_buffer_op_line",
            "op_completed": "_maybe_buffer_op_commit",
            "op_failed": "_maybe_buffer_op_commit",
        }
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                if node.name in required_hooks:
                    expected = required_hooks[node.name]
                    body_src = _ast.unparse(node)
                    if expected not in body_src:
                        violations.append(
                            f"{node.name}() missing {expected!r} call — "
                            "Gap #3 Slice 3 lifecycle hook regressed"
                        )
        return tuple(violations)

    def _validate_handle_expand_dispatches(tree, _source) -> tuple:
        del _source
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                if node.name == "_handle_expand":
                    body = _ast.unparse(node)
                    violations = []
                    for prefix in ("t-", "d-", "o-"):
                        if (
                            f"startswith('{prefix}')" not in body
                            and f'startswith("{prefix}")' not in body
                        ):
                            violations.append(
                                f"_handle_expand missing dispatch for "
                                f"{prefix!r} prefix"
                            )
                    return tuple(violations)
        return ("_handle_expand method not found — REPL verb missing",)

    return [
        ShippedCodeInvariant(
            invariant_name="status_line_callable_wired_into_prompt_async",
            target_file=(
                "backend/core/ouroboros/battle_test/serpent_flow.py"
            ),
            description=(
                "BUG-FIX REGRESSION PIN: PromptSession must use the "
                "wrapped bottom_toolbar from "
                "make_bottom_toolbar_callable. Otherwise the live "
                "status-line silently regresses to swarm-only display."
            ),
            validate=_validate_status_line_wired,
        ),
        ShippedCodeInvariant(
            invariant_name="op_block_state_taxonomy_frozen",
            target_file=(
                "backend/core/ouroboros/battle_test/op_block_buffer.py"
            ),
            description=(
                "OpBlockState's 3-value closed taxonomy "
                "(BUFFERING/COMMITTED/EXPANDED) must remain intact."
            ),
            validate=_validate_op_block_state_frozen,
        ),
        ShippedCodeInvariant(
            invariant_name="serpent_flow_op_lifecycle_buffer_hooks",
            target_file=(
                "backend/core/ouroboros/battle_test/serpent_flow.py"
            ),
            description=(
                "op_started/_op_line/op_completed/op_failed must each "
                "call their _maybe_buffer_* helper. Buffer is never "
                "populated without these hooks."
            ),
            validate=_validate_lifecycle_buffer_hooks,
        ),
        ShippedCodeInvariant(
            invariant_name="handle_expand_dispatches_three_prefixes",
            target_file=(
                "backend/core/ouroboros/battle_test/serpent_flow.py"
            ),
            description=(
                "_handle_expand must route t-N/d-N/o-N prefixes to the "
                "right substrate; losing any branch silently breaks "
                "expansion for that artifact kind."
            ),
            validate=_validate_handle_expand_dispatches,
        ),
    ]
