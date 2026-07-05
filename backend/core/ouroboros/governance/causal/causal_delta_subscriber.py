"""Domain-1 Staging-1 Task 2 -- CausalDeltaSubscriber (Brain receiver).

The other end of the Task-1 transport hop. The Body's ``StructuralDeltaSensor``
publishes content-free ``causal.delta.<repo>`` events over the proven
``TrinityEventBus`` bridge; this subscriber is the Brain-side RECEIVER. It
subscribes ``causal.delta.*`` and RECORDS receipt of each delta in causal
order.

It only records receipt. The graph fold -- weaving the deltas into a cross-repo
causality DAG -- is Staging 2 (single-writer; no graph is built here).

Design commitments:

* **Reflective source (Mandate 2 -- in-payload identity).** The origin repo is
  read from the IN-PAYLOAD ``lineage.repo`` via ``RepoType(...)`` reflection,
  never from ``event.source`` and never parsed from ``event.topic``. Mandate 2
  rides the RepoType as reflective metadata INSIDE the data payload, not typed
  on the transport -- and this is the identity that survives the cross-host WS
  hop verbatim (the generic ``TrinityBusBridge`` re-mints ``event.source`` to
  the receiver bus's ``local_repo``, so it MUST NOT be trusted). A
  non-``RepoType`` ``lineage.repo``, or ``RepoType.BROADCAST`` (a TARGET
  semantic, never a valid causal origin), is logged and dropped -- a causal
  delta must name ONE concrete source.
* **No new dedup.** The bus's OWN 60s fingerprint dedup
  (``TrinityEvent.fingerprint``) drops exact duplicates BEFORE the handler
  fires. The subscriber adds NO dedup algorithm; its ``(repo, emit_seq,
  head_sha)`` seen-set is only belt-and-suspenders for a REPLAY outside the
  60s window.
* **Causal order.** Deltas are ordered by ``emit_seq`` WITHIN each repo (the
  Lamport guarantee from Staging 0). Cross-repo interleave is preserved via a
  global receive-order index; the per-repo subsequence is the guarantee.
* **Fail-soft, non-blocking.** The handler NEVER raises into the bus delivery
  loop (any error is logged and swallowed) and is a fast append -- no sleeps,
  no heavy work. Heavy work (the Staging-2 graph fold) is deferred there.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.core.trinity_event_bus import RepoType, TrinityEvent

logger = logging.getLogger(__name__)

CAUSAL_DELTA_PATTERN = "causal.delta.*"

# The lineage keys a well-formed causal-delta envelope MUST carry (shape of the
# Staging-0 ``stamp_delta`` output).
_REQUIRED_LINEAGE_KEYS = (
    "repo",
    "head_sha",
    "parent_sha",
    "merge_base",
    "emit_seq",
)


class CausalDeltaSubscriber:
    """Subscribe ``causal.delta.*`` and record deltas in causal order.

    RECORDS receipt only -- no graph, no fold (Staging 2). ``on_delta`` is an
    optional fast, fail-soft sink for the raw envelope; it MUST NOT block the
    bus loop.
    """

    def __init__(
        self,
        trinity_bus: Any,
        *,
        on_delta: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self._bus = trinity_bus
        self._on_delta_cb = on_delta
        self._sid: Optional[str] = None
        # Belt-and-suspenders idempotency for replays OUTSIDE the bus's 60s
        # fingerprint window. NOT a dedup algorithm -- the bus owns that.
        self._seen: set = set()
        # Per-repo log of (emit_seq, head_sha), ordered on read by emit_seq.
        self._per_repo: Dict[str, List[Tuple[int, str]]] = {}
        # Global receive order (one repo.value per accepted delta) -- preserves
        # cross-repo interleave for observed().
        self._receive_order: List[str] = []

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Subscribe ``causal.delta.*``. The bus fingerprint-dedups before the
        handler fires; the authoritative repo is the in-payload ``lineage.repo``
        (``event.source`` collapses over the bridge -- never read here)."""
        self._sid = await self._bus.subscribe(CAUSAL_DELTA_PATTERN, self._on_delta)

    async def stop(self) -> None:
        """Unsubscribe. Fail-soft: never raises."""
        if self._sid is None:
            return
        try:
            await self._bus.unsubscribe(self._sid)
        except Exception:  # noqa: BLE001 -- fail-soft teardown
            logger.debug("[CausalDeltaSubscriber] unsubscribe failed",
                         exc_info=True)
        finally:
            self._sid = None

    # -- handler -----------------------------------------------------------

    async def _on_delta(self, event: TrinityEvent) -> None:
        """Record one causal delta. NEVER raises into the bus (fail-soft) and
        is a fast append (non-blocking -- no sleeps, no heavy work)."""
        try:
            payload = getattr(event, "payload", None)
            if not isinstance(payload, dict):
                logger.debug("[CausalDeltaSubscriber] dropping non-dict payload")
                return
            if "delta" not in payload:
                logger.debug("[CausalDeltaSubscriber] dropping envelope with no "
                             "'delta' block")
                return
            lineage = payload.get("lineage")
            if not isinstance(lineage, dict) or any(
                k not in lineage for k in _REQUIRED_LINEAGE_KEYS
            ):
                logger.debug("[CausalDeltaSubscriber] dropping envelope with "
                             "missing/malformed lineage: %r", lineage)
                return

            # Reflective source read from the IN-PAYLOAD lineage.repo (Mandate 2:
            # the RepoType rides as reflective metadata INSIDE the data payload,
            # never typed on the transport). This is the identity that survives
            # the WS hop verbatim -- ``event.source`` is re-minted to the
            # receiver bus's local_repo by the generic bridge and MUST NOT be
            # trusted here. ``RepoType(...)`` reflection IS the validation (no
            # string-matching / if-chain, no topic parsing); an unknown repo
            # raises -> log-and-drop. BROADCAST is a TARGET semantic, never a
            # valid causal origin, so it is dropped too.
            try:
                repo = RepoType(lineage["repo"])
            except (ValueError, TypeError):
                logger.debug(
                    "[CausalDeltaSubscriber] dropping delta with non-RepoType "
                    "lineage.repo %r (topic=%s)", lineage.get("repo"),
                    getattr(event, "topic", None))
                return
            if repo == RepoType.BROADCAST:
                logger.debug(
                    "[CausalDeltaSubscriber] dropping delta with BROADCAST "
                    "lineage.repo (a causal delta needs a concrete source)")
                return

            try:
                emit_seq = int(lineage["emit_seq"])
            except (TypeError, ValueError):
                logger.debug("[CausalDeltaSubscriber] dropping envelope with "
                             "non-int emit_seq: %r", lineage.get("emit_seq"))
                return
            head_sha = lineage["head_sha"]
            if not isinstance(head_sha, str) or not head_sha:
                logger.debug("[CausalDeltaSubscriber] dropping envelope with "
                             "empty/non-str head_sha: %r", head_sha)
                return

            key = (repo.value, emit_seq, head_sha)
            if key in self._seen:
                # Replay outside the bus's 60s window -- idempotent skip.
                logger.debug("[CausalDeltaSubscriber] already-seen delta %r", key)
                return
            self._seen.add(key)

            self._per_repo.setdefault(repo.value, []).append((emit_seq, head_sha))
            self._receive_order.append(repo.value)

            if self._on_delta_cb is not None:
                try:
                    self._on_delta_cb(payload)
                except Exception:  # noqa: BLE001 -- callback is fail-soft
                    logger.debug("[CausalDeltaSubscriber] on_delta callback "
                                 "raised (swallowed)", exc_info=True)
        except Exception:  # noqa: BLE001 -- handler MUST never raise into bus
            logger.warning("[CausalDeltaSubscriber] _on_delta failed "
                           "(swallowed, fail-soft)", exc_info=True)

    # -- read surface ------------------------------------------------------

    def observed(self) -> List[Tuple[str, int, str]]:
        """All observed ``(repo, emit_seq, head_sha)``.

        Cross-repo interleave follows global receive order; WITHIN each repo the
        subsequence is ``emit_seq``-ordered (the Lamport causal guarantee). We
        assign each repo's emit_seq-sorted items into that repo's receive-order
        slots, so both invariants hold simultaneously.
        """
        sorted_by_repo: Dict[str, List[Tuple[int, str]]] = {
            repo: sorted(items) for repo, items in self._per_repo.items()
        }
        cursors: Dict[str, int] = {repo: 0 for repo in sorted_by_repo}
        out: List[Tuple[str, int, str]] = []
        for repo in self._receive_order:
            idx = cursors[repo]
            emit_seq, head_sha = sorted_by_repo[repo][idx]
            cursors[repo] = idx + 1
            out.append((repo, emit_seq, head_sha))
        return out

    def observed_count(self) -> int:
        """Number of distinct deltas recorded."""
        return len(self._receive_order)
