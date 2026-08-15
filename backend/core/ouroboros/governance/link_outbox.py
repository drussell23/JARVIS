"""link_outbox — verdicts survive the Body being gone.

THE FAILURE THIS EXISTS FOR
---------------------------
The Body is on a laptop that leaves the house. The Engine keeps working —
that is the entire point of a proactive organism — and every completed op
produces a verdict the Body has not acknowledged. Held in a list, those
accumulate for as long as the outage lasts, and an outage measured in hours
ends as an OOM on the machine holding the accelerator.

The naive fixes are both wrong. An unbounded queue trades a network problem
for a memory problem. A bounded queue that drops silently trades it for a
correctness problem, and a dropped verdict is indistinguishable from an op
that never ran — the exact ambiguity §26.6 spent a section removing.

So: bounded in memory, **spilled to disk** past the threshold, and shed only
at a second bound with a loud, countable signal. Disk is cheap and a laptop
can be away a long time; silence is what must never happen.

WHY THIS REUSES THE TRANSCRIPT CODEC
------------------------------------
``transcript_log.encode_record`` produces ``<crc32-hex>\\t<json>\\n``:
self-delimiting by newline, CRC before payload, version per record. It was
built for an append log, and every property it was built for is the same
property a spill file needs — a frame torn by a power cut is rejected by
arithmetic rather than by a JSON parser's opinion, and ``recover_log``
returns the longest valid prefix instead of failing the file.

It is also the codec on the wire (see ``link_transport``). One encoding for
both, so a verdict spilled to disk and the same verdict on the socket are
byte-identical, and there is no serialise/deserialise seam between them
where a schema could drift.

Durability is ``durable_io``'s: ``flush_and_sync`` before the record is
counted as spilled, because "we wrote it" must mean "it survives".

ORDERING
--------
FIFO across both tiers, always. Drain reads the disk generation before the
memory tail, because anything spilled is by definition older. A verdict
delivered out of order is a verdict the Body's contiguous watermark will
refuse to advance past, so ordering here is not tidiness — it is what makes
the resume arithmetic in ``link_protocol`` work at all.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import logging
import os
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Ouroboros.LinkOutbox")

LINK_OUTBOX_SCHEMA_VERSION: str = "link_outbox.1"


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def memory_high_water() -> int:
    """Records held in RAM before spilling begins."""
    return _env_int("JARVIS_LINK_OUTBOX_MEM_RECORDS", 512, minimum=8)


def disk_max_records() -> int:
    """Hard ceiling across the spill file. Beyond this the link is hopeless."""
    return _env_int("JARVIS_LINK_OUTBOX_DISK_RECORDS", 50_000, minimum=64)


def spill_under_pressure_level() -> str:
    """Memory-pressure level at which spilling starts early. Default ``high``.

    Composes ``memory_pressure_gate`` rather than probing: the Engine's RAM
    is that module's subject, and a second reading here would be a second
    authority for a number the operator sees in one place.
    """
    return (os.environ.get("JARVIS_LINK_OUTBOX_SPILL_LEVEL", "high")
            or "high").strip().lower()


@dataclass(frozen=True)
class OutboxStats:
    queued: int
    in_memory: int
    on_disk: int
    spilled_total: int
    shed_total: int
    delivered_total: int
    disk_bytes: int
    schema_version: str = LINK_OUTBOX_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version, "queued": self.queued,
            "in_memory": self.in_memory, "on_disk": self.on_disk,
            "spilled_total": self.spilled_total, "shed_total": self.shed_total,
            "delivered_total": self.delivered_total,
            "disk_bytes": self.disk_bytes,
        }


class LinkOutbox:
    """A FIFO of undelivered records that survives both absence and restart.

    Two tiers, one order. Records enter memory; once memory is at its high
    water mark the OLDEST are appended to a spill file and dropped from RAM.
    Draining reads disk first, so FIFO holds across the boundary.

    Not a queue of tasks — a queue of BYTES already encoded. Encoding at
    ``put`` time means a record that cannot be represented fails at the
    caller, not later at the socket where the only options are to crash the
    writer or to silently drop.
    """

    def __init__(self, spill_path: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        self._mem: "deque[bytes]" = deque()
        self._spill_path = Path(spill_path) if spill_path else None
        self._disk_count = 0
        self._disk_consumed = 0
        self._spilled_total = 0
        self._shed_total = 0
        self._delivered_total = 0
        self._sealed = False

    # -- ingress ---------------------------------------------------------

    def put(self, record: Dict[str, Any]) -> bool:
        """Queue one record. False when it was shed (and said so loudly).

        The record must satisfy the transcript codec's envelope contract —
        ``v``/``seq``/``kind``/``ref`` — because reusing a codec means
        accepting its shape, not only its bytes. Its own comment says why:
        *a line whose CRC is intact but whose shape is wrong is still
        unusable, and accepting it would push the failure downstream.* A
        record that skipped the contract would encode cleanly, spill
        cleanly, and then be rejected by ``recover_log`` after a restart —
        losing exactly the verdicts this module exists to preserve, at
        exactly the moment they matter.

        ``seq`` and ``kind`` are the CALLER's to supply and are never
        invented here: ``seq`` is load-bearing for resume arithmetic, and a
        fabricated one would put a hole in a range the peer will later ask
        to replay. ``ref`` is derived, because for a link frame it is a
        restatement of the two.
        """
        try:
            from backend.core.ouroboros.battle_test.transcript_log import (
                encode_record,
            )
            from backend.core.ouroboros.governance.link_protocol import (
                ensure_frame_envelope,
            )
            payload = ensure_frame_envelope(record)
            frame = encode_record(payload)
        except (ValueError, TypeError) as exc:
            # Deliberately loud: an unencodable record is a caller bug, and
            # swallowing it here would put a hole in a sequence the peer
            # will later ask us to replay.
            logger.error("[LinkOutbox] unencodable record dropped: %s", exc)
            with self._lock:
                self._shed_total += 1
            return False

        with self._lock:
            self._mem.append(frame)
            over = len(self._mem) - self._effective_high_water()
        if over > 0:
            self._spill(over)
        return True

    def _effective_high_water(self) -> int:
        """High water, lowered when the machine is already under pressure.

        Adaptive rather than fixed: holding 512 records is harmless on an
        idle Engine and reckless on one whose RAM is already the constraint,
        and the module that knows which is which already exists.
        """
        base = memory_high_water()
        try:
            from backend.core.ouroboros.governance.memory_pressure_gate import (
                get_default_gate,
            )
            level = str(getattr(get_default_gate().pressure(), "value", ""))
            if level in ("high", "critical"):
                return max(8, base // 8)
            if level == "warn":
                return max(8, base // 2)
        except Exception:  # noqa: BLE001 — pressure is advice, never a blocker
            pass
        return base

    def _spill(self, count: int) -> None:
        """Move the OLDEST ``count`` frames to disk. Never raises."""
        if self._spill_path is None:
            # No spill target: bound in memory alone, shedding oldest and
            # saying so. Better a counted loss than an OOM on the machine
            # holding the accelerator.
            with self._lock:
                for _ in range(count):
                    if not self._mem:
                        break
                    self._mem.popleft()
                    self._shed_total += 1
            logger.warning(
                "[LinkOutbox] no spill path — shed %d record(s) at the "
                "memory bound", count)
            return
        with self._lock:
            if self._disk_count - self._disk_consumed >= disk_max_records():
                for _ in range(count):
                    if not self._mem:
                        break
                    self._mem.popleft()
                    self._shed_total += 1
                shed = self._shed_total
            else:
                batch = [self._mem.popleft() for _ in range(count)
                         if self._mem]
                shed = None
        if shed is not None:
            logger.error(
                "[LinkOutbox] spill file at its %d-record ceiling — shedding. "
                "Total shed=%d. The peer has been unreachable long enough "
                "that verdicts are now being lost.",
                disk_max_records(), shed)
            return
        try:
            self._spill_path.parent.mkdir(parents=True, exist_ok=True)
            from backend.core.ouroboros.governance.durable_io import (
                flush_and_sync,
            )
            with open(self._spill_path, "ab") as fh:
                for frame in batch:
                    fh.write(frame)
                # Durable BEFORE the record is counted as spilled: a frame
                # that is only in the page cache has not survived anything.
                flush_and_sync(fh)
            with self._lock:
                self._disk_count += len(batch)
                self._spilled_total += len(batch)
        except OSError as exc:
            # Disk refused. The frames are already out of memory, so put
            # them back rather than losing them silently.
            with self._lock:
                for frame in reversed(batch):
                    self._mem.appendleft(frame)
            logger.error("[LinkOutbox] spill failed (%s) — held in memory", exc)

    # -- egress ----------------------------------------------------------

    def drain(self, limit: int) -> List[bytes]:
        """Up to ``limit`` frames in FIFO order. Disk generation first.

        Frames are NOT removed until :meth:`ack` — a drained-but-unsent
        batch must survive the socket dying between the two, which is the
        window this whole module exists to cover.
        """
        n = max(0, int(limit))
        if n == 0:
            return []
        out: List[bytes] = []
        disk_frames = self._read_disk(n)
        out.extend(disk_frames)
        if len(out) < n:
            with self._lock:
                remaining = n - len(out)
                out.extend(list(self._mem)[:remaining])
        return out

    def ack(self, count: int) -> None:
        """Confirm ``count`` frames reached the peer. Removes them."""
        n = max(0, int(count))
        with self._lock:
            available_disk = self._disk_count - self._disk_consumed
            from_disk = min(n, available_disk)
            self._disk_consumed += from_disk
            n -= from_disk
            for _ in range(n):
                if not self._mem:
                    break
                self._mem.popleft()
            self._delivered_total += max(0, int(count))
            compact = (self._disk_consumed > 0
                       and self._disk_consumed == self._disk_count)
        if compact:
            self._truncate_spill()

    def _read_disk(self, limit: int) -> List[bytes]:
        """Re-read the spill generation. Never raises.

        Uses ``recover_log``'s discipline indirectly: a torn tail is the
        expected state after a power cut, and the longest valid prefix is
        the honest answer rather than a parse error on the whole file.
        """
        if self._spill_path is None:
            return []
        with self._lock:
            unread = self._disk_count - self._disk_consumed
            skip = self._disk_consumed
        if unread <= 0:
            return []
        try:
            frames: List[bytes] = []
            with open(self._spill_path, "rb") as fh:
                for idx, raw in enumerate(fh):
                    if idx < skip:
                        continue
                    if not raw.endswith(b"\n"):
                        break        # torn tail — stop at the valid prefix
                    frames.append(raw)
                    if len(frames) >= limit:
                        break
            return frames
        except OSError as exc:
            logger.error("[LinkOutbox] spill read failed: %s", exc)
            return []

    def _truncate_spill(self) -> None:
        """Everything on disk was acked — reclaim it durably."""
        if self._spill_path is None:
            return
        try:
            from backend.core.ouroboros.governance.durable_io import (
                atomic_replace,
            )
            tmp = self._spill_path.with_suffix(
                self._spill_path.suffix + ".compact")
            with open(tmp, "wb") as fh:
                from backend.core.ouroboros.governance.durable_io import (
                    flush_and_sync,
                )
                flush_and_sync(fh)
            atomic_replace(tmp, self._spill_path)
            with self._lock:
                self._disk_count = 0
                self._disk_consumed = 0
        except OSError as exc:
            logger.debug("[LinkOutbox] compaction deferred: %s", exc)

    # -- recovery --------------------------------------------------------

    def recover(self) -> int:
        """Adopt an existing spill file after a restart. Returns its count.

        The Engine restarting is not a reason to lose verdicts the Body has
        not seen: they are on disk precisely so they outlive the process
        that produced them.
        """
        if self._spill_path is None or not self._spill_path.exists():
            return 0
        try:
            from backend.core.ouroboros.battle_test.transcript_log import (
                recover_log,
            )
            result = recover_log(self._spill_path)
            count = len(getattr(result, "records", ()) or ())
            with self._lock:
                self._disk_count = count
                self._disk_consumed = 0
            if count:
                logger.info(
                    "[LinkOutbox] recovered %d undelivered record(s) from a "
                    "previous process", count)
            return count
        except Exception as exc:  # noqa: BLE001
            logger.error("[LinkOutbox] spill recovery failed: %s", exc)
            return 0

    # -- observability ---------------------------------------------------

    def stats(self) -> OutboxStats:
        with self._lock:
            on_disk = max(0, self._disk_count - self._disk_consumed)
            mem = len(self._mem)
        size = 0
        try:
            if self._spill_path and self._spill_path.exists():
                size = self._spill_path.stat().st_size
        except OSError:
            size = 0
        with self._lock:
            return OutboxStats(
                queued=mem + on_disk, in_memory=mem, on_disk=on_disk,
                spilled_total=self._spilled_total, shed_total=self._shed_total,
                delivered_total=self._delivered_total, disk_bytes=size,
            )
