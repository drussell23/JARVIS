#!/usr/bin/env python3
"""
Advanced Cloud SQL Proxy Manager v218.0
========================================

Dynamic, robust proxy lifecycle management with:
- Zero hardcoding - all config from database_config.json
- Auto-discovery of proxy binary location
- System-level persistence (launchd on macOS, systemd on Linux)
- Runtime health monitoring and auto-recovery
- Graceful degradation to SQLite fallback
- Port conflict resolution
- Multi-platform support

v218.0 CRITICAL FIX:
- Intelligent credential type detection (authorized_user vs service_account)
- authorized_user credentials work via ADC, NOT --credentials-file
- service_account credentials use --credentials-file
- This fixes the "exit code 1" crash when using gcloud auth credentials
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
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# v218.0: INTELLIGENT CREDENTIAL TYPE SYSTEM
# =============================================================================

class GCPCredentialType(Enum):
    """Types of GCP credentials with different proxy authentication strategies."""
    SERVICE_ACCOUNT = "service_account"      # Use --credentials-file
    AUTHORIZED_USER = "authorized_user"       # Use ADC (env var or implicit)
    EXTERNAL_ACCOUNT = "external_account"     # Workload identity federation
    IMPERSONATED = "impersonated_service_account"  # Service account impersonation
    METADATA_SERVER = "metadata_server"       # GCE/GKE metadata (no file needed)
    UNKNOWN = "unknown"                       # Fallback


@dataclass
class GCPCredentialInfo:
    """
    Detailed information about discovered GCP credentials.
    
    v218.0: Provides intelligent credential routing for Cloud SQL Proxy.
    """
    type: GCPCredentialType
    path: Optional[str]
    project_id: Optional[str]
    account: Optional[str]
    is_valid: bool
    error_message: Optional[str] = None
    
    @property
    def requires_credentials_file_flag(self) -> bool:
        """
        Determine if this credential type requires --credentials-file flag.
        
        CRITICAL: authorized_user credentials must NOT use --credentials-file
        because cloud-sql-proxy expects a service account JSON, not OAuth2 tokens.
        """
        return self.type == GCPCredentialType.SERVICE_ACCOUNT
    
    @property
    def uses_application_default_credentials(self) -> bool:
        """
        Determine if credentials work via Application Default Credentials.
        
        ADC is used for:
        - authorized_user (from gcloud auth application-default login)
        - external_account (workload identity)
        - impersonated credentials
        """
        return self.type in (
            GCPCredentialType.AUTHORIZED_USER,
            GCPCredentialType.EXTERNAL_ACCOUNT,
            GCPCredentialType.IMPERSONATED,
        )
    
    @property
    def proxy_env_vars(self) -> Dict[str, str]:
        """
        Get environment variables needed for the proxy based on credential type.
        """
        env = {}
        
        if self.uses_application_default_credentials and self.path:
            # Set GOOGLE_APPLICATION_CREDENTIALS for ADC
            env["GOOGLE_APPLICATION_CREDENTIALS"] = self.path
        
        if self.project_id:
            env["GOOGLE_CLOUD_PROJECT"] = self.project_id
            env["GCLOUD_PROJECT"] = self.project_id
        
        return env
    
    def get_proxy_auth_args(self) -> list:
        """
        Get command-line arguments for cloud-sql-proxy authentication.
        
        CRITICAL FIX v218.0:
        - authorized_user: NO --credentials-file (causes exit code 1!)
        - service_account: YES --credentials-file
        """
        args = []
        
        if self.requires_credentials_file_flag and self.path:
            args.append(f"--credentials-file={self.path}")
        
        # For ADC types, don't add --credentials-file
        # The proxy will use GOOGLE_APPLICATION_CREDENTIALS automatically
        
        return args


class IntelligentCredentialDetector:
    """
    v218.0: Enterprise-grade GCP credential detection with type awareness.
    
    Fixes the root cause of proxy crashes by correctly identifying credential
    types and using appropriate authentication strategies.
    """
    
    # Standard credential search paths
    CREDENTIAL_SEARCH_PATHS = [
        # gcloud ADC (most common for developers)
        Path.home() / ".config" / "gcloud" / "application_default_credentials.json",
        # Alternative gcloud location
        Path.home() / ".gcloud" / "application_default_credentials.json",
        # Service account key (common in CI/CD)
        Path.home() / ".jarvis" / "gcp" / "service_account.json",
        # System-wide credentials
        Path("/etc/google/auth/application_default_credentials.json"),
    ]
    
    @classmethod
    def detect_credentials(cls) -> GCPCredentialInfo:
        """
        Detect and validate GCP credentials with type identification.
        
        Returns:
            GCPCredentialInfo with type, path, and validation status.
        """
        # Check environment variable first
        env_creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if env_creds_path:
            path = Path(env_creds_path)
            if path.exists():
                return cls._analyze_credentials_file(str(path))
            else:
                logger.warning(
                    f"[CredentialDetector] GOOGLE_APPLICATION_CREDENTIALS set but file not found: {env_creds_path}"
                )
        
        # Check if running on GCE/GKE (metadata server)
        if cls._has_metadata_server():
            return GCPCredentialInfo(
                type=GCPCredentialType.METADATA_SERVER,
                path=None,
                project_id=cls._get_project_from_metadata(),
                account=None,
                is_valid=True,
            )
        
        # Search standard locations
        for search_path in cls.CREDENTIAL_SEARCH_PATHS:
            if search_path.exists():
                result = cls._analyze_credentials_file(str(search_path))
                if result.is_valid:
                    logger.info(
                        f"[CredentialDetector] ✅ Found valid {result.type.value} credentials at {search_path}"
                    )
                    return result
        
        # No credentials found
        return GCPCredentialInfo(
            type=GCPCredentialType.UNKNOWN,
            path=None,
            project_id=None,
            account=None,
            is_valid=False,
            error_message="No GCP credentials found. Run: gcloud auth application-default login",
        )
    
    @classmethod
    def _analyze_credentials_file(cls, path: str) -> GCPCredentialInfo:
        """
        Analyze a credentials file to determine its type and validity.
        
        CRITICAL: This properly identifies authorized_user vs service_account
        to prevent the proxy crash caused by using OAuth tokens with --credentials-file.
        """
        try:
            with open(path) as f:
                creds = json.load(f)
            
            cred_type_str = creds.get("type", "")
            project_id = creds.get("quota_project_id") or creds.get("project_id")
            account = creds.get("client_email") or creds.get("account")
            
            # Map string type to enum
            type_mapping = {
                "service_account": GCPCredentialType.SERVICE_ACCOUNT,
                "authorized_user": GCPCredentialType.AUTHORIZED_USER,
                "external_account": GCPCredentialType.EXTERNAL_ACCOUNT,
                "impersonated_service_account": GCPCredentialType.IMPERSONATED,
            }
            cred_type = type_mapping.get(cred_type_str, GCPCredentialType.UNKNOWN)
            
            # Validate based on type
            is_valid = False
            error_msg = None
            
            if cred_type == GCPCredentialType.SERVICE_ACCOUNT:
                # Service account needs client_email and private_key
                if creds.get("client_email") and creds.get("private_key"):
                    is_valid = True
                else:
                    error_msg = "Service account credentials missing client_email or private_key"
                    
            elif cred_type == GCPCredentialType.AUTHORIZED_USER:
                # authorized_user needs client_id, client_secret, refresh_token
                if creds.get("client_id") and creds.get("refresh_token"):
                    is_valid = True
                else:
                    error_msg = "Authorized user credentials missing client_id or refresh_token"
                    
            elif cred_type == GCPCredentialType.EXTERNAL_ACCOUNT:
                # External account (workload identity)
                if creds.get("audience") or creds.get("credential_source"):
                    is_valid = True
                else:
                    error_msg = "External account credentials incomplete"
                    
            elif cred_type == GCPCredentialType.UNKNOWN:
                error_msg = f"Unknown credential type: {cred_type_str}"
            
            return GCPCredentialInfo(
                type=cred_type,
                path=path,
                project_id=project_id,
                account=account,
                is_valid=is_valid,
                error_message=error_msg,
            )
            
        except json.JSONDecodeError as e:
            return GCPCredentialInfo(
                type=GCPCredentialType.UNKNOWN,
                path=path,
                project_id=None,
                account=None,
                is_valid=False,
                error_message=f"Invalid JSON in credentials file: {e}",
            )
        except Exception as e:
            return GCPCredentialInfo(
                type=GCPCredentialType.UNKNOWN,
                path=path,
                project_id=None,
                account=None,
                is_valid=False,
                error_message=f"Failed to read credentials: {e}",
            )
    
    @classmethod
    def _has_metadata_server(cls) -> bool:
        """Check if running on GCE/GKE with metadata server access."""
        try:
            import socket
            # Metadata server is always at this IP on GCE/GKE
            socket.setdefaulttimeout(0.1)
            socket.socket().connect(("169.254.169.254", 80))
            return True
        except Exception:
            return False
    
    @classmethod
    def _get_project_from_metadata(cls) -> Optional[str]:
        """Get project ID from metadata server."""
        try:
            import urllib.request
            req = urllib.request.Request(
                "http://169.254.169.254/computeMetadata/v1/project/project-id",
                headers={"Metadata-Flavor": "Google"}
            )
            with urllib.request.urlopen(req, timeout=1) as resp:
                return resp.read().decode()
        except Exception:
            return None


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
        """Check if port is already in use (sync version for non-async contexts)."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("127.0.0.1", port)) == 0

    async def _is_port_in_use_async(self, port: int) -> bool:
        """Check if port is already in use (async version - doesn't block event loop)."""
        return await asyncio.to_thread(self._is_port_in_use, port)

    def _get_process_name_from_pid(self, pid: int) -> Optional[str]:
        """
        Get process name from PID for zombie detection.

        v117.0: Enhanced with full command line fallback for more robust detection.
        - Primary: Get short process name (comm)
        - Fallback: Get full command line (args) for more context

        Returns:
            Process name/command if available, None if process doesn't exist.
        """
        try:
            if self.system == "Darwin":
                # macOS: Try full command line first (more reliable for Homebrew binaries)
                result = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "args="],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()

                # Fallback to short name
                result = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "comm="],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()

            elif self.system == "Linux":
                # Linux: read from /proc
                cmdline_path = Path(f"/proc/{pid}/cmdline")
                if cmdline_path.exists():
                    cmdline = cmdline_path.read_text().replace('\x00', ' ').strip()
                    return cmdline

            elif self.system == "Windows":
                # Windows: use tasklist
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0 and result.stdout.strip():
                    parts = result.stdout.strip().split(',')
                    if len(parts) >= 1:
                        return parts[0].strip('"')
        except Exception as e:
            logger.debug(f"[ProxyManager] Failed to get process name for PID {pid}: {e}")

        return None

    def _is_cloud_sql_proxy_process(self, pid: int) -> bool:
        """
        Check if a PID is actually a cloud-sql-proxy process.

        v117.0: Enhanced with multiple detection strategies and path-aware matching.
        - Checks process name, command line, and executable path
        - Handles Homebrew installations (/opt/homebrew/bin/cloud-sql-proxy)
        - Handles gcloud-installed proxies

        Returns:
            True if PID is a cloud-sql-proxy process, False otherwise.
        """
        proc_name = self._get_process_name_from_pid(pid)
        if not proc_name:
            # v117.0: Try psutil as fallback for more robust detection
            try:
                import psutil
                process = psutil.Process(pid)
                proc_name = process.name()
                if not proc_name:
                    # Try cmdline
                    cmdline = process.cmdline()
                    if cmdline:
                        proc_name = ' '.join(cmdline)
            except Exception:
                pass

        if not proc_name:
            return False

        # v117.0: Enhanced keyword matching with path components
        proc_name_lower = proc_name.lower()
        keywords = [
            'cloud-sql-proxy',
            'cloud_sql_proxy',
            'cloudsqlproxy',
            '/cloud-sql-proxy',  # Path component match
            'bin/cloud-sql-proxy',  # Binary path match
        ]
        return any(keyword in proc_name_lower for keyword in keywords)

    def _get_pid_using_port(self, port: int) -> Optional[int]:
        """
        Get the PID of the process LISTENING on a port (server, not clients).

        v117.0: Fixed zombie detection false positives by filtering for LISTEN state only.
        Previous versions used `lsof -t -i :PORT` which returned ALL connections
        including client connections, causing race conditions during startup where
        a Python client PID could be returned instead of the proxy server PID.

        Returns:
            PID of the LISTENING process if found, None otherwise.
        """
        try:
            if self.system == "Darwin" or self.system == "Linux":
                # v117.0: Use -sTCP:LISTEN to filter for LISTENING sockets only
                # This prevents false positives from client connections during startup
                result = subprocess.run(
                    ["lsof", "-t", "-iTCP:" + str(port), "-sTCP:LISTEN"],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if result.returncode == 0 and result.stdout.strip():
                    # lsof may return multiple PIDs if multiple processes listen
                    # (e.g., IPv4 + IPv6), but they should all be the same process
                    pids = result.stdout.strip().split('\n')
                    if pids:
                        try:
                            # Deduplicate PIDs (same process may appear for IPv4/IPv6)
                            unique_pids = list(set(int(p) for p in pids if p.strip()))
                            if unique_pids:
                                if len(unique_pids) > 1:
                                    logger.warning(
                                        f"[ProxyManager] Multiple processes LISTEN on port {port}: "
                                        f"{unique_pids} - using first one"
                                    )
                                return unique_pids[0]
                        except ValueError:
                            pass

                # v117.0: Fallback to full lsof if strict LISTEN filter returns nothing
                # (handles edge cases where socket state reporting varies)
                result_fallback = subprocess.run(
                    ["lsof", "-t", "-i", f":{port}"],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if result_fallback.returncode == 0 and result_fallback.stdout.strip():
                    pids = result_fallback.stdout.strip().split('\n')
                    if pids:
                        # v117.0: When using fallback, validate each PID is actually
                        # the server by checking if it's cloud-sql-proxy
                        for pid_str in pids:
                            try:
                                pid = int(pid_str.strip())
                                if self._is_cloud_sql_proxy_process(pid):
                                    return pid
                            except ValueError:
                                continue
                        # If no cloud-sql-proxy found, return first PID with warning
                        try:
                            first_pid = int(pids[0])
                            logger.debug(
                                f"[ProxyManager] No cloud-sql-proxy in PIDs {pids}, "
                                f"using first PID {first_pid}"
                            )
                            return first_pid
                        except ValueError:
                            pass

            elif self.system == "Windows":
                # Use netstat to find process on port (already filters LISTENING)
                result = subprocess.run(
                    ["netstat", "-ano"],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if result.returncode == 0:
                    for line in result.stdout.split('\n'):
                        if f":{port}" in line and "LISTENING" in line:
                            parts = line.split()
                            if parts:
                                try:
                                    return int(parts[-1])
                                except ValueError:
                                    pass
        except Exception as e:
            logger.debug(f"[ProxyManager] Failed to get PID for port {port}: {e}")

        return None

    def detect_zombie_state(self, retry_on_zombie: bool = True) -> Dict:
        """
        Detect zombie proxy state - port held but not by our proxy.

        v117.0: Enhanced with verification retry to prevent startup race condition false positives.
        During startup, there's a brief window where lsof may return the wrong PID before
        the proxy is fully ready. The retry mechanism waits 500ms and re-checks to confirm
        a zombie before declaring one.

        Args:
            retry_on_zombie: If True, retry detection once after 500ms delay when zombie
                           is suspected. This prevents false positives during startup.
                           Set to False for synchronous operations.

        Returns:
            Dict with:
            - is_zombie: True if port held by non-proxy process (verified)
            - port_in_use: True if port is occupied
            - pid_on_port: PID of process on port (if any)
            - is_cloud_sql_proxy: True if PID is actually cloud-sql-proxy
            - our_pid_valid: True if our PID file points to live cloud-sql-proxy
            - recommendation: Action to take
            - verified: True if zombie state was verified with retry (v117.0)
        """
        port = self.config["cloud_sql"]["port"]
        result = {
            'port': port,
            'is_zombie': False,
            'port_in_use': False,
            'pid_on_port': None,
            'is_cloud_sql_proxy': False,
            'our_pid_valid': False,
            'recommendation': 'none',
            'verified': False  # v117.0: Track verification status
        }

        # Check if port is in use
        result['port_in_use'] = self._is_port_in_use(port)

        if not result['port_in_use']:
            result['recommendation'] = 'start_proxy'
            return result

        # Port is in use - find out who
        pid_on_port = self._get_pid_using_port(port)
        result['pid_on_port'] = pid_on_port

        if pid_on_port:
            result['is_cloud_sql_proxy'] = self._is_cloud_sql_proxy_process(pid_on_port)

        # Check our PID file
        if self.pid_path.exists():
            try:
                with open(self.pid_path) as f:
                    our_pid = int(f.read().strip())

                # Check if our PID is still alive and is cloud-sql-proxy
                try:
                    os.kill(our_pid, 0)  # Check if alive
                    if self._is_cloud_sql_proxy_process(our_pid):
                        result['our_pid_valid'] = True
                except ProcessLookupError:
                    # Our PID is dead - stale PID file
                    logger.warning(f"[ProxyManager] Stale PID file detected (PID {our_pid} dead)")
                    self.pid_path.unlink(missing_ok=True)
            except (ValueError, OSError):
                logger.warning("[ProxyManager] Corrupted PID file - removing")
                self.pid_path.unlink(missing_ok=True)

        # Determine zombie state
        if result['port_in_use'] and not result['is_cloud_sql_proxy']:
            # v117.0: Potential zombie - verify with retry to avoid false positives
            if retry_on_zombie:
                logger.debug(
                    f"[ProxyManager] Potential zombie detected on port {port} "
                    f"(PID {pid_on_port}), verifying with 500ms delay..."
                )
                import time
                time.sleep(0.5)  # Wait for potential startup race to resolve

                # Re-check without retry to avoid infinite loop
                verification = self.detect_zombie_state(retry_on_zombie=False)

                if verification['is_cloud_sql_proxy']:
                    # False positive - proxy is now detected correctly
                    logger.info(
                        f"[ProxyManager] Zombie false positive resolved: "
                        f"PID {verification['pid_on_port']} is now recognized as cloud-sql-proxy"
                    )
                    verification['verified'] = True
                    return verification
                else:
                    # Confirmed zombie
                    result['is_zombie'] = True
                    result['verified'] = True
                    result['recommendation'] = 'kill_conflicting'
                    logger.warning(
                        f"[ProxyManager] ZOMBIE CONFIRMED: Port {port} held by PID {pid_on_port} "
                        f"which is NOT cloud-sql-proxy (verified after retry)"
                    )
            else:
                # No retry requested - report potential zombie
                result['is_zombie'] = True
                result['recommendation'] = 'kill_conflicting'
                logger.warning(
                    f"[ProxyManager] ZOMBIE detected: Port {port} held by PID {pid_on_port} "
                    f"which is NOT cloud-sql-proxy"
                )
        elif result['port_in_use'] and result['is_cloud_sql_proxy'] and not result['our_pid_valid']:
            # Proxy running but not from our PID file - orphan
            result['recommendation'] = 'adopt_or_restart'
            result['verified'] = True
            logger.info(
                f"[ProxyManager] Orphan proxy detected: cloud-sql-proxy on port {port} "
                f"(PID {pid_on_port}) not managed by us"
            )
        elif result['port_in_use'] and result['is_cloud_sql_proxy'] and result['our_pid_valid']:
            # Our proxy is running correctly
            result['recommendation'] = 'healthy'
            result['verified'] = True
        else:
            result['recommendation'] = 'investigate'

        return result

    async def detect_zombie_state_async(self, retry_on_zombie: bool = True) -> Dict:
        """
        Detect zombie proxy state - port held but not by our proxy (async version).

        This version doesn't block the event loop, making it safe for async contexts.
        Uses asyncio.sleep instead of time.sleep and async subprocess for process detection.

        Args:
            retry_on_zombie: If True, retry detection once after 500ms delay when zombie
                           is suspected. This prevents false positives during startup.

        Returns:
            Same as detect_zombie_state() - Dict with zombie detection results.
        """
        port = self.config["cloud_sql"]["port"]
        result = {
            'port': port,
            'is_zombie': False,
            'port_in_use': False,
            'pid_on_port': None,
            'is_cloud_sql_proxy': False,
            'our_pid_valid': False,
            'recommendation': 'none',
            'verified': False
        }

        # Check if port is in use (async)
        result['port_in_use'] = await self._is_port_in_use_async(port)

        if not result['port_in_use']:
            result['recommendation'] = 'start_proxy'
            return result

        # Port is in use - find out who (run in thread to avoid blocking)
        pid_on_port = await asyncio.to_thread(self._get_pid_using_port, port)
        result['pid_on_port'] = pid_on_port

        if pid_on_port:
            # Check if it's a cloud-sql-proxy process (run in thread)
            result['is_cloud_sql_proxy'] = await asyncio.to_thread(
                self._is_cloud_sql_proxy_process, pid_on_port
            )

        # Check our PID file (run file I/O in thread)
        if self.pid_path.exists():
            try:
                our_pid = await asyncio.to_thread(
                    lambda: int(self.pid_path.read_text().strip())
                )
                # Check if our PID is still alive and is cloud-sql-proxy
                try:
                    os.kill(our_pid, 0)  # Check if alive (fast, doesn't need thread)
                    if await asyncio.to_thread(self._is_cloud_sql_proxy_process, our_pid):
                        result['our_pid_valid'] = True
                except ProcessLookupError:
                    logger.warning(f"[ProxyManager] Stale PID file detected (PID {our_pid} dead)")
                    await asyncio.to_thread(lambda: self.pid_path.unlink(missing_ok=True))
            except (ValueError, OSError):
                logger.warning("[ProxyManager] Corrupted PID file - removing")
                await asyncio.to_thread(lambda: self.pid_path.unlink(missing_ok=True))

        # Determine zombie state
        if result['port_in_use'] and not result['is_cloud_sql_proxy']:
            if retry_on_zombie:
                logger.debug(
                    f"[ProxyManager] Potential zombie detected on port {port} "
                    f"(PID {pid_on_port}), verifying with 500ms delay..."
                )
                await asyncio.sleep(0.5)  # Non-blocking sleep

                # Re-check without retry to avoid infinite loop
                verification = await self.detect_zombie_state_async(retry_on_zombie=False)

                if verification['is_cloud_sql_proxy']:
                    logger.info(
                        f"[ProxyManager] Zombie false positive resolved: "
                        f"PID {verification['pid_on_port']} is now recognized as cloud-sql-proxy"
                    )
                    verification['verified'] = True
                    return verification
                else:
                    result['is_zombie'] = True
                    result['verified'] = True
                    result['recommendation'] = 'kill_conflicting'
                    logger.warning(
                        f"[ProxyManager] ZOMBIE CONFIRMED: Port {port} held by PID {pid_on_port} "
                        f"which is NOT cloud-sql-proxy (verified after retry)"
                    )
            else:
                result['is_zombie'] = True
                result['recommendation'] = 'kill_conflicting'
                logger.warning(
                    f"[ProxyManager] ZOMBIE detected: Port {port} held by PID {pid_on_port} "
                    f"which is NOT cloud-sql-proxy"
                )
        elif result['port_in_use'] and result['is_cloud_sql_proxy'] and not result['our_pid_valid']:
            result['recommendation'] = 'adopt_or_restart'
            result['verified'] = True
            logger.info(
                f"[ProxyManager] Orphan proxy detected: cloud-sql-proxy on port {port} "
                f"(PID {pid_on_port}) not managed by us"
            )
        elif result['port_in_use'] and result['is_cloud_sql_proxy'] and result['our_pid_valid']:
            result['recommendation'] = 'healthy'
            result['verified'] = True
        else:
            result['recommendation'] = 'investigate'

        return result

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

    async def _find_proxy_processes_async(self) -> list:
        """Find all running cloud-sql-proxy processes (async - doesn't block event loop)."""
        try:
            if self.system == "Darwin" or self.system == "Linux":
                proc = await asyncio.create_subprocess_exec(
                    "pgrep", "-f", "cloud-sql-proxy",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
                if proc.returncode == 0:
                    return [int(pid) for pid in stdout.decode().strip().split()]
            elif self.system == "Windows":
                proc = await asyncio.create_subprocess_exec(
                    "tasklist", "/FI", "IMAGENAME eq cloud-sql-proxy*",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
                # Parse Windows tasklist output
                pids = []
                for line in stdout.decode().split("\n")[3:]:
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

    async def _get_process_name_from_pid_async(self, pid: int) -> Optional[str]:
        """Get process name from PID (async - doesn't block event loop)."""
        try:
            if self.system == "Darwin":
                proc = await asyncio.create_subprocess_exec(
                    "ps", "-p", str(pid), "-o", "args=",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                if proc.returncode == 0 and stdout.decode().strip():
                    return stdout.decode().strip()
                # Fallback to short name
                proc = await asyncio.create_subprocess_exec(
                    "ps", "-p", str(pid), "-o", "comm=",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                if proc.returncode == 0 and stdout.decode().strip():
                    return stdout.decode().strip()
            elif self.system == "Linux":
                cmdline_path = Path(f"/proc/{pid}/cmdline")
                if cmdline_path.exists():
                    # Use to_thread for file I/O
                    cmdline = await asyncio.to_thread(lambda: cmdline_path.read_text().replace('\x00', ' ').strip())
                    return cmdline
            elif self.system == "Windows":
                proc = await asyncio.create_subprocess_exec(
                    "tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                if proc.returncode == 0 and stdout.decode().strip():
                    parts = stdout.decode().strip().split(',')
                    if len(parts) >= 1:
                        return parts[0].strip('"')
        except Exception as e:
            logger.debug(f"[ProxyManager] Failed to get process name for PID {pid}: {e}")
        return None

    def is_running(self, strict: bool = False) -> bool:
        """
        Check if proxy is running and healthy.

        v86.0: Enhanced with zombie detection and strict mode.

        Args:
            strict: If True, requires our PID file to be valid.
                   If False, accepts any cloud-sql-proxy on the port.

        Returns:
            True if a healthy cloud-sql-proxy is running on our port.
        """
        port = self.config["cloud_sql"]["port"]

        # Quick check: is port in use?
        if not self._is_port_in_use(port):
            return False

        # Check if PID file exists and process is alive
        if self.pid_path.exists():
            try:
                with open(self.pid_path) as f:
                    pid = int(f.read().strip())

                # Check if process exists and is cloud-sql-proxy
                try:
                    os.kill(pid, 0)  # Doesn't actually kill, just checks
                    if self._is_cloud_sql_proxy_process(pid):
                        return True
                    else:
                        # PID is alive but not cloud-sql-proxy - stale PID file
                        logger.warning(
                            f"[ProxyManager] PID file points to non-proxy process "
                            f"(PID {pid}) - cleaning up"
                        )
                        self.pid_path.unlink(missing_ok=True)
                except ProcessLookupError:
                    # Process dead - stale PID file
                    self.pid_path.unlink(missing_ok=True)
            except (ValueError, OSError):
                # Corrupted PID file
                self.pid_path.unlink(missing_ok=True)

        # In strict mode, we require our PID file to be valid
        if strict:
            return False

        # Non-strict mode: check if any cloud-sql-proxy is on the port
        pid_on_port = self._get_pid_using_port(port)
        if pid_on_port and self._is_cloud_sql_proxy_process(pid_on_port):
            # Adopt this proxy - update our PID file
            logger.info(f"[ProxyManager] Adopting orphan cloud-sql-proxy (PID {pid_on_port})")
            try:
                self.pid_path.write_text(str(pid_on_port))
            except OSError as e:
                logger.debug(f"[ProxyManager] Failed to update PID file: {e}")
            return True

        # Port is in use but not by cloud-sql-proxy - zombie state
        if pid_on_port:
            logger.warning(
                f"[ProxyManager] Port {port} held by non-proxy process "
                f"(PID {pid_on_port}) - zombie detected"
            )

        return False

    async def is_running_async(self, strict: bool = False) -> bool:
        """
        Check if proxy is running and healthy (async version - doesn't block event loop).

        v124.0: Fully async implementation using asyncio.to_thread for all blocking operations.
        This is the preferred method to call from async contexts like Phase 6 initialization.

        Args:
            strict: If True, requires our PID file to be valid.
                   If False, accepts any cloud-sql-proxy on the port.

        Returns:
            True if a healthy cloud-sql-proxy is running on our port.
        """
        port = self.config["cloud_sql"]["port"]

        # Quick check: is port in use? (async - doesn't block)
        if not await self._is_port_in_use_async(port):
            return False

        # Check if PID file exists and process is alive (run in thread pool)
        def _check_pid_file_sync():
            if self.pid_path.exists():
                try:
                    with open(self.pid_path) as f:
                        pid = int(f.read().strip())

                    # Check if process exists and is cloud-sql-proxy
                    try:
                        os.kill(pid, 0)  # Doesn't actually kill, just checks
                        if self._is_cloud_sql_proxy_process(pid):
                            return ("found", pid)
                        else:
                            # PID is alive but not cloud-sql-proxy - stale PID file
                            self.pid_path.unlink(missing_ok=True)
                            return ("stale", None)
                    except ProcessLookupError:
                        # Process dead - stale PID file
                        self.pid_path.unlink(missing_ok=True)
                        return ("dead", None)
                except (ValueError, OSError):
                    # Corrupted PID file
                    self.pid_path.unlink(missing_ok=True)
                    return ("corrupted", None)
            return ("no_file", None)

        result, pid = await asyncio.to_thread(_check_pid_file_sync)
        if result == "found":
            return True

        # In strict mode, we require our PID file to be valid
        if strict:
            return False

        # Non-strict mode: check if any cloud-sql-proxy is on the port (run in thread pool)
        def _check_port_process_sync():
            pid_on_port = self._get_pid_using_port(port)
            if pid_on_port and self._is_cloud_sql_proxy_process(pid_on_port):
                # Adopt this proxy - update our PID file
                logger.info(f"[ProxyManager] Adopting orphan cloud-sql-proxy (PID {pid_on_port})")
                try:
                    self.pid_path.write_text(str(pid_on_port))
                except OSError as e:
                    logger.debug(f"[ProxyManager] Failed to update PID file: {e}")
                return True
            return False

        return await asyncio.to_thread(_check_port_process_sync)

    async def is_running_db_level(self) -> bool:
        """
        Check if proxy is running with DB-level verification.

        v86.0: Uses ProxyReadinessGate for actual database connectivity check.
        v124.0: Now uses is_running_async() to avoid blocking the event loop.

        Returns:
            True if proxy is running AND can connect to database.
        """
        # First check TCP-level (async - doesn't block event loop)
        if not await self.is_running_async():
            return False

        # Then check DB-level via ProxyReadinessGate
        try:
            from intelligence.cloud_sql_connection_manager import (
                get_readiness_gate,
                ReadinessState
            )

            gate = get_readiness_gate()

            # Quick check - is gate already in ready state?
            if gate.state == ReadinessState.READY:
                return True

            # If not, trigger a check with short timeout
            result = await gate.wait_for_ready(timeout=5.0)
            return result.state == ReadinessState.READY

        except ImportError:
            # ProxyReadinessGate not available - fall back to TCP-level
            logger.debug("[ProxyManager] ProxyReadinessGate not available - using TCP-level check")
            return await self.is_running_async()
        except Exception as e:
            logger.debug(f"[ProxyManager] DB-level check failed: {e}")
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

    async def _kill_conflicting_processes_async(self):
        """Kill any conflicting proxy processes (async - doesn't block event loop)."""
        port = self.config["cloud_sql"]["port"]

        if not await self._is_port_in_use_async(port):
            return

        logger.warning(f"⚠️  Port {port} in use, killing conflicting processes...")
        pids = await self._find_proxy_processes_async()

        for pid in pids:
            try:
                logger.info(f"🔪 Killing proxy process: PID {pid}")
                os.kill(pid, signal.SIGTERM)
                await asyncio.sleep(0.5)  # Non-blocking sleep
                # Force kill if still alive
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            except Exception as e:
                logger.debug(f"Error killing PID {pid}: {e}")

        # Wait for port to become available (non-blocking)
        for _ in range(10):
            if not await self._is_port_in_use_async(port):
                break
            await asyncio.sleep(0.5)  # Non-blocking sleep
        else:
            logger.error(f"❌ Failed to free port {port}")

    async def start(self, force_restart: bool = False, max_retries: int = 3) -> bool:
        """
        Start Cloud SQL proxy with health monitoring and auto-recovery.

        v218.0 CRITICAL FIX:
        - Intelligent credential type detection (authorized_user vs service_account)
        - authorized_user credentials (from gcloud auth) DON'T use --credentials-file
        - service_account credentials use --credentials-file
        - This fixes the "exit code 1" crash caused by incorrect auth method

        Args:
            force_restart: Kill existing processes and start fresh
            max_retries: Maximum number of startup attempts

        Returns:
            True if started successfully, False otherwise
        """
        last_error = None

        # v218.0: Use intelligent credential detection to determine auth strategy
        cred_info = IntelligentCredentialDetector.detect_credentials()
        
        if not cred_info.is_valid:
            logger.warning(
                f"[ProxyManager v218.0] ⚠️ GCP credential issue: {cred_info.error_message}\n"
                "   For developer auth: gcloud auth application-default login\n"
                "   For service account: Set GOOGLE_APPLICATION_CREDENTIALS to key file"
            )
        else:
            logger.info(
                f"[ProxyManager v218.0] ✅ Detected {cred_info.type.value} credentials"
                + (f" at {cred_info.path}" if cred_info.path else "")
            )
            if cred_info.type == GCPCredentialType.AUTHORIZED_USER:
                logger.info(
                    "   → Using Application Default Credentials (NOT --credentials-file)"
                )
            elif cred_info.type == GCPCredentialType.SERVICE_ACCOUNT:
                logger.info(
                    "   → Using --credentials-file for service account"
                )
            elif cred_info.type == GCPCredentialType.METADATA_SERVER:
                logger.info(
                    "   → Using GCE/GKE metadata server for authentication"
                )

        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    logger.info(f"🔄 Retry attempt {attempt + 1}/{max_retries}...")
                    await asyncio.sleep(2)  # Wait before retry

                # Check if already running (async - doesn't block event loop)
                if await self.is_running_async() and not force_restart:
                    logger.info("✅ Cloud SQL proxy already running")
                    return True

                # Kill conflicting processes if needed (async - doesn't block event loop)
                if force_restart or attempt > 0:
                    await self._kill_conflicting_processes_async()

                # Build proxy command (no hardcoding!)
                cloud_sql = self.config["cloud_sql"]
                port = cloud_sql["port"]
                connection_name = cloud_sql["connection_name"]

                cmd = [
                    self.proxy_binary,
                    connection_name,
                    f"--port={port}",
                ]

                # v218.0: CRITICAL FIX - Add auth args based on credential TYPE
                # authorized_user credentials MUST NOT use --credentials-file
                # They work via GOOGLE_APPLICATION_CREDENTIALS env var (ADC)
                if cred_info.is_valid:
                    auth_args = cred_info.get_proxy_auth_args()
                    cmd.extend(auth_args)
                    if auth_args:
                        logger.info(f"   Auth method: --credentials-file (service account)")
                    else:
                        logger.info(f"   Auth method: Application Default Credentials")

                # Legacy support: explicit service_account_key in config
                if "auth_method" in cloud_sql:
                    if cloud_sql["auth_method"] == "service_account":
                        if "service_account_key" in cloud_sql:
                            key_path = cloud_sql['service_account_key']
                            # Only add if not already in command
                            if f"--credentials-file={key_path}" not in cmd:
                                cmd.append(f"--credentials-file={key_path}")

                logger.info(f"🚀 Starting Cloud SQL proxy (attempt {attempt + 1}/{max_retries})...")
                logger.info(f"   Binary: {self.proxy_binary}")
                logger.info(f"   Connection: {connection_name}")
                logger.info(f"   Port: {port}")
                logger.info(f"   Log: {self.log_path}")
                logger.info(f"   Command: {' '.join(cmd)}")

                # v218.0: Build environment with credential-type-aware settings
                proxy_env = os.environ.copy()
                
                # Add credential-specific environment variables
                if cred_info.is_valid:
                    cred_env_vars = cred_info.proxy_env_vars
                    proxy_env.update(cred_env_vars)
                    if cred_env_vars:
                        logger.info(f"   Environment: {', '.join(f'{k}=...' for k in cred_env_vars.keys())}")
                
                # Fallback: Set GOOGLE_APPLICATION_CREDENTIALS if not already set
                if "GOOGLE_APPLICATION_CREDENTIALS" not in proxy_env and cred_info.path:
                    proxy_env["GOOGLE_APPLICATION_CREDENTIALS"] = cred_info.path
                    logger.info(f"   GCP credentials (fallback): {cred_info.path}")
                
                def _start_proxy_process_sync():
                    """Sync helper - runs in thread pool."""
                    # Ensure log directory exists
                    self.log_path.parent.mkdir(parents=True, exist_ok=True)

                    # Start proxy process (truncate log file for fresh start)
                    log_file = open(self.log_path, "w")  # Use "w" to truncate old logs
                    process = subprocess.Popen(
                        cmd,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,  # Detach from parent
                        env=proxy_env,  # v217.0: Pass environment with GCP credentials
                    )

                    # Write PID file
                    with open(self.pid_path, "w") as f:
                        f.write(str(process.pid))

                    return process

                self.process = await asyncio.to_thread(_start_proxy_process_sync)
                logger.info(f"   PID: {self.process.pid}")

                # Wait for proxy to be ready (max 15 seconds) - ASYNC!
                # v124.0: All checks in this loop are now async to not block event loop
                logger.info(f"⏳ Waiting for proxy to be ready...")
                for i in range(30):
                    await asyncio.sleep(0.5)  # Non-blocking async sleep

                    # Check if process crashed (wrap poll() in thread)
                    poll_result = await asyncio.to_thread(self.process.poll)
                    if poll_result is not None:
                        def _read_log_sync():
                            return self.log_path.read_text() if self.log_path.exists() else "No log"
                        log_content = await asyncio.to_thread(_read_log_sync)
                        
                        # v218.0: Enhanced error diagnostics with credential-type awareness
                        exit_code = self.process.returncode
                        error_msg = f"Proxy process crashed (exit code: {exit_code})"
                        
                        # v218.0: Credential-type-specific diagnostics
                        if exit_code == 1 and (not log_content.strip() or len(log_content.strip()) < 50):
                            # Exit code 1 with minimal output = authentication failure
                            error_msg += "\n   ═══════════════════════════════════════════════════"
                            error_msg += "\n   AUTHENTICATION FAILURE DETECTED"
                            error_msg += "\n   ═══════════════════════════════════════════════════"
                            
                            if cred_info.type == GCPCredentialType.AUTHORIZED_USER:
                                error_msg += "\n   Using: authorized_user credentials (OAuth2)"
                                error_msg += "\n   Possible causes:"
                                error_msg += "\n   1. OAuth2 token expired - run: gcloud auth application-default login"
                                error_msg += "\n   2. Missing Cloud SQL Client IAM role"
                                error_msg += "\n   3. Network issue reaching OAuth token endpoint"
                            elif cred_info.type == GCPCredentialType.SERVICE_ACCOUNT:
                                error_msg += "\n   Using: service_account credentials"
                                error_msg += "\n   Possible causes:"
                                error_msg += "\n   1. Service account key is invalid or expired"
                                error_msg += "\n   2. Missing Cloud SQL Client IAM role for service account"
                                error_msg += f"\n   3. Wrong project - expected: {cred_info.project_id}"
                            elif not cred_info.is_valid:
                                error_msg += f"\n   Credential issue: {cred_info.error_message}"
                                error_msg += "\n   Run: gcloud auth application-default login"
                            else:
                                error_msg += "\n   Run: gcloud auth application-default login"
                                
                        # Provide specific guidance based on error content
                        elif "unauthorized" in log_content.lower() or "permission denied" in log_content.lower():
                            error_msg += "\n   → Authentication failed. Check IAM permissions."
                        elif "quota" in log_content.lower():
                            error_msg += "\n   → API quota exceeded. Wait or check GCP console."
                        elif "not found" in log_content.lower() and "instance" in log_content.lower():
                            error_msg += "\n   → Cloud SQL instance not found. Check connection_name in database_config.json"
                        
                        if log_content.strip():
                            # Show log content (truncated)
                            truncated_log = log_content[:500] + "..." if len(log_content) > 500 else log_content
                            error_msg += f"\n   Log:\n{truncated_log}"
                        
                        logger.error(f"❌ {error_msg}")
                        last_error = error_msg
                        break

                    # Check if port is now in use (async - doesn't block)
                    if await self._is_port_in_use_async(port):
                        logger.info(f"✅ Cloud SQL proxy ready on port {port} (took {i * 0.5:.1f}s)")
                        return True

                # If we got here, proxy didn't start in time
                poll_result = await asyncio.to_thread(self.process.poll)
                if poll_result is None:
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

    async def monitor(
        self,
        check_interval: int = 30,
        max_recovery_attempts: int = 3,
        db_level_check: bool = True
    ):
        """
        Monitor proxy health and auto-recover if needed.

        v86.0 Enhancements:
        - Optional DB-level health checks via ProxyReadinessGate
        - Enhanced zombie detection before recovery attempts
        - Notification to ProxyReadinessGate on failures

        Args:
            check_interval: Seconds between health checks
            max_recovery_attempts: Maximum consecutive recovery attempts before giving up
            db_level_check: If True, use DB-level verification (SELECT 1) instead of TCP-only
        """
        logger.info(f"🔍 Starting proxy health monitor (interval: {check_interval}s, db_level={db_level_check})")

        consecutive_failures = 0
        last_check_time = time.time()
        readiness_gate = None

        # Try to get ProxyReadinessGate for DB-level checks and failure notification
        if db_level_check:
            try:
                from intelligence.cloud_sql_connection_manager import get_readiness_gate
                readiness_gate = get_readiness_gate()
                logger.info("   Using ProxyReadinessGate for DB-level verification")
            except ImportError:
                logger.debug("   ProxyReadinessGate not available - using TCP-level checks")
                db_level_check = False

        health_check_timeout = float(os.getenv("TIMEOUT_PROXY_HEALTH_CHECK", "30.0"))
        while True:
            try:
                await asyncio.sleep(check_interval)

                # Health check with timeout
                is_healthy = False
                try:
                    if db_level_check and readiness_gate:
                        # v86.0: Use DB-level verification
                        is_healthy = await asyncio.wait_for(
                            self.is_running_db_level(),
                            timeout=health_check_timeout
                        )
                    else:
                        # TCP-level only
                        is_healthy = await asyncio.wait_for(
                            asyncio.get_event_loop().run_in_executor(None, self.is_running),
                            timeout=health_check_timeout
                        )
                except asyncio.TimeoutError:
                    logger.warning("[CloudSQL] Health check timed out")
                    is_healthy = False

                current_time = time.time()
                elapsed = current_time - last_check_time
                last_check_time = current_time

                if not is_healthy:
                    consecutive_failures += 1
                    logger.warning(
                        f"⚠️  Cloud SQL proxy unhealthy "
                        f"(consecutive failures: {consecutive_failures}/{max_recovery_attempts})"
                    )

                    # v86.0: Notify ProxyReadinessGate of failure
                    if readiness_gate:
                        try:
                            readiness_gate.notify_connection_failed()
                        except Exception as e:
                            logger.debug(f"[CloudSQL] Failed to notify gate: {e}")

                    # v86.0: Check for zombie state before recovery (async - doesn't block event loop)
                    zombie_state = await self.detect_zombie_state_async()
                    if zombie_state['is_zombie']:
                        logger.warning(
                            f"[CloudSQL] Zombie detected: port {zombie_state['port']} held by "
                            f"non-proxy process (PID {zombie_state['pid_on_port']})"
                        )
                        # Kill the zombie before attempting recovery (async - doesn't block event loop)
                        await self._kill_conflicting_processes_async()
                        await asyncio.sleep(1)  # Wait for port to be freed

                    if consecutive_failures <= max_recovery_attempts:
                        logger.info(f"🔄 Attempting automatic recovery (attempt {consecutive_failures})...")
                        success = await self.start(force_restart=True)

                        if success:
                            logger.info("✅ Proxy recovered successfully")
                            consecutive_failures = 0  # Reset counter on success

                            # v86.0: Trigger gate re-verification
                            if readiness_gate:
                                try:
                                    await readiness_gate.wait_for_ready(timeout=10.0)
                                except Exception as e:
                                    logger.debug(f"[CloudSQL] Gate re-verification after recovery: {e}")
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
                        check_type = "DB-level" if db_level_check else "TCP-level"
                        logger.debug(f"✅ Cloud SQL proxy healthy ({check_type}, uptime: {elapsed:.0f}s)")

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
                            except Exception:
                                pass
                        if conn:
                            try:
                                conn.close()
                            except Exception:
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
