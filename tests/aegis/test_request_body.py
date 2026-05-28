"""Slice 42 — Aegis full-body capped reader (multi-segment truncation fix)."""
from __future__ import annotations

import pytest

from backend.core.ouroboros.aegis.request_body import (
    BodyTooLarge,
    read_body_capped,
)


class _FragmentedContent:
    """Simulates aiohttp StreamReader delivering a body across multiple TCP
    segments — read(n) returns only the NEXT fragment (≤ n), not the whole
    body, which is exactly the semantics that truncated the old single-read."""

    def __init__(self, body: bytes, fragment_size: int):
        self._body = body
        self._frag = fragment_size
        self._pos = 0

    async def read(self, n: int) -> bytes:
        if self._pos >= len(self._body):
            return b""
        end = min(self._pos + min(self._frag, n), len(self._body))
        chunk = self._body[self._pos:end]
        self._pos = end
        return chunk


class _FakeReq:
    def __init__(self, content):
        self.content = content


async def test_reads_full_body_across_fragments():
    # 18 KB body delivered in 4 KB segments — the bug's exact shape.
    body = b"A" * 18432
    req = _FakeReq(_FragmentedContent(body, fragment_size=4096))
    out = await read_body_capped(req, cap=4 * 1024 * 1024)
    assert out == body  # byte-identical, FULL body (not just the first 4 KB)
    assert len(out) == 18432


async def test_old_single_read_would_truncate():
    # Documents the bug: a single read(huge) returns only the first segment.
    body = b"A" * 18432
    content = _FragmentedContent(body, fragment_size=4096)
    first = await content.read(4 * 1024 * 1024)
    assert len(first) == 4096 and first != body  # truncated → broken multipart


async def test_64kb_multi_fragment_byte_identical():
    body = bytes((i % 256) for i in range(65536))  # varied bytes, not just 'A'
    req = _FakeReq(_FragmentedContent(body, fragment_size=1500))  # ~MTU-sized
    out = await read_body_capped(req, cap=4 * 1024 * 1024)
    assert out == body


async def test_small_single_segment_body():
    body = b'{"x": 1}\n'
    req = _FakeReq(_FragmentedContent(body, fragment_size=64 * 1024))
    assert await read_body_capped(req, cap=4 * 1024 * 1024) == body


async def test_empty_body():
    req = _FakeReq(_FragmentedContent(b"", fragment_size=4096))
    assert await read_body_capped(req, cap=4 * 1024 * 1024) == b""


async def test_raises_too_large_over_cap():
    cap = 8192
    body = b"A" * (cap + 1)
    req = _FakeReq(_FragmentedContent(body, fragment_size=4096))
    with pytest.raises(BodyTooLarge) as ei:
        await read_body_capped(req, cap=cap)
    assert ei.value.cap == cap


async def test_exactly_at_cap_succeeds():
    cap = 8192
    body = b"A" * cap
    req = _FakeReq(_FragmentedContent(body, fragment_size=4096))
    out = await read_body_capped(req, cap=cap)
    assert len(out) == cap
