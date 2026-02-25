# tests/unit/core/test_locality_drivers.py
"""Tests for Locality Drivers -- InProcess, Subprocess, Remote."""

from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# Import sanity
# ---------------------------------------------------------------------------

class TestDriverImports:
    def test_module_imports(self):
        from backend.core.locality_drivers import InProcessDriver
        assert InProcessDriver is not None

    def test_required_exports(self):
        import backend.core.locality_drivers as mod
        assert hasattr(mod, "InProcessDriver")
        assert hasattr(mod, "SubprocessDriver")
        assert hasattr(mod, "RemoteDriver")

    def test_all_exports(self):
        from backend.core.locality_drivers import __all__
        assert "InProcessDriver" in __all__
        assert "SubprocessDriver" in __all__
        assert "RemoteDriver" in __all__


# ---------------------------------------------------------------------------
# InProcessDriver
# ---------------------------------------------------------------------------

class TestInProcessDriver:
    async def test_start_calls_registered_starter(self):
        from backend.core.locality_drivers import InProcessDriver
        driver = InProcessDriver()
        starter = AsyncMock(return_value=True)
        stopper = AsyncMock(return_value=True)
        driver.register("comp_a", start_fn=starter, stop_fn=stopper)
        result = await driver.start("comp_a")
        assert result is True
        starter.assert_awaited_once()

    async def test_stop_calls_registered_stopper(self):
        from backend.core.locality_drivers import InProcessDriver
        driver = InProcessDriver()
        starter = AsyncMock(return_value=True)
        stopper = AsyncMock(return_value=True)
        driver.register("comp_a", start_fn=starter, stop_fn=stopper)
        await driver.start("comp_a")
        result = await driver.stop("comp_a")
        assert result is True
        stopper.assert_awaited_once()

    async def test_start_unregistered_returns_false(self):
        from backend.core.locality_drivers import InProcessDriver
        driver = InProcessDriver()
        result = await driver.start("nonexistent")
        assert result is False

    async def test_stop_unregistered_returns_false(self):
        from backend.core.locality_drivers import InProcessDriver
        driver = InProcessDriver()
        result = await driver.stop("nonexistent")
        assert result is False

    async def test_health_check_default_uses_running_state(self):
        from backend.core.locality_drivers import InProcessDriver
        driver = InProcessDriver()
        starter = AsyncMock(return_value=True)
        stopper = AsyncMock(return_value=True)
        driver.register("comp_a", start_fn=starter, stop_fn=stopper)

        # Before start
        health = await driver.health_check("comp_a")
        assert health["healthy"] is False

        # After start
        await driver.start("comp_a")
        health = await driver.health_check("comp_a")
        assert health["healthy"] is True

    async def test_health_check_custom_function(self):
        from backend.core.locality_drivers import InProcessDriver
        driver = InProcessDriver()
        health_fn = AsyncMock(return_value={"healthy": True, "load": 0.5})
        driver.register(
            "comp_a",
            start_fn=AsyncMock(return_value=True),
            stop_fn=AsyncMock(return_value=True),
            health_fn=health_fn,
        )
        health = await driver.health_check("comp_a")
        assert health["healthy"] is True
        assert health["load"] == 0.5

    async def test_health_check_custom_function_exception(self):
        from backend.core.locality_drivers import InProcessDriver
        driver = InProcessDriver()
        health_fn = AsyncMock(side_effect=RuntimeError("probe failed"))
        driver.register(
            "comp_a",
            start_fn=AsyncMock(return_value=True),
            stop_fn=AsyncMock(return_value=True),
            health_fn=health_fn,
        )
        health = await driver.health_check("comp_a")
        assert health["healthy"] is False
        assert "probe failed" in health["error"]

    async def test_start_exception_returns_false(self):
        from backend.core.locality_drivers import InProcessDriver
        driver = InProcessDriver()
        starter = AsyncMock(side_effect=RuntimeError("init failed"))
        driver.register("comp_a", start_fn=starter, stop_fn=AsyncMock())
        result = await driver.start("comp_a")
        assert result is False

    async def test_stop_exception_returns_false(self):
        from backend.core.locality_drivers import InProcessDriver
        driver = InProcessDriver()
        stopper = AsyncMock(side_effect=RuntimeError("shutdown failed"))
        driver.register("comp_a", start_fn=AsyncMock(return_value=True), stop_fn=stopper)
        await driver.start("comp_a")
        result = await driver.stop("comp_a")
        assert result is False

    async def test_send_drain_delegates_to_stop(self):
        from backend.core.locality_drivers import InProcessDriver
        driver = InProcessDriver()
        stopper = AsyncMock(return_value=True)
        driver.register("comp_a", start_fn=AsyncMock(return_value=True), stop_fn=stopper)
        result = await driver.send_drain("comp_a")
        assert result is True
        stopper.assert_awaited_once()

    async def test_start_false_result_tracks_not_running(self):
        from backend.core.locality_drivers import InProcessDriver
        driver = InProcessDriver()
        starter = AsyncMock(return_value=False)
        driver.register("comp_a", start_fn=starter, stop_fn=AsyncMock())
        result = await driver.start("comp_a")
        assert result is False
        health = await driver.health_check("comp_a")
        assert health["healthy"] is False

    async def test_health_check_unknown_component(self):
        from backend.core.locality_drivers import InProcessDriver
        driver = InProcessDriver()
        health = await driver.health_check("unknown")
        assert health["healthy"] is False


# ---------------------------------------------------------------------------
# SubprocessDriver
# ---------------------------------------------------------------------------

class TestSubprocessDriver:
    async def test_start_calls_spawn_fn(self):
        from backend.core.locality_drivers import SubprocessDriver
        driver = SubprocessDriver()
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.pid = 12345
        spawn_fn = AsyncMock(return_value=mock_proc)
        driver.register("comp_a", spawn_fn=spawn_fn)
        result = await driver.start("comp_a")
        assert result is True
        spawn_fn.assert_awaited_once()

    async def test_start_unregistered_returns_false(self):
        from backend.core.locality_drivers import SubprocessDriver
        driver = SubprocessDriver()
        result = await driver.start("nonexistent")
        assert result is False

    async def test_start_spawn_returns_none(self):
        from backend.core.locality_drivers import SubprocessDriver
        driver = SubprocessDriver()
        spawn_fn = AsyncMock(return_value=None)
        driver.register("comp_a", spawn_fn=spawn_fn)
        result = await driver.start("comp_a")
        assert result is False

    async def test_start_spawn_exception(self):
        from backend.core.locality_drivers import SubprocessDriver
        driver = SubprocessDriver()
        spawn_fn = AsyncMock(side_effect=OSError("exec failed"))
        driver.register("comp_a", spawn_fn=spawn_fn)
        result = await driver.start("comp_a")
        assert result is False

    async def test_stop_terminates_process(self):
        from backend.core.locality_drivers import SubprocessDriver
        driver = SubprocessDriver()
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.pid = 12345
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)
        spawn_fn = AsyncMock(return_value=mock_proc)
        driver.register("comp_a", spawn_fn=spawn_fn)
        await driver.start("comp_a")
        result = await driver.stop("comp_a")
        assert result is True
        mock_proc.terminate.assert_called_once()

    async def test_stop_already_stopped(self):
        from backend.core.locality_drivers import SubprocessDriver
        driver = SubprocessDriver()
        result = await driver.stop("comp_a")
        assert result is True  # Already stopped is success

    async def test_health_check_process_not_running(self):
        from backend.core.locality_drivers import SubprocessDriver
        driver = SubprocessDriver()
        health = await driver.health_check("comp_a")
        assert health["healthy"] is False
        assert health["error"] == "process_not_running"

    async def test_health_check_process_exited(self):
        from backend.core.locality_drivers import SubprocessDriver
        driver = SubprocessDriver()
        mock_proc = MagicMock()
        mock_proc.returncode = 1  # exited
        mock_proc.pid = 12345
        driver._processes["comp_a"] = mock_proc
        health = await driver.health_check("comp_a")
        assert health["healthy"] is False

    async def test_health_check_process_running_no_url(self):
        from backend.core.locality_drivers import SubprocessDriver
        driver = SubprocessDriver()
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.pid = 12345
        driver._processes["comp_a"] = mock_proc
        health = await driver.health_check("comp_a")
        assert health["healthy"] is True
        assert health["pid"] == 12345

    async def test_register_with_health_url(self):
        from backend.core.locality_drivers import SubprocessDriver
        driver = SubprocessDriver()
        driver.register("comp_a", spawn_fn=AsyncMock(), health_url="http://localhost:8080/health")
        assert driver._health_urls["comp_a"] == "http://localhost:8080/health"

    async def test_send_drain_no_health_url_falls_back_to_stop(self):
        from backend.core.locality_drivers import SubprocessDriver
        driver = SubprocessDriver()
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.pid = 12345
        mock_proc.terminate = MagicMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)
        driver._processes["comp_a"] = mock_proc
        driver.register("comp_a", spawn_fn=AsyncMock())
        result = await driver.send_drain("comp_a")
        assert result is True
        mock_proc.terminate.assert_called_once()


# ---------------------------------------------------------------------------
# RemoteDriver
# ---------------------------------------------------------------------------

class TestRemoteDriver:
    async def test_start_calls_registered_fn(self):
        from backend.core.locality_drivers import RemoteDriver
        driver = RemoteDriver()
        start_fn = AsyncMock(return_value=True)
        stop_fn = AsyncMock(return_value=True)
        driver.register("comp_a", start_fn=start_fn, stop_fn=stop_fn)
        result = await driver.start("comp_a")
        assert result is True
        start_fn.assert_awaited_once()

    async def test_start_unregistered_returns_false(self):
        from backend.core.locality_drivers import RemoteDriver
        driver = RemoteDriver()
        result = await driver.start("nonexistent")
        assert result is False

    async def test_start_exception_returns_false(self):
        from backend.core.locality_drivers import RemoteDriver
        driver = RemoteDriver()
        start_fn = AsyncMock(side_effect=RuntimeError("VM provision failed"))
        driver.register("comp_a", start_fn=start_fn, stop_fn=AsyncMock())
        result = await driver.start("comp_a")
        assert result is False

    async def test_stop_calls_registered_fn(self):
        from backend.core.locality_drivers import RemoteDriver
        driver = RemoteDriver()
        start_fn = AsyncMock(return_value=True)
        stop_fn = AsyncMock(return_value=True)
        driver.register("comp_a", start_fn=start_fn, stop_fn=stop_fn)
        result = await driver.stop("comp_a")
        assert result is True
        stop_fn.assert_awaited_once()

    async def test_stop_unregistered_returns_false(self):
        from backend.core.locality_drivers import RemoteDriver
        driver = RemoteDriver()
        result = await driver.stop("nonexistent")
        assert result is False

    async def test_stop_clears_endpoint(self):
        from backend.core.locality_drivers import RemoteDriver
        driver = RemoteDriver()
        stop_fn = AsyncMock(return_value=True)
        driver.register("comp_a", start_fn=AsyncMock(), stop_fn=stop_fn)
        driver.set_endpoint("comp_a", "http://10.0.0.1:8001")
        assert "comp_a" in driver._endpoints
        await driver.stop("comp_a")
        assert "comp_a" not in driver._endpoints

    async def test_stop_exception_returns_false(self):
        from backend.core.locality_drivers import RemoteDriver
        driver = RemoteDriver()
        stop_fn = AsyncMock(side_effect=RuntimeError("VM delete failed"))
        driver.register("comp_a", start_fn=AsyncMock(), stop_fn=stop_fn)
        result = await driver.stop("comp_a")
        assert result is False

    async def test_set_endpoint(self):
        from backend.core.locality_drivers import RemoteDriver
        driver = RemoteDriver()
        driver.set_endpoint("comp_a", "http://10.0.0.1:8001")
        assert driver._endpoints["comp_a"] == "http://10.0.0.1:8001"

    async def test_health_check_no_endpoint(self):
        from backend.core.locality_drivers import RemoteDriver
        driver = RemoteDriver()
        health = await driver.health_check("comp_a")
        assert health["healthy"] is False
        assert health["error"] == "no_endpoint"

    async def test_health_check_uses_health_url_first(self):
        from backend.core.locality_drivers import RemoteDriver
        driver = RemoteDriver()
        driver.register(
            "comp_a",
            start_fn=AsyncMock(),
            stop_fn=AsyncMock(),
            health_url="http://10.0.0.1:8001/health",
        )
        # Without mocking aiohttp, the health check will raise and return error dict
        health = await driver.health_check("comp_a")
        # We just verify it tried (got an error, not "no_endpoint")
        assert health.get("error") != "no_endpoint"

    async def test_health_check_falls_back_to_endpoint(self):
        from backend.core.locality_drivers import RemoteDriver
        driver = RemoteDriver()
        driver.set_endpoint("comp_a", "http://10.0.0.1:8001")
        # Without mocking aiohttp, the health check will raise and return error dict
        health = await driver.health_check("comp_a")
        assert health.get("error") != "no_endpoint"

    async def test_send_drain_no_endpoint_returns_false(self):
        from backend.core.locality_drivers import RemoteDriver
        driver = RemoteDriver()
        result = await driver.send_drain("comp_a")
        assert result is False
