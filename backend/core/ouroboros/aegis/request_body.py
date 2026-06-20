"""Aegis-side full-body reader with DoS cap (Slice 42).

Fixes a latent multi-segment truncation bug at the two proxy body-read sites
(``passthrough.py`` + ``forwarding.py``). Both used::

    body_bytes = await request.content.read(cap)

``aiohttp.StreamReader.read(n)`` returns *at most* ``n`` bytes "but at least
one byte" — for a body that arrives across multiple TCP segments (e.g. an
18 KB multipart/form-data batch upload) the single call returns ONLY the first
buffered segment and the rest is silently dropped. The proxy then forwarded a
truncated multipart body upstream, which DW's parser rejected with HTTP 400
("Multipart parsing failed"). Small bodies (one segment) slipped through,
masking the bug.

``read_body_capped`` reads the FULL body via a chunk-accumulation loop and
preserves the DoS protection: if the accumulated length exceeds ``cap`` it
raises :class:`BodyTooLarge` (caller → HTTP 413) WITHOUT buffering past the
cap. It never silently truncates.

Sovereign Aegis Batch-Passthrough Matrix (2026-06-20)
-----------------------------------------------------
``read_body_capped`` buffers the ENTIRE body in memory before forwarding —
fine for the ~KB control-plane payloads, but for a MASSIVE multi-file
architectural refactor the batch input JSONL (full file contents in the
GENERATE prompt) can run to many megabytes, and buffering it twice (once
here, once in the outbound aiohttp request) is exactly the blocking behaviour
the batch lane must avoid. ``stream_body_capped`` is the constant-memory
counterpart: an async generator that drains ``request.content`` chunk-by-chunk
and YIELDS each chunk straight to the outbound request, holding at most one
``_READ_CHUNK_BYTES`` window at a time while still enforcing ``cap`` mid-stream.
``content_length_hint`` lets the caller reject an over-cap upload cleanly
(HTTP 413) from the declared ``Content-Length`` BEFORE a single byte is read,
when the client provides one (aiohttp always does for a bytes/FormData body).
"""
from __future__ import annotations

from typing import AsyncIterator, Optional

# Per-iteration read unit. Large enough to drain quickly, bounded so a single
# read() can't be coerced into an unbounded allocation. The cap is enforced
# across the accumulation, independent of this unit.
_READ_CHUNK_BYTES = 64 * 1024


class BodyTooLarge(Exception):
    """Raised when the accumulated request body exceeds the DoS cap.

    Carries ``cap`` so the caller can emit a precise HTTP 413 detail.
    """

    def __init__(self, cap: int) -> None:
        self.cap = cap
        super().__init__(f"request body exceeds cap of {cap} bytes")


async def read_body_capped(request, cap: int) -> bytes:
    """Read the ENTIRE request body, enforcing ``cap``.

    Loops ``request.content.read(_READ_CHUNK_BYTES)`` until EOF (empty read),
    accumulating chunks. Raises :class:`BodyTooLarge` the moment the running
    total exceeds ``cap`` — bounding buffered memory to ``cap + one chunk``
    so an exhaustion attack cannot force unbounded allocation. Returns the
    full body bytes on success.

    Does NOT swallow transport errors — ``asyncio.CancelledError`` /
    ``aiohttp.ClientError`` propagate to the caller's existing handler.
    """
    chunks = []
    total = 0
    while True:
        chunk = await request.content.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise BodyTooLarge(cap)
        chunks.append(chunk)
    return b"".join(chunks)


def content_length_hint(request) -> Optional[int]:
    """Return the declared ``Content-Length`` as an int, or ``None``.

    Used by the passthrough to reject an over-cap upload with a clean HTTP 413
    BEFORE reading any body, when the client declares its size (aiohttp always
    sets Content-Length for a ``bytes``/``FormData`` body). Returns ``None`` for
    a missing or malformed header — callers MUST then fall back to the
    mid-stream cap in :func:`stream_body_capped` (never trust the hint alone).
    NEVER raises.
    """
    raw = request.headers.get("Content-Length")
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


async def stream_body_capped(request, cap: int) -> AsyncIterator[bytes]:
    """Stream the request body chunk-by-chunk, enforcing ``cap`` mid-stream.

    Constant-memory counterpart to :func:`read_body_capped`: drains
    ``request.content`` one ``_READ_CHUNK_BYTES`` window at a time and YIELDS
    each chunk to the caller (which feeds it straight to the outbound request),
    so the proxy never holds the whole body. Raises :class:`BodyTooLarge` the
    moment the running total exceeds ``cap`` — bounding both buffered memory and
    accepted bytes to ``cap + one chunk``. Never silently truncates.

    Transport errors (``asyncio.CancelledError`` / ``aiohttp.ClientError``)
    propagate to the caller's existing handler — not swallowed.
    """
    total = 0
    while True:
        chunk = await request.content.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise BodyTooLarge(cap)
        yield chunk
