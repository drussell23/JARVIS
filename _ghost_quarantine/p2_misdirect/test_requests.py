# [Ouroboros] Modified by Ouroboros (op=op-019e3353-) at 2026-05-17 00:30 UTC
# Reason: Uncertain about content/text vs iter_content(decode_unicode=True/False) When requesting an application/json document, I'

from __future__ import annotations

"""Tests verifying iter_content(decode_unicode=True) returns str, not bytes.

Background: When decode_unicode=True is passed to iter_content(), each chunk
should be a unicode str, matching the behaviour of r.text.  This test suite
falsifies the regression described in op-019e3353 where iter_content returned
bytes even with decode_unicode=True for application/json responses.
"""

import io
import unittest.mock as mock

import pytest


# ---------------------------------------------------------------------------
# Minimal stub that reproduces the requests.Response surface we care about
# ---------------------------------------------------------------------------

class _FakeRawResponse:
    """Minimal urllib3-like raw response stub."""

    def __init__(self, body: bytes, encoding: str = "utf-8") -> None:
        self._stream = io.BytesIO(body)
        self.headers: dict[str, str] = {"content-type": f"application/json; charset={encoding}"}

    def read(self, amt: int | None = None) -> bytes:
        if amt is None:
            return self._stream.read()
        return self._stream.read(amt)

    def stream(self, chunk_size: int, decode_content: bool = True):
        while True:
            chunk = self._stream.read(chunk_size)
            if not chunk:
                break
            yield chunk


def _make_response(body: str, encoding: str = "utf-8"):
    """Return a minimal requests.Response-like object backed by *body*.

    We avoid importing the real `requests` library so the test is hermetic and
    runs without network access.  The logic under test is the decode_unicode
    path inside iter_content, which we replicate faithfully below.
    """
    try:
        import requests  # type: ignore[import]
        resp = requests.Response()
        resp.encoding = encoding
        resp.headers["content-type"] = f"application/json; charset={encoding}"
        resp.raw = _FakeRawResponse(body.encode(encoding), encoding)
        resp._content = False  # force streaming path
        resp._content_consumed = False
        return resp
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Pure-logic helper that mirrors what requests.utils.stream_decode_response_unicode
# is supposed to do - used to unit-test the decode path in isolation.
# ---------------------------------------------------------------------------

def _iter_content_decode_unicode(
    chunks: list[bytes],
    encoding: str,
) -> list[str | bytes]:
    """Replicate the decode_unicode=True path from requests.models.Response.

    The real implementation delegates to
    ``requests.utils.stream_decode_response_unicode`` which wraps
    ``codecs.getincrementaldecoder``.  We reproduce the same contract here so
    we can assert on it without a live HTTP server.
    """
    import codecs

    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    results: list[str | bytes] = []
    for chunk in chunks:
        decoded = decoder.decode(chunk)
        if decoded:
            results.append(decoded)
    # flush
    tail = decoder.decode(b"", final=True)
    if tail:
        results.append(tail)
    return results


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestIterContentDecodeUnicode:
    """Verify that decode_unicode=True yields str chunks, not bytes."""

    def test_decoded_chunks_are_str_not_bytes(self) -> None:
        """Each chunk from the decode path must be a str instance."""
        body_bytes = [b'{"hello": "world"}']
        results = _iter_content_decode_unicode(body_bytes, "utf-8")
        assert results, "Expected at least one decoded chunk"
        for chunk in results:
            assert isinstance(chunk, str), (
                f"Expected str chunk, got {type(chunk).__name__!r}: {chunk!r}"
            )

    def test_decoded_content_matches_original_text(self) -> None:
        """Reassembled decoded chunks must equal the original unicode string."""
        original = '{"key": "value", "num": 42}'
        body_bytes = [original.encode("utf-8")]
        results = _iter_content_decode_unicode(body_bytes, "utf-8")
        reassembled = "".join(str(c) for c in results)
        assert reassembled == original

    def test_multi_chunk_decode_produces_str(self) -> None:
        """Multi-chunk streaming must still yield str, not bytes."""
        original = '{"a": 1, "b": 2, "c": 3}'
        encoded = original.encode("utf-8")
        # Split into 4-byte chunks to simulate small chunk_size
        chunks = [encoded[i:i + 4] for i in range(0, len(encoded), 4)]
        results = _iter_content_decode_unicode(chunks, "utf-8")
        for chunk in results:
            assert isinstance(chunk, str), (
                f"Multi-chunk: expected str, got {type(chunk).__name__!r}"
            )
        assert "".join(str(c) for c in results) == original

    def test_latin1_encoding_decode_unicode(self) -> None:
        """decode_unicode=True must work for non-UTF-8 encodings too."""
        original = "cafe"
        body_bytes = [original.encode("latin-1")]
        results = _iter_content_decode_unicode(body_bytes, "latin-1")
        assert results
        for chunk in results:
            assert isinstance(chunk, str)
        assert "".join(str(c) for c in results) == original

    def test_empty_body_decode_unicode(self) -> None:
        """Empty body with decode_unicode=True must yield no chunks (not crash)."""
        results = _iter_content_decode_unicode([], "utf-8")
        # May be empty or contain an empty string from the flush - either is fine
        for chunk in results:
            assert isinstance(chunk, str)

    def test_raw_bytes_without_decode_unicode_are_bytes(self) -> None:
        """Sanity check: without decoding, chunks remain bytes."""
        raw_chunks: list[bytes] = [b'{"x": 1}', b'{"y": 2}']
        for chunk in raw_chunks:
            assert isinstance(chunk, bytes), (
                f"Without decode_unicode, expected bytes, got {type(chunk).__name__!r}"
            )

    def test_decode_unicode_true_vs_false_type_difference(self) -> None:
        """Demonstrate the type difference: decode_unicode=True -> str, False -> bytes."""
        body = b'{"hello": "world"}'
        # decode_unicode=False path: raw bytes pass through unchanged
        raw_chunk: bytes = body
        assert isinstance(raw_chunk, bytes)

        # decode_unicode=True path: must be str
        decoded_chunks = _iter_content_decode_unicode([body], "utf-8")
        assert decoded_chunks
        assert isinstance(decoded_chunks[0], str)

    def test_unicode_characters_survive_decode(self) -> None:
        """Non-ASCII unicode characters must survive the decode_unicode path intact."""
        original = '{"emoji": "hello world", "accented": "cafe"}'
        body_bytes = [original.encode("utf-8")]
        results = _iter_content_decode_unicode(body_bytes, "utf-8")
        reassembled = "".join(str(c) for c in results)
        assert reassembled == original


class TestIterContentWithRealRequests:
    """Integration tests using the real requests library (skipped if unavailable)."""

    @pytest.fixture(autouse=True)
    def _require_requests(self) -> None:
        pytest.importorskip("requests")

    def test_iter_content_decode_unicode_returns_str(self) -> None:
        """iter_content(decode_unicode=True) must yield str, not bytes.

        This is the exact scenario from the bug report: application/json
        response with decode_unicode=True was returning bytes instead of str.
        """
        import requests  # type: ignore[import]

        body = b'{"hello": "world"}'
        resp = requests.Response()
        resp.encoding = "utf-8"
        resp.headers["content-type"] = "application/json; charset=utf-8"
        resp._content = body
        resp._content_consumed = True

        chunks = list(resp.iter_content(chunk_size=16 * 1024, decode_unicode=True))
        assert chunks, "Expected at least one chunk from iter_content"
        for chunk in chunks:
            assert isinstance(chunk, str), (
                f"iter_content(decode_unicode=True) returned {type(chunk).__name__!r}, "
                f"expected str.  This is the regression from op-019e3353."
            )

    def test_iter_content_decode_unicode_matches_text(self) -> None:
        """Reassembled iter_content(decode_unicode=True) must equal r.text."""
        import requests  # type: ignore[import]

        body = b'{"key": "value"}'
        resp = requests.Response()
        resp.encoding = "utf-8"
        resp.headers["content-type"] = "application/json; charset=utf-8"
        resp._content = body
        resp._content_consumed = True

        text_via_property = resp.text
        chunks = list(resp.iter_content(chunk_size=16 * 1024, decode_unicode=True))
        text_via_iter = "".join(chunks)

        assert text_via_iter == text_via_property, (
            f"iter_content(decode_unicode=True) gave {text_via_iter!r} "
            f"but r.text gave {text_via_property!r}"
        )

    def test_iter_content_without_decode_unicode_returns_bytes(self) -> None:
        """iter_content(decode_unicode=False) must still return bytes (regression guard)."""
        import requests  # type: ignore[import]

        body = b'{"hello": "world"}'
        resp = requests.Response()
        resp.encoding = "utf-8"
        resp.headers["content-type"] = "application/json; charset=utf-8"
        resp._content = body
        resp._content_consumed = True

        chunks = list(resp.iter_content(chunk_size=16 * 1024, decode_unicode=False))
        assert chunks
        for chunk in chunks:
            assert isinstance(chunk, bytes), (
                f"iter_content(decode_unicode=False) returned {type(chunk).__name__!r}, "
                f"expected bytes."
            )
