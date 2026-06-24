"""ephemeral_memory_sandbox — the per-worker isolated MessageHistory (Phase 1b).

Phase 1b of the Sovereign Multi-Agent Swarm eliminates **Context Accretion**:
the parent Fleet Commander's conversation must NEVER grow with the scratchpad
reasoning, failed tool attempts, and intermediate messages of every worker it
dispatches. Each worker instead runs against an ISOLATED, freshly-instantiated
message-history container that holds ONLY:

  (a) the sub-goal prompt, and
  (b) the worker's own local tool-execution results.

The worker's tool-loop is fed from THIS sandbox alone. The sandbox has **NO
read-access to the global/parent Ouroboros conversation** — there is no
back-reference to any shared/parent structure. One sandbox is constructed per
worker dispatch and is **vaporized** (deterministic teardown) the instant the
worker terminates (success / failure / cancellation), in the executor's
``finally`` block.

Bounded by construction: ``max_turns`` (env ``JARVIS_SWARM_SANDBOX_MAX_TURNS``)
and ``max_tokens`` (env ``JARVIS_SWARM_SANDBOX_MAX_TOKENS``). Oldest turns are
evicted (a bounded ``deque``) — the sandbox can never grow unbounded.

This module is pure / stdlib-only at import time. ``gc`` is stdlib; ``torch``
is **never imported** — the CUDA-cache best-effort path only fires when torch
is ALREADY present in ``sys.modules`` (workers call a provider/model-runtime;
they do not own tensors, so this is a no-op safety net, not a dependency).
"""
from __future__ import annotations

import collections
import gc
import logging
import os
import sys
import threading
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Env knobs (no hardcoding of bounds)
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def sandbox_enabled() -> bool:
    """Master gate. Default FALSE -> Phase 1a behavior (no sandbox isolation).

    When OFF, the executor never constructs a sandbox and the worker path is
    byte-identical to Phase 1a.
    """
    return _env_bool("JARVIS_SWARM_EPHEMERAL_SANDBOX_ENABLED", False)


def force_gc_enabled() -> bool:
    """Whether ``vaporize()`` calls a stop-the-world ``gc.collect()``.

    Default TRUE — the operator wants deterministic vaporization. It is
    env-throttleable because a stop-the-world GC per worker under an elastic
    burst can thrash; the ``del``/clear (deterministic RAM release) ALWAYS
    happens regardless of this flag.
    """
    return _env_bool("JARVIS_SWARM_FORCE_GC", True)


def _max_turns() -> int:
    return _env_int("JARVIS_SWARM_SANDBOX_MAX_TURNS", 64)


def _max_tokens() -> int:
    return _env_int("JARVIS_SWARM_SANDBOX_MAX_TOKENS", 32000)


# Coarse token approximation: ~4 chars/token. Deterministic, stdlib-only; the
# sandbox never calls a tokenizer (no heavy import).
_CHARS_PER_TOKEN = 4


def _approx_tokens(turn: Any) -> int:
    """Approximate token cost of a turn. Deterministic, dependency-free."""
    try:
        if isinstance(turn, dict):
            text = "".join(str(v) for v in turn.values())
        else:
            text = str(turn)
    except Exception:  # noqa: BLE001 — never let measurement break append
        return 1
    return max(1, len(text) // _CHARS_PER_TOKEN)


class EphemeralMemorySandbox:
    """An isolated, bounded, per-worker message-history container.

    The worker reads ONLY from :meth:`messages`. There is no reference to any
    parent/global conversation — isolation is structural, not policy. When the
    worker terminates, :meth:`vaporize` deterministically clears the internal
    array (``del`` + new empty container) and (gated) triggers GC.

    Bounded by both ``max_turns`` (deque maxlen) and ``max_tokens`` (oldest
    turns evicted until the running approx-token total fits). Both bounds are
    enforced on every :meth:`append`.
    """

    __slots__ = (
        "worker_id",
        "_max_turns",
        "_max_tokens",
        "_turns",
        "_approx_tokens",
        "_vaporized",
        "_lock",
    )

    def __init__(
        self,
        *,
        worker_id: str,
        sub_goal_prompt: str,
        max_turns: Optional[int] = None,
        max_tokens: Optional[int] = None,
    ) -> None:
        """Construct a fresh isolated sandbox seeded with the sub-goal prompt.

        Parameters
        ----------
        worker_id:
            Identifier of the worker that owns this sandbox (observability).
        sub_goal_prompt:
            The sub-goal prompt — the FIRST turn. This is (a) of the allowed
            contents. The worker reads it back via :meth:`messages`.
        max_turns / max_tokens:
            Optional explicit bounds; default to the env knobs. A non-positive
            value falls back to the env default (fail-safe, never unbounded).
        """
        self.worker_id = str(worker_id or "swarm-worker")
        mt = max_turns if (max_turns is not None and max_turns > 0) else _max_turns()
        mtok = (
            max_tokens
            if (max_tokens is not None and max_tokens > 0)
            else _max_tokens()
        )
        self._max_turns = int(mt)
        self._max_tokens = int(mtok)
        self._turns: Deque[Dict[str, Any]] = collections.deque(maxlen=self._max_turns)
        self._approx_tokens = 0
        self._vaporized = False
        # Workers may run their tool-loop concurrently with bookkeeping; the
        # sandbox is single-writer in practice, but guard append/vaporize so a
        # late tool-result append racing vaporize cannot corrupt the deque.
        self._lock = threading.Lock()

        # Seed turn (a): the sub-goal prompt. This is the worker's mission;
        # it is NOT pulled from any parent conversation.
        seed = {"role": "system", "kind": "sub_goal", "content": str(sub_goal_prompt or "")}
        self._turns.append(seed)
        self._approx_tokens += _approx_tokens(seed)

    # -- append / read ----------------------------------------------------

    def append(self, turn: Any) -> None:
        """Append a turn (a local tool-execution result or worker message).

        Enforces BOTH bounds: the deque ``maxlen`` caps turn count; the token
        budget evicts oldest turns until the running approximate total fits.
        Never raises; an append after vaporize is a no-op (fail-CLOSED — a
        vaporized sandbox accepts no further context).
        """
        with self._lock:
            if self._vaporized:
                logger.debug(
                    "[SwarmSandbox] append after vaporize ignored worker=%s",
                    self.worker_id,
                )
                return
            entry = turn if isinstance(turn, dict) else {"role": "tool", "content": turn}
            cost = _approx_tokens(entry)

            # maxlen eviction: capture what the deque will drop so the running
            # token total stays correct.
            if len(self._turns) >= self._max_turns:
                evicted = self._turns[0]
                self._approx_tokens -= _approx_tokens(evicted)

            self._turns.append(entry)
            self._approx_tokens += cost

            # Token-budget eviction: drop oldest until we fit. Never evict the
            # only remaining turn (a single oversized turn is clamped, kept).
            while self._approx_tokens > self._max_tokens and len(self._turns) > 1:
                dropped = self._turns.popleft()
                self._approx_tokens -= _approx_tokens(dropped)
            if self._approx_tokens < 0:
                self._approx_tokens = 0

    def messages(self) -> List[Dict[str, Any]]:
        """Return the worker's context — the ONLY surface the worker reads.

        Returns a shallow copy so a caller cannot mutate the internal deque.
        After vaporize this is empty (the worker's context is gone).
        """
        with self._lock:
            return list(self._turns)

    def stats(self) -> Dict[str, Any]:
        """Return bounded observability stats (no message contents)."""
        with self._lock:
            return {
                "worker_id": self.worker_id,
                "turns": len(self._turns),
                "approx_tokens": self._approx_tokens,
                "max_turns": self._max_turns,
                "max_tokens": self._max_tokens,
                "vaporized": self._vaporized,
            }

    @property
    def vaporized(self) -> bool:
        return self._vaporized

    # -- deterministic teardown ------------------------------------------

    def vaporize(self, *, force_gc: Optional[bool] = None) -> Dict[str, Any]:
        """Deterministically tear down the sandbox. Idempotent.

        Steps (in order):
          1. capture the cleared turn count (physical proof),
          2. explicitly drop the message array: ``clear()`` the deque, then
             rebind ``self._turns`` to a NEW empty deque and drop the old
             reference (so any lingering alias to the old object does not keep
             the worker's scratchpad alive),
          3. mark ``vaporized = True``,
          4. (gated by ``force_gc`` / ``JARVIS_SWARM_FORCE_GC``) run a
             stop-the-world ``gc.collect()`` — flushes Python RAM,
          5. best-effort ``torch.cuda.empty_cache()`` ONLY if torch is ALREADY
             imported (never import it — workers do not own VRAM; this is a
             no-op safety net),
          6. emit the proof log line.

        Returns the pre-vaporize stats (with ``turns_cleared``) for the caller
        to assert/log.
        """
        with self._lock:
            if self._vaporized:
                # Idempotent: already vaporized -> turns already 0.
                return {
                    "worker_id": self.worker_id,
                    "turns_cleared": 0,
                    "already_vaporized": True,
                    "vaporized": True,
                }

            turns_cleared = len(self._turns)

            # (2) Deterministic RAM release: ALWAYS happens, regardless of GC.
            old = self._turns
            try:
                old.clear()
            except Exception:  # noqa: BLE001 — clear must never break teardown
                pass
            self._turns = collections.deque(maxlen=self._max_turns)
            del old
            self._approx_tokens = 0
            self._vaporized = True

        # (4) Gated stop-the-world GC — outside the lock (collect can be slow).
        do_gc = force_gc_enabled() if force_gc is None else bool(force_gc)
        collected = 0
        if do_gc:
            try:
                collected = gc.collect()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[SwarmSandbox] gc.collect failed (non-fatal) worker=%s",
                    self.worker_id, exc_info=True,
                )

        # (5) Best-effort CUDA cache flush ONLY if torch already loaded.
        self._maybe_empty_cuda_cache()

        # (6) Physical proof of vaporization.
        logger.info(
            "[SwarmSandbox] vaporized worker=%s turns_cleared=%d gc=%s collected=%d",
            self.worker_id, turns_cleared, do_gc, collected,
        )
        return {
            "worker_id": self.worker_id,
            "turns_cleared": turns_cleared,
            "gc": do_gc,
            "collected": collected,
            "vaporized": True,
        }

    @staticmethod
    def _maybe_empty_cuda_cache() -> None:
        """Flush model VRAM cache IFF torch is ALREADY imported. Never imports.

        Honest scoping: ``gc.collect()`` flushes Python RAM; it does NOT flush
        model VRAM. The worker calls a provider/model-runtime — it does not own
        tensors. This is purely a no-op safety net for the case where torch
        happens to be resident in the process; it never adds a dependency.
        """
        torch_mod = sys.modules.get("torch")
        if torch_mod is None:
            return
        try:
            cuda = getattr(torch_mod, "cuda", None)
            if cuda is not None and getattr(cuda, "is_available", lambda: False)():
                cuda.empty_cache()
        except Exception:  # noqa: BLE001 — best-effort only
            logger.debug(
                "[SwarmSandbox] torch.cuda.empty_cache best-effort failed",
                exc_info=True,
            )


def vaporize_quietly(sandbox: Optional[Any], *, force_gc: Optional[bool] = None) -> None:
    """Vaporize a (possibly None / possibly-broken) sandbox, never raising.

    Fail-CLOSED helper for the executor ``finally`` block: an unreadable /
    None / non-sandbox object is treated as needs-vaporize and swallowed so the
    teardown path can never break the executor's guaranteed cleanup.
    """
    if sandbox is None:
        return
    try:
        sandbox.vaporize(force_gc=force_gc)
    except Exception:  # noqa: BLE001 — teardown must never raise
        logger.warning(
            "[SwarmSandbox] vaporize raised (swallowed; treated as vaporized)",
            exc_info=True,
        )
