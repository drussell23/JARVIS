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


# ---------------------------------------------------------------------------
# THRASH / non-convergence pivot (THE FIX exposed by the Concurrency Gauntlet)
# A model that produces a DIFFERENT failure each attempt never repeats a
# signature, so the legacy stuck-signature pivot NEVER trips. The
# budget-exhaustion backstop in should_pivot must catch it.
# ---------------------------------------------------------------------------

# Distinct failure modes (each a DIFFERENT signature, never repeating):
# 1. Module-level NameError on import (collection error)
_THRASH_IMPORT_ERR = """```python
def merge_intervals(intervals):
    return _undefined_symbol(intervals)
```"""

# 2. SyntaxError (collection error, different text)
_THRASH_SYNTAX_ERR = """```python
def merge_intervals(intervals)
    return intervals
```"""

# 3. Returns the input untouched -> different assertion failures
_THRASH_IDENTITY = """```python
def merge_intervals(intervals):
    return list(intervals)
```"""

# 4. Returns empty -> yet another distinct assertion failure
_THRASH_EMPTY = """```python
def merge_intervals(intervals):
    return []
```"""

# 5. Raises a different runtime error
_THRASH_TYPEERROR = """```python
def merge_intervals(intervals):
    return intervals + 1
```"""


def test_thrash_never_repeating_signature_pivots_on_budget(monkeypatch):
    """THE FIX: a model that emits a DIFFERENT failure each attempt
    (never-repeating signature) must STILL pivot -> decompose, via the
    budget-exhaustion backstop, instead of silently exhausting.

    Pre-fix (legacy pivot_verdict only) this would NEVER pivot because
    repeated_signature_count stays 0.
    """
    real_decompose = acs.decompose_for_block
    seen = {}

    def _spy(goal, **kwargs):
        seen["called"] = True
        seen["goal"] = goal
        return real_decompose(goal, **kwargs)

    monkeypatch.setattr(acs, "decompose_for_block", _spy)
    monkeypatch.setenv("JARVIS_EPISTEMIC_THRASH_PIVOT_ENABLED", "true")

    # Five DISTINCT failure modes, none repeating -> repeated_signature_count
    # stays 0 throughout. With max_repairs=3 the pivot budget is 1+3=4, so the
    # pivot must fire once attempts reach 4. The trailing _CORRECT lets the
    # post-pivot extra GENERATE converge against the decomposed chunk.
    client = FakeLocalPrimeClient([
        _THRASH_IMPORT_ERR,
        _THRASH_SYNTAX_ERR,
        _THRASH_IDENTITY,
        _THRASH_EMPTY,
        _THRASH_TYPEERROR,
        _CORRECT,
    ])
    result = asyncio.run(acs.run_cognitive_soak(client=client, max_repairs=3))

    # The signatures must NOT all be identical (genuine thrash, not a stuck wall).
    sigs = result["signatures"]
    assert len(set(sigs)) > 1, f"expected DIFFERENT signatures (thrash), got {sigs!r}"

    # The fix: it pivoted on budget-exhaustion, NOT on a repeated signature.
    assert result["pivoted"] is True, "thrash must pivot on budget-exhaustion"
    assert result["pivot_reason"] == "budget_exhausted", result["pivot_reason"]
    assert seen.get("called") is True, "decompose_for_block must be exercised"
    assert result["decomposed"] is True


def test_thrash_does_not_pivot_when_flag_off(monkeypatch):
    """OFF byte-identical: with JARVIS_EPISTEMIC_THRASH_PIVOT_ENABLED=false the
    thrash (never-repeating signature) does NOT pivot -- legacy behavior."""
    real_decompose = acs.decompose_for_block
    seen = {}

    def _spy(goal, **kwargs):
        seen["called"] = True
        return real_decompose(goal, **kwargs)

    monkeypatch.setattr(acs, "decompose_for_block", _spy)
    monkeypatch.setenv("JARVIS_EPISTEMIC_THRASH_PIVOT_ENABLED", "false")

    client = FakeLocalPrimeClient([
        _THRASH_IMPORT_ERR,
        _THRASH_SYNTAX_ERR,
        _THRASH_IDENTITY,
        _THRASH_EMPTY,
        _THRASH_TYPEERROR,
    ])
    result = asyncio.run(acs.run_cognitive_soak(client=client, max_repairs=3))

    assert result["pivoted"] is False, "flag off -> no thrash pivot (legacy)"
    assert result["pivot_reason"] == ""
    assert seen.get("called") is not True


# ===========================================================================
# Payload #2: the Concurrency Gauntlet (thread-safe TTL+LRU cache)
# ===========================================================================
#
# The load-bearing check: the gauntlet's pytest suite must actually CATCH the
# two bugs a 7B almost always ships -- a missing lock and a lazy-only/blocking
# TTL -- while a known-CORRECT reference impl PASSES. We prove discrimination
# with three reference impls run through the REAL pytest subprocess boundary,
# then drive the cognitive loop end-to-end with a scripted fake.

# A CORRECT impl: RLock around every mutation + a non-blocking daemon reaper
# that periodically reaps expired keys under the lock + clean stop().
_LRU_CORRECT = '''```python
from __future__ import annotations
import threading
import time
from collections import OrderedDict


class TTLLRUCache:
    def __init__(self, capacity, ttl_seconds):
        self.capacity = int(capacity)
        self.ttl_seconds = float(ttl_seconds)
        self._lock = threading.RLock()
        self._data = OrderedDict()  # key -> (value, expiry_monotonic)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._reaper, daemon=True)
        self._thread.start()

    def _reaper(self):
        interval = max(0.02, self.ttl_seconds / 10.0)
        while not self._stop.is_set():
            self._stop.wait(interval)
            if self._stop.is_set():
                break
            now = time.monotonic()
            with self._lock:
                dead = [k for k, (_v, exp) in self._data.items() if exp <= now]
                for k in dead:
                    self._data.pop(k, None)

    def get(self, key):
        now = time.monotonic()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            value, exp = item
            if exp <= now:
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return value

    def put(self, key, value):
        now = time.monotonic()
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = (value, now + self.ttl_seconds)
            while len(self._data) > self.capacity:
                self._data.popitem(last=False)

    def __len__(self):
        with self._lock:
            return len(self._data)

    def stop(self):
        self._stop.set()
```'''

# BUGGY (a): NO lock + lazy-only TTL (no background mechanism). Fails the
# thread-safety test (race on the multi-step read-modify-write -> KeyError)
# AND the background-eviction test (entry survives the wait).
_LRU_BUGGY_NO_LOCK_LAZY = '''```python
from __future__ import annotations
import time
from collections import OrderedDict


class TTLLRUCache:
    def __init__(self, capacity, ttl_seconds):
        self.capacity = int(capacity)
        self.ttl_seconds = float(ttl_seconds)
        self._data = OrderedDict()

    def get(self, key):
        item = self._data.get(key)
        if item is None:
            return None
        value, exp = item
        if exp <= time.monotonic():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def put(self, key, value):
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = (value, time.monotonic() + self.ttl_seconds)
        while len(self._data) > self.capacity:
            self._data.popitem(last=False)

    def __len__(self):
        return len(self._data)

    def stop(self):
        pass
```'''

# BUGGY (b): has a background reaper (so TTL is background) but FORGETS the
# lock -- the very common 7B "added a thread, missed the mutex" output. Fails
# the thread-safety test under active eviction churn.
_LRU_BUGGY_NO_LOCK_REAPER = '''```python
from __future__ import annotations
import threading
import time
from collections import OrderedDict


class TTLLRUCache:
    def __init__(self, capacity, ttl_seconds):
        self.capacity = int(capacity)
        self.ttl_seconds = float(ttl_seconds)
        self._data = OrderedDict()
        self._stop = False
        self._thread = threading.Thread(target=self._reaper, daemon=True)
        self._thread.start()

    def _reaper(self):
        while not self._stop:
            time.sleep(0.0005)
            now = time.monotonic()
            # NO LOCK + iterate the LIVE dict (no list() snapshot): a concurrent
            # put mutating mid-iteration raises "OrderedDict mutated during
            # iteration"; the unguarded pop also races put's popitem -> KeyError.
            for k, (_v, exp) in self._data.items():
                if exp <= now:
                    self._data.pop(k, None)

    def get(self, key):
        item = self._data.get(key)
        if item is None:
            return None
        value, exp = item
        if exp <= time.monotonic():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def put(self, key, value):
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = (value, time.monotonic() + self.ttl_seconds)
        while len(self._data) > self.capacity:
            self._data.popitem(last=False)

    def __len__(self):
        return len(self._data)

    def stop(self):
        self._stop = True
```'''


def test_concurrency_payload_registered_and_default():
    assert "concurrency_lru" in acs.PAYLOADS
    assert acs.DEFAULT_PAYLOAD == "concurrency_lru"
    p = acs.PAYLOADS["concurrency_lru"]
    assert p.entry_symbol == "TTLLRUCache"
    assert "def test_thread_safety_no_corruption" in p.tests
    assert "def test_ttl_background_eviction_not_lazy" in p.tests


def test_concurrency_gauntlet_correct_impl_passes():
    # LOAD-BEARING: a known-CORRECT reference impl PASSES the gauntlet suite.
    p = acs.PAYLOADS["concurrency_lru"]
    out = acs._run_pytest_in_tempdir(
        acs._extract_code_block(_LRU_CORRECT), p.tests, timeout_s=120, payload=p
    )
    assert out["passed"] is True, out["stdout"][-2000:] + "\n" + out["stderr"][-1000:]


def test_concurrency_gauntlet_no_lock_lazy_impl_fails():
    # LOAD-BEARING: the no-lock + lazy-only TTL bug is CAUGHT.
    p = acs.PAYLOADS["concurrency_lru"]
    out = acs._run_pytest_in_tempdir(
        acs._extract_code_block(_LRU_BUGGY_NO_LOCK_LAZY), p.tests, timeout_s=120, payload=p
    )
    assert out["passed"] is False
    blob = (out["stdout"] or "") + (out["stderr"] or "")
    # Both the thread-safety AND the background-eviction tests should flag it.
    assert "test_thread_safety_no_corruption" in blob or \
        "test_ttl_background_eviction_not_lazy" in blob


def test_concurrency_gauntlet_no_lock_reaper_impl_fails():
    # LOAD-BEARING: the "added a thread, missed the mutex" bug is CAUGHT.
    #
    # This race is inherently nondeterministic (the unguarded reaper/put
    # interleave depends on scheduling), so we give it a few attempts -- a
    # missing lock surfaces well within 3 runs, while a CORRECT (locked) impl
    # never fails ANY run (proven separately + 10/10 in development). We assert
    # the gauntlet flags the bug at least once AND that, when it does, it is the
    # thread-safety test that catches it.
    p = acs.PAYLOADS["concurrency_lru"]
    impl = acs._extract_code_block(_LRU_BUGGY_NO_LOCK_REAPER)
    caught = False
    for _ in range(3):
        out = acs._run_pytest_in_tempdir(impl, p.tests, timeout_s=120, payload=p)
        if not out["passed"]:
            blob = (out["stdout"] or "") + (out["stderr"] or "")
            assert "test_thread_safety_no_corruption" in blob
            caught = True
            break
    assert caught, "thread-safety gauntlet failed to catch the no-lock reaper bug in 3 runs"


def test_concurrency_loop_fires_repair_then_converges():
    # buggy -> buggy(same sig) -> buggy(same sig) -> correct: the epistemic
    # diff is injected, temperature decays, and it converges on the LRU payload.
    p = acs.PAYLOADS["concurrency_lru"]
    scripted = [
        _LRU_BUGGY_NO_LOCK_LAZY,
        _LRU_BUGGY_NO_LOCK_LAZY,
        _LRU_BUGGY_NO_LOCK_LAZY,
        _LRU_CORRECT,
    ]
    client = FakeLocalPrimeClient(scripted)
    result = asyncio.run(
        acs.run_cognitive_soak(client=client, max_repairs=3, payload=p)
    )
    assert result["converged"] is True
    assert result["epistemic_diffs_injected"] >= 1
    # diff injected after the first failure (later prompts carry the stderr).
    assert "FAILING TEST STDERR" not in client.calls[0]["prompt"]
    assert any("FAILING TEST STDERR" in c["prompt"] for c in client.calls[1:])
    temps = result["temperature_trajectory"]
    assert all(temps[i] >= temps[i + 1] for i in range(len(temps) - 1))


def test_concurrency_loop_pivots_and_decomposes(monkeypatch):
    # Same-signature failures long enough to trip pivot_verdict, then decompose
    # fires, then a correct impl converges -- the full UNRESOLVABLE-PATH ->
    # decompose -> retry arc on the LRU payload.
    called = {"n": 0, "goals": []}
    real_decompose = acs.decompose_for_block

    def _spy(goal, **kwargs):
        called["n"] += 1
        called["goals"].append(goal)
        return real_decompose(goal, **kwargs)

    monkeypatch.setattr(acs, "decompose_for_block", _spy)

    p = acs.PAYLOADS["concurrency_lru"]
    client = FakeLocalPrimeClient([_LRU_BUGGY_NO_LOCK_LAZY] * 6 + [_LRU_CORRECT])
    result = asyncio.run(
        acs.run_cognitive_soak(client=client, max_repairs=6, payload=p)
    )
    assert result["pivoted"] is True
    assert result["decomposed"] is True
    assert called["n"] >= 1
    # The decompose goal was built FROM the LRU payload (not merge-intervals).
    assert any(getattr(g, "goal_id", "").startswith("adv-soak-ttllrucache")
               for g in called["goals"])


def test_concurrency_loop_bounded_non_convergence():
    p = acs.PAYLOADS["concurrency_lru"]
    client = FakeLocalPrimeClient([_LRU_BUGGY_NO_LOCK_LAZY])  # never fixes it
    result = asyncio.run(
        acs.run_cognitive_soak(client=client, max_repairs=3, payload=p)
    )
    assert result["converged"] is False
    assert result["attempts"] <= 8  # bounded, no infinite loop


# ===========================================================================
# Defect 1: AST-sanitize generated code so it is importable as a pure module
# ===========================================================================
#
# The model appended module-level demo code (cache = TTLLRUCache(3, 2)) and
# omitted `import threading` -> importing impl.py raised NameError -> pytest
# COLLECTION ERROR -> all tests "collected 0, 1 error" -> repair loop sees
# zero failing test IDs every attempt.
#
# Fix: _sanitize_importable(src) strips top-level executable statements (bare
# calls, instantiations, print(...), if __name__ == "__main__" blocks) but
# keeps Import/ImportFrom/ClassDef/FunctionDef and Assign-of-constants nodes.
# Fail-soft: if AST parse fails, return the raw text so a real SyntaxError
# surfaces as a genuine (and discriminating) failure signature.


def test_sanitize_strips_module_level_demo_code():
    """Strips bare instantiation + print + if __name__ but keeps imports+class."""
    src = '''\
from __future__ import annotations
import threading
import time
from collections import OrderedDict


class TTLLRUCache:
    def __init__(self, capacity, ttl_seconds):
        self._lock = threading.RLock()
        self._data = OrderedDict()

    def get(self, key):
        return None

    def put(self, key, value):
        pass

    def __len__(self):
        return 0

    def stop(self):
        pass


# module-level demo -- the bug the live gauntlet exposed
cache = TTLLRUCache(3, 2)
cache.put("a", 1)
print("demo:", cache.get("a"))

if __name__ == "__main__":
    print("standalone")
'''
    sanitized = acs._sanitize_importable(src)
    # Structural nodes must survive.
    assert "import threading" in sanitized
    assert "class TTLLRUCache" in sanitized
    assert "def __init__" in sanitized
    # Executable demo nodes must be gone.
    assert "cache = TTLLRUCache" not in sanitized
    assert 'cache.put("a"' not in sanitized
    assert "print(" not in sanitized
    assert '__name__ == "__main__"' not in sanitized
    # The sanitized code must be importable as a pure module.
    import importlib, types, sys as _sys
    m = types.ModuleType("_test_sanitize_mod")
    exec(compile(sanitized, "<sanitized>", "exec"), m.__dict__)  # noqa: S102
    assert hasattr(m, "TTLLRUCache")


def test_sanitize_keeps_constant_assign():
    """Top-level constant assignments (str/int/tuple literals) are kept."""
    src = '''\
import os

_VERSION = "1.0.0"
_MAX = 100


def helper():
    return _VERSION
'''
    sanitized = acs._sanitize_importable(src)
    assert "_VERSION" in sanitized
    assert "_MAX" in sanitized
    assert "def helper" in sanitized


def test_sanitize_fail_soft_on_broken_syntax():
    """If AST parse fails (model emitted broken code), return the raw text unchanged."""
    src = "def broken(\n    pass  # mismatched paren\n"
    result = acs._sanitize_importable(src)
    # Must not raise; must return the original text so the real SyntaxError
    # surfaces as a genuine (discriminating) failure signature.
    assert result == src


def test_extract_then_sanitize_makes_code_importable():
    """Full pipeline: extract code block then sanitize -> importable module."""
    # This is the exact failure shape from the live gauntlet: the model's
    # response includes a code fence with demo code at the bottom AND omits
    # import threading, but the sanitizer strips the demo so the NameError
    # from missing threading appears at class-body-access-time (real failure),
    # NOT at module-level instantiation (accidental collection error).
    response_with_demo = '''\
Here is the implementation:

```python
import time
from collections import OrderedDict


class TTLLRUCache:
    """Thread-safe TTL LRU cache."""

    def __init__(self, capacity, ttl_seconds):
        self.capacity = capacity
        self._data = OrderedDict()

    def get(self, key):
        return self._data.get(key)

    def put(self, key, value):
        self._data[key] = value

    def __len__(self):
        return len(self._data)

    def stop(self):
        pass


# demo usage appended by the model
cache = TTLLRUCache(3, 2)
cache.put("x", 10)
print(cache.get("x"))
```

Hope that helps!
'''
    raw = acs._extract_code_block(response_with_demo)
    sanitized = acs._sanitize_importable(raw)
    # The demo lines are gone.
    assert "cache = TTLLRUCache" not in sanitized
    assert "print(" not in sanitized
    # The class is still there and importable.
    import types
    m = types.ModuleType("_test_extract_sanitize")
    exec(compile(sanitized, "<extracted>", "exec"), m.__dict__)  # noqa: S102
    assert hasattr(m, "TTLLRUCache")


# ===========================================================================
# Defect 2: Robust failure signature for collection/import errors
# ===========================================================================
#
# When pytest cannot collect tests (NameError / ImportError at module level),
# there are zero FAILED test IDs. The old _signature_for returned
# failure_signature_hash([], "test") = a constant for ALL collection errors,
# so repeated_signature_count incremented (good) BUT a NameError vs an
# ImportError vs an AssertionError all got the SAME signature (bad -- genuine
# progress from one error class to a different one looked like a repeat).
#
# Fix: _signature_for must:
#   (a) STABLE: same collection error -> same hash across attempts (so repeat
#       tracking drives temp decay + pivot),
#   (b) DISCRIMINATING: NameError vs AssertionError vs SyntaxError -> DIFFERENT
#       hashes (so genuine progress resets the decay),
#   (c) PATH-INVARIANT: the tempdir path (/tmp/adv_soak_xyz/) is stripped
#       from any text used to build the signature.


def _make_collection_error_out(error_line: str, tempdir: str = "/tmp/adv_soak_abc123") -> dict:
    """Synthesize a pytest output dict for a collection error."""
    stdout = (
        f"==================================== ERRORS ====================================\n"
        f"________________________ ERROR collecting test_impl.py _________________________\n"
        f"{tempdir}/test_impl.py:1: in <module>\n"
        f"    from impl import TTLLRUCache\n"
        f"{tempdir}/impl.py:8: in __init__\n"
        f"    self._lock = threading.RLock()\n"
        f"E   {error_line}\n"
        f"=========================== short test summary info ============================\n"
        f"ERROR {tempdir}/test_impl.py - {error_line}\n"
        f"!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!\n"
        f"1 error in 0.10s\n"
    )
    return {"passed": False, "stdout": stdout, "stderr": "", "returncode": 2}


def test_signature_stable_for_same_collection_error_across_attempts():
    """Same collection error -> same signature regardless of which attempt / tempdir."""
    out1 = _make_collection_error_out(
        "NameError: name 'threading' is not defined",
        tempdir="/tmp/adv_soak_attempt1_xyzabc",
    )
    out2 = _make_collection_error_out(
        "NameError: name 'threading' is not defined",
        tempdir="/tmp/adv_soak_attempt2_defghi",
    )
    sig1 = acs._signature_for(out1)
    sig2 = acs._signature_for(out2)
    assert sig1 == sig2, (
        "Same collection error must produce the same signature across attempts "
        f"(got {sig1!r} vs {sig2!r})"
    )


def test_signature_discriminates_nameerror_from_assertionerror():
    """A NameError collection error and an AssertionError test failure -> different sigs."""
    # Collection error (NameError): no test IDs in output
    col_out = _make_collection_error_out("NameError: name 'threading' is not defined")
    col_sig = acs._signature_for(col_out)

    # Assertion failure: FAILED test IDs present
    assert_out = {
        "passed": False,
        "stdout": (
            "FAILED test_impl.py::test_thread_safety_no_corruption - AssertionError\n"
            "1 failed in 1.23s\n"
        ),
        "stderr": "",
        "returncode": 1,
    }
    assert_sig = acs._signature_for(assert_out)

    assert col_sig != assert_sig, (
        "NameError collection error and AssertionError test failure must have "
        f"different signatures (both got {col_sig!r})"
    )


def test_signature_discriminates_different_collection_errors():
    """NameError vs ImportError collection errors -> different signatures."""
    name_err_out = _make_collection_error_out("NameError: name 'threading' is not defined")
    import_err_out = _make_collection_error_out("ImportError: cannot import name 'TTLLRUCache'")
    sig1 = acs._signature_for(name_err_out)
    sig2 = acs._signature_for(import_err_out)
    assert sig1 != sig2, (
        f"Different collection errors must have different sigs: {sig1!r} vs {sig2!r}"
    )


def test_signature_invariant_to_tempdir_path():
    """The tempdir path component (/tmp/adv_soak_<random>/) must not affect the sig."""
    out_a = _make_collection_error_out(
        "NameError: name 'threading' is not defined",
        tempdir="/tmp/adv_soak_0000001",
    )
    out_b = _make_collection_error_out(
        "NameError: name 'threading' is not defined",
        tempdir="/tmp/adv_soak_9999999",
    )
    assert acs._signature_for(out_a) == acs._signature_for(out_b)


def test_syntax_error_output_is_discriminated():
    """A SyntaxError (from [SOVEREIGN SYNTAX FATAL]) differs from a NameError sig."""
    syntax_out = {
        "passed": False,
        "stdout": "",
        "stderr": "[SOVEREIGN SYNTAX FATAL] line=5 msg=invalid syntax",
        "returncode": 1,
    }
    name_out = _make_collection_error_out("NameError: name 'threading' is not defined")
    assert acs._signature_for(syntax_out) != acs._signature_for(name_out)


# ===========================================================================
# Defect 3: Pivot reachability -- the soak must reach temp_at_floor
# ===========================================================================
#
# With JARVIS_EPISTEMIC_TEMP_FLOOR=0.0 (production default), temperature_for_attempt
# halves forever (0.7 -> 0.35 -> 0.175 -> 0.0875 ...) and NEVER stabilizes.
# temp_at_floor is NEVER True so pivot_verdict NEVER fires, regardless of budget.
#
# Fix: run_cognitive_soak sets JARVIS_EPISTEMIC_TEMP_FLOOR to a non-zero
# default (0.1) if the caller has not already set it, so the temperature
# schedule DOES stabilize. The recommended --max-repairs is 5 (so the loop
# survives until count=3 where pivot_verdict fires with floor=0.1).
# The REAL pivot_verdict logic is unchanged.


def test_pivot_fires_with_production_env_and_recommended_max_repairs(monkeypatch):
    """The pivot CAN fire using the soak's own env defaults (no test fixture help).

    This test deliberately removes the fixture's JARVIS_EPISTEMIC_TEMP_FLOOR
    override and verifies that the soak's own default (0.1 floor) still lets
    pivot_verdict trip within the recommended --max-repairs=5 budget.
    """
    # Remove the fixture's floor env var so we test the SOAK's own default.
    monkeypatch.delenv("JARVIS_EPISTEMIC_TEMP_FLOOR", raising=False)
    monkeypatch.delenv("JARVIS_EPISTEMIC_TEMP_DECAY", raising=False)
    monkeypatch.delenv("JARVIS_EPISTEMIC_PIVOT_PASSES", raising=False)

    called = {"n": 0}
    real_decompose = acs.decompose_for_block

    def _spy(goal, **kwargs):
        called["n"] += 1
        return real_decompose(goal, **kwargs)

    monkeypatch.setattr(acs, "decompose_for_block", _spy)

    p = acs.PAYLOADS["merge_intervals"]
    # Script: always the SAME wrong impl (same failure signature) -> pivot must trip.
    client = FakeLocalPrimeClient([_WRONG_SAME] * 8 + [_CORRECT])
    result = asyncio.run(
        acs.run_cognitive_soak(client=client, max_repairs=5, payload=p)
    )
    assert result["pivoted"] is True, (
        "pivot must fire within max_repairs=5 when the soak sets its own "
        "JARVIS_EPISTEMIC_TEMP_FLOOR default. "
        f"temperature_trajectory={result['temperature_trajectory']}"
    )
    assert called["n"] >= 1


def test_pivot_full_arc_collection_error_same_signature(monkeypatch):
    """Full fail->repair->pivot->decompose arc triggered by a COLLECTION ERROR.

    Scripts the fake client to emit impl-with-demo (causes collection error,
    same signature every time) N times, then a correct impl.  With the soak's
    own floor default and --max-repairs=5, the harness must:
      * detect the repeated signature,
      * decay temperature,
      * fire pivot (decompose_for_block called),
      * converge on the correct impl.
    """
    monkeypatch.delenv("JARVIS_EPISTEMIC_TEMP_FLOOR", raising=False)
    monkeypatch.delenv("JARVIS_EPISTEMIC_TEMP_DECAY", raising=False)
    monkeypatch.delenv("JARVIS_EPISTEMIC_PIVOT_PASSES", raising=False)

    called = {"n": 0}
    real_decompose = acs.decompose_for_block

    def _spy(goal, **kwargs):
        called["n"] += 1
        return real_decompose(goal, **kwargs)

    monkeypatch.setattr(acs, "decompose_for_block", _spy)

    # An impl that imports threading BUT uses it at module level (so if the
    # model forgets threading the NameError triggers on import -> collection err).
    # We simulate this by using the wrong merge_intervals impl (same sig).
    #
    # With max_repairs=5: total_budget starts at 7. Pivot fires at attempt 5
    # (count=4, temp=floor=0.1) -> budget becomes 8. Attempts 6, 7, 8 use
    # scripted indices 5, 6, 7. So [_WRONG_SAME] * 7 + [_CORRECT] places the
    # correct impl exactly at the 8th call (index 7 -> budget just reached).
    p = acs.PAYLOADS["merge_intervals"]
    client = FakeLocalPrimeClient([_WRONG_SAME] * 7 + [_CORRECT])
    result = asyncio.run(
        acs.run_cognitive_soak(client=client, max_repairs=5, payload=p)
    )
    assert result["pivoted"] is True
    assert result["converged"] is True
    assert called["n"] >= 1
    # Temperature must have decayed.
    traj = result["temperature_trajectory"]
    assert min(traj) < max(traj)


# ---------------------------------------------------------------------------
# CLI: --payload selector + warmup-first
# ---------------------------------------------------------------------------


def test_payload_selector_dry_mode(monkeypatch, capsys):
    monkeypatch.setenv("JARVIS_CHAOS_INJECTOR_ENABLED", "true")
    rc = acs.main(["--payload", "merge_intervals"])  # dry mode (no --run)
    assert rc == 0
    out = capsys.readouterr().out
    assert "merge_intervals" in out
    assert acs.MERGE_INTERVALS_PAYLOAD.title in out


def test_payload_selector_defaults_to_concurrency(monkeypatch, capsys):
    monkeypatch.setenv("JARVIS_CHAOS_INJECTOR_ENABLED", "true")
    rc = acs.main([])  # no --payload -> default
    assert rc == 0
    out = capsys.readouterr().out
    assert "concurrency_lru" in out


def test_warmup_called_once_before_loop(monkeypatch):
    # In the --run path, the harness must warm the model exactly once BEFORE
    # the cognitive loop starts (eliminates the cold-start timeout).
    monkeypatch.setenv("JARVIS_CHAOS_INJECTOR_ENABLED", "true")

    events = []

    class _WarmupFake(FakeLocalPrimeClient):
        def __init__(self, scripted):
            super().__init__(scripted)
            self.warmup_calls = []

        async def warmup(self, *, timeout_s):
            self.warmup_calls.append(timeout_s)
            events.append("warmup")
            return True

        async def generate(self, *a, **k):
            events.append("generate")
            return await super().generate(*a, **k)

        async def aclose(self):
            return None

    fake = _WarmupFake([_LRU_CORRECT])
    monkeypatch.setattr(acs, "_build_real_client", lambda model: fake)

    args = acs.argparse.Namespace(
        run=True, model="qwen2.5-coder:7b", max_repairs=3,
        payload="concurrency_lru", warmup_timeout=5.0,
    )
    rc = asyncio.run(acs._amain(args))
    assert rc == 0  # converged on the correct impl
    assert fake.warmup_calls == [5.0]  # warmed exactly once, with the timeout
    # Warmup happened BEFORE any generation.
    assert events[0] == "warmup"
    assert "generate" in events
    assert events.index("warmup") < events.index("generate")


# ===========================================================================
# Payload #3: the Architectural Anomaly (Paxos-style leader election)
# ===========================================================================
#
# The FINAL flip-gate. The Paxos payload is past a 7B's single-context reach,
# so a model reliably THRASHES -> burns the repair budget -> fires
# should_pivot(budget_exhausted) -> [SOVEREIGN YIELD: UNRESOLVABLE PATH] ->
# decompose_for_block SEMANTICALLY chops it by SYMBOL -> retry against the
# first (failure-implicated) chunk. The gate is the GRACEFUL handling of that
# arc (pivot AND decompose AND retry AND no-lockup AND clean termination), NOT
# whether Paxos ultimately passes.
#
# We prove three things with the REAL pytest subprocess boundary + REAL
# primitives (should_pivot / decompose_for_block / isolate_symbols), no Ollama:
#   (a) the embedded CORRECT reference PaxosNode PASSES the suite + a known-
#       thrashy multi-symbol stub FAILS it (the suite discriminates + is fair);
#   (b) a FakeLocalPrimeClient that NEVER converges (always a thrashy multi-
#       symbol PaxosNode) -> pivot(budget_exhausted) -> decompose_for_block IS
#       called with target_files pointing at the WRITTEN impl -> the emitted
#       sub-goals' scoped symbols are LOGGED -> a retry against the first chunk
#       happens -> the loop terminates cleanly with
#       pivot_and_decompose_handled_gracefully=True and no lockup;
#   (c) the scoped-symbol logging shows >1 distinct symbol separated (the
#       semantic seam) when the generated impl has distinct symbols.

# A CORRECT reference PaxosNode + in-process transport (no sockets). Passes the
# fair, deterministic gauntlet suite.
_PAXOS_CORRECT = '''```python
from __future__ import annotations
import threading


class InProcTransport:
    """In-process message bus shared by PaxosNodes. No sockets."""

    def __init__(self):
        self._nodes = {}
        self._lock = threading.RLock()
        self._partition = set()

    def register(self, node):
        with self._lock:
            self._nodes[node.node_id] = node

    def partition(self, a, b):
        with self._lock:
            self._partition.add(frozenset((a, b)))

    def heal(self):
        with self._lock:
            self._partition.clear()

    def _blocked(self, src, dst):
        return frozenset((src, dst)) in self._partition

    def send(self, src, dst, msg):
        with self._lock:
            if self._blocked(src, dst):
                return None
            node = self._nodes.get(dst)
        if node is None:
            return None
        return node.deliver(src, msg)

    def peers(self, node_id):
        with self._lock:
            return [n for n in self._nodes if n != node_id]


class PaxosNode:
    def __init__(self, node_id, transport):
        self.node_id = node_id
        self._transport = transport
        self._lock = threading.RLock()
        self._term = 0
        self._leader_of_term = {}
        self._is_leader = False
        self._stop = threading.Event()
        transport.register(self)

    def advance_term(self, new_term=None):
        with self._lock:
            if new_term is None:
                new_term = self._term + 1
            if new_term < self._term:
                return self._term
            self._term = new_term
            self._is_leader = False
            return self._term

    @property
    def term(self):
        with self._lock:
            return self._term

    @property
    def is_leader(self):
        with self._lock:
            return self._is_leader

    def on_vote_request(self, candidate_id, term):
        with self._lock:
            if term < self._term:
                return (False, self._term)
            if term > self._term:
                self._term = term
                self._is_leader = False
            voted = self._leader_of_term.get(term)
            if voted is None or voted == candidate_id:
                self._leader_of_term[term] = candidate_id
                return (True, self._term)
            return (False, self._term)

    def request_election(self):
        with self._lock:
            self.advance_term()
            term = self._term
            self._leader_of_term[term] = self.node_id
            votes = 1
        peers = self._transport.peers(self.node_id)
        for p in peers:
            resp = self._transport.send(self.node_id, p, ("vote", term))
            if resp and resp[0]:
                votes += 1
        total = len(peers) + 1
        with self._lock:
            if votes * 2 > total and self._term == term:
                self._is_leader = True
                self._leader_of_term[term] = self.node_id
                return True
            return False

    def resolve_partition(self, observed_term):
        with self._lock:
            if observed_term > self._term:
                self._term = observed_term
                self._is_leader = False
                return True
            return False

    def deliver(self, src, msg):
        kind = msg[0]
        if kind == "vote":
            return self.on_vote_request(src, msg[1])
        if kind == "heartbeat":
            self.resolve_partition(msg[1])
            return ("ack", self.term)
        return None

    async def heartbeat_loop(self, interval=0.01, rounds=1):
        import asyncio
        for _ in range(rounds):
            if self._stop.is_set():
                break
            with self._lock:
                term = self._term
                leader = self._is_leader
            if leader:
                for p in self._transport.peers(self.node_id):
                    self._transport.send(self.node_id, p, ("heartbeat", term))
            await asyncio.sleep(interval)

    def stop(self):
        self._stop.set()
```'''

# A THRASHY stub: structurally complete (all the DISTINCT symbols the goal
# names, so isolate_symbols finds real seams) but SEMANTICALLY broken on every
# axis the suite checks (non-monotonic term, quorum-free leader, no brain-split
# step-down, BLOCKING non-async heartbeat). This is the shape a 7B ships when it
# cannot hold the full state machine -- it FAILS the gauntlet.
_PAXOS_THRASH = '''```python
from __future__ import annotations


class InProcTransport:
    def __init__(self):
        pass

    def register(self, node):
        pass

    def partition(self, a, b):
        pass

    def heal(self):
        pass

    def peers(self, node_id):
        return []

    def send(self, src, dst, msg):
        return None


class PaxosNode:
    def __init__(self, node_id, transport):
        self.node_id = node_id
        self._term = 0
        self._is_leader = False

    def advance_term(self, new_term=None):
        # BUG: not monotonic -- accepts a lower term.
        self._term = new_term if new_term is not None else self._term + 1
        return self._term

    @property
    def term(self):
        return self._term

    @property
    def is_leader(self):
        return self._is_leader

    def on_vote_request(self, candidate_id, term):
        # BUG: always grants -> multiple leaders per term.
        return (True, term)

    def request_election(self):
        # BUG: no quorum -- claims leadership unconditionally.
        self._is_leader = True
        return True

    def resolve_partition(self, observed_term):
        # BUG: never steps down.
        return False

    def heartbeat_loop(self, interval=0.01, rounds=1):
        # BUG: NOT async + blocking.
        import time
        time.sleep(interval * rounds)

    def stop(self):
        pass
```'''


# Thrash VARIANTS that each FAIL a DIFFERENT subset of the suite, so the
# failure SIGNATURE (the sorted set of failing test IDs) differs across
# attempts (never repeats consecutively) -> the legacy stuck-signature pivot
# can NOT trip; only the budget-exhaustion thrash backstop in should_pivot can.
# Each variant keeps the DISTINCT symbols isolate_symbols needs for a real
# semantic seam, and survives _sanitize_importable (pure definitions only).
#
# We perturb which axes are CORRECT so the failing-test-ID set differs:
#   Variant B: monotonic term is CORRECT -> only the OTHER 3 tests fail.
#   Variant C: monotonic term AND async heartbeat are CORRECT -> only 2 fail.
# (_PAXOS_THRASH fails all 4.) Three distinct failing-ID sets -> three distinct
# signatures, none repeating back-to-back across the rotation.

# Variant B: advance_term is correctly monotonic; the rest stay broken.
_PAXOS_THRASH_MONOTONIC_OK = '''```python
from __future__ import annotations


class InProcTransport:
    def register(self, node): pass
    def partition(self, a, b): pass
    def heal(self): pass
    def peers(self, node_id): return []
    def send(self, src, dst, msg): return None


class PaxosNode:
    def __init__(self, node_id, transport):
        self.node_id = node_id
        self._term = 0
        self._is_leader = False

    def advance_term(self, new_term=None):
        # CORRECT: monotonic.
        if new_term is None:
            new_term = self._term + 1
        if new_term < self._term:
            return self._term
        self._term = new_term
        return self._term

    @property
    def term(self):
        return self._term

    @property
    def is_leader(self):
        return self._is_leader

    def on_vote_request(self, candidate_id, term):
        return (True, term)  # BUG: always grants

    def request_election(self):
        self._is_leader = True  # BUG: no quorum
        return True

    def resolve_partition(self, observed_term):
        return False  # BUG: never steps down

    def heartbeat_loop(self, interval=0.01, rounds=1):
        import time
        time.sleep(interval * rounds)  # BUG: blocking, not async

    def stop(self):
        pass
```'''

# Variant C: advance_term monotonic AND heartbeat_loop async non-blocking
# correct; only the leader/partition axes stay broken (fewer failing tests).
_PAXOS_THRASH_MONOTONIC_HB_OK = '''```python
from __future__ import annotations


class InProcTransport:
    def register(self, node): pass
    def partition(self, a, b): pass
    def heal(self): pass
    def peers(self, node_id): return []
    def send(self, src, dst, msg): return None


class PaxosNode:
    def __init__(self, node_id, transport):
        self.node_id = node_id
        self._term = 0
        self._is_leader = False

    def advance_term(self, new_term=None):
        if new_term is None:
            new_term = self._term + 1
        if new_term < self._term:
            return self._term
        self._term = new_term
        return self._term

    @property
    def term(self):
        return self._term

    @property
    def is_leader(self):
        return self._is_leader

    def on_vote_request(self, candidate_id, term):
        return (True, term)  # BUG: always grants

    def request_election(self):
        self._is_leader = True  # BUG: no quorum
        return True

    def resolve_partition(self, observed_term):
        return False  # BUG: never steps down

    async def heartbeat_loop(self, interval=0.01, rounds=1):
        # CORRECT: async, non-blocking, bounded rounds.
        import asyncio
        for _ in range(rounds):
            await asyncio.sleep(interval)

    def stop(self):
        pass
```'''


def test_paxos_payload_registered():
    assert "paxos_election" in acs.PAYLOADS
    p = acs.PAYLOADS["paxos_election"]
    assert p.entry_symbol == "PaxosNode"
    # The DISTINCT symbols are named in the description so isolate_symbols
    # scopes them (the semantic seams).
    for sym in ("heartbeat_loop", "advance_term", "resolve_partition",
                "on_vote_request"):
        assert sym in p.description, sym
    assert "def test_monotonic_term_never_decreases" in p.tests
    assert "def test_partition_no_double_leader_same_term" in p.tests


def test_paxos_correct_reference_passes_suite():
    # LOAD-BEARING: the suite is FAIR -- a correct impl PASSES it.
    p = acs.PAYLOADS["paxos_election"]
    out = acs._run_pytest_in_tempdir(
        acs._extract_code_block(_PAXOS_CORRECT), p.tests, timeout_s=120, payload=p
    )
    assert out["passed"] is True, out["stdout"][-2000:] + "\n" + out["stderr"][-1000:]


def test_paxos_thrash_stub_fails_suite():
    # LOAD-BEARING: the suite DISCRIMINATES -- a thrashy stub FAILS it.
    p = acs.PAYLOADS["paxos_election"]
    out = acs._run_pytest_in_tempdir(
        acs._extract_code_block(_PAXOS_THRASH), p.tests, timeout_s=120, payload=p
    )
    assert out["passed"] is False
    blob = (out["stdout"] or "") + (out["stderr"] or "")
    # At least the monotonic-term and one-leader checks should flag it.
    assert "test_monotonic_term_never_decreases" in blob or \
        "test_exactly_one_leader_per_term" in blob


def test_paxos_thrash_stub_has_distinct_symbols_for_decompose():
    # The thrash stub has the DISTINCT symbols isolate_symbols needs for a
    # REAL semantic seam (this is what (c) below depends on).
    import tempfile as _tf
    import os as _os
    from backend.core.ouroboros.governance.ast_symbol_scoper import isolate_symbols
    p = acs.PAYLOADS["paxos_election"]
    src = acs._sanitize_importable(acs._extract_code_block(_PAXOS_THRASH))
    with _tf.TemporaryDirectory() as d:
        path = _os.path.join(d, "impl.py")
        with open(path, "w", encoding="ascii", errors="replace") as f:
            f.write(src)
        targets = isolate_symbols(path, p.description + p.decompose_focus)
        syms = sorted({t.symbol for t in targets if t.symbol})
    # Multiple DISTINCT scoped symbols -> a real semantic seam.
    assert len(syms) > 1, syms
    assert any("heartbeat_loop" in s for s in syms)
    assert any("advance_term" in s for s in syms)


def test_paxos_never_converges_pivots_decomposes_retries_gracefully(monkeypatch):
    """THE FLIP-GATE: a never-converging thrashy 7B drives the full arc.

    pivot(budget_exhausted) -> decompose_for_block called with target_files at
    the WRITTEN impl -> scoped symbols logged -> retry against the first chunk
    -> clean termination with pivot_and_decompose_handled_gracefully=True and
    no lockup. Paxos NEVER passes here (bonus not required).
    """
    monkeypatch.setenv("JARVIS_EPISTEMIC_THRASH_PIVOT_ENABLED", "true")

    captured = {"goals": [], "n": 0}
    real_decompose = acs.decompose_for_block

    def _spy(goal, **kwargs):
        captured["n"] += 1
        captured["goals"].append(goal)
        return real_decompose(goal, **kwargs)

    monkeypatch.setattr(acs, "decompose_for_block", _spy)

    p = acs.PAYLOADS["paxos_election"]
    # A DIFFERENT thrashy failure each attempt (never-repeating signature) ->
    # the legacy stuck-signature pivot can NOT trip; only the budget-exhaustion
    # thrash backstop can. The trailing constant thrash covers any post-pivot
    # extra GENERATE (still fails -> Paxos never converges, by design).
    client = FakeLocalPrimeClient([
        _PAXOS_THRASH,                  # all 4 tests fail (sig X)
        _PAXOS_THRASH_MONOTONIC_OK,     # 3 fail (sig Y != X)
        _PAXOS_THRASH_MONOTONIC_HB_OK,  # 2 fail (sig Z != Y)
        _PAXOS_THRASH,                  # back to 4 (sig X != Z)
        _PAXOS_THRASH_MONOTONIC_OK,     # 3 fail (sig Y != X)
        _PAXOS_THRASH_MONOTONIC_HB_OK,  # post-pivot retry padding
        _PAXOS_THRASH,
    ])
    result = asyncio.run(
        acs.run_cognitive_soak(client=client, max_repairs=5, payload=p)
    )

    # The signatures must NOT all be identical (genuine thrash, not a stuck wall).
    sigs = result["signatures"]
    assert len(set(sigs)) > 1, f"expected DIFFERENT signatures (thrash), got {sigs!r}"

    # The arc fired -- specifically on the budget-exhaustion thrash backstop.
    assert result["pivoted"] is True
    assert result["pivot_reason"] == "budget_exhausted", result["pivot_reason"]
    assert result["decomposed"] is True
    assert captured["n"] >= 1, "decompose_for_block must be exercised"

    # target_files pointed at the WRITTEN impl on disk (so symbol-scoping is
    # REAL, not a whole-file fallback on a non-existent bare filename).
    goal = captured["goals"][0]
    tf = list(getattr(goal, "target_files", ()) or ())
    assert len(tf) == 1
    assert tf[0].endswith("impl.py")
    assert os.path.isabs(tf[0]) or "adv_soak_decompose_" in tf[0], tf
    # It carries the goal id derived from the Paxos payload.
    assert getattr(goal, "goal_id", "").startswith("adv-soak-paxosnode")

    # Retry happened + handled gracefully + no lockup + bounded.
    assert result["retried_against_chunk"] is True
    assert result["lockup"] is False
    assert result["pivot_and_decompose_handled_gracefully"] is True
    assert result["converged"] is False  # Paxos never passes here (by design)
    # Bounded, no infinite loop: initial GENERATE + max_repairs + the initial
    # +1 reserve + the one pivot-granted extra GENERATE.
    assert result["attempts"] <= 1 + 5 + 2


def test_paxos_decompose_logs_distinct_scoped_symbols(monkeypatch):
    """(c): the scoped-symbol seams show >1 DISTINCT symbol separated."""
    monkeypatch.setenv("JARVIS_EPISTEMIC_THRASH_PIVOT_ENABLED", "true")
    p = acs.PAYLOADS["paxos_election"]
    client = FakeLocalPrimeClient([_PAXOS_THRASH])
    result = asyncio.run(
        acs.run_cognitive_soak(client=client, max_repairs=5, payload=p)
    )
    assert result["pivoted"] is True
    assert result["decomposed"] is True
    # The harness recorded the scoped symbols per emitted sub-goal.
    scoped = result["decomposed_scoped_symbols"]
    assert scoped, "scoped symbols must be recorded for the operator to watch"
    distinct = sorted({s for syms in scoped for s in syms})
    # >1 distinct symbol -> a REAL semantic seam (the gen impl had distinct
    # symbols). e.g. heartbeat_loop separated from advance_term.
    assert len(distinct) > 1, distinct
    assert any("heartbeat_loop" in s for s in distinct), distinct
    assert any("advance_term" in s for s in distinct), distinct


def test_paxos_dry_mode_selector(monkeypatch, capsys):
    monkeypatch.setenv("JARVIS_CHAOS_INJECTOR_ENABLED", "true")
    rc = acs.main(["--payload", "paxos_election"])  # dry mode
    assert rc == 0
    out = capsys.readouterr().out
    assert "paxos_election" in out
    assert acs.PAXOS_ELECTION_PAYLOAD.title in out
