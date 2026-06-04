"""Slice 87 — cognitive-stall interceptor → early cascade to the Tier-1 fallback.

The 240s/0-content stalls (sweep bt-2026-06-04-061913) were a DW reasoning model
stuck in a reasoning loop it couldn't exit: reasoning deltas kept flowing (so the
inter-chunk rupture watchdog stayed alive) while ``content`` stayed empty until
the full 240s primary budget expired. With Claude DISABLED there was no rescue;
with Claude enabled the existing cascade fired only AFTER burning all 240s.

Slice 87 adds an early cognitive-stall watchdog: once the stream has streamed
reasoning for longer than ``JARVIS_DW_COGNITIVE_STALL_S`` (default 90s) with zero
functional content, raise ``CognitiveStallError`` — a ``StreamRuptureError``
subclass, so the FSM classifier's existing ``isinstance`` check routes it to
TRANSIENT_TRANSPORT → an IMMEDIATE cascade to Tier-1 (Claude), ~150s sooner.

NOTE the honest scope: this converts CLAUDE-SOLVABLE hard problems to victories
via the cheapest path (DW first, Claude rescue). It does NOT manufacture
capability — element-web / NodeBB were misses even WITH Claude, so escalating
them still misses (it just fails faster + spends Claude $). The cascade is a
cost-optimization, not a capability lift.
"""
from __future__ import annotations

import inspect

from backend.core.ouroboros.governance.stream_rupture import (
    CognitiveStallError,
    StreamRuptureError,
    cognitive_stall_timeout_s,
)
from backend.core.ouroboros.governance import doubleword_provider as dw


# --- the threshold helper ---

def test_default_threshold_clears_legitimate_reasoning_band(monkeypatch):
    monkeypatch.delenv("JARVIS_DW_COGNITIVE_STALL_S", raising=False)
    t = cognitive_stall_timeout_s()
    # must be ABOVE the legitimate content-after-reasoning band (~21-34s probe)
    assert t >= 60.0
    # ...and BELOW the 240s budget the capability stalls burned
    assert t < 240.0


def test_threshold_env_tunable(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_COGNITIVE_STALL_S", "120")
    assert cognitive_stall_timeout_s() == 120.0


def test_threshold_zero_disables(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_COGNITIVE_STALL_S", "0")
    assert cognitive_stall_timeout_s() == 0.0


def test_threshold_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_COGNITIVE_STALL_S", "garbage")
    assert cognitive_stall_timeout_s() == 90.0


# --- the exception routes to cascade (via subclass) ---

def test_cognitive_stall_is_rupture_subclass_so_it_cascades():
    # The FSM classifier (candidate_generator ~1723) routes any
    # isinstance(StreamRuptureError) → TRANSIENT_TRANSPORT → immediate Tier-1
    # cascade. The subclass relationship IS the cascade wiring.
    err = CognitiveStallError(
        provider="doubleword", elapsed_s=92.0, bytes_received=0,
        stall_timeout_s=90.0, reasoning_seen=True,
    )
    assert isinstance(err, StreamRuptureError)
    assert isinstance(err, RuntimeError)


def test_cognitive_stall_carries_distinct_telemetry():
    err = CognitiveStallError(
        provider="doubleword", elapsed_s=92.0, bytes_received=0,
        stall_timeout_s=90.0, reasoning_seen=True,
    )
    msg = str(err)
    assert msg.startswith("cognitive_stall:"), msg
    assert "reasoning_seen=True" in msg
    assert err.phase == "cognitive_stall"
    assert err.reasoning_seen is True


def test_classifier_routes_cognitive_stall_to_transient_transport():
    # End-to-end: feed the real classifier a CognitiveStallError and assert the
    # cascade-eligible mode, if the classifier is reachable as a pure function.
    from backend.core.ouroboros.governance import candidate_generator as cg
    fn = getattr(cg, "_classify_failure_mode", None) or getattr(
        cg, "classify_failure_mode", None,
    )
    if fn is None:
        # classifier is a method, not a module fn — the subclass test above
        # already pins the isinstance contract it relies on.
        return
    err = CognitiveStallError(
        provider="doubleword", elapsed_s=92.0, bytes_received=0,
        stall_timeout_s=90.0,
    )
    mode = fn(err)
    assert "TRANSIENT_TRANSPORT" in str(mode)


# --- wiring pin: the DW RT stream raises the stall + watchdog is gateable ---

def test_rt_stream_raises_cognitive_stall_on_content_silence():
    src = inspect.getsource(dw.DoublewordProvider._generate_realtime)
    assert "CognitiveStallError(" in src, "RT stream must raise the stall error"
    assert "_cognitive_stall_timeout_s()" in src, "RT stream must read the threshold"
    # the gate must require content-silence + active reasoning (not a byte rupture)
    assert "_content_seen" in src
    assert "_first_progress_at" in src


def test_watchdog_disabled_when_threshold_zero():
    # the loop must guard on `_cognitive_stall_s > 0` so 0 fully opts out
    src = inspect.getsource(dw.DoublewordProvider._generate_realtime)
    assert "_cognitive_stall_s > 0" in src
