"""transcript_log — frame codec + crash-recovery reader for the spine.

Slice 2 of the transcript arc (slice 1 = :mod:`transcript_spine`, which
is memory-only and whose closing note names exactly this work: "append
log + torn-record recovery + atomic compaction … wants its own kill
test").

This module is steps 1 of the build: the **codec** and the **reader**.
Everything the three oracles test lives here; the writer is built to
satisfy it, not the other way round.

The frame
---------

One record, one line::

    <crc32-hex>\\t<json>\\n
    2f8a1c04\\t{"kind":"diff","ref":"d-3","seq":7,"v":1}\\n

Five properties, each chosen against a specific failure:

**Self-delimiting by newline.** ``json.dumps`` with ``ensure_ascii=True``
cannot emit a literal newline or tab — both are escaped inside strings —
so the first TAB is always the delimiter and the first NEWLINE is always
the record boundary. The framing can never be confused by its own
payload, which is what lets recovery resynchronise without a length
field to corrupt.

**CRC first, fixed width.** Eight hex characters before the payload mean
a record is validated *before* it is parsed. A torn line is rejected by
arithmetic, not by a JSON parser's opinion of it, and a truncation
landing inside the CRC itself is caught by the width check.

**CRC over the exact payload bytes.** POSIX does not promise that a
single ``write()`` larger than a sector is atomic under power loss, so
torn-record detection must not *depend* on write atomicity. It depends
on the checksum instead, which holds regardless of what the device did.

**Version per record, not per file.** A file header would be a single
point whose loss orphans an otherwise-intact log, and would need a
special case in the scanner. ``"v"`` on every record makes each line
self-describing, and a truncation at byte 3 costs one record rather than
the file's identity.

**Deterministic encoding.** ``sort_keys=True`` and compact separators
mean a given record always produces identical bytes — so a re-encode is
byte-comparable, and the oracles can assert on exact frames.

The reader, and why it stops
----------------------------

:func:`recover_log` returns the longest **valid prefix** and the byte
offset where it ends. On the first record that fails any check it stops —
it does not skip the bad record and keep going.

That is the load-bearing decision. Skipping would silently reorder the
transcript: the spine's whole guarantee is that ``seq`` is a total order,
and a reader that steps over a hole returns a sequence which never
existed while claiming it did. Worse, a valid record *after* an invalid
one means the writer appended past a torn tail — the one corruption the
writer's seal exists to prevent — so continuing would paper over the
exact bug the oracles are built to catch. A prefix is what a
crash-consistent log actually promises, so a prefix is what it returns.

Bytes after the stopping point are counted and reported, never parsed.

Authority boundary
------------------

* §1 deterministic — pure functions over bytes; no I/O policy, no LLM
* §7 fail-closed — an unreadable log yields an EMPTY prefix, never a
  guessed one
* §8 observable — every rejection carries a machine-readable reason
"""
from __future__ import annotations

import enum
import json
import logging
import os
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger("Ouroboros.TranscriptLog")


__all__ = [
    "FRAME_VERSION",
    "MAX_RECORD_BYTES_ENV_VAR",
    "RecoveryResult",
    "RejectReason",
    "decode_frame",
    "encode_record",
    "read_max_record_bytes",
    "recover_log",
]


#: Bumped only when the framing changes incompatibly. A reader that meets
#: a higher version stops rather than guessing — see :func:`recover_log`.
FRAME_VERSION: int = 1

_CRC_WIDTH: int = 8
_SEP: bytes = b"\t"
_TERM: bytes = b"\n"

#: Required keys on every decoded record. A line whose CRC is intact but
#: whose shape is wrong is still unusable, and accepting it would push the
#: failure downstream into whatever consumes the transcript.
_REQUIRED_FIELDS: Tuple[str, ...] = ("v", "seq", "kind", "ref")

MAX_RECORD_BYTES_ENV_VAR: str = "JARVIS_TRANSCRIPT_MAX_RECORD_BYTES"

#: A ceiling on one frame. Without it a corrupt file containing no
#: newline at all would be buffered entirely into memory before being
#: rejected — turning a disk fault into an OOM.
_DEFAULT_MAX_RECORD_BYTES: int = 1 << 20  # 1 MiB

_READ_CHUNK: int = 1 << 16


def read_max_record_bytes() -> int:
    """Resolve :data:`MAX_RECORD_BYTES_ENV_VAR`. NEVER raises."""
    raw = os.environ.get(MAX_RECORD_BYTES_ENV_VAR, "").strip()
    if not raw:
        return _DEFAULT_MAX_RECORD_BYTES
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_RECORD_BYTES
    return parsed if parsed > 0 else _DEFAULT_MAX_RECORD_BYTES


# ===========================================================================
# Closed taxonomy — why a frame was refused
# ===========================================================================


class RejectReason(str, enum.Enum):
    """Every way a frame can fail, named. Empty string = accepted.

    Closed and specific because "the log was corrupt" is not an
    actionable fact: a torn tail after a kill is expected and benign,
    while a CRC mismatch in the middle of a file is a different event
    entirely, and telemetry that cannot tell them apart cannot tell the
    operator which one happened.
    """

    NONE = ""
    TRUNCATED = "truncated"              # no terminator — the torn tail
    SHORT_FRAME = "short_frame"          # shorter than the CRC field
    BAD_CRC_FIELD = "bad_crc_field"      # CRC field is not 8 hex chars
    NO_SEPARATOR = "no_separator"        # no TAB after the CRC
    CRC_MISMATCH = "crc_mismatch"        # payload does not match its CRC
    BAD_JSON = "bad_json"                # CRC intact, JSON unparseable
    NOT_AN_OBJECT = "not_an_object"      # valid JSON, wrong top-level type
    MISSING_FIELD = "missing_field"      # required key absent
    BAD_VERSION = "bad_version"          # newer/unknown framing
    NON_MONOTONIC_SEQ = "non_monotonic_seq"  # order violated
    OVERSIZE = "oversize"                # frame exceeds the ceiling


# ===========================================================================
# Encode
# ===========================================================================


def encode_record(record: Mapping[str, Any]) -> bytes:
    """Serialise one record to a complete frame, terminator included.

    Raises ``ValueError`` / ``TypeError`` on a record that cannot be
    represented — deliberately, and unlike most of this codebase. A
    record that will not encode must never reach the log as a partial
    line; failing here keeps the fault on the caller's side of the file
    boundary, where it can still be handled without corrupting anything.
    """
    payload = dict(record)
    payload.setdefault("v", FRAME_VERSION)
    body = json.dumps(
        payload, separators=(",", ":"), sort_keys=True, ensure_ascii=True,
    ).encode("ascii")
    crc = zlib.crc32(body) & 0xFFFFFFFF
    return b"%s%s%s%s" % (
        ("%08x" % crc).encode("ascii"), _SEP, body, _TERM,
    )


# ===========================================================================
# Decode
# ===========================================================================


def decode_frame(line: bytes) -> Tuple[Optional[Dict[str, Any]], RejectReason]:
    """Validate and parse ONE frame body (terminator already stripped).

    Returns ``(record, RejectReason.NONE)`` or ``(None, reason)``. Pure:
    no I/O, no globals, no exceptions out. Checks run cheapest-first so a
    torn line costs a length comparison rather than a JSON parse."""
    if len(line) < _CRC_WIDTH:
        return None, RejectReason.SHORT_FRAME

    crc_field = line[:_CRC_WIDTH]
    try:
        expected = int(crc_field.decode("ascii"), 16)
    except (UnicodeDecodeError, ValueError):
        return None, RejectReason.BAD_CRC_FIELD

    if line[_CRC_WIDTH:_CRC_WIDTH + 1] != _SEP:
        return None, RejectReason.NO_SEPARATOR

    body = line[_CRC_WIDTH + 1:]
    if (zlib.crc32(body) & 0xFFFFFFFF) != expected:
        return None, RejectReason.CRC_MISMATCH

    try:
        record = json.loads(body.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        # Unreachable through a matching CRC unless the writer encoded
        # garbage; kept because "CRC says intact" must not be allowed to
        # imply "therefore parseable".
        return None, RejectReason.BAD_JSON

    if not isinstance(record, dict):
        return None, RejectReason.NOT_AN_OBJECT
    for key in _REQUIRED_FIELDS:
        if key not in record:
            return None, RejectReason.MISSING_FIELD
    if record.get("v") != FRAME_VERSION:
        return None, RejectReason.BAD_VERSION
    if not isinstance(record.get("seq"), int) or record["seq"] < 1:
        return None, RejectReason.MISSING_FIELD

    return record, RejectReason.NONE


# ===========================================================================
# Recovery
# ===========================================================================


@dataclass(frozen=True)
class RecoveryResult:
    """What survived a crash, and precisely where the good bytes end."""

    records: Tuple[Dict[str, Any], ...] = ()
    #: Offset one past the last VALID frame. The writer truncates to this
    #: before its first append, which is what makes a crash loop
    #: idempotent rather than cumulative.
    durable_bytes: int = 0
    #: Bytes present beyond ``durable_bytes`` — counted, never parsed.
    trailing_bytes: int = 0
    #: Why the scan stopped. NONE means it reached a clean end of file.
    stop_reason: RejectReason = RejectReason.NONE
    #: 1-indexed frame number that failed, or 0 when none did.
    stop_frame: int = 0
    file_size: int = 0
    exists: bool = True

    @property
    def clean(self) -> bool:
        """True when the file ended exactly on a frame boundary."""
        return self.trailing_bytes == 0 and self.stop_reason is RejectReason.NONE

    @property
    def head_seq(self) -> int:
        return self.records[-1]["seq"] if self.records else 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "records": len(self.records),
            "durable_bytes": self.durable_bytes,
            "trailing_bytes": self.trailing_bytes,
            "stop_reason": self.stop_reason.value,
            "stop_frame": self.stop_frame,
            "file_size": self.file_size,
            "head_seq": self.head_seq,
            "clean": self.clean,
            "exists": self.exists,
        }


def recover_log(
    path: "os.PathLike[str] | str", *, max_record_bytes: Optional[int] = None,
) -> RecoveryResult:
    """Read the longest valid prefix of a transcript log. NEVER raises.

    Streams the file in chunks so a multi-megabyte log costs bounded
    memory, and refuses any single frame larger than
    ``max_record_bytes`` — a corrupt file containing no newline must
    become a rejection, not an allocation.

    A missing file is not an error: an organism that has never written a
    transcript has an empty one, which is exactly what a first boot
    should recover.
    """
    limit = int(max_record_bytes) if max_record_bytes else read_max_record_bytes()
    p = Path(path)

    try:
        size = p.stat().st_size
    except FileNotFoundError:
        return RecoveryResult(exists=False)
    except OSError:
        logger.debug("[TranscriptLog] stat failed for %s", p, exc_info=True)
        return RecoveryResult(exists=False)

    records: List[Dict[str, Any]] = []
    durable = 0
    frame_no = 0
    stop = RejectReason.NONE
    stop_frame = 0
    last_seq = 0

    try:
        with open(p, "rb") as fh:
            buf = b""
            eof = False
            while not eof and stop is RejectReason.NONE:
                chunk = fh.read(_READ_CHUNK)
                if not chunk:
                    eof = True
                else:
                    buf += chunk

                while True:
                    nl = buf.find(_TERM)
                    if nl < 0:
                        # No complete frame in hand. An unterminated
                        # remainder longer than the ceiling can never
                        # become one, so reject now rather than buffer on.
                        if len(buf) > limit:
                            stop = RejectReason.OVERSIZE
                            stop_frame = frame_no + 1
                        break

                    line, buf = buf[:nl], buf[nl + 1:]
                    frame_no += 1

                    if len(line) + 1 > limit:
                        stop, stop_frame = RejectReason.OVERSIZE, frame_no
                        break

                    record, reason = decode_frame(line)
                    if reason is not RejectReason.NONE:
                        stop, stop_frame = reason, frame_no
                        break

                    assert record is not None  # decode contract
                    if record["seq"] <= last_seq:
                        # CRC cannot catch a reordered or duplicated
                        # append (two writers, or a resumed writer that
                        # rewound). The order IS the transcript, so a
                        # violation ends the trustworthy prefix.
                        stop, stop_frame = RejectReason.NON_MONOTONIC_SEQ, frame_no
                        break

                    last_seq = record["seq"]
                    records.append(record)
                    durable += len(line) + 1
                if stop is not RejectReason.NONE:
                    break
    except OSError:
        logger.debug("[TranscriptLog] read failed for %s", p, exc_info=True)
        # Whatever was validated before the read fault is still a valid
        # prefix; returning it beats discarding a good transcript because
        # its tail was unreadable.

    trailing = max(0, size - durable)
    if stop is RejectReason.NONE and trailing > 0:
        # Clean frames, then an unterminated remainder: the classic torn
        # tail left by a kill mid-append.
        stop = RejectReason.TRUNCATED
        stop_frame = frame_no + 1

    return RecoveryResult(
        records=tuple(records),
        durable_bytes=durable,
        trailing_bytes=trailing,
        stop_reason=stop,
        stop_frame=stop_frame,
        file_size=size,
        exists=True,
    )
