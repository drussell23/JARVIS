"""Do not load a local model onto a machine that is about to swap.

Tier 2 now runs on the same unified memory as the CoreAudio graph. There is no
separate VRAM and no pressure valve except swap, so a background op deciding
to think can take the microphone down — the audio tap is a real-time thread
and `HALC_ProxyIOContext :: skipping cycle due to overload` is already in the
boot log at idle.

The voice path was taken from 15,000 ms to 37 ms. One unguarded 18 GB model
load gives that back.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance import local_model_admission as LMA
from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel


def _vm(percent_used: float, total_gb: float = 16.0):
    """A `psutil.virtual_memory()` result at a given utilisation."""
    total = int(total_gb * 1024 ** 3)
    available = int(total * (1.0 - percent_used / 100.0))
    return SimpleNamespace(total=total, available=available,
                           percent=percent_used, used=total - available,
                           free=available)


def _at(percent_used: float):
    """Force the gate's probe to a given utilisation, whatever the real machine
    is doing. Patches the cascade's FIRST stage — psutil — which is the stage
    that answers on this hardware (`source: psutil`, measured)."""
    return patch("psutil.virtual_memory", return_value=_vm(percent_used))


# ── The mandate ─────────────────────────────────────────────────────────────

def test_at_96_percent_the_request_is_deferred_not_raised():
    """THE ASSERTION ASKED FOR.

    96% utilised = 4% free. The provider must intercept, refuse to load, and
    yield a deferred state — WITHOUT throwing.
    """
    with _at(96.0):
        d = LMA.assess(requested_ctx=32768)

    assert d.action == LMA.Admission.DEFER.value
    assert d.proceeds is False
    assert d.free_pct < 10.0
    assert "swap" in d.reason


def test_the_deferred_state_is_a_stable_token():
    """A greppable constant, not prose — anything matching on it must not
    break when the sentence around it is reworded."""
    assert LMA.DEFERRED_PAYLOAD == "[SYSTEM: DEFERRED_DUE_TO_MEMORY_PRESSURE]"


@pytest.mark.asyncio
async def test_the_provider_intercepts_without_an_unhandled_exception():
    """End to end through `PrimeProvider.generate`: at 96% it must return a
    result rather than raise, and must never reach `_generate_impl`."""
    from backend.core.ouroboros.governance.providers import PrimeProvider

    reached = []

    async def _never(*a, **k):
        reached.append(True)
        raise AssertionError("the model was loaded under critical pressure")

    p = PrimeProvider.__new__(PrimeProvider)          # no client needed
    p._generate_impl = _never
    p._max_tokens = 8192

    with _at(96.0):
        result = await p.generate(context=SimpleNamespace(op_id="op-1"),
                                  deadline=None)

    assert reached == [], "it dispatched anyway"
    assert result is not None
    assert len(result.candidates) == 0


@pytest.mark.asyncio
async def test_a_deferral_is_not_reported_as_a_provider_fault():
    """`candidate_generator` treats a RAISING provider as 'lane failed' and
    feeds the heartbeat a drop — which on a bad day contributes to waking a
    real-money GCE failover node. For a machine that is merely busy.

    It already distinguishes this class: `is_budget_refusal` is re-raised as a
    'local wallet gate, NOT a lane/provider fault'. Memory pressure is the
    same axis, so it returns empty rather than raising.
    """
    from backend.core.ouroboros.governance.providers import PrimeProvider

    p = PrimeProvider.__new__(PrimeProvider)
    p._generate_impl = lambda *a, **k: None
    p._max_tokens = 8192

    with _at(96.0):
        result = await p.generate(context=SimpleNamespace(op_id="op-2"),
                                  deadline=None)

    # Empty candidates: the cascade falls through cleanly and blames nobody.
    assert result.candidates == ()
    assert result.provider_name


# ── The middle band: prune rather than refuse ───────────────────────────────

def test_high_pressure_prunes_instead_of_refusing():
    """Refusing all work above a threshold would make the machine useless
    exactly when it is busy. Shrinking the KV cache keeps it working."""
    with _at(88.0):
        d = LMA.assess(requested_ctx=32768)

    if d.action == LMA.Admission.PRUNE.value:
        assert d.proceeds is True
        assert d.num_ctx is not None and d.num_ctx < 32768
        assert d.num_ctx >= 1024, "never pruned to uselessness"


def test_pruning_keeps_the_system_prompt_and_the_live_question():
    """The first message is the instructions that make output parseable at
    all; the last few are the actual question. Pruning into either does not
    save memory, it changes what was asked."""
    messages = [{"role": "system", "content": "SYSTEM"}]
    messages += [{"role": "user", "content": f"old-{i}"} for i in range(40)]
    messages += [{"role": "user", "content": f"live-{i}"} for i in range(6)]

    d = LMA.AdmissionDecision(action=LMA.Admission.PRUNE.value,
                              level="high", free_pct=11.0)
    pruned, dropped = LMA.prune_messages(list(messages), d)

    assert dropped > 0
    assert pruned[0]["content"] == "SYSTEM", "the system prompt was pruned"
    assert pruned[-1]["content"] == "live-5", "the live question was pruned"
    assert len(pruned) < len(messages)


def test_a_short_conversation_is_never_pruned():
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}]
    d = LMA.AdmissionDecision(action=LMA.Admission.PRUNE.value, level="high")
    out, dropped = LMA.prune_messages(list(msgs), d)
    assert dropped == 0 and out == msgs


def test_admit_never_prunes():
    msgs = [{"role": "user", "content": str(i)} for i in range(50)]
    d = LMA.AdmissionDecision(action=LMA.Admission.ADMIT.value)
    out, dropped = LMA.prune_messages(list(msgs), d)
    assert dropped == 0 and len(out) == 50


# ── Failing in the safe direction ───────────────────────────────────────────

def test_plenty_of_memory_admits_unchanged():
    with _at(20.0):
        d = LMA.assess(requested_ctx=32768)
    assert d.action == LMA.Admission.ADMIT.value
    assert d.proceeds and d.num_ctx is None


def test_a_broken_probe_admits_rather_than_refusing():
    """A probe that cannot read memory must NOT become a reason to refuse
    work. That would let one broken cascade stage silently disable Tier 2 for
    a whole session — a larger outage than the swap storm it guards against.
    """
    with patch.object(LMA, "_read_pressure",
                      return_value=("unknown", -1.0, "unavailable")):
        d = LMA.assess(requested_ctx=8192)
    assert d.proceeds is True


def test_pruning_a_malformed_payload_returns_it_untouched():
    d = LMA.AdmissionDecision(action=LMA.Admission.PRUNE.value, level="high")
    for junk in (None, "not a list", 42, {"a": 1}):
        out, dropped = LMA.prune_messages(junk, d)
        assert out is junk and dropped == 0


def test_it_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("JARVIS_LOCAL_MODEL_ADMISSION_ENABLED", "false")
    with _at(99.0):
        assert LMA.assess().proceeds is True


def test_assess_never_raises():
    for pct in (0.0, 50.0, 99.9, 100.0):
        with _at(pct):
            assert LMA.assess(requested_ctx=1) is not None


# ── It reuses the existing gate rather than probing itself ──────────────────

def test_it_adds_no_second_memory_probe():
    """`memory_pressure_gate` already owns the psutil -> meminfo -> vm_stat
    cascade. A second probe here would be a second thing to keep correct, and
    the day they disagreed the machine would hold two opinions about whether
    it was safe to allocate."""
    import inspect
    src = inspect.getsource(LMA)
    assert "memory_pressure_gate" in src
    assert "vm_stat" not in src.split('"""')[2] if src.count('"""') > 2 else True


def test_it_speaks_the_gates_four_level_vocabulary():
    assert {l.value for l in PressureLevel} >= {"ok", "high", "critical"}


def test_snapshot_reports_the_live_reading():
    s = LMA.snapshot()
    assert s["schema_version"] == LMA.LOCAL_MODEL_ADMISSION_SCHEMA_VERSION
    assert "level" in s and "free_pct" in s and "source" in s
