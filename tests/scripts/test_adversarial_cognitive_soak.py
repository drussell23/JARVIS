"""Tests for scripts/adversarial_cognitive_soak.py.

NO real model / Ollama is touched here. A FakeLocalPrimeClient feeds a scripted
response sequence (wrong -> wrong-same-signature -> wrong-same-signature ->
[pivot+decompose] -> correct) and we assert the REAL cognitive-loop mechanics
fire:

  * the Hybrid Epistemic Diff is injected after the first failure,
  * temperature DECAYS across same-signature repeats,
  * pivot_verdict trips at the 3rd same-signature fail and decompose_for_block
    IS called,
  * a correct response -> converged=True,
  * a never-correct fake -> converged=False (bounded, no infinite loop),
  * the gate-off path refuses.

VALIDATE uses the REAL pytest subprocess execution in a tempdir (the test
payload is tiny + fast), so the test-execution boundary is real, not mocked.
"""
from __future__ import annotations

import asyncio

import pytest

# Module under test.
import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
_SCRIPT = os.path.join(_REPO, "scripts", "adversarial_cognitive_soak.py")
_spec = importlib.util.spec_from_file_location("adversarial_cognitive_soak", _SCRIPT)
acs = importlib.util.module_from_spec(_spec)
import sys as _sys

_sys.modules["adversarial_cognitive_soak"] = acs  # needed for dataclass on 3.11
_spec.loader.exec_module(acs)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Mimics PrimeResponse just enough for the loop (only .content read)."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.source = "fake_local_prime"


class FakeLocalPrimeClient:
    """Scripted stand-in for LocalPrimeClient.

    Records every (prompt, system_prompt, temperature) it is called with so the
    test can assert diff-injection and temperature decay.
    """

    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = []  # list of dict(prompt, system_prompt, temperature)

    async def generate(self, prompt, system_prompt=None, temperature=0.7, **kwargs):
        self.calls.append(
            {"prompt": prompt, "system_prompt": system_prompt, "temperature": temperature}
        )
        idx = min(len(self.calls) - 1, len(self._scripted) - 1)
        return _FakeResponse(self._scripted[idx])


# A wrong impl that BLOWS THE SAME EDGE CASE every time (stable signature):
# returns the wrong merge of overlapping intervals (ignores adjacency entirely).
_WRONG_SAME = """```python
def merge_intervals(intervals):
    if not intervals:
        return []
    intervals = sorted(intervals)
    out = [list(intervals[0])]
    for s, e in intervals[1:]:
        if s < out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [tuple(x) for x in out]
```"""

# A correct impl (handles adjacency s <= last_end and overlap).
_CORRECT = """```python
def merge_intervals(intervals):
    if not intervals:
        return []
    intervals = sorted(intervals)
    out = [list(intervals[0])]
    for s, e in intervals[1:]:
        if s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [tuple(x) for x in out]
```"""


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch):
    monkeypatch.setenv("JARVIS_CHAOS_INJECTOR_ENABLED", "true")
    # Make the pivot trip fast + temperature visibly decay. A non-zero floor
    # is reached after two decays (0.7 -> 0.35 -> 0.175 == floor), so
    # temp_at_floor becomes true and pivot_verdict can trip.
    monkeypatch.setenv("JARVIS_EPISTEMIC_TEMP_DECAY", "0.5")
    monkeypatch.setenv("JARVIS_EPISTEMIC_TEMP_FLOOR", "0.175")
    monkeypatch.setenv("JARVIS_EPISTEMIC_PIVOT_PASSES", "2")


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def test_gate_off_refuses(monkeypatch):
    monkeypatch.delenv("JARVIS_CHAOS_INJECTOR_ENABLED", raising=False)
    assert acs.gate_enabled() is False


def test_gate_on(monkeypatch):
    monkeypatch.setenv("JARVIS_CHAOS_INJECTOR_ENABLED", "true")
    assert acs.gate_enabled() is True


# ---------------------------------------------------------------------------
# Payload sanity
# ---------------------------------------------------------------------------


def test_payload_has_real_tests_and_signature():
    p = acs.ADVERSARIAL_PAYLOAD
    assert "def test_" in p.tests
    assert p.entry_symbol  # the symbol the impl must define
    # The reference correct impl must pass its own test suite (sanity).
    out = acs._run_pytest_in_tempdir(_CORRECT_IMPL_PLAIN, p.tests, timeout_s=60)
    assert out["passed"] is True, out["stderr"][-1500:]


_CORRECT_IMPL_PLAIN = acs._extract_code_block(_CORRECT)


def test_wrong_impl_fails_its_tests():
    p = acs.ADVERSARIAL_PAYLOAD
    out = acs._run_pytest_in_tempdir(acs._extract_code_block(_WRONG_SAME), p.tests, timeout_s=60)
    assert out["passed"] is False


# ---------------------------------------------------------------------------
# Loop mechanics
# ---------------------------------------------------------------------------


def test_diff_injected_after_first_fail_and_temp_decays():
    # wrong, wrong(same sig), wrong(same sig), correct
    client = FakeLocalPrimeClient([_WRONG_SAME, _WRONG_SAME, _WRONG_SAME, _CORRECT])
    result = asyncio.run(acs.run_cognitive_soak(client=client, max_repairs=3))

    # First call has no epistemic diff; subsequent calls do.
    assert "SOVEREIGN" not in client.calls[0]["prompt"] and \
        "Epistemic Feedback" not in client.calls[0]["prompt"] and \
        "FAILING TEST STDERR" not in client.calls[0]["prompt"]
    assert any("FAILING TEST STDERR" in c["prompt"] for c in client.calls[1:])

    # Temperature is monotonically non-increasing and strictly decays at least
    # once across the same-signature repeats (the parametric degeneration).
    temps = result["temperature_trajectory"]
    assert all(temps[i] >= temps[i + 1] for i in range(len(temps) - 1))
    assert min(temps) < max(temps)  # strictly decayed at least once

    assert result["epistemic_diffs_injected"] >= 1


def test_pivot_and_decompose_fire(monkeypatch):
    called = {"n": 0, "hints": []}
    real_decompose = acs.decompose_for_block

    def _spy(goal, **kwargs):
        called["n"] += 1
        called["hints"].append(kwargs.get("failure_hint"))
        return real_decompose(goal, **kwargs)

    monkeypatch.setattr(acs, "decompose_for_block", _spy)

    # Keep failing with the SAME signature long enough that the temperature
    # decays to the floor while the repeat-count climbs to the pivot threshold,
    # so pivot_verdict trips; only AFTER the pivot does a correct impl appear.
    client = FakeLocalPrimeClient([_WRONG_SAME] * 6 + [_CORRECT])
    result = asyncio.run(acs.run_cognitive_soak(client=client, max_repairs=6))

    assert result["pivoted"] is True
    assert result["decomposed"] is True
    assert called["n"] >= 1
    # The pivot passed a failure_hint with the signature.
    assert any(h and "stderr_tail" in h for h in called["hints"])


def test_converges_on_correct():
    client = FakeLocalPrimeClient([_WRONG_SAME, _CORRECT])
    result = asyncio.run(acs.run_cognitive_soak(client=client, max_repairs=3))
    assert result["converged"] is True
    assert result["attempts"] >= 2


def test_never_correct_is_bounded_non_convergence():
    client = FakeLocalPrimeClient([_WRONG_SAME])  # always the same wrong impl
    result = asyncio.run(acs.run_cognitive_soak(client=client, max_repairs=3))
    assert result["converged"] is False
    # Bounded: attempts must not explode. Generous ceiling (pre + repairs + pivot).
    assert result["attempts"] <= 8


def test_run_cognitive_soak_refuses_when_gate_off(monkeypatch):
    monkeypatch.delenv("JARVIS_CHAOS_INJECTOR_ENABLED", raising=False)
    client = FakeLocalPrimeClient([_CORRECT])
    with pytest.raises(RuntimeError):
        asyncio.run(acs.run_cognitive_soak(client=client, max_repairs=3))
