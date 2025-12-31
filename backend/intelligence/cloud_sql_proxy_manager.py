#!/usr/bin/env python3
"""
Advanced Cloud SQL Proxy Manager
=================================

Dynamic, robust proxy lifecycle management with:
- Zero hardcoding - all config from database_config.json
- Auto-discovery of proxy binary location
- System-level persistence (launchd on macOS, systemd on Linux)
- Runtime health monitoring and auto-recovery
- Graceful degradation to SQLite fallback
- Port conflict resolution
- Multi-platform support
"""

import asyncio
import json
import logging
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class CloudSQLProxyManager:
    """
    Enterprise-grade Cloud SQL proxy lifecycle manager.

    Features:
    - Auto-discovers proxy binary and config
    - Manages proxy process lifecycle
    - Health monitoring with auto-recovery
    - System service integration (launchd/systemd)
    - Zero hardcoding - fully dynamic
    """

    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize proxy manager with dynamic configuration discovery.

        Args:
            config_path: Optional path to database_config.json
                        (auto-discovers from ~/.jarvis/gcp if not provided)
        """
        self.system = platform.system()
        self.config_path = config_path or self._discover_config_path()
        self.config = self._load_config()
        self.proxy_binary = self._discover_proxy_binary()
        # Use system temp directory for cross-platform compatibility
        temp_dir = Path(tempfile.gettempdir())
        self.log_path = temp_dir / "cloud-sql-proxy.log"
        self.pid_path = temp_dir / "cloud-sql-proxy.pid"
        self.service_name = "com.jarvis.cloudsql-proxy"
        self.process: Optional[subprocess.Popen] = None

        # NEW: Health monitoring and timeout tracking
        self.last_successful_query = None
        self.last_connection_check = None
        self.connection_failures = 0
        self.consecutive_failures = 0
        self.connection_history = []  # Rolling window of connection checks
        self.query_history = []  # Rolling window of query attempts
        self.health_window = 50  # Track last 50 health checks

        # Timeout thresholds (based on GCP documentation)
        self.idle_timeout_seconds = 600  # 10 minutes typical idle timeout
        self.warning_threshold_seconds = 480  # Warn at 8 minutes (80% of timeout)
        self.critical_threshold_seconds = 540  # Critical at 9 minutes (90% of timeout)

        # Rate limiting (from GCP Cloud SQL Admin API quotas)
        self.api_rate_limits = {
            'connect': 1000,  # requests per minute
            'get': 500,       # requests per minute
            'list': 500,      # requests per minute
            'mutate': 180     # requests per minute
        }
        self.api_call_history = {
            'connect': [],
            'get': [],
            'list': [],
            'mutate': []
        }

        # Self-healing configuration
        self.auto_reconnect_enabled = True
        self.max_reconnect_attempts = 3
        self.reconnect_backoff_seconds = [5, 15, 30]  # Exponential backoff
        self.last_reconnect_attempt = None
        self.reconnect_count = 0

        # Connection pool tracking
        self.active_connections = 0
        self.max_connections = 100  # Default for Cloud Run/App Engine
        self.connection_pool_warnings = []

        # SAI (Situational Awareness Intelligence) prediction tracking
        self.sai_predictions = []  # Rolling window of predictions
        self.last_sai_prediction = None
        self.sai_prediction_count = 0
        self.sai_max_predictions = 20  # Keep last 20 predictions

    def _discover_config_path(self) -> Path:
        """
        Auto-discover database config file location.

        Searches in order:
        1. ~/.jarvis/gcp/database_config.json
        2. $JARVIS_HOME/gcp/database_config.json
        3. ./database_config.json
        """
        search_paths = [
            Path.home() / ".jarvis" / "gcp" / "database_config.json",
            Path(os.getenv("JARVIS_HOME", ".")) / "gcp" / "database_config.json",
            Path("database_config.json"),
        ]

        for path in search_paths:
            if path.exists():
                logger.info(f"📂 Found database config: {path}")
                return path

        raise FileNotFoundError(f"Could not find database_config.json in any of: {search_paths}")

    def _load_config(self) -> Dict:
        """Load and validate database configuration."""
        try:
            with open(self.config_path) as f:
                config = json.load(f)

            # Validate required fields
            required = ["cloud_sql", "project_id"]
            if not all(k in config for k in required):
                raise ValueError(f"Config missing required fields: {required}")

            cloud_sql = config["cloud_sql"]
            required_sql = ["connection_name", "port"]
            if not all(k in cloud_sql for k in required_sql):
                raise ValueError(f"cloud_sql config missing: {required_sql}")

            logger.info(
                f"✅ Loaded config: {cloud_sql['connection_name']} " f"on port {cloud_sql['port']}"
            )
            return config

        except Exception as e:
            logger.error(f"Failed to load config from {self.config_path}: {e}")
            raise

    def _discover_proxy_binary(self) -> str:
        """
        Auto-discover cloud-sql-proxy binary location.

        Searches in order:
        1. $PATH (via which/where)
        2. ~/.local/bin/cloud-sql-proxy (common install location)
        3. ~/google-cloud-sdk/bin/cloud-sql-proxy
        4. /usr/local/bin/cloud-sql-proxy
        5. ~/bin/cloud-sql-proxy
        6. /opt/homebrew/bin/cloud-sql-proxy (Apple Silicon)
        """
        # Try PATH first
        binary = shutil.which("cloud-sql-proxy")
        if binary:
            logger.info(f"📍 Found proxy binary in PATH: {binary}")
            return binary

        # Try common locations (ENHANCED - includes ~/.local/bin first!)
        search_paths = [
            Path.home() / ".local" / "bin" / "cloud-sql-proxy",  # Most common
            Path.home() / "google-cloud-sdk" / "bin" / "cloud-sql-proxy",
            Path("/usr/local/bin/cloud-sql-proxy"),
            Path.home() / "bin" / "cloud-sql-proxy",
            Path("/opt/homebrew/bin/cloud-sql-proxy"),  # Apple Silicon Homebrew
        ]

        for path in search_paths:
            if path.exists() and os.access(path, os.X_OK):
                logger.info(f"📍 Found proxy binary: {path}")
                return str(path)

        # Enhanced error message with install instructions
        error_msg = (
            "❌ cloud-sql-proxy binary not found!\n\n"
            "Install options:\n"
            "  1. Download directly:\n"
            "     curl -o ~/.local/bin/cloud-sql-proxy https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.8.2/cloud-sql-proxy.darwin.amd64\n"
            "     chmod +x ~/.local/bin/cloud-sql-proxy\n\n"
            "  2. Using gcloud:\n"
            "     gcloud components install cloud-sql-proxy\n\n"
            f"Searched locations: {[str(p) for p in search_paths]}"
        )
        raise FileNotFoundError(error_msg)

    def _is_port_in_use(self, port: int) -> bool:
        """Check if port is already in use."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) == 0

    def _find_proxy_processes(self) -> list:
        """Find all running cloud-sql-proxy processes."""
        try:
            if self.system == "Darwin" or self.system == "Linux":
                result = subprocess.run(
                    ["pgrep", "-f", "cloud-sql-proxy"],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    return [int(pid) for pid in result.stdout.strip().split()]
            elif self.system == "Windows":
                result = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq cloud-sql-proxy*"],
                    capture_output=True,
                    text=True,
                )
                # Parse Windows tasklist output
                pids = []
                for line in result.stdout.split("\n")[3:]:
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            pids.append(int(parts[1]))
                        except ValueError:
                            pass
                return pids
        except Exception as e:
            logger.debug(f"Error finding proxy processes: {e}")

        return []

    def is_running(self) -> bool:
        """Check if proxy is running and healthy."""
        # Check if port is in use
        port = self.config["cloud_sql"]["port"]
        if not self._is_port_in_use(port):
            return False

        # Check if PID file exists and process is alive
        if self.pid_path.exists():
            try:
                with open(self.pid_path) as f:
                    pid = int(f.read().strip())
                # Check if process exists
                os.kill(pid, 0)  # Doesn't actually kill, just checks
                return True
            except (ProcessLookupError, ValueError):
                self.pid_path.unlink(missing_ok=True)

        return False

    def _kill_conflicting_processes(self):
        """Kill any conflicting proxy processes."""
        port = self.config["cloud_sql"]["port"]

        if not self._is_port_in_use(port):
            return

        logger.warning(f"⚠️  Port {port} in use, killing conflicting processes...")
        pids = self._find_proxy_processes()

        for pid in pids:
            try:
                logger.info(f"🔪 Killing proxy process: PID {pid}")
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.5)
                # Force kill if still alive
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            except Exception as e:
                logger.debug(f"Error killing PID {pid}: {e}")

        # Wait for port to become available
        for _ in range(10):
            if not self._is_port_in_use(port):
                break
            time.sleep(0.5)
        else:
            logger.error(f"❌ Failed to free port {port}")

    async def start(self, force_restart: bool = False, max_retries: int = 3) -> bool:
        """
        Start Cloud SQL proxy with health monitoring and auto-recovery.

        Args:
            force_restart: Kill existing processes and start fresh
            max_retries: Maximum number of startup attempts

        Returns:
            True if started successfully, False otherwise
        """
        last_error = None

        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    logger.info(f"🔄 Retry attempt {attempt + 1}/{max_retries}...")
                    await asyncio.sleep(2)  # Wait before retry

                # Check if already running
                if self.is_running() and not force_restart:
                    logger.info("✅ Cloud SQL proxy already running")
                    return True

                # Kill conflicting processes if needed
                if force_restart or attempt > 0:
                    self._kill_conflicting_processes()

                # Build proxy command (no hardcoding!)
                cloud_sql = self.config["cloud_sql"]
                port = cloud_sql["port"]
                connection_name = cloud_sql["connection_name"]

                cmd = [
                    self.proxy_binary,
                    connection_name,
                    f"--port={port}",
                ]

                # Add optional auth if specified in config
                if "auth_method" in cloud_sql:
                    if cloud_sql["auth_method"] == "service_account":
                        if "service_account_key" in cloud_sql:
                            cmd.append(f"--credentials-file={cloud_sql['service_account_key']}")

                logger.info(f"🚀 Starting Cloud SQL proxy (attempt {attempt + 1}/{max_retries})...")
                logger.info(f"   Binary: {self.proxy_binary}")
                logger.info(f"   Connection: {connection_name}")
                logger.info(f"   Port: {port}")
                logger.info(f"   Log: {self.log_path}")
                logger.info(f"   Command: {' '.join(cmd)}")

                # Ensure log directory exists
                self.log_path.parent.mkdir(parents=True, exist_ok=True)

                # Start proxy process (truncate log file for fresh start)
                log_file = open(self.log_path, "w")  # Use "w" to truncate old logs
                self.process = subprocess.Popen(
                    cmd,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,  # Detach from parent
                )

                # Write PID file
                with open(self.pid_path, "w") as f:
                    f.write(str(self.process.pid))

                logger.info(f"   PID: {self.process.pid}")

                # Wait for proxy to be ready (max 15 seconds) - ASYNC!
                logger.info(f"⏳ Waiting for proxy to be ready...")
                for i in range(30):
                    await asyncio.sleep(0.5)  # Non-blocking async sleep

                    # Check if process crashed
                    if self.process.poll() is not None:
                        log_content = self.log_path.read_text() if self.log_path.exists() else "No log"
                        error_msg = f"Proxy process crashed (exit code: {self.process.returncode})\nLog:\n{log_content}"
                        logger.error(f"❌ {error_msg}")
                        last_error = error_msg
                        break

                    if self._is_port_in_use(port):
                        logger.info(f"✅ Cloud SQL proxy ready on port {port} (took {i * 0.5:.1f}s)")
                        return True

                # If we got here, proxy didn't start in time
                if self.process.poll() is None:
                    logger.error(f"❌ Proxy failed to start within 15 seconds")
                    last_error = "Startup timeout"
                    # Kill the slow process
                    self.process.terminate()

            except FileNotFoundError as e:
                logger.error(f"❌ Proxy binary not found: {e}")
                last_error = str(e)
                break  # No point retrying if binary doesn't exist

            except Exception as e:
                logger.error(f"❌ Failed to start Cloud SQL proxy: {e}", exc_info=True)
                last_error = str(e)

        # All retries failed
        logger.error(f"❌ Cloud SQL proxy failed to start after {max_retries} attempts")
        if last_error:
            logger.error(f"   Last error: {last_error}")

        # Show log file for debugging
        if self.log_path.exists():
            logger.error(f"   Check log file: {self.log_path}")
            try:
                log_tail = self.log_path.read_text().split("\n")[-20:]
                logger.error(f"   Last 20 lines of log:\n" + "\n".join(log_tail))
            except Exception:
                pass

        return False

    async def stop(self) -> bool:
        """Stop Cloud SQL proxy gracefully."""
        try:
            if not self.is_running():
                logger.info("Cloud SQL proxy not running")
                return True

            logger.info("🛑 Stopping Cloud SQL proxy...")

            # Try graceful shutdown first
            if self.process:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            else:
                # Kill using PID file
                if self.pid_path.exists():
                    with open(self.pid_path) as f:
                        pid = int(f.read().strip())
                    os.kill(pid, signal.SIGTERM)

            self.pid_path.unlink(missing_ok=True)
            logger.info("✅ Cloud SQL proxy stopped")
            return True

        except Exception as e:
            logger.error(f"Error stopping proxy: {e}", exc_info=True)
            return False

    async def restart(self) -> bool:
        """Restart Cloud SQL proxy."""
        logger.info("🔄 Restarting Cloud SQL proxy...")
        await self.stop()
        await asyncio.sleep(1)
        return await self.start(force_restart=True)

    async def monitor(self, check_interval: int = 30, max_recovery_attempts: int = 3):
        """
        Monitor proxy health and auto-recover if needed.

        Args:
            check_interval: Seconds between health checks
            max_recovery_attempts: Maximum consecutive recovery attempts before giving up
        """
        logger.info(f"🔍 Starting proxy health monitor (interval: {check_interval}s)")

        consecutive_failures = 0
        last_check_time = time.time()

        while True:
            try:
                await asyncio.sleep(check_interval)

                # Health check
                is_healthy = self.is_running()
                current_time = time.time()
                elapsed = current_time - last_check_time
                last_check_time = current_time

                if not is_healthy:
                    consecutive_failures += 1
                    logger.warning(
                        f"⚠️  Cloud SQL proxy unhealthy "
                        f"(consecutive failures: {consecutive_failures}/{max_recovery_attempts})"
                    )

                    if consecutive_failures <= max_recovery_attempts:
                        logger.info(f"🔄 Attempting automatic recovery (attempt {consecutive_failures})...")
                        success = await self.start(force_restart=True)

                        if success:
                            logger.info("✅ Proxy recovered successfully")
                            consecutive_failures = 0  # Reset counter on success
                        else:
                            logger.error(f"❌ Proxy recovery attempt {consecutive_failures} failed")

                            # If max attempts reached, alert and wait longer
                            if consecutive_failures >= max_recovery_attempts:
                                logger.error(
                                    f"❌ Proxy recovery failed after {max_recovery_attempts} attempts"
                                )
                                logger.error("   Voice authentication will be unavailable")
                                logger.error("   Will continue monitoring and retry in 5 minutes...")
                                await asyncio.sleep(300)  # Wait 5 minutes before trying again
                                consecutive_failures = 0  # Reset to try again
                    else:
                        logger.error("❌ Max recovery attempts exceeded, waiting before retry...")
                else:
                    # Proxy is healthy
                    if consecutive_failures > 0:
                        logger.info("✅ Proxy health restored")
                        consecutive_failures = 0

                    # Log periodic health status
                    if int(current_time) % 300 == 0:  # Every 5 minutes
                        logger.debug(f"✅ Cloud SQL proxy healthy (uptime: {elapsed:.0f}s)")

            except asyncio.CancelledError:
                logger.info("Health monitor stopped")
                break
            except Exception as e:
                logger.error(f"Health monitor error: {e}", exc_info=True)

    def install_system_service(self) -> bool:
        """
        Install Cloud SQL proxy as system service (launchd/systemd).

        macOS: Creates launchd plist in ~/Library/LaunchAgents/
        Linux: Creates systemd unit in ~/.config/systemd/user/
        """
        try:
            if self.system == "Darwin":
                return self._install_launchd_service()
            elif self.system == "Linux":
                return self._install_systemd_service()
            else:
                logger.warning(f"System service not supported on {self.system}")
                return False
        except Exception as e:
            logger.error(f"Failed to install system service: {e}", exc_info=True)
            return False

    def _install_launchd_service(self) -> bool:
        """Install macOS launchd service."""
        plist_dir = Path.home() / "Library" / "LaunchAgents"
        plist_dir.mkdir(parents=True, exist_ok=True)
        plist_path = plist_dir / f"{self.service_name}.plist"

        cloud_sql = self.config["cloud_sql"]

        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{self.service_name}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{self.proxy_binary}</string>
        <string>{cloud_sql['connection_name']}</string>
        <string>--port={cloud_sql['port']}</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>StandardOutPath</key>
    <string>{self.log_path}</string>

    <key>StandardErrorPath</key>
    <string>{self.log_path}</string>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
"""

        with open(plist_path, "w") as f:
            f.write(plist_content)

        logger.info(f"✅ Created launchd service: {plist_path}")

        # Load service
        subprocess.run(["launchctl", "load", str(plist_path)], check=True)
        logger.info(f"✅ Loaded launchd service: {self.service_name}")

        return True

    def _install_systemd_service(self) -> bool:
        """Install Linux systemd user service."""
        systemd_dir = Path.home() / ".config" / "systemd" / "user"
        systemd_dir.mkdir(parents=True, exist_ok=True)
        service_path = systemd_dir / f"{self.service_name}.service"

        cloud_sql = self.config["cloud_sql"]

        service_content = f"""[Unit]
Description=JARVIS Cloud SQL Proxy
After=network.target

[Service]
Type=simple
ExecStart={self.proxy_binary} {cloud_sql['connection_name']} --port={cloud_sql['port']}
Restart=on-failure
RestartSec=5
StandardOutput=append:{self.log_path}
StandardError=append:{self.log_path}

[Install]
WantedBy=default.target
"""

        with open(service_path, "w") as f:
            f.write(service_content)

        logger.info(f"✅ Created systemd service: {service_path}")

        # Reload systemd and enable service
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(
            ["systemctl", "--user", "enable", f"{self.service_name}.service"], check=True
        )
        subprocess.run(["systemctl", "--user", "start", f"{self.service_name}.service"], check=True)

        logger.info(f"✅ Enabled systemd service: {self.service_name}")
        return True

    def uninstall_system_service(self) -> bool:
        """Uninstall system service."""
        try:
            if self.system == "Darwin":
                plist_path = Path.home() / "Library" / "LaunchAgents" / f"{self.service_name}.plist"
                if plist_path.exists():
                    subprocess.run(["launchctl", "unload", str(plist_path)])
                    plist_path.unlink()
                    logger.info(f"✅ Uninstalled launchd service")
            elif self.system == "Linux":
                subprocess.run(["systemctl", "--user", "stop", f"{self.service_name}.service"])
                subprocess.run(["systemctl", "--user", "disable", f"{self.service_name}.service"])
                service_path = (
                    Path.home() / ".config" / "systemd" / "user" / f"{self.service_name}.service"
                )
                if service_path.exists():
                    service_path.unlink()
                subprocess.run(["systemctl", "--user", "daemon-reload"])
                logger.info(f"✅ Uninstalled systemd service")
            return True
        except Exception as e:
            logger.error(f"Error uninstalling service: {e}", exc_info=True)
            return False

    # ========================================================================
    # HEALTH MONITORING & TIMEOUT FORECASTING
    # ========================================================================

    async def check_connection_health(self) -> Dict:
        """
        Comprehensive connection health check with detailed metrics

        Returns:
            Dict with health status, timeout forecasting, rate limit usage, and recommendations
        """
        from datetime import datetime, timedelta

        health_data = {
            'timestamp': datetime.now().isoformat(),
            'proxy_running': False,
            'connection_active': False,
            'last_query_age_seconds': None,
            'timeout_forecast': None,
            'timeout_status': 'unknown',
            'rate_limit_status': {},
            'connection_pool': {},
            'recommendations': [],
            'needs_reconnect': False,
            'auto_heal_triggered': False
        }

        # Check for psycopg2 dependency (PostgreSQL driver) using unified driver manager
        psycopg2 = None
        try:
            from intelligence.unified_database_drivers import get_driver_manager, DriverStatus
            driver_manager = get_driver_manager()
            status = driver_manager.check_driver('psycopg2', auto_install=True)
            if status == DriverStatus.READY:
                psycopg2 = driver_manager.get_psycopg2()
        except ImportError:
            # Fallback to direct import if unified manager not available
            try:
                import psycopg2 as _psycopg2
                psycopg2 = _psycopg2
            except ImportError:
                pass

        if psycopg2 is None:
            logger.warning("[CLOUDSQL] ❌ psycopg2 not installed - database connection checks disabled")
            health_data['connection_active'] = False
            health_data['error'] = 'psycopg2 not installed'
            health_data['recommendations'].append(
                'Install PostgreSQL driver: pip install psycopg2-binary'
            )
            # Proxy can still be running even without psycopg2
            health_data['proxy_running'] = self.is_running()
            return health_data

        # Check if proxy process is running
        health_data['proxy_running'] = self.is_running()
        if not health_data['proxy_running']:
            logger.error("[CLOUDSQL] ❌ Proxy not running!")
            health_data['recommendations'].append("Proxy process not running - auto-healing will attempt restart")
            health_data['needs_reconnect'] = True

            # AUTONOMOUS SELF-HEALING: Restart proxy
            if self.auto_reconnect_enabled:
                logger.info("[CLOUDSQL] 🔧 AUTO-HEAL: Attempting proxy restart...")
                success = await self._auto_heal_reconnect()
                health_data['auto_heal_triggered'] = True
                health_data['proxy_running'] = success
                if success:
                    logger.info("[CLOUDSQL] ✅ AUTO-HEAL: Proxy restarted successfully")
                else:
                    logger.error("[CLOUDSQL] ❌ AUTO-HEAL: Proxy restart failed")

            return health_data

        # Try actual database connection with retry logic
        # GCP CloudSQL instances may need time to wake up from auto-suspend
        conn = None
        cursor = None
        max_retries = 3
        base_timeout = 10  # Increased from 5 to handle slow wakeups
        last_error = None

        try:
            for attempt in range(max_retries):
                try:
                    port = self.config['cloud_sql']['port']
                    # Exponential backoff for timeout: 10s, 15s, 20s
                    timeout = base_timeout + (attempt * 5)

                    conn = psycopg2.connect(
                        host='127.0.0.1',
                        port=port,
                        database=self.config['cloud_sql'].get('database', 'postgres'),
                        user=self.config['cloud_sql'].get('user', 'postgres'),
                        password=self.config['cloud_sql'].get('password', ''),
                        connect_timeout=timeout
                    )

                    # Execute test query
                    cursor = conn.cursor()
                    cursor.execute("SELECT 1")
                    cursor.fetchone()

                    # Success!
                    current_time = datetime.now()
                    self.last_successful_query = current_time
                    self.last_connection_check = current_time
                    self.consecutive_failures = 0
                    health_data['connection_active'] = True
                    if attempt > 0:
                        logger.info(f"[CLOUDSQL] ✅ Connection succeeded on retry {attempt + 1}")

                    # Track in history
                    self.connection_history.append({
                        'timestamp': current_time.isoformat(),
                        'success': True
                    })
                    if len(self.connection_history) > self.health_window:
                        self.connection_history.pop(0)

                    logger.info(f"[CLOUDSQL] ✅ Connection healthy (query successful)")
                    break  # Exit retry loop on success

                except Exception as e:
                    last_error = e
                    if attempt < max_retries - 1:
                        wait_time = (attempt + 1) * 2  # 2s, 4s
                        logger.debug(f"[CLOUDSQL] Connection attempt {attempt + 1} failed, retrying in {wait_time}s: {e}")
                        await asyncio.sleep(wait_time)
                        # Close failed connection if any
                        if cursor:
                            try:
                                cursor.close()
                            except:
                                pass
                        if conn:
                            try:
                                conn.close()
                            except:
                                pass
                        conn = None
                        cursor = None
                    else:
                        # Final attempt failed, raise the error
                        raise last_error

        except Exception as e:
            logger.error(f"[CLOUDSQL] ❌ Connection failed: {e}")
            self.consecutive_failures += 1
            self.connection_failures += 1
            health_data['connection_active'] = False
            health_data['error'] = str(e)

            # Track failure in history
            current_time = datetime.now()
            self.connection_history.append({
                'timestamp': current_time.isoformat(),
                'success': False,
                'error': str(e)
            })
            if len(self.connection_history) > self.health_window:
                self.connection_history.pop(0)

            # Check if we need to reconnect
            if self.consecutive_failures >= 3:
                health_data['needs_reconnect'] = True
                health_data['recommendations'].append(f"3+ consecutive failures - reconnection needed")

                # AUTONOMOUS SELF-HEALING
                if self.auto_reconnect_enabled:
                    logger.info(f"[CLOUDSQL] 🔧 AUTO-HEAL: {self.consecutive_failures} failures detected, reconnecting...")
                    success = await self._auto_heal_reconnect()
                    health_data['auto_heal_triggered'] = True
                    if success:
                        logger.info("[CLOUDSQL] ✅ AUTO-HEAL: Reconnected successfully")
                        health_data['connection_active'] = True
                        self.consecutive_failures = 0
                    else:
                        logger.error("[CLOUDSQL] ❌ AUTO-HEAL: Reconnection failed")

        finally:
            # CRITICAL: Always close cursor and connection to prevent leaks
            if cursor:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

        # Calculate timeout forecast
        if self.last_successful_query:
            age_seconds = (datetime.now() - self.last_successful_query).total_seconds()
            health_data['last_query_age_seconds'] = int(age_seconds)

            # Forecast timeout
            time_until_timeout = self.idle_timeout_seconds - age_seconds
            health_data['timeout_forecast'] = {
                'seconds_until_timeout': int(max(0, time_until_timeout)),
                'minutes_until_timeout': int(max(0, time_until_timeout / 60)),
                'timeout_threshold': self.idle_timeout_seconds,
                'percentage_used': min(100, (age_seconds / self.idle_timeout_seconds) * 100)
            }

            # Determine status
            if age_seconds >= self.critical_threshold_seconds:
                health_data['timeout_status'] = 'critical'
                health_data['recommendations'].append(
                    f"⚠️  CRITICAL: {int(time_until_timeout)}s until timeout - immediate action required"
                )
                # AUTO-HEAL: Preemptive reconnect
                if self.auto_reconnect_enabled and time_until_timeout < 60:
                    logger.warning("[CLOUDSQL] 🔧 AUTO-HEAL: Preemptive reconnect (< 60s until timeout)")
                    await self._auto_heal_reconnect()
                    health_data['auto_heal_triggered'] = True

            elif age_seconds >= self.warning_threshold_seconds:
                health_data['timeout_status'] = 'warning'
                health_data['recommendations'].append(
                    f"⚠️  WARNING: {int(time_until_timeout)}s until timeout"
                )
            else:
                health_data['timeout_status'] = 'healthy'

        # Check rate limits
        health_data['rate_limit_status'] = self._check_rate_limits()

        # Connection pool status
        success_rate = self._calculate_success_rate()
        health_data['connection_pool'] = {
            'active_connections': self.active_connections,
            'max_connections': self.max_connections,
            'utilization_percent': (self.active_connections / self.max_connections) * 100,
            'total_failures': self.connection_failures,
            'consecutive_failures': self.consecutive_failures,
            'success_rate': success_rate
        }

        # Voice profile verification (if connection is active)
        if health_data['connection_active']:
            health_data['voice_profiles'] = await self._check_voice_profiles()

        # SAI Prediction (Situational Awareness Intelligence)
        sai_prediction = self._generate_sai_prediction()
        if sai_prediction:
            health_data['sai_prediction'] = sai_prediction

            # PROACTIVE SELF-HEALING based on SAI predictions
            if sai_prediction['severity'] == 'critical' and sai_prediction.get('auto_heal_available'):
                if sai_prediction['type'] == 'timeout_imminent' and sai_prediction['time_horizon_seconds'] < 60:
                    logger.warning(
                        f"[CLOUDSQL SAI] 🔧 PROACTIVE AUTO-HEAL: {sai_prediction['predicted_event']} "
                        f"in {sai_prediction['time_horizon_seconds']}s - triggering preemptive reconnection"
                    )
                    success = await self._auto_heal_reconnect()
                    health_data['sai_auto_heal_triggered'] = True
                    health_data['sai_auto_heal_success'] = success
                elif sai_prediction['type'] in ['connection_degradation', 'failure_trend']:
                    logger.warning(
                        f"[CLOUDSQL SAI] 🔧 PROACTIVE AUTO-HEAL: {sai_prediction['reason']} - "
                        f"triggering reconnection"
                    )
                    success = await self._auto_heal_reconnect()
                    health_data['sai_auto_heal_triggered'] = True
                    health_data['sai_auto_heal_success'] = success

        return health_data

    async def _auto_heal_reconnect(self) -> bool:
        """
        Autonomous self-healing reconnection with exponential backoff

        Returns:
            bool: True if reconnection successful
        """
        from datetime import datetime, timedelta

        # Check if we've exceeded max attempts
        if self.reconnect_count >= self.max_reconnect_attempts:
            logger.error(f"[CLOUDSQL] ❌ Max reconnect attempts ({self.max_reconnect_attempts}) exceeded")
            return False

        # Check backoff time
        if self.last_reconnect_attempt:
            backoff_index = min(self.reconnect_count, len(self.reconnect_backoff_seconds) - 1)
            backoff_seconds = self.reconnect_backoff_seconds[backoff_index]
            time_since_last = (datetime.now() - self.last_reconnect_attempt).total_seconds()

            if time_since_last < backoff_seconds:
                logger.info(f"[CLOUDSQL] ⏳ Backoff active: wait {int(backoff_seconds - time_since_last)}s")
                return False

        self.last_reconnect_attempt = datetime.now()
        self.reconnect_count += 1

        logger.info(f"[CLOUDSQL] 🔄 Reconnect attempt {self.reconnect_count}/{self.max_reconnect_attempts}")

        try:
            # Stop existing proxy
            if self.is_running():
                logger.info("[CLOUDSQL] 🛑 Stopping existing proxy...")
                await self.stop()
                await asyncio.sleep(2)

            # Start new proxy
            logger.info("[CLOUDSQL] 🚀 Starting new proxy...")
            success = await self.start()

            if success:
                # Wait for proxy to be ready
                await asyncio.sleep(3)

                # Verify connection
                test_result = await self.check_connection_health()
                if test_result.get('connection_active'):
                    logger.info("[CLOUDSQL] ✅ Reconnection successful")
                    self.reconnect_count = 0  # Reset on success
                    return True
                else:
                    logger.error("[CLOUDSQL] ❌ Reconnection failed: connection not active")
                    return False
            else:
                logger.error("[CLOUDSQL] ❌ Reconnection failed: proxy start failed")
                return False

        except Exception as e:
            logger.error(f"[CLOUDSQL] ❌ Reconnection error: {e}")
            return False

    def _check_rate_limits(self) -> Dict:
        """
        Check API rate limit usage

        Returns:
            Dict with rate limit status for each API category
        """
        from datetime import datetime, timedelta

        current_time = datetime.now()
        one_minute_ago = current_time - timedelta(minutes=1)

        rate_status = {}
        for category, limit in self.api_rate_limits.items():
            # Clean old entries
            self.api_call_history[category] = [
                call for call in self.api_call_history[category]
                if datetime.fromisoformat(call) > one_minute_ago
            ]

            current_usage = len(self.api_call_history[category])
            usage_percent = (current_usage / limit) * 100

            rate_status[category] = {
                'current_usage': current_usage,
                'limit': limit,
                'usage_percent': usage_percent,
                'status': 'critical' if usage_percent > 90 else 'warning' if usage_percent > 75 else 'healthy',
                'remaining': limit - current_usage
            }

        return rate_status

    def _calculate_success_rate(self) -> float:
        """Calculate recent connection success rate"""
        if not self.connection_history:
            return 1.0

        recent = self.connection_history[-20:]  # Last 20 attempts
        successes = sum(1 for c in recent if c.get('success'))
        return successes / len(recent) if recent else 1.0

    def record_api_call(self, category: str):
        """Record an API call for rate limit tracking"""
        from datetime import datetime

        if category in self.api_call_history:
            self.api_call_history[category].append(datetime.now().isoformat())
            logger.debug(f"[CLOUDSQL] API call recorded: {category}")

    def _generate_sai_prediction(self) -> Optional[Dict]:
        """
        SAI (Situational Awareness Intelligence) prediction for CloudSQL issues

        Analyzes historical patterns to predict:
        - Imminent timeout/disconnection
        - Rate limit approaching
        - Connection degradation
        - Auto-healing needs

        Returns:
            Dict with prediction details or None if no issues predicted
        """
        from datetime import datetime, timedelta

        predictions = []
        current_time = datetime.now()

        # PREDICTION 1: Timeout imminent
        if self.last_successful_query:
            age_seconds = (current_time - self.last_successful_query).total_seconds()
            time_until_timeout = self.idle_timeout_seconds - age_seconds
            timeout_percentage = (age_seconds / self.idle_timeout_seconds) * 100

            if time_until_timeout < 120 and timeout_percentage >= 80:  # < 2 minutes and >= 80%
                confidence = min(1.0, timeout_percentage / 100)
                predictions.append({
                    'type': 'timeout_imminent',
                    'severity': 'critical' if time_until_timeout < 60 else 'warning',
                    'confidence': confidence,
                    'time_horizon_seconds': int(time_until_timeout),
                    'predicted_event': 'CloudSQL connection timeout',
                    'reason': f'Connection idle for {int(age_seconds)}s ({timeout_percentage:.1f}% of timeout threshold)',
                    'recommended_action': 'Trigger preemptive reconnection',
                    'auto_heal_available': self.auto_reconnect_enabled
                })

        # PREDICTION 2: Rate limit approaching
        rate_limits = self._check_rate_limits()
        for category, status in rate_limits.items():
            if status['usage_percent'] > 80:
                predictions.append({
                    'type': 'rate_limit_approaching',
                    'severity': 'critical' if status['usage_percent'] > 90 else 'warning',
                    'confidence': min(1.0, status['usage_percent'] / 100),
                    'time_horizon_seconds': 60,  # Within next minute
                    'predicted_event': f'{category} rate limit exceeded',
                    'reason': f"{category} API at {status['usage_percent']:.1f}% ({status['current_usage']}/{status['limit']} calls/min)",
                    'recommended_action': 'Throttle API calls or wait for rate limit window to reset',
                    'auto_heal_available': False
                })

        # PREDICTION 3: Connection degradation pattern
        if len(self.connection_history) >= 10:
            recent_10 = self.connection_history[-10:]
            failures_in_last_10 = sum(1 for c in recent_10 if not c.get('success'))
            failure_rate = failures_in_last_10 / 10

            if failure_rate > 0.3:  # > 30% failure rate
                predictions.append({
                    'type': 'connection_degradation',
                    'severity': 'critical' if failure_rate > 0.5 else 'warning',
                    'confidence': failure_rate,
                    'time_horizon_seconds': 30,
                    'predicted_event': 'Connection failure imminent',
                    'reason': f'{failures_in_last_10}/10 recent connection attempts failed ({failure_rate:.0%} failure rate)',
                    'recommended_action': 'Investigate network issues and trigger reconnection',
                    'auto_heal_available': self.auto_reconnect_enabled
                })

        # PREDICTION 4: Consecutive failures trend
        if self.consecutive_failures >= 2:
            predictions.append({
                'type': 'failure_trend',
                'severity': 'critical' if self.consecutive_failures >= 3 else 'warning',
                'confidence': min(1.0, self.consecutive_failures / 3),
                'time_horizon_seconds': 10,
                'predicted_event': 'Auto-healing trigger imminent',
                'reason': f'{self.consecutive_failures} consecutive failures detected',
                'recommended_action': 'Auto-healing will trigger after 3 consecutive failures',
                'auto_heal_available': self.auto_reconnect_enabled
            })

        # Return highest severity prediction
        if predictions:
            # Sort by severity (critical first) and confidence
            predictions.sort(key=lambda p: (
                0 if p['severity'] == 'critical' else 1,
                -p['confidence']
            ))

            best_prediction = predictions[0]
            best_prediction['timestamp'] = current_time.isoformat()
            best_prediction['alternative_predictions'] = len(predictions) - 1

            # Store prediction
            self.last_sai_prediction = best_prediction
            self.sai_predictions.append(best_prediction)
            if len(self.sai_predictions) > self.sai_max_predictions:
                self.sai_predictions.pop(0)
            self.sai_prediction_count += 1

            # Log at DEBUG level to avoid spam
            logger.debug(
                f"[CLOUDSQL SAI] 🔮 Prediction #{self.sai_prediction_count}: "
                f"{best_prediction['predicted_event']} in {best_prediction['time_horizon_seconds']}s "
                f"(confidence: {best_prediction['confidence']:.1%}, severity: {best_prediction['severity'].upper()})"
            )

            return best_prediction

        return None

    async def _check_voice_profiles(self) -> Dict:
        """
        Verify voice profiles are stored and ready in CloudSQL database

        Returns:
            Dict with voice profile status, sample counts, and readiness
        """
        profile_data = {
            'status': 'unknown',
            'profiles_found': 0,
            'speakers': [],
            'total_samples': 0,
            'ready_for_unlock': False,
            'issues': []
        }

        # Check for psycopg2 dependency using unified driver manager
        psycopg2 = None
        try:
            from intelligence.unified_database_drivers import get_driver_manager, DriverStatus
            driver_manager = get_driver_manager()
            status = driver_manager.check_driver('psycopg2', auto_install=True)
            if status == DriverStatus.READY:
                psycopg2 = driver_manager.get_psycopg2()
        except ImportError:
            # Fallback to direct import
            try:
                import psycopg2 as _psycopg2
                psycopg2 = _psycopg2
            except ImportError:
                pass

        if psycopg2 is None:
            logger.warning("[CLOUDSQL] ❌ psycopg2 not installed - voice profile checks disabled")
            profile_data['status'] = 'psycopg2_missing'
            profile_data['error'] = 'psycopg2 not installed'
            profile_data['issues'].append('Install PostgreSQL driver: pip install psycopg2-binary')
            return profile_data

        conn = None
        cursor = None
        try:
            port = self.config['cloud_sql']['port']
            conn = psycopg2.connect(
                host='127.0.0.1',
                port=port,
                database=self.config['cloud_sql'].get('database', 'postgres'),
                user=self.config['cloud_sql'].get('user', 'postgres'),
                password=self.config['cloud_sql'].get('password', ''),
                connect_timeout=5
            )

            cursor = conn.cursor()

            # Query speaker profiles with correct column names
            cursor.execute("""
                SELECT speaker_id, speaker_name, voiceprint_embedding, total_samples,
                       last_updated, recognition_confidence, successful_verifications,
                       failed_verifications
                FROM speaker_profiles
                ORDER BY speaker_id
            """)

            profiles = cursor.fetchall()
            profile_data['profiles_found'] = len(profiles)

            logger.info(f"[CLOUDSQL] 🎤 Found {len(profiles)} voice profile(s) in database")

            for profile in profiles:
                (speaker_id, speaker_name, voiceprint_embedding, total_samples,
                 last_updated, recognition_confidence, successful_verifications,
                 failed_verifications) = profile

                # Check if embedding exists (stored as binary/bytea)
                embedding_valid = voiceprint_embedding is not None and len(voiceprint_embedding) > 0

                # Get actual sample count from voice_samples table (join by speaker_id)
                cursor.execute("""
                    SELECT COUNT(*) FROM voice_samples
                    WHERE speaker_id = %s
                """, (speaker_id,))
                actual_sample_count = cursor.fetchone()[0]

                profile_data['total_samples'] += actual_sample_count

                # Calculate average confidence from verifications
                total_verifications = successful_verifications + failed_verifications
                avg_confidence = recognition_confidence if recognition_confidence else 0.0

                speaker_info = {
                    'speaker_id': speaker_id,
                    'speaker_name': speaker_name,
                    'embedding_valid': embedding_valid,
                    'embedding_size': len(voiceprint_embedding) if voiceprint_embedding else 0,
                    'total_samples': total_samples,
                    'actual_samples_in_db': actual_sample_count,
                    'last_trained': last_updated.isoformat() if last_updated else None,
                    'avg_confidence': float(avg_confidence),
                    'successful_verifications': successful_verifications or 0,
                    'failed_verifications': failed_verifications or 0,
                    'total_verifications': total_verifications,
                    'ready': embedding_valid and actual_sample_count > 0
                }

                profile_data['speakers'].append(speaker_info)

                logger.info(f"[CLOUDSQL]   └─ {speaker_name}: "
                           f"{'✅' if speaker_info['ready'] else '❌'} "
                           f"{actual_sample_count} samples, "
                           f"embedding: {'valid' if embedding_valid else 'MISSING'}, "
                           f"verifications: {successful_verifications}/{total_verifications}")

                # Check for issues
                if not embedding_valid:
                    profile_data['issues'].append(f"{speaker_name}: Embedding missing or corrupted")
                if actual_sample_count == 0:
                    profile_data['issues'].append(f"{speaker_name}: No voice samples found")
                elif actual_sample_count < 10:
                    profile_data['issues'].append(f"{speaker_name}: Only {actual_sample_count} samples (recommend 30+)")

            # =================================================================
            # AUTO-CLEANUP: Remove invalid profiles (unknown, test, etc.)
            # =================================================================
            INVALID_NAMES = {'unknown', 'test', 'placeholder', 'anonymous', 'guest', 'none', 'null'}
            profiles_to_cleanup = []

            # First, identify primary user(s) dynamically from is_primary_user flag
            cursor.execute("""
                SELECT speaker_id, speaker_name FROM speaker_profiles
                WHERE is_primary_user = TRUE
            """)
            primary_users = {row[0]: row[1] for row in cursor.fetchall()}

            for speaker in profile_data['speakers']:
                speaker_name = speaker['speaker_name']
                speaker_id = speaker['speaker_id']
                is_primary = speaker_id in primary_users

                # Check if profile should be cleaned up:
                # 1. Invalid placeholder names (unknown, test, etc.)
                # 2. Missing embedding AND not a primary user (don't delete owner even if embedding is temporarily missing)
                should_cleanup = (
                    speaker_name.lower() in INVALID_NAMES or
                    (not speaker['embedding_valid'] and not is_primary)
                )

                if should_cleanup:
                    profiles_to_cleanup.append(speaker)

            if profiles_to_cleanup:
                logger.info(f"[CLOUDSQL] 🧹 Auto-cleaning {len(profiles_to_cleanup)} invalid profile(s)...")
                for invalid_profile in profiles_to_cleanup:
                    try:
                        speaker_id = invalid_profile['speaker_id']
                        speaker_name = invalid_profile['speaker_name']

                        # Delete voice samples first, then profile
                        cursor.execute("DELETE FROM voice_samples WHERE speaker_id = %s", (speaker_id,))
                        cursor.execute("DELETE FROM speaker_profiles WHERE speaker_id = %s", (speaker_id,))

                        logger.info(f"[CLOUDSQL] 🧹 Removed invalid profile: {speaker_name}")

                        # Remove from issues list
                        profile_data['issues'] = [
                            issue for issue in profile_data['issues']
                            if speaker_name not in issue
                        ]
                        profile_data['speakers'] = [
                            s for s in profile_data['speakers']
                            if s['speaker_id'] != speaker_id
                        ]
                        profile_data['profiles_found'] -= 1

                    except Exception as cleanup_error:
                        logger.warning(f"[CLOUDSQL] ⚠️ Failed to cleanup {speaker_name}: {cleanup_error}")

                conn.commit()
                logger.info(f"[CLOUDSQL] ✅ Cleanup complete - removed {len(profiles_to_cleanup)} invalid profile(s)")

            # Determine overall status (after cleanup)
            valid_profiles = [s for s in profile_data['speakers'] if s['embedding_valid']]
            if len(valid_profiles) == 0:
                profile_data['status'] = 'no_profiles'
                profile_data['ready_for_unlock'] = False
            elif profile_data['issues']:
                # Only flag as issues if there are real issues (not just cleaned up ones)
                profile_data['status'] = 'issues_found'
                profile_data['ready_for_unlock'] = len(valid_profiles) > 0  # Ready if at least one valid
            else:
                profile_data['status'] = 'ready'
                profile_data['ready_for_unlock'] = True

            logger.info(f"[CLOUDSQL] 🎤 Voice Profile Status: {profile_data['status'].upper()} "
                       f"({'✅ Ready' if profile_data['ready_for_unlock'] else '⚠️  Not Ready'})")

        except Exception as e:
            logger.error(f"[CLOUDSQL] ❌ Voice profile check failed: {e}")
            profile_data['status'] = 'check_failed'
            profile_data['error'] = str(e)

        finally:
            # CRITICAL: Always close cursor and connection to prevent leaks
            if cursor:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

        return profile_data


def get_proxy_manager() -> CloudSQLProxyManager:
    """Get singleton proxy manager instance."""
    if not hasattr(get_proxy_manager, "_instance"):
        get_proxy_manager._instance = CloudSQLProxyManager()
    return get_proxy_manager._instance


if __name__ == "__main__":
    # CLI for manual testing
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    parser = argparse.ArgumentParser(description="Cloud SQL Proxy Manager")
    parser.add_argument(
        "command", choices=["start", "stop", "restart", "status", "install", "uninstall"]
    )
    parser.add_argument("--force", action="store_true", help="Force restart if already running")

    args = parser.parse_args()

    manager = CloudSQLProxyManager()

    if args.command == "start":
        success = asyncio.run(manager.start(force_restart=args.force))
        sys.exit(0 if success else 1)
    elif args.command == "stop":
        success = asyncio.run(manager.stop())
        sys.exit(0 if success else 1)
    elif args.command == "restart":
        success = asyncio.run(manager.restart())
        sys.exit(0 if success else 1)
    elif args.command == "status":
        if manager.is_running():
            print("✅ Cloud SQL proxy is running")
            sys.exit(0)
        else:
            print("❌ Cloud SQL proxy is not running")
            sys.exit(1)
    elif args.command == "install":
        success = manager.install_system_service()
        sys.exit(0 if success else 1)
    elif args.command == "uninstall":
        success = manager.uninstall_system_service()
        sys.exit(0 if success else 1)
