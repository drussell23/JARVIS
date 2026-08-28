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
import json
import os
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, List, Optional, Sequence, Tuple

_ENV_MASTER = "JARVIS_EPISODIC_CORE_ENABLED"
_ENV_WINDOW = "JARVIS_EPISODIC_WINDOW"
_ENV_LONGTERM_MAX = "JARVIS_EPISODIC_LONGTERM_MAX"
_ENV_RELEVANCE_K = "JARVIS_EPISODIC_RELEVANCE_K"
_ENV_PERSIST = "JARVIS_EPISODIC_PERSIST_ENABLED"
_DEFAULT_WINDOW = 8
_DEFAULT_LONGTERM_MAX = 512
_DEFAULT_RELEVANCE_K = 3


_ENV_SUMMARY_MAX = "JARVIS_EPISODIC_SUMMARY_MAX_CHARS"
_DEFAULT_SUMMARY_MAX = 240


def _summary_max_chars() -> int:
    """Per-episode summary ceiling. ``0`` disables the clamp entirely."""
    try:
        return max(0, int(os.getenv(_ENV_SUMMARY_MAX, "").strip()
                          or _DEFAULT_SUMMARY_MAX))
    except (TypeError, ValueError):
        return _DEFAULT_SUMMARY_MAX


def _clamp_summary(summary: object) -> str:
    """Bound a summary at RECORD time, where the cost is paid once.

    Every other dimension of episodic injection is already bounded: the window
    is count-bounded (``JARVIS_EPISODIC_WINDOW``, default 8), long-term recall
    is count-bounded (``JARVIS_EPISODIC_RELEVANCE_K``), and ``Episode.render``
    emits exactly ONE line per episode. Summary length was the only unbounded
    input, so a single caller passing a stack trace or a file body could put
    an arbitrarily large block into the prompt tail — on a 32K-context local
    model, memory bloat causing truncation would be a self-inflicted version
    of the very failure the truncation work exists to prevent.

    Clamped here rather than at render because an oversized summary is also
    written to the durable hash-chained receipt; trimming only at display
    would leave the ledger carrying what the prompt refuses to show.

    Nothing in-tree writes a long summary today (they read like
    "economic CASCADE_CHEAP → cheap-default"), so this is a guard against a
    future caller, not a fix for a present bug. NEVER raises.
    """
    text = str(summary or "")
    limit = _summary_max_chars()
    if limit <= 0 or len(text) <= limit:
        return text
    # Marked, not silently cut: a reader of the ledger must be able to tell
    # a short summary from a truncated one.
    return text[:limit].rstrip() + f" …[+{len(text) - limit} chars]"


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


def _persist_enabled() -> bool:
    """Durable cross-session long-term store. Default TRUE, but only ever
    engages under the master gate (``episodic_core_enabled``), so no file is
    touched while episodic memory is off. NEVER raises."""
    return os.getenv(_ENV_PERSIST, "true").strip().lower() in ("1", "true", "yes", "on")


@dataclasses.dataclass
class Episode:
    seq: int
    ts: float
    kind: str            # transition | route | error | complete | ...
    op_id: str
    summary: str
    context: dict = dataclasses.field(default_factory=dict)
    coalesce_key: str = ""  # Slice 135 — non-empty groups mid-cycle spam (e.g. routing)

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
        store_path: Any = None,
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
        # Cross-session long-term store (Task #9). None → default
        # `.jarvis/episodic_longterm.jsonl`. Text + embedding are preserved
        # here (the tamper-evident blue ledger keeps only payload_sha256, so
        # it can't rehydrate recall). Rehydrated once, now, so recall spans
        # sessions — the synthetic soul (Manifesto §4).
        self._store_path = store_path
        self._rehydrate_longterm()

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
        context: Optional[dict] = None, coalesce_key: str = "",
    ) -> Optional[Episode]:
        """Append an episode: durable hash-chained receipt + short-term window;
        the episode evicted from the window is embedded into long-term recall.

        **Slice 135 spam-guard:** when ``coalesce_key`` is non-empty and an
        episode with the same key is already in the window, that episode is
        UPDATED IN PLACE (latest summary + merged context + ``coalesced_count``)
        instead of appending a new one — so a flurry of mid-cycle micro-routing
        decisions collapses to one high-signal episode and cannot flush the
        terminal episodes out of the window (no append → no eviction → no extra
        durable I/O). Fail-soft — never raises."""
        try:
            summary = _clamp_summary(summary)
            with self._lock:
                if coalesce_key:
                    for existing in self._window:
                        if existing.coalesce_key == coalesce_key:
                            existing.summary = summary
                            existing.ts = time.time()
                            existing.context.update(dict(context or {}))
                            existing.context["coalesced_count"] = (
                                existing.context.get("coalesced_count", 1) + 1
                            )
                            return existing  # coalesced — no append, no eviction
                ep = Episode(self._seq, time.time(), str(kind), str(op_id),
                             summary, dict(context or {}),
                             str(coalesce_key or ""))
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
        # Cross-session durability: append this episode (text + embedding) to
        # the durable store so a future process can recall it. Fail-soft —
        # persistence never blocks the learning path.
        self._persist_one(vec, ep)

    # ── cross-session durability (Task #9) ──────────────────────────────────
    def _resolved_store_path(self):
        from pathlib import Path
        if self._store_path is not None:
            return Path(self._store_path)
        return Path(".jarvis") / "episodic_longterm.jsonl"

    def _persist_one(self, vec: List[float], ep: Episode) -> None:
        """Append one (embedding, episode) row to the durable long-term store.
        Best-effort: only under the master + persist gates, never raises."""
        if not (episodic_core_enabled() and _persist_enabled()):
            return
        try:
            row = {
                "seq": ep.seq, "ts": ep.ts, "kind": ep.kind,
                "op_id": ep.op_id, "summary": ep.summary,
                "context": ep.context, "coalesce_key": ep.coalesce_key,
                "vec": [float(x) for x in vec],
            }
            path = self._resolved_store_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        except Exception:  # noqa: BLE001 — durability never blocks learning
            pass

    def _rehydrate_longterm(self) -> None:
        """Load the durable store into ``_longterm`` at construction so recall
        spans sessions, then COMPACT the file to the retained tail (bounds
        disk to ``longterm_max``). Uses each row's stored embedding — no boot
        re-embedding — and falls back to re-embedding only if a vec is
        missing/corrupt. Gated + fail-soft: a missing/garbage store yields an
        empty long-term memory, never an error."""
        if not (episodic_core_enabled() and _persist_enabled()):
            return
        try:
            path = self._resolved_store_path()
            if not path.exists():
                return
            cap = self._longterm.maxlen or _longterm_max()
            rows: List[dict] = []
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:  # noqa: BLE001 — skip a torn line
                        continue
            # Keep only the most-recent `cap` rows (the deque's own bound).
            tail = rows[-cap:] if len(rows) > cap else rows
            restored: List[Tuple[List[float], Episode]] = []
            max_seq = -1
            for r in tail:
                try:
                    ep = Episode(
                        int(r.get("seq", 0)), float(r.get("ts", 0.0)),
                        str(r.get("kind", "")), str(r.get("op_id", "")),
                        str(r.get("summary", "")), dict(r.get("context", {}) or {}),
                        str(r.get("coalesce_key", "")),
                    )
                    vec = r.get("vec")
                    if not (isinstance(vec, list) and vec):
                        vec = self._embed(ep.summary)
                    if vec is None:
                        continue
                    restored.append(([float(x) for x in vec], ep))
                    max_seq = max(max_seq, ep.seq)
                except Exception:  # noqa: BLE001 — skip a bad row
                    continue
            with self._lock:
                self._longterm.extend(restored)
                # Continue the sequence past the rehydrated high-water mark so
                # new episodes never collide with restored seqs.
                if max_seq >= self._seq:
                    self._seq = max_seq + 1
            # Compact the file to the retained tail so it can't grow unbounded
            # across sessions (rewrite is best-effort; a failure just leaves
            # the longer file, which the next boot re-trims).
            if len(rows) > cap:
                self._compact_store(path, restored)
        except Exception:  # noqa: BLE001 — rehydration never blocks boot
            pass

    def _compact_store(self, path, restored: List[Tuple[List[float], Episode]]) -> None:
        """Atomically rewrite the store to just the retained rows. Fail-soft."""
        try:
            tmp = path.with_suffix(path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                for vec, ep in restored:
                    row = {
                        "seq": ep.seq, "ts": ep.ts, "kind": ep.kind,
                        "op_id": ep.op_id, "summary": ep.summary,
                        "context": ep.context, "coalesce_key": ep.coalesce_key,
                        "vec": [float(x) for x in vec],
                    }
                    fh.write(json.dumps(row, separators=(",", ":")) + "\n")
            os.replace(str(tmp), str(path))
        except Exception:  # noqa: BLE001
            pass

    def prune(self, match: Callable[["Episode"], bool], *, tombstone_label: str = "") -> int:
        """Integrity-preserving retirement of superseded episodes.

        Evicts episodes for which ``match(ep)`` is True from the in-RAM long-term recall
        cache AND the short-term window — freeing RAM immediately and stopping a
        consolidated failure pattern from resurfacing in ``recall``. It then APPENDS a
        tombstone receipt to the tamper-evident ledger recording the supersession.

        It NEVER deletes from the append-only durable ledger: erasing a hash-chained
        receipt would break tamper-evidence (Manifesto §8 audit-immutability + the
        dissertation-evidence purity this module guarantees). The audit-correct way to
        retire a record is to append a supersession marker, not erase the original.

        Returns the number of in-memory episodes evicted. Fail-soft → 0. A predicate that
        raises is treated as "no match" (keep) so a bad matcher can never nuke the cache.
        """
        def _hit(ep: "Episode") -> bool:
            try:
                return bool(match(ep))
            except Exception:  # noqa: BLE001
                return False
        try:
            with self._lock:
                kept_lt = [(v, ep) for (v, ep) in self._longterm if not _hit(ep)]
                kept_w = [ep for ep in self._window if not _hit(ep)]
                removed = (len(self._longterm) - len(kept_lt)) + (len(self._window) - len(kept_w))
                if removed:
                    self._longterm = deque(kept_lt, maxlen=self._longterm.maxlen)
                    self._window = deque(kept_w, maxlen=self._window.maxlen)
            if removed:
                try:
                    bl = self._blue_ledger()
                    if bl is not None:
                        bl.record(
                            attack_class="episodic_superseded",
                            payload=f"superseded {removed} episodes: {tombstone_label}"[:300],
                            verdict="superseded", blocked=False,
                        )
                except Exception:  # noqa: BLE001
                    pass
            return removed
        except Exception:  # noqa: BLE001
            return 0

    def recent(self, n: int) -> List[Episode]:
        """The immediate short-term window (most recent last). NEVER raises."""
        try:
            with self._lock:
                items = list(self._window)
            return items[-max(0, int(n)):] if n else items
        except Exception:  # noqa: BLE001
            return []

    def recall_sync(self, query: str, k: int = 3) -> List[Episode]:
        """Sync cosine recall over long-term (aged-out) episodes, ranked by
        similarity to ``query``. Fail-soft → []. This is the substrate the
        generation path consumes (the prompt builder is sync); ``recall`` is
        the async façade over the same core — no duplicated ranking logic."""
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

    async def recall(self, query: str, k: int = 3) -> List[Episode]:
        """Cosine recall over long-term (aged-out) episodes. Fail-soft → []."""
        return self.recall_sync(query, k)

    def render_relevant(self, query: str, n: int) -> str:
        """Render the top-``n`` long-term episodes SEMANTICALLY RELEVANT to
        ``query`` (the current op's intent) as a prompt block. This is the
        active-recall counterpart to ``render_recent``: instead of "what I
        just did" it surfaces "how I handled similar situations before" —
        the long-term learning tier the organism accumulates but, until this
        wire, never consulted. VOLATILE (caller appends to the user-prompt
        tail, never the cached prefix). "" when nothing relevant. NEVER
        raises."""
        eps = self.recall_sync(query, n)
        if not eps:
            return ""
        body = "\n".join(ep.render() for ep in eps)
        return (
            "## Relevant Past Experience (semantic recall — how you handled "
            "similar situations before)\n\n" + body
        )

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


def prune_episodes(match: Callable[["Episode"], bool], *, tombstone_label: str = "") -> int:
    """Module helper: retire superseded episodes on the default ledger (integrity-preserving;
    see :meth:`EpisodicLedger.prune`). No-op (0) when episodic core is disabled."""
    if not episodic_core_enabled():
        return 0
    return get_episodic_ledger().prune(match, tombstone_label=tombstone_label)


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


async def record_route(
    *, op_id: str, router: str, summary: str = "", context: Optional[dict] = None,
) -> Optional[Episode]:
    """Record a routing decision (kind=route), COALESCED per op so mid-cycle
    micro-decisions collapse to one high-signal episode. Gated + fail-soft."""
    if not episodic_core_enabled():
        return None
    ctx = dict(context or {})
    ctx["router"] = str(router)
    return await get_episodic_ledger().record(
        kind="route", op_id=str(op_id),
        summary=summary or f"{router} routing decision",
        context=ctx, coalesce_key=f"route:{op_id}",
    )


_pending_tasks: set = set()


def _fire_nowait(coro) -> None:
    """Schedule ``coro`` fire-and-forget on the running loop (or run inline in a
    sync context). Non-blocking, fail-soft; holds a strong task ref (no GC)."""
    import asyncio
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            task = loop.create_task(coro)
            _pending_tasks.add(task)
            task.add_done_callback(_pending_tasks.discard)
        else:
            asyncio.run(coro)  # sync context (tests/CLI) — bounded
    except Exception:  # noqa: BLE001 — a synapse must never perturb the FSM
        try:
            coro.close()
        except Exception:  # noqa: BLE001
            pass


def note_transition_nowait(
    *, op_id: str, phase_from: str, phase_to: str,
    summary: str = "", context: Optional[dict] = None,
) -> None:
    """FIRE-AND-FORGET, NON-BLOCKING FSM synapse for the hot orchestrator path —
    schedules ``record_transition`` and returns immediately. Gated + fail-soft."""
    if not episodic_core_enabled():
        return
    _fire_nowait(record_transition(
        op_id=str(op_id) if op_id is not None else "",
        phase_from=str(phase_from) if phase_from is not None else "",
        phase_to=str(phase_to) if phase_to is not None else "",
        summary=summary, context=context,
    ))


def note_route_nowait(
    *, op_id: str, router: str, summary: str = "", context: Optional[dict] = None,
) -> None:
    """FIRE-AND-FORGET, NON-BLOCKING mid-cycle ROUTING synapse — schedules
    ``record_route`` (coalesced per op) and returns immediately. Gated +
    fail-soft. Safe to call from the hot routing path."""
    if not episodic_core_enabled():
        return
    _fire_nowait(record_route(
        op_id=str(op_id) if op_id is not None else "",
        router=str(router), summary=summary, context=context,
    ))


def render_episodic_context(n: int = 0) -> str:
    """Gated render of the recent window for passive prompt injection. "" when
    disabled/empty. NEVER raises."""
    if not episodic_core_enabled():
        return ""
    try:
        return get_episodic_ledger().render_recent(n or _window_size())
    except Exception:  # noqa: BLE001
        return ""


def _relevance_k() -> int:
    try:
        return max(1, int(os.getenv(_ENV_RELEVANCE_K, _DEFAULT_RELEVANCE_K)))
    except (TypeError, ValueError):
        return _DEFAULT_RELEVANCE_K


def render_relevant_context(query: str, k: int = 0) -> str:
    """Gated ACTIVE recall: surface the long-term episodes most relevant to
    ``query`` (the current op's intent) for injection into the generation
    prompt. This is the wire that makes the accumulated long-term episodic
    memory actionable — without it, ``recall`` has no consumer and the
    learning tier is write-only. "" when disabled / no query / nothing
    relevant. NEVER raises."""
    if not episodic_core_enabled():
        return ""
    q = (query or "").strip()
    if not q:
        return ""
    try:
        return get_episodic_ledger().render_relevant(q, k or _relevance_k())
    except Exception:  # noqa: BLE001
        return ""


__all__ = [
    "episodic_core_enabled",
    "Episode",
    "EpisodicLedger",
    "get_episodic_ledger",
    "reset_episodic_ledger",
    "record_transition",
    "record_route",
    "note_transition_nowait",
    "note_route_nowait",
    "render_episodic_context",
    "render_relevant_context",
]
