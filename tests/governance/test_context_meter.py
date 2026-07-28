"""The context meter — two walls, measured tokens, and honest ignorance.

CC shows `Context left until auto-compact: 23%`. O+V had the ingredients and
never assembled them, and the piece it did surface measured a CHARACTER budget
this process enforces on itself while calling it context. The provider's TOKEN
window — the wall that refuses the request rather than summarising it — was
never compared against at all.

The tests that matter here are the ones where being wrong looks like being
right: a fabricated window, an assumed char/token ratio, a zeroed reading that
means "unmeasured".
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import context_meter as cm
from backend.core.ouroboros.governance import tool_executor as te


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    cm.reset_for_tests()
    monkeypatch.delenv("JARVIS_CONTEXT_WINDOW_TOKENS", raising=False)
    yield
    cm.reset_for_tests()


def _calibrate(provider="doubleword", ratio=3.2, n=6):
    """Feed n real (chars, tokens) pairs at a known ratio."""
    for i in range(n):
        cm.note_prompt_chars(f"cal-{i}", int(32000))
        cm.note_prompt_tokens(f"cal-{i}", provider, int(32000 / ratio))


class TestTheRatioIsMeasuredNotAssumed:
    def test_the_prior_holds_until_there_is_evidence(self):
        """Two samples can agree by luck. The configured value is a PRIOR,
        and it stays until enough real pairs outvote it."""
        ratio, prov = cm.chars_per_token("doubleword")
        assert prov == "configured"
        _calibrate(n=cm.min_ratio_samples() - 1)
        assert cm.chars_per_token("doubleword")[1] == "configured"

    def test_real_pairs_take_over_and_say_so(self):
        _calibrate(ratio=3.2)
        ratio, prov = cm.chars_per_token("doubleword")
        assert prov.startswith("observed:")
        assert 3.0 < ratio < 3.4

    def test_the_difference_is_worth_tens_of_thousands_of_tokens(self):
        """3.2 vs the 4.0 prose default over a 131k budget. This is the whole
        reason the ratio is measured rather than assumed."""
        _calibrate(ratio=3.2)
        measured, _ = cm.chars_per_token("doubleword")
        assert int(131072 / measured) - int(131072 / 4.0) > 8000

    def test_an_orphan_token_count_is_discarded(self):
        """A token count with no matching char count must not be attributed
        to whatever op was measured last — a mis-paired sample poisons the
        ratio in a way nothing downstream could detect."""
        _calibrate(ratio=3.2)
        before = cm.chars_per_token("doubleword")
        cm.note_prompt_tokens("never-measured", "doubleword", 9999)
        assert cm.chars_per_token("doubleword") == before

    def test_an_absurd_ratio_is_refused(self):
        """1000 chars per token is not content, it is a bug — a truncated
        prompt, a cached-token response reporting a fraction of what was
        sent. Folding it in would move the estimate for every later op."""
        _calibrate(ratio=3.2)
        before = cm.chars_per_token("doubleword")
        cm.note_prompt_chars("bad", 1000)
        cm.note_prompt_tokens("bad", "doubleword", 1)
        assert cm.chars_per_token("doubleword") == before

    def test_ratios_are_per_provider(self):
        """Two models tokenise differently. One blended number would be wrong
        for both."""
        _calibrate(provider="doubleword", ratio=3.2)
        assert cm.chars_per_token("claude")[1] == "configured"


class TestUnknownIsAnAnswer:
    def test_an_unmatched_model_does_NOT_inherit_a_strangers_window(self):
        """THE regression, found by running the meter.

        The policy file declares 4,096 tokens for a local 1B llama. Falling
        back to the smallest declared window told an op running against a
        remote 397B that it had 0% of its window left, and named `window` as
        the binding wall. A confident alarm about a limit nobody measured is
        worse than silence, because silence does not get acted on.
        """
        window, prov = cm.window_tokens("doubleword", "a-model-not-in-the-file")
        assert window is None and prov == "unknown"

    def test_an_unknown_window_leaves_the_MEASURED_wall_speaking(self):
        te.note_context_utilisation("op-x", 92000)
        reading = cm.read("op-x", provider="doubleword", model="nope")
        assert reading is not None
        assert reading.binding == cm.BINDING_COMPACTION
        assert reading.window_tokens is None
        assert "compact" in cm.render(reading)

    def test_a_named_model_resolves_to_its_own_declared_window(self):
        window, prov = cm.window_tokens("gcp_prime", "qwen-2.5-coder-32b")
        assert window == 16384 and prov == "policy_yaml"

    def test_the_operator_outranks_every_inference(self):
        import os
        os.environ["JARVIS_CONTEXT_WINDOW_TOKENS"] = "200000"
        try:
            assert cm.window_tokens("any", "thing") == (200000, "env")
        finally:
            del os.environ["JARVIS_CONTEXT_WINDOW_TOKENS"]

    def test_unmeasured_is_None_not_a_zeroed_reading(self):
        """"This op has used no context" and "we never measured this op" are
        different facts, and a caller that cannot tell them apart renders the
        first when it means the second."""
        assert cm.read("an-op-nobody-ran") is None


class TestWhichWallBinds:
    def test_the_nearer_wall_wins_and_is_NAMED(self, monkeypatch):
        """The two call for different actions: compaction means earlier
        rounds are about to be summarised away; the window means the request
        itself is about to be refused."""
        # Both cases held above the warn fraction on purpose: this test is
        # about WHICH wall binds, and visibility has its own class. Mixing
        # them is what made the first draft of it fail against correct code.
        _calibrate(ratio=4.0)
        te.note_context_utilisation("op-w", 70000)      # ~71% of compaction

        monkeypatch.setenv("JARVIS_CONTEXT_WINDOW_TOKENS", "20000")
        reading = cm.read("op-w", provider="doubleword")
        assert reading.binding == cm.BINDING_WINDOW
        assert "window" in cm.render(reading)

        monkeypatch.setenv("JARVIS_CONTEXT_WINDOW_TOKENS", "500000")
        reading = cm.read("op-w", provider="doubleword")
        assert reading.binding == cm.BINDING_COMPACTION
        assert "compact" in cm.render(reading)

    def test_compaction_counts_down_to_the_EVENT_not_the_ceiling(self):
        """Compaction is what the operator experiences, so it is what the
        meter counts down to — a percentage of the hard ceiling would read
        as roomy at the exact moment rounds start being summarised."""
        te.note_context_utilisation("op-c", int(0.75 * 131072))
        reading = cm.read("op-c")
        assert reading.compaction_pct >= 0.99


class TestItStaysQuietUntilItMatters:
    def test_below_the_warn_fraction_it_renders_nothing(self):
        te.note_context_utilisation("op-quiet", 1000)
        reading = cm.read("op-quiet")
        assert reading is not None and not reading.worth_showing
        assert cm.render(reading) == ""

    def test_the_threshold_is_a_knob_not_a_literal(self, monkeypatch):
        te.note_context_utilisation("op-knob", 30000)
        monkeypatch.setenv("JARVIS_CONTEXT_WARN_FRACTION", "0.01")
        assert cm.render(cm.read("op-knob")) != ""
        monkeypatch.setenv("JARVIS_CONTEXT_WARN_FRACTION", "0.99")
        assert cm.render(cm.read("op-knob")) == ""


class TestItCrossesTheBridge:
    def test_one_renderer_serves_both_processes(self):
        """A remote cockpit holds a dict, not a dataclass. Rehydrating it
        here is the lesson the agent roster paid for."""
        import json
        te.note_context_utilisation("op-b", 100000)
        payload = cm.as_payload(cm.read("op-b", provider="doubleword"))
        wire = json.loads(json.dumps(payload))
        assert cm.render_payload(wire) == cm.render(cm.read(
            "op-b", provider="doubleword"))

    def test_the_heartbeat_carries_it(self):
        import inspect
        from backend.core.ouroboros.battle_test import attach_heartbeat as hb
        src = inspect.getsource(hb.build_heartbeat_payload)
        assert '"context"' in src

    def test_a_missing_reading_renders_nothing_anywhere(self):
        assert cm.render_payload(None) == ""
        assert cm.render_payload("junk") == ""   # type: ignore[arg-type]
        assert cm.render(None) == ""


class TestNeverRaises:
    @pytest.mark.parametrize("call", [
        lambda: cm.note_prompt_chars(None, None),      # type: ignore[arg-type]
        lambda: cm.note_prompt_tokens(None, None, None),  # type: ignore
        lambda: cm.chars_per_token(None),              # type: ignore[arg-type]
        lambda: cm.window_tokens(None, None),          # type: ignore[arg-type]
        lambda: cm.read(None),                         # type: ignore[arg-type]
        lambda: cm.as_payload(None),
    ])
    def test_junk_degrades(self, call):
        call()

    def test_the_master_flag_silences_it(self, monkeypatch):
        te.note_context_utilisation("op-off", 100000)
        monkeypatch.setenv("JARVIS_CONTEXT_METER_ENABLED", "0")
        assert cm.read("op-off") is None
