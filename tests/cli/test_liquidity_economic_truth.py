"""``liquidity anthropic: 5,000,000 tokens`` — while every request 400'd.

Soak bt-2026-08-01-015739 ran 20.7 hours and produced 23 operations, **0
completed, $0.00 spent**. Both providers were out of credit:

    DoubleWord  402 "Account balance too low. Please add credits to continue."
    Anthropic   400 "Your credit balance is too low to access the Anthropic API."

(The Anthropic line was confirmed by probing the API directly with the same
key the soak used — both configured model ids return it.)

The control flow handled this correctly. Slice 127 reclassified the 400 to
``terminal_quota`` rather than sticky config-error, the Claude economic
breaker opened, and Slice 238 suppressed further attempts as a "known-dead
lane" so ops degraded cleanly instead of hammering a provider that could not
pay. A latched breaker for 20 hours was the RIGHT behaviour.

What failed was telling anyone. The cockpit banner read
``liquidity anthropic: 5,000,000 tokens`` throughout, because the liquidity
ledger records ``anthropic-ratelimit-tokens-remaining`` — a per-period bucket
that REFILLS — and an account with no credit has a full bucket it cannot spend
a token of.

**Two different exhaustion axes, one display.** Maximum health and total
inability to spend rendered identically, and the operator had no way to tell
them apart. `_liquidity_lines`' own docstring already worried that
"``5,000,000 tokens`` beside 'a runway is dry' reads as a contradiction" — this
is that contradiction, with the dry half missing.

The state existed the whole time: `claude_circuit_breaker.snapshot()` carried
``consecutive_economic_failures``, one import away from the harness function
that builds the banner's payload. Computed, and dropped one frame short of the
eye — the same shape as every other defect this codebase keeps finding.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.cli.ov import _liquidity_lines

_FULL_BUCKET = {"anthropic": {"tokens_remaining": 5_000_000,
                              "seconds_to_reset": None}}
_DEAD = {"claude": {"state": "open", "consecutive_economic_failures": 3,
                    "recovery_window_s": 60.0}}


def _joined(**kw) -> str:
    return "\n".join(_liquidity_lines(_FULL_BUCKET, **kw))


class TestTheSoakBanner:
    def test_a_full_bucket_alone_still_reads_healthy(self):
        """The regression case, preserved. Nothing about a rate-limit bucket
        is wrong — it is simply not a balance."""
        out = _joined()
        assert "5,000,000 tokens" in out
        assert "OUT OF CREDIT" not in out

    def test_economic_death_is_stated_beside_the_healthy_number(self):
        out = _joined(any_exhausted=True, economic=_DEAD)
        assert "5,000,000 tokens" in out          # the fact is not suppressed
        assert "OUT OF CREDIT" in out             # nor is the one that matters

    def test_it_names_the_axis_the_operator_would_otherwise_confuse(self):
        """"You have 5M tokens" and "you cannot spend anything" are both true.
        Only saying so removes the contradiction."""
        out = _joined(any_exhausted=True, economic=_DEAD)
        assert "RATE LIMIT, not a balance" in out

    def test_it_names_the_remedy(self):
        """The one action in this banner an operator can take immediately."""
        assert "Add credits" in _joined(any_exhausted=True, economic=_DEAD)

    def test_it_names_the_provider(self):
        """"a provider" is the one thing the operator cannot look up — the
        rule the dry-runway line already follows."""
        assert "claude:" in _joined(any_exhausted=True, economic=_DEAD)


class TestItDoesNotCryWolf:
    def test_zero_economic_failures_says_nothing(self):
        quiet = {"claude": {"state": "closed",
                            "consecutive_economic_failures": 0}}
        assert "OUT OF CREDIT" not in _joined(economic=quiet)

    def test_the_vague_fallback_yields_to_the_specific_one(self):
        """`any_exhausted` with no matching row prints "a runway is reported
        dry but no provider row shows it — run /provider". That hedge is right
        when nothing better is known and noise once the cause is named."""
        out = _joined(any_exhausted=True, economic=_DEAD)
        assert "no provider row shows it" not in out

    def test_the_vague_fallback_SURVIVES_when_there_is_no_economic_detail(self):
        out = _joined(any_exhausted=True)
        assert "no provider row shows it" in out

    @pytest.mark.parametrize("hostile", [
        None, {}, {"claude": {}}, {"claude": None},
        {"claude": {"consecutive_economic_failures": "three"}},
        {"claude": {"consecutive_economic_failures": -1}},
    ])
    def test_malformed_economic_payloads_never_raise(self, hostile):
        lines = _liquidity_lines(_FULL_BUCKET, economic=hostile)
        assert isinstance(lines, list)
        assert any("liquidity" in row for row in lines)


class TestTheProducerSide:
    def test_the_harness_reads_the_breaker(self):
        """The state was always one import away from the payload builder.

        Asserted on the harness source because the function is a closure
        inside a 200-line boot sequence — reaching it any other way would mean
        booting a cockpit to test a dictionary.
        """
        from pathlib import Path
        source = Path(
            "backend/core/ouroboros/battle_test/harness.py"
        ).read_text(encoding="utf-8", errors="replace")
        assert "claude_circuit_breaker" in source
        assert '"economic"' in source

    def test_economic_death_forces_the_exhausted_flag(self):
        """An economically dead lane IS an exhausted runway whatever the
        bucket says. Without this the aggregate warning never fires and the
        banner keeps its cheerful count."""
        from pathlib import Path
        source = Path(
            "backend/core/ouroboros/battle_test/harness.py"
        ).read_text(encoding="utf-8", errors="replace")
        assert 'payload["any_exhausted"] = True' in source

    def test_the_breaker_exposes_what_the_banner_needs(self):
        """Contract check against the real class, so a renamed field fails
        here rather than silently emptying the warning."""
        from backend.core.ouroboros.governance.claude_circuit_breaker import (
            get_claude_circuit_breaker,
        )
        snap = get_claude_circuit_breaker().snapshot()
        assert "consecutive_economic_failures" in snap
        assert "state" in snap
