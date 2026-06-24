"""Master Arming Switch tests (cross_repo_master_flag).

Tests the JARVIS_CROSS_REPO_MUTATION_ENABLED handshake gate:
  * flag unset / falsy / garbage -> NOT armed (byte-identical default)
  * flag=true + docker-alive + sinkhole-ok -> armed=True
  * flag=true + docker-DEAD -> armed=False, reason mentions docker
  * flag=true + sinkhole-not-configurable -> armed=False (degrade)
  * handshake exception -> armed=False (fail-soft/fail-CLOSED)
  * cross_repo_mutation_enabled() reflects armed
  * caching works + re-eval flag (JARVIS_CROSS_REPO_HANDSHAKE_CACHE)
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Fake injectable runner (no real Docker in tests)
# ---------------------------------------------------------------------------
class _FakeRunner:
    """Injectable fake runner for unit tests -- never calls Docker."""

    def __init__(
        self,
        *,
        docker_alive: bool = True,
        sinkhole_ok: bool = True,
    ) -> None:
        self.docker_alive = docker_alive
        self.sinkhole_ok = sinkhole_ok
        self.calls: list = []

    async def docker_info(self):
        self.calls.append("docker_info")
        from backend.core.ouroboros.governance.cross_repo_master_flag import RunResult
        return RunResult(
            returncode=0 if self.docker_alive else 1,
            stdout="Docker info" if self.docker_alive else "",
            stderr="" if self.docker_alive else "Cannot connect to Docker",
        )

    async def can_render_airgap(self) -> bool:
        self.calls.append("can_render_airgap")
        return self.sinkhole_ok


class _ExplodingRunner:
    """Runner that always raises -- simulates catastrophic failure."""

    async def docker_info(self):
        raise RuntimeError("simulated docker crash")

    async def can_render_airgap(self) -> bool:
        raise RuntimeError("simulated sinkhole crash")


# ---------------------------------------------------------------------------
# Autouse fixture: bust the module-level handshake cache between tests
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    import backend.core.ouroboros.governance.cross_repo_master_flag as mod
    monkeypatch.setattr(mod, "_cached_handshake", None, raising=False)
    yield
    monkeypatch.setattr(mod, "_cached_handshake", None, raising=False)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _handshake(runner, *, flag_value, monkeypatch):
    """Set env and run_arming_handshake with given flag value and runner."""
    import backend.core.ouroboros.governance.cross_repo_master_flag as mod
    if flag_value is None:
        monkeypatch.delenv("JARVIS_CROSS_REPO_MUTATION_ENABLED", raising=False)
    else:
        monkeypatch.setenv("JARVIS_CROSS_REPO_MUTATION_ENABLED", flag_value)
    return _run(mod.run_arming_handshake(runner=runner))


# ===========================================================================
# 1. flag unset -> NOT armed (byte-identical default)
# ===========================================================================
def test_flag_unset_not_armed(monkeypatch):
    monkeypatch.delenv("JARVIS_CROSS_REPO_MUTATION_ENABLED", raising=False)
    runner = _FakeRunner(docker_alive=True, sinkhole_ok=True)
    result = _handshake(runner, flag_value=None, monkeypatch=monkeypatch)
    assert result.armed is False
    assert result.flag_set is False


# ===========================================================================
# 2. flag=garbage/"maybe"/"2" -> NOT armed (fail-CLOSED)
#    also verifies falsy values (false/0/no/off) do NOT arm
# ===========================================================================
@pytest.mark.parametrize("bad_val", [
    "garbage", "maybe", "2", "TRUE1", "FALSE", "0", "off", "no", "false",
    "  ", "1.5", "GARBAGE",
])
def test_garbage_and_falsy_not_armed(monkeypatch, bad_val):
    import backend.core.ouroboros.governance.cross_repo_master_flag as mod
    monkeypatch.setattr(mod, "_cached_handshake", None, raising=False)
    runner = _FakeRunner(docker_alive=True, sinkhole_ok=True)
    result = _handshake(runner, flag_value=bad_val, monkeypatch=monkeypatch)
    assert result.armed is False, f"Expected NOT armed for flag={bad_val!r}"
    assert result.flag_set is False, f"Expected flag_set=False for flag={bad_val!r}"


# ===========================================================================
# 3. flag=true + docker-alive + sinkhole-ok -> armed=True
# ===========================================================================
def test_flag_true_docker_alive_sinkhole_ok_armed(monkeypatch):
    runner = _FakeRunner(docker_alive=True, sinkhole_ok=True)
    result = _handshake(runner, flag_value="true", monkeypatch=monkeypatch)
    assert result.armed is True
    assert result.flag_set is True
    assert result.docker_alive is True
    assert result.sinkhole_configurable is True


@pytest.mark.parametrize("truthy_val", ["1", "true", "yes", "on", "TRUE", "YES", "ON", "True"])
def test_all_truthy_values_arm(monkeypatch, truthy_val):
    import backend.core.ouroboros.governance.cross_repo_master_flag as mod
    monkeypatch.setattr(mod, "_cached_handshake", None, raising=False)
    runner = _FakeRunner(docker_alive=True, sinkhole_ok=True)
    result = _handshake(runner, flag_value=truthy_val, monkeypatch=monkeypatch)
    assert result.armed is True, f"Expected armed for flag={truthy_val!r}"
    assert result.flag_set is True


# ===========================================================================
# 4. flag=true + docker-DEAD -> armed=False, reason mentions "docker"
# ===========================================================================
def test_flag_true_docker_dead_not_armed(monkeypatch):
    runner = _FakeRunner(docker_alive=False, sinkhole_ok=True)
    result = _handshake(runner, flag_value="true", monkeypatch=monkeypatch)
    assert result.armed is False
    assert result.flag_set is True
    assert result.docker_alive is False
    assert "docker" in result.reason.lower()


# ===========================================================================
# 5. flag=true + sinkhole-not-configurable -> armed=False (degrade)
# ===========================================================================
def test_flag_true_sinkhole_not_configurable_not_armed(monkeypatch):
    runner = _FakeRunner(docker_alive=True, sinkhole_ok=False)
    result = _handshake(runner, flag_value="true", monkeypatch=monkeypatch)
    assert result.armed is False
    assert result.flag_set is True
    assert result.docker_alive is True
    assert result.sinkhole_configurable is False
    assert result.reason


# ===========================================================================
# 6. handshake exception -> armed=False (fail-soft / fail-CLOSED)
# ===========================================================================
def test_handshake_exception_not_armed(monkeypatch):
    monkeypatch.setenv("JARVIS_CROSS_REPO_MUTATION_ENABLED", "true")
    import backend.core.ouroboros.governance.cross_repo_master_flag as mod
    result = _run(mod.run_arming_handshake(runner=_ExplodingRunner()))
    assert result.armed is False
    assert "error" in result.reason.lower() or result.reason


# ===========================================================================
# 7. cross_repo_mutation_enabled() reflects armed
# ===========================================================================
def test_mutation_enabled_reflects_armed_true(monkeypatch):
    import backend.core.ouroboros.governance.cross_repo_master_flag as mod
    monkeypatch.setenv("JARVIS_CROSS_REPO_MUTATION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CROSS_REPO_HANDSHAKE_CACHE", "0")
    runner = _FakeRunner(docker_alive=True, sinkhole_ok=True)
    with patch.object(mod, "_make_default_runner", return_value=runner):
        enabled = mod.cross_repo_mutation_enabled()
    assert enabled is True


def test_mutation_enabled_reflects_armed_false(monkeypatch):
    import backend.core.ouroboros.governance.cross_repo_master_flag as mod
    monkeypatch.delenv("JARVIS_CROSS_REPO_MUTATION_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_CROSS_REPO_HANDSHAKE_CACHE", "0")
    runner = _FakeRunner(docker_alive=True, sinkhole_ok=True)
    with patch.object(mod, "_make_default_runner", return_value=runner):
        enabled = mod.cross_repo_mutation_enabled()
    assert enabled is False


# ===========================================================================
# 8. caching works + re-eval flag (JARVIS_CROSS_REPO_HANDSHAKE_CACHE)
# ===========================================================================
def test_caching_returns_same_result(monkeypatch):
    """With caching enabled (default), handshake runs once and is reused."""
    import backend.core.ouroboros.governance.cross_repo_master_flag as mod
    monkeypatch.setenv("JARVIS_CROSS_REPO_MUTATION_ENABLED", "true")
    monkeypatch.delenv("JARVIS_CROSS_REPO_HANDSHAKE_CACHE", raising=False)
    runner = _FakeRunner(docker_alive=True, sinkhole_ok=True)
    with patch.object(mod, "_make_default_runner", return_value=runner):
        r1 = mod.cross_repo_mutation_enabled()
        # Poison the runner -- re-run would give different result
        runner.docker_alive = False
        r2 = mod.cross_repo_mutation_enabled()
    assert r1 is True
    assert r2 is True  # cache held, not re-evaluated


def test_cache_disabled_re_evals(monkeypatch):
    """With JARVIS_CROSS_REPO_HANDSHAKE_CACHE=0, each call re-evaluates."""
    import backend.core.ouroboros.governance.cross_repo_master_flag as mod
    monkeypatch.setenv("JARVIS_CROSS_REPO_MUTATION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CROSS_REPO_HANDSHAKE_CACHE", "0")
    runner = _FakeRunner(docker_alive=True, sinkhole_ok=True)
    with patch.object(mod, "_make_default_runner", return_value=runner):
        r1 = mod.cross_repo_mutation_enabled()
        assert r1 is True
        runner.docker_alive = False
        r2 = mod.cross_repo_mutation_enabled()
    assert r2 is False  # re-evaluated, docker now dead


# ===========================================================================
# 9. ArmingHandshake.to_dict() method coverage
# ===========================================================================
def test_arming_handshake_to_dict(monkeypatch):
    runner = _FakeRunner(docker_alive=True, sinkhole_ok=True)
    result = _handshake(runner, flag_value="true", monkeypatch=monkeypatch)
    d = result.to_dict()
    assert isinstance(d, dict)
    assert "armed" in d
    assert "flag_set" in d
    assert "docker_alive" in d
    assert "sinkhole_configurable" in d
    assert "reason" in d
    assert d["armed"] is True


# ===========================================================================
# 10. arming_status() returns an ArmingHandshake
# ===========================================================================
def test_arming_status_returns_handshake(monkeypatch):
    import backend.core.ouroboros.governance.cross_repo_master_flag as mod
    monkeypatch.setenv("JARVIS_CROSS_REPO_MUTATION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CROSS_REPO_HANDSHAKE_CACHE", "0")
    runner = _FakeRunner(docker_alive=True, sinkhole_ok=True)
    with patch.object(mod, "_make_default_runner", return_value=runner):
        status = mod.arming_status()
    assert hasattr(status, "armed")
    assert hasattr(status, "flag_set")
    assert hasattr(status, "docker_alive")
    assert hasattr(status, "sinkhole_configurable")
    assert hasattr(status, "reason")
    assert status.armed is True


# ===========================================================================
# 11. log_arming_status_on_boot() does not raise
# ===========================================================================
def test_log_arming_status_on_boot_does_not_raise(monkeypatch):
    import backend.core.ouroboros.governance.cross_repo_master_flag as mod
    monkeypatch.setenv("JARVIS_CROSS_REPO_MUTATION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CROSS_REPO_HANDSHAKE_CACHE", "0")
    runner = _FakeRunner(docker_alive=True, sinkhole_ok=True)
    with patch.object(mod, "_make_default_runner", return_value=runner):
        mod.log_arming_status_on_boot()


def test_log_arming_status_on_boot_does_not_raise_degraded(monkeypatch):
    import backend.core.ouroboros.governance.cross_repo_master_flag as mod
    monkeypatch.setenv("JARVIS_CROSS_REPO_MUTATION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CROSS_REPO_HANDSHAKE_CACHE", "0")
    runner = _FakeRunner(docker_alive=False, sinkhole_ok=False)
    with patch.object(mod, "_make_default_runner", return_value=runner):
        mod.log_arming_status_on_boot()
