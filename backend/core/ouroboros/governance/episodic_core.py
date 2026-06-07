"""Slice 133 — The Sovereign Episodic Memory Core.

A continuously-aware organism must passively recall its immediate past — what it
just routed, generated, or failed at — without firing a manual MEMORY_SEARCH
tool. This is the hippocampus: a continuous, append-only episodic ledger plus
passive injection of the recent window into the generation prompt.

**Pure composition — nothing reinvented:**
  * **Durable tamper-evidence** — each episode appends a hash-chained receipt via
    the existing ``BlueEvidenceLedger`` (red_blue_matrix), on a DEDICATED path
    (``.jarvis/episodic_memory.jsonl``) so the dissertation evidence chain stays
    pure. (The blue ledger stores ``payload_sha256`` — tamper-evidence — so the
    full episode content lives in the in-memory window + the semantic index.)
  * **Long-term recall** — as episodes fall out of the short-term window they are
    embedded via the ``SemanticIndex`` embedder (``_embedder_factory``) and kept
    for cosine recall. No new vectorizer.

**Two memory tiers:**
  * **Short-term window** — a bounded deque (``JARVIS_EPISODIC_WINDOW``, default 8)
    of full episodes; this is what gets passively injected into the prompt.
  * **Long-term** — evicted episodes, embedded, recalled by similarity.

**P2a-safe injection:** ``render_episodic_context`` returns a block that the
caller appends to the VOLATILE user-prompt tail only — episodes change every loop
and must NEVER enter the cached system prefix. Gated ``JARVIS_EPISODIC_CORE_ENABLED``
default-FALSE. All paths fail-soft (memory never blocks generation).
"""
from __future__ import annotations

import dataclasses
import os
import threading
import time
from collections import deque
from typing import Any, Deque, List, Optional, Sequence, Tuple

_ENV_MASTER = "JARVIS_EPISODIC_CORE_ENABLED"
_ENV_WINDOW = "JARVIS_EPISODIC_WINDOW"
_ENV_LONGTERM_MAX = "JARVIS_EPISODIC_LONGTERM_MAX"
_DEFAULT_WINDOW = 8
_DEFAULT_LONGTERM_MAX = 512


def episodic_core_enabled() -> bool:
    """Master gate, default-FALSE per §33.1. NEVER raises."""
    return os.getenv(_ENV_MASTER, "false").strip().lower() in ("1", "true", "yes", "on")


def _window_size() -> int:
    try:
        return max(1, int(os.getenv(_ENV_WINDOW, _DEFAULT_WINDOW)))
    except (TypeError, ValueError):
        return _DEFAULT_WINDOW


def _longterm_max() -> int:
    try:
        return max(1, int(os.getenv(_ENV_LONGTERM_MAX, _DEFAULT_LONGTERM_MAX)))
    except (TypeError, ValueError):
        return _DEFAULT_LONGTERM_MAX


@dataclasses.dataclass
class Episode:
    seq: int
    ts: float
    kind: str            # transition | route | error | complete | ...
    op_id: str
    summary: str
    context: dict = dataclasses.field(default_factory=dict)

    def render(self) -> str:
        return f"- [{self.kind}] op={self.op_id}: {self.summary}"


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    try:
        from backend.core.ouroboros.governance.semantic_index import _cosine as _si
        return float(_si(a, b))
    except Exception:  # noqa: BLE001
        try:
            num = sum(x * y for x, y in zip(a, b))
            na = sum(x * x for x in a) ** 0.5
            nb = sum(y * y for y in b) ** 0.5
            return num / (na * nb) if na and nb else -1.0
        except Exception:  # noqa: BLE001
            return -1.0


class EpisodicLedger:
    """Append-only episodic memory: bounded short-term window + tamper-evident
    durable receipts + long-term embedded recall. All async paths fail-soft."""

    def __init__(
        self,
        *,
        window: Optional[int] = None,
        blue_ledger: Any = None,
        embedder: Any = None,
        longterm_max: Optional[int] = None,
    ) -> None:
        self._window: Deque[Episode] = deque(maxlen=window or _window_size())
        self._longterm: Deque[Tuple[List[float], Episode]] = deque(
            maxlen=longterm_max or _longterm_max()
        )
        self._blue = blue_ledger          # None → lazy default (dedicated path)
        self._blue_resolved = blue_ledger is not None
        self._embedder = embedder         # None → lazy SemanticIndex factory
        self._seq = 0
        self._lock = threading.Lock()

    # ── substrate composition (lazy, fail-soft) ─────────────────────────────
    def _blue_ledger(self) -> Any:
        if not self._blue_resolved:
            self._blue_resolved = True
            try:
                from pathlib import Path
                from backend.core.ouroboros.governance.red_blue_matrix import (
                    BlueEvidenceLedger,
                )
                self._blue = BlueEvidenceLedger(
                    path=Path(".jarvis") / "episodic_memory.jsonl"
                )
            except Exception:  # noqa: BLE001
                self._blue = None
        return self._blue

    def _get_embedder(self) -> Any:
        if self._embedder is None:
            try:
                from backend.core.ouroboros.governance.semantic_index import (
                    _embedder_factory,
                )
                self._embedder = _embedder_factory()
            except Exception:  # noqa: BLE001
                self._embedder = None
        return self._embedder

    def _embed(self, text: str) -> Optional[List[float]]:
        try:
            emb = self._get_embedder()
            if emb is None:
                return None
            vecs = emb.embed([text or ""])
            if not vecs or not vecs[0]:
                return None
            return [float(x) for x in vecs[0]]
        except Exception:  # noqa: BLE001
            return None

    # ── record / recall ─────────────────────────────────────────────────────
    async def record(
        self, *, kind: str, op_id: str, summary: str,
        context: Optional[dict] = None,
    ) -> Optional[Episode]:
        """Append an episode: durable hash-chained receipt + short-term window;
        the episode evicted from the window is embedded into long-term recall.
        Fail-soft — returns the Episode (or None on hard failure), never raises."""
        try:
            with self._lock:
                ep = Episode(self._seq, time.time(), str(kind), str(op_id),
                             str(summary or ""), dict(context or {}))
                self._seq += 1
                evicted: Optional[Episode] = None
                if len(self._window) == self._window.maxlen:
                    evicted = self._window[0]  # about to be dropped by append
                self._window.append(ep)
            # Durable tamper-evident receipt (best-effort).
            try:
                bl = self._blue_ledger()
                if bl is not None:
                    bl.record(attack_class=str(kind), payload=str(summary or ""),
                              verdict="recorded", blocked=False)
            except Exception:  # noqa: BLE001
                pass
            # Write-through the evicted episode → long-term semantic recall.
            if evicted is not None:
                self._writethrough(evicted)
            return ep
        except Exception:  # noqa: BLE001
            return None

    def _writethrough(self, ep: Episode) -> None:
        """Embed an aged-out episode for long-term recall. Fail-soft."""
        vec = self._embed(ep.summary)
        if vec is None:
            return
        with self._lock:
            self._longterm.append((vec, ep))

    def recent(self, n: int) -> List[Episode]:
        """The immediate short-term window (most recent last). NEVER raises."""
        try:
            with self._lock:
                items = list(self._window)
            return items[-max(0, int(n)):] if n else items
        except Exception:  # noqa: BLE001
            return []

    async def recall(self, query: str, k: int = 3) -> List[Episode]:
        """Cosine recall over long-term (aged-out) episodes. Fail-soft → []."""
        try:
            qv = self._embed(query)
            if qv is None:
                return []
            with self._lock:
                snapshot = list(self._longterm)
            scored = sorted(
                ((_cosine(qv, v), ep) for v, ep in snapshot),
                key=lambda t: t[0], reverse=True,
            )
            return [ep for _, ep in scored[: max(1, int(k))]]
        except Exception:  # noqa: BLE001
            return []

    def render_recent(self, n: int) -> str:
        """Render the recent window as a prompt block (VOLATILE — caller appends
        to the user prompt tail, never the cached prefix). "" when empty."""
        eps = self.recent(n)
        if not eps:
            return ""
        body = "\n".join(ep.render() for ep in eps)
        return (
            "## Recent Episodes (your short-term memory — what you just did)\n\n"
            + body
        )


# ── singleton + module helpers ──────────────────────────────────────────────
_singleton: Optional[EpisodicLedger] = None
_singleton_lock = threading.Lock()


def get_episodic_ledger() -> EpisodicLedger:
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = EpisodicLedger()
    return _singleton


def reset_episodic_ledger() -> None:
    global _singleton
    with _singleton_lock:
        _singleton = None


async def record_transition(
    *, op_id: str, phase_from: str, phase_to: str,
    summary: str = "", context: Optional[dict] = None,
) -> Optional[Episode]:
    """Convenience: record an FSM state transition. Gated + fail-soft."""
    if not episodic_core_enabled():
        return None
    txt = summary or f"{phase_from} -> {phase_to}"
    ctx = dict(context or {})
    ctx.update({"phase_from": phase_from, "phase_to": phase_to})
    return await get_episodic_ledger().record(
        kind="transition", op_id=op_id, summary=txt, context=ctx,
    )


_pending_tasks: set = set()


def note_transition_nowait(
    *, op_id: str, phase_from: str, phase_to: str,
    summary: str = "", context: Optional[dict] = None,
) -> None:
    """FIRE-AND-FORGET, NON-BLOCKING FSM synapse for the hot orchestrator path.

    Schedules ``record_transition`` as a background task on the running event loop
    and returns IMMEDIATELY — it never awaits, so it cannot block or starve the
    main loop. Gated (no-op when disabled) and fully fail-soft (never raises). In
    a sync context with no running loop (tests/CLI) it best-effort runs the record
    inline. The strong task reference is held until completion to prevent GC."""
    if not episodic_core_enabled():
        return
    try:
        import asyncio
        coro = record_transition(
            op_id=str(op_id) if op_id is not None else "",
            phase_from=str(phase_from) if phase_from is not None else "",
            phase_to=str(phase_to) if phase_to is not None else "",
            summary=summary, context=context,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            task = loop.create_task(coro)
            _pending_tasks.add(task)
            task.add_done_callback(_pending_tasks.discard)
        else:
            asyncio.run(coro)  # sync context (tests) — bounded, append-only
    except Exception:  # noqa: BLE001 — a synapse must never perturb the FSM
        pass


def render_episodic_context(n: int = 0) -> str:
    """Gated render of the recent window for passive prompt injection. "" when
    disabled/empty. NEVER raises."""
    if not episodic_core_enabled():
        return ""
    try:
        return get_episodic_ledger().render_recent(n or _window_size())
    except Exception:  # noqa: BLE001
        return ""


__all__ = [
    "episodic_core_enabled",
    "Episode",
    "EpisodicLedger",
    "get_episodic_ledger",
    "reset_episodic_ledger",
    "record_transition",
    "note_transition_nowait",
    "render_episodic_context",
]
