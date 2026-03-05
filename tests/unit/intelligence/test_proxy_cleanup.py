"""Tests for Cloud SQL proxy lifecycle cleanup."""
import os
import signal
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest


class TestProxyCleanup:
    def test_atexit_registered_after_start(self):
        """atexit.register should be called after proxy starts."""
        with patch("atexit.register") as mock_register:
            from backend.intelligence.cloud_sql_proxy_manager import CloudSQLProxyManager
            mgr = CloudSQLProxyManager.__new__(CloudSQLProxyManager)
            mgr.process = MagicMock()
            mgr.process.pid = 12345
            mgr.process.returncode = None
            mgr.pid_path = Path(tempfile.mktemp())
            mgr._atexit_registered = False

            mgr._register_atexit_cleanup()
            assert mgr._atexit_registered is True
            mock_register.assert_called_once()

    def test_atexit_not_double_registered(self):
        """atexit should only be registered once."""
        with patch("atexit.register") as mock_register:
            from backend.intelligence.cloud_sql_proxy_manager import CloudSQLProxyManager
            mgr = CloudSQLProxyManager.__new__(CloudSQLProxyManager)
            mgr.process = MagicMock()
            mgr.pid_path = Path(tempfile.mktemp())
            mgr._atexit_registered = False

            mgr._register_atexit_cleanup()
            mgr._register_atexit_cleanup()
            assert mock_register.call_count == 1

    def test_stale_pid_with_wrong_process_not_killed(self):
        """Stale PID belonging to a different process must not be killed."""
        from backend.intelligence.cloud_sql_proxy_manager import CloudSQLProxyManager
        mgr = CloudSQLProxyManager.__new__(CloudSQLProxyManager)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".pid", delete=False) as f:
            f.write(str(os.getpid()))  # Our own PID -- not a proxy
            pid_path = Path(f.name)

        mgr.pid_path = pid_path
        mgr._is_cloud_sql_proxy_process = MagicMock(return_value=False)

        with patch("os.kill") as mock_kill:
            mgr._cleanup_stale_proxy_sync()
            mock_kill.assert_not_called()

        # PID file should be cleaned up regardless
        assert not pid_path.exists()

    def test_stale_pid_with_proxy_process_killed(self):
        """Stale PID that IS a proxy process should be terminated via SIGTERM."""
        from backend.intelligence.cloud_sql_proxy_manager import CloudSQLProxyManager
        mgr = CloudSQLProxyManager.__new__(CloudSQLProxyManager)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".pid", delete=False) as f:
            f.write("99999999")
            pid_path = Path(f.name)

        mgr.pid_path = pid_path
        mgr._is_cloud_sql_proxy_process = MagicMock(return_value=True)

        mock_proc = MagicMock()
        mock_proc.uids.return_value = MagicMock(real=os.getuid())  # Same UID

        with patch("psutil.Process", return_value=mock_proc):
            with patch("os.kill") as mock_kill:
                mgr._cleanup_stale_proxy_sync()
                mock_kill.assert_any_call(99999999, signal.SIGTERM)

        assert not pid_path.exists()

    def test_stale_pid_wrong_uid_not_killed(self):
        """Proxy owned by different user must not be killed."""
        from backend.intelligence.cloud_sql_proxy_manager import CloudSQLProxyManager
        mgr = CloudSQLProxyManager.__new__(CloudSQLProxyManager)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".pid", delete=False) as f:
            f.write("12345")
            pid_path = Path(f.name)

        mgr.pid_path = pid_path
        mgr._is_cloud_sql_proxy_process = MagicMock(return_value=True)

        mock_proc = MagicMock()
        mock_proc.uids.return_value = MagicMock(real=99999)  # Different UID

        with patch("psutil.Process", return_value=mock_proc):
            with patch("os.kill") as mock_kill:
                mgr._cleanup_stale_proxy_sync()
                mock_kill.assert_not_called()

        # PID file still cleaned up
        assert not pid_path.exists()
