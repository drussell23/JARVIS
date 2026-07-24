"""Moltbook Garnish — the wit tier, behind a concurrency-1 choke.

Slice 3 mandates (operator, 2026-07-24):

* **The API Concurrency Choke.** ALL LLM garnish traffic flows through
  ONE bounded ``asyncio.Queue`` drained by ONE worker — concurrency is
  structurally clamped to 1 (there is exactly one consumer coroutine;
  no semaphore to misconfigure). A full queue rejects the submit
  INSTANTLY and the caller falls back to its deterministic persona
  template — the chatroom can never hog network connections, stall the
  event loop, or compete with the active Swarm for DoubleWord capacity.
* **Sliding-window context.** The worker queries the SQLite store for
  the last ≤4 posts of the ACTIVE THREAD only (``thread_window`` — the
  mandate-2b bound from Slice 1): token cost is O(window · body_cap),
  never O(history).
* **DRY provider path.** The LLM call rides the SAME infrastructure the
  NarrativeChannel's intent prompter uses — ``rt_gate.gate_completion``
  with a lazily-constructed ``DoublewordProvider`` fallback handle
  (DW-primary economics), wrapped in ``asyncio.wait_for``. Injectable
  ``llm_fn`` for tests — no network in CI.
* **Budgeted.** Per-hour garnish budget (``JARVIS_MOLTBOOK_GARNISH_
  PER_HOUR``, default 30) on top of the queue bound; exhausted budget →
  instant template fallback. Output re-enters ``post_molt`` and gets
  the full Tier -1 treatment (sanitize + ref fence) — a garnished body
  is exactly as inert as a template one.

Masters: ``JARVIS_MOLTBOOK_GARNISH_ENABLED`` (default on — degrades to
templates instantly whenever DW is unavailable). NEVER raises anywhere.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger("Ouroboros.MoltbookGarnish")

_TRUTHY = ("1", "true", "yes", "on")


def garnish_enabled() -> bool:
    return os.environ.get(
        "JARVIS_MOLTBOOK_GARNISH_ENABLED", "1",
    ).strip().lower() in _TRUTHY


def _queue_max() -> int:
    try:
        return max(1, min(16, int(os.environ.get(
            "JARVIS_MOLTBOOK_GARNISH_QUEUE_MAX", "4"))))
    except (TypeError, ValueError):
        return 4


def _garnish_timeout_s() -> float:
    try:
        return max(2.0, min(60.0, float(os.environ.get(
            "JARVIS_MOLTBOOK_GARNISH_TIMEOUT_S", "12"))))
    except (TypeError, ValueError):
        return 12.0


def _garnish_max_tokens() -> int:
    try:
        return max(24, min(300, int(os.environ.get(
            "JARVIS_MOLTBOOK_GARNISH_MAX_TOKENS", "90"))))
    except (TypeError, ValueError):
        return 90


def _garnish_per_hour() -> int:
    try:
        return max(1, min(600, int(os.environ.get(
            "JARVIS_MOLTBOOK_GARNISH_PER_HOUR", "30"))))
    except (TypeError, ValueError):
        return 30


def _context_window() -> int:
    try:
        return max(1, min(5, int(os.environ.get(
            "JARVIS_MOLTBOOK_GARNISH_CONTEXT_WINDOW", "4"))))
    except (TypeError, ValueError):
        return 4


@dataclass(frozen=True)
class GarnishRequest:
    """One wit request — pure data; the worker owns all I/O."""

    author_id: str
    kind: str
    facts: Dict[str, Any] = field(default_factory=dict)
    reply_to: str = ""
    thread_root: str = ""
    op_id: str = ""


class GarnishQueue:
    """The choke: one queue, ONE worker coroutine — concurrency is a
    structural fact, not a tunable. ``submit`` never blocks and never
    raises; a full queue / spent budget returns False so callers fall
    back to deterministic templates instantly."""

    def __init__(
        self,
        llm_fn: Optional[Callable[[str], Awaitable[str]]] = None,
    ) -> None:
        self._llm_fn = llm_fn
        self._q: Optional[asyncio.Queue] = None
        self._worker: Optional[asyncio.Task] = None
        self._hour_marks: List[float] = []
        self._hour_lock = threading.Lock()
        # Telemetry (read by tests + /moltbook stats): the structural
        # concurrency proof — max observed in-flight must stay 1.
        self.stats: Dict[str, int] = {
            "submitted": 0, "rejected_full": 0, "rejected_budget": 0,
            "garnished": 0, "fell_back": 0, "max_inflight": 0,
        }
        self._inflight = 0

    # -- admission ----------------------------------------------------

    def _budget_ok(self, now: float) -> bool:
        try:
            with self._hour_lock:
                cutoff = now - 3600.0
                self._hour_marks[:] = [
                    t for t in self._hour_marks if t >= cutoff
                ]
                if len(self._hour_marks) >= _garnish_per_hour():
                    return False
                self._hour_marks.append(now)
                return True
        except Exception:  # noqa: BLE001
            return False

    def submit(self, req: GarnishRequest) -> bool:
        """Non-blocking admission. True = queued for wit; False = caller
        must fall back to its deterministic template NOW. NEVER raises."""
        try:
            if not garnish_enabled():
                return False
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return False
            if self._q is None:
                self._q = asyncio.Queue(maxsize=_queue_max())
            if self._worker is None or self._worker.done():
                self._worker = loop.create_task(self._worker_loop())
            if not self._budget_ok(time.time()):
                self.stats["rejected_budget"] += 1
                return False
            try:
                self._q.put_nowait(req)
            except asyncio.QueueFull:
                self.stats["rejected_full"] += 1
                return False
            self.stats["submitted"] += 1
            return True
        except Exception:  # noqa: BLE001
            return False

    # -- the ONE worker ----------------------------------------------

    async def _worker_loop(self) -> None:
        try:
            while True:
                req = await self._q.get()          # type: ignore[union-attr]
                self._inflight += 1
                self.stats["max_inflight"] = max(
                    self.stats["max_inflight"], self._inflight,
                )
                try:
                    await self._process(req)
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    self._inflight -= 1
                    try:
                        self._q.task_done()        # type: ignore[union-attr]
                    except Exception:  # noqa: BLE001
                        pass
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            return

    async def _process(self, req: GarnishRequest) -> None:
        """Garnish one post; ANY failure falls back to the deterministic
        template — a request is never lost, only less witty."""
        from backend.core.ouroboros.governance.moltbook import post_molt
        body: Optional[str] = None
        try:
            prompt = await self._build_prompt(req)
            prose = await asyncio.wait_for(
                self._call_llm(prompt), timeout=_garnish_timeout_s(),
            )
            prose = str(prose or "").strip().strip('"')
            if prose:
                body = prose
        except Exception:  # noqa: BLE001
            body = None
        if body is not None:
            self.stats["garnished"] += 1
            await post_molt(
                req.author_id, req.kind, body,
                reply_to=req.reply_to, op_id=req.op_id,
            )
        else:
            self.stats["fell_back"] += 1
            await post_molt(
                req.author_id, req.kind, None, facts=dict(req.facts),
                reply_to=req.reply_to, op_id=req.op_id,
            )

    async def _build_prompt(self, req: GarnishRequest) -> str:
        """Persona voice card + STRICT sliding-window thread context
        (mandate: last ≤4 posts of the active thread — never history)."""
        from backend.core.ouroboros.governance.moltbook import (
            get_default_store,
        )
        from backend.core.ouroboros.governance.moltbook_personas import (
            persona_for,
        )
        persona = persona_for(req.author_id)
        context_lines: List[str] = []
        if req.thread_root:
            window = await get_default_store().thread_window(
                req.thread_root, window=_context_window(),
            )
            for p in window:
                context_lines.append(f"{p.handle} ({p.kind}): {p.body}")
        facts = "; ".join(
            f"{k}={v}" for k, v in sorted(req.facts.items())
        )
        ctx = "\n".join(context_lines) if context_lines else "(no thread)"
        return (
            f"You are {persona.handle}, a resident of an engineering "
            f"organism's internal social feed. Persona: {persona.tagline}. "
            f"Write ONE short in-character post (max 2 sentences, no "
            f"hashtags, no emoji spam) reacting to this thread.\n"
            f"Thread (most recent last):\n{ctx}\n"
            f"Your post is a {req.kind}. Facts to weave in: {facts}\n"
            f"Reply with the post text only."
        )

    async def _call_llm(self, prompt: str) -> str:
        if self._llm_fn is not None:
            return await self._llm_fn(prompt)
        # DRY: the SAME DW-primary gate the intent prompter rides.
        from backend.core.ouroboros.governance.rt_gate import (
            gate_completion,
        )
        _dw = None
        try:
            from backend.core.ouroboros.governance.doubleword_provider import (
                DoublewordProvider,
            )
            _dw = DoublewordProvider()
        except Exception:  # noqa: BLE001
            _dw = None
        return await gate_completion(
            prompt,
            caller_id="moltbook_garnish",
            max_tokens=_garnish_max_tokens(),
            dw_provider=_dw,
        )


_QUEUE: Optional[GarnishQueue] = None
_QUEUE_LOCK = threading.Lock()


def get_garnish_queue() -> GarnishQueue:
    global _QUEUE
    with _QUEUE_LOCK:
        if _QUEUE is None:
            _QUEUE = GarnishQueue()
        return _QUEUE


def reset_garnish_for_tests() -> None:
    global _QUEUE
    with _QUEUE_LOCK:
        if _QUEUE is not None and _QUEUE._worker is not None:
            try:
                _QUEUE._worker.cancel()
            except Exception:  # noqa: BLE001
                pass
        _QUEUE = None


def garnish_or_template(
    author_id: str,
    kind: str,
    facts: Dict[str, Any],
    *,
    reply_to: str = "",
    thread_root: str = "",
    op_id: str = "",
) -> bool:
    """THE entry point for witty posts: try the choke queue; a rejected
    admission posts the deterministic template immediately (fire-and-
    forget). Returns True when queued for garnish. NEVER raises."""
    try:
        queued = get_garnish_queue().submit(GarnishRequest(
            author_id=author_id, kind=kind, facts=dict(facts),
            reply_to=reply_to, thread_root=thread_root, op_id=op_id,
        ))
        if not queued:
            from backend.core.ouroboros.governance.moltbook import (
                post_molt_nowait,
            )
            post_molt_nowait(
                author_id, kind, facts=dict(facts),
                reply_to=reply_to, op_id=op_id,
            )
        return queued
    except Exception:  # noqa: BLE001
        return False
