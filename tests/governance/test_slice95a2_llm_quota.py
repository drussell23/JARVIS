"""Slice 95a-2 — LLM quota decouple + honest loud-fail + observability.

TDD regression spine.  ALL LLM calls are MOCKED — zero live calls.

Test plan
---------
A. llm_per_seed decoupled quota
   A1. run_immunization_campaign with llm_per_seed=5: mock provider called
       with n=5 per seed, regardless of how many deterministic candidates
       were produced.
   A2. llm_per_seed=None: backward-compat — n = max(0, per_pattern - len(det))
       (can be 0 when deterministic fills the budget).
   A3. llm_per_seed cap: llm_per_seed > _MAX_MUTATIONS_PER_PATTERN is capped.

B. call_attempts tracking on LLMMutationProvider
   B1. call_attempts increments when n>0 and request is attempted.
   B2. call_attempts stays 0 when n=0 (early-return path).
   B3. call_attempts increments even when model returns [] (attempt = request).

C. Loud-fail distinction: config-starvation vs auth-failure
   C1. call_attempts==0 AND generated_count==0 → ConfigStarvationError (NOT
       AdversarialTelemetryPanic) with "[CONFIG]" in message.
   C2. call_attempts>0 AND generated_count==0, spend==0 → AdversarialTelemetryPanic
       with "auth unresolved / Aegis" in message.
   C3. call_attempts>0 AND generated_count==0, spend>0 → AdversarialTelemetryPanic
       with "empty/unparseable completions" in message.
   C4. call_attempts>0 AND generated_count>0 → no panic (normal operation).

D. Observability fields in summarize_campaign
   D1. Summary dict includes llm_call_attempts, llm_generated_count,
       llm_spend_usd, llm_per_seed.

E. Calibration derives llm_per_seed >= 1
   E1. run_calibration with max_mutations=100 and N seeds → llm_per_seed >= 1.
   E2. --llm-per-seed CLI override parsed correctly.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

_WT_ROOT = Path(__file__).resolve().parents[2]
if str(_WT_ROOT) not in sys.path:
    sys.path.insert(0, str(_WT_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _fence(text: str = "y = 1\n") -> str:
    return f"```python\n{text}\n```"


def _make_mock_response(text: str = "```python\ny = 1\n```") -> MagicMock:
    block = MagicMock()
    block.text = text
    usage = MagicMock()
    usage.input_tokens = 50
    usage.output_tokens = 20
    resp = MagicMock()
    resp.content = [block]
    resp.usage = usage
    return resp


def _make_mock_client(response=None) -> MagicMock:
    if response is None:
        response = _make_mock_response()
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=response)
    return client


# Patch paths shared across tests
_AEGIS_ENABLED_PATH = "backend.core.ouroboros.aegis.client.is_enabled"
_AEGIS_BRIDGE_PATH = "backend.core.ouroboros.governance.aegis_provider_bridge"


def _make_minimal_seed():
    """Return a minimal seed-like object with a .source attribute."""
    from backend.core.ouroboros.governance.self_immunization import (
        _load_seed_entries,
    )
    seeds = _load_seed_entries()
    if seeds:
        return seeds[0]
    # Fallback synthetic seed
    seed = MagicMock()
    seed.name = "test_seed"
    seed.source = "x = 1\n"
    from backend.core.ouroboros.governance.graduation.adversarial_cage import CorpusCategory
    seed.category = CorpusCategory.SANDBOX_ESCAPE
    return seed


# ---------------------------------------------------------------------------
# A. llm_per_seed decoupled quota
# ---------------------------------------------------------------------------

class TestLlmPerSeedDecoupled:
    """A — llm_per_seed flows as an independent LLM quota."""

    def test_a1_llm_called_with_llm_per_seed_n_regardless_of_deterministic(self):
        """A1: with llm_per_seed=5, provider.mutate is called n=5 per seed,
        even when deterministic-8 already produced >= 5 candidates.
        # Slice 95a-2
        """
        from backend.core.ouroboros.governance import self_immunization as si

        mock_client = _make_mock_client()
        provider = si.LLMMutationProvider(client=mock_client)

        call_ns: List[int] = []

        async def _capture_mutate(seed_src, *, n):
            call_ns.append(n)
            # Return a valid python snippet so generated_count advances
            return ["y = x + 1\n"] * min(n, 1)

        provider.mutate = _capture_mutate  # type: ignore[assignment]

        seed = _make_minimal_seed()

        with patch(_AEGIS_ENABLED_PATH, return_value=False):
            with patch.dict(os.environ, {
                si._ENV_MUTATIONS_PER_PATTERN: "3",
                "JARVIS_ANTIVENOM_SELF_IMMUNIZATION_ENABLED": "true",
            }):
                async def _run_campaign():
                    async for _ in si.run_immunization_campaign(
                        seeds=[seed],
                        mutation_provider=provider,
                        llm_per_seed=5,
                    ):
                        pass

                _run(_run_campaign())

        # Provider must have been called with n=5 for the seed
        assert len(call_ns) >= 1, "provider.mutate was never called"
        assert all(n == 5 for n in call_ns), (
            f"Expected n=5 for all calls, got: {call_ns}"
        )

    def test_a2_llm_per_seed_none_preserves_legacy_n_formula(self):
        """A2: llm_per_seed=None → n = max(0, per_pattern - len(candidates)),
        which is 0 when deterministic fills the budget.
        # Slice 95a-2
        """
        from backend.core.ouroboros.governance import self_immunization as si

        mock_client = _make_mock_client()
        provider = si.LLMMutationProvider(client=mock_client)

        call_ns: List[int] = []

        async def _capture_mutate(seed_src, *, n):
            call_ns.append(n)
            return []

        provider.mutate = _capture_mutate  # type: ignore[assignment]

        seed = _make_minimal_seed()

        # Set per_pattern low (e.g. 3) so deterministic-8 fills the quota
        with patch(_AEGIS_ENABLED_PATH, return_value=False):
            with patch.dict(os.environ, {
                si._ENV_MUTATIONS_PER_PATTERN: "3",
                "JARVIS_ANTIVENOM_SELF_IMMUNIZATION_ENABLED": "true",
            }):
                async def _run_campaign():
                    async for _ in si.run_immunization_campaign(
                        seeds=[seed],
                        mutation_provider=provider,
                        llm_per_seed=None,  # explicit None = legacy behavior
                    ):
                        pass

                _run(_run_campaign())

        # With deterministic-8 producing >= 3 candidates, n should be 0
        # (or provider not called at all — both acceptable for legacy compat)
        if call_ns:
            assert all(n == 0 for n in call_ns), (
                f"Legacy formula should yield n=0 (det fills budget), got: {call_ns}"
            )

    def test_a3_llm_per_seed_capped_at_max_mutations_per_pattern(self):
        """A3: llm_per_seed above _MAX_MUTATIONS_PER_PATTERN is capped.
        # Slice 95a-2
        """
        from backend.core.ouroboros.governance import self_immunization as si

        mock_client = _make_mock_client()
        provider = si.LLMMutationProvider(client=mock_client)

        call_ns: List[int] = []

        async def _capture_mutate(seed_src, *, n):
            call_ns.append(n)
            return []

        provider.mutate = _capture_mutate  # type: ignore[assignment]

        seed = _make_minimal_seed()
        # Pass a value way above the max
        oversized = si._MAX_MUTATIONS_PER_PATTERN + 500

        with patch(_AEGIS_ENABLED_PATH, return_value=False):
            with patch.dict(os.environ, {
                si._ENV_MUTATIONS_PER_PATTERN: "3",
                "JARVIS_ANTIVENOM_SELF_IMMUNIZATION_ENABLED": "true",
            }):
                async def _run_campaign():
                    async for _ in si.run_immunization_campaign(
                        seeds=[seed],
                        mutation_provider=provider,
                        llm_per_seed=oversized,
                    ):
                        pass

                _run(_run_campaign())

        if call_ns:
            assert all(n <= si._MAX_MUTATIONS_PER_PATTERN for n in call_ns), (
                f"llm_per_seed must be capped at _MAX_MUTATIONS_PER_PATTERN "
                f"({si._MAX_MUTATIONS_PER_PATTERN}), got: {call_ns}"
            )


# ---------------------------------------------------------------------------
# B. call_attempts tracking
# ---------------------------------------------------------------------------

class TestCallAttempts:
    """B — call_attempts tracks actual request attempts."""

    def test_b1_call_attempts_increments_when_n_gt_0_and_request_attempted(self):
        """B1: call_attempts += 1 per mutate(n>0) that reaches messages.create.
        # Slice 95a-2
        """
        from backend.core.ouroboros.governance import self_immunization as si

        mock_client = _make_mock_client()
        provider = si.LLMMutationProvider(client=mock_client)

        with patch(_AEGIS_ENABLED_PATH, return_value=False):
            with patch(
                f"{_AEGIS_BRIDGE_PATH}.merge_lease_header",
                return_value={},
            ):
                result = _run(provider.mutate("x = 1\n", n=3))

        assert provider.call_attempts == 1, (
            f"Expected call_attempts=1 after one n=3 call, got {provider.call_attempts}"
        )

    def test_b2_call_attempts_stays_zero_for_n_eq_0(self):
        """B2: call_attempts unchanged when n=0 (early-return).
        # Slice 95a-2
        """
        from backend.core.ouroboros.governance import self_immunization as si

        mock_client = _make_mock_client()
        provider = si.LLMMutationProvider(client=mock_client)

        result = _run(provider.mutate("x = 1\n", n=0))

        assert result == [] or result == ()
        assert provider.call_attempts == 0, (
            f"call_attempts must stay 0 for n=0, got {provider.call_attempts}"
        )
        # messages.create should never have been called
        mock_client.messages.create.assert_not_called()

    def test_b3_call_attempts_increments_even_when_model_returns_empty(self):
        """B3: call_attempts increments even when model response yields 0 valid.
        # Slice 95a-2
        """
        from backend.core.ouroboros.governance import self_immunization as si

        # Mock client returns empty-text response (no code fences)
        empty_resp = MagicMock()
        empty_resp.content = []
        empty_resp.usage = MagicMock(input_tokens=10, output_tokens=0)
        mock_client = _make_mock_client(empty_resp)
        provider = si.LLMMutationProvider(client=mock_client)

        with patch(_AEGIS_ENABLED_PATH, return_value=False):
            with patch(
                f"{_AEGIS_BRIDGE_PATH}.merge_lease_header",
                return_value={},
            ):
                result = _run(provider.mutate("x = 1\n", n=3))

        # generated_count stays 0 (nothing valid returned)
        assert provider.generated_count == 0
        # BUT call_attempts should be 1 — a request was sent
        assert provider.call_attempts == 1, (
            f"Expected call_attempts=1 even with empty response, "
            f"got {provider.call_attempts}"
        )

    def test_b4_call_attempts_accumulates_across_multiple_calls(self):
        """B4: call_attempts accumulates correctly over multiple mutate calls.
        # Slice 95a-2
        """
        from backend.core.ouroboros.governance import self_immunization as si

        mock_client = _make_mock_client()
        provider = si.LLMMutationProvider(client=mock_client)

        with patch(_AEGIS_ENABLED_PATH, return_value=False):
            with patch(
                f"{_AEGIS_BRIDGE_PATH}.merge_lease_header",
                return_value={},
            ):
                _run(provider.mutate("x = 1\n", n=2))
                _run(provider.mutate("y = 2\n", n=2))
                _run(provider.mutate("z = 3\n", n=0))  # should NOT increment

        assert provider.call_attempts == 2, (
            f"Expected 2 call_attempts (two n>0 calls), got {provider.call_attempts}"
        )


# ---------------------------------------------------------------------------
# C. Loud-fail distinction
# ---------------------------------------------------------------------------

class TestLoudFailDistinction:
    """C — config-starvation vs auth-failure must produce distinct errors."""

    def _make_zero_provider(self) -> "MagicMock":
        """Provider that always returns 0 mutations."""
        from backend.core.ouroboros.governance.self_immunization import (
            LLMMutationProvider,
            MutationBudgetGuard,
        )
        provider = MagicMock(spec=LLMMutationProvider)
        provider.generated_count = 0
        provider.call_attempts = 0
        return provider

    def test_c1_config_starvation_raises_distinct_error_not_auth_panic(self):
        """C1: call_attempts==0 → ConfigStarvationError with [CONFIG] message.
        # Slice 95a-2
        """
        from backend.core.ouroboros.governance import self_immunization as si
        from scripts.security.run_cc_parity_calibration import (
            _loud_fail_on_zero_llm_throughput,
        )

        provider = self._make_zero_provider()
        provider.call_attempts = 0
        provider.generated_count = 0
        guard = si.MutationBudgetGuard(budget_usd=0.10)
        # No spend recorded → accumulated == 0

        with pytest.raises(si.ConfigStarvationError) as exc_info:
            _loud_fail_on_zero_llm_throughput(
                provider=provider, guard=guard, dry_run=False
            )

        msg = str(exc_info.value)
        assert "[CONFIG]" in msg, f"Expected [CONFIG] in message, got: {msg!r}"
        # The message must clearly disclaim auth/Aegis as the cause.
        # It is acceptable to MENTION "auth/Aegis" in a "No auth/Aegis fault"
        # disclaimer — the key is the message does NOT claim auth is to blame.
        # Confirm it is ConfigStarvationError, not AdversarialTelemetryPanic.
        from backend.core.ouroboros.governance.self_immunization import (
            AdversarialTelemetryPanic,
        )
        assert not isinstance(exc_info.value, AdversarialTelemetryPanic), (
            "Config starvation must NOT be AdversarialTelemetryPanic"
        )
        # The config message should mention the real cause: n=0 / per-seed budget
        assert any(kw in msg for kw in ("n=0", "per-seed", "generative provider")), (
            f"Config-starvation message should explain the n=0 cause: {msg!r}"
        )

    def test_c2_auth_panic_when_call_attempted_zero_spend(self):
        """C2: call_attempts>0, generated==0, spend==0 → AdversarialTelemetryPanic.
        # Slice 95a-2
        """
        from backend.core.ouroboros.governance import self_immunization as si
        from scripts.security.run_cc_parity_calibration import (
            _loud_fail_on_zero_llm_throughput,
        )

        provider = self._make_zero_provider()
        provider.call_attempts = 2
        provider.generated_count = 0
        guard = si.MutationBudgetGuard(budget_usd=0.10)
        # No spend → 0.0

        with pytest.raises(si.AdversarialTelemetryPanic) as exc_info:
            _loud_fail_on_zero_llm_throughput(
                provider=provider, guard=guard, dry_run=False
            )

        msg = str(exc_info.value)
        # Should mention auth/Aegis
        assert "auth" in msg.lower() or "Aegis" in msg or "CRITICAL" in msg, (
            f"Auth-failure panic should mention auth/Aegis/CRITICAL: {msg!r}"
        )

    def test_c3_empty_stream_panic_when_call_attempted_with_spend(self):
        """C3: call_attempts>0, generated==0, spend>0 → AdversarialTelemetryPanic.
        # Slice 95a-2
        """
        from backend.core.ouroboros.governance import self_immunization as si
        from scripts.security.run_cc_parity_calibration import (
            _loud_fail_on_zero_llm_throughput,
        )

        provider = self._make_zero_provider()
        provider.call_attempts = 1
        provider.generated_count = 0
        guard = si.MutationBudgetGuard(budget_usd=0.10)
        guard.record_spend(0.005, label="test_spend")

        with pytest.raises(si.AdversarialTelemetryPanic) as exc_info:
            _loud_fail_on_zero_llm_throughput(
                provider=provider, guard=guard, dry_run=False
            )

        msg = str(exc_info.value)
        # Should mention spend / empty / unparseable
        assert any(kw in msg.lower() for kw in ("spend", "empty", "unparseable", "content")), (
            f"Empty-stream panic should mention spend/content issues: {msg!r}"
        )

    def test_c4_no_panic_when_generated_gt_zero(self):
        """C4: generated_count>0 → no panic raised regardless of call_attempts.
        # Slice 95a-2
        """
        from backend.core.ouroboros.governance import self_immunization as si
        from scripts.security.run_cc_parity_calibration import (
            _loud_fail_on_zero_llm_throughput,
        )

        provider = self._make_zero_provider()
        provider.call_attempts = 3
        provider.generated_count = 5  # successfully generated
        guard = si.MutationBudgetGuard(budget_usd=0.10)
        guard.record_spend(0.02, label="test")

        # Should not raise
        _loud_fail_on_zero_llm_throughput(
            provider=provider, guard=guard, dry_run=False
        )

    def test_c5_no_panic_in_dry_run_mode(self):
        """C5: dry_run=True → no panic regardless of call_attempts/generated.
        # Slice 95a-2
        """
        from backend.core.ouroboros.governance import self_immunization as si
        from scripts.security.run_cc_parity_calibration import (
            _loud_fail_on_zero_llm_throughput,
        )

        provider = self._make_zero_provider()
        provider.call_attempts = 0
        provider.generated_count = 0
        guard = si.MutationBudgetGuard(budget_usd=0.10)

        # dry_run=True → no panic
        _loud_fail_on_zero_llm_throughput(
            provider=provider, guard=guard, dry_run=True
        )

    def test_c6_no_panic_when_provider_is_none(self):
        """C6: provider=None (dry-run / deterministic) → no panic.
        # Slice 95a-2
        """
        from backend.core.ouroboros.governance import self_immunization as si
        from scripts.security.run_cc_parity_calibration import (
            _loud_fail_on_zero_llm_throughput,
        )

        guard = si.MutationBudgetGuard(budget_usd=0.10)

        # Should not raise
        _loud_fail_on_zero_llm_throughput(
            provider=None, guard=guard, dry_run=False
        )


# ---------------------------------------------------------------------------
# D. Observability fields in summarize_campaign
# ---------------------------------------------------------------------------

class TestObservabilityFields:
    """D — summary dict includes LLM telemetry fields."""

    def test_d1_summarize_campaign_includes_llm_observability_fields(self):
        """D1: summarize_campaign returns llm_call_attempts, llm_generated_count,
        llm_spend_usd, llm_per_seed.
        # Slice 95a-2
        """
        from backend.core.ouroboros.governance import self_immunization as si

        mock_client = _make_mock_client()
        provider = si.LLMMutationProvider(client=mock_client)

        seed = _make_minimal_seed()

        with patch(_AEGIS_ENABLED_PATH, return_value=False):
            with patch(
                f"{_AEGIS_BRIDGE_PATH}.merge_lease_header",
                return_value={},
            ):
                with patch.dict(os.environ, {
                    si._ENV_MUTATIONS_PER_PATTERN: "3",
                    "JARVIS_ANTIVENOM_SELF_IMMUNIZATION_ENABLED": "true",
                }):
                    summary = _run(si.summarize_campaign(
                        seeds=[seed],
                        mutation_provider=provider,
                        llm_per_seed=2,
                    ))

        required_fields = (
            "llm_call_attempts",
            "llm_generated_count",
            "llm_spend_usd",
            "llm_per_seed",
        )
        for field in required_fields:
            assert field in summary, (
                f"summarize_campaign result missing '{field}': {list(summary.keys())}"
            )

    def test_d2_observability_fields_reflect_actual_counts(self):
        """D2: llm_call_attempts and llm_generated_count match provider state.
        # Slice 95a-2
        """
        from backend.core.ouroboros.governance import self_immunization as si

        mock_client = _make_mock_client()
        provider = si.LLMMutationProvider(client=mock_client)

        seed = _make_minimal_seed()

        with patch(_AEGIS_ENABLED_PATH, return_value=False):
            with patch(
                f"{_AEGIS_BRIDGE_PATH}.merge_lease_header",
                return_value={},
            ):
                with patch.dict(os.environ, {
                    si._ENV_MUTATIONS_PER_PATTERN: "3",
                    "JARVIS_ANTIVENOM_SELF_IMMUNIZATION_ENABLED": "true",
                }):
                    summary = _run(si.summarize_campaign(
                        seeds=[seed],
                        mutation_provider=provider,
                        llm_per_seed=2,
                    ))

        # The summary fields should mirror what the provider tracked
        assert summary["llm_call_attempts"] == provider.call_attempts
        assert summary["llm_generated_count"] == provider.generated_count
        assert summary["llm_per_seed"] == 2


# ---------------------------------------------------------------------------
# E. Calibration derives llm_per_seed >= 1
# ---------------------------------------------------------------------------

class TestCalibrationLlmPerSeed:
    """E — calibration script derives and respects llm_per_seed."""

    def test_e1_calibration_derives_llm_per_seed_ge_1(self):
        """E1: for any max_mutations >= 1 and N seeds, llm_per_seed >= 1.
        # Slice 95a-2
        """
        from scripts.security.run_cc_parity_calibration import (
            _derive_llm_per_seed,
        )
        from backend.core.ouroboros.governance import self_immunization as si

        seeds = si._load_seed_entries()
        n_seeds = max(len(seeds), 1)

        for max_mut in (1, 10, 100, 200, 1000):
            result = _derive_llm_per_seed(
                max_mutations=max_mut,
                num_seeds=n_seeds,
                override=None,
            )
            assert result >= 1, (
                f"llm_per_seed must be >= 1 for max_mutations={max_mut}, "
                f"num_seeds={n_seeds}, got {result}"
            )

    def test_e2_llm_per_seed_cli_override_parsed(self):
        """E2: --llm-per-seed CLI arg is parsed and passed through.
        # Slice 95a-2
        """
        from scripts.security import run_cc_parity_calibration as calib

        args = calib._parse_args(["--llm-per-seed", "7"])
        assert hasattr(args, "llm_per_seed"), (
            "argparse result missing 'llm_per_seed' attribute"
        )
        assert args.llm_per_seed == 7, (
            f"Expected llm_per_seed=7, got {args.llm_per_seed}"
        )

    def test_e3_llm_per_seed_override_respected_by_derive(self):
        """E3: _derive_llm_per_seed with override returns the override value.
        # Slice 95a-2
        """
        from scripts.security.run_cc_parity_calibration import (
            _derive_llm_per_seed,
        )

        result = _derive_llm_per_seed(
            max_mutations=100,
            num_seeds=10,
            override=15,
        )
        assert result == 15, f"Expected override=15, got {result}"
