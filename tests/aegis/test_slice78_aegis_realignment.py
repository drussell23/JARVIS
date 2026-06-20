"""Slice 78 Track 2 — Aegis upstream transport robustness (env-tunable sock_read
+ caught upstream-read timeout).

Verify-first scope: the runbook's premises (strip `-dottxt`, fix a flush/buffer
drop) were DISPROVEN — no `-dottxt` transform exists in the code and the SSE
forward loop is byte-faithful. What IS real (proven by the diagnostic agent):
  1. The upstream `sock_read` was a hardcoded 30s that ALSO governs TTFT; a
     reasoning model's first token can exceed it, so the proxy would cut before
     the DW provider's own 120s/360s rupture window. Now env-tunable (default
     UNCHANGED at 30s — not raised blindly without confirmed TTFT>30s evidence).
  2. The `iter_any()` upstream-read loop had NO `except` — a sock_read timeout
     ESCAPED `forward_request` (which documents "never raises") and EOF'd the
     client with 0 SSE bytes → the DW provider saw an empty stream → exhaustion
     → misclassified `live_transport:RuntimeError`. Now caught → clean
     `UPSTREAM_UNREACHABLE`.
"""
from __future__ import annotations

import inspect

from backend.core.ouroboros.aegis import forwarding


def test_sock_read_default_is_unchanged_30s(monkeypatch):
    monkeypatch.delenv("JARVIS_AEGIS_UPSTREAM_SOCK_READ_S", raising=False)
    assert forwarding._upstream_sock_read_timeout_s() == 30.0


def test_sock_read_is_env_overridable(monkeypatch):
    monkeypatch.setenv("JARVIS_AEGIS_UPSTREAM_SOCK_READ_S", "120")
    assert forwarding._upstream_sock_read_timeout_s() == 120.0
    monkeypatch.setenv("JARVIS_AEGIS_UPSTREAM_SOCK_READ_S", "360.5")
    assert forwarding._upstream_sock_read_timeout_s() == 360.5


def test_sock_read_invalid_falls_back_to_default(monkeypatch):
    for bad in ("bad", "-5", "0", "", "   "):
        monkeypatch.setenv("JARVIS_AEGIS_UPSTREAM_SOCK_READ_S", bad)
        assert forwarding._upstream_sock_read_timeout_s() == 30.0, bad


def test_timeout_construction_uses_the_resolver():
    src = inspect.getsource(forwarding.forward_request)
    assert "_upstream_sock_read_timeout_s()" in src, (
        "the ClientTimeout must use the env-tunable resolver, not the constant"
    )


# --- the contract fix: upstream-read failures are CAUGHT, not escaped ---

def test_forward_loop_catches_upstream_read_timeout():
    src = inspect.getsource(forwarding.forward_request)
    # the iter_any loop's try must now catch the upstream read exceptions
    assert "asyncio.TimeoutError" in src
    assert "aiohttp.ServerTimeoutError" in src
    assert "upstream_read_failed" in src


def test_upstream_read_failure_maps_to_unreachable_outcome():
    src = inspect.getsource(forwarding.forward_request)
    # the new flag must drive the clean UPSTREAM_UNREACHABLE outcome
    assert "upstream_read_failed" in src
    assert "ForwardOutcome.UPSTREAM_UNREACHABLE" in src
    # ordering: the flag is set in the except, consumed in the outcome ladder
    assert src.index("upstream_read_failed = True") < src.rindex(
        "ForwardOutcome.UPSTREAM_UNREACHABLE"
    )


# --- the upstream connection must be released on EVERY exit path -------------
# Bug (2026-06-20 DW-stream soak): only the guillotine branch released the
# upstream ClientResponse. The normal-completion, client-disconnect, and
# upstream-read-failed paths left it unreleased, so when ``async with session``
# closed, aiohttp logged "Unclosed connection" and orphaned the upstream socket.


def test_upstream_response_released_in_finally_on_all_paths():
    src = inspect.getsource(forwarding.forward_request)
    # release must appear at least twice: the pre-existing guillotine branch
    # AND the new all-paths release in the streaming finally.
    assert src.count("upstream_resp.release()") >= 2, (
        "upstream_resp must be released on every exit path, not just guillotine"
    )
    # the all-paths release must live inside the streaming finally (after the
    # write_eof), so client-disconnect / upstream-read-failed breaks still hit it.
    fin = src.index("finally:")
    eof = src.index("write_eof", fin)
    assert src.index("upstream_resp.release()", eof) > eof, (
        "the all-paths release must be in the finally, after write_eof"
    )


def test_release_is_guarded_so_cleanup_never_fails_loud():
    src = inspect.getsource(forwarding.forward_request)
    rel = src.index("upstream_resp.release()", src.index("write_eof"))
    # the all-paths release is wrapped so a cleanup error can't break the return
    window = src[rel - 60:rel + 120]
    assert "try:" in window and "except Exception" in window, (
        "the all-paths release must be guarded (cleanup must never fail loud)"
    )
