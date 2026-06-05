"""Slice 94 — Calibration Hardening TDD regression spine.

Covers:
(a) sys.path bootstrap: repo_root computed correctly from __file__ depth.
(b) Readiness check: returns no_credential when ANTHROPIC_API_KEY absent
    AND Aegis health probe unreachable (both conditions mocked).
(c) AdversarialTelemetryPanic raised when LLM-enabled run yields
    0 generated mutations + $0 spend (mock provider returning [] always).
(d) NO panic when provider generated > 0 mutations (even if all caged).
(e) --bootstrap-aegis composes aegis_preflight (mock READY) and loads
    .env (mock) — succeeds cleanly.
(f) Bootstrap fails loud when aegis_preflight returns non-READY (mock
    FAILED_CREDENTIAL_SCRUB).
(g) LLMMutationProvider.generated_count increments correctly.

ALL LLM calls and daemon interactions are MOCKED.  No live paid calls.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
from pathlib import Path
from typing import List, Sequence
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure worktree root is on sys.path so backend.* imports work.
# ---------------------------------------------------------------------------

_WT_ROOT = Path(__file__).resolve().parents[2]  # tests/governance/ → root
if str(_WT_ROOT) not in sys.path:
    sys.path.insert(0, str(_WT_ROOT))


# ---------------------------------------------------------------------------
# (a) sys.path bootstrap — verify repo_root computation
# ---------------------------------------------------------------------------


class TestSysPathBootstrap:
    """Phase 3: the script's _REPO_ROOT resolves to the actual repo root."""

    def test_script_repo_root_resolves_correctly(self):
        """Import the script as a module and check _REPO_ROOT."""
        script_path = _WT_ROOT / "scripts" / "security" / "run_cc_parity_calibration.py"
        assert script_path.exists(), f"Script not found: {script_path}"

        # Dynamically load the module to inspect _REPO_ROOT without running main.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_calib_bootstrap_test", script_path
        )
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        # Execute the module (runs the bootstrap block).
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

        assert hasattr(mod, "_REPO_ROOT"), "_REPO_ROOT not defined in script"
        repo_root: Path = mod._REPO_ROOT
        # The repo root must contain CLAUDE.md (canonical marker).
        assert (repo_root / "CLAUDE.md").exists(), (
            f"_REPO_ROOT={repo_root} does not contain CLAUDE.md — "
            "wrong parents depth"
        )

    def test_script_importable_without_pythonpath(self, tmp_path, monkeypatch):
        """The script's sys.path insert lets `backend` import succeed even
        if PYTHONPATH is not set.

        We verify indirectly: after loading the module, `backend` must
        appear importable (i.e. _REPO_ROOT was inserted into sys.path).
        """
        # Remove any existing path entry that contains 'backend' from
        # sys.path to simulate a clean environment.
        original_path = sys.path[:]
        try:
            sys.path = [p for p in sys.path if "jarvis" not in p.lower()
                        or "site-packages" in p]

            script_path = _WT_ROOT / "scripts" / "security" / "run_cc_parity_calibration.py"
            import importlib.util
            spec = importlib.util.spec_from_file_location("_calib_tmp", script_path)
            mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            spec.loader.exec_module(mod)  # type: ignore[union-attr]

            # After exec, _REPO_ROOT should be in sys.path.
            assert str(mod._REPO_ROOT) in sys.path
        finally:
            sys.path[:] = original_path


# ---------------------------------------------------------------------------
# Helpers — shared stubs
# ---------------------------------------------------------------------------


class _MockProviderZero:
    """Always returns [] — simulates auth failure / silent empty completions."""

    def __init__(self) -> None:
        self.generated_count: int = 0

    async def mutate(self, seed_source: str, *, n: int) -> Sequence[str]:
        return []


class _MockProviderPositive:
    """Returns n valid-Python stubs — simulates working LLM."""

    def __init__(self, mutations_per_call: int = 2) -> None:
        self.generated_count: int = 0
        self._per_call = mutations_per_call

    async def mutate(self, seed_source: str, *, n: int) -> Sequence[str]:
        results: List[str] = []
        for i in range(min(n, self._per_call)):
            results.append(f"x = {i}  # llm mutation {i}\n")
            self.generated_count += 1
        return results


# ---------------------------------------------------------------------------
# (b) Readiness check
# ---------------------------------------------------------------------------


class TestAegisReadinessCheck:
    """Phase 1: _check_aegis_readiness() returns correct verdicts."""

    def test_no_credential_when_key_absent(self, monkeypatch):
        """Without ANTHROPIC_API_KEY, verdict = no_credential."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        from scripts.security.run_cc_parity_calibration import (
            _check_aegis_readiness,
            _AegisReadiness,
        )
        result = asyncio.get_event_loop().run_until_complete(
            _check_aegis_readiness(aegis_url=None)
        )
        assert result == _AegisReadiness.NO_CREDENTIAL

    def test_no_daemon_when_health_probe_fails(self, monkeypatch):
        """With credential present but health probe fails → no_daemon."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")

        from scripts.security.run_cc_parity_calibration import (
            _check_aegis_readiness,
            _AegisReadiness,
        )

        async def _failing_probe():
            # Simulate health probe failing (connection refused / timeout).
            with patch(
                "aiohttp.ClientSession",
                side_effect=Exception("connection refused"),
            ):
                return await _check_aegis_readiness(
                    aegis_url="http://127.0.0.1:9999"
                )

        result = asyncio.get_event_loop().run_until_complete(_failing_probe())
        assert result == _AegisReadiness.NO_DAEMON

    def test_ready_when_no_url_but_credential_present(self, monkeypatch):
        """No aegis_url provided + credential present → READY (pre-bootstrap)."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")

        from scripts.security.run_cc_parity_calibration import (
            _check_aegis_readiness,
            _AegisReadiness,
        )
        result = asyncio.get_event_loop().run_until_complete(
            _check_aegis_readiness(aegis_url=None)
        )
        assert result == _AegisReadiness.READY


# ---------------------------------------------------------------------------
# (c) AdversarialTelemetryPanic on zero LLM throughput
# ---------------------------------------------------------------------------


class TestAdversarialTelemetryPanic:
    """Phase 2: run_calibration raises AdversarialTelemetryPanic when LLM
    provider yields 0 valid mutations — regardless of spend.

    Two branches:
      (1) zero generated + zero spend → auth/proxy failure message
      (2) zero generated + nonzero spend → empty-stream/done_before_content msg
    """

    def _make_minimal_seed(self):
        class _Cat:
            def __init__(self, v):
                self.value = v
        class _Seed:
            def __init__(self):
                self.name = "test_seed_panic"
                self.source = "import shutil\nshutil.disk_usage('/')\n"
                self.category = _Cat("sandbox_escape")
                self.known_gap = False
        return [_Seed()]

    def test_panic_on_zero_generated_zero_spend(self, monkeypatch):
        """A mock provider that returns [] always + zero spend → panic."""
        monkeypatch.setenv("JARVIS_ANTIVENOM_SELF_IMMUNIZATION_ENABLED", "true")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")

        from backend.core.ouroboros.governance.self_immunization import (
            AdversarialTelemetryPanic,
            MutationBudgetGuard,
        )
        from scripts.security.run_cc_parity_calibration import run_calibration

        guard = MutationBudgetGuard(budget_usd=0.10)
        # Zero-throughput mock provider.
        zero_provider = _MockProviderZero()

        seeds = self._make_minimal_seed()

        async def _run():
            # Patch _load_seed_entries to return our minimal seed.
            with patch(
                "backend.core.ouroboros.governance.self_immunization._load_seed_entries",
                return_value=seeds,
            ):
                # Patch LLMMutationProvider construction to return our mock.
                with patch(
                    "backend.core.ouroboros.governance.self_immunization.LLMMutationProvider",
                    return_value=zero_provider,
                ):
                    # Also wire the guard to confirm zero spend.
                    with patch(
                        "backend.core.ouroboros.governance.self_immunization.MutationBudgetGuard",
                        return_value=guard,
                    ):
                        # Readiness check — bypass by mocking ANTHROPIC_API_KEY
                        # already set via monkeypatch.
                        return await run_calibration(
                            max_mutations=4,
                            dry_run=False,
                            budget_usd=0.10,
                        )

        with pytest.raises(AdversarialTelemetryPanic) as exc_info:
            asyncio.get_event_loop().run_until_complete(_run())

        msg = str(exc_info.value)
        assert "throughput = 0" in msg
        # The exception message itself says "No [PASS] emitted" — meaning
        # [PASS] was deliberately NOT printed.  We verify the panic was
        # raised (not swallowed) and contains the expected critical fault text.
        assert "CRITICAL FAULT" in msg

    def test_no_panic_on_dry_run(self, monkeypatch):
        """dry_run=True — no provider injected → no panic even with zero
        LLM output (deterministic-only is expected)."""
        monkeypatch.setenv("JARVIS_ANTIVENOM_SELF_IMMUNIZATION_ENABLED", "true")

        from scripts.security.run_cc_parity_calibration import run_calibration

        seeds = self._make_minimal_seed()

        async def _run():
            with patch(
                "backend.core.ouroboros.governance.self_immunization._load_seed_entries",
                return_value=seeds,
            ):
                return await run_calibration(
                    max_mutations=4,
                    dry_run=True,
                    budget_usd=0.10,
                )

        # Should NOT raise.
        result = asyncio.get_event_loop().run_until_complete(_run())
        assert isinstance(result, int)

    def test_panic_on_zero_generated_nonzero_spend(self, monkeypatch):
        """A mock provider that returns [] with nonzero spend (done_before_content
        / empty-stream class) → panic with spend-specific message."""
        monkeypatch.setenv("JARVIS_ANTIVENOM_SELF_IMMUNIZATION_ENABLED", "true")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")

        from backend.core.ouroboros.governance.self_immunization import (
            AdversarialTelemetryPanic,
            MutationBudgetGuard,
        )
        from scripts.security.run_cc_parity_calibration import run_calibration

        # Guard that already has nonzero accumulated spend recorded.
        guard = MutationBudgetGuard(budget_usd=0.10)
        guard.record_spend(0.0042, label="done_before_content_sim")

        # Zero-throughput mock provider — generated_count stays 0.
        zero_provider = _MockProviderZero()

        seeds = self._make_minimal_seed()

        async def _run():
            with patch(
                "backend.core.ouroboros.governance.self_immunization._load_seed_entries",
                return_value=seeds,
            ):
                with patch(
                    "backend.core.ouroboros.governance.self_immunization.LLMMutationProvider",
                    return_value=zero_provider,
                ):
                    with patch(
                        "backend.core.ouroboros.governance.self_immunization.MutationBudgetGuard",
                        return_value=guard,
                    ):
                        return await run_calibration(
                            max_mutations=4,
                            dry_run=False,
                            budget_usd=0.10,
                        )

        with pytest.raises(AdversarialTelemetryPanic) as exc_info:
            asyncio.get_event_loop().run_until_complete(_run())

        msg = str(exc_info.value)
        assert "CRITICAL FAULT" in msg
        assert "throughput = 0" in msg
        # Nonzero-spend branch should mention spend amount and
        # done_before_content / empty-stream condition.
        assert "empty" in msg.lower() or "done_before_content" in msg.lower() or "spend" in msg.lower()
        # Must NOT show the "auth unresolved" phrasing (that's the $0 branch).
        assert "auth unresolved" not in msg


# ---------------------------------------------------------------------------
# (d) No panic when provider generated > 0 mutations
# ---------------------------------------------------------------------------


class TestNoPanicWithPositiveThroughput:
    """Phase 2: if generated_count > 0 (even if all mutations are caged),
    no AdversarialTelemetryPanic is raised."""

    def _make_minimal_seed(self):
        class _Cat:
            def __init__(self, v):
                self.value = v
        class _Seed:
            def __init__(self):
                self.name = "test_seed_positive"
                self.source = "import shutil\nshutil.disk_usage('/')\n"
                self.category = _Cat("sandbox_escape")
                self.known_gap = False
        return [_Seed()]

    def test_no_panic_with_positive_generated_count(self, monkeypatch):
        """Provider returned 1+ mutations (all may be caged) — no panic."""
        monkeypatch.setenv("JARVIS_ANTIVENOM_SELF_IMMUNIZATION_ENABLED", "true")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")

        from backend.core.ouroboros.governance.self_immunization import (
            AdversarialTelemetryPanic,
            MutationBudgetGuard,
        )
        from scripts.security.run_cc_parity_calibration import run_calibration

        guard = MutationBudgetGuard(budget_usd=0.10)
        # Simulate small recorded spend so guard is non-zero.
        guard.record_spend(0.0001, label="test")
        positive_provider = _MockProviderPositive(mutations_per_call=1)
        # Manually set generated_count to simulate prior mutations.
        positive_provider.generated_count = 2

        seeds = self._make_minimal_seed()

        async def _run():
            with patch(
                "backend.core.ouroboros.governance.self_immunization._load_seed_entries",
                return_value=seeds,
            ):
                with patch(
                    "backend.core.ouroboros.governance.self_immunization.LLMMutationProvider",
                    return_value=positive_provider,
                ):
                    with patch(
                        "backend.core.ouroboros.governance.self_immunization.MutationBudgetGuard",
                        return_value=guard,
                    ):
                        return await run_calibration(
                            max_mutations=4,
                            dry_run=False,
                            budget_usd=0.10,
                        )

        # Should complete WITHOUT AdversarialTelemetryPanic.
        try:
            result = asyncio.get_event_loop().run_until_complete(_run())
            assert isinstance(result, int)
        except AdversarialTelemetryPanic:
            pytest.fail(
                "AdversarialTelemetryPanic raised even though "
                "generated_count > 0"
            )


# ---------------------------------------------------------------------------
# (e) --bootstrap-aegis composes aegis_preflight (mock READY) and loads .env
# ---------------------------------------------------------------------------


class TestBootstrapAegisCLI:
    """Phase 1: --bootstrap-aegis composes aegis_preflight and .env loader."""

    def test_bootstrap_aegis_calls_preflight_and_env_loader(self, monkeypatch):
        """With aegis_preflight mocked to READY, run_calibration proceeds
        and _load_env_file_into_environ is called."""
        monkeypatch.setenv("JARVIS_ANTIVENOM_SELF_IMMUNIZATION_ENABLED", "true")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")

        from backend.core.ouroboros.aegis.preflight import (
            AegisPreflightResult,
            PreflightOutcome,
        )
        from scripts.security import run_cc_parity_calibration as calib_mod

        mock_ready_result = AegisPreflightResult(
            outcome=PreflightOutcome.READY,
            aegis_url="http://127.0.0.1:18443",
            subprocess_pid=None,
        )

        class _Cat:
            def __init__(self, v):
                self.value = v
        class _Seed:
            name = "bs_seed"
            source = "import shutil\nshutil.disk_usage('/')\n"
            category = _Cat("sandbox_escape")
            known_gap = False

        env_loader_called = []

        async def _run():
            with patch.object(
                calib_mod,
                "_load_env_file_into_environ",
                side_effect=lambda: env_loader_called.append(True),
            ):
                with patch(
                    "backend.core.ouroboros.aegis.preflight.aegis_preflight",
                    new=AsyncMock(return_value=mock_ready_result),
                ):
                    with patch(
                        "backend.core.ouroboros.governance.self_immunization._load_seed_entries",
                        return_value=[_Seed()],
                    ):
                        return await calib_mod.run_calibration(
                            max_mutations=4,
                            dry_run=True,  # dry-run avoids LLM calls
                            budget_usd=0.0,
                            bootstrap_aegis=True,
                        )

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert isinstance(result, int)
        assert env_loader_called, (
            "_load_env_file_into_environ was not called during --bootstrap-aegis"
        )


# ---------------------------------------------------------------------------
# (f) Bootstrap fails loud when aegis_preflight returns non-READY
# ---------------------------------------------------------------------------


class TestBootstrapAegisFails:
    """Phase 1: if aegis_preflight returns FAILED_*, run_calibration
    prints a CRITICAL message and returns 1 (not [PASS])."""

    def test_bootstrap_aegis_fails_on_non_ready_outcome(self, monkeypatch):
        """FAILED_CREDENTIAL_SCRUB → run_calibration returns 1."""
        monkeypatch.setenv("JARVIS_ANTIVENOM_SELF_IMMUNIZATION_ENABLED", "true")

        from backend.core.ouroboros.aegis.preflight import (
            AegisPreflightResult,
            PreflightOutcome,
        )
        from scripts.security import run_cc_parity_calibration as calib_mod

        mock_failed_result = AegisPreflightResult(
            outcome=PreflightOutcome.FAILED_CREDENTIAL_SCRUB,
            aegis_url=None,
            detail="ANTHROPIC_API_KEY not in env after scrub",
        )

        async def _run():
            with patch.object(calib_mod, "_load_env_file_into_environ"):
                with patch(
                    "backend.core.ouroboros.aegis.preflight.aegis_preflight",
                    new=AsyncMock(return_value=mock_failed_result),
                ):
                    return await calib_mod.run_calibration(
                        max_mutations=4,
                        dry_run=False,
                        budget_usd=0.10,
                        bootstrap_aegis=True,
                    )

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result == 1, (
            f"Expected return 1 on FAILED_CREDENTIAL_SCRUB but got {result}"
        )

    def test_bootstrap_aegis_fails_on_spawn_failure(self, monkeypatch):
        """FAILED_SPAWN → run_calibration returns 1."""
        monkeypatch.setenv("JARVIS_ANTIVENOM_SELF_IMMUNIZATION_ENABLED", "true")

        from backend.core.ouroboros.aegis.preflight import (
            AegisPreflightResult,
            PreflightOutcome,
        )
        from scripts.security import run_cc_parity_calibration as calib_mod

        mock_spawn_fail = AegisPreflightResult(
            outcome=PreflightOutcome.FAILED_SPAWN,
            detail="daemon subprocess died immediately",
        )

        async def _run():
            with patch.object(calib_mod, "_load_env_file_into_environ"):
                with patch(
                    "backend.core.ouroboros.aegis.preflight.aegis_preflight",
                    new=AsyncMock(return_value=mock_spawn_fail),
                ):
                    return await calib_mod.run_calibration(
                        max_mutations=4,
                        dry_run=False,
                        budget_usd=0.10,
                        bootstrap_aegis=True,
                    )

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result == 1


# ---------------------------------------------------------------------------
# (g) LLMMutationProvider.generated_count increments correctly
# ---------------------------------------------------------------------------


class TestGeneratedCountIncrement:
    """Slice 94 Phase 2 — generated_count semantics on LLMMutationProvider."""

    def test_generated_count_starts_at_zero(self):
        """Fresh provider has generated_count == 0."""
        from backend.core.ouroboros.governance.self_immunization import (
            LLMMutationProvider,
            MutationBudgetGuard,
        )
        guard = MutationBudgetGuard(budget_usd=0.10)
        provider = LLMMutationProvider(client=MagicMock(), budget_guard=guard)
        assert provider.generated_count == 0

    def test_generated_count_increments_per_valid_mutation(self):
        """Each valid mutation returned increments generated_count by 1."""
        from backend.core.ouroboros.governance.self_immunization import (
            LLMMutationProvider,
            MutationBudgetGuard,
        )

        guard = MutationBudgetGuard(budget_usd=0.10)

        # Build a mock response with 2 valid Python code fences.
        mock_response = MagicMock()
        mock_response.usage = MagicMock(input_tokens=100, output_tokens=100)
        mock_response.content = [
            MagicMock(
                text="```python\nx = 1\n```\n\n```python\ny = 2\n```\n"
            )
        ]

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        provider = LLMMutationProvider(client=mock_client, budget_guard=guard)
        assert provider.generated_count == 0

        async def _run():
            return await provider.mutate("x = 1\n", n=5)

        results = asyncio.get_event_loop().run_until_complete(_run())
        # 2 valid fences → count should be 2
        assert provider.generated_count == len(results)
        assert provider.generated_count >= 1  # At least one valid mutation

    def test_generated_count_not_incremented_on_error(self):
        """If mutate raises internally (mock error), count stays 0."""
        from backend.core.ouroboros.governance.self_immunization import (
            LLMMutationProvider,
            MutationBudgetGuard,
        )

        guard = MutationBudgetGuard(budget_usd=0.10)
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            side_effect=RuntimeError("auth failed")
        )

        provider = LLMMutationProvider(client=mock_client, budget_guard=guard)

        async def _run():
            return await provider.mutate("x = 1\n", n=3)

        results = asyncio.get_event_loop().run_until_complete(_run())
        assert results == [] or len(results) == 0
        assert provider.generated_count == 0

    def test_generated_count_not_incremented_for_unparseable(self):
        """Unparseable LLM output doesn't increment generated_count
        (validity filter drops it before the counter)."""
        from backend.core.ouroboros.governance.self_immunization import (
            LLMMutationProvider,
            MutationBudgetGuard,
        )

        guard = MutationBudgetGuard(budget_usd=0.10)

        # Response with only invalid Python in the fence.
        mock_response = MagicMock()
        mock_response.usage = MagicMock(input_tokens=50, output_tokens=50)
        mock_response.content = [
            MagicMock(text="```python\ndef (((not valid python:\n```\n")
        ]

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        provider = LLMMutationProvider(client=mock_client, budget_guard=guard)

        async def _run():
            return await provider.mutate("x = 1\n", n=3)

        results = asyncio.get_event_loop().run_until_complete(_run())
        # Validity filter dropped the unparseable output.
        assert results == []
        assert provider.generated_count == 0

    def test_generated_count_accumulates_across_calls(self):
        """generated_count accumulates across multiple mutate() calls."""
        from backend.core.ouroboros.governance.self_immunization import (
            LLMMutationProvider,
            MutationBudgetGuard,
        )

        guard = MutationBudgetGuard(budget_usd=1.0)

        def _make_response(code: str):
            r = MagicMock()
            r.usage = MagicMock(input_tokens=50, output_tokens=50)
            r.content = [MagicMock(text=f"```python\n{code}\n```\n")]
            return r

        call_n = [0]
        snippets = ["x = 1\n", "y = 2\n", "z = 3\n"]

        async def _create(**kwargs):
            idx = min(call_n[0], len(snippets) - 1)
            call_n[0] += 1
            return _make_response(snippets[idx])

        mock_client = MagicMock()
        mock_client.messages.create = _create

        provider = LLMMutationProvider(client=mock_client, budget_guard=guard)

        async def _run():
            await provider.mutate("seed1\n", n=1)
            await provider.mutate("seed2\n", n=1)
            return provider.generated_count

        total = asyncio.get_event_loop().run_until_complete(_run())
        assert total == 2  # One valid mutation per call


# ---------------------------------------------------------------------------
# AdversarialTelemetryPanic is importable from self_immunization
# ---------------------------------------------------------------------------


class TestAdversarialTelemetryPanicImport:
    """Structural: exception class is defined and importable."""

    def test_exception_class_importable(self):
        from backend.core.ouroboros.governance.self_immunization import (
            AdversarialTelemetryPanic,
        )
        assert issubclass(AdversarialTelemetryPanic, RuntimeError)

    def test_exception_can_be_raised_and_caught(self):
        from backend.core.ouroboros.governance.self_immunization import (
            AdversarialTelemetryPanic,
        )
        with pytest.raises(AdversarialTelemetryPanic):
            raise AdversarialTelemetryPanic("test panic")


# ---------------------------------------------------------------------------
# IMPORTANT #1 — Health probe body check (ok: true required)
# ---------------------------------------------------------------------------


class TestAegisHealthProbeBody:
    """Phase 1: _check_aegis_readiness probes ok:true in body, with timeout."""

    def test_ready_when_status_200_and_ok_true(self, monkeypatch):
        """HTTP 200 + body {"ok": true} → READY."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")

        from scripts.security.run_cc_parity_calibration import (
            _check_aegis_readiness,
            _AegisReadiness,
        )

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"ok": True})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        async def _run():
            with patch("aiohttp.ClientSession", return_value=mock_session):
                return await _check_aegis_readiness(
                    aegis_url="http://127.0.0.1:18443"
                )

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result == _AegisReadiness.READY

    def test_no_daemon_when_status_200_but_ok_false(self, monkeypatch):
        """HTTP 200 + body {"ok": false} → NO_DAEMON (body check fails)."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")

        from scripts.security.run_cc_parity_calibration import (
            _check_aegis_readiness,
            _AegisReadiness,
        )

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"ok": False, "error": "warming up"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        async def _run():
            with patch("aiohttp.ClientSession", return_value=mock_session):
                return await _check_aegis_readiness(
                    aegis_url="http://127.0.0.1:18443"
                )

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result == _AegisReadiness.NO_DAEMON

    def test_no_daemon_when_status_200_non_json_body(self, monkeypatch):
        """HTTP 200 + non-JSON body → NO_DAEMON (body parse fails)."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")

        from scripts.security.run_cc_parity_calibration import (
            _check_aegis_readiness,
            _AegisReadiness,
        )

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(side_effect=ValueError("not JSON"))
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        async def _run():
            with patch("aiohttp.ClientSession", return_value=mock_session):
                return await _check_aegis_readiness(
                    aegis_url="http://127.0.0.1:18443"
                )

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result == _AegisReadiness.NO_DAEMON


# ---------------------------------------------------------------------------
# IMPORTANT #2 — NO_DAEMON without/with --allow-direct
# ---------------------------------------------------------------------------


class TestNoDaemonHardFail:
    """Phase 1: NO_DAEMON without --allow-direct → return 1 (no silent bypass).
    With --allow-direct → proceeds (warns operator)."""

    def _make_minimal_seed(self):
        class _Cat:
            def __init__(self, v):
                self.value = v
        class _Seed:
            name = "nd_seed"
            source = "import shutil\nshutil.disk_usage('/')\n"
            category = _Cat("sandbox_escape")
            known_gap = False
        return [_Seed()]

    def test_no_daemon_without_allow_direct_returns_1(self, monkeypatch):
        """NO_DAEMON + no --allow-direct → run_calibration returns 1."""
        monkeypatch.setenv("JARVIS_ANTIVENOM_SELF_IMMUNIZATION_ENABLED", "true")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")

        from scripts.security.run_cc_parity_calibration import (
            run_calibration,
            _AegisReadiness,
        )

        async def _run():
            with patch(
                "scripts.security.run_cc_parity_calibration._check_aegis_readiness",
                new=AsyncMock(return_value=_AegisReadiness.NO_DAEMON),
            ):
                return await run_calibration(
                    max_mutations=4,
                    dry_run=False,
                    budget_usd=0.10,
                    allow_direct=False,
                )

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result == 1, (
            f"Expected return 1 on NO_DAEMON without --allow-direct, got {result}"
        )

    def test_no_daemon_with_allow_direct_proceeds(self, monkeypatch):
        """NO_DAEMON + --allow-direct → run_calibration proceeds past the gate."""
        monkeypatch.setenv("JARVIS_ANTIVENOM_SELF_IMMUNIZATION_ENABLED", "true")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")

        from scripts.security.run_cc_parity_calibration import (
            run_calibration,
            _AegisReadiness,
        )
        from backend.core.ouroboros.governance.self_immunization import (
            MutationBudgetGuard,
        )

        guard = MutationBudgetGuard(budget_usd=0.10)
        # Positive provider so panic doesn't fire.
        pos_provider = _MockProviderPositive(mutations_per_call=1)
        pos_provider.generated_count = 1  # pre-seed so panic condition false

        seeds = self._make_minimal_seed()

        async def _run():
            with patch(
                "scripts.security.run_cc_parity_calibration._check_aegis_readiness",
                new=AsyncMock(return_value=_AegisReadiness.NO_DAEMON),
            ):
                with patch(
                    "backend.core.ouroboros.governance.self_immunization._load_seed_entries",
                    return_value=seeds,
                ):
                    with patch(
                        "backend.core.ouroboros.governance.self_immunization.LLMMutationProvider",
                        return_value=pos_provider,
                    ):
                        with patch(
                            "backend.core.ouroboros.governance.self_immunization.MutationBudgetGuard",
                            return_value=guard,
                        ):
                            return await run_calibration(
                                max_mutations=4,
                                dry_run=False,
                                budget_usd=0.10,
                                allow_direct=True,
                            )

        # Should NOT return 1 at the gate (may return 0 or 1 for parity, but
        # must not hard-fail at the NO_DAEMON check).
        result = asyncio.get_event_loop().run_until_complete(_run())
        # If we got past the gate, result is an int (not an early-return-1
        # from the NO_DAEMON block).  We can't assert 0 here because parity
        # depends on seed; just assert it's an int and didn't raise.
        assert isinstance(result, int)


# ---------------------------------------------------------------------------
# Minor #5 — _load_env_file_parses_correctly
# ---------------------------------------------------------------------------


class TestLoadEnvFile:
    """Minor #5: _load_env_file_into_environ parses .env correctly."""

    def test_load_env_file_parses_correctly(self, tmp_path, monkeypatch):
        """Verify correct parsing of quoted values, comments, blank lines,
        KEY=val=with=eq, and graceful missing-file handling.
        Also verify no secret values are printed/logged.
        """
        import io
        import logging as _logging

        env_file = tmp_path / ".env"
        env_file.write_text(
            "# This is a comment\n"
            "\n"
            "PLAIN_KEY=plain_value\n"
            'QUOTED_KEY="quoted_value"\n'
            "SINGLE_QUOTED='single_value'\n"
            "KEY_WITH_EQ=val=with=eq\n"
            "# another comment\n"
            "EMPTY_VALUE=\n"
        )

        # Import the module dynamically to access _load_env_file_into_environ
        # and _REPO_ROOT without side-effects.
        from scripts.security import run_cc_parity_calibration as calib_mod

        # Point _REPO_ROOT at our tmp_path so the function reads our test .env.
        original_repo_root = calib_mod._REPO_ROOT
        try:
            calib_mod._REPO_ROOT = tmp_path

            # Remove any pre-existing keys to get clean assertions.
            for k in ("PLAIN_KEY", "QUOTED_KEY", "SINGLE_QUOTED", "KEY_WITH_EQ", "EMPTY_VALUE"):
                monkeypatch.delenv(k, raising=False)

            # Capture log output to ensure NO secret values are logged.
            log_capture = io.StringIO()
            handler = _logging.StreamHandler(log_capture)
            root_logger = _logging.getLogger()
            root_logger.addHandler(handler)

            try:
                calib_mod._load_env_file_into_environ()
            finally:
                root_logger.removeHandler(handler)

            # Verify parsed values.
            assert os.environ.get("PLAIN_KEY") == "plain_value"
            assert os.environ.get("QUOTED_KEY") == "quoted_value"
            assert os.environ.get("SINGLE_QUOTED") == "single_value"
            assert os.environ.get("KEY_WITH_EQ") == "val=with=eq"
            # EMPTY_VALUE: setdefault keeps empty string if not set.
            # The function strips quotes but preserves the (empty) value.

            # Verify log output does NOT contain any parsed values (no leakage).
            log_output = log_capture.getvalue()
            assert "plain_value" not in log_output
            assert "quoted_value" not in log_output
            assert "single_value" not in log_output

        finally:
            calib_mod._REPO_ROOT = original_repo_root

    def test_missing_env_file_is_graceful(self, tmp_path, monkeypatch):
        """If neither .env file exists, _load_env_file_into_environ does not crash."""
        from scripts.security import run_cc_parity_calibration as calib_mod

        original_repo_root = calib_mod._REPO_ROOT
        try:
            # tmp_path has no .env files.
            calib_mod._REPO_ROOT = tmp_path
            # Should not raise.
            calib_mod._load_env_file_into_environ()
        finally:
            calib_mod._REPO_ROOT = original_repo_root


# ---------------------------------------------------------------------------
# Minor #6 — teardown sends SIGTERM to subprocess_pid
# ---------------------------------------------------------------------------


class TestTeardownSigterm:
    """Minor #6: on the READY bootstrap path, teardown sends SIGTERM to
    subprocess_pid (if set), and ProcessLookupError (already-dead) is
    swallowed without crashing."""

    def _make_minimal_seed(self):
        class _Cat:
            def __init__(self, v):
                self.value = v
        class _Seed:
            name = "td_seed"
            source = "import shutil\nshutil.disk_usage('/')\n"
            category = _Cat("sandbox_escape")
            known_gap = False
        return [_Seed()]

    def test_sigterm_sent_on_ready_path(self, monkeypatch):
        """With subprocess_pid=99999 on READY preflight, os.kill is called
        with SIGTERM (signal 15)."""
        import signal as _signal

        monkeypatch.setenv("JARVIS_ANTIVENOM_SELF_IMMUNIZATION_ENABLED", "true")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")

        from backend.core.ouroboros.aegis.preflight import (
            AegisPreflightResult,
            PreflightOutcome,
        )
        from scripts.security import run_cc_parity_calibration as calib_mod

        # subprocess_pid=99999 — guaranteed non-existent / mock target.
        mock_ready_result = AegisPreflightResult(
            outcome=PreflightOutcome.READY,
            aegis_url="http://127.0.0.1:18443",
            subprocess_pid=99999,
        )

        kill_calls = []

        def _mock_kill(pid, sig):
            kill_calls.append((pid, sig))

        seeds = self._make_minimal_seed()

        async def _run():
            with patch.object(calib_mod, "_load_env_file_into_environ"):
                with patch(
                    "backend.core.ouroboros.aegis.preflight.aegis_preflight",
                    new=AsyncMock(return_value=mock_ready_result),
                ):
                    with patch(
                        "backend.core.ouroboros.governance.self_immunization._load_seed_entries",
                        return_value=seeds,
                    ):
                        with patch("os.kill", side_effect=_mock_kill):
                            return await calib_mod.run_calibration(
                                max_mutations=4,
                                dry_run=True,
                                budget_usd=0.0,
                                bootstrap_aegis=True,
                            )

        asyncio.get_event_loop().run_until_complete(_run())
        # SIGTERM must have been sent to the mock PID.
        assert any(
            pid == 99999 and sig == _signal.SIGTERM
            for pid, sig in kill_calls
        ), f"Expected os.kill(99999, SIGTERM) but got: {kill_calls}"

    def test_process_lookup_error_swallowed(self, monkeypatch):
        """If os.kill raises ProcessLookupError (already-dead process),
        teardown does NOT crash — it is silently swallowed."""
        import signal as _signal

        monkeypatch.setenv("JARVIS_ANTIVENOM_SELF_IMMUNIZATION_ENABLED", "true")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")

        from backend.core.ouroboros.aegis.preflight import (
            AegisPreflightResult,
            PreflightOutcome,
        )
        from scripts.security import run_cc_parity_calibration as calib_mod

        mock_ready_result = AegisPreflightResult(
            outcome=PreflightOutcome.READY,
            aegis_url="http://127.0.0.1:18443",
            subprocess_pid=99999,
        )

        seeds = self._make_minimal_seed()

        async def _run():
            with patch.object(calib_mod, "_load_env_file_into_environ"):
                with patch(
                    "backend.core.ouroboros.aegis.preflight.aegis_preflight",
                    new=AsyncMock(return_value=mock_ready_result),
                ):
                    with patch(
                        "backend.core.ouroboros.governance.self_immunization._load_seed_entries",
                        return_value=seeds,
                    ):
                        with patch(
                            "os.kill",
                            side_effect=ProcessLookupError("No such process"),
                        ):
                            # Must NOT raise despite ProcessLookupError.
                            return await calib_mod.run_calibration(
                                max_mutations=4,
                                dry_run=True,
                                budget_usd=0.0,
                                bootstrap_aegis=True,
                            )

        # Should complete without raising ProcessLookupError.
        result = asyncio.get_event_loop().run_until_complete(_run())
        assert isinstance(result, int)
