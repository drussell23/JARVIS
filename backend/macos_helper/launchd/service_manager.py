"""
JARVIS macOS Helper - LaunchAgent Service Manager

Manages the LaunchAgent configuration for running the macOS helper
as a persistent background service.

Features:
- Generate LaunchAgent plist dynamically
- Install/uninstall to ~/Library/LaunchAgents
- Start/stop/restart via launchctl
- Status monitoring
- Log file management
"""

from __future__ import annotations

import asyncio
import logging
import os
import plistlib
import subprocess
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# Constants
# =============================================================================

SERVICE_LABEL = "com.jarvis.macos-helper"
PLIST_FILENAME = f"{SERVICE_LABEL}.plist"

# Paths
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
LOGS_DIR = Path.home() / ".jarvis" / "logs"


# =============================================================================
# Service Status
# =============================================================================

class ServiceStatus(str, Enum):
    """Status of the LaunchAgent service."""
    RUNNING = "running"
    STOPPED = "stopped"
    NOT_INSTALLED = "not_installed"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass
class ServiceInfo:
    """Information about the service."""
    status: ServiceStatus
    pid: Optional[int] = None
    label: str = SERVICE_LABEL
    plist_path: Optional[str] = None
    last_exit_status: Optional[int] = None
    loaded: bool = False


# =============================================================================
# Plist Generation
# =============================================================================

def generate_plist(
    python_path: Optional[str] = None,
    working_directory: Optional[str] = None,
    enable_voice: bool = True,
    enable_agi: bool = True,
    log_level: str = "INFO",
    run_at_load: bool = True,
    keep_alive: bool = True,
    throttle_interval: int = 10,
) -> Dict[str, Any]:
    """
    Generate LaunchAgent plist configuration.

    Args:
        python_path: Path to Python interpreter (auto-detected if None)
        working_directory: Working directory for the service
        enable_voice: Enable voice feedback
        enable_agi: Enable AGI OS integration
        log_level: Logging level
        run_at_load: Start immediately when loaded
        keep_alive: Restart on crash
        throttle_interval: Minimum seconds between restarts

    Returns:
        Dictionary representation of the plist
    """
    # Auto-detect paths
    if python_path is None:
        # Try to find the venv Python
        backend_dir = Path(__file__).parent.parent.parent
        venv_python = backend_dir / "venv" / "bin" / "python"
        if venv_python.exists():
            python_path = str(venv_python)
        else:
            python_path = "python3"

    if working_directory is None:
        working_directory = str(Path(__file__).parent.parent.parent)

    # Build program arguments
    program_args = [
        python_path,
        "-m",
        "macos_helper.macos_helper_coordinator",
        f"--log-level={log_level}",
    ]

    if not enable_voice:
        program_args.append("--no-voice")
    if not enable_agi:
        program_args.append("--no-agi")

    # Ensure log directory exists
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # Build plist
    plist = {
        "Label": SERVICE_LABEL,
        "ProgramArguments": program_args,
        "WorkingDirectory": working_directory,
        "RunAtLoad": run_at_load,
        "KeepAlive": {
            "SuccessfulExit": False,  # Restart on crash
            "Crashed": True,
        } if keep_alive else False,
        "ThrottleInterval": throttle_interval,
        "StandardOutPath": str(LOGS_DIR / "macos_helper.log"),
        "StandardErrorPath": str(LOGS_DIR / "macos_helper.error.log"),
        "EnvironmentVariables": {
            "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
            "PYTHONUNBUFFERED": "1",
            "JARVIS_SERVICE_MODE": "1",
        },
        # Nice priority (lower = higher priority)
        "Nice": 5,
        # Process type for activity monitor
        "ProcessType": "Background",
        # Legacy key for compatibility
        "LegacyTimers": True,
    }

    return plist


def save_plist(plist: Dict[str, Any], path: Optional[Path] = None) -> Path:
    """
    Save plist to file.

    Args:
        plist: Plist dictionary
        path: Target path (uses default if None)

    Returns:
        Path where plist was saved
    """
    if path is None:
        LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        path = LAUNCH_AGENTS_DIR / PLIST_FILENAME

    with open(path, "wb") as f:
        plistlib.dump(plist, f)

    logger.info(f"Saved plist to {path}")
    return path


# =============================================================================
# Service Management
# =============================================================================

class LaunchAgentManager:
    """
    Manages the JARVIS macOS helper LaunchAgent.

    Provides methods to install, uninstall, start, stop, and monitor
    the background service.
    """

    def __init__(self):
        self.label = SERVICE_LABEL
        self.plist_path = LAUNCH_AGENTS_DIR / PLIST_FILENAME

    def is_installed(self) -> bool:
        """Check if the LaunchAgent plist is installed."""
        return self.plist_path.exists()

    def get_status(self) -> ServiceInfo:
        """Get current service status."""
        info = ServiceInfo(status=ServiceStatus.UNKNOWN)

        if not self.is_installed():
            info.status = ServiceStatus.NOT_INSTALLED
            return info

        info.plist_path = str(self.plist_path)

        try:
            # Check if loaded via launchctl
            result = subprocess.run(
                ["launchctl", "list", self.label],
                capture_output=True,
                text=True,
            )

            if result.returncode == 0:
                info.loaded = True

                # Parse output for PID and status
                # Format: "PID\tStatus\tLabel" or just the JSON output
                lines = result.stdout.strip().split("\n")
                if lines:
                    parts = lines[0].split("\t")
                    if len(parts) >= 2:
                        try:
                            pid_str = parts[0].strip()
                            if pid_str and pid_str != "-":
                                info.pid = int(pid_str)
                                info.status = ServiceStatus.RUNNING
                            else:
                                info.status = ServiceStatus.STOPPED
                        except (ValueError, IndexError):
                            pass

                        try:
                            info.last_exit_status = int(parts[1].strip())
                        except (ValueError, IndexError):
                            pass
            else:
                info.loaded = False
                info.status = ServiceStatus.STOPPED

        except Exception as e:
            logger.error(f"Error checking service status: {e}")
            info.status = ServiceStatus.ERROR

        return info

    def install(
        self,
        enable_voice: bool = True,
        enable_agi: bool = True,
        log_level: str = "INFO",
        start_immediately: bool = True,
    ) -> bool:
        """
        Install the LaunchAgent.

        Args:
            enable_voice: Enable voice feedback
            enable_agi: Enable AGI OS integration
            log_level: Logging level
            start_immediately: Load and start immediately

        Returns:
            True if installation successful
        """
        try:
            # Unload if already loaded
            if self.is_installed():
                self.unload()

            # Generate and save plist
            plist = generate_plist(
                enable_voice=enable_voice,
                enable_agi=enable_agi,
                log_level=log_level,
            )
            save_plist(plist, self.plist_path)

            # Set permissions
            # launchd requires plist files to be world-readable (0o644)
            os.chmod(self.plist_path, 0o644)

            logger.info(f"Installed LaunchAgent: {self.plist_path}")

            # Load if requested
            if start_immediately:
                return self.load()

            return True

        except Exception as e:
            logger.error(f"Failed to install LaunchAgent: {e}")
            return False

    def uninstall(self) -> bool:
        """
        Uninstall the LaunchAgent.

        Returns:
            True if uninstallation successful
        """
        try:
            # Unload first
            self.unload()

            # Remove plist
            if self.plist_path.exists():
                self.plist_path.unlink()
                logger.info(f"Removed LaunchAgent: {self.plist_path}")

            return True

        except Exception as e:
            logger.error(f"Failed to uninstall LaunchAgent: {e}")
            return False

    def load(self) -> bool:
        """
        Load the LaunchAgent (launchctl load).

        Returns:
            True if loaded successfully
        """
        try:
            result = subprocess.run(
                ["launchctl", "load", str(self.plist_path)],
                capture_output=True,
                text=True,
            )

            if result.returncode == 0:
                logger.info("LaunchAgent loaded")
                return True
            else:
                logger.error(f"Failed to load LaunchAgent: {result.stderr}")
                return False

        except Exception as e:
            logger.error(f"Error loading LaunchAgent: {e}")
            return False

    def unload(self) -> bool:
        """
        Unload the LaunchAgent (launchctl unload).

        Returns:
            True if unloaded successfully
        """
        try:
            result = subprocess.run(
                ["launchctl", "unload", str(self.plist_path)],
                capture_output=True,
                text=True,
            )

            # Return code 0 or "not loaded" error both count as success
            if result.returncode == 0 or "not find" in result.stderr.lower():
                logger.info("LaunchAgent unloaded")
                return True
            else:
                logger.warning(f"Failed to unload LaunchAgent: {result.stderr}")
                return False

        except Exception as e:
            logger.error(f"Error unloading LaunchAgent: {e}")
            return False

    def start(self) -> bool:
        """
        Start the service (launchctl start).

        Returns:
            True if started successfully
        """
        try:
            result = subprocess.run(
                ["launchctl", "start", self.label],
                capture_output=True,
                text=True,
            )

            if result.returncode == 0:
                logger.info("Service started")
                return True
            else:
                logger.error(f"Failed to start service: {result.stderr}")
                return False

        except Exception as e:
            logger.error(f"Error starting service: {e}")
            return False

    def stop(self) -> bool:
        """
        Stop the service (launchctl stop).

        Returns:
            True if stopped successfully
        """
        try:
            result = subprocess.run(
                ["launchctl", "stop", self.label],
                capture_output=True,
                text=True,
            )

            if result.returncode == 0:
                logger.info("Service stopped")
                return True
            else:
                logger.error(f"Failed to stop service: {result.stderr}")
                return False

        except Exception as e:
            logger.error(f"Error stopping service: {e}")
            return False

    def restart(self) -> bool:
        """
        Restart the service.

        Returns:
            True if restarted successfully
        """
        self.stop()
        return self.start()

    def get_logs(self, lines: int = 100) -> Dict[str, str]:
        """
        Get recent log output.

        Args:
            lines: Number of lines to return

        Returns:
            Dictionary with stdout and stderr logs
        """
        logs = {"stdout": "", "stderr": ""}

        stdout_path = LOGS_DIR / "macos_helper.log"
        stderr_path = LOGS_DIR / "macos_helper.error.log"

        if stdout_path.exists():
            try:
                result = subprocess.run(
                    ["tail", "-n", str(lines), str(stdout_path)],
                    capture_output=True,
                    text=True,
                )
                logs["stdout"] = result.stdout
            except Exception:
                pass

        if stderr_path.exists():
            try:
                result = subprocess.run(
                    ["tail", "-n", str(lines), str(stderr_path)],
                    capture_output=True,
                    text=True,
                )
                logs["stderr"] = result.stdout
            except Exception:
                pass

        return logs


# =============================================================================
# Convenience Functions
# =============================================================================

_manager: Optional[LaunchAgentManager] = None


def _get_manager() -> LaunchAgentManager:
    """Get singleton manager instance."""
    global _manager
    if _manager is None:
        _manager = LaunchAgentManager()
    return _manager


def install_service(**kwargs) -> bool:
    """Install the service."""
    return _get_manager().install(**kwargs)


def uninstall_service() -> bool:
    """Uninstall the service."""
    return _get_manager().uninstall()


def get_service_status() -> ServiceInfo:
    """Get service status."""
    return _get_manager().get_status()


def start_service() -> bool:
    """Start the service."""
    return _get_manager().start()


def stop_service() -> bool:
    """Stop the service."""
    return _get_manager().stop()


def restart_service() -> bool:
    """Restart the service."""
    return _get_manager().restart()


# =============================================================================
# CLI Entry Point
# =============================================================================

def main():
    """CLI entry point for service management."""
    import argparse

    parser = argparse.ArgumentParser(
        description="JARVIS macOS Helper Service Manager"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command")

    # Install command
    install_parser = subparsers.add_parser("install", help="Install the service")
    install_parser.add_argument(
        "--no-voice", action="store_true", help="Disable voice feedback"
    )
    install_parser.add_argument(
        "--no-agi", action="store_true", help="Disable AGI OS integration"
    )
    install_parser.add_argument(
        "--log-level", default="INFO", help="Log level"
    )
    install_parser.add_argument(
        "--no-start", action="store_true", help="Don't start immediately"
    )

    # Uninstall command
    subparsers.add_parser("uninstall", help="Uninstall the service")

    # Status command
    subparsers.add_parser("status", help="Show service status")

    # Start/stop/restart commands
    subparsers.add_parser("start", help="Start the service")
    subparsers.add_parser("stop", help="Stop the service")
    subparsers.add_parser("restart", help="Restart the service")

    # Logs command
    logs_parser = subparsers.add_parser("logs", help="Show service logs")
    logs_parser.add_argument(
        "-n", "--lines", type=int, default=50, help="Number of lines"
    )

    args = parser.parse_args()

    if args.command == "install":
        success = install_service(
            enable_voice=not args.no_voice,
            enable_agi=not args.no_agi,
            log_level=args.log_level,
            start_immediately=not args.no_start,
        )
        print("✅ Installed" if success else "❌ Installation failed")

    elif args.command == "uninstall":
        success = uninstall_service()
        print("✅ Uninstalled" if success else "❌ Uninstallation failed")

    elif args.command == "status":
        info = get_service_status()
        print(f"Status: {info.status.value}")
        if info.pid:
            print(f"PID: {info.pid}")
        if info.plist_path:
            print(f"Plist: {info.plist_path}")
        print(f"Loaded: {info.loaded}")

    elif args.command == "start":
        success = start_service()
        print("✅ Started" if success else "❌ Start failed")

    elif args.command == "stop":
        success = stop_service()
        print("✅ Stopped" if success else "❌ Stop failed")

    elif args.command == "restart":
        success = restart_service()
        print("✅ Restarted" if success else "❌ Restart failed")

    elif args.command == "logs":
        logs = _get_manager().get_logs(args.lines)
        print("=== STDOUT ===")
        print(logs["stdout"])
        print("\n=== STDERR ===")
        print(logs["stderr"])

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
