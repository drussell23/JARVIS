"""Tests for PhantomHardwareManager resolution control methods."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestSetResolution:
    @pytest.mark.asyncio
    async def test_set_resolution_calls_cli(self):
        from backend.system.phantom_hardware_manager import PhantomHardwareManager

        mgr = PhantomHardwareManager.__new__(PhantomHardwareManager)
        mgr._cached_cli_path = "/usr/local/bin/betterdisplaycli"
        mgr.ghost_display_name = "JARVIS_GHOST"
        mgr._ghost_display_info = MagicMock(resolution="1920x1080")
        mgr._stats = {"resolution_changes": 0}

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"OK", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            result = await mgr.set_resolution_async("1280x720")
            assert result is True
            mock_exec.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_resolution_idempotent(self):
        from backend.system.phantom_hardware_manager import PhantomHardwareManager

        mgr = PhantomHardwareManager.__new__(PhantomHardwareManager)
        mgr._cached_cli_path = "/usr/local/bin/betterdisplaycli"
        mgr.ghost_display_name = "JARVIS_GHOST"
        mgr._ghost_display_info = MagicMock(resolution="1280x720")
        mgr._stats = {"resolution_changes": 0}

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            result = await mgr.set_resolution_async("1280x720")
            assert result is True
            mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_set_resolution_no_cli_returns_false(self):
        from backend.system.phantom_hardware_manager import PhantomHardwareManager

        mgr = PhantomHardwareManager.__new__(PhantomHardwareManager)
        mgr._cached_cli_path = None
        mgr.ghost_display_name = "JARVIS_GHOST"
        mgr._ghost_display_info = MagicMock(resolution="1920x1080")
        mgr._stats = {"resolution_changes": 0}
        result = await mgr.set_resolution_async("1280x720")
        assert result is False

    @pytest.mark.asyncio
    async def test_set_resolution_updates_info_and_stats(self):
        from backend.system.phantom_hardware_manager import PhantomHardwareManager

        mgr = PhantomHardwareManager.__new__(PhantomHardwareManager)
        mgr._cached_cli_path = "/usr/local/bin/betterdisplaycli"
        mgr.ghost_display_name = "JARVIS_GHOST"
        mgr._ghost_display_info = MagicMock(resolution="1920x1080")
        mgr._stats = {"resolution_changes": 0}

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"OK", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await mgr.set_resolution_async("1280x720")
            assert mgr._ghost_display_info.resolution == "1280x720"
            assert mgr._stats["resolution_changes"] == 1

    @pytest.mark.asyncio
    async def test_set_resolution_handles_timeout(self):
        from backend.system.phantom_hardware_manager import PhantomHardwareManager

        mgr = PhantomHardwareManager.__new__(PhantomHardwareManager)
        mgr._cached_cli_path = "/usr/local/bin/betterdisplaycli"
        mgr.ghost_display_name = "JARVIS_GHOST"
        mgr._ghost_display_info = MagicMock(resolution="1920x1080")
        mgr._stats = {"resolution_changes": 0}

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
                result = await mgr.set_resolution_async("1280x720")
                assert result is False

    @pytest.mark.asyncio
    async def test_set_resolution_nonzero_returncode(self):
        from backend.system.phantom_hardware_manager import PhantomHardwareManager

        mgr = PhantomHardwareManager.__new__(PhantomHardwareManager)
        mgr._cached_cli_path = "/usr/local/bin/betterdisplaycli"
        mgr.ghost_display_name = "JARVIS_GHOST"
        mgr._ghost_display_info = MagicMock(resolution="1920x1080")
        mgr._stats = {"resolution_changes": 0}

        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"error", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await mgr.set_resolution_async("1280x720")
            assert result is False
            assert mgr._stats["resolution_changes"] == 0


class TestDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_calls_cli(self):
        from backend.system.phantom_hardware_manager import PhantomHardwareManager

        mgr = PhantomHardwareManager.__new__(PhantomHardwareManager)
        mgr._cached_cli_path = "/usr/local/bin/betterdisplaycli"
        mgr.ghost_display_name = "JARVIS_GHOST"
        mgr._ghost_display_info = MagicMock(is_active=True)
        mgr._stats = {"disconnects": 0}

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"OK", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await mgr.disconnect_async()
            assert result is True

    @pytest.mark.asyncio
    async def test_disconnect_idempotent(self):
        from backend.system.phantom_hardware_manager import PhantomHardwareManager

        mgr = PhantomHardwareManager.__new__(PhantomHardwareManager)
        mgr._cached_cli_path = "/usr/local/bin/betterdisplaycli"
        mgr.ghost_display_name = "JARVIS_GHOST"
        mgr._ghost_display_info = MagicMock(is_active=False)
        mgr._stats = {"disconnects": 0}

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            result = await mgr.disconnect_async()
            assert result is True
            mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_disconnect_updates_state_and_stats(self):
        from backend.system.phantom_hardware_manager import PhantomHardwareManager

        mgr = PhantomHardwareManager.__new__(PhantomHardwareManager)
        mgr._cached_cli_path = "/usr/local/bin/betterdisplaycli"
        mgr.ghost_display_name = "JARVIS_GHOST"
        mgr._ghost_display_info = MagicMock(is_active=True)
        mgr._stats = {"disconnects": 0}

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"OK", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await mgr.disconnect_async()
            assert mgr._ghost_display_info.is_active is False
            assert mgr._stats["disconnects"] == 1

    @pytest.mark.asyncio
    async def test_disconnect_no_cli_returns_false(self):
        from backend.system.phantom_hardware_manager import PhantomHardwareManager

        mgr = PhantomHardwareManager.__new__(PhantomHardwareManager)
        mgr._cached_cli_path = None
        mgr.ghost_display_name = "JARVIS_GHOST"
        mgr._ghost_display_info = MagicMock(is_active=True)
        mgr._stats = {"disconnects": 0}
        result = await mgr.disconnect_async()
        assert result is False


class TestReconnect:
    @pytest.mark.asyncio
    async def test_reconnect_calls_connect(self):
        from backend.system.phantom_hardware_manager import PhantomHardwareManager

        mgr = PhantomHardwareManager.__new__(PhantomHardwareManager)
        mgr._cached_cli_path = "/usr/local/bin/betterdisplaycli"
        mgr.ghost_display_name = "JARVIS_GHOST"
        mgr._ghost_display_info = MagicMock(is_active=False, resolution="")
        mgr._stats = {"reconnects": 0, "resolution_changes": 0}

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"OK", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await mgr.reconnect_async("1024x576")
            assert result is True

    @pytest.mark.asyncio
    async def test_reconnect_updates_state_and_stats(self):
        from backend.system.phantom_hardware_manager import PhantomHardwareManager

        mgr = PhantomHardwareManager.__new__(PhantomHardwareManager)
        mgr._cached_cli_path = "/usr/local/bin/betterdisplaycli"
        mgr.ghost_display_name = "JARVIS_GHOST"
        mgr._ghost_display_info = MagicMock(is_active=False, resolution="")
        mgr._stats = {"reconnects": 0, "resolution_changes": 0}

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"OK", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await mgr.reconnect_async()
            assert mgr._ghost_display_info.is_active is True
            assert mgr._stats["reconnects"] == 1

    @pytest.mark.asyncio
    async def test_reconnect_with_resolution(self):
        from backend.system.phantom_hardware_manager import PhantomHardwareManager

        mgr = PhantomHardwareManager.__new__(PhantomHardwareManager)
        mgr._cached_cli_path = "/usr/local/bin/betterdisplaycli"
        mgr.ghost_display_name = "JARVIS_GHOST"
        mgr._ghost_display_info = MagicMock(is_active=False, resolution="1920x1080")
        mgr._stats = {"reconnects": 0, "resolution_changes": 0}

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"OK", b""))

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await mgr.reconnect_async("1024x576")
            assert result is True
            # Should have called both reconnect and set_resolution
            assert mgr._ghost_display_info.resolution == "1024x576"
            assert mgr._stats["resolution_changes"] == 1

    @pytest.mark.asyncio
    async def test_reconnect_no_cli_returns_false(self):
        from backend.system.phantom_hardware_manager import PhantomHardwareManager

        mgr = PhantomHardwareManager.__new__(PhantomHardwareManager)
        mgr._cached_cli_path = None
        mgr.ghost_display_name = "JARVIS_GHOST"
        mgr._ghost_display_info = MagicMock(is_active=False)
        mgr._stats = {"reconnects": 0}
        result = await mgr.reconnect_async()
        assert result is False


class TestGetCurrentMode:
    @pytest.mark.asyncio
    async def test_returns_resolution_dict(self):
        from backend.system.phantom_hardware_manager import PhantomHardwareManager

        mgr = PhantomHardwareManager.__new__(PhantomHardwareManager)
        mgr._cached_cli_path = "/usr/local/bin/betterdisplaycli"
        mgr.ghost_display_name = "JARVIS_GHOST"
        mgr._ghost_display_info = MagicMock(is_active=True, resolution="1920x1080")

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(b"resolution: 1920x1080\nconnected: true\n", b"")
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            mode = await mgr.get_current_mode_async()
            assert "resolution" in mode
            assert "connected" in mode
            assert mode["resolution"] == "1920x1080"
            assert mode["connected"] is True

    @pytest.mark.asyncio
    async def test_returns_defaults_on_error(self):
        from backend.system.phantom_hardware_manager import PhantomHardwareManager

        mgr = PhantomHardwareManager.__new__(PhantomHardwareManager)
        mgr._cached_cli_path = "/usr/local/bin/betterdisplaycli"
        mgr.ghost_display_name = "JARVIS_GHOST"
        mgr._ghost_display_info = MagicMock()

        with patch("asyncio.create_subprocess_exec", side_effect=Exception("boom")):
            mode = await mgr.get_current_mode_async()
            assert mode["resolution"] == "unknown"
            assert mode["connected"] is False

    @pytest.mark.asyncio
    async def test_returns_defaults_no_cli(self):
        from backend.system.phantom_hardware_manager import PhantomHardwareManager

        mgr = PhantomHardwareManager.__new__(PhantomHardwareManager)
        mgr._cached_cli_path = None
        mgr.ghost_display_name = "JARVIS_GHOST"
        mgr._ghost_display_info = MagicMock()

        mode = await mgr.get_current_mode_async()
        assert mode["resolution"] == "unknown"
        assert mode["connected"] is False

    @pytest.mark.asyncio
    async def test_parses_connected_false(self):
        from backend.system.phantom_hardware_manager import PhantomHardwareManager

        mgr = PhantomHardwareManager.__new__(PhantomHardwareManager)
        mgr._cached_cli_path = "/usr/local/bin/betterdisplaycli"
        mgr.ghost_display_name = "JARVIS_GHOST"
        mgr._ghost_display_info = MagicMock()

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(b"resolution: 1024x576\nconnected: false\n", b"")
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            mode = await mgr.get_current_mode_async()
            assert mode["resolution"] == "1024x576"
            assert mode["connected"] is False
            assert "raw_output" in mode
