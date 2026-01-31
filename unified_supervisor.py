#!/usr/bin/env python3
"""
JARVIS Unified System Kernel v1.0.0
═══════════════════════════════════════════════════════════════════════════════

The ONE file that controls the entire JARVIS ecosystem.
This is a Monolithic Kernel - all logic inline, zero external module dependencies.

Merges capabilities from:
- run_supervisor.py (27k lines) - Supervisor, Trinity, Hot Reload
- start_system.py (23k lines) - Docker, GCP, ML Intelligence

Architecture:
    ZONE 0: EARLY PROTECTION      - Signal handling, venv, fast checks
    ZONE 1: FOUNDATION            - Imports, config, constants
    ZONE 2: CORE UTILITIES        - Logging, locks, retry logic
    ZONE 3: RESOURCE MANAGERS     - Docker, GCP, ports, storage
    ZONE 4: INTELLIGENCE LAYER    - ML routing, goal inference, SAI
    ZONE 5: PROCESS ORCHESTRATION - Signals, cleanup, hot reload, Trinity
    ZONE 6: THE KERNEL            - JarvisSystemKernel class
    ZONE 7: ENTRY POINT           - CLI, main()

Usage:
    # Standard startup (auto-detects everything)
    python unified_supervisor.py

    # Production mode (no hot reload)
    python unified_supervisor.py --mode production

    # Skip Docker/GCP (local-only)
    python unified_supervisor.py --skip-docker --skip-gcp

    # Control running kernel
    python unified_supervisor.py --status
    python unified_supervisor.py --shutdown
    python unified_supervisor.py --restart

Design Principles:
    - Zero hardcoding (all values from env vars or dynamic detection)
    - Async-first (parallel initialization where possible)
    - Graceful degradation (components can fail independently)
    - Self-healing (auto-restart crashed components)
    - Observable (metrics, logs, health endpoints)
    - Lazy loading (ML models only loaded when needed)
    - Adaptive (thresholds learn from outcomes)

Author: JARVIS System
Version: 1.0.0
"""
from __future__ import annotations

# ╔═══════════════════════════════════════════════════════════════════════════════╗
# ║                                                                               ║
# ║   ███████╗ ██████╗ ███╗   ██╗███████╗     ██████╗                            ║
# ║   ╚══███╔╝██╔═══██╗████╗  ██║██╔════╝    ██╔═████╗                           ║
# ║     ███╔╝ ██║   ██║██╔██╗ ██║█████╗      ██║██╔██║                           ║
# ║    ███╔╝  ██║   ██║██║╚██╗██║██╔══╝      ████╔╝██║                           ║
# ║   ███████╗╚██████╔╝██║ ╚████║███████╗    ╚██████╔╝                           ║
# ║   ╚══════╝ ╚═════╝ ╚═╝  ╚═══╝╚══════╝     ╚═════╝                            ║
# ║                                                                               ║
# ║   EARLY PROTECTION - Signal handling, venv activation, fast checks           ║
# ║   MUST execute before ANY other imports to survive signal storms             ║
# ║                                                                               ║
# ╚═══════════════════════════════════════════════════════════════════════════════╝

# =============================================================================
# CRITICAL: EARLY SIGNAL PROTECTION FOR CLI COMMANDS
# =============================================================================
# When running --restart, the supervisor sends signals that can kill the client
# process DURING Python startup (before main() runs). This protection MUST
# happen at module level, before ANY other imports, to survive the signal storm.
#
# Exit code 144 = 128 + 16 (killed by signal 16) was happening because signals
# arrived during import phase when Python signal handlers weren't yet installed.
# =============================================================================
import sys as _early_sys
import signal as _early_signal
import os as _early_os

# Suppress multiprocessing resource_tracker semaphore warnings
# This MUST be set BEFORE any multiprocessing imports to affect child processes
_existing_warnings = _early_os.environ.get('PYTHONWARNINGS', '')
_filter = 'ignore::UserWarning:multiprocessing.resource_tracker'
if _filter not in _existing_warnings:
    _early_os.environ['PYTHONWARNINGS'] = f"{_existing_warnings},{_filter}" if _existing_warnings else _filter
del _existing_warnings, _filter

# Check if this is a CLI command that needs signal protection
_cli_flags = ('--restart', '--shutdown', '--status', '--cleanup', '--takeover')
_is_cli_mode = any(flag in _early_sys.argv for flag in _cli_flags)

if _is_cli_mode:
    # FIRST: Ignore ALL signals to protect this process
    for _sig in (
        _early_signal.SIGINT,   # 2 - Ctrl+C
        _early_signal.SIGTERM,  # 15 - Termination
        _early_signal.SIGHUP,   # 1 - Hangup
        _early_signal.SIGURG,   # 16 - Urgent data (exit 144!)
        _early_signal.SIGPIPE,  # 13 - Broken pipe
        _early_signal.SIGALRM,  # 14 - Alarm
        _early_signal.SIGUSR1,  # 30 - User signal 1
        _early_signal.SIGUSR2,  # 31 - User signal 2
    ):
        try:
            _early_signal.signal(_sig, _early_signal.SIG_IGN)
        except (OSError, ValueError):
            pass  # Some signals can't be ignored

    # For --restart and --shutdown, launch detached child and EXIT IMMEDIATELY.
    # The detached child does the actual work in complete isolation.
    _needs_detached = (
        ('--restart' in _early_sys.argv and not _early_os.environ.get('_JARVIS_RESTART_REEXEC')) or
        ('--shutdown' in _early_sys.argv and not _early_os.environ.get('_JARVIS_SHUTDOWN_REEXEC'))
    )
    if _needs_detached:
        import subprocess as _sp
        import tempfile as _tmp

        _is_shutdown = '--shutdown' in _early_sys.argv
        _cmd_name = 'shutdown' if _is_shutdown else 'restart'
        _reexec_marker = '_JARVIS_SHUTDOWN_REEXEC' if _is_shutdown else '_JARVIS_RESTART_REEXEC'
        _result_path = f"/tmp/jarvis_{_cmd_name}_{_early_os.getpid()}.result"

        # Write standalone command script with full signal immunity
        _script_content = f'''#!/usr/bin/env python3
import os, sys, signal, subprocess, time

# Full signal immunity
for s in range(1, 32):
    try:
        if s not in (9, 17):
            signal.signal(s, signal.SIG_IGN)
    except: pass

# New session
try: os.setsid()
except: pass

# Run the actual command
env = dict(os.environ)
env[{_reexec_marker!r}] = "1"
result = subprocess.run(
    [{_early_sys.executable!r}] + {_early_sys.argv!r},
    cwd={_early_os.getcwd()!r},
    capture_output=True,
    env=env,
)

# Write result
with open({_result_path!r}, "w") as f:
    f.write(str(result.returncode) + "\\n")
    f.write(result.stdout.decode())
    f.write(result.stderr.decode())
'''
        _fd, _script_path = _tmp.mkstemp(suffix='.py', prefix=f'jarvis_{_cmd_name}_')
        _early_os.write(_fd, _script_content.encode())
        _early_os.close(_fd)
        _early_os.chmod(_script_path, 0o755)

        # Launch completely detached (double-fork daemon pattern)
        _proc = _sp.Popen(
            [_early_sys.executable, _script_path],
            start_new_session=True,
            stdin=_sp.DEVNULL,
            stdout=_sp.DEVNULL,
            stderr=_sp.DEVNULL,
        )

        # Print message and exit IMMEDIATELY
        _early_sys.stdout.write(f"\n{'='*60}\n")
        _early_sys.stdout.write(f"  JARVIS Kernel {_cmd_name.title()} Initiated\n")
        _early_sys.stdout.write(f"{'='*60}\n")
        _early_sys.stdout.write(f"  Running in background.\n")
        _early_sys.stdout.write(f"  Status: python3 unified_supervisor.py --status\n")
        _early_sys.stdout.write(f"  Results: {_result_path}\n")
        _early_sys.stdout.write(f"{'='*60}\n")
        _early_sys.stdout.flush()
        _early_os._exit(0)

    # Try to create own process group for additional isolation
    try:
        _early_os.setpgrp()
    except (OSError, PermissionError):
        pass

    _early_os.environ['_JARVIS_CLI_PROTECTED'] = '1'

# Clean up early imports
del _early_sys, _early_signal, _early_os, _cli_flags, _is_cli_mode


# =============================================================================
# CRITICAL: VENV AUTO-ACTIVATION (MUST BE BEFORE ANY IMPORTS)
# =============================================================================
# Ensures we use the venv Python with correct packages. If running with system
# Python and venv exists, re-exec with venv Python. This MUST happen before
# ANY imports to prevent loading wrong packages.
# =============================================================================
import os as _os
import sys as _sys
from pathlib import Path as _Path


def _ensure_venv_python() -> None:
    """
    Ensure we're running with the venv Python.
    Re-executes script with venv Python if necessary.

    Uses site-packages check (not executable path) since venv Python
    often symlinks to system Python.
    """
    # Skip if explicitly disabled
    if _os.environ.get('JARVIS_SKIP_VENV_CHECK') == '1':
        return

    # Skip if already re-executed (prevent infinite loop)
    if _os.environ.get('_JARVIS_VENV_REEXEC') == '1':
        return

    script_dir = _Path(__file__).parent.resolve()

    # Find venv Python (try multiple locations)
    venv_candidates = [
        script_dir / "venv" / "bin" / "python3",
        script_dir / "venv" / "bin" / "python",
        script_dir / ".venv" / "bin" / "python3",
        script_dir / ".venv" / "bin" / "python",
    ]

    venv_python = None
    for candidate in venv_candidates:
        if candidate.exists():
            venv_python = candidate
            break

    if not venv_python:
        return  # No venv found, continue with current Python

    # Check if venv site-packages is in sys.path
    venv_site_packages = str(script_dir / "venv" / "lib")
    venv_in_path = any(venv_site_packages in p for p in _sys.path)

    if venv_in_path:
        return  # Already running with venv Python

    # Check if running from venv bin directory
    current_exe = _Path(_sys.executable)
    if str(script_dir / "venv" / "bin") in str(current_exe):
        return

    # NOT running with venv - need to re-exec
    print(f"[KERNEL] Detected system Python without venv packages")
    print(f"[KERNEL] Current: {_sys.executable}")
    print(f"[KERNEL] Switching to: {venv_python}")

    _os.environ['_JARVIS_VENV_REEXEC'] = '1'

    # Set PYTHONPATH to include project directories
    pythonpath = _os.pathsep.join([
        str(script_dir),
        str(script_dir / "backend"),
        _os.environ.get('PYTHONPATH', '')
    ])
    _os.environ['PYTHONPATH'] = pythonpath

    # Re-execute with venv Python
    _os.execv(str(venv_python), [str(venv_python)] + _sys.argv)


# Execute venv check immediately
_ensure_venv_python()

# Clean up temporary imports
del _os, _sys, _Path, _ensure_venv_python


# =============================================================================
# FAST EARLY-EXIT FOR RUNNING KERNEL
# =============================================================================
# Check runs BEFORE heavy imports (PyTorch, transformers, GCP libs).
# If kernel is already running and healthy, we can exit immediately
# without loading 2GB+ of ML libraries.
# =============================================================================
def _fast_kernel_check() -> bool:
    """
    Ultra-fast check for running kernel before heavy imports.

    Uses only standard library - no external dependencies.
    Returns True if we handled the request and should exit.
    """
    import os as _os
    import sys as _sys
    import socket as _socket
    import json as _json
    from pathlib import Path as _Path

    # Only run fast path if no action flags passed
    action_flags = [
        '--restart', '--shutdown', '--takeover', '--force',
        '--status', '--cleanup', '--task', '--mode', '--help', '-h',
        '--skip-docker', '--skip-gcp', '--goal-preset', '--debug',
    ]
    if any(flag in _sys.argv for flag in action_flags):
        return False  # Need full initialization

    # Check if IPC socket exists
    sock_path = _Path.home() / ".jarvis" / "locks" / "kernel.sock"
    if not sock_path.exists():
        # Try legacy path
        sock_path = _Path.home() / ".jarvis" / "locks" / "supervisor.sock"
        if not sock_path.exists():
            return False  # No kernel running

    # Try to connect to kernel
    data = b''
    max_retries = 2
    sock_timeout = 8.0

    for attempt in range(max_retries):
        try:
            sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            sock.settimeout(sock_timeout)
            sock.connect(str(sock_path))

            # Send health command
            msg = _json.dumps({'command': 'health'}) + '\n'
            sock.sendall(msg.encode())

            # Receive response
            while True:
                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                    if b'\n' in data:
                        break
                except _socket.timeout:
                    break

            sock.close()

            if data:
                break

        except (_socket.timeout, ConnectionRefusedError, FileNotFoundError):
            if attempt < max_retries - 1:
                import time as _time
                _time.sleep(0.5)
                continue
            return False
        except Exception:
            return False

    if not data:
        return False

    # Parse response
    try:
        result = _json.loads(data.decode().strip())
    except (_json.JSONDecodeError, UnicodeDecodeError):
        return False

    if not result.get('success'):
        return False

    health_data = result.get('result', {})
    health_level = health_data.get('health_level', 'UNKNOWN')

    # Only fast-exit if kernel is healthy
    if health_level not in ('FULLY_READY', 'HTTP_HEALTHY', 'IPC_RESPONSIVE'):
        return False

    # Check for auto-restart behavior
    skip_restart = _os.environ.get('JARVIS_KERNEL_SKIP_RESTART', '').lower() in ('1', 'true', 'yes')

    if not skip_restart:
        return False  # Let main() handle shutdown → start

    # Show status and exit
    pid = health_data.get('pid', 'unknown')
    uptime = health_data.get('uptime_seconds', 0)
    uptime_str = f"{int(uptime // 60)}m {int(uptime % 60)}s" if uptime > 60 else f"{int(uptime)}s"

    print(f"\n{'='*70}")
    print(f"  JARVIS Kernel (PID {pid}) is running and healthy")
    print(f"{'='*70}")
    print(f"   Health:  {health_level}")
    print(f"   Uptime:  {uptime_str}")
    print(f"")
    print(f"   No action needed - kernel is ready.")
    print(f"   Commands:  --restart | --shutdown | --status")
    print(f"{'='*70}\n")

    return True


# Run fast check before heavy imports
if _fast_kernel_check():
    import sys as _sys
    _sys.exit(0)

del _fast_kernel_check


# =============================================================================
# PYTHON 3.9 COMPATIBILITY PATCH
# =============================================================================
# Patches importlib.metadata.packages_distributions() for Python 3.9
# =============================================================================
import sys as _sys
if _sys.version_info < (3, 10):
    try:
        from importlib import metadata as _metadata
        if not hasattr(_metadata, 'packages_distributions'):
            def _packages_distributions_fallback():
                try:
                    import importlib_metadata as _backport
                    if hasattr(_backport, 'packages_distributions'):
                        return _backport.packages_distributions()
                except ImportError:
                    pass
                return {}
            _metadata.packages_distributions = _packages_distributions_fallback
    except Exception:
        pass
del _sys


# =============================================================================
# PYTORCH/TRANSFORMERS COMPATIBILITY SHIM
# =============================================================================
# Fix for transformers 4.57+ expecting register_pytree_node but PyTorch 2.1.x
# only exposes _register_pytree_node (private).
# =============================================================================
def _apply_pytorch_compat() -> bool:
    """Apply PyTorch compatibility shim before any transformers imports."""
    import os as _os

    try:
        import torch.utils._pytree as _pytree
    except ImportError:
        return False

    if hasattr(_pytree, 'register_pytree_node'):
        return False  # No shim needed

    if hasattr(_pytree, '_register_pytree_node'):
        _original_register = _pytree._register_pytree_node

        def _compat_register_pytree_node(
            typ,
            flatten_fn,
            unflatten_fn,
            *,
            serialized_type_name=None,
            to_dumpable_context=None,
            from_dumpable_context=None,
            **extra_kwargs
        ):
            kwargs = {}
            if to_dumpable_context is not None:
                kwargs['to_dumpable_context'] = to_dumpable_context
            if from_dumpable_context is not None:
                kwargs['from_dumpable_context'] = from_dumpable_context

            try:
                return _original_register(typ, flatten_fn, unflatten_fn, **kwargs)
            except TypeError as e:
                if 'unexpected keyword argument' in str(e):
                    return _original_register(typ, flatten_fn, unflatten_fn)
                raise

        _pytree.register_pytree_node = _compat_register_pytree_node

        if _os.environ.get("JARVIS_DEBUG"):
            import sys
            print("[KERNEL] Applied pytree compatibility wrapper", file=sys.stderr)
        return True

    # No-op fallback
    def _noop_register(cls, flatten_fn, unflatten_fn, **kwargs):
        pass
    _pytree.register_pytree_node = _noop_register
    return True


_apply_pytorch_compat()
del _apply_pytorch_compat


# =============================================================================
# TRANSFORMERS SECURITY CHECK BYPASS (CVE-2025-32434)
# =============================================================================
# For PyTorch < 2.6, bypass security check for trusted HuggingFace models.
# =============================================================================
def _apply_transformers_security_bypass() -> bool:
    """Bypass torch.load security check for trusted HuggingFace models."""
    import os as _os

    if _os.environ.get("JARVIS_STRICT_TORCH_SECURITY") == "1":
        return False

    try:
        import torch
        torch_version = tuple(int(x) for x in torch.__version__.split('.')[:2])
        if torch_version >= (2, 6):
            return False

        import transformers.utils.import_utils as _import_utils
        if not hasattr(_import_utils, 'check_torch_load_is_safe'):
            return False

        def _bypassed_check():
            pass

        _import_utils.check_torch_load_is_safe = _bypassed_check

        try:
            import transformers.modeling_utils as _modeling_utils
            if hasattr(_modeling_utils, 'check_torch_load_is_safe'):
                _modeling_utils.check_torch_load_is_safe = _bypassed_check
        except ImportError:
            pass

        return True

    except ImportError:
        return False
    except Exception:
        return False


_apply_transformers_security_bypass()
del _apply_transformers_security_bypass


# ╔═══════════════════════════════════════════════════════════════════════════════╗
# ║                                                                               ║
# ║   ███████╗ ██████╗ ███╗   ██╗███████╗     ██╗                                ║
# ║   ╚══███╔╝██╔═══██╗████╗  ██║██╔════╝    ███║                                ║
# ║     ███╔╝ ██║   ██║██╔██╗ ██║█████╗      ╚██║                                ║
# ║    ███╔╝  ██║   ██║██║╚██╗██║██╔══╝       ██║                                ║
# ║   ███████╗╚██████╔╝██║ ╚████║███████╗     ██║                                ║
# ║   ╚══════╝ ╚═════╝ ╚═╝  ╚═══╝╚══════╝     ╚═╝                                ║
# ║                                                                               ║
# ║   FOUNDATION - Imports, configuration, constants, type definitions           ║
# ║                                                                               ║
# ╚═══════════════════════════════════════════════════════════════════════════════╝

# =============================================================================
# STANDARD LIBRARY IMPORTS
# =============================================================================
import argparse
import asyncio
import contextlib
import functools
import hashlib
import inspect
import json
import logging
import os
import platform
import re
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import warnings
from abc import ABC, abstractmethod
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any, Awaitable, Callable, Coroutine, Dict, Generator, Generic,
    List, Literal, Optional, Set, Tuple, Type, TypeVar, Union,
)

# Type variables
T = TypeVar('T')
ConfigT = TypeVar('ConfigT', bound='SystemKernelConfig')

# =============================================================================
# THIRD-PARTY IMPORTS (with graceful fallbacks)
# =============================================================================

# aiohttp - async HTTP client
try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    aiohttp = None

# aiofiles - async file I/O
try:
    import aiofiles
    AIOFILES_AVAILABLE = True
except ImportError:
    AIOFILES_AVAILABLE = False
    aiofiles = None

# psutil - process utilities
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    psutil = None

# uvicorn - ASGI server
try:
    import uvicorn
    UVICORN_AVAILABLE = True
except ImportError:
    UVICORN_AVAILABLE = False
    uvicorn = None

# dotenv - environment loading
try:
    from dotenv import load_dotenv
    DOTENV_AVAILABLE = True
except ImportError:
    DOTENV_AVAILABLE = False
    load_dotenv = None

# numpy - numerical operations
try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False
    np = None

# =============================================================================
# CONSTANTS
# =============================================================================

# Kernel version
KERNEL_VERSION = "1.0.0"
KERNEL_NAME = "JARVIS Unified System Kernel"

# Default paths (dynamically resolved at runtime)
PROJECT_ROOT = Path(__file__).parent.resolve()
BACKEND_DIR = PROJECT_ROOT / "backend"
JARVIS_HOME = Path.home() / ".jarvis"
LOCKS_DIR = JARVIS_HOME / "locks"
CACHE_DIR = JARVIS_HOME / "cache"
LOGS_DIR = JARVIS_HOME / "logs"

# IPC socket paths
KERNEL_SOCKET_PATH = LOCKS_DIR / "kernel.sock"
LEGACY_SOCKET_PATH = LOCKS_DIR / "supervisor.sock"

# Port ranges (for dynamic allocation)
BACKEND_PORT_RANGE = (8000, 8100)
WEBSOCKET_PORT_RANGE = (8765, 8800)
LOADING_SERVER_PORT_RANGE = (8080, 8090)

# Timeouts (seconds)
DEFAULT_STARTUP_TIMEOUT = 120.0
DEFAULT_SHUTDOWN_TIMEOUT = 30.0
DEFAULT_HEALTH_CHECK_INTERVAL = 10.0
DEFAULT_HOT_RELOAD_INTERVAL = 10.0
DEFAULT_HOT_RELOAD_GRACE_PERIOD = 120.0
DEFAULT_IDLE_TIMEOUT = 300

# Memory defaults
DEFAULT_MEMORY_TARGET_PERCENT = 30.0
DEFAULT_MAX_MEMORY_GB = 4.8

# Cost defaults
DEFAULT_DAILY_BUDGET_USD = 5.0

# =============================================================================
# SUPPRESS NOISY WARNINGS
# =============================================================================
warnings.filterwarnings("ignore", message=".*speechbrain.*deprecated.*", category=UserWarning)
warnings.filterwarnings("ignore", message=".*torchaudio.*deprecated.*", category=UserWarning)
warnings.filterwarnings("ignore", message=".*Wav2Vec2Model is frozen.*", category=UserWarning)
warnings.filterwarnings("ignore", message=".*model is frozen.*", category=UserWarning)

# Configure noisy loggers
for _logger_name in [
    "speechbrain", "speechbrain.utils.checkpoints", "transformers",
    "transformers.modeling_utils", "urllib3", "asyncio",
]:
    logging.getLogger(_logger_name).setLevel(logging.ERROR)

# =============================================================================
# ENVIRONMENT LOADING
# =============================================================================
def _load_environment_files() -> List[str]:
    """
    Load environment variables from .env files.

    Priority (later files override earlier):
    1. Root .env (base configuration)
    2. backend/.env (backend-specific)
    3. .env.gcp (GCP hybrid cloud)

    Returns list of loaded file names.
    """
    if not DOTENV_AVAILABLE:
        return []

    loaded = []
    env_files = [
        PROJECT_ROOT / ".env",
        PROJECT_ROOT / "backend" / ".env",
        PROJECT_ROOT / ".env.gcp",
    ]

    for env_file in env_files:
        if env_file.exists():
            load_dotenv(env_file, override=True)
            loaded.append(env_file.name)

    return loaded


# Load environment files immediately
_loaded_env_files = _load_environment_files()


# =============================================================================
# DYNAMIC DETECTION HELPERS
# =============================================================================
def _detect_best_port(start: int, end: int) -> int:
    """
    Find the first available port in range.

    Uses socket binding test to verify availability.
    """
    for port in range(start, end + 1):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    return start  # Fallback to start of range


def _discover_venv() -> Optional[Path]:
    """Discover virtual environment path."""
    candidates = [
        PROJECT_ROOT / "venv",
        PROJECT_ROOT / ".venv",
        PROJECT_ROOT / "backend" / "venv",
    ]
    for candidate in candidates:
        if candidate.exists() and (candidate / "bin" / "python").exists():
            return candidate
    return None


def _discover_repo(names: List[str]) -> Optional[Path]:
    """Discover sibling repository by name."""
    parent = PROJECT_ROOT.parent
    for name in names:
        path = parent / name
        if path.exists() and (path / "pyproject.toml").exists():
            return path
    return None


def _discover_prime_repo() -> Optional[Path]:
    """Discover JARVIS-Prime repository."""
    return _discover_repo(["JARVIS-Prime", "jarvis-prime"])


def _discover_reactor_repo() -> Optional[Path]:
    """Discover Reactor-Core repository."""
    return _discover_repo(["Reactor-Core", "reactor-core"])


def _detect_gcp_credentials() -> bool:
    """Check if GCP credentials are available."""
    # Check for service account file
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        creds_path = Path(os.environ["GOOGLE_APPLICATION_CREDENTIALS"])
        if creds_path.exists():
            return True

    # Check for default credentials
    default_creds = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    if default_creds.exists():
        return True

    return False


def _detect_gcp_project() -> Optional[str]:
    """Detect GCP project ID."""
    # Check environment variable
    if project := os.environ.get("GOOGLE_CLOUD_PROJECT"):
        return project
    if project := os.environ.get("GCP_PROJECT"):
        return project
    if project := os.environ.get("GCLOUD_PROJECT"):
        return project

    # Try gcloud config
    try:
        result = subprocess.run(
            ["gcloud", "config", "get-value", "project"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return None


def _calculate_memory_budget() -> float:
    """Calculate memory budget based on system RAM."""
    if not PSUTIL_AVAILABLE:
        return DEFAULT_MAX_MEMORY_GB

    total_gb = psutil.virtual_memory().total / (1024 ** 3)
    target_percent = float(os.environ.get("JARVIS_MEMORY_TARGET", DEFAULT_MEMORY_TARGET_PERCENT))

    return round(total_gb * (target_percent / 100), 1)


def _get_env_bool(key: str, default: bool = False) -> bool:
    """Get boolean from environment variable."""
    value = os.environ.get(key, "").lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return default


def _get_env_int(key: str, default: int) -> int:
    """Get integer from environment variable."""
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


def _get_env_float(key: str, default: float) -> float:
    """Get float from environment variable."""
    try:
        return float(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


# =============================================================================
# SYSTEM KERNEL CONFIGURATION
# =============================================================================
@dataclass
class SystemKernelConfig:
    """
    Unified configuration for the JARVIS System Kernel.

    Merges:
    - BootstrapConfig (run_supervisor.py) - supervisor features
    - StartupSystemConfig (start_system.py) - resource management

    All values are dynamically detected or loaded from environment.
    Zero hardcoding.
    """

    # ═══════════════════════════════════════════════════════════════════════════
    # CORE IDENTITY
    # ═══════════════════════════════════════════════════════════════════════════
    kernel_version: str = KERNEL_VERSION
    kernel_id: str = field(default_factory=lambda: f"kernel-{uuid.uuid4().hex[:8]}")
    start_time: datetime = field(default_factory=datetime.now)

    # ═══════════════════════════════════════════════════════════════════════════
    # OPERATING MODE
    # ═══════════════════════════════════════════════════════════════════════════
    mode: str = field(default_factory=lambda: os.environ.get("JARVIS_MODE", "supervisor"))
    in_process_backend: bool = field(default_factory=lambda: _get_env_bool("JARVIS_IN_PROCESS", True))
    dev_mode: bool = field(default_factory=lambda: _get_env_bool("JARVIS_DEV_MODE", True))
    zero_touch_enabled: bool = field(default_factory=lambda: _get_env_bool("JARVIS_ZERO_TOUCH", False))
    debug: bool = field(default_factory=lambda: _get_env_bool("JARVIS_DEBUG", False))
    verbose: bool = field(default_factory=lambda: _get_env_bool("JARVIS_VERBOSE", False))

    # ═══════════════════════════════════════════════════════════════════════════
    # NETWORK
    # ═══════════════════════════════════════════════════════════════════════════
    backend_host: str = field(default_factory=lambda: os.environ.get("JARVIS_HOST", "0.0.0.0"))
    backend_port: int = field(default_factory=lambda: _get_env_int("JARVIS_BACKEND_PORT", 0))
    websocket_port: int = field(default_factory=lambda: _get_env_int("JARVIS_WEBSOCKET_PORT", 0))
    loading_server_port: int = field(default_factory=lambda: _get_env_int("JARVIS_LOADING_PORT", 0))

    # ═══════════════════════════════════════════════════════════════════════════
    # PATHS
    # ═══════════════════════════════════════════════════════════════════════════
    project_root: Path = field(default_factory=lambda: PROJECT_ROOT)
    backend_dir: Path = field(default_factory=lambda: BACKEND_DIR)
    venv_path: Optional[Path] = field(default_factory=_discover_venv)
    jarvis_home: Path = field(default_factory=lambda: JARVIS_HOME)

    # ═══════════════════════════════════════════════════════════════════════════
    # TRINITY / CROSS-REPO
    # ═══════════════════════════════════════════════════════════════════════════
    trinity_enabled: bool = field(default_factory=lambda: _get_env_bool("JARVIS_TRINITY_ENABLED", True))
    prime_repo_path: Optional[Path] = field(default_factory=_discover_prime_repo)
    reactor_repo_path: Optional[Path] = field(default_factory=_discover_reactor_repo)
    prime_cloud_run_url: Optional[str] = field(default_factory=lambda: os.environ.get("JARVIS_PRIME_CLOUD_RUN_URL"))
    prime_enabled: bool = field(default_factory=lambda: _get_env_bool("JARVIS_PRIME_ENABLED", True))
    reactor_enabled: bool = field(default_factory=lambda: _get_env_bool("REACTOR_CORE_ENABLED", True))

    # ═══════════════════════════════════════════════════════════════════════════
    # DOCKER
    # ═══════════════════════════════════════════════════════════════════════════
    docker_enabled: bool = field(default_factory=lambda: _get_env_bool("JARVIS_DOCKER_ENABLED", True))
    docker_auto_start: bool = field(default_factory=lambda: _get_env_bool("JARVIS_DOCKER_AUTO_START", True))
    docker_health_check_interval: float = field(default_factory=lambda: _get_env_float("JARVIS_DOCKER_HEALTH_INTERVAL", 30.0))

    # ═══════════════════════════════════════════════════════════════════════════
    # GCP / CLOUD
    # ═══════════════════════════════════════════════════════════════════════════
    gcp_enabled: bool = field(default_factory=lambda: _get_env_bool("JARVIS_GCP_ENABLED", True) and _detect_gcp_credentials())
    gcp_project_id: Optional[str] = field(default_factory=_detect_gcp_project)
    gcp_zone: str = field(default_factory=lambda: os.environ.get("JARVIS_GCP_ZONE", "us-central1-a"))
    spot_vm_enabled: bool = field(default_factory=lambda: _get_env_bool("JARVIS_SPOT_VM_ENABLED", False))
    prefer_cloud_run: bool = field(default_factory=lambda: _get_env_bool("JARVIS_PREFER_CLOUD_RUN", False))
    cloud_sql_enabled: bool = field(default_factory=lambda: _get_env_bool("JARVIS_CLOUD_SQL_ENABLED", True))

    # ═══════════════════════════════════════════════════════════════════════════
    # COST OPTIMIZATION
    # ═══════════════════════════════════════════════════════════════════════════
    scale_to_zero_enabled: bool = field(default_factory=lambda: _get_env_bool("JARVIS_SCALE_TO_ZERO", True))
    idle_timeout_seconds: int = field(default_factory=lambda: _get_env_int("JARVIS_IDLE_TIMEOUT", DEFAULT_IDLE_TIMEOUT))
    cost_budget_daily_usd: float = field(default_factory=lambda: _get_env_float("JARVIS_DAILY_BUDGET", DEFAULT_DAILY_BUDGET_USD))

    # ═══════════════════════════════════════════════════════════════════════════
    # INTELLIGENCE / ML
    # ═══════════════════════════════════════════════════════════════════════════
    hybrid_intelligence_enabled: bool = field(default_factory=lambda: _get_env_bool("JARVIS_INTELLIGENCE_ENABLED", True))
    goal_inference_enabled: bool = field(default_factory=lambda: _get_env_bool("JARVIS_GOAL_INFERENCE", True))
    goal_preset: str = field(default_factory=lambda: os.environ.get("JARVIS_GOAL_PRESET", "auto"))
    voice_cache_enabled: bool = field(default_factory=lambda: _get_env_bool("JARVIS_VOICE_CACHE", True))

    # ═══════════════════════════════════════════════════════════════════════════
    # VOICE / AUDIO
    # ═══════════════════════════════════════════════════════════════════════════
    voice_enabled: bool = field(default_factory=lambda: _get_env_bool("JARVIS_VOICE_ENABLED", True))
    narrator_enabled: bool = field(default_factory=lambda: _get_env_bool("STARTUP_NARRATOR_VOICE", True))
    wake_word_enabled: bool = field(default_factory=lambda: _get_env_bool("JARVIS_WAKE_WORD", True))
    ecapa_enabled: bool = field(default_factory=lambda: _get_env_bool("JARVIS_ECAPA_ENABLED", True))

    # ═══════════════════════════════════════════════════════════════════════════
    # MEMORY / RESOURCES
    # ═══════════════════════════════════════════════════════════════════════════
    memory_mode: str = field(default_factory=lambda: os.environ.get("JARVIS_MEMORY_MODE", "auto"))
    memory_target_percent: float = field(default_factory=lambda: _get_env_float("JARVIS_MEMORY_TARGET", DEFAULT_MEMORY_TARGET_PERCENT))
    max_memory_gb: float = field(default_factory=_calculate_memory_budget)

    # ═══════════════════════════════════════════════════════════════════════════
    # READINESS / HEALTH
    # ═══════════════════════════════════════════════════════════════════════════
    health_check_interval: float = field(default_factory=lambda: _get_env_float("JARVIS_HEALTH_INTERVAL", DEFAULT_HEALTH_CHECK_INTERVAL))
    startup_timeout: float = field(default_factory=lambda: _get_env_float("JARVIS_STARTUP_TIMEOUT", DEFAULT_STARTUP_TIMEOUT))

    # ═══════════════════════════════════════════════════════════════════════════
    # HOT RELOAD / DEV
    # ═══════════════════════════════════════════════════════════════════════════
    hot_reload_enabled: bool = field(default_factory=lambda: _get_env_bool("JARVIS_HOT_RELOAD", True))
    reload_check_interval: float = field(default_factory=lambda: _get_env_float("JARVIS_RELOAD_CHECK_INTERVAL", DEFAULT_HOT_RELOAD_INTERVAL))
    reload_grace_period: float = field(default_factory=lambda: _get_env_float("JARVIS_RELOAD_GRACE_PERIOD", DEFAULT_HOT_RELOAD_GRACE_PERIOD))
    watch_patterns: List[str] = field(default_factory=lambda: ["*.py", "*.yaml", "*.yml"])

    def __post_init__(self):
        """Post-initialization: resolve dynamic ports if not set."""
        if self.backend_port == 0:
            self.backend_port = _detect_best_port(*BACKEND_PORT_RANGE)
        if self.websocket_port == 0:
            self.websocket_port = _detect_best_port(*WEBSOCKET_PORT_RANGE)
        if self.loading_server_port == 0:
            self.loading_server_port = _detect_best_port(*LOADING_SERVER_PORT_RANGE)

        # Ensure directories exist
        self.jarvis_home.mkdir(parents=True, exist_ok=True)
        LOCKS_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

        # Apply mode-specific defaults
        if self.mode == "production":
            self.dev_mode = False
            self.hot_reload_enabled = False
        elif self.mode == "minimal":
            self.docker_enabled = False
            self.gcp_enabled = False
            self.trinity_enabled = False
            self.hybrid_intelligence_enabled = False

    @classmethod
    def from_environment(cls) -> "SystemKernelConfig":
        """Factory: Create config from environment variables."""
        return cls()

    def validate(self) -> List[str]:
        """
        Validate configuration.

        Returns list of warnings (empty if valid).
        """
        warnings_list = []

        if self.in_process_backend and not UVICORN_AVAILABLE:
            warnings_list.append("in_process_backend=True but uvicorn not installed")

        if self.gcp_enabled and not self.gcp_project_id:
            warnings_list.append("GCP enabled but no project ID found")

        if self.trinity_enabled and not self.prime_repo_path and not self.prime_cloud_run_url:
            warnings_list.append("Trinity enabled but JARVIS-Prime not found (local or cloud)")

        if self.hot_reload_enabled and not self.dev_mode:
            warnings_list.append("hot_reload_enabled but dev_mode=False (hot reload will be disabled)")

        return warnings_list

    def to_dict(self) -> Dict[str, Any]:
        """Serialize config for logging/debugging."""
        result = {}
        for field_name in self.__dataclass_fields__:
            value = getattr(self, field_name)
            if isinstance(value, Path):
                value = str(value)
            elif isinstance(value, datetime):
                value = value.isoformat()
            result[field_name] = value
        return result

    def summary(self) -> str:
        """Get human-readable config summary."""
        lines = [
            f"Mode: {self.mode}",
            f"Backend: {'in-process' if self.in_process_backend else 'subprocess'} on port {self.backend_port}",
            f"Dev Mode: {self.dev_mode} (Hot Reload: {self.hot_reload_enabled})",
            f"Docker: {self.docker_enabled}",
            f"GCP: {self.gcp_enabled} (Project: {self.gcp_project_id or 'N/A'})",
            f"Trinity: {self.trinity_enabled}",
            f"Intelligence: {self.hybrid_intelligence_enabled}",
            f"Memory: {self.max_memory_gb}GB target ({self.memory_mode} mode)",
        ]
        return "\n".join(lines)


# =============================================================================
# ADD BACKEND TO PATH
# =============================================================================
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ╔═══════════════════════════════════════════════════════════════════════════════╗
# ║                                                                               ║
# ║   END OF ZONE 0 & ZONE 1                                                      ║
# ║   Zones 2-7 will be added in subsequent commits                               ║
# ║                                                                               ║
# ╚═══════════════════════════════════════════════════════════════════════════════╝

# Placeholder for remaining zones - will be implemented incrementally
# ZONE 2: Core Utilities (UnifiedLogger, locks, retry, terminal UI)
# ZONE 3: Resource Managers (Docker, GCP, ports, storage)
# ZONE 4: Intelligence Layer (routing, goal inference, SAI)
# ZONE 5: Process Orchestration (signals, cleanup, hot reload, Trinity)
# ZONE 6: The Kernel (JarvisSystemKernel class)
# ZONE 7: Entry Point (CLI, main)

if __name__ == "__main__":
    print(f"\n{KERNEL_NAME} v{KERNEL_VERSION}")
    print("=" * 60)
    print("Zone 0 (Early Protection) and Zone 1 (Foundation) implemented.")
    print("Zones 2-7 coming soon...")
    print("=" * 60)

    # Show config summary
    config = SystemKernelConfig.from_environment()
    print("\nConfiguration:")
    print(config.summary())

    # Show warnings
    warnings_list = config.validate()
    if warnings_list:
        print("\nWarnings:")
        for w in warnings_list:
            print(f"  - {w}")
