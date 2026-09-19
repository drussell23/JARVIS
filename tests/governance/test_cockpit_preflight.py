"""The cockpit must not refuse to start because the model it wants is loaded.

`cockpit_interactive.sh` gated on raw free VRAM against a 20,480 MiB default —
the 30B's own footprint. Once that model is resident its ~20 GiB have moved from
`free` into `used`, so the gate compared the footprint against the space the
footprint occupies and refused. Measured on this host: 8,431 MiB free against a
20,480 MiB demand, with the message *"A training run or another soak still holds
the card"*. What held the card was the model the cockpit wanted.

Same arithmetic as the admission defect fixed in `16c530cfd3`, one layer up in
the launcher.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import cockpit_preflight as P

GIB = 1024 ** 3


def _run(model, **kw):
    return asyncio.run(P.verdict_for(model, **kw))


@pytest.fixture()
def card(monkeypatch):
    """Control both probes: what the card reports and what is resident."""
    state = {"free": 8 * GIB, "resident": ("qwen3-coder-ov:30b", 20 * GIB)}

    async def _resident(endpoint):
        return state["resident"]

    import backend.core.ouroboros.governance.candidate_generator as CG
    monkeypatch.setattr(CG, "fetch_resident_weights", _resident)
    monkeypatch.setattr(CG, "local_lane_endpoint", lambda: "http://x:11434")
    monkeypatch.setattr(P, "_accelerator_free_bytes", lambda: state["free"])
    return state


# --------------------------------------------------------------------------
# THE regression
# --------------------------------------------------------------------------

def test_a_resident_model_does_not_need_room_for_itself(card):
    """The defect, exactly: 8 GiB free, a 20 GiB model — already loaded."""
    v = _run("qwen3-coder-ov:30b")
    assert v.ok is True
    assert "already resident" in v.reason
    assert v.resident_bytes == 20 * GIB


def test_the_old_arithmetic_would_have_refused_this(card):
    """Pins the contrast: the raw-free gate demanded the whole footprint."""
    v = _run("qwen3-coder-ov:30b")
    assert v.free_bytes < 20 * GIB       # would fail `free >= footprint`
    assert v.ok is True                  # and is correct anyway


def test_a_resident_model_still_needs_KV_headroom(card):
    """Resident is not a blank cheque — the KV cache grows during a session."""
    card["free"] = 1 * GIB
    v = _run("qwen3-coder-ov:30b")
    assert v.ok is False
    assert "KV growth" in v.reason


def test_a_tag_variant_still_counts_as_resident(card):
    """`qwen3-coder-ov:30b` serves a request for `qwen3-coder-ov`."""
    assert _run("qwen3-coder-ov").ok is True


# --------------------------------------------------------------------------
# The absent case must still gate honestly
# --------------------------------------------------------------------------

def test_an_absent_model_must_fit_in_full(card):
    card["resident"] = ("", 0)
    card["free"] = 8 * GIB
    v = _run("other-model", footprint_bytes=20 * GIB)
    assert v.ok is False
    assert "does not fit" in v.reason


def test_an_absent_model_that_fits_is_admitted(card):
    card["resident"] = ("", 0)
    card["free"] = 30 * GIB
    v = _run("other-model", footprint_bytes=20 * GIB)
    assert v.ok is True
    assert "fits" in v.reason


def test_it_says_which_model_would_be_EVICTED(card):
    """A different model holding the card is the launcher's original message,
    and here it is true rather than a misdiagnosis."""
    card["free"] = 8 * GIB
    v = _run("other-model", footprint_bytes=20 * GIB)
    assert v.ok is False
    assert "would be evicted" in v.reason
    assert "qwen3-coder-ov:30b" in v.reason


# --------------------------------------------------------------------------
# It must never invent a refusal
# --------------------------------------------------------------------------

def test_an_unmeasurable_accelerator_gates_nothing(card):
    """A Mac or a CPU box has no nvidia-smi. A probe that could not run must
    not deny the operator a cockpit."""
    card["free"] = 0
    assert _run("anything", footprint_bytes=20 * GIB).ok is True


def test_an_unknown_footprint_defers_to_the_admission_gate(card):
    card["resident"] = ("", 0)
    v = _run("a-model-with-no-catalog-entry")
    assert v.ok is True
    assert "admission gate" in v.reason


def test_a_broken_probe_never_refuses(monkeypatch):
    async def _boom(endpoint):
        raise RuntimeError("ollama on fire")

    import backend.core.ouroboros.governance.candidate_generator as CG
    monkeypatch.setattr(CG, "fetch_resident_weights", _boom)
    monkeypatch.setattr(CG, "local_lane_endpoint", lambda: "http://x")
    v = _run("qwen3-coder-ov:30b")
    assert v.ok is True
    assert "not gating" in v.reason


def test_it_composes_the_admission_gates_own_probe():
    """One probe, not two. A second opinion about what is on the card is how
    the launcher and the gate came to disagree in the first place."""
    import inspect

    src = inspect.getsource(P.verdict_for)
    assert "fetch_resident_weights" in src


def test_the_eviction_race_is_NOT_re_guarded_here():
    """Deliberate: `local_model_admission` re-reads the accelerator at dispatch
    and is the authority. A redundant lease here could only disagree with it."""
    import inspect

    doc = P.__doc__ or ""
    assert "eviction race" in doc.lower()
    assert "admission" in doc.lower()
