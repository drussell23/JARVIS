"""A diff archive that lives in another process.

The gap
-------
`capability_handoff` measured `diff_rows` UNSET on `ov`. The daemon owns the
`DiffArchive`, so `/expand d-3` typed at an attached cockpit travelled to the
daemon, opened the diff on the DAEMON's overlay, and mirrored back one line
saying `⏺ d-3 — esc closes`. The operator was told a diff had opened and was
shown nothing. On the surface an operator reviews changes from, the review
surface was the one thing that did not cross.

Why this is not a second overlay
--------------------------------
`DiffOverlayController` takes its archive as a constructor argument. It was
built transport-agnostic and nobody had used that: it needs only `lookup`,
`list_recent` and `all_refs`. So the client does not need a second controller,
a second renderer, a second epoch guard or a second off-thread Pygments pass —
it needs something archive-SHAPED. This is that.

Everything downstream is then literally the same code: the same syntax
highlighting, the same `Escape` arbitration, the same 483 ms off-thread render
that keeps the loop unstalled. A regression in the daemon's diff surfaces on
the client too, which is the property this codebase keeps buying deliberately.

Two payloads, because they have different costs
-----------------------------------------------
A CATALOG — refs, file counts, summaries, risk tiers, no diff text — is small
enough to ride the 1 Hz heartbeat, and it is what makes two things CORRECT
rather than approximate:

  * `all_refs()` — so an unknown ref lists what IS available. The archive is a
    ring, so a ref an operator read minutes ago may have been evicted, and
    "no such diff" alone invites them to doubt their typing.
  * the loading placeholder's file count — so the overlay confirms they opened
    what they meant before the diff itself arrives.

The TEXT is fetched only when a diff is actually opened. Diffs are unbounded
in a way nothing else on this lane is; putting them on the heartbeat would
mean shipping megabytes per second to draw nothing.

Pending is not absent
---------------------
The controller resolves synchronously and treats `None` as "no such diff" —
correct for a local dict, wrong across a socket, where "not here yet" and "not
a thing" are different answers and only one of them is an error. So `lookup`
returns a PENDING record built from the catalog: the overlay opens, shows the
right ref and file count, and re-renders when the text lands. An operator
never sees "no such diff" for a diff that exists and is in flight.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.DiffBridge")

DIFF_BRIDGE_SCHEMA_VERSION: str = "diff_bridge.1"

#: The frame the daemon publishes in reply to a fetch. Addressed to the
#: cockpit that asked, never broadcast — two attached operators reviewing
#: different diffs must not overwrite each other's overlay.
DIFF_PAYLOAD_KIND: str = "diff_payload"

#: The key the catalog rides under on the heartbeat. Additive under
#: `heartbeat.v1`, exactly as `agents` was: a client that has never heard of
#: it ignores the key, and a daemon that does not send one leaves the client
#: with an empty catalog rather than a version error.
CATALOG_KEY: str = "diffs"

__all__ = [
    "CATALOG_KEY",
    "DIFF_BRIDGE_SCHEMA_VERSION",
    "DIFF_PAYLOAD_KIND",
    "RemoteDiffArchive",
    "build_diff_catalog",
    "catalog_rows",
    "diff_bridge_enabled",
    "fetch_timeout_s",
    "max_diff_chars",
]


def diff_bridge_enabled() -> bool:
    """``JARVIS_DIFF_BRIDGE_ENABLED`` (default true). NEVER raises."""
    return os.environ.get(
        "JARVIS_DIFF_BRIDGE_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def catalog_rows() -> int:
    """Refs advertised on the heartbeat. NEVER raises.

    Bounded because this rides a 1 Hz frame. The number only has to cover
    what an operator might plausibly still refer to by ref; the archive's own
    ring is the real limit and `/diffs` can list it in full.
    """
    try:
        return max(1, min(64, int(
            os.environ.get("JARVIS_DIFF_CATALOG_ROWS", "") or 12)))
    except (TypeError, ValueError):
        return 12


def max_diff_chars() -> int:
    """Cap on a fetched diff's text. NEVER raises.

    A diff is the one payload on this lane with no natural bound — a
    generated migration can be megabytes, and a bridge frame that large
    stalls every other frame queued behind it, including the heartbeat that
    tells the operator anything is happening at all.

    Truncation is announced in the text rather than silent, because a diff
    that simply stops is indistinguishable from one that ended.
    """
    try:
        return max(4_000, min(4_000_000, int(
            os.environ.get("JARVIS_DIFF_MAX_CHARS", "") or 400_000)))
    except (TypeError, ValueError):
        return 400_000


def fetch_timeout_s() -> float:
    """How long a fetch may be outstanding before it is called lost.

    Not cosmetic: without it a daemon that dies mid-fetch leaves the overlay
    saying "rendering…" forever, which is the same lie every other
    heartbeat-fed surface in this cockpit is careful not to tell.
    """
    try:
        return max(1.0, min(120.0, float(
            os.environ.get("JARVIS_DIFF_FETCH_TIMEOUT_S", "") or 10.0)))
    except (TypeError, ValueError):
        return 10.0


def build_diff_catalog(archive: Any, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """The daemon's side: a bounded, text-free projection of the archive.

    Built from `ArchivedDiff.to_dict(include_diff_text=False)` — the
    projection that already exists for "keep SSE / observability payloads
    bounded" — rather than a hand-assembled dict, so a field added to the
    record travels without a second edit here. NEVER raises.
    """
    try:
        if archive is None:
            return []
        rows = list(archive.list_recent() or ())
        cap = catalog_rows() if limit is None else max(1, int(limit))
        out: List[Dict[str, Any]] = []
        for entry in rows[:cap]:
            try:
                out.append(entry.to_dict(include_diff_text=False))
            except Exception:  # noqa: BLE001
                continue
        return out
    except Exception:  # noqa: BLE001
        logger.debug("[DiffBridge] catalog degraded", exc_info=True)
        return []


class RemoteDiffArchive:
    """The daemon's archive, as seen from a cockpit that is not the daemon.

    Satisfies the duck-type `DiffOverlayController` already requires —
    ``lookup``, ``list_recent``, ``all_refs`` — so the controller mounts
    against it with no change at all.
    """

    __slots__ = ("_lock", "_catalog", "_full", "_pending", "_request",
                 "_clock", "_missing")

    def __init__(
        self,
        request: Optional[Callable[[str], None]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.RLock()
        #: ref -> catalog dict (no diff text). Replaced wholesale per frame.
        self._catalog: Dict[str, Dict[str, Any]] = {}
        #: ref -> fully-hydrated ArchivedDiff.
        self._full: Dict[str, Any] = {}
        #: ref -> monotonic time the fetch was issued.
        self._pending: Dict[str, float] = {}
        #: refs the daemon has explicitly said it does not have.
        self._missing: set = set()
        self._request = request
        self._clock = clock

    # -- ingest ---------------------------------------------------------

    def ingest_catalog(self, rows: Any) -> int:
        """Absorb the heartbeat's catalog. NEVER raises; returns the count.

        Replaced wholesale rather than merged: the archive is a RING, so an
        absent ref means evicted. Merging would let a ref the daemon dropped
        live forever in a client that happened to see it once.
        """
        try:
            fresh: Dict[str, Dict[str, Any]] = {}
            for row in list(rows or ()):
                if isinstance(row, dict) and row.get("ref"):
                    fresh[str(row["ref"])] = row
            with self._lock:
                self._catalog = fresh
                # A hydrated diff for a ref that has since been evicted is
                # dropped with it — otherwise `all_refs` and `lookup` would
                # disagree about what exists.
                for ref in [r for r in self._full if r not in fresh]:
                    self._full.pop(ref, None)
                self._missing.intersection_update(set())
            return len(fresh)
        except Exception:  # noqa: BLE001
            return 0

    def ingest_payload(self, frame: Any) -> Optional[str]:
        """Absorb a fetched diff. NEVER raises; returns the ref it filled.

        A frame marked `missing` records the negative rather than dropping it:
        without that, `lookup` would re-issue the fetch on the next render and
        the overlay would sit in a request loop against a ref that will never
        arrive.
        """
        try:
            if not isinstance(frame, dict):
                return None
            ref = str(frame.get("ref") or "")
            if not ref:
                return None
            with self._lock:
                self._pending.pop(ref, None)
                if frame.get("missing"):
                    self._missing.add(ref)
                    self._full.pop(ref, None)
                    return ref
                from backend.core.ouroboros.battle_test.diff_archive import (
                    ArchivedDiff,
                )
                entry = ArchivedDiff.from_dict(frame)
                if entry is None:
                    return None
                self._full[ref] = entry
                self._missing.discard(ref)
            return ref
        except Exception:  # noqa: BLE001
            logger.debug("[DiffBridge] payload ingest degraded", exc_info=True)
            return None

    # -- the archive duck-type ------------------------------------------

    def lookup(self, ref: object) -> Any:
        """``d-N`` -> entry, a PENDING entry, or None. NEVER raises.

        Three answers where a local archive has two, because across a socket
        "not here yet" is not "not a thing". Returning None for an in-flight
        fetch would render "no such diff" over a diff that exists.
        """
        try:
            text = str(ref or "").strip()
            if not text:
                return None
            with self._lock:
                if text in self._missing:
                    return None
                hydrated = self._full.get(text)
                if hydrated is not None:
                    return hydrated
                known = self._catalog.get(text)
            if known is None:
                return None
            self._ensure_fetch(text)
            # A record with no diff text. The controller renders its header
            # and file tree from the catalog fields, so the overlay is
            # immediately correct about WHAT is opening while the body loads.
            from backend.core.ouroboros.battle_test.diff_archive import (
                ArchivedDiff,
            )
            return ArchivedDiff.from_dict(known)
        except Exception:  # noqa: BLE001
            return None

    def list_recent(self, limit: Optional[int] = None) -> List[Any]:
        """Newest first, hydrated where possible. NEVER raises."""
        try:
            with self._lock:
                rows = list(self._catalog.values())
                full = dict(self._full)
            from backend.core.ouroboros.battle_test.diff_archive import (
                ArchivedDiff,
            )
            out = []
            for row in rows:
                ref = str(row.get("ref") or "")
                out.append(full.get(ref) or ArchivedDiff.from_dict(row))
            out = [e for e in out if e is not None]
            return out[:limit] if limit else out
        except Exception:  # noqa: BLE001
            return []

    def all_refs(self) -> Tuple[str, ...]:
        """Every ref the daemon currently holds. NEVER raises."""
        with self._lock:
            return tuple(self._catalog)

    # -- fetching -------------------------------------------------------

    def _ensure_fetch(self, ref: str) -> None:
        """Issue a fetch unless one is already outstanding. NEVER raises.

        Deduplicated on the ref, because `lookup` runs from a RENDER path and
        a repainting overlay would otherwise ask the daemon for the same diff
        at the frame rate.
        """
        try:
            now = float(self._clock())
            with self._lock:
                issued = self._pending.get(ref)
                if issued is not None and (now - issued) < fetch_timeout_s():
                    return          # already in flight
                self._pending[ref] = now
            if self._request is not None:
                self._request(ref)
        except Exception:  # noqa: BLE001
            logger.debug("[DiffBridge] fetch degraded", exc_info=True)

    def pending_refs(self) -> Tuple[str, ...]:
        """Fetches still within their timeout. NEVER raises."""
        try:
            now = float(self._clock())
            timeout = fetch_timeout_s()
            with self._lock:
                live = [r for r, at in self._pending.items()
                        if (now - at) < timeout]
                for ref in [r for r in self._pending if r not in live]:
                    self._pending.pop(ref, None)
            return tuple(live)
        except Exception:  # noqa: BLE001
            return ()

    def is_hydrated(self, ref: object) -> bool:
        with self._lock:
            return str(ref or "") in self._full

    def clear(self) -> None:
        with self._lock:
            self._catalog.clear()
            self._full.clear()
            self._pending.clear()
            self._missing.clear()
