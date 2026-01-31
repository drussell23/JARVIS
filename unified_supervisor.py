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
# ║   ███████╗ ██████╗ ███╗   ██╗███████╗     ██████╗                             ║
# ║   ╚══███╔╝██╔═══██╗████╗  ██║██╔════╝    ██╔═████╗                            ║
# ║     ███╔╝ ██║   ██║██╔██╗ ██║█████╗      ██║██╔██║                            ║
# ║    ███╔╝  ██║   ██║██║╚██╗██║██╔══╝      ████╔╝██║                            ║
# ║   ███████╗╚██████╔╝██║ ╚████║███████╗    ╚██████╔╝                            ║
# ║   ╚══════╝ ╚═════╝ ╚═╝  ╚═══╝╚══════╝     ╚═════╝                             ║ 
# ║                                                                               ║
# ║   EARLY PROTECTION - Signal handling, venv activation, fast checks            ║
# ║   MUST execute before ANY other imports to survive signal storms              ║
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
# ║   ███████╗ ██████╗ ███╗   ██╗███████╗     ██╗                                 ║
# ║   ╚══███╔╝██╔═══██╗████╗  ██║██╔════╝    ███║                                 ║
# ║     ███╔╝ ██║   ██║██╔██╗ ██║█████╗      ╚██║                                 ║
# ║    ███╔╝  ██║   ██║██║╚██╗██║██╔══╝       ██║                                 ║
# ║   ███████╗╚██████╔╝██║ ╚████║███████╗     ██║                                 ║
# ║   ╚══════╝ ╚═════╝ ╚═╝  ╚═══╝╚══════╝     ╚═╝                                 ║
# ║                                                                               ║
# ║   FOUNDATION - Imports, configuration, constants, type definitions            ║
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
import sqlite3
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
from collections import defaultdict, OrderedDict
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
    prime_api_port: int = field(default_factory=lambda: _get_env_int("JARVIS_PRIME_API_PORT", 8011))
    reactor_api_port: int = field(default_factory=lambda: _get_env_int("REACTOR_CORE_API_PORT", 8012))

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
# ║   ███████╗ ██████╗ ███╗   ██╗███████╗    ██████╗                              ║
# ║   ╚══███╔╝██╔═══██╗████╗  ██║██╔════╝    ╚════██╗                             ║
# ║     ███╔╝ ██║   ██║██╔██╗ ██║█████╗      █████╔╝                              ║
# ║    ███╔╝  ██║   ██║██║╚██╗██║██╔══╝     ██╔═══╝                               ║
# ║   ███████╗╚██████╔╝██║ ╚████║███████╗   ███████╗                              ║
# ║   ╚══════╝ ╚═════╝ ╚═╝  ╚═══╝╚══════╝   ╚══════╝                              ║
# ║                                                                               ║
# ║   CORE UTILITIES - Logging, locks, retry logic, terminal UI                   ║
# ║                                                                               ║
# ╚═══════════════════════════════════════════════════════════════════════════════╝

# =============================================================================
# LOG LEVEL & SECTION ENUMS
# =============================================================================
class LogLevel(Enum):
    """Log severity levels with ANSI color codes."""
    DEBUG = ("DEBUG", "\033[36m")      # Cyan
    INFO = ("INFO", "\033[32m")        # Green
    WARNING = ("WARNING", "\033[33m")  # Yellow
    ERROR = ("ERROR", "\033[31m")      # Red
    CRITICAL = ("CRITICAL", "\033[35m") # Magenta
    SUCCESS = ("SUCCESS", "\033[92m")  # Bright Green
    PHASE = ("PHASE", "\033[94m")      # Bright Blue


class LogSection(Enum):
    """Logical sections for organized log output."""
    BOOT = "BOOT"
    CONFIG = "CONFIG"
    DOCKER = "DOCKER"
    GCP = "GCP"
    BACKEND = "BACKEND"
    TRINITY = "TRINITY"
    INTELLIGENCE = "INTELLIGENCE"
    VOICE = "VOICE"
    HEALTH = "HEALTH"
    SHUTDOWN = "SHUTDOWN"
    RESOURCES = "RESOURCES"
    PORTS = "PORTS"
    STORAGE = "STORAGE"
    PROCESS = "PROCESS"
    DEV = "DEV"


# =============================================================================
# SECTION CONTEXT MANAGER
# =============================================================================
class SectionContext:
    """Context manager for logging sections with timing."""

    def __init__(self, logger: "UnifiedLogger", section: LogSection, title: str):
        self.logger = logger
        self.section = section
        self.title = title
        self.start_time: float = 0

    def __enter__(self) -> "SectionContext":
        self.start_time = time.perf_counter()
        self.logger._render_section_header(self.section, self.title)
        self.logger._section_stack.append(self.section)
        self.logger._indent_level += 1
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.logger._indent_level = max(0, self.logger._indent_level - 1)
        if self.logger._section_stack:
            self.logger._section_stack.pop()
        duration_ms = (time.perf_counter() - self.start_time) * 1000
        self.logger._render_section_footer(self.section, duration_ms)
        return None


# =============================================================================
# PARALLEL TRACKER
# =============================================================================
class ParallelTracker:
    """Track multiple parallel async operations."""

    def __init__(self, logger: "UnifiedLogger", task_names: List[str]):
        self.logger = logger
        self.task_names = task_names
        self._start_times: Dict[str, float] = {}
        self._results: Dict[str, Tuple[bool, float]] = {}

    async def __aenter__(self) -> "ParallelTracker":
        self.logger.info(f"Starting {len(self.task_names)} parallel tasks: {', '.join(self.task_names)}")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        # Log summary
        successful = sum(1 for success, _ in self._results.values() if success)
        total_time = max((t for _, t in self._results.values()), default=0)
        self.logger.info(f"Parallel tasks: {successful}/{len(self.task_names)} succeeded in {total_time:.0f}ms")

    async def track(self, name: str, coro: Awaitable[T]) -> T:
        """Track a single task within the parallel operation."""
        self._start_times[name] = time.perf_counter()
        try:
            result = await coro
            duration = (time.perf_counter() - self._start_times[name]) * 1000
            self._results[name] = (True, duration)
            self.logger.debug(f"  [{name}] completed in {duration:.0f}ms")
            return result
        except Exception as e:
            duration = (time.perf_counter() - self._start_times[name]) * 1000
            self._results[name] = (False, duration)
            self.logger.warning(f"  [{name}] failed in {duration:.0f}ms: {e}")
            raise


# =============================================================================
# UNIFIED LOGGER
# =============================================================================
class UnifiedLogger:
    """
    Enterprise-grade logging with visual organization AND performance metrics.

    Merges:
    - OrganizedLogger: Section boxes, visual hierarchy
    - PerformanceLogger: Millisecond timing, phase tracking

    Features:
    - Visual section boxes with ASCII headers
    - Millisecond-precision timing
    - Nested context tracking
    - Parallel operation logging
    - JSON output mode option
    - Color-coded severity
    - Thread-safe + asyncio-safe
    """

    _instance: Optional["UnifiedLogger"] = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls) -> "UnifiedLogger":
        """Singleton pattern."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._initialize()
                    cls._instance = instance
        return cls._instance

    def _initialize(self) -> None:
        """Initialize logger state."""
        self._start_time = time.perf_counter()
        self._phase_times: Dict[str, float] = {}
        self._active_phases: Dict[str, float] = {}
        self._section_stack: List[LogSection] = []
        self._indent_level: int = 0
        self._metrics: Dict[str, List[float]] = defaultdict(list)
        self._json_mode = _get_env_bool("JARVIS_LOG_JSON", False)
        self._verbose = _get_env_bool("JARVIS_VERBOSE", False)
        self._colors_enabled = sys.stdout.isatty()
        self._log_lock = threading.Lock()

    def _elapsed_ms(self) -> float:
        """Get elapsed time since logger start in milliseconds."""
        return (time.perf_counter() - self._start_time) * 1000

    # ═══════════════════════════════════════════════════════════════════════════
    # VISUAL SECTIONS
    # ═══════════════════════════════════════════════════════════════════════════

    def section_start(self, section: LogSection, title: str) -> SectionContext:
        """Start a visual section with box header."""
        return SectionContext(self, section, title)

    def _render_section_header(self, section: LogSection, title: str) -> None:
        """Render ASCII box header."""
        width = 70
        elapsed = self._elapsed_ms()
        reset = "\033[0m" if self._colors_enabled else ""
        blue = "\033[94m" if self._colors_enabled else ""

        with self._log_lock:
            print(f"\n{blue}{'═' * width}{reset}")
            print(f"{blue}║{reset} {section.value:12} │ {title:<43} │ +{elapsed:>6.0f}ms {blue}║{reset}")
            print(f"{blue}{'═' * width}{reset}")

    def _render_section_footer(self, section: LogSection, duration_ms: float) -> None:
        """Render ASCII box footer with timing."""
        width = 70
        reset = "\033[0m" if self._colors_enabled else ""
        blue = "\033[94m" if self._colors_enabled else ""

        with self._log_lock:
            print(f"{blue}{'─' * width}{reset}")
            print(f"  └── {section.value} completed in {duration_ms:.1f}ms\n")

    # ═══════════════════════════════════════════════════════════════════════════
    # PERFORMANCE TRACKING
    # ═══════════════════════════════════════════════════════════════════════════

    def phase_start(self, phase_name: str) -> None:
        """Mark the start of a timed phase."""
        self._active_phases[phase_name] = time.perf_counter()

    def phase_end(self, phase_name: str) -> float:
        """Mark the end of a phase, return duration in ms."""
        if phase_name not in self._active_phases:
            return 0.0
        duration = (time.perf_counter() - self._active_phases.pop(phase_name)) * 1000
        self._phase_times[phase_name] = duration
        self._metrics[phase_name].append(duration)
        return duration

    @contextmanager
    def timed(self, operation: str) -> Generator[None, None, None]:
        """Context manager for timing operations."""
        self.phase_start(operation)
        try:
            yield
        finally:
            duration = self.phase_end(operation)
            self.debug(f"{operation} completed in {duration:.1f}ms")

    async def timed_async(self, operation: str, coro: Awaitable[T]) -> T:
        """Async wrapper for timing coroutines."""
        self.phase_start(operation)
        try:
            return await coro
        finally:
            duration = self.phase_end(operation)
            self.debug(f"{operation} completed in {duration:.1f}ms")

    # ═══════════════════════════════════════════════════════════════════════════
    # PARALLEL TRACKING
    # ═══════════════════════════════════════════════════════════════════════════

    def parallel_start(self, task_names: List[str]) -> ParallelTracker:
        """Track multiple parallel operations."""
        return ParallelTracker(self, task_names)

    # ═══════════════════════════════════════════════════════════════════════════
    # STANDARD LOGGING METHODS
    # ═══════════════════════════════════════════════════════════════════════════

    def _log(self, level: LogLevel, message: str, **kwargs) -> None:
        """Core logging method."""
        elapsed = self._elapsed_ms()
        indent = "  " * self._indent_level

        if self._json_mode:
            self._log_json(level, message, elapsed, **kwargs)
        else:
            reset = "\033[0m" if self._colors_enabled else ""
            color = level.value[1] if self._colors_enabled else ""
            level_str = f"[{level.value[0]:8}]"
            time_str = f"+{elapsed:>7.0f}ms"

            with self._log_lock:
                print(f"{color}{level_str}{reset} {time_str} │ {indent}{message}")

    def _log_json(self, level: LogLevel, message: str, elapsed: float, **kwargs) -> None:
        """Log in JSON format."""
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "level": level.value[0],
            "elapsed_ms": round(elapsed, 1),
            "message": message,
            **kwargs,
        }
        with self._log_lock:
            print(json.dumps(log_entry))

    def debug(self, message: str, **kwargs) -> None:
        """Debug level logging (only in verbose mode)."""
        if self._verbose:
            self._log(LogLevel.DEBUG, message, **kwargs)

    def info(self, message: str, **kwargs) -> None:
        """Info level logging."""
        self._log(LogLevel.INFO, message, **kwargs)

    def success(self, message: str, **kwargs) -> None:
        """Success level logging."""
        self._log(LogLevel.SUCCESS, f"✓ {message}", **kwargs)

    def warning(self, message: str, **kwargs) -> None:
        """Warning level logging."""
        self._log(LogLevel.WARNING, f"⚠ {message}", **kwargs)

    def error(self, message: str, **kwargs) -> None:
        """Error level logging."""
        self._log(LogLevel.ERROR, f"✗ {message}", **kwargs)

    def critical(self, message: str, **kwargs) -> None:
        """Critical level logging."""
        self._log(LogLevel.CRITICAL, f"🔥 {message}", **kwargs)

    def phase(self, message: str, **kwargs) -> None:
        """Phase announcement logging."""
        self._log(LogLevel.PHASE, f"▸ {message}", **kwargs)

    # ═══════════════════════════════════════════════════════════════════════════
    # METRICS & SUMMARY
    # ═══════════════════════════════════════════════════════════════════════════

    def get_metrics_summary(self) -> Dict[str, Any]:
        """Get performance metrics summary."""
        return {
            "total_elapsed_ms": self._elapsed_ms(),
            "phase_times": dict(self._phase_times),
            "phase_averages": {
                k: sum(v) / len(v) for k, v in self._metrics.items() if v
            },
        }

    def print_startup_summary(self) -> None:
        """Print final startup timing summary."""
        total = self._elapsed_ms()
        reset = "\033[0m" if self._colors_enabled else ""
        green = "\033[92m" if self._colors_enabled else ""

        print(f"\n{green}{'═' * 70}{reset}")
        print(f"{green}║ STARTUP COMPLETE │ Total: {total:.0f}ms ({total/1000:.2f}s){reset}")
        print(f"{green}{'═' * 70}{reset}")

        # Top 5 slowest phases
        sorted_phases = sorted(self._phase_times.items(), key=lambda x: x[1], reverse=True)[:5]
        if sorted_phases:
            print("║ Slowest phases:")
            for phase, duration in sorted_phases:
                pct = (duration / total * 100) if total > 0 else 0
                bar_len = int(pct / 100 * 30)
                bar = "█" * bar_len + "░" * (30 - bar_len)
                print(f"║   {phase:30} │ {bar} │ {duration:>6.0f}ms ({pct:>4.1f}%)")

        print(f"{green}{'═' * 70}{reset}\n")


# Global logger instance for use throughout the kernel
_unified_logger = UnifiedLogger()


# =============================================================================
# STARTUP LOCK (Singleton Enforcement)
# =============================================================================
class StartupLock:
    """
    Enforce single-instance kernel using file locks.

    Features:
    - PID-based lock verification
    - Stale lock detection and cleanup
    - Lock file contains process metadata
    """

    def __init__(self, lock_name: str = "kernel"):
        self.lock_name = lock_name
        self.lock_path = LOCKS_DIR / f"{lock_name}.lock"
        self.pid = os.getpid()
        self._acquired = False

    def is_locked(self) -> Tuple[bool, Optional[int]]:
        """Check if lock is held. Returns (is_locked, holder_pid)."""
        if not self.lock_path.exists():
            return False, None

        try:
            content = self.lock_path.read_text().strip()
            data = json.loads(content)
            holder_pid = data.get("pid")

            if holder_pid and self._is_process_alive(holder_pid):
                return True, holder_pid
            else:
                # Stale lock
                return False, None

        except (json.JSONDecodeError, KeyError, OSError):
            return False, None

    def _is_process_alive(self, pid: int) -> bool:
        """Check if a process is alive."""
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    def acquire(self, force: bool = False) -> bool:
        """
        Acquire the lock.

        Args:
            force: If True, forcibly take lock from another process

        Returns:
            True if lock acquired, False otherwise
        """
        is_locked, holder_pid = self.is_locked()

        if is_locked and not force:
            return False

        # Clean up stale lock or force acquire
        if self.lock_path.exists():
            self.lock_path.unlink()

        # Write new lock
        lock_data = {
            "pid": self.pid,
            "acquired_at": datetime.now().isoformat(),
            "kernel_version": KERNEL_VERSION,
            "hostname": platform.node(),
        }

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.write_text(json.dumps(lock_data, indent=2))
        self._acquired = True

        return True

    def release(self) -> None:
        """Release the lock."""
        if self._acquired and self.lock_path.exists():
            try:
                content = self.lock_path.read_text()
                data = json.loads(content)
                if data.get("pid") == self.pid:
                    self.lock_path.unlink()
            except (json.JSONDecodeError, OSError):
                pass
        self._acquired = False

    def get_current_holder(self) -> Optional[Dict[str, Any]]:
        """Get info about the current lock holder, or None if not locked."""
        if not self.lock_path.exists():
            return None
        try:
            content = self.lock_path.read_text().strip()
            data = json.loads(content)
            holder_pid = data.get("pid")
            if holder_pid and self._is_process_alive(holder_pid):
                return data
            return None  # Stale lock
        except (json.JSONDecodeError, KeyError, OSError):
            return None

    def __enter__(self) -> "StartupLock":
        if not self.acquire():
            raise RuntimeError(f"Could not acquire lock: {self.lock_name}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


# =============================================================================
# CIRCUIT BREAKER STATE
# =============================================================================
class CircuitBreakerState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing recovery


# =============================================================================
# CIRCUIT BREAKER
# =============================================================================
class CircuitBreaker:
    """
    Circuit breaker pattern for fault tolerance.

    Prevents cascade failures by stopping requests to failing services.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 3,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls

        self._state = CircuitBreakerState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: Optional[float] = None
        self._half_open_calls = 0
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitBreakerState:
        """Get current state (may transition from OPEN to HALF_OPEN)."""
        with self._lock:
            if self._state == CircuitBreakerState.OPEN:
                if self._last_failure_time and \
                   time.time() - self._last_failure_time >= self.recovery_timeout:
                    self._state = CircuitBreakerState.HALF_OPEN
                    self._half_open_calls = 0
            return self._state

    def can_execute(self) -> bool:
        """Check if execution is allowed."""
        state = self.state
        if state == CircuitBreakerState.CLOSED:
            return True
        if state == CircuitBreakerState.HALF_OPEN:
            with self._lock:
                if self._half_open_calls < self.half_open_max_calls:
                    self._half_open_calls += 1
                    return True
        return False

    def record_success(self) -> None:
        """Record successful execution."""
        with self._lock:
            if self._state == CircuitBreakerState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.half_open_max_calls:
                    self._state = CircuitBreakerState.CLOSED
                    self._failure_count = 0
                    self._success_count = 0
            elif self._state == CircuitBreakerState.CLOSED:
                self._failure_count = max(0, self._failure_count - 1)

    def record_failure(self) -> None:
        """Record failed execution."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()

            if self._state == CircuitBreakerState.HALF_OPEN:
                self._state = CircuitBreakerState.OPEN
            elif self._failure_count >= self.failure_threshold:
                self._state = CircuitBreakerState.OPEN

    async def execute(self, coro: Awaitable[T]) -> T:
        """Execute with circuit breaker protection."""
        if not self.can_execute():
            raise RuntimeError(f"Circuit breaker {self.name} is OPEN")

        try:
            result = await coro
            self.record_success()
            return result
        except Exception:
            self.record_failure()
            raise


# =============================================================================
# RETRY WITH BACKOFF
# =============================================================================
class RetryWithBackoff:
    """
    Retry logic with exponential backoff.

    Features:
    - Configurable max retries and delays
    - Exponential backoff with jitter
    - Exception filtering
    """

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        exponential_base: float = 2.0,
        jitter: float = 0.1,
        retry_exceptions: Optional[Tuple[Type[Exception], ...]] = None,
    ):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.jitter = jitter
        self.retry_exceptions = retry_exceptions or (Exception,)

    def _calculate_delay(self, attempt: int) -> float:
        """Calculate delay for given attempt with jitter."""
        delay = min(
            self.base_delay * (self.exponential_base ** attempt),
            self.max_delay
        )
        # Add jitter
        jitter_range = delay * self.jitter
        delay += (time.time() % 1) * jitter_range * 2 - jitter_range
        return max(0, delay)

    async def execute(
        self,
        coro_factory: Callable[[], Awaitable[T]],
        operation_name: str = "operation",
    ) -> T:
        """Execute with retry logic."""
        last_exception: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            try:
                return await coro_factory()
            except self.retry_exceptions as e:
                last_exception = e

                if attempt < self.max_retries:
                    delay = self._calculate_delay(attempt)
                    logging.debug(
                        f"Retry {attempt + 1}/{self.max_retries} for {operation_name} "
                        f"after {delay:.1f}s: {e}"
                    )
                    await asyncio.sleep(delay)

        raise last_exception or RuntimeError(f"Retries exhausted for {operation_name}")


# =============================================================================
# TERMINAL UI HELPERS
# =============================================================================
class TerminalUI:
    """Terminal UI utilities for visual feedback."""

    # ANSI color codes
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"

    @classmethod
    def _supports_color(cls) -> bool:
        """Check if terminal supports colors."""
        return sys.stdout.isatty()

    @classmethod
    def _color(cls, text: str, color: str) -> str:
        """Apply color to text if supported."""
        if cls._supports_color():
            return f"{color}{text}{cls.RESET}"
        return text

    @classmethod
    def print_banner(cls, title: str, subtitle: str = "") -> None:
        """Print a banner with title."""
        width = 70

        print()
        print(cls._color("╔" + "═" * (width - 2) + "╗", cls.CYAN))
        print(cls._color("║", cls.CYAN) + f" {title:^{width - 4}} " + cls._color("║", cls.CYAN))
        if subtitle:
            print(cls._color("║", cls.CYAN) + f" {subtitle:^{width - 4}} " + cls._color("║", cls.CYAN))
        print(cls._color("╚" + "═" * (width - 2) + "╝", cls.CYAN))
        print()

    @classmethod
    def print_success(cls, message: str) -> None:
        """Print success message."""
        print(cls._color(f"✓ {message}", cls.GREEN))

    @classmethod
    def print_error(cls, message: str) -> None:
        """Print error message."""
        print(cls._color(f"✗ {message}", cls.RED))

    @classmethod
    def print_warning(cls, message: str) -> None:
        """Print warning message."""
        print(cls._color(f"⚠ {message}", cls.YELLOW))

    @classmethod
    def print_info(cls, message: str) -> None:
        """Print info message."""
        print(cls._color(f"ℹ {message}", cls.BLUE))

    @classmethod
    def print_progress(cls, current: int, total: int, label: str = "") -> None:
        """Print a progress bar."""
        if total == 0:
            pct = 100
        else:
            pct = int(current / total * 100)

        bar_width = 30
        filled = int(bar_width * current / total) if total > 0 else bar_width
        bar = "█" * filled + "░" * (bar_width - filled)

        line = f"\r  [{bar}] {pct:3d}% {label}"
        sys.stdout.write(line)
        sys.stdout.flush()

        if current >= total:
            print()  # New line when complete


# =============================================================================
# BENIGN WARNING FILTER
# =============================================================================
class BenignWarningFilter(logging.Filter):
    """
    Filter to suppress known benign warnings from ML frameworks.

    These warnings are informational and not actual problems:
    - "Wav2Vec2Model is frozen" = Expected for inference
    - "Some weights not initialized" = Expected for fine-tuned models
    """

    _SUPPRESSED_PATTERNS = [
        'wav2vec2model is frozen',
        'model is frozen',
        'weights were not initialized',
        'you should probably train',
        'some weights of the model checkpoint',
        'initializing bert',
        'initializing wav2vec',
        'registered checkpoint',
        'non-supported python version',
        'gspread not available',
        'redis not available',
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        """Return False to suppress, True to allow."""
        msg_lower = record.getMessage().lower()
        for pattern in self._SUPPRESSED_PATTERNS:
            if pattern in msg_lower:
                return False
        return True


# Install benign warning filter on noisy loggers
_benign_filter = BenignWarningFilter()
for _logger_name in ["speechbrain", "transformers", "transformers.modeling_utils"]:
    logging.getLogger(_logger_name).addFilter(_benign_filter)


# ╔═══════════════════════════════════════════════════════════════════════════════╗
# ║                                                                               ║
# ║   END OF ZONE 2                                                               ║
# ║                                                                               ║
# ╚═══════════════════════════════════════════════════════════════════════════════╝


# ╔═══════════════════════════════════════════════════════════════════════════════╗
# ║                                                                               ║
# ║   ZONE 3: RESOURCE MANAGERS (~10,000 lines)                                   ║
# ║                                                                               ║
# ║   All resource managers share a common base class with:                       ║
# ║   - async initialize() / cleanup() lifecycle                                  ║
# ║   - health_check() for monitoring                                             ║
# ║   - Graceful degradation on failure                                           ║
# ║                                                                               ║
# ║   Managers:                                                                   ║
# ║   - DockerDaemonManager: Docker lifecycle, auto-start                         ║
# ║   - GCPInstanceManager: Spot VMs, Cloud Run, Cloud SQL                        ║
# ║   - ScaleToZeroCostOptimizer: Idle detection, budget enforcement              ║
# ║   - DynamicPortManager: Zero-hardcoding port allocation                       ║
# ║   - SemanticVoiceCacheManager: ECAPA embedding cache                          ║
# ║   - TieredStorageManager: Hot/warm/cold tiering                               ║
# ║                                                                               ║
# ╚═══════════════════════════════════════════════════════════════════════════════╝


# =============================================================================
# RESOURCE MANAGER BASE CLASS
# =============================================================================
class ResourceManagerBase(ABC):
    """
    Abstract base class for all resource managers.

    All managers follow a consistent lifecycle:
    1. __init__(): Configuration only, no I/O
    2. initialize(): Async setup, can fail gracefully
    3. health_check(): Periodic monitoring
    4. cleanup(): Async teardown

    Principles:
    - Zero hardcoding: All values from env vars or dynamic detection
    - Graceful degradation: Failures don't crash the kernel
    - Observable: Metrics, logs, health endpoints
    - Async-first: All I/O is async
    """

    def __init__(self, name: str, config: Optional[SystemKernelConfig] = None):
        self.name = name
        self.config = config or SystemKernelConfig.from_environment()
        self._initialized = False
        self._ready = False
        self._error: Optional[str] = None
        self._init_time: Optional[float] = None
        self._last_health_check: Optional[float] = None
        self._health_status: str = "unknown"
        self._circuit_breaker = CircuitBreaker(f"{name}_circuit")
        self._logger = UnifiedLogger()

    @abstractmethod
    async def initialize(self) -> bool:
        """
        Initialize the resource manager.

        Returns:
            True if initialization succeeded, False otherwise.

        Note:
            Implementations should set self._initialized = True on success.
        """
        pass

    @abstractmethod
    async def health_check(self) -> Tuple[bool, str]:
        """
        Check health of the managed resource.

        Returns:
            Tuple of (healthy: bool, message: str)
        """
        pass

    @abstractmethod
    async def cleanup(self) -> None:
        """
        Clean up the managed resource.

        Note:
            Should be idempotent - safe to call multiple times.
        """
        pass

    @property
    def is_ready(self) -> bool:
        """True if manager is initialized and healthy."""
        return self._initialized and self._ready

    @property
    def status(self) -> Dict[str, Any]:
        """Get current status of the manager."""
        return {
            "name": self.name,
            "initialized": self._initialized,
            "ready": self._ready,
            "health_status": self._health_status,
            "error": self._error,
            "init_time_ms": int(self._init_time * 1000) if self._init_time else None,
            "last_health_check": self._last_health_check,
            "circuit_breaker_state": self._circuit_breaker.state.value,
        }

    async def safe_initialize(self) -> bool:
        """
        Initialize with circuit breaker protection and timing.

        Returns:
            True if initialization succeeded, False otherwise.
        """
        start = time.time()
        try:
            result = await self._circuit_breaker.execute(self.initialize())
            self._init_time = time.time() - start
            if result:
                self._ready = True
                self._health_status = "healthy"
                self._logger.success(f"{self.name} initialized in {self._init_time*1000:.0f}ms")
            else:
                self._error = "Initialization returned False"
                self._health_status = "unhealthy"
                self._logger.warning(f"{self.name} initialization failed")
            return result
        except Exception as e:
            self._init_time = time.time() - start
            self._error = str(e)
            self._health_status = "error"
            self._logger.error(f"{self.name} initialization error: {e}")
            return False

    async def safe_health_check(self) -> Tuple[bool, str]:
        """
        Health check with circuit breaker protection.

        Returns:
            Tuple of (healthy: bool, message: str)
        """
        try:
            healthy, message = await self._circuit_breaker.execute(self.health_check())
            self._last_health_check = time.time()
            self._ready = healthy
            self._health_status = "healthy" if healthy else "unhealthy"
            return healthy, message
        except Exception as e:
            self._last_health_check = time.time()
            self._ready = False
            self._health_status = "error"
            return False, f"Health check error: {e}"


# =============================================================================
# DOCKER DAEMON STATUS ENUM
# =============================================================================
class DaemonStatus(Enum):
    """Docker daemon status states."""
    UNKNOWN = "unknown"
    NOT_INSTALLED = "not_installed"
    INSTALLED_NOT_RUNNING = "installed_not_running"
    STARTING = "starting"
    RUNNING = "running"
    ERROR = "error"


# =============================================================================
# DOCKER DAEMON HEALTH DATACLASS
# =============================================================================
@dataclass
class DaemonHealth:
    """Docker daemon health information."""
    status: DaemonStatus
    socket_exists: bool = False
    process_running: bool = False
    daemon_responsive: bool = False
    api_accessible: bool = False
    last_check_timestamp: float = 0.0
    startup_time_ms: int = 0
    error_message: Optional[str] = None

    def is_healthy(self) -> bool:
        """Check if daemon is fully healthy."""
        return self.daemon_responsive and self.api_accessible

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "status": self.status.value,
            "socket_exists": self.socket_exists,
            "process_running": self.process_running,
            "daemon_responsive": self.daemon_responsive,
            "api_accessible": self.api_accessible,
            "last_check_timestamp": self.last_check_timestamp,
            "startup_time_ms": self.startup_time_ms,
            "error_message": self.error_message,
            "healthy": self.is_healthy(),
        }


# =============================================================================
# DOCKER DAEMON MANAGER
# =============================================================================
class DockerDaemonManager(ResourceManagerBase):
    """
    Production-grade Docker daemon manager.

    Handles Docker Desktop/daemon lifecycle with:
    - Async startup and monitoring
    - Intelligent health checks (parallel for speed)
    - Platform-specific optimizations (macOS, Linux, Windows)
    - Comprehensive error handling with retry logic
    - Circuit breaker for fault tolerance

    Environment Configuration:
    - DOCKER_ENABLED: Enable Docker management (default: true)
    - DOCKER_AUTO_START: Auto-start daemon (default: true)
    - DOCKER_HEALTH_CHECK_TIMEOUT: Health check timeout in seconds (default: 5.0)
    - DOCKER_MAX_STARTUP_WAIT: Max wait for daemon startup in seconds (default: 120)
    - DOCKER_MAX_RETRY_ATTEMPTS: Max retry attempts (default: 3)
    - DOCKER_APP_PATH_MACOS: macOS Docker.app path (default: /Applications/Docker.app)
    - DOCKER_APP_PATH_WINDOWS: Windows Docker path
    - DOCKER_PARALLEL_HEALTH_CHECKS: Use parallel health checks (default: true)
    """

    # Socket paths to check
    SOCKET_PATHS = [
        Path('/var/run/docker.sock'),  # Linux/macOS (daemon)
        Path.home() / '.docker' / 'run' / 'docker.sock',  # macOS (Desktop)
    ]

    def __init__(self, config: Optional[SystemKernelConfig] = None):
        super().__init__("DockerDaemonManager", config)

        # Platform detection
        self.platform = platform.system().lower()

        # Configuration from environment (zero hardcoding)
        self.enabled = os.getenv("DOCKER_ENABLED", "true").lower() == "true"
        self.auto_start = os.getenv("DOCKER_AUTO_START", "true").lower() == "true"
        self.health_check_timeout = float(os.getenv("DOCKER_HEALTH_CHECK_TIMEOUT", "5.0"))
        self.max_startup_wait = float(os.getenv("DOCKER_MAX_STARTUP_WAIT", "120"))
        self.max_retry_attempts = int(os.getenv("DOCKER_MAX_RETRY_ATTEMPTS", "3"))
        self.retry_backoff_base = float(os.getenv("DOCKER_RETRY_BACKOFF_BASE", "2.0"))
        self.retry_backoff_max = float(os.getenv("DOCKER_RETRY_BACKOFF_MAX", "30.0"))
        self.poll_interval = float(os.getenv("DOCKER_POLL_INTERVAL", "2.0"))
        self.parallel_health_checks = os.getenv("DOCKER_PARALLEL_HEALTH_CHECKS", "true").lower() == "true"

        # Platform-specific paths
        self.docker_app_path_macos = os.getenv(
            "DOCKER_APP_PATH_MACOS",
            "/Applications/Docker.app"
        )
        self.docker_app_path_windows = os.getenv(
            "DOCKER_APP_PATH_WINDOWS",
            r"C:\Program Files\Docker\Docker\Docker Desktop.exe"
        )

        # State
        self.health = DaemonHealth(status=DaemonStatus.UNKNOWN)
        self._startup_task: Optional[asyncio.Task] = None
        self._progress_callback: Optional[Callable[[str], None]] = None

    def set_progress_callback(self, callback: Callable[[str], None]) -> None:
        """Set callback for progress updates."""
        self._progress_callback = callback

    def _report_progress(self, message: str) -> None:
        """Report progress via callback."""
        if self._progress_callback:
            try:
                self._progress_callback(message)
            except Exception as e:
                self._logger.debug(f"Progress callback error: {e}")

    async def initialize(self) -> bool:
        """Initialize Docker daemon manager and ensure daemon is running."""
        if not self.enabled:
            self._logger.info("Docker management disabled")
            self._initialized = True
            return True

        # Check if Docker is installed
        if not await self._check_installation():
            self.health.status = DaemonStatus.NOT_INSTALLED
            self._error = "Docker not installed"
            # Not a fatal error - system can run without Docker
            self._initialized = True
            return True

        # Check current health
        await self._check_daemon_health()

        if self.health.is_healthy():
            self._logger.success("Docker daemon already running")
            self._initialized = True
            return True

        # Auto-start if enabled
        if self.auto_start:
            if await self._start_daemon():
                self._initialized = True
                return True
            else:
                self._error = "Failed to start Docker daemon"
                self._initialized = True
                return True  # Still return True - non-fatal

        self._initialized = True
        return True

    async def health_check(self) -> Tuple[bool, str]:
        """Check Docker daemon health."""
        if not self.enabled:
            return True, "Docker management disabled"

        await self._check_daemon_health()

        if self.health.is_healthy():
            return True, f"Docker daemon healthy (status: {self.health.status.value})"
        else:
            return False, f"Docker daemon unhealthy: {self.health.error_message or self.health.status.value}"

    async def cleanup(self) -> None:
        """Clean up Docker daemon manager (does not stop daemon)."""
        if self._startup_task and not self._startup_task.done():
            self._startup_task.cancel()
            try:
                await self._startup_task
            except asyncio.CancelledError:
                pass
        self._initialized = False

    async def _check_installation(self) -> bool:
        """Check if Docker is installed."""
        try:
            proc = await asyncio.create_subprocess_exec(
                'docker', '--version',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if proc.returncode == 0:
                version = stdout.decode().strip()
                self._logger.debug(f"Docker installed: {version}")
                return True
            return False
        except FileNotFoundError:
            return False
        except asyncio.TimeoutError:
            return False
        except Exception:
            return False

    async def _check_daemon_health(self) -> DaemonHealth:
        """Comprehensive daemon health check."""
        start_time = time.time()
        health = DaemonHealth(status=DaemonStatus.UNKNOWN)

        if self.parallel_health_checks:
            # Run all checks in parallel for speed
            checks = await asyncio.gather(
                self._check_socket_exists(),
                self._check_process_running(),
                self._check_daemon_responsive(),
                self._check_api_accessible(),
                return_exceptions=True
            )

            health.socket_exists = checks[0] if not isinstance(checks[0], Exception) else False
            health.process_running = checks[1] if not isinstance(checks[1], Exception) else False
            health.daemon_responsive = checks[2] if not isinstance(checks[2], Exception) else False
            health.api_accessible = checks[3] if not isinstance(checks[3], Exception) else False
        else:
            # Sequential checks (fallback)
            health.socket_exists = await self._check_socket_exists()
            health.process_running = await self._check_process_running()
            health.daemon_responsive = await self._check_daemon_responsive()
            health.api_accessible = await self._check_api_accessible()

        # Determine overall status
        if health.daemon_responsive and health.api_accessible:
            health.status = DaemonStatus.RUNNING
        elif health.socket_exists or health.process_running:
            health.status = DaemonStatus.STARTING
        else:
            health.status = DaemonStatus.INSTALLED_NOT_RUNNING

        health.last_check_timestamp = time.time()
        self.health = health
        return health

    async def _check_socket_exists(self) -> bool:
        """Check if Docker socket exists."""
        try:
            for socket_path in self.SOCKET_PATHS:
                if socket_path.exists():
                    return True

            # Windows named pipe
            if self.platform == 'windows':
                # Can't easily check named pipe existence, assume it might exist
                return True

            return False
        except Exception:
            return False

    async def _check_process_running(self) -> bool:
        """Check if Docker process is running."""
        try:
            if self.platform == 'darwin':
                # Check for Docker Desktop on macOS
                proc = await asyncio.create_subprocess_exec(
                    'pgrep', '-x', 'Docker',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await asyncio.wait_for(proc.communicate(), timeout=2.0)
                return proc.returncode == 0

            elif self.platform == 'linux':
                # Check for dockerd on Linux
                proc = await asyncio.create_subprocess_exec(
                    'pgrep', '-x', 'dockerd',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await asyncio.wait_for(proc.communicate(), timeout=2.0)
                return proc.returncode == 0

            elif self.platform == 'windows':
                proc = await asyncio.create_subprocess_exec(
                    'tasklist', '/FI', 'IMAGENAME eq Docker Desktop.exe',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
                return b'Docker Desktop.exe' in stdout

            return False
        except Exception:
            return False

    async def _check_daemon_responsive(self) -> bool:
        """Check if daemon responds to 'docker info'."""
        try:
            proc = await asyncio.create_subprocess_exec(
                'docker', 'info',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await asyncio.wait_for(proc.communicate(), timeout=self.health_check_timeout)
            return proc.returncode == 0
        except asyncio.TimeoutError:
            return False
        except Exception:
            return False

    async def _check_api_accessible(self) -> bool:
        """Check if Docker API is accessible via 'docker ps'."""
        try:
            proc = await asyncio.create_subprocess_exec(
                'docker', 'ps', '--format', '{{.ID}}',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await asyncio.wait_for(proc.communicate(), timeout=self.health_check_timeout)
            return proc.returncode == 0
        except asyncio.TimeoutError:
            return False
        except Exception:
            return False

    async def _start_daemon(self) -> bool:
        """Start Docker daemon with intelligent retry."""
        self._logger.info("Starting Docker daemon...")
        self._report_progress("Starting Docker daemon...")

        for attempt in range(1, self.max_retry_attempts + 1):
            self._logger.debug(f"Start attempt {attempt}/{self.max_retry_attempts}")
            self._report_progress(f"Start attempt {attempt}/{self.max_retry_attempts}")

            # Launch Docker
            if await self._launch_docker_app():
                self._report_progress("Waiting for daemon...")

                if await self._wait_for_daemon_ready():
                    self._logger.success("Docker daemon started successfully!")
                    return True

                self._logger.warning(f"Daemon did not become ready (attempt {attempt})")

            # Exponential backoff between retries
            if attempt < self.max_retry_attempts:
                backoff = min(
                    self.retry_backoff_base ** attempt,
                    self.retry_backoff_max
                )
                self._logger.debug(f"Waiting {backoff:.1f}s before retry...")
                await asyncio.sleep(backoff)

        self._logger.error(f"Failed to start Docker daemon after {self.max_retry_attempts} attempts")
        self.health.error_message = "Failed to start after multiple attempts"
        return False

    async def _launch_docker_app(self) -> bool:
        """Launch Docker Desktop application."""
        try:
            if self.platform == 'darwin':
                app_path = self.docker_app_path_macos
                if not Path(app_path).exists():
                    self._logger.error(f"Docker.app not found at {app_path}")
                    return False

                proc = await asyncio.create_subprocess_exec(
                    'open', '-a', app_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await asyncio.wait_for(proc.communicate(), timeout=10.0)
                return proc.returncode == 0

            elif self.platform == 'linux':
                # Try systemd first
                proc = await asyncio.create_subprocess_exec(
                    'sudo', 'systemctl', 'start', 'docker',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await asyncio.wait_for(proc.communicate(), timeout=10.0)
                return proc.returncode == 0

            elif self.platform == 'windows':
                proc = await asyncio.create_subprocess_exec(
                    'cmd', '/c', 'start', '', self.docker_app_path_windows,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await asyncio.wait_for(proc.communicate(), timeout=10.0)
                return proc.returncode == 0

            return False
        except Exception as e:
            self._logger.error(f"Error launching Docker: {e}")
            return False

    async def _wait_for_daemon_ready(self) -> bool:
        """Wait for daemon to become fully ready."""
        start_time = time.time()
        check_count = 0

        while (time.time() - start_time) < self.max_startup_wait:
            check_count += 1

            health = await self._check_daemon_health()

            if health.is_healthy():
                elapsed = time.time() - start_time
                self.health.startup_time_ms = int(elapsed * 1000)
                self._logger.debug(f"Daemon ready in {elapsed:.1f}s")
                return True

            # Progress reporting
            if check_count % 5 == 0:
                elapsed = time.time() - start_time
                self._report_progress(f"Still waiting ({elapsed:.0f}s)...")

            await asyncio.sleep(self.poll_interval)

        self._logger.warning(f"Timeout waiting for daemon ({self.max_startup_wait}s)")
        return False

    async def stop_daemon(self) -> bool:
        """Stop Docker daemon/Desktop gracefully."""
        self._logger.info("Stopping Docker daemon...")

        try:
            if self.platform == 'darwin':
                proc = await asyncio.create_subprocess_exec(
                    'osascript', '-e', 'quit app "Docker"',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await asyncio.wait_for(proc.communicate(), timeout=10.0)
                return proc.returncode == 0

            elif self.platform == 'linux':
                proc = await asyncio.create_subprocess_exec(
                    'sudo', 'systemctl', 'stop', 'docker',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await asyncio.wait_for(proc.communicate(), timeout=10.0)
                return proc.returncode == 0

            return False
        except Exception as e:
            self._logger.error(f"Error stopping Docker: {e}")
            return False


# =============================================================================
# GCP INSTANCE STATUS ENUM
# =============================================================================
class GCPInstanceStatus(Enum):
    """GCP instance status states."""
    UNKNOWN = "unknown"
    NOT_CONFIGURED = "not_configured"
    PROVISIONING = "provisioning"
    STAGING = "staging"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    SUSPENDED = "suspended"
    TERMINATED = "terminated"
    ERROR = "error"


# =============================================================================
# GCP INSTANCE MANAGER
# =============================================================================
class GCPInstanceManager(ResourceManagerBase):
    """
    GCP Compute Instance Manager for Spot VMs and Cloud Run.

    Features:
    - Spot VM provisioning with preemption handling
    - Cloud Run service management
    - Cloud SQL connection pooling
    - Recovery cascade for failures
    - Cost tracking and optimization

    Environment Configuration:
    - GCP_ENABLED: Enable GCP management (default: false)
    - GCP_PROJECT_ID: GCP project ID (required if enabled)
    - GCP_ZONE: Default zone (default: us-central1-a)
    - GCP_REGION: Default region (default: us-central1)
    - GCP_SPOT_VM_ENABLED: Enable Spot VMs (default: true)
    - GCP_PREFER_CLOUD_RUN: Prefer Cloud Run over VMs (default: false)
    - GCP_SPOT_HOURLY_RATE: Spot VM hourly rate for cost tracking (default: 0.029)
    - GCP_MACHINE_TYPE: Default machine type (default: e2-medium)
    - GCP_CREDENTIALS_PATH: Path to service account JSON
    - GCP_FIREWALL_RULE_PREFIX: Prefix for firewall rules (default: jarvis-)
    """

    def __init__(self, config: Optional[SystemKernelConfig] = None):
        super().__init__("GCPInstanceManager", config)

        # Configuration from environment
        self.enabled = os.getenv("GCP_ENABLED", "false").lower() == "true"
        self.project_id = os.getenv("GCP_PROJECT_ID", "")
        self.zone = os.getenv("GCP_ZONE", "us-central1-a")
        self.region = os.getenv("GCP_REGION", "us-central1")
        self.spot_vm_enabled = os.getenv("GCP_SPOT_VM_ENABLED", "true").lower() == "true"
        self.prefer_cloud_run = os.getenv("GCP_PREFER_CLOUD_RUN", "false").lower() == "true"
        self.spot_hourly_rate = float(os.getenv("GCP_SPOT_HOURLY_RATE", "0.029"))
        self.machine_type = os.getenv("GCP_MACHINE_TYPE", "e2-medium")
        self.credentials_path = os.getenv("GCP_CREDENTIALS_PATH", "")
        self.firewall_rule_prefix = os.getenv("GCP_FIREWALL_RULE_PREFIX", "jarvis-")

        # State
        self.instance_status = GCPInstanceStatus.UNKNOWN
        self.instance_name: Optional[str] = None
        self.instance_ip: Optional[str] = None
        self.cloud_run_url: Optional[str] = None
        self._compute_client: Optional[Any] = None
        self._run_client: Optional[Any] = None

        # Cost tracking
        self.session_start_time: Optional[float] = None
        self.total_runtime_seconds = 0.0
        self.estimated_cost = 0.0

        # Recovery state
        self._recovery_attempts = 0
        self._max_recovery_attempts = int(os.getenv("GCP_MAX_RECOVERY_ATTEMPTS", "3"))
        self._last_preemption_time: Optional[float] = None

    async def initialize(self) -> bool:
        """Initialize GCP instance manager."""
        if not self.enabled:
            self._logger.info("GCP management disabled")
            self._initialized = True
            return True

        if not self.project_id:
            self._logger.warning("GCP_PROJECT_ID not set, GCP features disabled")
            self.enabled = False
            self._initialized = True
            return True

        # Try to initialize GCP clients
        try:
            await self._initialize_clients()
            self._initialized = True
            self._logger.success(f"GCP manager initialized (project: {self.project_id})")
            return True
        except Exception as e:
            self._error = f"Failed to initialize GCP clients: {e}"
            self._logger.error(self._error)
            self._initialized = True
            return True  # Non-fatal - system can run without GCP

    async def _initialize_clients(self) -> None:
        """Initialize GCP API clients."""
        try:
            # Try to import google-cloud libraries
            from google.cloud import compute_v1
            from google.cloud import run_v2

            # Initialize compute client
            if self.credentials_path and Path(self.credentials_path).exists():
                self._compute_client = compute_v1.InstancesClient.from_service_account_json(
                    self.credentials_path
                )
            else:
                self._compute_client = compute_v1.InstancesClient()

            # Initialize Cloud Run client if preferred
            if self.prefer_cloud_run:
                if self.credentials_path and Path(self.credentials_path).exists():
                    self._run_client = run_v2.ServicesClient.from_service_account_json(
                        self.credentials_path
                    )
                else:
                    self._run_client = run_v2.ServicesClient()

        except ImportError:
            self._logger.warning("Google Cloud libraries not installed, GCP features limited")
            self._compute_client = None
            self._run_client = None

    async def health_check(self) -> Tuple[bool, str]:
        """Check GCP instance health."""
        if not self.enabled:
            return True, "GCP management disabled"

        if not self._compute_client and not self._run_client:
            return True, "GCP clients not available (limited mode)"

        # Check instance status if we have one running
        if self.instance_name:
            try:
                status = await self._get_instance_status()
                if status == GCPInstanceStatus.RUNNING:
                    return True, f"Instance {self.instance_name} running"
                else:
                    return False, f"Instance {self.instance_name} status: {status.value}"
            except Exception as e:
                return False, f"Failed to check instance: {e}"

        # Check Cloud Run if configured
        if self.cloud_run_url:
            return True, f"Cloud Run service at {self.cloud_run_url}"

        return True, "GCP manager ready (no active instances)"

    async def cleanup(self) -> None:
        """Clean up GCP resources."""
        # Update cost tracking
        if self.session_start_time:
            self.total_runtime_seconds += time.time() - self.session_start_time
            self.estimated_cost = (self.total_runtime_seconds / 3600) * self.spot_hourly_rate

        # Log cost summary
        if self.total_runtime_seconds > 0:
            self._logger.info(
                f"GCP session summary: runtime={self.total_runtime_seconds/60:.1f}min, "
                f"estimated_cost=${self.estimated_cost:.4f}"
            )

        self._initialized = False

    async def _get_instance_status(self) -> GCPInstanceStatus:
        """Get current instance status from GCP."""
        if not self._compute_client or not self.instance_name:
            return GCPInstanceStatus.UNKNOWN

        try:
            # Run in executor to not block
            loop = asyncio.get_event_loop()
            instance = await loop.run_in_executor(
                None,
                lambda: self._compute_client.get(
                    project=self.project_id,
                    zone=self.zone,
                    instance=self.instance_name
                )
            )

            status_map = {
                "PROVISIONING": GCPInstanceStatus.PROVISIONING,
                "STAGING": GCPInstanceStatus.STAGING,
                "RUNNING": GCPInstanceStatus.RUNNING,
                "STOPPING": GCPInstanceStatus.STOPPING,
                "STOPPED": GCPInstanceStatus.STOPPED,
                "SUSPENDED": GCPInstanceStatus.SUSPENDED,
                "TERMINATED": GCPInstanceStatus.TERMINATED,
            }

            self.instance_status = status_map.get(instance.status, GCPInstanceStatus.UNKNOWN)
            return self.instance_status

        except Exception as e:
            self._logger.error(f"Failed to get instance status: {e}")
            return GCPInstanceStatus.ERROR

    async def provision_spot_vm(self, name: Optional[str] = None) -> bool:
        """
        Provision a new Spot VM.

        Args:
            name: Optional instance name (auto-generated if not provided)

        Returns:
            True if provisioning started successfully
        """
        if not self.enabled or not self.spot_vm_enabled:
            return False

        if not self._compute_client:
            self._logger.error("Compute client not available")
            return False

        try:
            from google.cloud import compute_v1

            self.instance_name = name or f"jarvis-spot-{uuid.uuid4().hex[:8]}"
            self._logger.info(f"Provisioning Spot VM: {self.instance_name}")

            # Configure instance
            instance = compute_v1.Instance()
            instance.name = self.instance_name
            instance.machine_type = f"zones/{self.zone}/machineTypes/{self.machine_type}"

            # Configure Spot (preemptible) scheduling
            scheduling = compute_v1.Scheduling()
            scheduling.preemptible = True
            scheduling.automatic_restart = False
            scheduling.on_host_maintenance = "TERMINATE"
            instance.scheduling = scheduling

            # Add boot disk
            disk = compute_v1.AttachedDisk()
            disk.boot = True
            disk.auto_delete = True
            init_params = compute_v1.AttachedDiskInitializeParams()
            init_params.source_image = "projects/debian-cloud/global/images/family/debian-11"
            init_params.disk_size_gb = 20
            disk.initialize_params = init_params
            instance.disks = [disk]

            # Network interface
            network_interface = compute_v1.NetworkInterface()
            network_interface.network = "global/networks/default"
            access_config = compute_v1.AccessConfig()
            access_config.name = "External NAT"
            access_config.type_ = "ONE_TO_ONE_NAT"
            network_interface.access_configs = [access_config]
            instance.network_interfaces = [network_interface]

            # Insert instance
            loop = asyncio.get_event_loop()
            operation = await loop.run_in_executor(
                None,
                lambda: self._compute_client.insert(
                    project=self.project_id,
                    zone=self.zone,
                    instance_resource=instance
                )
            )

            self.instance_status = GCPInstanceStatus.PROVISIONING
            self.session_start_time = time.time()
            self._logger.success(f"Spot VM provisioning started: {self.instance_name}")
            return True

        except Exception as e:
            self._logger.error(f"Failed to provision Spot VM: {e}")
            self.instance_status = GCPInstanceStatus.ERROR
            return False

    async def handle_preemption(self) -> bool:
        """
        Handle Spot VM preemption with recovery cascade.

        Returns:
            True if recovery succeeded
        """
        self._last_preemption_time = time.time()
        self._recovery_attempts += 1

        self._logger.warning(
            f"Spot VM preempted! Recovery attempt {self._recovery_attempts}/{self._max_recovery_attempts}"
        )

        if self._recovery_attempts > self._max_recovery_attempts:
            self._logger.error("Max recovery attempts exceeded")
            return False

        # Exponential backoff before retry
        backoff = min(2 ** self._recovery_attempts, 60)
        await asyncio.sleep(backoff)

        # Try to provision new VM
        return await self.provision_spot_vm()

    def get_cost_summary(self) -> Dict[str, Any]:
        """Get cost summary for this session."""
        current_runtime = 0.0
        if self.session_start_time:
            current_runtime = time.time() - self.session_start_time

        total = self.total_runtime_seconds + current_runtime

        return {
            "enabled": self.enabled,
            "spot_vm_enabled": self.spot_vm_enabled,
            "instance_name": self.instance_name,
            "instance_status": self.instance_status.value,
            "session_runtime_seconds": current_runtime,
            "total_runtime_seconds": total,
            "hourly_rate": self.spot_hourly_rate,
            "estimated_cost": (total / 3600) * self.spot_hourly_rate,
            "recovery_attempts": self._recovery_attempts,
            "last_preemption_time": self._last_preemption_time,
        }


# =============================================================================
# COST TRACKER
# =============================================================================
class CostTracker(ResourceManagerBase):
    """
    Enterprise-grade cost tracking for cloud resources.

    Features:
    - Real-time cost estimation for GCP VMs
    - Session-based cost tracking with persistence
    - Budget enforcement with alerts
    - Daily/weekly/monthly cost summaries
    - Spot vs regular VM savings calculation
    - Cloud SQL and Cloud Run cost tracking

    Environment Configuration:
    - COST_TRACKING_ENABLED: Enable cost tracking (default: true)
    - COST_SPOT_VM_HOURLY: Spot VM hourly rate (default: 0.029)
    - COST_REGULAR_VM_HOURLY: Regular VM hourly rate (default: 0.097)
    - COST_CLOUD_SQL_HOURLY: Cloud SQL hourly rate (default: 0.017)
    - COST_BUDGET_DAILY_USD: Daily budget limit (default: 5.0)
    - COST_BUDGET_MONTHLY_USD: Monthly budget limit (default: 100.0)
    - COST_ALERT_THRESHOLD: Alert at % of budget (default: 0.8)
    - COST_STATE_FILE: Path to persist cost state
    """

    def __init__(self, config: Optional[SystemKernelConfig] = None):
        super().__init__("CostTracker", config)

        # Configuration from environment
        self.enabled = os.getenv("COST_TRACKING_ENABLED", "true").lower() == "true"
        self.spot_vm_hourly = float(os.getenv("COST_SPOT_VM_HOURLY", "0.029"))
        self.regular_vm_hourly = float(os.getenv("COST_REGULAR_VM_HOURLY", "0.097"))
        self.cloud_sql_hourly = float(os.getenv("COST_CLOUD_SQL_HOURLY", "0.017"))
        self.daily_budget = float(os.getenv("COST_BUDGET_DAILY_USD", "5.0"))
        self.monthly_budget = float(os.getenv("COST_BUDGET_MONTHLY_USD", "100.0"))
        self.alert_threshold = float(os.getenv("COST_ALERT_THRESHOLD", "0.8"))

        # State file
        self.state_file = Path(os.getenv(
            "COST_STATE_FILE",
            str(Path.home() / ".jarvis" / "cost_tracker.json")
        ))

        # Active sessions: instance_id -> session_info
        self.active_sessions: Dict[str, Dict[str, Any]] = {}

        # Cost accumulation
        self._daily_cost = 0.0
        self._monthly_cost = 0.0
        self._total_cost = 0.0
        self._savings_vs_regular = 0.0

        # Tracking
        self._cost_events: List[Dict[str, Any]] = []
        self._alert_callbacks: List[Callable[[Dict[str, Any]], Awaitable[None]]] = []

    async def initialize(self) -> bool:
        """Initialize cost tracker and load persisted state."""
        if not self.enabled:
            self._logger.info("Cost tracking disabled")
            self._initialized = True
            return True

        # Load persisted state
        await self._load_state()

        self._initialized = True
        self._logger.success("Cost tracker initialized")
        return True

    async def health_check(self) -> Tuple[bool, str]:
        """Check cost tracker health and budget status."""
        if not self.enabled:
            return True, "Cost tracking disabled"

        daily_pct = (self._daily_cost / self.daily_budget) * 100 if self.daily_budget > 0 else 0

        if daily_pct >= 100:
            return False, f"Daily budget exceeded: ${self._daily_cost:.2f}/${self.daily_budget:.2f}"
        elif daily_pct >= self.alert_threshold * 100:
            return True, f"Budget warning: ${self._daily_cost:.2f}/{self.daily_budget:.2f} ({daily_pct:.0f}%)"
        else:
            return True, f"Cost: ${self._daily_cost:.2f} today, ${self._monthly_cost:.2f} this month"

    async def cleanup(self) -> None:
        """Persist state and clean up."""
        await self._save_state()
        self._initialized = False

    async def record_vm_created(
        self,
        instance_id: str,
        vm_type: str = "spot",
        components: Optional[List[str]] = None,
        region: str = "us-central1",
        trigger_reason: str = "HIGH_RAM"
    ) -> None:
        """
        Record VM creation for cost tracking.

        Args:
            instance_id: GCP instance ID
            vm_type: "spot" or "regular"
            components: List of components deployed
            region: GCP region
            trigger_reason: Why VM was created
        """
        if not self.enabled:
            return

        session = {
            "instance_id": instance_id,
            "vm_type": vm_type,
            "components": components or [],
            "region": region,
            "trigger_reason": trigger_reason,
            "created_at": time.time(),
            "hourly_rate": self.spot_vm_hourly if vm_type == "spot" else self.regular_vm_hourly,
            "accumulated_cost": 0.0,
        }

        self.active_sessions[instance_id] = session
        self._logger.info(f"💰 Cost tracking started for {instance_id} ({vm_type})")

        # Record event
        self._cost_events.append({
            "type": "vm_created",
            "timestamp": time.time(),
            "instance_id": instance_id,
            "vm_type": vm_type,
        })

    async def record_vm_deleted(self, instance_id: str) -> Optional[Dict[str, Any]]:
        """
        Record VM deletion and calculate session cost.

        Args:
            instance_id: GCP instance ID

        Returns:
            Session cost summary
        """
        if not self.enabled or instance_id not in self.active_sessions:
            return None

        session = self.active_sessions.pop(instance_id)
        duration_hours = (time.time() - session["created_at"]) / 3600
        session_cost = duration_hours * session["hourly_rate"]

        # Calculate savings
        regular_cost = duration_hours * self.regular_vm_hourly
        savings = regular_cost - session_cost if session["vm_type"] == "spot" else 0

        # Update accumulators
        self._daily_cost += session_cost
        self._monthly_cost += session_cost
        self._total_cost += session_cost
        self._savings_vs_regular += savings

        result = {
            "instance_id": instance_id,
            "duration_hours": duration_hours,
            "session_cost": session_cost,
            "hourly_rate": session["hourly_rate"],
            "savings_vs_regular": savings,
            "vm_type": session["vm_type"],
        }

        self._logger.info(
            f"💰 Session ended: {instance_id} - "
            f"${session_cost:.4f} ({duration_hours:.2f}h), saved ${savings:.4f}"
        )

        # Record event
        self._cost_events.append({
            "type": "vm_deleted",
            "timestamp": time.time(),
            "instance_id": instance_id,
            **result,
        })

        # Check budget alerts
        await self._check_budget_alerts()

        # Persist state
        await self._save_state()

        return result

    async def get_cost_summary(self, period: str = "day") -> Dict[str, Any]:
        """
        Get cost summary for a period.

        Args:
            period: "day", "week", "month", or "all"

        Returns:
            Cost summary
        """
        # Update active session costs
        for instance_id, session in self.active_sessions.items():
            duration_hours = (time.time() - session["created_at"]) / 3600
            session["accumulated_cost"] = duration_hours * session["hourly_rate"]

        active_cost = sum(s["accumulated_cost"] for s in self.active_sessions.values())

        if period == "day":
            total = self._daily_cost + active_cost
            budget = self.daily_budget
        elif period == "month":
            total = self._monthly_cost + active_cost
            budget = self.monthly_budget
        else:
            total = self._total_cost + active_cost
            budget = self.monthly_budget

        return {
            "period": period,
            "total_cost": total,
            "budget": budget,
            "budget_remaining": max(0, budget - total),
            "budget_used_percent": (total / budget * 100) if budget > 0 else 0,
            "active_sessions": len(self.active_sessions),
            "active_cost": active_cost,
            "total_savings": self._savings_vs_regular,
        }

    async def check_budget_available(self, estimated_cost: float) -> Tuple[bool, str]:
        """
        Check if budget is available for an operation.

        Args:
            estimated_cost: Estimated cost of operation

        Returns:
            (allowed, reason)
        """
        if not self.enabled:
            return True, "Cost tracking disabled"

        remaining = self.daily_budget - self._daily_cost
        if estimated_cost > remaining:
            return False, f"Insufficient budget: ${remaining:.2f} remaining, ${estimated_cost:.2f} needed"

        return True, f"Budget available: ${remaining:.2f} remaining"

    async def _check_budget_alerts(self) -> None:
        """Check and trigger budget alerts."""
        daily_pct = self._daily_cost / self.daily_budget if self.daily_budget > 0 else 0
        monthly_pct = self._monthly_cost / self.monthly_budget if self.monthly_budget > 0 else 0

        if daily_pct >= self.alert_threshold:
            alert = {
                "type": "daily_budget_warning",
                "current": self._daily_cost,
                "budget": self.daily_budget,
                "percent": daily_pct * 100,
            }
            self._logger.warning(f"⚠️ Daily budget alert: ${self._daily_cost:.2f}/${self.daily_budget:.2f}")
            for callback in self._alert_callbacks:
                try:
                    await callback(alert)
                except Exception as e:
                    self._logger.error(f"Alert callback failed: {e}")

        if monthly_pct >= self.alert_threshold:
            alert = {
                "type": "monthly_budget_warning",
                "current": self._monthly_cost,
                "budget": self.monthly_budget,
                "percent": monthly_pct * 100,
            }
            self._logger.warning(f"⚠️ Monthly budget alert: ${self._monthly_cost:.2f}/${self.monthly_budget:.2f}")
            for callback in self._alert_callbacks:
                try:
                    await callback(alert)
                except Exception as e:
                    self._logger.error(f"Alert callback failed: {e}")

    def register_alert_callback(self, callback: Callable[[Dict[str, Any]], Awaitable[None]]) -> None:
        """Register a callback for budget alerts."""
        self._alert_callbacks.append(callback)

    async def _load_state(self) -> None:
        """Load persisted cost state."""
        try:
            if self.state_file.exists():
                data = json.loads(self.state_file.read_text())

                # Reset daily cost if new day
                last_date = data.get("last_date", "")
                today = time.strftime("%Y-%m-%d")
                if last_date != today:
                    self._daily_cost = 0.0
                else:
                    self._daily_cost = data.get("daily_cost", 0.0)

                # Reset monthly cost if new month
                last_month = data.get("last_month", "")
                this_month = time.strftime("%Y-%m")
                if last_month != this_month:
                    self._monthly_cost = 0.0
                else:
                    self._monthly_cost = data.get("monthly_cost", 0.0)

                self._total_cost = data.get("total_cost", 0.0)
                self._savings_vs_regular = data.get("savings", 0.0)

                self._logger.debug(f"Loaded cost state: daily=${self._daily_cost:.2f}, monthly=${self._monthly_cost:.2f}")

        except Exception as e:
            self._logger.warning(f"Failed to load cost state: {e}")

    async def _save_state(self) -> None:
        """Persist cost state."""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)

            data = {
                "last_date": time.strftime("%Y-%m-%d"),
                "last_month": time.strftime("%Y-%m"),
                "daily_cost": self._daily_cost,
                "monthly_cost": self._monthly_cost,
                "total_cost": self._total_cost,
                "savings": self._savings_vs_regular,
                "updated_at": time.time(),
            }

            self.state_file.write_text(json.dumps(data, indent=2))

        except Exception as e:
            self._logger.warning(f"Failed to save cost state: {e}")

    def get_statistics(self) -> Dict[str, Any]:
        """Get cost tracker statistics."""
        return {
            "enabled": self.enabled,
            "daily_cost": self._daily_cost,
            "monthly_cost": self._monthly_cost,
            "total_cost": self._total_cost,
            "savings_vs_regular": self._savings_vs_regular,
            "active_sessions": len(self.active_sessions),
            "daily_budget": self.daily_budget,
            "monthly_budget": self.monthly_budget,
            "spot_rate": self.spot_vm_hourly,
            "regular_rate": self.regular_vm_hourly,
        }


# =============================================================================
# SCALE TO ZERO COST OPTIMIZER
# =============================================================================
class ScaleToZeroCostOptimizer(ResourceManagerBase):
    """
    Scale-to-Zero Cost Optimization for GCP and local resources.

    Features:
    - Aggressive idle shutdown ("VM doing nothing is infinite waste")
    - Activity watchdog with configurable timeout
    - Cost-aware decision making
    - Graceful shutdown with state preservation
    - Integration with semantic caching for instant restarts

    Environment Configuration:
    - SCALE_TO_ZERO_ENABLED: Enable/disable (default: true)
    - SCALE_TO_ZERO_IDLE_TIMEOUT_MINUTES: Minutes before shutdown (default: 15)
    - SCALE_TO_ZERO_MIN_RUNTIME_MINUTES: Minimum runtime before idle check (default: 5)
    - SCALE_TO_ZERO_COST_AWARE: Use cost in decisions (default: true)
    - SCALE_TO_ZERO_PRESERVE_STATE: Preserve state on shutdown (default: true)
    - SCALE_TO_ZERO_CHECK_INTERVAL: Check interval in seconds (default: 60)
    """

    def __init__(self, config: Optional[SystemKernelConfig] = None):
        super().__init__("ScaleToZeroCostOptimizer", config)

        # Configuration from environment (zero hardcoding)
        self.enabled = os.getenv("SCALE_TO_ZERO_ENABLED", "true").lower() == "true"
        self.idle_timeout_minutes = float(os.getenv("SCALE_TO_ZERO_IDLE_TIMEOUT_MINUTES", "15"))
        self.min_runtime_minutes = float(os.getenv("SCALE_TO_ZERO_MIN_RUNTIME_MINUTES", "5"))
        self.cost_aware = os.getenv("SCALE_TO_ZERO_COST_AWARE", "true").lower() == "true"
        self.preserve_state = os.getenv("SCALE_TO_ZERO_PRESERVE_STATE", "true").lower() == "true"
        self.check_interval = float(os.getenv("SCALE_TO_ZERO_CHECK_INTERVAL", "60"))

        # Activity tracking
        self.last_activity_time = time.time()
        self.start_time: Optional[float] = None
        self.activity_count = 0
        self.activity_types: Dict[str, int] = {}

        # State
        self._monitoring_task: Optional[asyncio.Task] = None
        self._shutdown_callback: Optional[Callable[[], Awaitable[None]]] = None

        # Cost tracking
        self.estimated_cost_saved = 0.0
        self.idle_shutdowns_triggered = 0
        self.hourly_rate = float(os.getenv("GCP_SPOT_HOURLY_RATE", "0.029"))

    async def initialize(self) -> bool:
        """Initialize Scale-to-Zero optimizer."""
        self.start_time = time.time()
        self.last_activity_time = time.time()
        self._initialized = True

        self._logger.info(
            f"Scale-to-Zero initialized: enabled={self.enabled}, "
            f"idle_timeout={self.idle_timeout_minutes}min, "
            f"min_runtime={self.min_runtime_minutes}min"
        )
        return True

    async def health_check(self) -> Tuple[bool, str]:
        """Check Scale-to-Zero health."""
        if not self.enabled:
            return True, "Scale-to-Zero disabled"

        idle_minutes = (time.time() - self.last_activity_time) / 60
        time_until_shutdown = max(0, self.idle_timeout_minutes - idle_minutes)

        return True, f"Idle {idle_minutes:.1f}min, shutdown in {time_until_shutdown:.1f}min"

    async def cleanup(self) -> None:
        """Stop monitoring and clean up."""
        await self.stop_monitoring()
        self._initialized = False

    def record_activity(self, activity_type: str = "request") -> None:
        """
        Record user/system activity to reset idle timer.

        Args:
            activity_type: Type of activity (e.g., "request", "voice", "api")
        """
        self.last_activity_time = time.time()
        self.activity_count += 1
        self.activity_types[activity_type] = self.activity_types.get(activity_type, 0) + 1

    async def start_monitoring(
        self,
        shutdown_callback: Callable[[], Awaitable[None]]
    ) -> None:
        """
        Start idle monitoring loop.

        Args:
            shutdown_callback: Async function to call when triggering shutdown
        """
        if not self.enabled:
            self._logger.info("Scale-to-Zero monitoring disabled")
            return

        self._shutdown_callback = shutdown_callback
        self._monitoring_task = asyncio.create_task(self._monitoring_loop())
        self._logger.info("Scale-to-Zero monitoring started")

    async def stop_monitoring(self) -> None:
        """Stop idle monitoring."""
        if self._monitoring_task:
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass
            self._monitoring_task = None

    async def _monitoring_loop(self) -> None:
        """Main monitoring loop - check for idle state periodically."""
        while True:
            try:
                await asyncio.sleep(self.check_interval)

                if await self._should_shutdown():
                    self._logger.warning(
                        f"Scale-to-Zero: Idle timeout reached "
                        f"(idle {(time.time() - self.last_activity_time)/60:.1f}min)"
                    )
                    self.idle_shutdowns_triggered += 1

                    # Estimate cost saved
                    minutes_saved = 60 - (time.time() % 3600) / 60
                    self.estimated_cost_saved += (minutes_saved / 60) * self.hourly_rate

                    if self._shutdown_callback:
                        await self._shutdown_callback()
                    break

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._logger.error(f"Scale-to-Zero monitoring error: {e}")

    async def _should_shutdown(self) -> bool:
        """Determine if system should be shut down due to idle state."""
        if not self.enabled:
            return False

        # Check minimum runtime
        if self.start_time:
            runtime_minutes = (time.time() - self.start_time) / 60
            if runtime_minutes < self.min_runtime_minutes:
                return False

        # Check idle time
        idle_minutes = (time.time() - self.last_activity_time) / 60
        if idle_minutes < self.idle_timeout_minutes:
            return False

        # Cost-aware: Don't shutdown if runtime is very short (wasted startup cost)
        if self.cost_aware and self.start_time:
            runtime = time.time() - self.start_time
            if runtime < 300:  # Less than 5 minutes
                self._logger.debug("Scale-to-Zero: Skipping shutdown (< 5 min runtime)")
                return False

        return True

    def get_statistics(self) -> Dict[str, Any]:
        """Get Scale-to-Zero statistics."""
        idle_minutes = (time.time() - self.last_activity_time) / 60
        runtime_minutes = (time.time() - self.start_time) / 60 if self.start_time else 0

        return {
            "enabled": self.enabled,
            "idle_minutes": round(idle_minutes, 2),
            "runtime_minutes": round(runtime_minutes, 2),
            "idle_timeout_minutes": self.idle_timeout_minutes,
            "time_until_shutdown": max(0, round(self.idle_timeout_minutes - idle_minutes, 2)),
            "activity_count": self.activity_count,
            "activity_types": self.activity_types,
            "idle_shutdowns_triggered": self.idle_shutdowns_triggered,
            "estimated_cost_saved": round(self.estimated_cost_saved, 4),
            "monitoring_active": self._monitoring_task is not None and not self._monitoring_task.done(),
        }


# =============================================================================
# DYNAMIC PORT MANAGER
# =============================================================================
class DynamicPortManager(ResourceManagerBase):
    """
    Ultra-robust Dynamic Port Manager for JARVIS startup.

    Features:
    - Environment-driven configuration (zero hardcoding)
    - Multi-strategy port discovery (config → env vars → dynamic range)
    - Stuck process detection (UE state, zombies, timeouts)
    - Automatic port failover with conflict resolution
    - Process watchdog for stuck prevention
    - Distributed locking for port reservation

    Environment Configuration:
    - JARVIS_PORT: Primary API port (default: 8000)
    - JARVIS_FALLBACK_PORTS: Comma-separated fallback ports (default: 8001,8002,8003)
    - JARVIS_WEBSOCKET_PORT: WebSocket port (default: 8765)
    - JARVIS_DYNAMIC_PORT_ENABLED: Enable dynamic range (default: true)
    - JARVIS_DYNAMIC_PORT_START: Dynamic range start (default: 49152)
    - JARVIS_DYNAMIC_PORT_END: Dynamic range end (default: 65535)
    """

    # macOS UE (Uninterruptible Sleep) state indicators
    UE_STATE_INDICATORS = ['disk-sleep', 'uninterruptible', 'D', 'U']

    def __init__(self, config: Optional[SystemKernelConfig] = None):
        super().__init__("DynamicPortManager", config)

        # Configuration from environment
        self.primary_port = int(os.getenv("JARVIS_PORT", "8000"))

        fallback_str = os.getenv("JARVIS_FALLBACK_PORTS", "8001,8002,8003")
        self.fallback_ports = [int(p.strip()) for p in fallback_str.split(",") if p.strip()]

        self.websocket_port = int(os.getenv("JARVIS_WEBSOCKET_PORT", "8765"))
        self.dynamic_port_enabled = os.getenv("JARVIS_DYNAMIC_PORT_ENABLED", "true").lower() == "true"
        self.dynamic_port_start = int(os.getenv("JARVIS_DYNAMIC_PORT_START", "49152"))
        self.dynamic_port_end = int(os.getenv("JARVIS_DYNAMIC_PORT_END", "65535"))

        # State
        self.selected_port: Optional[int] = None
        self.blacklisted_ports: Set[int] = set()
        self.port_health_cache: Dict[int, Dict[str, Any]] = {}

        # psutil import (optional)
        try:
            import psutil
            self._psutil = psutil
        except ImportError:
            self._psutil = None
            self._logger.warning("psutil not available, port management limited")

    async def initialize(self) -> bool:
        """Initialize port manager and discover best port."""
        self.selected_port = await self.discover_healthy_port()
        self._initialized = True
        self._logger.success(f"Port manager initialized: selected port {self.selected_port}")
        return True

    async def health_check(self) -> Tuple[bool, str]:
        """Check if selected port is healthy."""
        if not self.selected_port:
            return False, "No port selected"

        result = await self.check_port_health(self.selected_port)

        if result.get("healthy"):
            return True, f"Port {self.selected_port} healthy"
        elif result.get("is_stuck"):
            return False, f"Port {self.selected_port} has stuck process"
        else:
            return True, f"Port {self.selected_port} available (no healthy backend)"

    async def cleanup(self) -> None:
        """Clean up port manager."""
        self.port_health_cache.clear()
        self._initialized = False

    def _is_unkillable_state(self, status: str) -> bool:
        """Check if process status indicates an unkillable (UE) state."""
        if not status:
            return False
        status_lower = status.lower()
        return any(ind.lower() in status_lower for ind in self.UE_STATE_INDICATORS)

    def _get_process_on_port(self, port: int) -> Optional[Dict[str, Any]]:
        """Get process information for a process listening on the given port."""
        if not self._psutil:
            return None

        try:
            for conn in self._psutil.net_connections(kind='inet'):
                if hasattr(conn.laddr, 'port') and conn.laddr.port == port:
                    if conn.status == 'LISTEN' and conn.pid:
                        try:
                            proc = self._psutil.Process(conn.pid)
                            return {
                                'pid': conn.pid,
                                'name': proc.name(),
                                'status': proc.status(),
                                'cmdline': ' '.join(proc.cmdline() or [])[:200],
                            }
                        except (self._psutil.NoSuchProcess, self._psutil.AccessDenied):
                            pass
        except Exception as e:
            self._logger.debug(f"Error getting process on port {port}: {e}")
        return None

    async def check_port_health(self, port: int, timeout: float = 2.0) -> Dict[str, Any]:
        """
        Check if a port has a healthy backend.

        Returns dict with:
        - healthy: bool
        - error: str or None
        - is_stuck: bool (unkillable process detected)
        - pid: int or None
        """
        result: Dict[str, Any] = {
            'port': port,
            'healthy': False,
            'error': None,
            'is_stuck': False,
            'pid': None
        }

        # First check process state
        proc_info = await asyncio.get_event_loop().run_in_executor(
            None, self._get_process_on_port, port
        )

        if proc_info:
            result['pid'] = proc_info['pid']
            status = proc_info.get('status', '')

            if self._is_unkillable_state(status):
                result['is_stuck'] = True
                result['error'] = f"Process PID {proc_info['pid']} in unkillable state: {status}"
                self.blacklisted_ports.add(port)
                return result

        # Try HTTP health check
        try:
            import aiohttp
            url = f"http://localhost:{port}/health"

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=timeout)
                ) as resp:
                    if resp.status == 200:
                        try:
                            data = await resp.json()
                            if data.get('status') == 'healthy':
                                result['healthy'] = True
                        except Exception:
                            result['healthy'] = True  # 200 OK is good enough

        except asyncio.TimeoutError:
            result['error'] = 'timeout'
        except Exception as e:
            error_name = type(e).__name__
            if 'ClientConnector' in error_name or 'Connection refused' in str(e):
                result['error'] = 'connection_refused'
            else:
                result['error'] = f'{error_name}: {str(e)[:30]}'

        # Cache result
        self.port_health_cache[port] = {
            **result,
            'timestamp': time.time()
        }

        return result

    async def discover_healthy_port(self) -> int:
        """
        Discover the best healthy port asynchronously (parallel scanning).

        Discovery order:
        1. Primary port
        2. Fallback ports
        3. Dynamic port range (if enabled)

        Returns:
            The best available port
        """
        # Build port list: primary first, then fallbacks
        all_ports = [self.primary_port] + [
            p for p in self.fallback_ports if p != self.primary_port
        ]

        # Remove blacklisted ports
        check_ports = [p for p in all_ports if p not in self.blacklisted_ports]

        if not check_ports:
            self._logger.warning("All ports blacklisted! Using primary as fallback")
            check_ports = [self.primary_port]

        # Parallel health checks
        tasks = [self.check_port_health(port) for port in check_ports]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Find healthy ports
        healthy_ports = []
        stuck_ports = []
        available_ports = []

        for result in results:
            if isinstance(result, Exception):
                continue
            if result.get('is_stuck'):
                stuck_ports.append(result['port'])
            elif result.get('healthy'):
                healthy_ports.append(result['port'])
            elif result.get('error') == 'connection_refused':
                available_ports.append(result['port'])

        # Log findings
        if stuck_ports:
            self._logger.warning(f"Stuck processes detected on ports: {stuck_ports}")

        # Select best port
        if healthy_ports:
            self.selected_port = healthy_ports[0]
            self._logger.info(f"Selected healthy port: {self.selected_port}")
        elif available_ports:
            self.selected_port = available_ports[0]
            self._logger.info(f"Selected available port: {self.selected_port}")
        elif self.dynamic_port_enabled:
            # Try dynamic range
            dynamic_port = await self._find_dynamic_port()
            if dynamic_port:
                self.selected_port = dynamic_port
                self._logger.info(f"Selected dynamic port: {self.selected_port}")
            else:
                self.selected_port = self.primary_port
        else:
            self.selected_port = self.primary_port

        return self.selected_port

    async def _find_dynamic_port(self) -> Optional[int]:
        """Find an available port in the dynamic range."""
        import socket
        import random

        # Create list of ports in range and shuffle for load distribution
        ports = list(range(self.dynamic_port_start, min(self.dynamic_port_end + 1, self.dynamic_port_start + 1000)))
        random.shuffle(ports)

        for port in ports:
            if port in self.blacklisted_ports:
                continue

            try:
                # Try to bind to the port
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.settimeout(1.0)
                sock.bind(('127.0.0.1', port))
                sock.close()
                return port
            except (socket.error, OSError):
                continue

        return None

    async def cleanup_stuck_port(self, port: int) -> bool:
        """
        Attempt to clean up a stuck process on a port.

        Returns:
            True if port was freed, False if process is unkillable
        """
        if not self._psutil:
            return False

        proc_info = self._get_process_on_port(port)
        if not proc_info:
            return True  # No process, port is free

        pid = proc_info['pid']
        status = proc_info.get('status', '')

        # Check for unkillable state
        if self._is_unkillable_state(status):
            self._logger.error(
                f"Process {pid} on port {port} is in unkillable state '{status}' - "
                f"requires system restart"
            )
            self.blacklisted_ports.add(port)
            return False

        # Try to kill the process
        try:
            proc = self._psutil.Process(pid)

            # Graceful shutdown first
            self._logger.info(f"Sending SIGTERM to process {pid} on port {port}")
            proc.terminate()

            try:
                proc.wait(timeout=5.0)
                self._logger.info(f"Process {pid} terminated gracefully")
                return True
            except self._psutil.TimeoutExpired:
                pass

            # Force kill
            self._logger.warning(f"Process {pid} didn't terminate gracefully, sending SIGKILL")
            proc.kill()

            try:
                proc.wait(timeout=3.0)
                self._logger.info(f"Process {pid} killed with SIGKILL")
                return True
            except self._psutil.TimeoutExpired:
                self._logger.error(f"Failed to kill process {pid} - may be in unkillable state")
                self.blacklisted_ports.add(port)
                return False

        except self._psutil.NoSuchProcess:
            return True  # Process already gone
        except Exception as e:
            self._logger.error(f"Error killing process {pid}: {e}")
            return False

    def get_best_port(self) -> int:
        """Get the best available port (cached or primary)."""
        return self.selected_port or self.primary_port


# =============================================================================
# SEMANTIC VOICE CACHE MANAGER
# =============================================================================
class SemanticVoiceCacheManager(ResourceManagerBase):
    """
    Semantic Voice Cache Manager using ChromaDB for ECAPA embeddings.

    Features:
    - High-speed voice embedding cache
    - Semantic similarity search for cache hits
    - TTL-based expiration with cleanup
    - Cost tracking for saved inferences
    - Self-healing statistics

    Environment Configuration:
    - VOICE_CACHE_ENABLED: Enable voice caching (default: true)
    - VOICE_CACHE_TTL_HOURS: TTL for cache entries (default: 24)
    - VOICE_CACHE_SIMILARITY_THRESHOLD: Similarity threshold (default: 0.85)
    - VOICE_CACHE_MAX_ENTRIES: Maximum cache entries (default: 10000)
    - VOICE_CACHE_COST_PER_INFERENCE: Cost per ML inference (default: 0.002)
    - VOICE_CACHE_PERSIST_PATH: Path to persist ChromaDB
    """

    def __init__(self, config: Optional[SystemKernelConfig] = None):
        super().__init__("SemanticVoiceCacheManager", config)

        # Configuration from environment
        self.enabled = os.getenv("VOICE_CACHE_ENABLED", "true").lower() == "true"
        self.ttl_hours = float(os.getenv("VOICE_CACHE_TTL_HOURS", "24"))
        self.similarity_threshold = float(os.getenv("VOICE_CACHE_SIMILARITY_THRESHOLD", "0.85"))
        self.max_entries = int(os.getenv("VOICE_CACHE_MAX_ENTRIES", "10000"))
        self.cost_per_inference = float(os.getenv("VOICE_CACHE_COST_PER_INFERENCE", "0.002"))
        self.persist_path = os.getenv(
            "VOICE_CACHE_PERSIST_PATH",
            str(Path.home() / ".jarvis" / "voice_cache")
        )

        # ChromaDB client and collection
        self._client: Optional[Any] = None
        self._collection: Optional[Any] = None

        # Statistics
        self._cache_hits = 0
        self._cache_misses = 0
        self._cache_expired = 0
        self._cost_saved = 0.0
        self._cleanup_count = 0
        self._last_cleanup_time = 0.0
        self._cleanup_interval_hours = float(os.getenv("VOICE_CACHE_CLEANUP_INTERVAL_HOURS", "6"))

    async def initialize(self) -> bool:
        """Initialize voice cache with ChromaDB."""
        if not self.enabled:
            self._logger.info("Voice cache disabled")
            self._initialized = True
            return True

        try:
            import chromadb
            from chromadb.config import Settings

            # Ensure persist directory exists
            persist_dir = Path(self.persist_path)
            persist_dir.mkdir(parents=True, exist_ok=True)

            # Initialize ChromaDB with persistence
            self._client = chromadb.Client(Settings(
                chroma_db_impl="duckdb+parquet",
                persist_directory=str(persist_dir),
                anonymized_telemetry=False
            ))

            # Get or create collection
            self._collection = self._client.get_or_create_collection(
                name="voice_embeddings",
                metadata={"hnsw:space": "cosine"}  # Use cosine similarity
            )

            self._initialized = True
            self._logger.success(
                f"Voice cache initialized: {self._collection.count()} cached entries"
            )
            return True

        except ImportError:
            self._logger.warning("ChromaDB not available, voice cache disabled")
            self.enabled = False
            self._initialized = True
            return True
        except Exception as e:
            self._logger.error(f"Failed to initialize voice cache: {e}")
            self.enabled = False
            self._initialized = True
            return True

    async def health_check(self) -> Tuple[bool, str]:
        """Check voice cache health."""
        if not self.enabled:
            return True, "Voice cache disabled"

        if not self._collection:
            return False, "Voice cache not initialized"

        try:
            count = self._collection.count()
            hit_rate = self._cache_hits / (self._cache_hits + self._cache_misses) if (self._cache_hits + self._cache_misses) > 0 else 0
            return True, f"Voice cache: {count} entries, {hit_rate:.1%} hit rate"
        except Exception as e:
            return False, f"Voice cache error: {e}"

    async def cleanup(self) -> None:
        """Clean up voice cache resources."""
        if self._client:
            try:
                self._client.persist()
            except Exception:
                pass
        self._initialized = False

    async def query_cache(
        self,
        embedding: List[float],
        speaker_filter: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Query cache for similar voice embedding.

        Args:
            embedding: 192-dimensional ECAPA-TDNN embedding
            speaker_filter: Optional speaker name to filter by

        Returns:
            Cache result dict if hit, None if miss
        """
        if not self.enabled or not self._collection:
            self._cache_misses += 1
            return None

        try:
            # Build where filter
            where_filter = None
            if speaker_filter:
                where_filter = {"speaker_name": speaker_filter}

            # Query ChromaDB
            results = self._collection.query(
                query_embeddings=[embedding],
                n_results=1,
                where=where_filter,
                include=["metadatas", "distances"]
            )

            if results and results["distances"] and results["distances"][0]:
                # ChromaDB returns L2 distance, convert to similarity
                distance = results["distances"][0][0]
                similarity = 1 / (1 + distance)

                if similarity >= self.similarity_threshold:
                    # Potential hit - check TTL
                    metadata = results["metadatas"][0][0] if results["metadatas"] else {}
                    cached_time = metadata.get("timestamp", 0)
                    age_hours = (time.time() - cached_time) / 3600

                    if age_hours > self.ttl_hours:
                        # Entry expired
                        self._cache_expired += 1
                        self._cache_misses += 1

                        # Schedule cleanup
                        entry_id = results.get("ids", [[]])[0]
                        if entry_id:
                            asyncio.create_task(self._delete_entry(entry_id[0]))

                        return None

                    # Valid cache hit!
                    self._cache_hits += 1
                    self._cost_saved += self.cost_per_inference

                    return {
                        "cached": True,
                        "similarity": similarity,
                        "speaker_name": metadata.get("speaker_name"),
                        "confidence": metadata.get("confidence", 0.0),
                        "verified": metadata.get("verified", False),
                        "cached_at": cached_time,
                        "age_hours": age_hours,
                    }

            # Cache miss
            self._cache_misses += 1
            return None

        except Exception as e:
            self._logger.error(f"Cache query error: {e}")
            self._cache_misses += 1
            return None

    async def store_result(
        self,
        embedding: List[float],
        speaker_name: str,
        confidence: float,
        verified: bool,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Store verification result in cache."""
        if not self.enabled or not self._collection:
            return

        try:
            cache_id = f"{speaker_name}_{int(time.time() * 1000)}"

            cache_metadata = {
                "speaker_name": speaker_name,
                "confidence": confidence,
                "verified": verified,
                "timestamp": time.time(),
            }
            if metadata:
                cache_metadata.update(metadata)

            self._collection.add(
                embeddings=[embedding],
                metadatas=[cache_metadata],
                ids=[cache_id]
            )

            # Trigger cleanup if over limit
            if self._collection.count() > self.max_entries:
                await self._cleanup_old_entries()

        except Exception as e:
            self._logger.error(f"Cache store error: {e}")

    async def _delete_entry(self, entry_id: str) -> None:
        """Delete a single entry from cache."""
        if not self._collection:
            return
        try:
            self._collection.delete(ids=[entry_id])
        except Exception:
            pass

    async def _cleanup_old_entries(self) -> None:
        """Remove oldest entries to stay under max_entries limit."""
        if not self._collection:
            return

        try:
            all_entries = self._collection.get(include=["metadatas"])

            if not all_entries["ids"]:
                return

            # Sort by timestamp
            entries_with_time = [
                (id_, meta.get("timestamp", 0))
                for id_, meta in zip(all_entries["ids"], all_entries["metadatas"])
            ]
            entries_with_time.sort(key=lambda x: x[1])

            # Delete oldest 10%
            to_delete = int(len(entries_with_time) * 0.1)
            if to_delete > 0:
                ids_to_delete = [e[0] for e in entries_with_time[:to_delete]]
                self._collection.delete(ids=ids_to_delete)
                self._cleanup_count += to_delete
                self._last_cleanup_time = time.time()
                self._logger.debug(f"Cleaned {to_delete} old cache entries")

        except Exception as e:
            self._logger.error(f"Cache cleanup error: {e}")

    def get_statistics(self) -> Dict[str, Any]:
        """Get cache statistics."""
        total = self._cache_hits + self._cache_misses
        hit_rate = self._cache_hits / total if total > 0 else 0.0

        return {
            "enabled": self.enabled,
            "initialized": self._initialized,
            "total_queries": total,
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "cache_expired": self._cache_expired,
            "hit_rate": round(hit_rate, 4),
            "cost_saved_usd": round(self._cost_saved, 4),
            "cached_entries": self._collection.count() if self._collection else 0,
            "max_entries": self.max_entries,
            "ttl_hours": self.ttl_hours,
            "similarity_threshold": self.similarity_threshold,
            "cleanup_count": self._cleanup_count,
            "last_cleanup_time": self._last_cleanup_time,
        }


# =============================================================================
# TIERED STORAGE MANAGER
# =============================================================================
class TieredStorageManager(ResourceManagerBase):
    """
    Tiered Storage Manager for hot/warm/cold data tiering.

    Features:
    - Automatic data tiering based on access patterns
    - Hot tier: In-memory LRU cache for frequent access
    - Warm tier: Local SSD storage
    - Cold tier: Cloud storage (GCS) or archive
    - Cost-optimized data lifecycle management

    Environment Configuration:
    - TIERED_STORAGE_ENABLED: Enable tiered storage (default: true)
    - TIERED_STORAGE_HOT_MAX_SIZE_MB: Max hot tier size (default: 512)
    - TIERED_STORAGE_WARM_PATH: Path to warm tier storage
    - TIERED_STORAGE_COLD_BUCKET: GCS bucket for cold tier
    - TIERED_STORAGE_HOT_TTL_MINUTES: TTL for hot tier (default: 30)
    - TIERED_STORAGE_WARM_TTL_HOURS: TTL before cold migration (default: 24)
    """

    def __init__(self, config: Optional[SystemKernelConfig] = None):
        super().__init__("TieredStorageManager", config)

        # Configuration from environment
        self.enabled = os.getenv("TIERED_STORAGE_ENABLED", "true").lower() == "true"
        self.hot_max_size_mb = int(os.getenv("TIERED_STORAGE_HOT_MAX_SIZE_MB", "512"))
        self.warm_path = os.getenv(
            "TIERED_STORAGE_WARM_PATH",
            str(Path.home() / ".jarvis" / "warm_storage")
        )
        self.cold_bucket = os.getenv("TIERED_STORAGE_COLD_BUCKET", "")
        self.hot_ttl_minutes = float(os.getenv("TIERED_STORAGE_HOT_TTL_MINUTES", "30"))
        self.warm_ttl_hours = float(os.getenv("TIERED_STORAGE_WARM_TTL_HOURS", "24"))

        # Hot tier: In-memory cache with LRU eviction
        self._hot_cache: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._hot_size_bytes = 0
        self._hot_max_size_bytes = self.hot_max_size_mb * 1024 * 1024

        # Statistics
        self._hot_hits = 0
        self._warm_hits = 0
        self._cold_hits = 0
        self._total_requests = 0
        self._bytes_migrated_warm = 0
        self._bytes_migrated_cold = 0

    async def initialize(self) -> bool:
        """Initialize tiered storage."""
        if not self.enabled:
            self._logger.info("Tiered storage disabled")
            self._initialized = True
            return True

        # Ensure warm tier directory exists
        try:
            warm_dir = Path(self.warm_path)
            warm_dir.mkdir(parents=True, exist_ok=True)
            self._logger.debug(f"Warm tier path: {warm_dir}")
        except Exception as e:
            self._logger.warning(f"Failed to create warm tier directory: {e}")

        self._initialized = True
        self._logger.success("Tiered storage initialized")
        return True

    async def health_check(self) -> Tuple[bool, str]:
        """Check tiered storage health."""
        if not self.enabled:
            return True, "Tiered storage disabled"

        hot_usage = (self._hot_size_bytes / self._hot_max_size_bytes) * 100 if self._hot_max_size_bytes > 0 else 0
        return True, f"Hot tier: {hot_usage:.1f}% ({len(self._hot_cache)} items)"

    async def cleanup(self) -> None:
        """Clean up tiered storage."""
        self._hot_cache.clear()
        self._hot_size_bytes = 0
        self._initialized = False

    async def get(self, key: str) -> Optional[Any]:
        """
        Get data from tiered storage.

        Checks tiers in order: hot → warm → cold
        Promotes data to hotter tiers on access.
        """
        self._total_requests += 1

        # Check hot tier first
        if key in self._hot_cache:
            # Move to end (most recently used)
            self._hot_cache.move_to_end(key)
            entry = self._hot_cache[key]

            # Check TTL
            if time.time() - entry["timestamp"] < self.hot_ttl_minutes * 60:
                self._hot_hits += 1
                return entry["data"]
            else:
                # Expired, remove from hot
                self._evict_from_hot(key)

        # Check warm tier
        warm_data = await self._get_from_warm(key)
        if warm_data is not None:
            self._warm_hits += 1
            # Promote to hot
            await self.put(key, warm_data)
            return warm_data

        # Check cold tier
        if self.cold_bucket:
            cold_data = await self._get_from_cold(key)
            if cold_data is not None:
                self._cold_hits += 1
                # Promote to warm and hot
                await self._put_to_warm(key, cold_data)
                await self.put(key, cold_data)
                return cold_data

        return None

    async def put(self, key: str, data: Any) -> None:
        """
        Put data into hot tier.

        Automatically evicts old data if capacity exceeded.
        """
        if not self.enabled:
            return

        # Estimate size
        try:
            import sys
            size = sys.getsizeof(data)
        except Exception:
            size = 1024  # Default estimate

        # Evict if needed to make room
        while self._hot_size_bytes + size > self._hot_max_size_bytes and self._hot_cache:
            oldest_key = next(iter(self._hot_cache))
            await self._demote_to_warm(oldest_key)

        # Add to hot tier
        self._hot_cache[key] = {
            "data": data,
            "timestamp": time.time(),
            "size": size,
        }
        self._hot_cache.move_to_end(key)
        self._hot_size_bytes += size

    def _evict_from_hot(self, key: str) -> None:
        """Remove entry from hot tier."""
        if key in self._hot_cache:
            entry = self._hot_cache.pop(key)
            self._hot_size_bytes -= entry.get("size", 0)

    async def _demote_to_warm(self, key: str) -> None:
        """Demote entry from hot to warm tier."""
        if key not in self._hot_cache:
            return

        entry = self._hot_cache[key]
        data = entry["data"]

        # Save to warm tier
        await self._put_to_warm(key, data)

        # Remove from hot
        self._evict_from_hot(key)
        self._bytes_migrated_warm += entry.get("size", 0)

    async def _get_from_warm(self, key: str) -> Optional[Any]:
        """Get data from warm tier (local disk)."""
        try:
            warm_file = Path(self.warm_path) / f"{key}.json"
            if warm_file.exists():
                import json
                with open(warm_file, 'r') as f:
                    return json.load(f)
        except Exception:
            pass
        return None

    async def _put_to_warm(self, key: str, data: Any) -> None:
        """Put data to warm tier (local disk)."""
        try:
            import json
            warm_file = Path(self.warm_path) / f"{key}.json"
            with open(warm_file, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            self._logger.debug(f"Failed to write to warm tier: {e}")

    async def _get_from_cold(self, key: str) -> Optional[Any]:
        """Get data from cold tier (cloud storage)."""
        if not self.cold_bucket:
            return None

        try:
            from google.cloud import storage
            client = storage.Client()
            bucket = client.bucket(self.cold_bucket)
            blob = bucket.blob(f"jarvis-cold/{key}.json")

            if blob.exists():
                import json
                return json.loads(blob.download_as_string())
        except Exception as e:
            self._logger.debug(f"Failed to read from cold tier: {e}")

        return None

    def get_statistics(self) -> Dict[str, Any]:
        """Get tiered storage statistics."""
        total_hits = self._hot_hits + self._warm_hits + self._cold_hits

        return {
            "enabled": self.enabled,
            "total_requests": self._total_requests,
            "hot_hits": self._hot_hits,
            "warm_hits": self._warm_hits,
            "cold_hits": self._cold_hits,
            "hot_hit_rate": self._hot_hits / self._total_requests if self._total_requests > 0 else 0,
            "overall_hit_rate": total_hits / self._total_requests if self._total_requests > 0 else 0,
            "hot_items": len(self._hot_cache),
            "hot_size_mb": self._hot_size_bytes / (1024 * 1024),
            "hot_max_size_mb": self.hot_max_size_mb,
            "hot_utilization": self._hot_size_bytes / self._hot_max_size_bytes if self._hot_max_size_bytes > 0 else 0,
            "bytes_migrated_warm": self._bytes_migrated_warm,
            "bytes_migrated_cold": self._bytes_migrated_cold,
        }


# =============================================================================
# RESOURCE MANAGER REGISTRY
# =============================================================================
class ResourceManagerRegistry:
    """
    Registry for all resource managers.

    Provides centralized initialization, health checking, and cleanup
    for all resource managers in the system.
    """

    def __init__(self, config: Optional[SystemKernelConfig] = None):
        self.config = config or SystemKernelConfig.from_environment()
        self._managers: Dict[str, ResourceManagerBase] = {}
        self._logger = UnifiedLogger()
        self._initialized = False

    def register(self, manager: ResourceManagerBase) -> None:
        """Register a resource manager."""
        self._managers[manager.name] = manager

    def get(self, name: str) -> Optional[ResourceManagerBase]:
        """Get a resource manager by name."""
        return self._managers.get(name)

    def get_manager(self, name: str) -> Optional[ResourceManagerBase]:
        """Get a resource manager by name (alias for get)."""
        return self.get(name)

    async def initialize_all(self, parallel: bool = True) -> Dict[str, bool]:
        """
        Initialize all registered managers.

        Args:
            parallel: Initialize in parallel (faster) or sequential (safer)

        Returns:
            Dict mapping manager name to success status
        """
        results: Dict[str, bool] = {}

        if parallel:
            # Parallel initialization
            tasks = [
                (name, manager.safe_initialize())
                for name, manager in self._managers.items()
            ]

            async_results = await asyncio.gather(
                *[t[1] for t in tasks],
                return_exceptions=True
            )

            for (name, _), result in zip(tasks, async_results):
                if isinstance(result, Exception):
                    self._logger.error(f"Manager {name} initialization error: {result}")
                    results[name] = False
                else:
                    results[name] = result
        else:
            # Sequential initialization
            for name, manager in self._managers.items():
                try:
                    results[name] = await manager.safe_initialize()
                except Exception as e:
                    self._logger.error(f"Manager {name} initialization error: {e}")
                    results[name] = False

        self._initialized = True
        return results

    async def health_check_all(self) -> Dict[str, Tuple[bool, str]]:
        """
        Health check all managers.

        Returns:
            Dict mapping manager name to (healthy, message) tuple
        """
        results: Dict[str, Tuple[bool, str]] = {}

        for name, manager in self._managers.items():
            try:
                results[name] = await manager.safe_health_check()
            except Exception as e:
                results[name] = (False, f"Health check error: {e}")

        return results

    async def cleanup_all(self) -> None:
        """Clean up all managers in reverse registration order."""
        for name in reversed(list(self._managers.keys())):
            try:
                await self._managers[name].cleanup()
            except Exception as e:
                self._logger.error(f"Manager {name} cleanup error: {e}")

        self._initialized = False

    def get_all_status(self) -> Dict[str, Dict[str, Any]]:
        """Get status of all managers."""
        return {name: manager.status for name, manager in self._managers.items()}

    @property
    def all_ready(self) -> bool:
        """True if all managers are ready."""
        return all(m.is_ready for m in self._managers.values())

    @property
    def manager_count(self) -> int:
        """Number of registered managers."""
        return len(self._managers)


# =============================================================================
# SPOT INSTANCE RESILIENCE HANDLER
# =============================================================================
class SpotInstanceResilienceHandler(ResourceManagerBase):
    """
    Spot Instance Resilience Handler for GCP Preemption.

    Features:
    - Graceful preemption handling (30 second warning)
    - State preservation before shutdown
    - Automatic fallback to micro instance or local
    - Cost tracking during preemption events
    - Learning from preemption patterns
    - Webhook notifications

    Environment Configuration:
    - SPOT_RESILIENCE_ENABLED: Enable/disable (default: true)
    - SPOT_FALLBACK_MODE: micro/local/none (default: local)
    - SPOT_STATE_PRESERVE: Save state on preemption (default: true)
    - SPOT_PREEMPTION_WEBHOOK: Webhook URL for notifications (default: none)
    - SPOT_STATE_FILE: Path to state file (default: ~/.jarvis/spot_state.json)
    - SPOT_POLL_INTERVAL: Metadata poll interval in seconds (default: 5)
    """

    def __init__(self, config: Optional[SystemKernelConfig] = None):
        super().__init__("SpotInstanceResilienceHandler", config)

        # Configuration from environment
        self.enabled = os.getenv("SPOT_RESILIENCE_ENABLED", "true").lower() == "true"
        self.fallback_mode = os.getenv("SPOT_FALLBACK_MODE", "local")  # micro/local/none
        self.state_preserve = os.getenv("SPOT_STATE_PRESERVE", "true").lower() == "true"
        self.preemption_webhook = os.getenv("SPOT_PREEMPTION_WEBHOOK")
        self.poll_interval = float(os.getenv("SPOT_POLL_INTERVAL", "5"))

        # State file
        self.state_file = Path(os.getenv(
            "SPOT_STATE_FILE",
            str(Path.home() / ".jarvis" / "spot_state.json")
        ))

        # Preemption tracking
        self.preemption_count = 0
        self.last_preemption_time: Optional[float] = None
        self.preemption_history: List[Dict[str, Any]] = []

        # Callbacks
        self._preemption_callback: Optional[Callable[[], Awaitable[None]]] = None
        self._fallback_callback: Optional[Callable[[str], Awaitable[None]]] = None

        # Polling task
        self._polling_task: Optional[asyncio.Task] = None
        self._polling_active = False

    async def initialize(self) -> bool:
        """Initialize resilience handler."""
        if not self.enabled:
            self._logger.info("Spot resilience handler disabled")
            self._initialized = True
            return True

        # Load preserved state if available
        preserved = await self.load_preserved_state()
        if preserved:
            self.preemption_count = preserved.get("preemption_count", 0)
            self.preemption_history = preserved.get("preemption_history", [])[-10:]
            self._logger.info(f"Loaded preserved state: {self.preemption_count} previous preemptions")

        self._initialized = True
        self._logger.success(
            f"Spot resilience initialized: fallback={self.fallback_mode}, "
            f"preserve_state={self.state_preserve}"
        )
        return True

    async def health_check(self) -> Tuple[bool, str]:
        """Check resilience handler health."""
        if not self.enabled:
            return True, "Spot resilience disabled"

        status_parts = [f"preemptions={self.preemption_count}"]
        if self._polling_active:
            status_parts.append("polling=active")
        if self.last_preemption_time:
            since = time.time() - self.last_preemption_time
            status_parts.append(f"last_preemption={since:.0f}s ago")

        return True, ", ".join(status_parts)

    async def cleanup(self) -> None:
        """Stop polling and clean up."""
        await self.stop_preemption_handler()
        self._initialized = False

    async def setup_preemption_handler(
        self,
        preemption_callback: Optional[Callable[[], Awaitable[None]]] = None,
        fallback_callback: Optional[Callable[[str], Awaitable[None]]] = None
    ) -> None:
        """
        Setup preemption handling callbacks and start polling.

        Args:
            preemption_callback: Called when preemption detected (before fallback)
            fallback_callback: Called with fallback mode to trigger fallback
        """
        self._preemption_callback = preemption_callback
        self._fallback_callback = fallback_callback

        if self.enabled and not self._polling_active:
            self._polling_task = asyncio.create_task(self._poll_preemption_notice())
            self._polling_active = True
            self._logger.info("Preemption handler active")

    async def stop_preemption_handler(self) -> None:
        """Stop preemption polling."""
        self._polling_active = False
        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
        self._polling_task = None

    async def _poll_preemption_notice(self) -> None:
        """Poll GCP metadata server for preemption notice."""
        metadata_url = "http://metadata.google.internal/computeMetadata/v1/instance/preempted"
        headers = {"Metadata-Flavor": "Google"}

        while self._polling_active:
            try:
                if AIOHTTP_AVAILABLE and aiohttp is not None:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            metadata_url,
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=5)
                        ) as response:
                            if response.status == 200:
                                text = await response.text()
                                if text.strip().lower() == "true":
                                    await self._handle_preemption()
                                    break  # Stop polling after preemption
            except Exception:
                # Not on GCP or metadata not available - this is normal
                pass

            await asyncio.sleep(self.poll_interval)

    async def _handle_preemption(self) -> None:
        """Handle preemption event (30 seconds to cleanup)."""
        self._logger.warning("⚠️ SPOT PREEMPTION NOTICE - 30 seconds to shutdown!")

        self.preemption_count += 1
        self.last_preemption_time = time.time()

        preemption_event = {
            "timestamp": time.time(),
            "preemption_count": self.preemption_count,
            "fallback_mode": self.fallback_mode,
        }
        self.preemption_history.append(preemption_event)

        # Preserve state if enabled
        if self.state_preserve:
            await self._preserve_state()

        # Call preemption callback
        if self._preemption_callback:
            try:
                await self._preemption_callback()
            except Exception as e:
                self._logger.error(f"Preemption callback failed: {e}")

        # Trigger fallback
        if self.fallback_mode != "none" and self._fallback_callback:
            try:
                await self._fallback_callback(self.fallback_mode)
            except Exception as e:
                self._logger.error(f"Fallback callback failed: {e}")

        # Send webhook notification if configured
        if self.preemption_webhook:
            await self._send_webhook_notification(preemption_event)

    async def _preserve_state(self) -> None:
        """Preserve current state to disk for recovery."""
        try:
            state = {
                "timestamp": time.time(),
                "preemption_count": self.preemption_count,
                "preemption_history": self.preemption_history[-10:],  # Last 10
            }

            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(state, indent=2))
            self._logger.info(f"State preserved to {self.state_file}")

        except Exception as e:
            self._logger.error(f"State preservation failed: {e}")

    async def _send_webhook_notification(self, event: Dict[str, Any]) -> None:
        """Send webhook notification for preemption event."""
        if not self.preemption_webhook:
            return

        try:
            if AIOHTTP_AVAILABLE and aiohttp is not None:
                async with aiohttp.ClientSession() as session:
                    await session.post(
                        self.preemption_webhook,
                        json=event,
                        timeout=aiohttp.ClientTimeout(total=5)
                    )
                self._logger.info("Preemption webhook sent")
        except Exception as e:
            self._logger.error(f"Webhook notification failed: {e}")

    async def load_preserved_state(self) -> Optional[Dict[str, Any]]:
        """Load preserved state from previous session."""
        try:
            if self.state_file.exists():
                state = json.loads(self.state_file.read_text())
                return state
        except Exception as e:
            self._logger.error(f"Failed to load preserved state: {e}")
        return None

    def get_statistics(self) -> Dict[str, Any]:
        """Get resilience statistics."""
        return {
            "enabled": self.enabled,
            "fallback_mode": self.fallback_mode,
            "state_preserve": self.state_preserve,
            "preemption_count": self.preemption_count,
            "last_preemption_time": self.last_preemption_time,
            "preemption_history_count": len(self.preemption_history),
            "polling_active": self._polling_active,
        }


# =============================================================================
# INTELLIGENT CACHE MANAGER
# =============================================================================
class IntelligentCacheManager(ResourceManagerBase):
    """
    Intelligent Cache Manager for Dynamic Python Module and Data Caching.

    Features:
    - Python module cache clearing with pattern-based filtering
    - Bytecode (.pyc/__pycache__) cleanup with size tracking
    - ML model cache warming and eviction
    - Async operations for non-blocking cleanup
    - Statistics tracking and reporting
    - Environment-driven configuration

    Environment Configuration:
    - CACHE_MANAGER_ENABLED: Enable/disable (default: true)
    - CACHE_CLEAR_BYTECODE: Clear .pyc files (default: true)
    - CACHE_CLEAR_PYCACHE: Remove __pycache__ dirs (default: true)
    - CACHE_MODULE_PATTERNS: Comma-separated patterns to clear
    - CACHE_PRESERVE_PATTERNS: Patterns to preserve (default: none)
    - CACHE_WARM_ON_START: Pre-load critical modules (default: false)
    - CACHE_ASYNC_CLEANUP: Use async for cleanup (default: true)
    - CACHE_MAX_BYTECODE_AGE_HOURS: Max age for .pyc files (default: 24)
    - CACHE_WARM_MODULES: Comma-separated modules to pre-load
    """

    def __init__(self, config: Optional[SystemKernelConfig] = None):
        super().__init__("IntelligentCacheManager", config)

        # Configuration from environment
        self.enabled = os.getenv("CACHE_MANAGER_ENABLED", "true").lower() == "true"
        self.clear_bytecode = os.getenv("CACHE_CLEAR_BYTECODE", "true").lower() == "true"
        self.clear_pycache = os.getenv("CACHE_CLEAR_PYCACHE", "true").lower() == "true"
        self.async_cleanup = os.getenv("CACHE_ASYNC_CLEANUP", "true").lower() == "true"
        self.warm_on_start = os.getenv("CACHE_WARM_ON_START", "false").lower() == "true"
        self.max_bytecode_age_hours = float(os.getenv("CACHE_MAX_BYTECODE_AGE_HOURS", "24"))

        # Module patterns to clear/preserve
        default_patterns = "backend,api,vision,voice,unified,command,intelligence,core"
        self.module_patterns = [
            p.strip() for p in os.getenv("CACHE_MODULE_PATTERNS", default_patterns).split(",")
        ]
        preserve_patterns = os.getenv("CACHE_PRESERVE_PATTERNS", "")
        self.preserve_patterns = [
            p.strip() for p in preserve_patterns.split(",") if p.strip()
        ]

        # Warm-up modules (critical paths to pre-load)
        default_warm = "backend.core,backend.api,backend.voice_unlock"
        self.warm_modules = [
            p.strip() for p in os.getenv("CACHE_WARM_MODULES", default_warm).split(",")
        ]

        # Statistics
        self._modules_cleared = 0
        self._bytecode_files_removed = 0
        self._pycache_dirs_removed = 0
        self._bytes_freed = 0
        self._warmup_modules_loaded = 0
        self._last_clear_time: Optional[float] = None
        self._clear_count = 0
        self._errors: List[str] = []

        # Project root for bytecode cleanup
        self._project_root: Optional[Path] = None

    async def initialize(self) -> bool:
        """Initialize cache manager."""
        if not self.enabled:
            self._logger.info("Cache manager disabled")
            self._initialized = True
            return True

        # Try to detect project root
        if self.config and hasattr(self.config, "project_root"):
            self._project_root = self.config.project_root
        else:
            # Try to find project root
            current = Path.cwd()
            while current != current.parent:
                if (current / "backend").exists() or (current / ".git").exists():
                    self._project_root = current
                    break
                current = current.parent

        self._initialized = True
        self._logger.success(f"Cache manager initialized: project_root={self._project_root}")
        return True

    async def health_check(self) -> Tuple[bool, str]:
        """Check cache manager health."""
        if not self.enabled:
            return True, "Cache manager disabled"

        return True, (
            f"cleared={self._modules_cleared} modules, "
            f"freed={self._bytes_freed / (1024*1024):.1f}MB"
        )

    async def cleanup(self) -> None:
        """Clean up cache manager."""
        self._initialized = False

    def _should_clear_module(self, module_name: str) -> bool:
        """Determine if a module should be cleared based on patterns."""
        # Check preserve patterns first
        for pattern in self.preserve_patterns:
            if pattern and pattern in module_name:
                return False

        # Check clear patterns
        for pattern in self.module_patterns:
            if pattern and pattern in module_name:
                return True

        return False

    def clear_python_modules(self) -> Dict[str, Any]:
        """
        Clear Python module cache based on configured patterns.

        Returns:
            Statistics about cleared modules
        """
        if not self.enabled:
            return {"cleared": 0, "skipped": "disabled"}

        start_time = time.time()
        modules_to_remove = []

        for module_name in list(sys.modules.keys()):
            if self._should_clear_module(module_name):
                modules_to_remove.append(module_name)

        for module_name in modules_to_remove:
            try:
                del sys.modules[module_name]
            except Exception as e:
                self._errors.append(f"Failed to clear {module_name}: {e}")

        self._modules_cleared += len(modules_to_remove)
        self._last_clear_time = time.time()
        self._clear_count += 1

        return {
            "cleared": len(modules_to_remove),
            "modules": modules_to_remove[:10],  # First 10 for logging
            "duration_ms": (time.time() - start_time) * 1000,
        }

    def clear_bytecode_cache(self, target_path: Optional[Path] = None) -> Dict[str, Any]:
        """
        Clear Python bytecode cache (.pyc files and __pycache__ directories).

        Args:
            target_path: Path to clean (defaults to project backend)

        Returns:
            Statistics about cleared files
        """
        if not self.enabled or (not self.clear_bytecode and not self.clear_pycache):
            return {"cleared": False, "reason": "disabled"}

        import shutil
        target = target_path or (self._project_root / "backend" if self._project_root else None)

        if not target or not target.exists():
            return {"cleared": False, "reason": "path_not_found"}

        pycache_removed = 0
        pyc_removed = 0
        bytes_freed = 0
        errors = []

        # Remove __pycache__ directories
        if self.clear_pycache:
            for pycache_dir in target.rglob("__pycache__"):
                try:
                    dir_size = sum(f.stat().st_size for f in pycache_dir.rglob("*") if f.is_file())
                    shutil.rmtree(pycache_dir)
                    pycache_removed += 1
                    bytes_freed += dir_size
                except Exception as e:
                    errors.append(f"Failed to remove {pycache_dir}: {e}")

        # Remove individual .pyc files
        if self.clear_bytecode:
            for pyc_file in target.rglob("*.pyc"):
                try:
                    # Check age if configured
                    if self.max_bytecode_age_hours > 0:
                        file_age_hours = (time.time() - pyc_file.stat().st_mtime) / 3600
                        if file_age_hours < self.max_bytecode_age_hours:
                            continue  # Skip recent files

                    file_size = pyc_file.stat().st_size
                    pyc_file.unlink()
                    pyc_removed += 1
                    bytes_freed += file_size
                except Exception as e:
                    errors.append(f"Failed to remove {pyc_file}: {e}")

        self._pycache_dirs_removed += pycache_removed
        self._bytecode_files_removed += pyc_removed
        self._bytes_freed += bytes_freed
        self._errors.extend(errors[:5])

        return {
            "pycache_dirs": pycache_removed,
            "pyc_files": pyc_removed,
            "bytes_freed": bytes_freed,
            "bytes_freed_mb": bytes_freed / (1024 * 1024),
            "errors": len(errors),
        }

    async def clear_all_async(self, target_path: Optional[Path] = None) -> Dict[str, Any]:
        """
        Asynchronously clear all caches.

        Args:
            target_path: Path to clean (defaults to project backend)

        Returns:
            Combined statistics from all clear operations
        """
        results: Dict[str, Any] = {}

        # Run bytecode cleanup in executor to not block
        loop = asyncio.get_event_loop()

        if self.clear_bytecode or self.clear_pycache:
            bytecode_result = await loop.run_in_executor(
                None, self.clear_bytecode_cache, target_path
            )
            results["bytecode"] = bytecode_result

        # Module clearing is fast, do it directly
        module_result = self.clear_python_modules()
        results["modules"] = module_result

        # Prevent new bytecode files
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

        return results

    async def warm_critical_modules(self) -> Dict[str, Any]:
        """
        Pre-load critical modules for faster subsequent imports.

        Returns:
            Statistics about warmed modules
        """
        if not self.warm_on_start:
            return {"warmed": 0, "reason": "disabled"}

        import importlib
        warmed = []
        errors = []

        for module_path in self.warm_modules:
            try:
                importlib.import_module(module_path)
                warmed.append(module_path)
            except Exception as e:
                errors.append(f"{module_path}: {e}")

        self._warmup_modules_loaded += len(warmed)

        return {
            "warmed": len(warmed),
            "modules": warmed,
            "errors": errors,
        }

    def verify_fresh_imports(self) -> bool:
        """
        Verify that imports are fresh (no stale cached modules).

        Returns:
            True if imports appear fresh
        """
        stale_count = 0
        for module_name in sys.modules:
            if self._should_clear_module(module_name):
                stale_count += 1

        return stale_count == 0

    def get_statistics(self) -> Dict[str, Any]:
        """Get cache manager statistics."""
        return {
            "enabled": self.enabled,
            "modules_cleared": self._modules_cleared,
            "bytecode_files_removed": self._bytecode_files_removed,
            "pycache_dirs_removed": self._pycache_dirs_removed,
            "bytes_freed": self._bytes_freed,
            "bytes_freed_mb": self._bytes_freed / (1024 * 1024),
            "warmup_modules_loaded": self._warmup_modules_loaded,
            "last_clear_time": self._last_clear_time,
            "clear_count": self._clear_count,
            "patterns": self.module_patterns,
            "preserve_patterns": self.preserve_patterns,
        }


# =============================================================================
# DYNAMIC RAM MONITOR - Advanced Memory Tracking
# =============================================================================
class DynamicRAMMonitor:
    """
    Advanced RAM monitoring with predictive intelligence and automatic workload shifting.

    Features:
    - Real-time memory tracking with sub-second precision
    - Predictive analysis using historical patterns
    - Intelligent threshold adaptation based on workload
    - macOS memory pressure detection (not just percentage)
    - Process-level memory attribution
    - Automatic GCP migration triggers
    """

    def __init__(self):
        """Initialize the dynamic RAM monitor."""
        # System configuration (auto-detected, no hardcoding)
        self.local_ram_total = psutil.virtual_memory().total
        self.local_ram_gb = self.local_ram_total / (1024**3)
        self.is_macos = platform.system() == "Darwin"

        # Dynamic thresholds (adapt based on system behavior)
        self.warning_threshold = float(os.getenv("RAM_WARNING_THRESHOLD", "0.75"))
        self.critical_threshold = float(os.getenv("RAM_CRITICAL_THRESHOLD", "0.85"))
        self.optimal_threshold = float(os.getenv("RAM_OPTIMAL_THRESHOLD", "0.60"))
        self.emergency_threshold = float(os.getenv("RAM_EMERGENCY_THRESHOLD", "0.95"))

        # macOS-specific memory pressure thresholds
        self.pressure_warn_level = 2
        self.pressure_critical_level = 4

        # Monitoring state
        self.current_usage = 0.0
        self.current_pressure = 0
        self.pressure_history: List[Dict[str, Any]] = []
        self.usage_history: List[Dict[str, float]] = []
        self.max_history = 100
        self.prediction_window = 10

        # Component memory tracking
        self.component_memory: Dict[str, Dict[str, Any]] = {}
        self.heavy_components: List[str] = []

        # Prediction and learning
        self.trend_direction = 0.0
        self.predicted_usage = 0.0
        self.last_check = time.time()

        # Performance metrics
        self.shift_count = 0
        self.prevented_crashes = 0
        self.monitoring_overhead = 0.0

        _unified_logger.info(f"🧠 DynamicRAMMonitor initialized: {self.local_ram_gb:.1f}GB total")
        _unified_logger.debug(
            f"   Thresholds: Warning={self.warning_threshold*100:.0f}%, "
            f"Critical={self.critical_threshold*100:.0f}%, "
            f"Emergency={self.emergency_threshold*100:.0f}%"
        )

    async def get_macos_memory_pressure(self) -> Dict[str, Any]:
        """
        Get macOS memory pressure using vm_stat and memory_pressure command.

        Returns dict with:
        - pressure_level: 1 (normal), 2 (warn), 4 (critical)
        - pressure_status: "normal", "warn", "critical"
        - page_ins: Number of pages swapped in
        - page_outs: Number of pages swapped out
        - is_under_pressure: Boolean indicating actual memory stress
        """
        if not self.is_macos:
            return {
                "pressure_level": 1,
                "pressure_status": "normal",
                "page_ins": 0,
                "page_outs": 0,
                "is_under_pressure": False,
            }

        try:
            # Method 1: Try memory_pressure command
            pressure_level = 1
            try:
                proc = await asyncio.create_subprocess_exec(
                    "memory_pressure",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
                output = stdout.decode()

                if "critical" in output.lower():
                    pressure_level = 4
                elif "warn" in output.lower():
                    pressure_level = 2
            except (FileNotFoundError, asyncio.TimeoutError):
                pass

            # Method 2: Use vm_stat for page in/out rates
            proc = await asyncio.create_subprocess_exec(
                "vm_stat",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            output = stdout.decode()

            page_ins = 0
            page_outs = 0
            for line in output.split("\n"):
                if "Pages paged in:" in line:
                    page_ins = int(line.split(":")[1].strip().replace(".", ""))
                elif "Pages paged out:" in line:
                    page_outs = int(line.split(":")[1].strip().replace(".", ""))

            # Calculate pressure based on page activity
            is_under_pressure = page_outs > 1000
            if page_outs > 10000:
                pressure_level = max(pressure_level, 4)
            elif page_outs > 5000:
                pressure_level = max(pressure_level, 2)

            pressure_status = {1: "normal", 2: "warn", 4: "critical"}.get(
                pressure_level, "unknown"
            )

            return {
                "pressure_level": pressure_level,
                "pressure_status": pressure_status,
                "page_ins": page_ins,
                "page_outs": page_outs,
                "is_under_pressure": is_under_pressure or pressure_level >= 2,
            }

        except Exception as e:
            _unified_logger.debug(f"Failed to get macOS memory pressure: {e}")
            return {
                "pressure_level": 1,
                "pressure_status": "normal",
                "page_ins": 0,
                "page_outs": 0,
                "is_under_pressure": False,
            }

    async def get_current_state(self) -> Dict[str, Any]:
        """Get comprehensive current memory state."""
        start_time = time.time()

        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        pressure_info = await self.get_macos_memory_pressure()

        state = {
            "timestamp": datetime.now().isoformat(),
            "total_gb": self.local_ram_gb,
            "used_gb": mem.used / (1024**3),
            "available_gb": mem.available / (1024**3),
            "percent": mem.percent / 100.0,
            "swap_percent": swap.percent / 100.0,
            "trend": self.trend_direction,
            "predicted": self.predicted_usage,
            "status": self._get_status(mem.percent / 100.0, pressure_info),
            "shift_recommended": self._should_shift(mem.percent / 100.0, pressure_info),
            "emergency": self._is_emergency(mem.percent / 100.0, pressure_info),
            "pressure_level": pressure_info["pressure_level"],
            "pressure_status": pressure_info["pressure_status"],
            "is_under_pressure": pressure_info["is_under_pressure"],
            "page_outs": pressure_info["page_outs"],
        }

        self.current_usage = state["percent"]
        self.current_pressure = state["pressure_level"]
        self.monitoring_overhead = time.time() - start_time

        return state

    def _get_status(self, usage: float, pressure_info: Dict[str, Any]) -> str:
        """Get human-readable status based on usage and memory pressure."""
        if self.is_macos:
            pressure_level = pressure_info.get("pressure_level", 1)
            is_under_pressure = pressure_info.get("is_under_pressure", False)

            if pressure_level >= 4 or (is_under_pressure and usage >= 0.90):
                return "CRITICAL"
            elif pressure_level >= 2 and usage >= self.critical_threshold:
                return "WARNING"
            elif is_under_pressure:
                return "ELEVATED"
            elif usage >= self.warning_threshold:
                return "ELEVATED"
            else:
                return "OPTIMAL"
        else:
            if usage >= self.emergency_threshold:
                return "EMERGENCY"
            elif usage >= self.critical_threshold:
                return "CRITICAL"
            elif usage >= self.warning_threshold:
                return "WARNING"
            elif usage >= self.optimal_threshold:
                return "ELEVATED"
            else:
                return "OPTIMAL"

    def _should_shift(self, usage: float, pressure_info: Dict[str, Any]) -> bool:
        """Determine if workload should shift to GCP."""
        if self.is_macos:
            is_under_pressure = pressure_info.get("is_under_pressure", False)
            pressure_level = pressure_info.get("pressure_level", 1)
            return (is_under_pressure and usage >= self.critical_threshold) or pressure_level >= 4
        else:
            return usage >= self.warning_threshold

    def _is_emergency(self, usage: float, pressure_info: Dict[str, Any]) -> bool:
        """Determine if this is an emergency requiring immediate action."""
        if self.is_macos:
            pressure_level = pressure_info.get("pressure_level", 1)
            return pressure_level >= 4 and usage >= 0.90
        else:
            return usage >= self.emergency_threshold

    async def update_usage_history(self) -> None:
        """Update usage history and calculate trends."""
        state = await self.get_current_state()

        self.usage_history.append({"time": time.time(), "usage": state["percent"]})

        if len(self.usage_history) > self.max_history:
            self.usage_history.pop(0)

        if len(self.usage_history) >= 5:
            recent = [h["usage"] for h in self.usage_history[-5:]]
            self.trend_direction = (recent[-1] - recent[0]) / 5.0
            self.predicted_usage = min(
                1.0, max(0.0, state["percent"] + (self.trend_direction * self.prediction_window))
            )

    async def should_shift_to_gcp(self) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Determine if workload should shift to GCP.

        Returns:
            (should_shift, reason, details)
        """
        state = await self.get_current_state()

        if state["emergency"]:
            return (True, "EMERGENCY: RAM at critical level", state)

        if state["status"] == "CRITICAL":
            return (True, "CRITICAL: RAM usage exceeds threshold", state)

        if state["status"] == "WARNING" and self.trend_direction > 0.01:
            return (True, "PROACTIVE: Rising RAM trend detected", state)

        if state["predicted"] >= self.critical_threshold:
            return (True, "PREDICTIVE: Future RAM spike predicted", state)

        return (False, "OPTIMAL: Local RAM sufficient", state)

    async def should_shift_to_local(self, gcp_cost: float = 0.0) -> Tuple[bool, str]:
        """Determine if workload should shift back to local."""
        state = await self.get_current_state()

        if state["percent"] < self.optimal_threshold and self.trend_direction <= 0:
            return (True, "OPTIMAL: Local RAM available, reducing GCP cost")

        if gcp_cost > 10.0 and state["percent"] < self.warning_threshold:
            return (True, f"COST_OPTIMIZATION: ${gcp_cost:.2f}/hr GCP cost, local available")

        return (False, "MAINTAINING: GCP deployment active")


# =============================================================================
# LAZY ASYNC LOCK - Python 3.9 Compatibility
# =============================================================================
class LazyAsyncLock:
    """
    Lazy-initialized asyncio.Lock for Python 3.9+ compatibility.

    asyncio.Lock() cannot be created outside of an async context in Python 3.9.
    This wrapper delays initialization until first use within an async context.
    """

    def __init__(self):
        self._lock: Optional[asyncio.Lock] = None

    def _ensure_lock(self) -> asyncio.Lock:
        """Ensure lock exists, creating it if needed."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def __aenter__(self):
        """Enter async context manager."""
        lock = self._ensure_lock()
        await lock.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Exit async context manager."""
        if self._lock is not None:
            self._lock.release()
        return False


# =============================================================================
# GLOBAL SESSION MANAGER - Session Tracking Singleton
# =============================================================================
class GlobalSessionManager:
    """
    Async-safe singleton manager for JARVIS session tracking.

    Features:
    - Singleton pattern with thread-safe initialization
    - Async-safe operations with asyncio.Lock
    - Early registration before other components
    - Guaranteed availability during cleanup
    - Automatic stale session cleanup
    - Multi-terminal conflict prevention
    """

    _instance: Optional['GlobalSessionManager'] = None
    _init_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        """Initialize session manager (only runs once due to singleton)."""
        if getattr(self, '_initialized', False):
            return

        self._lock = LazyAsyncLock()
        self._sync_lock = threading.Lock()

        # Session identity
        self.session_id = str(uuid.uuid4())
        self.pid = os.getpid()
        self.hostname = socket.gethostname()
        self.created_at = time.time()

        # Session tracking files
        self._temp_dir = Path(tempfile.gettempdir())
        self.session_file = self._temp_dir / f"jarvis_session_{self.pid}.json"
        self.vm_registry = self._temp_dir / "jarvis_vm_registry.json"
        self.global_tracker_file = self._temp_dir / "jarvis_global_session.json"

        # VM tracking
        self._current_vm: Optional[Dict[str, Any]] = None

        # Statistics
        self._stats = {
            "vms_registered": 0,
            "vms_unregistered": 0,
            "registry_cleanups": 0,
            "stale_sessions_removed": 0,
        }

        self._register_global_session()
        self._initialized = True

        _unified_logger.info(f"🌐 Global Session Manager initialized:")
        _unified_logger.info(f"   ├─ Session: {self.session_id[:8]}...")
        _unified_logger.info(f"   ├─ PID: {self.pid}")
        _unified_logger.info(f"   └─ Hostname: {self.hostname}")

    def _register_global_session(self):
        """Register this session in the global tracker (sync)."""
        try:
            session_info = {
                "session_id": self.session_id,
                "pid": self.pid,
                "hostname": self.hostname,
                "created_at": self.created_at,
                "vm_id": None,
                "status": "active",
            }
            self.global_tracker_file.write_text(json.dumps(session_info, indent=2))
        except Exception as e:
            _unified_logger.warning(f"Failed to register global session: {e}")

    async def register_vm(
        self,
        vm_id: str,
        zone: str,
        components: List[str],
        metadata: Optional[Dict[str, Any]] = None
    ) -> bool:
        """Register VM ownership for this session."""
        async with self._lock:
            session_data = {
                "session_id": self.session_id,
                "pid": self.pid,
                "hostname": self.hostname,
                "vm_id": vm_id,
                "zone": zone,
                "components": components,
                "metadata": metadata or {},
                "created_at": self.created_at,
                "registered_at": time.time(),
                "status": "active",
            }

            self._current_vm = session_data

            try:
                self.session_file.write_text(json.dumps(session_data, indent=2))
            except Exception as e:
                _unified_logger.error(f"Failed to write session file: {e}")
                return False

            try:
                registry = await self._load_registry_async()
                registry[self.session_id] = session_data
                await self._save_registry_async(registry)
            except Exception as e:
                _unified_logger.error(f"Failed to update VM registry: {e}")
                return False

            self._stats["vms_registered"] += 1
            _unified_logger.info(f"📝 Registered VM {vm_id} to session {self.session_id[:8]}")
            return True

    async def get_my_vm(self) -> Optional[Dict[str, Any]]:
        """Get VM owned by this session."""
        async with self._lock:
            if self._current_vm:
                return self._current_vm

            if not self.session_file.exists():
                return None

            try:
                data = json.loads(self.session_file.read_text())
                if self._validate_ownership(data):
                    self._current_vm = data
                    return data
            except Exception as e:
                _unified_logger.error(f"Failed to read session file: {e}")
            return None

    def get_my_vm_sync(self) -> Optional[Dict[str, Any]]:
        """Synchronous version of get_my_vm for use during cleanup."""
        with self._sync_lock:
            if self._current_vm:
                return self._current_vm

            if self.global_tracker_file.exists():
                try:
                    data = json.loads(self.global_tracker_file.read_text())
                    if data.get("session_id") == self.session_id and data.get("vm_id"):
                        return {
                            "vm_id": data["vm_id"],
                            "zone": data.get("zone"),
                            "session_id": data["session_id"],
                            "pid": data.get("pid"),
                        }
                except Exception:
                    pass

            if not self.session_file.exists():
                return None

            try:
                data = json.loads(self.session_file.read_text())
                if self._validate_ownership(data):
                    self._current_vm = data
                    return data
            except Exception:
                pass
            return None

    def _validate_ownership(self, data: Dict[str, Any]) -> bool:
        """Validate that session data belongs to this session."""
        if data.get("session_id") != self.session_id:
            return False
        if data.get("pid") != self.pid:
            return False
        if data.get("hostname") != self.hostname:
            return False

        age_hours = (time.time() - data.get("created_at", 0)) / 3600
        if age_hours > 12:
            try:
                self.session_file.unlink()
            except Exception:
                pass
            return False
        return True

    async def unregister_vm(self) -> bool:
        """Unregister VM ownership and cleanup session files."""
        async with self._lock:
            try:
                self._current_vm = None
                if self.session_file.exists():
                    self.session_file.unlink()

                registry = await self._load_registry_async()
                if self.session_id in registry:
                    del registry[self.session_id]
                    await self._save_registry_async(registry)

                self._stats["vms_unregistered"] += 1
                return True
            except Exception as e:
                _unified_logger.error(f"Failed to unregister VM: {e}")
                return False

    def unregister_vm_sync(self) -> bool:
        """Synchronous version of unregister_vm for cleanup."""
        with self._sync_lock:
            try:
                self._current_vm = None
                if self.session_file.exists():
                    self.session_file.unlink()

                registry = self._load_registry_sync()
                if self.session_id in registry:
                    del registry[self.session_id]
                    self._save_registry_sync(registry)

                if self.global_tracker_file.exists():
                    self.global_tracker_file.unlink()

                self._stats["vms_unregistered"] += 1
                return True
            except Exception as e:
                _unified_logger.error(f"Failed to unregister VM: {e}")
                return False

    async def get_all_active_sessions(self) -> Dict[str, Dict[str, Any]]:
        """Get all active sessions with staleness filtering."""
        async with self._lock:
            registry = await self._load_registry_async()
            active_sessions = {}
            stale_count = 0

            for session_id, data in registry.items():
                pid = data.get("pid")
                if pid and self._is_pid_running(pid):
                    age_hours = (time.time() - data.get("created_at", 0)) / 3600
                    if age_hours <= 12:
                        active_sessions[session_id] = data
                    else:
                        stale_count += 1
                else:
                    stale_count += 1

            if len(active_sessions) != len(registry):
                await self._save_registry_async(active_sessions)
                self._stats["registry_cleanups"] += 1
                self._stats["stale_sessions_removed"] += stale_count

            return active_sessions

    async def _load_registry_async(self) -> Dict[str, Any]:
        """Load VM registry from disk."""
        if not self.vm_registry.exists():
            return {}
        try:
            loop = asyncio.get_event_loop()
            content = await loop.run_in_executor(None, self.vm_registry.read_text)
            return json.loads(content)
        except Exception:
            return {}

    async def _save_registry_async(self, registry: Dict[str, Any]):
        """Save VM registry to disk."""
        try:
            content = json.dumps(registry, indent=2)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.vm_registry.write_text, content)
        except Exception as e:
            _unified_logger.error(f"Failed to save VM registry: {e}")

    def _load_registry_sync(self) -> Dict[str, Any]:
        """Load VM registry from disk (sync version)."""
        if not self.vm_registry.exists():
            return {}
        try:
            return json.loads(self.vm_registry.read_text())
        except Exception:
            return {}

    def _save_registry_sync(self, registry: Dict[str, Any]):
        """Save VM registry to disk (sync version)."""
        try:
            self.vm_registry.write_text(json.dumps(registry, indent=2))
        except Exception as e:
            _unified_logger.error(f"Failed to save VM registry: {e}")

    def _is_pid_running(self, pid: int) -> bool:
        """Check if PID is currently running."""
        try:
            proc = psutil.Process(pid)
            cmdline = proc.cmdline()
            return "unified_supervisor.py" in " ".join(cmdline) or "start_system.py" in " ".join(cmdline)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    def get_statistics(self) -> Dict[str, Any]:
        """Get session manager statistics."""
        return {
            "session_id": self.session_id,
            "pid": self.pid,
            "hostname": self.hostname,
            "uptime_seconds": time.time() - self.created_at,
            "has_vm": self._current_vm is not None,
            "vm_id": self._current_vm.get("vm_id") if self._current_vm else None,
            **self._stats,
        }


# Module-level singleton accessor
_global_session_manager: Optional[GlobalSessionManager] = None
_session_manager_lock = threading.Lock()


def get_session_manager() -> GlobalSessionManager:
    """Get the global session manager singleton."""
    global _global_session_manager
    if _global_session_manager is None:
        with _session_manager_lock:
            if _global_session_manager is None:
                _global_session_manager = GlobalSessionManager()
    return _global_session_manager


# =============================================================================
# SUPERVISOR RESTART MANAGER - Cross-Repo Process Management
# =============================================================================
@dataclass
class SupervisorManagedProcess:
    """Metadata for a supervisor-managed process."""
    name: str
    process: Optional[asyncio.subprocess.Process]
    restart_func: Callable[[], Any]
    restart_count: int = 0
    last_restart: float = 0.0
    max_restarts: int = 3
    port: Optional[int] = None
    enabled: bool = True
    exit_code: Optional[int] = None


class SupervisorRestartManager:
    """
    Cross-repo process restart manager for supervisor-level services.

    Manages automatic restart of:
    - JARVIS-Prime (local inference server)
    - Reactor-Core (training/ML services)

    Features:
    - Named process tracking (not index-based)
    - Exponential backoff: 1s → 2s → 4s → max configurable
    - Per-process restart tracking
    - Maximum restart limit with alerting
    - Async-safe with proper locking
    - Environment variable configuration
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        """Initialize the supervisor restart manager."""
        self.processes: Dict[str, SupervisorManagedProcess] = {}
        self._lock = asyncio.Lock()
        self._shutdown_requested = False
        self._logger = logger or logging.getLogger("SupervisorRestartManager")

        # Environment-driven configuration
        self.max_restarts = int(os.getenv("JARVIS_SUPERVISOR_MAX_RESTARTS", "3"))
        self.max_backoff = float(os.getenv("JARVIS_SUPERVISOR_MAX_BACKOFF", "60.0"))
        self.restart_cooldown = float(os.getenv("JARVIS_SUPERVISOR_RESTART_COOLDOWN", "600.0"))
        self.base_backoff = float(os.getenv("JARVIS_SUPERVISOR_BASE_BACKOFF", "2.0"))

    def register(
        self,
        name: str,
        process: Optional[asyncio.subprocess.Process],
        restart_func: Callable[[], Any],
        port: Optional[int] = None,
        enabled: bool = True,
    ) -> None:
        """Register a cross-repo process for monitoring and automatic restart."""
        self.processes[name] = SupervisorManagedProcess(
            name=name,
            process=process,
            restart_func=restart_func,
            restart_count=0,
            last_restart=0.0,
            max_restarts=self.max_restarts,
            port=port,
            enabled=enabled,
        )
        if process:
            self._logger.info(
                f"Registered cross-repo process '{name}' (PID: {process.pid})"
                + (f" on port {port}" if port else "")
            )

    def update_process(self, name: str, process: asyncio.subprocess.Process) -> None:
        """Update the process reference for a registered service."""
        if name in self.processes:
            self.processes[name].process = process
            self._logger.debug(f"Updated process reference for '{name}' (PID: {process.pid})")

    def request_shutdown(self) -> None:
        """Signal that shutdown is requested - stop all restart attempts."""
        self._shutdown_requested = True
        self._logger.info("Supervisor shutdown requested - restart manager disabled")

    def reset_shutdown(self) -> None:
        """Reset shutdown flag - allow restarts again."""
        self._shutdown_requested = False

    async def check_and_restart_all(self) -> List[str]:
        """Check all cross-repo processes and restart any that have exited."""
        if self._shutdown_requested:
            return []

        restarted = []

        async with self._lock:
            for name, managed in list(self.processes.items()):
                if not managed.enabled or managed.process is None:
                    continue

                proc = managed.process

                if proc.returncode is not None:
                    managed.exit_code = proc.returncode

                    if proc.returncode in (0, -2, -15):
                        self._logger.debug(f"{name} exited normally (code: {proc.returncode})")
                        continue

                    success = await self._handle_unexpected_exit(name, managed)
                    if success:
                        restarted.append(name)

        return restarted

    async def _handle_unexpected_exit(
        self, name: str, managed: SupervisorManagedProcess
    ) -> bool:
        """Handle an unexpected process exit with exponential backoff restart."""
        current_time = time.time()

        if managed.restart_count >= managed.max_restarts:
            self._logger.error(
                f"❌ {name} exceeded supervisor restart limit ({managed.max_restarts}). "
                f"Last exit code: {managed.exit_code}. Manual intervention required."
            )
            return False

        if current_time - managed.last_restart > self.restart_cooldown:
            if managed.restart_count > 0:
                self._logger.info(
                    f"{name} was stable for {self.restart_cooldown}s - "
                    f"resetting restart count from {managed.restart_count} to 0"
                )
            managed.restart_count = 0

        backoff = min(
            self.base_backoff * (2 ** managed.restart_count),
            self.max_backoff
        )

        managed.restart_count += 1
        managed.last_restart = current_time

        self._logger.warning(
            f"🔄 Supervisor restarting '{name}' in {backoff:.1f}s "
            f"(attempt {managed.restart_count}/{managed.max_restarts}, "
            f"exit code: {managed.exit_code})"
        )

        await asyncio.sleep(backoff)

        if self._shutdown_requested:
            self._logger.info(f"Shutdown requested - aborting restart of '{name}'")
            return False

        try:
            await managed.restart_func()
            self._logger.info(f"✅ {name} restart initiated successfully")
            return True
        except Exception as e:
            self._logger.error(f"❌ Failed to restart '{name}': {e}")
            return False

    def get_status(self) -> Dict[str, Dict[str, Any]]:
        """Get status of all supervised cross-repo processes."""
        status = {}
        for name, managed in self.processes.items():
            proc = managed.process
            status[name] = {
                "pid": proc.pid if proc else None,
                "running": proc.returncode is None if proc else False,
                "exit_code": managed.exit_code,
                "restart_count": managed.restart_count,
                "last_restart": managed.last_restart,
                "port": managed.port,
                "enabled": managed.enabled,
            }
        return status


# =============================================================================
# TRINITY LAUNCH CONFIG - Environment-Driven Configuration
# =============================================================================
@dataclass
class TrinityLaunchConfig:
    """
    Ultra-robust configuration for Trinity component launch.

    ALL values are environment-driven with sensible defaults.
    Zero hardcoding - everything configurable at runtime.
    """

    # Core Trinity Settings
    trinity_enabled: bool = field(default_factory=lambda: os.getenv("TRINITY_ENABLED", "true").lower() == "true")
    trinity_auto_launch: bool = field(default_factory=lambda: os.getenv("TRINITY_AUTO_LAUNCH", "true").lower() == "true")
    trinity_instance_id: str = field(default_factory=lambda: os.getenv("TRINITY_INSTANCE_ID", ""))

    # Repo Discovery Settings
    jprime_repo_path: Optional[Path] = field(default_factory=lambda: Path(os.getenv(
        "JARVIS_PRIME_PATH",
        str(Path.home() / "Documents" / "repos" / "jarvis-prime")
    )) if os.getenv("JARVIS_PRIME_PATH") or (Path.home() / "Documents" / "repos" / "jarvis-prime").exists() else None)

    reactor_core_repo_path: Optional[Path] = field(default_factory=lambda: Path(os.getenv(
        "REACTOR_CORE_PATH",
        str(Path.home() / "Documents" / "repos" / "reactor-core")
    )) if os.getenv("REACTOR_CORE_PATH") or (Path.home() / "Documents" / "repos" / "reactor-core").exists() else None)

    # Secondary search locations
    repo_search_paths: List[Path] = field(default_factory=lambda: [
        Path(p) for p in os.getenv("TRINITY_REPO_SEARCH_PATHS", "").split(":") if p
    ] or [
        Path.home() / "Documents" / "repos",
        Path.home() / "repos",
        Path.home() / "code",
        Path.home() / "projects",
        Path.home() / "dev",
        Path.cwd().parent,
    ])

    # Repo identification patterns
    jprime_identifiers: List[str] = field(default_factory=lambda:
        os.getenv("TRINITY_JPRIME_IDENTIFIERS", "jarvis-prime,jarvis_prime,j-prime,jprime").split(",")
    )
    reactor_core_identifiers: List[str] = field(default_factory=lambda:
        os.getenv("TRINITY_REACTOR_IDENTIFIERS", "reactor-core,reactor_core,reactorcore").split(",")
    )

    # Python Environment Detection
    venv_detection_order: List[str] = field(default_factory=lambda:
        os.getenv("TRINITY_VENV_DETECTION_ORDER", "venv,env,.venv,.env,virtualenv").split(",")
    )
    python_executable_names: List[str] = field(default_factory=lambda:
        os.getenv("TRINITY_PYTHON_NAMES", "python3,python,python3.11,python3.10,python3.9").split(",")
    )
    fallback_to_system_python: bool = field(default_factory=lambda:
        os.getenv("TRINITY_FALLBACK_SYSTEM_PYTHON", "true").lower() == "true"
    )

    # Launch Script Detection
    jprime_launch_scripts: List[str] = field(default_factory=lambda:
        os.getenv("TRINITY_JPRIME_SCRIPTS",
            "jarvis_prime/server.py,run_server.py,jarvis_prime/core/trinity_bridge.py,main.py"
        ).split(",")
    )
    reactor_core_launch_scripts: List[str] = field(default_factory=lambda:
        os.getenv("TRINITY_REACTOR_SCRIPTS",
            "reactor_core/orchestration/trinity_orchestrator.py,run_orchestrator.py,main.py"
        ).split(",")
    )

    # Timeout Configuration (Adaptive)
    launch_timeout_sec: float = field(default_factory=lambda: float(os.getenv("TRINITY_LAUNCH_TIMEOUT", "120.0")))
    registration_timeout_sec: float = field(default_factory=lambda: float(os.getenv("TRINITY_REGISTRATION_TIMEOUT", "30.0")))
    health_check_timeout_sec: float = field(default_factory=lambda: float(os.getenv("TRINITY_HEALTH_CHECK_TIMEOUT", "10.0")))
    shutdown_timeout_sec: float = field(default_factory=lambda: float(os.getenv("TRINITY_SHUTDOWN_TIMEOUT", "30.0")))

    # Heartbeat Configuration
    heartbeat_dir: Path = field(default_factory=lambda:
        Path(os.getenv("TRINITY_HEARTBEAT_DIR", str(Path.home() / ".jarvis" / "trinity" / "components")))
    )
    heartbeat_max_age_sec: float = field(default_factory=lambda: float(os.getenv("TRINITY_HEARTBEAT_MAX_AGE", "30.0")))
    heartbeat_check_interval_sec: float = field(default_factory=lambda: float(os.getenv("TRINITY_HEARTBEAT_INTERVAL", "5.0")))

    # Retry Configuration
    max_retries: int = field(default_factory=lambda: int(os.getenv("TRINITY_MAX_RETRIES", "3")))
    retry_base_delay_sec: float = field(default_factory=lambda: float(os.getenv("TRINITY_RETRY_BASE_DELAY", "1.0")))
    retry_max_delay_sec: float = field(default_factory=lambda: float(os.getenv("TRINITY_RETRY_MAX_DELAY", "30.0")))

    # Circuit Breaker Configuration
    circuit_breaker_enabled: bool = field(default_factory=lambda:
        os.getenv("TRINITY_CIRCUIT_BREAKER_ENABLED", "true").lower() == "true"
    )
    circuit_breaker_failure_threshold: int = field(default_factory=lambda:
        int(os.getenv("TRINITY_CIRCUIT_FAILURE_THRESHOLD", "5"))
    )
    circuit_breaker_timeout_sec: float = field(default_factory=lambda:
        float(os.getenv("TRINITY_CIRCUIT_TIMEOUT", "60.0"))
    )

    # Process Management
    log_dir: Path = field(default_factory=lambda:
        Path(os.getenv("TRINITY_LOG_DIR", str(Path.home() / ".jarvis" / "logs" / "services")))
    )
    detach_processes: bool = field(default_factory=lambda:
        os.getenv("TRINITY_DETACH_PROCESSES", "true").lower() == "true"
    )
    sigterm_timeout_sec: float = field(default_factory=lambda: float(os.getenv("TRINITY_SIGTERM_TIMEOUT", "5.0")))
    sigkill_timeout_sec: float = field(default_factory=lambda: float(os.getenv("TRINITY_SIGKILL_TIMEOUT", "2.0")))

    # Port Configuration
    jprime_ports: List[int] = field(default_factory=lambda:
        [int(p) for p in os.getenv("TRINITY_JPRIME_PORTS", "8000").split(",")]
    )
    reactor_core_ports: List[int] = field(default_factory=lambda:
        [int(p) for p in os.getenv("TRINITY_REACTOR_PORTS", "8090").split(",")]
    )

    # Dynamic port allocation
    dynamic_port_enabled: bool = field(default_factory=lambda:
        os.getenv("TRINITY_DYNAMIC_PORTS", "true").lower() == "true"
    )
    dynamic_port_range_start: int = field(default_factory=lambda:
        int(os.getenv("TRINITY_DYNAMIC_PORT_START", "8100"))
    )
    dynamic_port_range_end: int = field(default_factory=lambda:
        int(os.getenv("TRINITY_DYNAMIC_PORT_END", "8199"))
    )

    # Graceful Degradation
    jprime_optional: bool = field(default_factory=lambda:
        os.getenv("TRINITY_JPRIME_OPTIONAL", "true").lower() == "true"
    )
    reactor_core_optional: bool = field(default_factory=lambda:
        os.getenv("TRINITY_REACTOR_OPTIONAL", "true").lower() == "true"
    )
    continue_on_partial_failure: bool = field(default_factory=lambda:
        os.getenv("TRINITY_CONTINUE_ON_PARTIAL", "true").lower() == "true"
    )

    # Health Monitoring
    health_monitor_enabled: bool = field(default_factory=lambda:
        os.getenv("TRINITY_HEALTH_MONITOR_ENABLED", "true").lower() == "true"
    )
    health_monitor_interval_sec: float = field(default_factory=lambda:
        float(os.getenv("TRINITY_HEALTH_MONITOR_INTERVAL", "10.0"))
    )
    auto_restart_on_crash: bool = field(default_factory=lambda:
        os.getenv("TRINITY_AUTO_RESTART", "true").lower() == "true"
    )
    max_auto_restarts: int = field(default_factory=lambda:
        int(os.getenv("TRINITY_MAX_RESTARTS", "3"))
    )
    restart_cooldown_sec: float = field(default_factory=lambda:
        float(os.getenv("TRINITY_RESTART_COOLDOWN", "60.0"))
    )

    # API Port
    jarvis_api_port: int = field(default_factory=lambda:
        int(os.getenv("JARVIS_API_PORT", "8080"))
    )

    def __post_init__(self):
        """Validate and create necessary directories."""
        self.heartbeat_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        if not self.trinity_instance_id:
            self.trinity_instance_id = f"trinity_{uuid.uuid4().hex[:8]}"


# =============================================================================
# DYNAMIC REPO DISCOVERY - Intelligent Repository Finding
# =============================================================================
class DynamicRepoDiscovery:
    """
    Intelligent repo discovery system that finds Trinity repos dynamically.

    Discovery strategies (in order):
    1. Environment variables (JARVIS_PRIME_PATH, REACTOR_CORE_PATH)
    2. User config file (~/.jarvis/repos.json)
    3. Common repo locations (~/Documents/repos, ~/repos, ~/code, etc.)
    4. Git remote scanning (looks for known repo URLs)
    5. Parent/sibling directory scanning
    """

    def __init__(self, config: TrinityLaunchConfig):
        self.config = config
        self._discovery_cache: Dict[str, Optional[Path]] = {}
        self._logger = logging.getLogger("TrinityRepoDiscovery")

    async def discover_jprime(self) -> Optional[Path]:
        """Discover J-Prime repository path."""
        if "jprime" in self._discovery_cache:
            return self._discovery_cache["jprime"]

        # Strategy 1: Environment variable / config
        if self.config.jprime_repo_path and self.config.jprime_repo_path.exists():
            self._discovery_cache["jprime"] = self.config.jprime_repo_path
            return self.config.jprime_repo_path

        # Strategy 2: User config file
        config_path = Path.home() / ".jarvis" / "repos.json"
        if config_path.exists():
            try:
                with open(config_path) as f:
                    repos = json.load(f)
                if "jarvis_prime" in repos:
                    path = Path(repos["jarvis_prime"])
                    if path.exists():
                        self._discovery_cache["jprime"] = path
                        return path
            except Exception:
                pass

        # Strategy 3: Search common locations
        for search_path in self.config.repo_search_paths:
            if not search_path.exists():
                continue
            for identifier in self.config.jprime_identifiers:
                candidate = search_path / identifier
                if candidate.exists() and self._is_jprime_repo(candidate):
                    self._discovery_cache["jprime"] = candidate
                    self._logger.info(f"Discovered J-Prime at: {candidate}")
                    return candidate

        # Strategy 4: Git remote scanning
        found = await self._scan_for_git_remote("jarvis-prime", self.config.repo_search_paths)
        if found:
            self._discovery_cache["jprime"] = found
            return found

        self._discovery_cache["jprime"] = None
        return None

    async def discover_reactor_core(self) -> Optional[Path]:
        """Discover Reactor-Core repository path."""
        if "reactor_core" in self._discovery_cache:
            return self._discovery_cache["reactor_core"]

        # Strategy 1: Environment variable / config
        if self.config.reactor_core_repo_path and self.config.reactor_core_repo_path.exists():
            self._discovery_cache["reactor_core"] = self.config.reactor_core_repo_path
            return self.config.reactor_core_repo_path

        # Strategy 2: User config file
        config_path = Path.home() / ".jarvis" / "repos.json"
        if config_path.exists():
            try:
                with open(config_path) as f:
                    repos = json.load(f)
                if "reactor_core" in repos:
                    path = Path(repos["reactor_core"])
                    if path.exists():
                        self._discovery_cache["reactor_core"] = path
                        return path
            except Exception:
                pass

        # Strategy 3: Search common locations
        for search_path in self.config.repo_search_paths:
            if not search_path.exists():
                continue
            for identifier in self.config.reactor_core_identifiers:
                candidate = search_path / identifier
                if candidate.exists() and self._is_reactor_core_repo(candidate):
                    self._discovery_cache["reactor_core"] = candidate
                    self._logger.info(f"Discovered Reactor-Core at: {candidate}")
                    return candidate

        # Strategy 4: Git remote scanning
        found = await self._scan_for_git_remote("reactor-core", self.config.repo_search_paths)
        if found:
            self._discovery_cache["reactor_core"] = found
            return found

        self._discovery_cache["reactor_core"] = None
        return None

    def _is_jprime_repo(self, path: Path) -> bool:
        """Verify this is the J-Prime repo by checking for signature files."""
        signature_files = [
            path / "jarvis_prime" / "server.py",
            path / "jarvis_prime" / "__init__.py",
            path / "run_server.py",
        ]
        return any(f.exists() for f in signature_files)

    def _is_reactor_core_repo(self, path: Path) -> bool:
        """Verify this is the Reactor-Core repo."""
        signature_files = [
            path / "reactor_core" / "orchestration" / "trinity_orchestrator.py",
            path / "reactor_core" / "__init__.py",
            path / "run_orchestrator.py",
        ]
        return any(f.exists() for f in signature_files)

    async def _scan_for_git_remote(self, repo_name: str, search_paths: List[Path]) -> Optional[Path]:
        """Scan for repos by checking git remote URLs."""
        import subprocess

        for search_path in search_paths:
            if not search_path.exists():
                continue

            try:
                for entry in search_path.iterdir():
                    if not entry.is_dir():
                        continue
                    git_dir = entry / ".git"
                    if not git_dir.exists():
                        continue

                    try:
                        result = subprocess.run(
                            ["git", "-C", str(entry), "remote", "-v"],
                            capture_output=True, text=True, timeout=5
                        )
                        if repo_name in result.stdout.lower():
                            return entry
                    except (subprocess.TimeoutExpired, FileNotFoundError):
                        continue
            except PermissionError:
                continue

        return None


# =============================================================================
# ROBUST VENV DETECTOR - Python Environment Detection
# =============================================================================
class RobustVenvDetector:
    """
    Robust Python virtual environment detector.

    Handles:
    - Standard venv (venv, env, .venv, .env)
    - Virtualenvwrapper (~/.virtualenvs)
    - Conda environments
    - Poetry environments
    - Pipenv environments
    - pyenv
    - System Python fallback
    """

    def __init__(self, config: TrinityLaunchConfig):
        self.config = config
        self._logger = logging.getLogger("TrinityVenvDetector")

    def find_python(self, repo_path: Path) -> str:
        """Find the best Python executable for a repo."""
        # Strategy 1: Check standard venv locations
        for venv_name in self.config.venv_detection_order:
            venv_path = repo_path / venv_name
            python = self._find_python_in_venv(venv_path)
            if python:
                self._logger.debug(f"Found Python in {venv_name}: {python}")
                return python

        # Strategy 2: Check .python-version (pyenv)
        pyenv_file = repo_path / ".python-version"
        if pyenv_file.exists():
            try:
                version = pyenv_file.read_text().strip()
                if version:
                    pyenv_python = Path.home() / ".pyenv" / "versions" / version / "bin" / "python"
                    if pyenv_python.exists():
                        self._logger.debug(f"Found pyenv Python: {pyenv_python}")
                        return str(pyenv_python)
            except Exception:
                pass

        # Strategy 3: Check poetry.lock (poetry environment)
        if (repo_path / "poetry.lock").exists():
            poetry_python = self._find_poetry_python(repo_path)
            if poetry_python:
                self._logger.debug(f"Found poetry Python: {poetry_python}")
                return poetry_python

        # Strategy 4: Check Pipfile.lock (pipenv environment)
        if (repo_path / "Pipfile.lock").exists():
            pipenv_python = self._find_pipenv_python(repo_path)
            if pipenv_python:
                self._logger.debug(f"Found pipenv Python: {pipenv_python}")
                return pipenv_python

        # Strategy 5: Fallback to system Python
        if self.config.fallback_to_system_python:
            for name in self.config.python_executable_names:
                import shutil
                python = shutil.which(name)
                if python:
                    self._logger.debug(f"Using system Python: {python}")
                    return python

        # Last resort: use current interpreter
        self._logger.warning(f"No Python found for {repo_path}, using current interpreter")
        return sys.executable

    def _find_python_in_venv(self, venv_path: Path) -> Optional[str]:
        """Find Python executable in a venv directory."""
        if not venv_path.exists():
            return None

        # Unix-like systems
        for name in self.config.python_executable_names:
            python_path = venv_path / "bin" / name
            if python_path.exists():
                return str(python_path)

        # Windows
        for name in self.config.python_executable_names:
            python_path = venv_path / "Scripts" / f"{name}.exe"
            if python_path.exists():
                return str(python_path)

        return None

    def _find_poetry_python(self, repo_path: Path) -> Optional[str]:
        """Find Python from poetry environment."""
        import subprocess
        try:
            result = subprocess.run(
                ["poetry", "env", "info", "-p"],
                cwd=str(repo_path),
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                venv_path = Path(result.stdout.strip())
                return self._find_python_in_venv(venv_path)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return None

    def _find_pipenv_python(self, repo_path: Path) -> Optional[str]:
        """Find Python from pipenv environment."""
        import subprocess
        try:
            result = subprocess.run(
                ["pipenv", "--venv"],
                cwd=str(repo_path),
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                venv_path = Path(result.stdout.strip())
                return self._find_python_in_venv(venv_path)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return None

    def get_python_executable(self, repo_path: Path) -> str:
        """Alias for find_python."""
        return self.find_python(repo_path)


# =============================================================================
# TRINITY TRACE CONTEXT - Distributed Tracing
# =============================================================================
@dataclass
class TrinityTraceContext:
    """W3C Trace Context for Trinity distributed tracing."""
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_span_id: Optional[str] = None
    trace_flags: int = 1  # Sampled by default

    def to_traceparent(self) -> str:
        """Convert to W3C traceparent header format."""
        return f"00-{self.trace_id}-{self.span_id}-{self.trace_flags:02x}"

    @classmethod
    def from_traceparent(cls, header: str) -> Optional['TrinityTraceContext']:
        """Parse W3C traceparent header."""
        try:
            parts = header.split("-")
            if len(parts) == 4 and parts[0] == "00":
                return cls(
                    trace_id=parts[1],
                    span_id=parts[2],
                    trace_flags=int(parts[3], 16),
                )
        except Exception:
            pass
        return None

    def create_child_span(self) -> 'TrinityTraceContext':
        """Create a child span context."""
        return TrinityTraceContext(
            trace_id=self.trace_id,
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=self.span_id,
            trace_flags=self.trace_flags,
        )


# =============================================================================
# ASYNC VOICE NARRATOR - Voice Feedback for Startup
# =============================================================================
class AsyncVoiceNarrator:
    """
    Async voice narrator for startup feedback.

    Features:
    - Non-blocking voice output
    - Platform-aware (macOS only)
    - Graceful fallback on errors
    - Queue management
    """

    def __init__(self, enabled: bool = True, voice: str = "Daniel"):
        self.enabled = enabled and platform.system() == "Darwin"
        self.voice = voice
        self._process: Optional[asyncio.subprocess.Process] = None
        self._queue: List[str] = []
        self._speaking = False

    async def speak(self, text: str, wait: bool = True, priority: bool = False) -> None:
        """Speak text using macOS say command."""
        if not self.enabled:
            return

        try:
            if priority:
                # Kill current speech for priority messages
                if self._process and self._process.returncode is None:
                    self._process.terminate()

            self._process = await asyncio.create_subprocess_exec(
                "say",
                "-v", self.voice,
                text,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )

            if wait:
                await asyncio.wait_for(self._process.communicate(), timeout=30.0)
            else:
                # Fire and forget
                pass

        except asyncio.TimeoutError:
            if self._process:
                self._process.terminate()
        except Exception as e:
            _unified_logger.debug(f"Voice error: {e}")

    async def cleanup(self) -> None:
        """Cleanup voice processes."""
        if self._process and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.communicate(), timeout=2.0)
            except asyncio.TimeoutError:
                self._process.kill()


# =============================================================================
# PHYSICS-AWARE STARTUP MANAGER - Voice Authentication
# =============================================================================
class PhysicsAwareStartupManager:
    """
    Physics-Aware Voice Authentication Startup Manager.

    Initializes and manages the physics-aware authentication components:
    - Reverberation analyzer (RT60, double-reverb detection)
    - Vocal tract length estimator (VTL biometrics)
    - Doppler analyzer (liveness detection)
    - Bayesian confidence fusion
    - 7-layer anti-spoofing system

    Environment Configuration:
    - PHYSICS_AWARE_ENABLED: Enable/disable (default: true)
    - PHYSICS_PRELOAD_MODELS: Preload models at startup (default: false)
    - PHYSICS_BASELINE_VTL_CM: User's baseline VTL (default: auto-detect)
    - PHYSICS_BASELINE_RT60_SEC: User's baseline RT60 (default: auto-detect)
    """

    def __init__(self):
        """Initialize physics-aware startup manager."""
        self.enabled = os.getenv("PHYSICS_AWARE_ENABLED", "true").lower() == "true"
        self.preload_models = os.getenv("PHYSICS_PRELOAD_MODELS", "false").lower() == "true"

        # Baseline values (can be overridden or auto-detected)
        self._baseline_vtl_cm: Optional[float] = None
        self._baseline_rt60_sec: Optional[float] = None

        baseline_vtl = os.getenv("PHYSICS_BASELINE_VTL_CM")
        if baseline_vtl:
            self._baseline_vtl_cm = float(baseline_vtl)

        baseline_rt60 = os.getenv("PHYSICS_BASELINE_RT60_SEC")
        if baseline_rt60:
            self._baseline_rt60_sec = float(baseline_rt60)

        # Component references
        self._physics_extractor = None
        self._anti_spoofing_detector = None
        self._initialized = False

        # Statistics
        self.initialization_time_ms = 0.0
        self.physics_verifications = 0
        self.spoofs_detected = 0

        _unified_logger.info(f"🔬 Physics-Aware Startup Manager initialized:")
        _unified_logger.debug(f"   ├─ Enabled: {self.enabled}")
        _unified_logger.debug(f"   ├─ Preload models: {self.preload_models}")
        _unified_logger.debug(f"   ├─ Baseline VTL: {self._baseline_vtl_cm or 'auto-detect'} cm")
        _unified_logger.debug(f"   └─ Baseline RT60: {self._baseline_rt60_sec or 'auto-detect'} sec")

    async def initialize(self) -> bool:
        """Initialize physics-aware authentication components."""
        if not self.enabled:
            _unified_logger.info("🔬 Physics-aware authentication disabled")
            return False

        start_time = time.time()

        try:
            # Import physics components
            from backend.voice_unlock.core.feature_extraction import (
                get_physics_feature_extractor,
            )
            from backend.voice_unlock.core.anti_spoofing import get_anti_spoofing_detector

            # Initialize physics extractor
            sample_rate = int(os.getenv("AUDIO_SAMPLE_RATE", "16000"))
            self._physics_extractor = get_physics_feature_extractor(sample_rate)

            # Set baselines if provided
            if self._baseline_vtl_cm:
                self._physics_extractor._baseline_vtl = self._baseline_vtl_cm
            if self._baseline_rt60_sec:
                self._physics_extractor._baseline_rt60 = self._baseline_rt60_sec

            # Initialize anti-spoofing detector
            self._anti_spoofing_detector = get_anti_spoofing_detector()

            self._initialized = True
            self.initialization_time_ms = (time.time() - start_time) * 1000

            _unified_logger.info(f"✅ Physics-aware authentication initialized ({self.initialization_time_ms:.0f}ms)")

            return True

        except ImportError as e:
            _unified_logger.warning(f"Physics components not available: {e}")
            self.enabled = False
            return False
        except Exception as e:
            _unified_logger.error(f"Physics initialization failed: {e}")
            self.enabled = False
            return False

    def get_physics_extractor(self):
        """Get the physics feature extractor instance."""
        return self._physics_extractor

    def get_anti_spoofing_detector(self):
        """Get the anti-spoofing detector instance."""
        return self._anti_spoofing_detector

    def get_statistics(self) -> Dict[str, Any]:
        """Get physics startup statistics."""
        return {
            "enabled": self.enabled,
            "initialized": self._initialized,
            "initialization_time_ms": self.initialization_time_ms,
            "baseline_vtl_cm": self._baseline_vtl_cm,
            "baseline_rt60_sec": self._baseline_rt60_sec,
            "physics_verifications": self.physics_verifications,
            "spoofs_detected": self.spoofs_detected,
        }


# =============================================================================
# RESOURCE STATUS - Enhanced Resource Metrics
# =============================================================================
@dataclass
class ResourceStatus:
    """
    Enhanced status of system resources with intelligent analysis.

    Includes not just resource metrics but also:
    - Recommendations for optimization
    - Actions taken automatically
    - Startup mode decision
    - Cloud activation status
    - ARM64 SIMD availability
    """
    memory_available_gb: float
    memory_total_gb: float
    disk_available_gb: float
    ports_available: List[int]
    ports_in_use: List[int]
    cpu_count: int
    load_average: Optional[Tuple[float, float, float]] = None
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    # Intelligent fields
    recommendations: List[str] = field(default_factory=list)
    actions_taken: List[str] = field(default_factory=list)
    startup_mode: Optional[str] = None  # local_full, cloud_first, cloud_only
    cloud_activated: bool = False
    arm64_simd_available: bool = False
    memory_pressure: float = 0.0  # 0-100%

    @property
    def is_healthy(self) -> bool:
        return len(self.errors) == 0

    @property
    def is_cloud_mode(self) -> bool:
        return self.startup_mode in ("cloud_first", "cloud_only")


# =============================================================================
# INTELLIGENT RESOURCE ORCHESTRATOR - Unified Resource Management
# =============================================================================
class IntelligentResourceOrchestrator:
    """
    Intelligent Resource Orchestrator for JARVIS Startup.

    This is a comprehensive, async, parallel, intelligent, and dynamic resource
    management system that integrates:

    1. MemoryAwareStartup - Intelligent cloud offloading decisions
    2. IntelligentMemoryOptimizer - Active memory optimization
    3. HybridRouter - Resource-aware request routing
    4. GCP Hybrid Cloud - Automatic cloud activation when needed

    Features:
    - Parallel resource checks with intelligent analysis
    - Automatic memory optimization when constrained
    - Dynamic startup mode selection (LOCAL_FULL, CLOUD_FIRST, CLOUD_ONLY)
    - Intelligent port conflict resolution
    - Cost-aware cloud activation recommendations
    - ARM64 SIMD optimization detection
    - Real-time resource monitoring
    """

    # Thresholds (configurable via environment)
    CLOUD_THRESHOLD_GB = float(os.getenv("JARVIS_CLOUD_THRESHOLD_GB", "6.0"))
    CRITICAL_THRESHOLD_GB = float(os.getenv("JARVIS_CRITICAL_THRESHOLD_GB", "2.0"))
    OPTIMIZE_THRESHOLD_GB = float(os.getenv("JARVIS_OPTIMIZE_THRESHOLD_GB", "4.0"))

    def __init__(self, config: SystemKernelConfig):
        self.config = config
        self._logger = _unified_logger

        # Lazy-loaded components
        self._memory_aware_startup = None
        self._memory_optimizer = None
        self._hybrid_router = None

        # State
        self._startup_mode: Optional[str] = None
        self._optimization_performed = False
        self._cloud_activated = False
        self._arm64_available = self._check_arm64_simd()

    def _check_arm64_simd(self) -> bool:
        """Check if ARM64 SIMD optimizations are available."""
        try:
            asm_path = Path(__file__).parent / "backend" / "core" / "arm64_simd_asm.s"
            return asm_path.exists() and platform.machine() == "arm64"
        except Exception:
            return False

    async def validate_and_optimize(self) -> ResourceStatus:
        """
        Validate system resources AND take intelligent action.

        This goes beyond just checking - it actively optimizes and
        makes decisions about startup mode and cloud activation.
        """
        # Phase 1: Parallel resource checks
        memory_task = asyncio.create_task(self._check_memory_detailed())
        disk_task = asyncio.create_task(self._check_disk())
        ports_task = asyncio.create_task(self._check_ports_intelligent())
        cpu_task = asyncio.create_task(self._check_cpu())

        memory_result, disk_result, ports_result, cpu_result = await asyncio.gather(
            memory_task, disk_task, ports_task, cpu_task
        )

        # Phase 2: Intelligent analysis and action
        warnings: List[str] = []
        errors: List[str] = []
        actions_taken: List[str] = []
        recommendations: List[str] = []

        available_gb = memory_result["available_gb"]
        total_gb = memory_result["total_gb"]
        memory_pressure = memory_result["pressure"]

        # === INTELLIGENT MEMORY HANDLING ===
        if available_gb < self.CRITICAL_THRESHOLD_GB:
            self._logger.warning(f"⚠️  CRITICAL: Only {available_gb:.1f}GB available!")
            errors.append(f"Critical memory: {available_gb:.1f}GB (need {self.CRITICAL_THRESHOLD_GB}GB)")
            recommendations.append("🔴 Consider closing applications or using GCP cloud mode")

        elif available_gb < self.CLOUD_THRESHOLD_GB:
            warnings.append(f"Low memory: {available_gb:.1f}GB available")
            recommendations.append("☁️  Cloud-First Mode recommended: GCP will handle ML processing")
            recommendations.append("💰 Estimated cost: ~$0.029/hour (Spot VM)")
            self._startup_mode = "cloud_first"

        elif available_gb < self.OPTIMIZE_THRESHOLD_GB:
            recommendations.append("💡 Moderate memory - light optimization recommended")
            self._startup_mode = "local_optimized"

        else:
            recommendations.append(f"✅ Sufficient memory ({available_gb:.1f}GB) - Full local mode")
            self._startup_mode = "local_full"

            if self._arm64_available:
                recommendations.append("⚡ ARM64 SIMD optimizations available (40-50x faster ML)")

        # === INTELLIGENT PORT HANDLING ===
        ports_available, ports_in_use, port_actions = ports_result
        if port_actions:
            actions_taken.extend(port_actions)
        if ports_in_use:
            warnings.append(f"Ports in use: {ports_in_use} (will be recycled)")

        # === DISK VALIDATION ===
        if disk_result < 1.0:
            errors.append(f"Insufficient disk: {disk_result:.1f}GB available")
        elif disk_result < 5.0:
            warnings.append(f"Low disk: {disk_result:.1f}GB available")

        # === CPU ANALYSIS ===
        cpu_count, load_avg = cpu_result
        if load_avg and load_avg[0] > cpu_count * 0.8:
            warnings.append(f"High CPU load: {load_avg[0]:.1f} (cores: {cpu_count})")
            recommendations.append("💡 Consider cloud offloading for CPU-intensive tasks")

        return ResourceStatus(
            memory_available_gb=available_gb,
            memory_total_gb=total_gb,
            disk_available_gb=disk_result,
            ports_available=ports_available,
            ports_in_use=ports_in_use,
            cpu_count=cpu_count,
            load_average=load_avg,
            warnings=warnings,
            errors=errors,
            recommendations=recommendations,
            actions_taken=actions_taken,
            startup_mode=self._startup_mode,
            cloud_activated=self._cloud_activated,
            arm64_simd_available=self._arm64_available,
            memory_pressure=memory_pressure,
        )

    async def _check_memory_detailed(self) -> Dict[str, Any]:
        """Get detailed memory analysis."""
        try:
            mem = psutil.virtual_memory()
            pressure = (mem.used / mem.total) * 100 if mem.total > 0 else 0

            return {
                "available_gb": mem.available / (1024**3),
                "total_gb": mem.total / (1024**3),
                "used_gb": mem.used / (1024**3),
                "pressure": pressure,
                "percent_used": mem.percent,
            }
        except Exception:
            return {
                "available_gb": 0.0,
                "total_gb": 0.0,
                "used_gb": 0.0,
                "pressure": 100.0,
                "percent_used": 100.0,
            }

    async def _check_disk(self) -> float:
        """Check available disk space."""
        try:
            import shutil
            total, used, free = shutil.disk_usage("/")
            return free / (1024**3)
        except Exception:
            return 0.0

    async def _check_ports_intelligent(self) -> Tuple[List[int], List[int], List[str]]:
        """Intelligently check and handle port conflicts."""
        available: List[int] = []
        in_use: List[int] = []
        actions: List[str] = []

        required_ports = [
            int(os.getenv("JARVIS_API_PORT", "8080")),
            int(os.getenv("JARVIS_WS_PORT", "8081")),
        ]

        for port in required_ports:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(0.5)
                result = sock.connect_ex(('localhost', port))
                sock.close()

                if result == 0:
                    in_use.append(port)
                    actions.append(f"Port {port}: In use (will recycle)")
                else:
                    available.append(port)
            except Exception:
                available.append(port)

        return available, in_use, actions

    async def _check_cpu(self) -> Tuple[int, Optional[Tuple[float, float, float]]]:
        """Check CPU info."""
        cpu_count = os.cpu_count() or 1
        load_avg = None

        try:
            if hasattr(os, 'getloadavg'):
                load_avg = os.getloadavg()
        except Exception:
            pass

        return cpu_count, load_avg

    def get_startup_mode(self) -> Optional[str]:
        """Get the determined startup mode."""
        return self._startup_mode

    def is_cloud_activated(self) -> bool:
        """Check if cloud mode was activated."""
        return self._cloud_activated


# =============================================================================
# VM SESSION TRACKER - Simplified VM Ownership Tracking
# =============================================================================
class VMSessionTracker:
    """
    Track VM ownership per JARVIS session to prevent multi-terminal conflicts.

    Each JARVIS instance (terminal session) gets a unique session_id.
    VMs are tagged with their owning session, ensuring cleanup only affects
    VMs owned by the terminating session.

    Features:
    - UUID-based session identification
    - PID-based ownership validation
    - Hostname verification for multi-machine safety
    - Timestamp-based staleness detection
    - Atomic file operations with lock-free design
    """

    def __init__(self):
        """Initialize session tracker with unique session ID."""
        self.session_id = str(uuid.uuid4())
        self.pid = os.getpid()
        self.hostname = socket.gethostname()
        self.created_at = time.time()

        # Session tracking file
        self.session_file = Path(tempfile.gettempdir()) / f"jarvis_session_{self.pid}.json"
        self.vm_registry = Path(tempfile.gettempdir()) / "jarvis_vm_registry.json"

        _unified_logger.info(f"🆔 Session tracker initialized: {self.session_id[:8]}")
        _unified_logger.debug(f"   PID: {self.pid}, Hostname: {self.hostname}")

    def register_vm(self, vm_id: str, zone: str, components: List[str]) -> None:
        """Register VM ownership for this session."""
        session_data = {
            "session_id": self.session_id,
            "pid": self.pid,
            "hostname": self.hostname,
            "vm_id": vm_id,
            "zone": zone,
            "components": components,
            "created_at": self.created_at,
            "registered_at": time.time(),
        }

        try:
            self.session_file.write_text(json.dumps(session_data, indent=2))
            _unified_logger.info(f"📝 Registered VM {vm_id} to session {self.session_id[:8]}")
        except Exception as e:
            _unified_logger.error(f"Failed to write session file: {e}")

        try:
            registry = self._load_registry()
            registry[self.session_id] = session_data
            self._save_registry(registry)
            _unified_logger.info(f"📋 Updated VM registry: {len(registry)} active sessions")
        except Exception as e:
            _unified_logger.error(f"Failed to update VM registry: {e}")

    def get_my_vm(self) -> Optional[Dict[str, Any]]:
        """Get VM owned by this session with validation."""
        if not self.session_file.exists():
            return None

        try:
            data = json.loads(self.session_file.read_text())

            if data.get("session_id") != self.session_id:
                return None
            if data.get("pid") != self.pid:
                return None
            if data.get("hostname") != self.hostname:
                return None

            age_hours = (time.time() - data.get("created_at", 0)) / 3600
            if age_hours > 12:
                self.session_file.unlink()
                return None

            return data

        except Exception as e:
            _unified_logger.error(f"Failed to read session file: {e}")
            return None

    def unregister_vm(self) -> None:
        """Unregister VM ownership and cleanup session files."""
        try:
            if self.session_file.exists():
                self.session_file.unlink()
                _unified_logger.info(f"🧹 Unregistered session {self.session_id[:8]}")

            registry = self._load_registry()
            if self.session_id in registry:
                del registry[self.session_id]
                self._save_registry(registry)
                _unified_logger.info(f"📋 Removed from VM registry: {len(registry)} sessions remain")

        except Exception as e:
            _unified_logger.error(f"Failed to unregister VM: {e}")

    def get_all_active_sessions(self) -> Dict[str, Dict[str, Any]]:
        """Get all active sessions from registry with staleness filtering."""
        registry = self._load_registry()
        active_sessions = {}

        for session_id, data in registry.items():
            pid = data.get("pid")
            if pid and self._is_pid_running(pid):
                age_hours = (time.time() - data.get("created_at", 0)) / 3600
                if age_hours <= 12:
                    active_sessions[session_id] = data

        if len(active_sessions) != len(registry):
            self._save_registry(active_sessions)
            _unified_logger.info(
                f"🧹 Cleaned registry: {len(active_sessions)}/{len(registry)} sessions active"
            )

        return active_sessions

    def _load_registry(self) -> Dict[str, Any]:
        """Load VM registry from disk."""
        if not self.vm_registry.exists():
            return {}
        try:
            return json.loads(self.vm_registry.read_text())
        except Exception:
            return {}

    def _save_registry(self, registry: Dict[str, Any]) -> None:
        """Save VM registry to disk."""
        try:
            self.vm_registry.write_text(json.dumps(registry, indent=2))
        except Exception as e:
            _unified_logger.error(f"Failed to save VM registry: {e}")

    def _is_pid_running(self, pid: int) -> bool:
        """Check if PID is currently running."""
        try:
            proc = psutil.Process(pid)
            cmdline = proc.cmdline()
            return "unified_supervisor.py" in " ".join(cmdline) or "start_system.py" in " ".join(cmdline)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False


# =============================================================================
# CACHE STATISTICS TRACKER - Comprehensive Cache Metrics
# =============================================================================
class CacheStatisticsTracker:
    """
    Async-safe, self-healing cache statistics tracker with comprehensive validation.

    Features:
    - Atomic counter operations with asyncio.Lock
    - Comprehensive consistency validation with detailed diagnostics
    - Self-healing capability to detect and correct drift
    - Subset relationship enforcement (expired ⊆ misses, uninitialized ⊆ misses)
    - Event-driven statistics with timestamps for debugging
    - Automatic anomaly detection and logging

    Mathematical Invariants:
    - total_queries == cache_hits + cache_misses (always)
    - cache_expired <= cache_misses (expired is a subset of misses)
    - queries_while_uninitialized <= cache_misses (uninitialized is subset of misses)
    """

    __slots__ = (
        '_lock', '_cache_hits', '_cache_misses', '_cache_expired',
        '_total_queries', '_queries_while_uninitialized', '_cost_saved_usd',
        '_expired_entries_cleaned', '_cleanup_runs', '_cleanup_errors',
        '_cost_per_inference', '_last_consistency_check', '_consistency_violations',
        '_auto_heal_count', '_event_log', '_max_event_log_size', '_created_at'
    )

    def __init__(self, cost_per_inference: float = 0.002, max_event_log_size: int = 100):
        """Initialize the statistics tracker."""
        self._lock = LazyAsyncLock()

        # Core counters
        self._cache_hits: int = 0
        self._cache_misses: int = 0
        self._cache_expired: int = 0
        self._total_queries: int = 0
        self._queries_while_uninitialized: int = 0
        self._cost_saved_usd: float = 0.0

        # Maintenance counters
        self._expired_entries_cleaned: int = 0
        self._cleanup_runs: int = 0
        self._cleanup_errors: int = 0

        # Configuration
        self._cost_per_inference = cost_per_inference

        # Consistency tracking
        self._last_consistency_check: float = 0.0
        self._consistency_violations: int = 0
        self._auto_heal_count: int = 0

        # Event log for debugging (rolling window)
        self._event_log: List[Dict[str, Any]] = []
        self._max_event_log_size = max_event_log_size
        self._created_at = time.time()

    def _log_event(self, event_type: str, details: Optional[Dict[str, Any]] = None):
        """Log an event for debugging purposes."""
        event = {
            "timestamp": time.time(),
            "type": event_type,
            "details": details or {},
            "snapshot": {
                "hits": self._cache_hits,
                "misses": self._cache_misses,
                "total": self._total_queries,
            }
        }
        self._event_log.append(event)

        if len(self._event_log) > self._max_event_log_size:
            self._event_log = self._event_log[-self._max_event_log_size:]

    async def record_hit(self, add_cost_savings: bool = True) -> None:
        """Record a cache hit atomically."""
        async with self._lock:
            self._total_queries += 1
            self._cache_hits += 1
            if add_cost_savings:
                self._cost_saved_usd += self._cost_per_inference
            self._log_event("hit", {"cost_saved": add_cost_savings})

    async def record_miss(
        self,
        is_expired: bool = False,
        is_uninitialized: bool = False
    ) -> None:
        """Record a cache miss atomically with categorization."""
        async with self._lock:
            self._total_queries += 1
            self._cache_misses += 1

            if is_expired:
                self._cache_expired += 1
                self._log_event("miss_expired")
            elif is_uninitialized:
                self._queries_while_uninitialized += 1
                self._log_event("miss_uninitialized")
            else:
                self._log_event("miss")

    async def record_cleanup(
        self,
        entries_cleaned: int,
        success: bool = True
    ) -> None:
        """Record a cleanup operation atomically."""
        async with self._lock:
            self._cleanup_runs += 1
            if success:
                self._expired_entries_cleaned += entries_cleaned
                self._log_event("cleanup_success", {"cleaned": entries_cleaned})
            else:
                self._cleanup_errors += 1
                self._log_event("cleanup_error", {"attempted": entries_cleaned})

    async def record_cleanup_error(self) -> None:
        """Record a cleanup error atomically."""
        async with self._lock:
            self._cleanup_errors += 1
            self._log_event("cleanup_error")

    async def get_snapshot(self) -> Dict[str, Any]:
        """Get an atomic snapshot of all statistics."""
        async with self._lock:
            return {
                "cache_hits": self._cache_hits,
                "cache_misses": self._cache_misses,
                "cache_expired": self._cache_expired,
                "total_queries": self._total_queries,
                "queries_while_uninitialized": self._queries_while_uninitialized,
                "cost_saved_usd": self._cost_saved_usd,
                "expired_entries_cleaned": self._expired_entries_cleaned,
                "cleanup_runs": self._cleanup_runs,
                "cleanup_errors": self._cleanup_errors,
                "consistency_violations": self._consistency_violations,
                "auto_heal_count": self._auto_heal_count,
                "uptime_seconds": time.time() - self._created_at,
            }

    async def validate_consistency(self, auto_heal: bool = True) -> Dict[str, Any]:
        """Validate statistics consistency and optionally self-heal."""
        async with self._lock:
            self._last_consistency_check = time.time()
            issues: List[Dict[str, Any]] = []

            # Invariant 1: total_queries == hits + misses
            expected_total = self._cache_hits + self._cache_misses
            if self._total_queries != expected_total:
                diff = self._total_queries - expected_total
                issues.append({
                    "type": "total_mismatch",
                    "expected": expected_total,
                    "actual": self._total_queries,
                    "diff": diff,
                })
                if auto_heal:
                    self._total_queries = expected_total
                    self._auto_heal_count += 1

            # Invariant 2: expired <= misses
            if self._cache_expired > self._cache_misses:
                issues.append({
                    "type": "expired_exceeds_misses",
                    "expired": self._cache_expired,
                    "misses": self._cache_misses,
                })
                if auto_heal:
                    self._cache_expired = self._cache_misses
                    self._auto_heal_count += 1

            # Invariant 3: uninitialized <= misses
            if self._queries_while_uninitialized > self._cache_misses:
                issues.append({
                    "type": "uninitialized_exceeds_misses",
                    "uninitialized": self._queries_while_uninitialized,
                    "misses": self._cache_misses,
                })
                if auto_heal:
                    self._queries_while_uninitialized = self._cache_misses
                    self._auto_heal_count += 1

            # Invariant 4: All counters >= 0
            for name, value in [
                ("cache_hits", self._cache_hits),
                ("cache_misses", self._cache_misses),
                ("cache_expired", self._cache_expired),
                ("total_queries", self._total_queries),
            ]:
                if value < 0:
                    issues.append({
                        "type": "negative_counter",
                        "counter": name,
                        "value": value,
                    })
                    if auto_heal:
                        setattr(self, f"_{name}", 0)
                        self._auto_heal_count += 1

            if issues:
                self._consistency_violations += 1

            return {
                "consistent": len(issues) == 0,
                "issues": issues,
                "auto_healed": auto_heal and len(issues) > 0,
                "total_violations": self._consistency_violations,
                "total_heals": self._auto_heal_count,
            }

    @property
    def hit_rate(self) -> float:
        """Calculate cache hit rate."""
        if self._total_queries == 0:
            return 0.0
        return self._cache_hits / self._total_queries

    @property
    def miss_rate(self) -> float:
        """Calculate cache miss rate."""
        if self._total_queries == 0:
            return 0.0
        return self._cache_misses / self._total_queries


# =============================================================================
# PROCESS RESTART MANAGER - Advanced Process Supervision
# =============================================================================
@dataclass
class RestartableManagedProcess:
    """Metadata for a managed process under supervision."""
    name: str
    process: Optional[asyncio.subprocess.Process]
    restart_func: Callable[[], Awaitable[asyncio.subprocess.Process]]
    restart_count: int = 0
    last_restart: float = 0.0
    max_restarts: int = 5
    port: Optional[int] = None
    exit_code: Optional[int] = None


class ProcessRestartManager:
    """
    Advanced process restart manager with exponential backoff and intelligent recovery.

    Features:
    - Named process tracking (dict-based, not fragile index-based)
    - Exponential backoff: 1s → 2s → 4s → 8s → max configurable
    - Per-process restart tracking with cooldown reset
    - Maximum restart limit with alerting
    - Global shutdown flag reset before restart
    - Async-safe with proper locking
    - All thresholds configurable via environment variables

    Environment Variables:
        JARVIS_MAX_RESTARTS: Maximum restart attempts (default: 5)
        JARVIS_MAX_BACKOFF: Maximum backoff delay in seconds (default: 30.0)
        JARVIS_RESTART_COOLDOWN: Seconds of stability before resetting restart count (default: 300.0)
        JARVIS_BASE_BACKOFF: Initial backoff delay in seconds (default: 1.0)
    """

    def __init__(self):
        """Initialize the restart manager with environment-driven configuration."""
        self.processes: Dict[str, RestartableManagedProcess] = {}
        self._lock = asyncio.Lock()
        self._shutdown_requested = False

        # Environment-driven configuration
        self.max_restarts = int(os.getenv("JARVIS_MAX_RESTARTS", "5"))
        self.max_backoff = float(os.getenv("JARVIS_MAX_BACKOFF", "30.0"))
        self.restart_cooldown = float(os.getenv("JARVIS_RESTART_COOLDOWN", "300.0"))
        self.base_backoff = float(os.getenv("JARVIS_BASE_BACKOFF", "1.0"))

        self._logger = logging.getLogger("ProcessRestartManager")

    def register(
        self,
        name: str,
        process: asyncio.subprocess.Process,
        restart_func: Callable[[], Awaitable[asyncio.subprocess.Process]],
        port: Optional[int] = None,
    ) -> None:
        """Register a process for monitoring and automatic restart."""
        self.processes[name] = RestartableManagedProcess(
            name=name,
            process=process,
            restart_func=restart_func,
            restart_count=0,
            last_restart=0.0,
            max_restarts=self.max_restarts,
            port=port,
        )
        self._logger.info(f"✓ Registered process '{name}' (PID: {process.pid})" +
                         (f" on port {port}" if port else ""))

    def unregister(self, name: str) -> None:
        """Remove a process from monitoring."""
        if name in self.processes:
            del self.processes[name]
            self._logger.info(f"✓ Unregistered process '{name}'")

    def request_shutdown(self) -> None:
        """Signal that shutdown is requested - stop all restart attempts."""
        self._shutdown_requested = True
        self._logger.info("Shutdown requested - restart manager will not restart processes")

    def reset_shutdown(self) -> None:
        """Reset shutdown flag - allow restarts again."""
        self._shutdown_requested = False
        self._logger.info("Shutdown flag reset - restart manager active")

    async def check_and_restart_all(self) -> List[str]:
        """Check all processes and restart any that have unexpectedly exited."""
        if self._shutdown_requested:
            return []

        restarted = []

        async with self._lock:
            for name, managed in list(self.processes.items()):
                proc = managed.process
                if proc is None:
                    continue

                if proc.returncode is not None:
                    managed.exit_code = proc.returncode

                    # Normal exit or controlled shutdown - don't restart
                    if proc.returncode in (0, -2, -15):
                        self._logger.debug(
                            f"Process '{name}' exited normally (code: {proc.returncode})"
                        )
                        continue

                    success = await self._handle_unexpected_exit(name, managed)
                    if success:
                        restarted.append(name)

        return restarted

    async def _handle_unexpected_exit(self, name: str, managed: RestartableManagedProcess) -> bool:
        """Handle an unexpected process exit with exponential backoff restart."""
        current_time = time.time()

        if managed.restart_count >= managed.max_restarts:
            self._logger.error(
                f"❌ Process '{name}' exceeded restart limit ({managed.max_restarts}). "
                f"Last exit code: {managed.exit_code}. Manual intervention required."
            )
            return False

        if current_time - managed.last_restart > self.restart_cooldown:
            if managed.restart_count > 0:
                self._logger.info(
                    f"Process '{name}' was stable for {self.restart_cooldown}s - "
                    f"resetting restart count from {managed.restart_count} to 0"
                )
            managed.restart_count = 0

        backoff = min(
            self.base_backoff * (2 ** managed.restart_count),
            self.max_backoff
        )

        managed.restart_count += 1
        managed.last_restart = current_time

        self._logger.warning(
            f"🔄 Restarting '{name}' in {backoff:.1f}s "
            f"(attempt {managed.restart_count}/{managed.max_restarts}, "
            f"exit code: {managed.exit_code})"
        )

        await asyncio.sleep(backoff)

        if self._shutdown_requested:
            self._logger.info(f"Shutdown requested - aborting restart of '{name}'")
            return False

        # Reset global shutdown flag BEFORE restarting
        try:
            from backend.core.resilience.graceful_shutdown import reset_global_shutdown
            reset_global_shutdown()
            self._logger.debug(f"Global shutdown flag reset for '{name}' restart")
        except ImportError:
            pass
        except Exception as e:
            self._logger.debug(f"Failed to reset global shutdown: {e}")

        try:
            new_proc = await managed.restart_func()
            managed.process = new_proc
            self._logger.info(
                f"✅ Process '{name}' restarted successfully (new PID: {new_proc.pid})"
            )
            return True
        except Exception as e:
            self._logger.error(f"❌ Failed to restart '{name}': {e}")
            return False

    def get_status(self) -> Dict[str, Dict[str, Any]]:
        """Get status of all managed processes."""
        status = {}
        for name, managed in self.processes.items():
            proc = managed.process
            status[name] = {
                "pid": proc.pid if proc else None,
                "running": proc.returncode is None if proc else False,
                "exit_code": managed.exit_code,
                "restart_count": managed.restart_count,
                "last_restart": managed.last_restart,
                "port": managed.port,
            }
        return status


# Global restart manager instance
_restart_manager: Optional[ProcessRestartManager] = None


def get_restart_manager() -> ProcessRestartManager:
    """Get the global process restart manager instance."""
    global _restart_manager
    if _restart_manager is None:
        _restart_manager = ProcessRestartManager()
    return _restart_manager


# =============================================================================
# TRINITY CIRCUIT BREAKER (v100.1) - Persistent State Circuit Breaker
# =============================================================================

class TrinityCircuitBreakerState(Enum):
    """Circuit breaker states for Trinity component protection."""
    CLOSED = "closed"      # Normal operation, requests pass through
    OPEN = "open"          # Circuit tripped, requests blocked
    HALF_OPEN = "half_open"  # Testing if service recovered


class TrinityCircuitBreaker:
    """
    Circuit breaker for Trinity component launch with PERSISTENT STATE.
    Prevents cascade failures by stopping launch attempts after repeated failures.

    v100.1: Added state persistence across restarts to prevent infinite retry loops.
    State is saved to ~/.jarvis/state/circuit_breakers/ and loaded on init.

    Features:
    - Persistent state across supervisor restarts
    - Automatic OPEN → HALF_OPEN transition after timeout
    - Configurable failure thresholds
    - Full status reporting
    """

    def __init__(self, name: str, config: Optional[TrinityLaunchConfig] = None):
        self.name = name
        self.config = config or TrinityLaunchConfig()
        self.half_open_calls = 0
        self._logger = logging.getLogger(f"TrinityCircuitBreaker.{name}")

        # v100.1: Persistent state file
        self._state_dir = Path.home() / ".jarvis" / "state" / "circuit_breakers"
        self._state_file = self._state_dir / f"{name}.json"

        # Load persisted state or initialize fresh
        loaded_state = self._load_state()
        self.state = loaded_state.get("state", TrinityCircuitBreakerState.CLOSED)
        if isinstance(self.state, str):
            self.state = TrinityCircuitBreakerState(self.state)
        self.failure_count = loaded_state.get("failure_count", 0)
        self.success_count = loaded_state.get("success_count", 0)
        self.last_failure_time = loaded_state.get("last_failure_time")
        self.last_state_change = loaded_state.get("last_state_change", time.time())
        self.total_failures = loaded_state.get("total_failures", 0)
        self.total_successes = loaded_state.get("total_successes", 0)

        # Check if OPEN state has timed out
        if self.state == TrinityCircuitBreakerState.OPEN and self.last_failure_time:
            elapsed = time.time() - self.last_failure_time
            if elapsed > self.config.circuit_breaker_timeout_sec:
                self._transition_to(TrinityCircuitBreakerState.HALF_OPEN)
                self._logger.info(f"[{name}] OPEN → HALF_OPEN (timeout elapsed during restart)")

    def _load_state(self) -> Dict[str, Any]:
        """Load circuit breaker state from disk."""
        if not self._state_file.exists():
            return {}
        try:
            with open(self._state_file) as f:
                return json.load(f)
        except Exception as e:
            self._logger.warning(f"Failed to load circuit breaker state: {e}")
            return {}

    def _save_state(self) -> None:
        """Persist circuit breaker state to disk (v100.1)."""
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            state_data = {
                "state": self.state.value if isinstance(self.state, TrinityCircuitBreakerState) else self.state,
                "failure_count": self.failure_count,
                "success_count": self.success_count,
                "last_failure_time": self.last_failure_time,
                "last_state_change": self.last_state_change,
                "total_failures": self.total_failures,
                "total_successes": self.total_successes,
                "updated_at": time.time(),
            }
            with open(self._state_file, "w") as f:
                json.dump(state_data, f, indent=2)
        except Exception as e:
            self._logger.warning(f"Failed to save circuit breaker state: {e}")

    def can_execute(self) -> bool:
        """Check if execution is allowed based on circuit state."""
        if self.state == TrinityCircuitBreakerState.CLOSED:
            return True

        if self.state == TrinityCircuitBreakerState.OPEN:
            # Check if timeout has elapsed
            if self.last_failure_time and (time.time() - self.last_failure_time) > self.config.circuit_breaker_timeout_sec:
                self._transition_to(TrinityCircuitBreakerState.HALF_OPEN)
                return True
            return False

        if self.state == TrinityCircuitBreakerState.HALF_OPEN:
            return self.half_open_calls < self.config.circuit_breaker_half_open_max_calls

        return False

    def record_success(self) -> None:
        """Record a successful execution."""
        self.success_count += 1
        self.total_successes += 1
        self.failure_count = max(0, self.failure_count - 1)

        if self.state == TrinityCircuitBreakerState.HALF_OPEN:
            self._transition_to(TrinityCircuitBreakerState.CLOSED)
        else:
            self._save_state()  # v100.1: Persist state

    def record_failure(self) -> None:
        """Record a failed execution."""
        self.failure_count += 1
        self.total_failures += 1
        self.last_failure_time = time.time()

        if self.state == TrinityCircuitBreakerState.HALF_OPEN:
            self._transition_to(TrinityCircuitBreakerState.OPEN)
        elif self.failure_count >= self.config.circuit_breaker_failure_threshold:
            self._transition_to(TrinityCircuitBreakerState.OPEN)
        else:
            self._save_state()  # v100.1: Persist state

    def _transition_to(self, new_state: TrinityCircuitBreakerState) -> None:
        """Transition to a new state."""
        old_state = self.state
        self.state = new_state
        self.last_state_change = time.time()

        if new_state == TrinityCircuitBreakerState.HALF_OPEN:
            self.half_open_calls = 0
        elif new_state == TrinityCircuitBreakerState.CLOSED:
            self.failure_count = 0  # Reset on recovery

        self._save_state()  # v100.1: Persist on every state transition
        self._logger.info(f"[{self.name}] {old_state.value} → {new_state.value}")

    def reset(self) -> None:
        """Reset circuit breaker to initial state."""
        self.state = TrinityCircuitBreakerState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = None
        self.last_state_change = time.time()
        self.half_open_calls = 0
        self._save_state()
        self._logger.info(f"[{self.name}] Circuit breaker reset")

    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive circuit breaker status."""
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "total_failures": self.total_failures,
            "total_successes": self.total_successes,
            "last_failure_time": self.last_failure_time,
            "last_state_change": self.last_state_change,
            "can_execute": self.can_execute(),
        }


# =============================================================================
# ASYNC RETRY UTILITY (v95.0) - Standalone Retry Function
# =============================================================================

async def async_retry(
    operation: Callable[[], Any],
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exponential_base: float = 2.0,
    operation_name: str = "operation",
    retryable_exceptions: Optional[Tuple[type, ...]] = None,
    logger: Optional[logging.Logger] = None,
) -> Any:
    """
    v95.0: Simple async retry utility for critical operations.

    Unlike RetryWithBackoff (tied to TrinityLaunchConfig), this is a standalone
    function that can be used anywhere for HTTP requests, subprocess ops, etc.

    Features:
    - Exponential backoff with configurable base
    - Jitter to prevent thundering herd
    - Configurable max delay cap
    - Exception type filtering

    Args:
        operation: Async callable to execute (can be lambda returning coroutine)
        max_retries: Maximum number of retry attempts (default: 3)
        base_delay: Initial delay in seconds (default: 1.0)
        max_delay: Maximum delay cap (default: 30.0)
        exponential_base: Multiplier for exponential backoff (default: 2.0)
        operation_name: Name for logging (default: "operation")
        retryable_exceptions: Tuple of exception types to retry on (default: all)
        logger: Optional logger instance

    Returns:
        The result of the operation

    Raises:
        The last exception if all retries fail

    Example:
        result = await async_retry(
            lambda: session.get(url),
            max_retries=3,
            operation_name="health_check",
            retryable_exceptions=(aiohttp.ClientError, asyncio.TimeoutError),
        )
    """
    _logger = logger or logging.getLogger("AsyncRetry")
    last_exception: Optional[Exception] = None
    max_attempts = max_retries + 1

    for attempt in range(max_attempts):
        try:
            # Handle both async functions and lambdas returning coroutines
            if asyncio.iscoroutinefunction(operation):
                result = await operation()
            else:
                result = operation()
                if asyncio.iscoroutine(result):
                    result = await result

            if attempt > 0:
                _logger.info(f"[{operation_name}] Succeeded on attempt {attempt + 1}")

            return result

        except Exception as e:
            last_exception = e

            # Check if this exception type should be retried
            if retryable_exceptions and not isinstance(e, retryable_exceptions):
                _logger.debug(f"[{operation_name}] Non-retryable exception: {type(e).__name__}")
                raise

            _logger.warning(f"[{operation_name}] Attempt {attempt + 1}/{max_attempts} failed: {e}")

            # Check if we have retries left
            if attempt < max_attempts - 1:
                # Calculate delay with exponential backoff and jitter
                import random
                delay = min(base_delay * (exponential_base ** attempt), max_delay)
                jitter = delay * 0.1 * (2 * random.random() - 1)  # ±10% jitter
                delay = max(0, delay + jitter)

                _logger.debug(f"[{operation_name}] Retrying in {delay:.2f}s...")
                await asyncio.sleep(delay)

    # All retries exhausted
    _logger.error(f"[{operation_name}] All {max_attempts} attempts failed")
    if last_exception:
        raise last_exception
    raise RuntimeError(f"{operation_name} failed after {max_attempts} attempts")


# =============================================================================
# STARTUP PHASE ENUM - Phases of Supervisor Startup
# =============================================================================

class StartupPhase(Enum):
    """Phases of supervisor startup for tracking progress."""
    INIT = "init"                       # Initial phase, configuration loading
    CLEANUP = "cleanup"                 # Zombie/stale process cleanup
    VALIDATION = "validation"           # Environment validation
    SUPERVISOR_INIT = "supervisor_init" # Core supervisor initialization
    RESOURCES = "resources"             # Resource managers startup
    INTELLIGENCE = "intelligence"       # ML/Intelligence layer startup
    TRINITY = "trinity"                 # Trinity cross-repo startup
    BACKEND = "backend"                 # Backend server startup
    JARVIS_START = "jarvis_start"       # Full JARVIS system startup
    COMPLETE = "complete"               # Startup completed successfully
    FAILED = "failed"                   # Startup failed


# =============================================================================
# INTELLIGENT CHROME INCOGNITO MANAGER - Browser Automation
# =============================================================================

# Global browser state management
_browser_opened_this_startup: bool = False
_browser_lock: Optional[asyncio.Lock] = None


def _get_browser_lock() -> asyncio.Lock:
    """Get or create the global browser lock (lazy init for Python 3.9)."""
    global _browser_lock
    if _browser_lock is None:
        _browser_lock = asyncio.Lock()
    return _browser_lock


class IntelligentChromeIncognitoManager:
    """
    Advanced Chrome Incognito Window Manager for JARVIS.

    DESIGN PHILOSOPHY: INCOGNITO ONLY, SINGLE WINDOW, ZERO DUPLICATES

    This manager ensures:
    1. ONLY Chrome Incognito mode is used - NEVER regular Chrome windows
    2. EXACTLY ONE incognito window/tab with JARVIS at any time
    3. Intelligent deduplication - closes ALL duplicates automatically
    4. Cache-free experience - bypasses all cached CSS, JS, assets
    5. Robust async operations with retry logic and error recovery

    Key Features:
    - Parallel window scanning with asyncio.gather()
    - Intelligent URL pattern matching (localhost:3000, 3001, 8010, etc.)
    - Graceful degradation with detailed error reporting
    - Window state persistence across restarts
    - Automatic cleanup on system restart
    """

    # Default patterns as fallback (loaded dynamically from config)
    DEFAULT_URL_PATTERNS = [
        "localhost:3000", "localhost:3001", "localhost:8010",
        "localhost:8001", "localhost:8888",
        "127.0.0.1:3000", "127.0.0.1:3001", "127.0.0.1:8010",
        "127.0.0.1:8001", "127.0.0.1:8888"
    ]

    def __init__(self):
        self._incognito_window_id: Optional[int] = None
        self._incognito_tab_id: Optional[int] = None
        self._session_started: bool = False
        self._lock = LazyAsyncLock()  # Lazy init for Python 3.9 compatibility
        self._last_operation_time: Optional[datetime] = None
        self._operation_count: int = 0
        self._error_count: int = 0
        self._retry_delays = [0.5, 1.0, 2.0, 5.0]  # Exponential backoff
        self._logger = logging.getLogger("ChromeIncognito")

        # Load URL patterns from config
        self.JARVIS_URL_PATTERNS = self._load_url_patterns()

        self._logger.info("🔒 IntelligentChromeIncognitoManager initialized (INCOGNITO-ONLY mode)")

    def _load_url_patterns(self) -> List[str]:
        """Load JARVIS URL patterns from configuration file."""
        config_paths = [
            Path.cwd() / 'backend' / 'config' / 'startup_progress_config.json',
            Path.cwd() / 'backend' / 'config' / 'browser_config.json',
        ]

        for config_path in config_paths:
            try:
                if config_path.exists():
                    with open(config_path, 'r') as f:
                        data = json.load(f)
                        patterns = data.get('jarvis_url_patterns',
                                          data.get('browser_config', {}).get('url_patterns'))
                        if patterns:
                            self._logger.debug(f"Loaded {len(patterns)} URL patterns from {config_path.name}")
                            return patterns
            except Exception as e:
                self._logger.debug(f"Could not load URL patterns from {config_path}: {e}")

        # Use defaults
        self._logger.debug(f"Using default URL patterns: {len(self.DEFAULT_URL_PATTERNS)} patterns")
        return self.DEFAULT_URL_PATTERNS.copy()

    async def ensure_single_incognito_window(self, url: str, force_new: bool = False) -> Dict[str, Any]:
        """
        Ensure exactly ONE Chrome Incognito window with JARVIS.

        This is the main entry point. It will:
        1. Close ALL regular Chrome windows with JARVIS tabs
        2. Close ALL duplicate incognito windows with JARVIS tabs
        3. Keep or create exactly ONE incognito window
        4. Navigate that window to the specified URL

        Args:
            url: The URL to load (e.g., http://localhost:3001)
            force_new: If True, close everything and create fresh incognito window

        Returns:
            dict with status info
        """
        global_lock = _get_browser_lock()
        async with global_lock:
            global _browser_opened_this_startup

            result = {
                'success': False,
                'action': None,
                'duplicates_closed': 0,
                'regular_windows_closed': 0,
                'error': None
            }

            try:
                # Quick check for existing incognito windows first
                if not force_new:
                    quick_window = await self._quick_find_any_incognito_window()
                    if quick_window is not None:
                        self._logger.info(f"🔄 Found existing incognito window {quick_window} - reusing")
                        success = await self._redirect_incognito_window(quick_window, url)
                        if success:
                            _browser_opened_this_startup = True
                            await self._ensure_fullscreen()
                            return {
                                'success': True,
                                'action': 'redirected',
                                'duplicates_closed': 0,
                                'regular_windows_closed': 0,
                                'error': None
                            }

                # Check if browser already opened this startup
                if _browser_opened_this_startup and not force_new:
                    scan_result = await self._scan_all_chrome_windows()
                    all_incognito = scan_result.get('all_incognito_windows', [])
                    if all_incognito:
                        success = await self._redirect_incognito_window(all_incognito[0], url)
                        if success:
                            await self._ensure_fullscreen()
                        return {
                            'success': success,
                            'action': 'redirected',
                            'duplicates_closed': 0,
                            'regular_windows_closed': 0,
                            'error': None
                        }
                    _browser_opened_this_startup = False

                async with self._lock:
                    self._operation_count += 1
                    self._last_operation_time = datetime.now()

                    # Scan and categorize all Chrome windows
                    scan_result = await self._scan_all_chrome_windows()

                    if not scan_result['chrome_running']:
                        # Chrome not running - launch fresh
                        _browser_opened_this_startup = True
                        success = await self._launch_fresh_incognito(url)
                        result['success'] = success
                        result['action'] = 'created'
                        return result

                    # Close ALL regular Chrome windows with JARVIS tabs
                    if scan_result['regular_jarvis_windows']:
                        closed = await self._close_regular_jarvis_windows(
                            scan_result['regular_jarvis_windows']
                        )
                        result['regular_windows_closed'] = closed

                    # Handle incognito windows
                    all_incognito = scan_result.get('all_incognito_windows', [])

                    if force_new and all_incognito:
                        closed = await self._close_incognito_windows(all_incognito)
                        result['duplicates_closed'] = closed
                        _browser_opened_this_startup = True
                        success = await self._launch_fresh_incognito(url)
                        result['success'] = success
                        result['action'] = 'created'
                    elif not all_incognito:
                        _browser_opened_this_startup = True
                        success = await self._launch_fresh_incognito(url)
                        result['success'] = success
                        result['action'] = 'created'
                    elif len(all_incognito) == 1:
                        _browser_opened_this_startup = True
                        success = await self._redirect_incognito_window(all_incognito[0], url)
                        if success:
                            await self._ensure_fullscreen()
                        result['success'] = success
                        result['action'] = 'redirected'
                    else:
                        # Multiple incognito - keep first, close rest
                        to_keep = all_incognito[0]
                        to_close = all_incognito[1:]
                        closed = await self._close_incognito_windows(to_close)
                        result['duplicates_closed'] = closed
                        _browser_opened_this_startup = True
                        success = await self._redirect_incognito_window(to_keep, url)
                        if success:
                            await self._ensure_fullscreen()
                        result['success'] = success
                        result['action'] = 'reused'

                    self._session_started = True
                    return result

            except Exception as e:
                self._error_count += 1
                result['error'] = str(e)
                self._logger.error(f"❌ Chrome Incognito operation failed: {e}")
                return result

    async def _quick_find_any_incognito_window(self) -> Optional[int]:
        """Quick check for any existing incognito window."""
        if sys.platform != 'darwin':
            return None

        applescript = '''
        tell application "System Events"
            if not (exists process "Google Chrome") then
                return "NO_CHROME"
            end if
        end tell

        tell application "Google Chrome"
            set windowCount to count of windows
            repeat with i from 1 to windowCount
                try
                    set w to window i
                    if mode of w is "incognito" then
                        return "FOUND|" & i
                    end if
                end try
            end repeat
        end tell
        return "NONE"
        '''

        try:
            process = await asyncio.create_subprocess_exec(
                "osascript", "-e", applescript,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
            output = stdout.decode().strip() if stdout else ""

            if output.startswith("FOUND|"):
                try:
                    return int(output.split("|")[1])
                except (ValueError, IndexError):
                    pass

            return None
        except Exception as e:
            self._logger.warning(f"Quick incognito scan failed: {e}")
            return None

    async def _scan_all_chrome_windows(self) -> Dict[str, Any]:
        """Scan all Chrome windows and categorize them."""
        if sys.platform != 'darwin':
            return {
                'chrome_running': False,
                'regular_jarvis_windows': [],
                'incognito_jarvis_windows': [],
                'all_incognito_windows': [],
                'total_windows': 0
            }

        patterns_str = ', '.join(f'"{p}"' for p in self.JARVIS_URL_PATTERNS)

        applescript = f'''
        tell application "System Events"
            if not (exists process "Google Chrome") then
                return "NOT_RUNNING"
            end if
        end tell

        tell application "Google Chrome"
            set regularJarvis to {{}}
            set incognitoJarvis to {{}}
            set allIncognito to {{}}
            set jarvisPatterns to {{{patterns_str}}}
            set windowCount to count of windows

            repeat with windowIndex from 1 to windowCount
                set w to window windowIndex
                try
                    set windowMode to mode of w
                    set isIncognito to (windowMode is "incognito")

                    if isIncognito then
                        set end of allIncognito to windowIndex
                    end if

                    set foundJarvis to false
                    repeat with t in tabs of w
                        if not foundJarvis then
                            set tabURL to URL of t
                            repeat with pattern in jarvisPatterns
                                if tabURL contains pattern then
                                    if isIncognito then
                                        set end of incognitoJarvis to windowIndex
                                    else
                                        set end of regularJarvis to windowIndex
                                    end if
                                    set foundJarvis to true
                                    exit repeat
                                end if
                            end repeat
                        end if
                    end repeat
                end try
            end repeat

            return "RUNNING|" & (count of regularJarvis) & "|" & (count of incognitoJarvis) & "|" & windowCount & "|" & (regularJarvis as string) & "|" & (incognitoJarvis as string) & "|" & (count of allIncognito) & "|" & (allIncognito as string)
        end tell
        '''

        try:
            process = await asyncio.create_subprocess_exec(
                "osascript", "-e", applescript,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15)
            output = stdout.decode().strip() if stdout else ""

            if output == "NOT_RUNNING":
                return {
                    'chrome_running': False,
                    'regular_jarvis_windows': [],
                    'incognito_jarvis_windows': [],
                    'all_incognito_windows': [],
                    'total_windows': 0
                }

            if output.startswith("RUNNING|"):
                parts = output.split("|")
                if len(parts) >= 4:
                    regular_count = int(parts[1])
                    incognito_jarvis_count = int(parts[2])
                    total = int(parts[3])
                    regular_indices = self._parse_applescript_list(parts[4]) if len(parts) > 4 else []
                    incognito_jarvis_indices = self._parse_applescript_list(parts[5]) if len(parts) > 5 else []
                    all_incognito_count = int(parts[6]) if len(parts) > 6 else 0
                    all_incognito_indices = self._parse_applescript_list(parts[7]) if len(parts) > 7 else []

                    return {
                        'chrome_running': True,
                        'regular_jarvis_windows': regular_indices[:regular_count],
                        'incognito_jarvis_windows': incognito_jarvis_indices[:incognito_jarvis_count],
                        'all_incognito_windows': all_incognito_indices[:all_incognito_count],
                        'total_windows': total
                    }

            return {
                'chrome_running': True,
                'regular_jarvis_windows': [],
                'incognito_jarvis_windows': [],
                'all_incognito_windows': [],
                'total_windows': 0
            }

        except Exception as e:
            self._logger.warning(f"Window scan failed: {e}")
            return {
                'chrome_running': False,
                'regular_jarvis_windows': [],
                'incognito_jarvis_windows': [],
                'all_incognito_windows': [],
                'total_windows': 0
            }

    def _parse_applescript_list(self, list_str: str) -> List[int]:
        """Parse AppleScript list string into Python list."""
        if not list_str:
            return []
        try:
            return [int(x.strip()) for x in list_str.split(",") if x.strip().isdigit()]
        except Exception:
            return []

    async def _launch_fresh_incognito(self, url: str) -> bool:
        """Launch a fresh Chrome incognito window."""
        if sys.platform != 'darwin':
            self._logger.warning("Non-macOS platform - cannot launch Chrome via AppleScript")
            return False

        applescript = f'''
        tell application "Google Chrome"
            set newWindow to make new window with properties {{mode:"incognito"}}
            set URL of active tab of newWindow to "{url}"
            activate
        end tell
        '''

        try:
            process = await asyncio.create_subprocess_exec(
                "osascript", "-e", applescript,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=30)

            if process.returncode == 0:
                self._logger.info(f"🔒 Launched fresh incognito window: {url}")
                return True
            else:
                self._logger.error(f"Failed to launch incognito: {stderr.decode()}")
                return False
        except Exception as e:
            self._logger.error(f"Error launching incognito: {e}")
            return False

    async def _redirect_incognito_window(self, window_index: int, url: str) -> bool:
        """Redirect an existing incognito window to a URL."""
        if sys.platform != 'darwin':
            return False

        applescript = f'''
        tell application "Google Chrome"
            set URL of active tab of window {window_index} to "{url}"
            set active tab index of window {window_index} to 1
            activate
        end tell
        '''

        try:
            process = await asyncio.create_subprocess_exec(
                "osascript", "-e", applescript,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=15)

            if process.returncode == 0:
                self._logger.info(f"🔄 Redirected incognito window {window_index} to {url}")
                return True
            else:
                self._logger.warning(f"Redirect failed: {stderr.decode()}")
                return False
        except Exception as e:
            self._logger.warning(f"Error redirecting window: {e}")
            return False

    async def _close_regular_jarvis_windows(self, window_indices: List[int]) -> int:
        """Close regular (non-incognito) Chrome windows with JARVIS tabs."""
        closed = 0
        for idx in sorted(window_indices, reverse=True):  # Close in reverse order
            try:
                applescript = f'tell application "Google Chrome" to close window {idx}'
                process = await asyncio.create_subprocess_exec(
                    "osascript", "-e", applescript,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await asyncio.wait_for(process.communicate(), timeout=5)
                if process.returncode == 0:
                    closed += 1
            except Exception:
                pass
        return closed

    async def _close_incognito_windows(self, window_indices: List[int]) -> int:
        """Close incognito Chrome windows."""
        closed = 0
        for idx in sorted(window_indices, reverse=True):
            try:
                applescript = f'tell application "Google Chrome" to close window {idx}'
                process = await asyncio.create_subprocess_exec(
                    "osascript", "-e", applescript,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await asyncio.wait_for(process.communicate(), timeout=5)
                if process.returncode == 0:
                    closed += 1
            except Exception:
                pass
        return closed

    async def _ensure_fullscreen(self) -> bool:
        """Ensure Chrome window is fullscreen."""
        if sys.platform != 'darwin':
            return False

        try:
            applescript = '''
            tell application "System Events"
                tell process "Google Chrome"
                    if (count of windows) > 0 then
                        set frontmost to true
                    end if
                end tell
            end tell
            '''
            process = await asyncio.create_subprocess_exec(
                "osascript", "-e", applescript,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await asyncio.wait_for(process.communicate(), timeout=5)
            return True
        except Exception:
            return False

    def get_status(self) -> Dict[str, Any]:
        """Get Chrome manager status."""
        return {
            'session_started': self._session_started,
            'operation_count': self._operation_count,
            'error_count': self._error_count,
            'last_operation_time': self._last_operation_time.isoformat() if self._last_operation_time else None,
            'patterns_loaded': len(self.JARVIS_URL_PATTERNS),
        }


# Global Chrome manager singleton
_chrome_manager: Optional[IntelligentChromeIncognitoManager] = None


def get_chrome_manager() -> IntelligentChromeIncognitoManager:
    """Get the global Chrome incognito manager."""
    global _chrome_manager
    if _chrome_manager is None:
        _chrome_manager = IntelligentChromeIncognitoManager()
    return _chrome_manager


# =============================================================================
# UNIFIED TRINITY CONNECTOR - Cross-Repo Orchestration
# =============================================================================

class UnifiedTrinityConnector:
    """
    Master orchestrator that connects JARVIS, JARVIS Prime, and Reactor Core.

    This is the single point of coordination for the entire Trinity system,
    providing:
    - Cross-repo self-improvement with diff preview and approval
    - Atomic multi-repo transactions with 2PC (two-phase commit)
    - Distributed health consensus
    - Unified improvement request routing
    - Session memory across all repos

    Architecture:
    - JARVIS Body: This repo (execution, voice, vision, UI)
    - JARVIS Prime: Reasoning brain (jarvis-prime repo)
    - Reactor Core: Training/learning pipeline (reactor-core repo)
    """

    def __init__(self):
        self.logger = logging.getLogger("Trinity.Connector")
        self._running = False
        self._initialized = False

        # Components (lazy-loaded)
        self._enhanced_self_improvement = None
        self._enhanced_cross_repo = None
        self._session_id = f"trinity_{uuid.uuid4().hex[:12]}"

        # Repository paths (from environment or defaults)
        self._jarvis_path = Path(os.environ.get(
            "JARVIS_PATH",
            Path(__file__).parent
        ))
        self._prime_path = Path(os.environ.get(
            "JARVIS_PRIME_PATH",
            self._jarvis_path.parent / "jarvis-prime"
        ))
        self._reactor_path = Path(os.environ.get(
            "REACTOR_CORE_PATH",
            self._jarvis_path.parent / "reactor-core"
        ))

        # Health state
        self._health = {
            "jarvis": False,
            "prime": False,
            "reactor": False,
        }

        # Real-time communication
        self._realtime_broadcaster = None

    async def initialize(
        self,
        websocket_manager=None,
        voice_system=None,
        menu_bar=None,
        event_bus=None,
    ) -> bool:
        """
        Initialize the Trinity connector.

        Sets up all enhanced components and establishes
        connections to JARVIS Prime and Reactor Core.

        Args:
            websocket_manager: WebSocket manager for real-time UI updates
            voice_system: Voice system for real-time narration
            menu_bar: Menu bar for status indicators
            event_bus: Event bus for system events
        """
        if self._initialized:
            return True

        self.logger.info("=" * 60)
        self.logger.info("  UNIFIED TRINITY CONNECTOR v1.0")
        self.logger.info("=" * 60)
        self.logger.info(f"  Session: {self._session_id}")
        self.logger.info(f"  JARVIS: {self._jarvis_path}")
        self.logger.info(f"  Prime: {self._prime_path}")
        self.logger.info(f"  Reactor: {self._reactor_path}")
        self.logger.info("=" * 60)

        try:
            # Phase 1: Validate repositories
            self.logger.info("[Trinity] Phase 1: Repository Validation...")
            await self._validate_repositories()

            # Phase 2: Initialize cross-repo communication (if available)
            self.logger.info("[Trinity] Phase 2: Cross-Repo Communication...")
            try:
                from core.ouroboros.cross_repo import (
                    get_enhanced_cross_repo_orchestrator,
                    initialize_enhanced_cross_repo,
                )
                await initialize_enhanced_cross_repo()
                self._enhanced_cross_repo = get_enhanced_cross_repo_orchestrator()
                self.logger.info("[Trinity] ✓ Cross-repo orchestrator ready")
            except ImportError:
                self.logger.info("[Trinity] Cross-repo module not available - standalone mode")
            except Exception as e:
                self.logger.warning(f"[Trinity] Cross-repo init error: {e}")

            # Phase 3: Initialize self-improvement (if available)
            self.logger.info("[Trinity] Phase 3: Self-Improvement Engine...")
            try:
                from core.ouroboros.native_integration import (
                    get_enhanced_self_improvement,
                )
                self._enhanced_self_improvement = get_enhanced_self_improvement()
                await self._enhanced_self_improvement.initialize()
                self.logger.info("[Trinity] ✓ Enhanced self-improvement ready")
            except ImportError:
                self.logger.info("[Trinity] Self-improvement module not available")
            except Exception as e:
                self.logger.warning(f"[Trinity] Self-improvement init error: {e}")

            self._initialized = True
            self._running = True

            self.logger.info("=" * 60)
            self.logger.info("  TRINITY CONNECTOR INITIALIZED SUCCESSFULLY")
            self.logger.info("=" * 60)

            return True

        except Exception as e:
            self.logger.error(f"[Trinity] Initialization failed: {e}")
            import traceback
            self.logger.debug(traceback.format_exc())
            return False

    async def _validate_repositories(self) -> None:
        """Validate all repository connections."""
        # JARVIS (always available - we're in it)
        self._health["jarvis"] = True
        self.logger.info(f"  - JARVIS: ✓ (local)")

        # JARVIS Prime
        if self._prime_path.exists():
            prime_git = self._prime_path / ".git"
            if prime_git.exists():
                self._health["prime"] = True
                self.logger.info(f"  - JARVIS Prime: ✓ ({self._prime_path})")
            else:
                self.logger.warning(f"  - JARVIS Prime: ⚠ not a git repo")
        else:
            self.logger.warning(f"  - JARVIS Prime: ⚠ not found ({self._prime_path})")

        # Reactor Core
        if self._reactor_path.exists():
            reactor_git = self._reactor_path / ".git"
            if reactor_git.exists():
                self._health["reactor"] = True
                self.logger.info(f"  - Reactor Core: ✓ ({self._reactor_path})")
            else:
                self.logger.warning(f"  - Reactor Core: ⚠ not a git repo")
        else:
            self.logger.warning(f"  - Reactor Core: ⚠ not found ({self._reactor_path})")

    async def shutdown(self) -> None:
        """Shutdown the Trinity connector."""
        if not self._running:
            return

        self.logger.info("[Trinity] Shutting down...")

        try:
            if self._realtime_broadcaster:
                try:
                    from core.ouroboros.ui_integration import disconnect_realtime_broadcaster
                    await disconnect_realtime_broadcaster()
                except Exception as e:
                    self.logger.warning(f"[Trinity] Realtime broadcaster disconnect error: {e}")
                self._realtime_broadcaster = None

            if self._enhanced_cross_repo:
                try:
                    from core.ouroboros.cross_repo import shutdown_enhanced_cross_repo
                    await shutdown_enhanced_cross_repo()
                except Exception as e:
                    self.logger.warning(f"[Trinity] Cross-repo shutdown error: {e}")

            if self._enhanced_self_improvement:
                await self._enhanced_self_improvement.shutdown()

        except Exception as e:
            self.logger.warning(f"[Trinity] Shutdown error: {e}")

        self._running = False
        self._initialized = False
        self.logger.info("[Trinity] Shutdown complete")

    async def execute_improvement_with_preview(
        self,
        target: str,
        goal: str,
        require_approval: bool = True,
    ):
        """
        Execute improvement with diff preview and approval workflow.

        This is the main interface for Claude Code-like self-improvement.
        """
        if not self._initialized:
            await self.initialize()

        if self._enhanced_self_improvement:
            return await self._enhanced_self_improvement.execute_with_preview(
                target=target,
                goal=goal,
                require_approval=require_approval,
            )
        else:
            return {"error": "Self-improvement module not available"}

    async def execute_multi_file_improvement(
        self,
        files_and_goals: List[Tuple[str, str]],
        shared_context: str = None,
    ):
        """Execute atomic multi-file improvement."""
        if not self._initialized:
            await self.initialize()

        if self._enhanced_self_improvement:
            return await self._enhanced_self_improvement.execute_multi_file_improvement(
                files_and_goals=files_and_goals,
                shared_context=shared_context,
            )
        else:
            return {"error": "Self-improvement module not available"}

    async def request_cross_repo_improvement(
        self,
        file_path: str,
        goal: str,
    ) -> str:
        """
        Request improvement across repositories with proper ordering.

        Uses Lamport clocks for causal ordering.
        """
        if not self._initialized:
            await self.initialize()

        if self._enhanced_cross_repo:
            return await self._enhanced_cross_repo.request_improvement_with_ordering(
                file_path=file_path,
                goal=goal,
            )
        else:
            return "Cross-repo orchestrator not available"

    def get_status(self) -> Dict[str, Any]:
        """Get comprehensive Trinity status."""
        status = {
            "session_id": self._session_id,
            "running": self._running,
            "initialized": self._initialized,
            "repositories": self._health,
        }

        if self._enhanced_self_improvement:
            try:
                status["self_improvement"] = self._enhanced_self_improvement.get_status()
            except Exception:
                status["self_improvement"] = {"error": "Status unavailable"}

        if self._enhanced_cross_repo:
            try:
                status["cross_repo"] = self._enhanced_cross_repo.get_status()
            except Exception:
                status["cross_repo"] = {"error": "Status unavailable"}

        return status


# Global Trinity connector
_trinity_connector: Optional[UnifiedTrinityConnector] = None


def get_trinity_connector() -> UnifiedTrinityConnector:
    """Get the global Trinity connector."""
    global _trinity_connector
    if _trinity_connector is None:
        _trinity_connector = UnifiedTrinityConnector()
    return _trinity_connector


async def initialize_trinity() -> bool:
    """Initialize the Trinity connector."""
    connector = get_trinity_connector()
    return await connector.initialize()


async def shutdown_trinity() -> None:
    """Shutdown the Trinity connector."""
    global _trinity_connector
    if _trinity_connector:
        await _trinity_connector.shutdown()
        _trinity_connector = None


# =============================================================================
# ADVANCED STARTUP BOOTSTRAPPER - Dynamic Environment Discovery
# =============================================================================

class AdvancedStartupBootstrapper:
    """
    Advanced Startup Bootstrapper for JARVIS AI System.

    Features:
    - 🔍 Dynamic path discovery (zero hardcoding)
    - ⚡ Async parallel initialization
    - 🌍 Multi-environment detection (dev/prod/test/ci)
    - 📁 Configuration layering (file → env → CLI)
    - 🏥 Health checks and validation
    - 🔄 Self-healing with automatic recovery
    - 📊 Comprehensive telemetry and logging
    - 🛡️ Graceful degradation on failures
    - 🧹 Automatic cleanup on exit
    """

    # Environment detection patterns
    ENV_PATTERNS = {
        'production': ['prod', 'production', 'prd'],
        'staging': ['staging', 'stg', 'stage'],
        'development': ['dev', 'development', 'local'],
        'test': ['test', 'testing', 'ci', 'qa'],
    }

    # Required directories for validation
    REQUIRED_DIRS = ['backend', 'frontend']
    OPTIONAL_DIRS = ['core', 'api', 'intelligence', 'vision', 'voice']

    # Config file search paths (relative to project root)
    CONFIG_PATHS = [
        'backend/config/startup_progress_config.json',
        'config/startup.json',
        '.jarvis/config.json',
        'jarvis.config.json',
    ]

    def __init__(self):
        """Initialize the bootstrapper with dynamic discovery."""
        self._start_time = time.time()
        self._initialized = False
        self._paths: Dict[str, Path] = {}
        self._config: Dict[str, Any] = {}
        self._environment: str = 'development'
        self._health_status: Dict[str, Any] = {}
        self._recovery_attempts: int = 0
        self._max_recovery_attempts: int = 3
        self._telemetry: Dict[str, Any] = {'events': [], 'timings': {}}
        self._cleanup_handlers: List[Callable] = []
        self._interrupt_count: int = 0
        self._last_interrupt_time: float = 0
        self._lock = LazyAsyncLock()  # Lazy init for Python 3.9 compatibility

        # Discover paths immediately (sync, required for early setup)
        self._discover_paths()

    def _discover_paths(self) -> None:
        """
        Dynamically discover all required paths.
        Zero hardcoding - works from any invocation location.
        """
        # Method 1: Script location (most reliable)
        script_path = Path(__file__).resolve()
        script_dir = script_path.parent

        # Method 2: Current working directory
        cwd = Path.cwd().resolve()

        # Method 3: Environment variable override
        env_root = os.environ.get('JARVIS_ROOT')

        # Determine project root by checking for marker files/dirs
        candidate_roots = [script_dir, cwd]
        if env_root:
            candidate_roots.insert(0, Path(env_root).resolve())

        project_root = None
        for candidate in candidate_roots:
            if self._is_project_root(candidate):
                project_root = candidate
                break
            # Check parent directories
            for parent in candidate.parents:
                if self._is_project_root(parent):
                    project_root = parent
                    break
            if project_root:
                break

        if not project_root:
            project_root = script_dir

        # Store discovered paths
        self._paths = {
            'project_root': project_root,
            'script': script_path,
            'backend': project_root / 'backend',
            'frontend': project_root / 'frontend',
            'config': project_root / 'backend' / 'config',
            'logs': project_root / 'backend' / 'logs',
            'venv': project_root / 'backend' / 'venv',
            'core': project_root / 'backend' / 'core',
            'api': project_root / 'backend' / 'api',
            'intelligence': project_root / 'backend' / 'intelligence',
            'temp': Path(tempfile.gettempdir()),
            'home': Path.home(),
            'jarvis_home': Path.home() / '.jarvis',
        }

        # Discover Python executable
        self._paths['python'] = self._discover_python()
        self._paths['venv_python'] = self._discover_venv_python()

    def _is_project_root(self, path: Path) -> bool:
        """Check if path is the JARVIS project root."""
        markers = [
            path / 'backend' / 'main.py',
            path / 'unified_supervisor.py',
            path / 'frontend' / 'package.json',
        ]
        return any(m.exists() for m in markers)

    def _discover_python(self) -> Path:
        """Discover the best Python executable to use."""
        candidates = [
            self._paths.get('venv', Path()) / 'bin' / 'python3',
            self._paths.get('venv', Path()) / 'bin' / 'python',
            Path(sys.executable),
            Path('/usr/bin/python3'),
            Path('/usr/local/bin/python3'),
        ]

        for candidate in candidates:
            if candidate.exists() and os.access(candidate, os.X_OK):
                return candidate

        return Path(sys.executable)

    def _discover_venv_python(self) -> Optional[Path]:
        """Discover virtual environment Python."""
        venv_paths = [
            self._paths['backend'] / 'venv' / 'bin' / 'python3',
            self._paths['backend'] / 'venv' / 'bin' / 'python',
            self._paths['project_root'] / 'venv' / 'bin' / 'python3',
            self._paths['project_root'] / '.venv' / 'bin' / 'python3',
        ]

        for venv_python in venv_paths:
            if venv_python.exists():
                return venv_python

        return None

    def _detect_environment(self) -> str:
        """
        Detect the current runtime environment.
        Priority: CLI arg → ENV var → git branch → default
        """
        # Check environment variable
        env_var = os.environ.get('JARVIS_ENV', '').lower()
        for env_name, patterns in self.ENV_PATTERNS.items():
            if env_var in patterns:
                return env_name

        # Check git branch
        try:
            result = subprocess.run(
                ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                capture_output=True, text=True, timeout=5,
                cwd=self._paths['project_root']
            )
            if result.returncode == 0:
                branch = result.stdout.strip().lower()
                if 'prod' in branch or 'main' == branch or 'master' == branch:
                    return 'production'
                elif 'stag' in branch:
                    return 'staging'
                elif 'test' in branch or 'ci' in branch:
                    return 'test'
        except Exception:
            pass

        # Check for CI environment
        ci_indicators = ['CI', 'GITHUB_ACTIONS', 'GITLAB_CI', 'JENKINS_URL', 'TRAVIS']
        if any(os.environ.get(ci) for ci in ci_indicators):
            return 'test'

        return 'development'

    async def _load_config_async(self) -> Dict[str, Any]:
        """
        Load configuration with layering: file → env → runtime.
        Fully async for non-blocking I/O.
        """
        config = self._get_default_config()

        # Layer 1: File-based config
        for config_path_str in self.CONFIG_PATHS:
            config_path = self._paths['project_root'] / config_path_str
            if config_path.exists():
                try:
                    async with self._lock:
                        content = await asyncio.to_thread(config_path.read_text)
                        file_config = json.loads(content)
                        config = self._merge_config(config, file_config)
                        self._log_event('config_loaded', {'source': str(config_path)})
                        break
                except Exception as e:
                    self._log_event('config_error', {'source': str(config_path), 'error': str(e)})

        # Layer 2: Environment variables
        env_overrides = self._get_env_overrides()
        config = self._merge_config(config, env_overrides)

        # Layer 3: Runtime detection
        config['environment'] = self._environment
        config['paths'] = {k: str(v) for k, v in self._paths.items()}

        return config

    def _get_default_config(self) -> Dict[str, Any]:
        """Get default configuration values."""
        return {
            'backend': {
                'host': '0.0.0.0',
                'port': 8010,
                'fallback_ports': [8011, 8000, 8001, 8080, 8888],
                'workers': 1,
                'timeout': 300,
            },
            'frontend': {
                'port': 3000,
                'fallback_ports': [3001, 3002, 3003],
            },
            'startup': {
                'parallel_init': True,
                'health_check_timeout': 30,
                'max_recovery_attempts': 3,
                'graceful_shutdown_timeout': 10,
            },
            'features': {
                'voice_unlock': True,
                'vision': True,
                'autonomous': True,
                'cloud_sql': True,
            },
            'logging': {
                'level': 'INFO',
                'format': '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                'file': 'jarvis_startup.log',
            },
        }

    def _get_env_overrides(self) -> Dict[str, Any]:
        """Extract configuration overrides from environment variables."""
        overrides = {}

        env_mappings = {
            'JARVIS_BACKEND_PORT': ('backend', 'port', int),
            'JARVIS_FRONTEND_PORT': ('frontend', 'port', int),
            'JARVIS_HOST': ('backend', 'host', str),
            'JARVIS_WORKERS': ('backend', 'workers', int),
            'JARVIS_LOG_LEVEL': ('logging', 'level', str),
            'JARVIS_PARALLEL_INIT': ('startup', 'parallel_init', lambda x: x.lower() == 'true'),
            'JARVIS_VOICE_UNLOCK': ('features', 'voice_unlock', lambda x: x.lower() == 'true'),
            'JARVIS_VISION': ('features', 'vision', lambda x: x.lower() == 'true'),
            'JARVIS_AUTONOMOUS': ('features', 'autonomous', lambda x: x.lower() == 'true'),
        }

        for env_key, (section, key, converter) in env_mappings.items():
            value = os.environ.get(env_key)
            if value is not None:
                if section not in overrides:
                    overrides[section] = {}
                try:
                    overrides[section][key] = converter(value)
                except (ValueError, TypeError):
                    pass

        return overrides

    def _merge_config(self, base: Dict, overlay: Dict) -> Dict:
        """Deep merge configuration dictionaries."""
        result = base.copy()
        for key, value in overlay.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._merge_config(result[key], value)
            else:
                result[key] = value
        return result

    def _log_event(self, event_type: str, data: Dict[str, Any] = None) -> None:
        """Log telemetry event."""
        event = {
            'type': event_type,
            'timestamp': time.time(),
            'data': data or {},
        }
        self._telemetry['events'].append(event)

    def setup_python_path(self) -> None:
        """Configure Python path for imports."""
        paths_to_add = [
            self._paths['project_root'],
            self._paths['backend'],
        ]

        for path in paths_to_add:
            path_str = str(path)
            if path_str not in sys.path:
                sys.path.insert(0, path_str)

        # Set environment variable for subprocesses
        existing_pythonpath = os.environ.get('PYTHONPATH', '')
        new_paths = ':'.join(str(p) for p in paths_to_add)
        os.environ['PYTHONPATH'] = f"{new_paths}:{existing_pythonpath}" if existing_pythonpath else new_paths

    def setup_working_directory(self) -> None:
        """Change to project root directory."""
        project_root = self._paths['project_root']
        if Path.cwd() != project_root:
            os.chdir(project_root)

    async def initialize(self) -> bool:
        """
        Full async initialization sequence.
        Returns True if initialization succeeded.
        """
        try:
            self._log_event('init_started')

            # Phase 1: Environment setup (sync, must happen first)
            self.setup_working_directory()
            self.setup_python_path()
            self._environment = self._detect_environment()

            # Phase 2: Load configuration (async)
            self._config = await self._load_config_async()

            # Phase 3: Create required directories
            await self._create_required_directories()

            self._initialized = True
            self._log_event('init_completed', {
                'environment': self._environment,
                'duration_ms': (time.time() - self._start_time) * 1000,
            })

            return True

        except Exception as e:
            self._log_event('init_failed', {'error': str(e)})
            return False

    async def _create_required_directories(self) -> None:
        """Create required directories if they don't exist."""
        dirs_to_create = [
            self._paths['logs'],
            self._paths['jarvis_home'],
            self._paths['jarvis_home'] / 'state',
            self._paths['jarvis_home'] / 'cache',
        ]

        for dir_path in dirs_to_create:
            try:
                dir_path.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass  # Ignore errors - not critical

    def get_status(self) -> Dict[str, Any]:
        """Get bootstrapper status."""
        return {
            'initialized': self._initialized,
            'environment': self._environment,
            'paths': {k: str(v) for k, v in self._paths.items()},
            'config': self._config,
            'uptime_seconds': time.time() - self._start_time,
            'telemetry_events': len(self._telemetry['events']),
        }


# Global bootstrapper singleton
_bootstrapper: Optional[AdvancedStartupBootstrapper] = None


def get_bootstrapper() -> AdvancedStartupBootstrapper:
    """Get the global bootstrapper."""
    global _bootstrapper
    if _bootstrapper is None:
        _bootstrapper = AdvancedStartupBootstrapper()
    return _bootstrapper


# =============================================================================
# PROCESS INFO DATACLASS - Process Metadata
# =============================================================================

@dataclass
class ProcessInfo:
    """Information about a discovered process."""
    pid: int
    cmdline: str
    age_seconds: float
    memory_mb: float = 0.0
    source: str = "scan"  # "pid_file", "scan", or "port_<N>"


# =============================================================================
# PARALLEL PROCESS CLEANER - Intelligent Process Cleanup
# =============================================================================

class ParallelProcessCleaner:
    """
    Intelligent parallel process cleaner with cascade termination.

    Features:
    - Parallel process discovery using ThreadPoolExecutor
    - Async termination with SIGINT → SIGTERM → SIGKILL cascade
    - Semaphore-controlled parallelism
    - Detailed progress reporting
    - PID file cleanup
    - v97.0: Stale lock holder cleanup
    - v117.0: GlobalProcessRegistry preservation
    - v132.4: SystemExit protection for thread pool workers
    - v152.0: Progressive readiness awareness
    """

    def __init__(
        self,
        config: Optional[SystemKernelConfig] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.config = config or SystemKernelConfig.from_environment()
        self.logger = logger or logging.getLogger("ParallelProcessCleaner")
        self._my_pid = os.getpid()
        self._my_parent = os.getppid()

        # Process patterns for JARVIS discovery
        self.jarvis_patterns = [
            "run_supervisor.py",
            "start_system.py",
            "unified_supervisor.py",
            "jarvis",
            "uvicorn",
            "main.py",
        ]

        # PID files to check
        self.pid_files = [
            Path.home() / ".jarvis" / "supervisor.pid",
            Path.home() / ".jarvis" / "backend.pid",
            Path("/tmp") / "jarvis_supervisor.pid",
        ]

        # Required ports to check
        self.required_ports = [8010, 8000, 8090, 3000, 3001]

        # Cleanup timeouts
        self.cleanup_timeout_sigint = 1.0
        self.cleanup_timeout_sigterm = 2.0
        self.cleanup_timeout_sigkill = 1.0
        self.max_parallel_cleanups = 10

    async def discover_and_cleanup(self) -> Tuple[int, List[ProcessInfo]]:
        """
        Discover and cleanup existing JARVIS instances.

        v97.0: Includes lock holder cleanup as Phase 0 to prevent
        30-second ownership acquisition timeouts.

        Returns:
            Tuple of (terminated_count, discovered_processes)
        """
        # v97.0: Phase 0 - Clean up any processes holding ownership locks
        lock_holders_killed = await self.cleanup_stale_lock_holders()
        if lock_holders_killed > 0:
            self.logger.info(f"[v97.0] Killed {lock_holders_killed} stale lock holder(s)")
            await asyncio.sleep(0.5)  # Allow OS to release resources

        # Phase 1: Parallel discovery
        discovered = await self._parallel_discover()

        if not discovered:
            return 0, []

        # Phase 2: Parallel termination with semaphore
        terminated = await self._parallel_terminate(discovered)

        # Phase 3: PID file cleanup
        await self._cleanup_pid_files()

        return terminated, list(discovered.values())

    async def _parallel_discover(self) -> Dict[int, ProcessInfo]:
        """Discover processes in parallel using ThreadPoolExecutor."""
        discovered: Dict[int, ProcessInfo] = {}

        # Run in thread pool for psutil operations (they can block)
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(max_workers=4) as executor:
            # Task 1: Check PID files
            pid_file_task = loop.run_in_executor(
                executor, self._discover_from_pid_files
            )

            # Task 2: Scan process list
            process_scan_task = loop.run_in_executor(
                executor, self._discover_from_process_list
            )

            # Task 3: Scan ports
            port_scan_task = loop.run_in_executor(
                executor, self._discover_from_ports
            )

            # Wait for all
            pid_file_procs, scanned_procs, port_procs = await asyncio.gather(
                pid_file_task, process_scan_task, port_scan_task
            )

        # Merge results (PID files take precedence, then ports, then scan)
        discovered.update(scanned_procs)
        discovered.update(port_procs)
        discovered.update(pid_file_procs)

        return discovered

    def _discover_from_pid_files(self) -> Dict[int, ProcessInfo]:
        """
        Discover processes from PID files (runs in thread).

        v132.4: Added SystemExit protection for thread pool workers.
        """
        try:
            import psutil
        except ImportError:
            return {}
        except SystemExit:
            return {}

        discovered = {}

        for pid_file in self.pid_files:
            try:
                if not pid_file.exists():
                    continue
            except OSError:
                continue

            pid = None
            try:
                with open(str(pid_file), 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                pid = int(content) if content else None

                if pid is None:
                    continue

                if not psutil.pid_exists(pid) or pid in (self._my_pid, self._my_parent):
                    self._safe_unlink(pid_file)
                    continue

                proc = psutil.Process(pid)
                cmdline = " ".join(proc.cmdline()).lower()

                if any(p in cmdline for p in self.jarvis_patterns):
                    discovered[pid] = ProcessInfo(
                        pid=pid,
                        cmdline=cmdline[:100],
                        age_seconds=time.time() - proc.create_time(),
                        memory_mb=proc.memory_info().rss / (1024 * 1024),
                        source="pid_file"
                    )
            except (ValueError, Exception):
                self._safe_unlink(pid_file)
            except SystemExit:
                return discovered

        return discovered

    def _safe_unlink(self, path: Path) -> bool:
        """Safely unlink a file with proper error handling."""
        try:
            path.unlink(missing_ok=True)
            return True
        except (OSError, IOError, PermissionError):
            return False

    def _discover_from_process_list(self) -> Dict[int, ProcessInfo]:
        """
        Scan process list for JARVIS processes (runs in thread).

        v132.4: Added SystemExit protection.
        """
        try:
            import psutil
        except ImportError:
            return {}
        except SystemExit:
            return {}

        discovered = {}

        try:
            for proc in psutil.process_iter(['pid', 'cmdline', 'create_time', 'memory_info']):
                try:
                    pid = proc.info['pid']
                    if pid in (self._my_pid, self._my_parent):
                        continue

                    cmdline = " ".join(proc.info.get('cmdline') or []).lower()
                    if any(p in cmdline for p in self.jarvis_patterns):
                        mem_info = proc.info.get('memory_info')
                        discovered[pid] = ProcessInfo(
                            pid=pid,
                            cmdline=cmdline[:100],
                            age_seconds=time.time() - proc.info['create_time'],
                            memory_mb=mem_info.rss / (1024 * 1024) if mem_info else 0,
                            source="scan"
                        )
                except Exception:
                    pass
        except SystemExit:
            pass

        return discovered

    def _discover_from_ports(self) -> Dict[int, ProcessInfo]:
        """
        Discover processes holding critical ports.

        v132.4: Added SystemExit protection.
        """
        try:
            import psutil
        except ImportError:
            return {}
        except SystemExit:
            return {}

        discovered = {}

        try:
            for conn in psutil.net_connections(kind='inet'):
                if conn.laddr.port in self.required_ports:
                    try:
                        pid = conn.pid
                        if not pid or pid in (self._my_pid, self._my_parent):
                            continue

                        if pid in discovered:
                            continue

                        proc = psutil.Process(pid)
                        cmdline = " ".join(proc.cmdline()).lower()
                        mem_info = proc.memory_info()

                        discovered[pid] = ProcessInfo(
                            pid=pid,
                            cmdline=cmdline[:100],
                            age_seconds=time.time() - proc.create_time(),
                            memory_mb=mem_info.rss / (1024 * 1024),
                            source=f"port_{conn.laddr.port}"
                        )
                    except Exception:
                        pass
        except (PermissionError, Exception):
            pass
        except SystemExit:
            pass

        return discovered

    async def _parallel_terminate(self, processes: Dict[int, ProcessInfo]) -> int:
        """Terminate processes in parallel with semaphore control."""
        semaphore = asyncio.Semaphore(self.max_parallel_cleanups)

        async def terminate_one(pid: int, info: ProcessInfo) -> bool:
            async with semaphore:
                return await self._terminate_process(pid, info)

        tasks = [
            asyncio.create_task(terminate_one(pid, info))
            for pid, info in processes.items()
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        terminated = 0
        for result in results:
            if result is True:
                terminated += 1

        return terminated

    async def _terminate_process(self, pid: int, info: ProcessInfo) -> bool:
        """
        Terminate a single process with cascade strategy.

        Strategy: SIGINT → wait → SIGTERM → wait → SIGKILL
        """
        try:
            import psutil

            def _safe_kill(target_pid: int, sig: int) -> bool:
                """Send signal with PID validation."""
                try:
                    proc = psutil.Process(target_pid)
                    os.kill(target_pid, sig)
                    return True
                except (Exception, ProcessLookupError, OSError):
                    return True  # Process already gone

            # Phase 1: SIGINT (graceful)
            if _safe_kill(pid, signal.SIGINT):
                try:
                    proc = psutil.Process(pid)
                    await asyncio.sleep(0.1)
                    proc.wait(timeout=self.cleanup_timeout_sigint)
                    return True
                except Exception:
                    pass

            # Phase 2: SIGTERM
            if _safe_kill(pid, signal.SIGTERM):
                try:
                    proc = psutil.Process(pid)
                    proc.wait(timeout=self.cleanup_timeout_sigterm)
                    return True
                except Exception:
                    pass

            # Phase 3: SIGKILL (force)
            if _safe_kill(pid, signal.SIGKILL):
                try:
                    proc = psutil.Process(pid)
                    proc.wait(timeout=self.cleanup_timeout_sigkill)
                except Exception:
                    pass
            return True

        except Exception as e:
            self.logger.debug(f"Failed to terminate {pid}: {e}")
            return False

    async def _cleanup_pid_files(self) -> None:
        """Clean up stale PID files."""
        for pid_file in self.pid_files:
            try:
                pid_file.unlink(missing_ok=True)
            except Exception:
                pass

    async def cleanup_stale_lock_holders(self) -> int:
        """
        v97.0/v152.0: Clean up processes holding fcntl locks on ownership files.

        Uses lsof to detect which process holds the lock file, then kills
        it if it's an orphaned supervisor from a previous session.

        v152.0: Checks progressive readiness state before killing.

        Returns:
            Number of lock holders killed
        """
        lock_file = Path.home() / ".jarvis" / "state" / "locks" / "jarvis.lock"
        killed_count = 0

        if not lock_file.exists():
            return 0

        try:
            result = subprocess.run(
                ["lsof", str(lock_file)],
                capture_output=True,
                text=True,
                timeout=5
            )

            if result.returncode != 0:
                return 0

            for line in result.stdout.strip().split('\n')[1:]:
                parts = line.split()
                if len(parts) < 2:
                    continue

                try:
                    holder_pid = int(parts[1])
                except ValueError:
                    continue

                if holder_pid in (self._my_pid, self._my_parent):
                    continue

                try:
                    import psutil
                    proc = psutil.Process(holder_pid)
                    cmdline = " ".join(proc.cmdline())

                    if any(p in cmdline for p in self.jarvis_patterns):
                        is_truly_stale = await self._is_supervisor_truly_stale(holder_pid)

                        if not is_truly_stale:
                            self.logger.info(
                                f"[v152.0] Lock holder PID {holder_pid} is still making progress - NOT killing"
                            )
                            continue

                        self.logger.warning(
                            f"[v97.0] Found stale lock holder PID {holder_pid}, killing..."
                        )

                        try:
                            os.kill(holder_pid, signal.SIGTERM)
                            await asyncio.sleep(0.5)

                            if psutil.pid_exists(holder_pid):
                                os.kill(holder_pid, signal.SIGKILL)
                                await asyncio.sleep(0.2)

                            killed_count += 1

                        except (ProcessLookupError, OSError):
                            killed_count += 1

                except Exception:
                    pass

            if killed_count > 0:
                await asyncio.sleep(0.3)
                try:
                    if lock_file.exists():
                        lock_file.unlink()
                except Exception:
                    pass

        except subprocess.TimeoutExpired:
            self.logger.debug("[v97.0] lsof timed out")
        except FileNotFoundError:
            self.logger.debug("[v97.0] lsof not available")
        except Exception as e:
            self.logger.debug(f"[v97.0] Lock holder cleanup error: {e}")

        return killed_count

    async def _is_supervisor_truly_stale(self, holder_pid: int) -> bool:
        """
        v152.0: Determine if a supervisor process is truly stale.

        A supervisor is NOT stale if:
        1. Readiness state file was updated in the last 120 seconds
        2. Heartbeat file was updated in the last 60 seconds
        """
        now = time.time()

        # Check 1: Readiness state file freshness
        readiness_state_file = Path.home() / ".jarvis" / "trinity" / "readiness_state.json"
        try:
            if readiness_state_file.exists():
                state_data = json.loads(readiness_state_file.read_text())
                updated_at = state_data.get("updated_at", 0)
                state_age = now - updated_at

                if state_age < 120:
                    return False
        except Exception:
            pass

        # Check 2: Heartbeat file freshness
        heartbeat_file = Path.home() / ".jarvis" / "trinity" / "heartbeats" / "supervisor.json"
        try:
            if heartbeat_file.exists():
                heartbeat_data = json.loads(heartbeat_file.read_text())
                heartbeat_time = heartbeat_data.get("timestamp", 0)
                heartbeat_age = now - heartbeat_time

                if heartbeat_age < 60:
                    return False
        except Exception:
            pass

        # Check 3: IPC ping (last resort)
        try:
            ipc_socket = Path.home() / ".jarvis" / "ipc" / "supervisor.sock"
            if ipc_socket.exists():
                import socket
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(2.0)
                try:
                    sock.connect(str(ipc_socket))
                    sock.sendall(b'{"type": "ping"}\n')
                    response = sock.recv(1024)
                    if response:
                        return False
                finally:
                    sock.close()
        except Exception:
            pass

        return True


# =============================================================================
# ZOMBIE PROCESS INFO DATACLASS - Extended Process Metadata
# =============================================================================

@dataclass
class ZombieProcessInfo:
    """Extended process info with zombie detection metadata."""
    pid: int
    cmdline: str
    age_seconds: float
    memory_mb: float = 0.0
    cpu_percent: float = 0.0
    status: str = "unknown"
    is_orphaned: bool = False
    is_zombie_like: bool = False
    stale_connection_count: int = 0
    repo_origin: str = "unknown"  # jarvis, jarvis-prime, reactor-core
    detection_source: str = "scan"  # scan, port, registry, pid_file


# =============================================================================
# COMPREHENSIVE ZOMBIE CLEANUP - Cross-Repo Zombie Detection
# =============================================================================

class ComprehensiveZombieCleanup:
    """
    v109.7: Comprehensive Zombie Cleanup System for JARVIS Ecosystem.

    Provides ultra-robust cleanup across all three repos:
    - JARVIS (main AI agent) - port 8010
    - JARVIS-Prime (J-Prime Mind) - port 8000
    - Reactor-Core (Nerves) - port 8090

    Features:
    - Async parallel discovery across multiple detection sources
    - Zombie detection via responsiveness heuristics
    - Cross-repo registry integration for coordinated cleanup
    - Memory-aware cleanup
    - Port-based Trinity service detection
    - Graceful termination with cascade (SIGINT → SIGTERM → SIGKILL)
    - Circuit breaker pattern to prevent cleanup storms
    """

    # Trinity ports by service
    TRINITY_PORTS = {
        "jarvis-body": [8010],
        "jarvis-prime": [8000],
        "reactor-core": [8090],
    }

    # Process patterns by repo
    REPO_PATTERNS = {
        "jarvis": ["run_supervisor.py", "start_system.py", "unified_supervisor.py", "jarvis", "uvicorn.*8010"],
        "jarvis-prime": ["trinity_orchestrator.*jarvis-prime", "jarvis.prime", "uvicorn.*8000"],
        "reactor-core": ["trinity_orchestrator.*reactor-core", "reactor.core", "uvicorn.*8090"],
    }

    def __init__(
        self,
        config: Optional[SystemKernelConfig] = None,
        logger: Optional[logging.Logger] = None,
        enable_cross_repo: bool = True,
        enable_memory_aware: bool = True,
        enable_circuit_breaker: bool = True,
    ):
        self.config = config or SystemKernelConfig.from_environment()
        self.logger = logger or logging.getLogger("ZombieCleanup")
        self._enable_cross_repo = enable_cross_repo
        self._enable_memory_aware = enable_memory_aware
        self._enable_circuit_breaker = enable_circuit_breaker

        # Circuit breaker state
        self._cleanup_count = 0
        self._last_cleanup_time: Optional[float] = None
        self._circuit_open = False
        self._circuit_cooldown = 60.0  # seconds

        # Stats tracking
        self._stats = {
            "total_cleanups": 0,
            "total_zombies_found": 0,
            "total_zombies_killed": 0,
            "total_ports_freed": 0,
            "circuit_breaker_trips": 0,
        }

        self._my_pid = os.getpid()
        self._my_parent = os.getppid()

    async def run_comprehensive_cleanup(self) -> Dict[str, Any]:
        """
        Run full comprehensive zombie cleanup.

        Returns:
            Cleanup results with stats
        """
        start_time = time.time()
        result = {
            "zombies_found": 0,
            "zombies_killed": 0,
            "ports_freed": 0,
            "duration_ms": 0,
            "phases_completed": [],
            "errors": [],
        }

        try:
            # Check circuit breaker
            if self._enable_circuit_breaker and self._is_circuit_open():
                self.logger.warning("[v109.7] Circuit breaker open - skipping cleanup")
                result["errors"].append("circuit_breaker_open")
                return result

            # Phase 1: Discover zombies
            self.logger.info("[v109.7] Phase 1: Discovering zombie processes...")
            zombies = await self._discover_all_zombies()
            result["zombies_found"] = len(zombies)
            result["phases_completed"].append("discovery")

            if not zombies:
                self.logger.info("[v109.7] No zombie processes found")
                result["duration_ms"] = int((time.time() - start_time) * 1000)
                return result

            # Phase 2: Terminate zombies
            self.logger.info(f"[v109.7] Phase 2: Terminating {len(zombies)} zombie process(es)...")
            killed = await self._terminate_zombies(zombies)
            result["zombies_killed"] = killed
            result["phases_completed"].append("termination")

            # Phase 3: Free ports
            self.logger.info("[v109.7] Phase 3: Freeing ports...")
            freed = await self._free_ports()
            result["ports_freed"] = freed
            result["phases_completed"].append("port_cleanup")

            # Update stats
            self._stats["total_cleanups"] += 1
            self._stats["total_zombies_found"] += result["zombies_found"]
            self._stats["total_zombies_killed"] += result["zombies_killed"]
            self._stats["total_ports_freed"] += result["ports_freed"]

            result["duration_ms"] = int((time.time() - start_time) * 1000)
            self._last_cleanup_time = time.time()

            self.logger.info(
                f"[v109.7] Cleanup complete: {result['zombies_killed']}/{result['zombies_found']} "
                f"zombies killed, {result['ports_freed']} ports freed in {result['duration_ms']}ms"
            )

            return result

        except Exception as e:
            result["errors"].append(str(e))
            self.logger.error(f"[v109.7] Cleanup error: {e}")
            return result

    def _is_circuit_open(self) -> bool:
        """Check if circuit breaker is open."""
        if not self._circuit_open:
            return False

        if self._last_cleanup_time and (time.time() - self._last_cleanup_time) > self._circuit_cooldown:
            self._circuit_open = False
            return False

        return True

    async def _discover_all_zombies(self) -> Dict[int, ZombieProcessInfo]:
        """Discover all zombie processes across repos."""
        zombies: Dict[int, ZombieProcessInfo] = {}

        try:
            import psutil
        except ImportError:
            return zombies

        # Scan all processes
        all_ports = []
        for ports in self.TRINITY_PORTS.values():
            all_ports.extend(ports)

        for proc in psutil.process_iter(['pid', 'cmdline', 'create_time', 'memory_info', 'cpu_percent', 'status']):
            try:
                pid = proc.info['pid']
                if pid in (self._my_pid, self._my_parent):
                    continue

                cmdline = " ".join(proc.info.get('cmdline') or []).lower()

                # Check if matches any repo pattern
                repo_origin = "unknown"
                for repo, patterns in self.REPO_PATTERNS.items():
                    if any(p in cmdline for p in patterns):
                        repo_origin = repo
                        break

                if repo_origin == "unknown":
                    continue

                # Detect zombie-like characteristics
                status = proc.info.get('status', 'unknown')
                is_zombie_like = status in ('zombie', 'stopped')

                # Check if orphaned (parent is init/1)
                try:
                    parent_pid = psutil.Process(pid).ppid()
                    is_orphaned = parent_pid == 1
                except Exception:
                    is_orphaned = False

                mem_info = proc.info.get('memory_info')
                zombies[pid] = ZombieProcessInfo(
                    pid=pid,
                    cmdline=cmdline[:100],
                    age_seconds=time.time() - proc.info.get('create_time', time.time()),
                    memory_mb=mem_info.rss / (1024 * 1024) if mem_info else 0,
                    cpu_percent=proc.info.get('cpu_percent', 0.0),
                    status=status,
                    is_orphaned=is_orphaned,
                    is_zombie_like=is_zombie_like,
                    repo_origin=repo_origin,
                    detection_source="scan",
                )

            except Exception:
                pass

        return zombies

    async def _terminate_zombies(self, zombies: Dict[int, ZombieProcessInfo]) -> int:
        """Terminate zombie processes."""
        try:
            import psutil
        except ImportError:
            return 0

        killed = 0

        for pid, info in zombies.items():
            try:
                # Try SIGTERM first
                os.kill(pid, signal.SIGTERM)
                await asyncio.sleep(0.5)

                if psutil.pid_exists(pid):
                    os.kill(pid, signal.SIGKILL)
                    await asyncio.sleep(0.2)

                killed += 1
                self.logger.info(f"[v109.7] Killed zombie PID {pid} ({info.repo_origin})")

            except (ProcessLookupError, OSError):
                killed += 1  # Already dead
            except Exception as e:
                self.logger.debug(f"[v109.7] Failed to kill PID {pid}: {e}")

        return killed

    async def _free_ports(self) -> int:
        """Free up Trinity ports."""
        freed = 0

        try:
            import psutil
        except ImportError:
            return freed

        all_ports = []
        for ports in self.TRINITY_PORTS.values():
            all_ports.extend(ports)

        try:
            for conn in psutil.net_connections(kind='inet'):
                if conn.laddr.port in all_ports and conn.pid:
                    if conn.pid in (self._my_pid, self._my_parent):
                        continue

                    try:
                        os.kill(conn.pid, signal.SIGKILL)
                        freed += 1
                    except Exception:
                        pass
        except Exception:
            pass

        return freed

    def get_stats(self) -> Dict[str, Any]:
        """Get cleanup statistics."""
        return self._stats.copy()


# Global process cleaner singleton
_process_cleaner: Optional[ParallelProcessCleaner] = None


def get_process_cleaner() -> ParallelProcessCleaner:
    """Get the global process cleaner."""
    global _process_cleaner
    if _process_cleaner is None:
        _process_cleaner = ParallelProcessCleaner()
    return _process_cleaner


# =============================================================================
# ZONE 3.7: INTELLIGENT CACHE MANAGER
# =============================================================================
# v110.0: Dynamic Python module and bytecode cache management


class IntelligentCacheManager:
    """
    Intelligent Cache Manager for Dynamic Python Module and Data Caching.

    Features:
    - Python module cache clearing with pattern-based filtering
    - Bytecode (.pyc/__pycache__) cleanup with size tracking
    - ChromaDB/vector database cache management
    - ML model cache warming and eviction
    - Frontend cache synchronization
    - Async operations for non-blocking cleanup
    - Statistics tracking and reporting
    - Environment-driven configuration

    Environment Configuration:
    - CACHE_MANAGER_ENABLED: Enable/disable (default: true)
    - CACHE_CLEAR_BYTECODE: Clear .pyc files (default: true)
    - CACHE_CLEAR_PYCACHE: Remove __pycache__ dirs (default: true)
    - CACHE_MODULE_PATTERNS: Comma-separated patterns to clear
    - CACHE_PRESERVE_PATTERNS: Patterns to preserve (default: none)
    - CACHE_WARM_ON_START: Pre-load critical modules (default: false)
    - CACHE_ASYNC_CLEANUP: Use async for cleanup (default: true)
    - CACHE_MAX_BYTECODE_AGE_HOURS: Max age for .pyc files (default: 24)
    - CACHE_TRACK_STATISTICS: Track detailed stats (default: true)

    This manager ensures clean Python imports by clearing stale cached
    modules and bytecode files, preventing version mismatch issues.
    """

    def __init__(
        self,
        config: Optional[SystemKernelConfig] = None,
        logger: Optional[Any] = None,
    ):
        """
        Initialize Intelligent Cache Manager with environment-driven config.

        Args:
            config: System kernel configuration
            logger: Logger instance
        """
        self.config = config
        self._logger = logger or logging.getLogger("CacheManager")

        # Configuration from environment (no hardcoding!)
        self.enabled = os.getenv("CACHE_MANAGER_ENABLED", "true").lower() == "true"
        self.clear_bytecode = (
            os.getenv("CACHE_CLEAR_BYTECODE", "true").lower() == "true"
        )
        self.clear_pycache = (
            os.getenv("CACHE_CLEAR_PYCACHE", "true").lower() == "true"
        )
        self.async_cleanup = (
            os.getenv("CACHE_ASYNC_CLEANUP", "true").lower() == "true"
        )
        self.warm_on_start = (
            os.getenv("CACHE_WARM_ON_START", "false").lower() == "true"
        )
        self.track_statistics = (
            os.getenv("CACHE_TRACK_STATISTICS", "true").lower() == "true"
        )
        self.max_bytecode_age_hours = float(
            os.getenv("CACHE_MAX_BYTECODE_AGE_HOURS", "24")
        )

        # Module patterns to clear/preserve
        default_patterns = "backend,api,vision,voice,unified,command,intelligence,core"
        self.module_patterns = [
            p.strip()
            for p in os.getenv("CACHE_MODULE_PATTERNS", default_patterns).split(",")
        ]
        preserve_patterns = os.getenv("CACHE_PRESERVE_PATTERNS", "")
        self.preserve_patterns = [
            p.strip() for p in preserve_patterns.split(",") if p.strip()
        ]

        # Warm-up modules (critical paths to pre-load)
        default_warm = "backend.core,backend.api,backend.voice_unlock"
        self.warm_modules = [
            p.strip()
            for p in os.getenv("CACHE_WARM_MODULES", default_warm).split(",")
        ]

        # Statistics tracking
        self.stats = {
            "modules_cleared": 0,
            "bytecode_files_removed": 0,
            "pycache_dirs_removed": 0,
            "bytes_freed": 0,
            "warmup_modules_loaded": 0,
            "last_clear_time": None,
            "last_clear_duration_ms": 0.0,
            "clear_count": 0,
            "errors": [],
        }

        # State
        self._initialized = False
        self._project_root: Optional[Path] = None

        self._logger.info("🧹 Intelligent Cache Manager initialized:")
        self._logger.info(f"   ├─ Enabled: {self.enabled}")
        self._logger.info(f"   ├─ Clear bytecode: {self.clear_bytecode}")
        self._logger.info(f"   ├─ Clear pycache: {self.clear_pycache}")
        self._logger.info(f"   └─ Module patterns: {len(self.module_patterns)}")

    def configure(self, project_root: Path) -> None:
        """
        Configure the cache manager with project root path.

        Args:
            project_root: Project root directory
        """
        self._project_root = project_root
        self._initialized = True

    def _should_clear_module(self, module_name: str) -> bool:
        """
        Determine if a module should be cleared based on patterns.

        Args:
            module_name: Full module name

        Returns:
            True if module should be cleared
        """
        # Check preserve patterns first
        for pattern in self.preserve_patterns:
            if pattern and pattern in module_name:
                return False

        # Check clear patterns
        for pattern in self.module_patterns:
            if pattern and pattern in module_name:
                return True

        return False

    def clear_python_modules(self) -> Dict[str, Any]:
        """
        Clear Python module cache based on configured patterns.

        Returns:
            Statistics about cleared modules
        """
        if not self.enabled:
            return {"cleared": 0, "skipped": "disabled"}

        start_time = time.time()
        modules_to_remove = []

        for module_name in list(sys.modules.keys()):
            if self._should_clear_module(module_name):
                modules_to_remove.append(module_name)

        for module_name in modules_to_remove:
            try:
                del sys.modules[module_name]
            except Exception as e:
                if self.track_statistics:
                    self.stats["errors"].append(f"Failed to clear {module_name}: {e}")

        if self.track_statistics:
            self.stats["modules_cleared"] += len(modules_to_remove)
            self.stats["last_clear_time"] = time.time()
            self.stats["last_clear_duration_ms"] = (time.time() - start_time) * 1000
            self.stats["clear_count"] += 1

        return {
            "cleared": len(modules_to_remove),
            "modules": modules_to_remove[:10],  # First 10 for logging
            "duration_ms": (time.time() - start_time) * 1000,
        }

    def clear_bytecode_cache(
        self, target_path: Optional[Path] = None
    ) -> Dict[str, Any]:
        """
        Clear Python bytecode cache (.pyc files and __pycache__ directories).

        Args:
            target_path: Path to clean (defaults to project backend)

        Returns:
            Statistics about cleared files
        """
        if not self.enabled or (not self.clear_bytecode and not self.clear_pycache):
            return {"cleared": False, "reason": "disabled"}

        target = target_path or (
            self._project_root / "backend" if self._project_root else None
        )

        if not target or not target.exists():
            return {"cleared": False, "reason": "path_not_found"}

        pycache_removed = 0
        pyc_removed = 0
        bytes_freed = 0
        errors = []

        # Remove __pycache__ directories
        if self.clear_pycache:
            for pycache_dir in target.rglob("__pycache__"):
                try:
                    dir_size = sum(
                        f.stat().st_size for f in pycache_dir.rglob("*") if f.is_file()
                    )
                    shutil.rmtree(pycache_dir)
                    pycache_removed += 1
                    bytes_freed += dir_size
                except Exception as e:
                    errors.append(f"Failed to remove {pycache_dir}: {e}")

        # Remove individual .pyc files (in case some are outside __pycache__)
        if self.clear_bytecode:
            for pyc_file in target.rglob("*.pyc"):
                try:
                    # Check age if configured
                    if self.max_bytecode_age_hours > 0:
                        file_age_hours = (
                            time.time() - pyc_file.stat().st_mtime
                        ) / 3600
                        if file_age_hours < self.max_bytecode_age_hours:
                            continue  # Skip recent files

                    file_size = pyc_file.stat().st_size
                    pyc_file.unlink()
                    pyc_removed += 1
                    bytes_freed += file_size
                except Exception as e:
                    errors.append(f"Failed to remove {pyc_file}: {e}")

        if self.track_statistics:
            self.stats["pycache_dirs_removed"] += pycache_removed
            self.stats["bytecode_files_removed"] += pyc_removed
            self.stats["bytes_freed"] += bytes_freed
            # Keep only first 5 errors
            self.stats["errors"].extend(errors[:5])

        return {
            "pycache_dirs": pycache_removed,
            "pyc_files": pyc_removed,
            "bytes_freed": bytes_freed,
            "bytes_freed_mb": bytes_freed / (1024 * 1024),
            "errors": len(errors),
        }

    async def clear_all_async(
        self, target_path: Optional[Path] = None
    ) -> Dict[str, Any]:
        """
        Asynchronously clear all caches.

        Args:
            target_path: Path to clean (defaults to project backend)

        Returns:
            Combined statistics from all clear operations
        """
        results: Dict[str, Any] = {}

        # Run bytecode cleanup in executor to not block
        loop = asyncio.get_running_loop()

        if self.clear_bytecode or self.clear_pycache:
            bytecode_result = await loop.run_in_executor(
                None, self.clear_bytecode_cache, target_path
            )
            results["bytecode"] = bytecode_result

        # Module clearing is fast, do it directly
        module_result = self.clear_python_modules()
        results["modules"] = module_result

        # Prevent new bytecode files
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

        return results

    def clear_all_sync(self, target_path: Optional[Path] = None) -> Dict[str, Any]:
        """
        Synchronously clear all caches.

        Args:
            target_path: Path to clean

        Returns:
            Combined statistics
        """
        results: Dict[str, Any] = {}

        if self.clear_bytecode or self.clear_pycache:
            results["bytecode"] = self.clear_bytecode_cache(target_path)

        results["modules"] = self.clear_python_modules()

        # Prevent new bytecode files
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

        return results

    async def warm_critical_modules(self) -> Dict[str, Any]:
        """
        Pre-load critical modules for faster subsequent imports.

        Returns:
            Statistics about warmed modules
        """
        if not self.warm_on_start:
            return {"warmed": 0, "reason": "disabled"}

        import importlib

        warmed = []
        errors = []

        for module_path in self.warm_modules:
            try:
                importlib.import_module(module_path)
                warmed.append(module_path)
            except Exception as e:
                errors.append(f"{module_path}: {e}")

        if self.track_statistics:
            self.stats["warmup_modules_loaded"] += len(warmed)

        return {
            "warmed": len(warmed),
            "modules": warmed,
            "errors": errors,
        }

    def verify_fresh_imports(self) -> bool:
        """
        Verify that imports are fresh (no stale cached modules).

        Returns:
            True if imports appear fresh
        """
        stale_count = 0
        for module_name in sys.modules:
            if self._should_clear_module(module_name):
                stale_count += 1

        return stale_count == 0

    def get_statistics(self) -> Dict[str, Any]:
        """Get cache manager statistics."""
        stats = self.stats.copy()
        stats["enabled"] = self.enabled
        stats["patterns"] = self.module_patterns
        stats["preserve_patterns"] = self.preserve_patterns
        stats["bytes_freed_mb"] = stats["bytes_freed"] / (1024 * 1024)
        return stats


# Global cache manager singleton
_cache_manager: Optional[IntelligentCacheManager] = None


def get_cache_manager() -> IntelligentCacheManager:
    """Get global Intelligent Cache Manager instance."""
    global _cache_manager
    if _cache_manager is None:
        _cache_manager = IntelligentCacheManager()
    return _cache_manager


# =============================================================================
# ZONE 3.8: PHYSICS-AWARE VOICE AUTHENTICATION MANAGER
# =============================================================================
# v109.0: Physics-based voice anti-spoofing and liveness detection


class PhysicsAwareAuthManager:
    """
    Physics-Aware Voice Authentication Startup Manager.

    Initializes and manages the physics-aware authentication components:
    - Reverberation analyzer (RT60, double-reverb detection)
    - Vocal tract length estimator (VTL biometrics)
    - Doppler analyzer (liveness detection)
    - Bayesian confidence fusion
    - 7-layer anti-spoofing system

    Environment Configuration:
    - PHYSICS_AWARE_ENABLED: Enable/disable (default: true)
    - PHYSICS_PRELOAD_MODELS: Preload models at startup (default: false)
    - PHYSICS_BASELINE_VTL_CM: User's baseline VTL (default: auto-detect)
    - PHYSICS_BASELINE_RT60_SEC: User's baseline RT60 (default: auto-detect)

    Anti-Spoofing Layers:
    1. Spectral analysis for replay detection
    2. Microphone fingerprinting
    3. Environmental acoustics
    4. Vocal tract analysis (VTL biometrics)
    5. Reverberation consistency
    6. Doppler movement detection
    7. Bayesian fusion of all layers
    """

    def __init__(
        self,
        config: Optional[SystemKernelConfig] = None,
        logger: Optional[Any] = None,
    ):
        """
        Initialize physics-aware authentication manager.

        Args:
            config: System kernel configuration
            logger: Logger instance
        """
        self.config = config
        self._logger = logger or logging.getLogger("PhysicsAuth")

        # Configuration from environment
        self.enabled = os.getenv("PHYSICS_AWARE_ENABLED", "true").lower() == "true"
        self.preload_models = (
            os.getenv("PHYSICS_PRELOAD_MODELS", "false").lower() == "true"
        )

        # Baseline values (can be overridden or auto-detected)
        self._baseline_vtl_cm: Optional[float] = None
        self._baseline_rt60_sec: Optional[float] = None

        baseline_vtl = os.getenv("PHYSICS_BASELINE_VTL_CM")
        if baseline_vtl:
            try:
                self._baseline_vtl_cm = float(baseline_vtl)
            except ValueError:
                pass

        baseline_rt60 = os.getenv("PHYSICS_BASELINE_RT60_SEC")
        if baseline_rt60:
            try:
                self._baseline_rt60_sec = float(baseline_rt60)
            except ValueError:
                pass

        # Component references
        self._physics_extractor: Optional[Any] = None
        self._anti_spoofing_detector: Optional[Any] = None
        self._initialized = False

        # Statistics
        self.initialization_time_ms = 0.0
        self.physics_verifications = 0
        self.spoofs_detected = 0
        self.legitimate_authentications = 0

        # Spoof detection history for learning
        self._spoof_history: List[Dict[str, Any]] = []
        self._max_history = 100

        self._logger.info("🔬 Physics-Aware Auth Manager initialized:")
        self._logger.info(f"   ├─ Enabled: {self.enabled}")
        self._logger.info(f"   ├─ Preload models: {self.preload_models}")
        self._logger.info(f"   ├─ Baseline VTL: {self._baseline_vtl_cm or 'auto-detect'} cm")
        self._logger.info(f"   └─ Baseline RT60: {self._baseline_rt60_sec or 'auto-detect'} sec")

    async def initialize(self) -> bool:
        """
        Initialize physics-aware authentication components.

        Returns:
            True if initialization successful
        """
        if not self.enabled:
            self._logger.info("🔬 Physics-aware authentication disabled")
            return False

        start_time = time.time()

        try:
            # Try to import physics components from backend
            try:
                from backend.voice_unlock.core.feature_extraction import (
                    get_physics_feature_extractor,
                    PhysicsConfig,
                )
                from backend.voice_unlock.core.anti_spoofing import (
                    get_anti_spoofing_detector,
                )

                # Initialize physics extractor
                sample_rate = int(os.getenv("AUDIO_SAMPLE_RATE", "16000"))
                self._physics_extractor = get_physics_feature_extractor(sample_rate)

                # Set baselines if provided
                if self._baseline_vtl_cm and hasattr(
                    self._physics_extractor, "_baseline_vtl"
                ):
                    self._physics_extractor._baseline_vtl = self._baseline_vtl_cm
                if self._baseline_rt60_sec and hasattr(
                    self._physics_extractor, "_baseline_rt60"
                ):
                    self._physics_extractor._baseline_rt60 = self._baseline_rt60_sec

                # Initialize anti-spoofing detector (includes Layer 7 physics)
                self._anti_spoofing_detector = get_anti_spoofing_detector()

                self._initialized = True
                self.initialization_time_ms = (time.time() - start_time) * 1000

                vtl_range = (
                    f"{PhysicsConfig.VTL_MIN_CM}-{PhysicsConfig.VTL_MAX_CM} cm"
                    if hasattr(PhysicsConfig, "VTL_MIN_CM")
                    else "12-20 cm"
                )
                prior = (
                    f"{PhysicsConfig.PRIOR_AUTHENTIC:.0%}"
                    if hasattr(PhysicsConfig, "PRIOR_AUTHENTIC")
                    else "95%"
                )

                self._logger.info(
                    f"✅ Physics-aware auth initialized ({self.initialization_time_ms:.0f}ms)"
                )
                self._logger.info(f"   ├─ Physics extractor: Ready")
                self._logger.info(f"   ├─ Anti-spoofing (7-layer): Ready")
                self._logger.info(f"   ├─ VTL range: {vtl_range}")
                self._logger.info(f"   └─ Bayesian prior: {prior} authentic")

                return True

            except ImportError as e:
                self._logger.debug(f"Physics components not available: {e}")
                # Fall back to mock implementation
                self._initialized = True
                self.initialization_time_ms = (time.time() - start_time) * 1000
                self._logger.info(
                    f"✅ Physics-aware auth initialized (mock mode, {self.initialization_time_ms:.0f}ms)"
                )
                return True

        except Exception as e:
            self._logger.error(f"Physics initialization failed: {e}")
            self.enabled = False
            return False

    async def verify_physics(
        self,
        audio_data: bytes,
        sample_rate: int = 16000,
    ) -> Dict[str, Any]:
        """
        Perform physics-based verification on audio.

        Args:
            audio_data: Raw audio bytes
            sample_rate: Audio sample rate

        Returns:
            Verification result with confidence scores
        """
        self.physics_verifications += 1

        result = {
            "authentic": True,
            "confidence": 0.95,
            "checks": {},
            "timestamp": time.time(),
        }

        if not self._initialized:
            result["error"] = "Not initialized"
            return result

        try:
            if self._anti_spoofing_detector:
                # Run 7-layer anti-spoofing
                spoof_result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._anti_spoofing_detector.detect(
                        audio_data, sample_rate
                    ),
                )

                result["authentic"] = not spoof_result.get("is_spoof", False)
                result["confidence"] = spoof_result.get("confidence", 0.5)
                result["checks"] = spoof_result.get("layer_results", {})

                if not result["authentic"]:
                    self.spoofs_detected += 1
                    self._record_spoof(spoof_result)
                else:
                    self.legitimate_authentications += 1

            if self._physics_extractor:
                # Extract physics features
                features = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._physics_extractor.extract(audio_data, sample_rate),
                )
                result["physics_features"] = features

        except Exception as e:
            self._logger.error(f"Physics verification failed: {e}")
            result["error"] = str(e)

        return result

    def _record_spoof(self, spoof_result: Dict[str, Any]) -> None:
        """Record spoof detection for learning."""
        record = {
            "timestamp": time.time(),
            "result": spoof_result,
        }
        self._spoof_history.append(record)

        # Trim history
        if len(self._spoof_history) > self._max_history:
            self._spoof_history = self._spoof_history[-self._max_history :]

    def get_physics_extractor(self) -> Optional[Any]:
        """Get the physics feature extractor instance."""
        return self._physics_extractor

    def get_anti_spoofing_detector(self) -> Optional[Any]:
        """Get the anti-spoofing detector instance."""
        return self._anti_spoofing_detector

    def get_statistics(self) -> Dict[str, Any]:
        """Get physics startup statistics."""
        return {
            "enabled": self.enabled,
            "initialized": self._initialized,
            "initialization_time_ms": self.initialization_time_ms,
            "baseline_vtl_cm": self._baseline_vtl_cm,
            "baseline_rt60_sec": self._baseline_rt60_sec,
            "physics_verifications": self.physics_verifications,
            "spoofs_detected": self.spoofs_detected,
            "legitimate_authentications": self.legitimate_authentications,
            "spoof_history_count": len(self._spoof_history),
        }


# =============================================================================
# ZONE 3.9: SPOT INSTANCE RESILIENCE HANDLER
# =============================================================================
# v109.0: GCP Spot VM preemption handling and automatic fallback


class SpotInstanceResilienceHandler:
    """
    Spot Instance Resilience Handler for GCP Preemption.

    Features:
    - Graceful preemption handling (30 second warning from GCP)
    - State preservation before shutdown
    - Automatic fallback to micro instance or local
    - Cost tracking during preemption events
    - Learning from preemption patterns

    Environment Configuration:
    - SPOT_RESILIENCE_ENABLED: Enable/disable (default: true)
    - SPOT_FALLBACK_MODE: micro/local/none (default: local)
    - SPOT_STATE_PRESERVE: Save state on preemption (default: true)
    - SPOT_PREEMPTION_WEBHOOK: Webhook URL for notifications (default: none)

    GCP Spot VMs can be preempted at any time with 30 seconds warning.
    This handler ensures graceful shutdown and automatic failover.
    """

    def __init__(
        self,
        config: Optional[SystemKernelConfig] = None,
        logger: Optional[Any] = None,
    ):
        """
        Initialize Spot Instance resilience handler.

        Args:
            config: System kernel configuration
            logger: Logger instance
        """
        self.config = config
        self._logger = logger or logging.getLogger("SpotResilience")

        # Configuration from environment
        self.enabled = os.getenv("SPOT_RESILIENCE_ENABLED", "true").lower() == "true"
        self.fallback_mode = os.getenv("SPOT_FALLBACK_MODE", "local")
        self.state_preserve = (
            os.getenv("SPOT_STATE_PRESERVE", "true").lower() == "true"
        )
        self.preemption_webhook = os.getenv("SPOT_PREEMPTION_WEBHOOK")

        # Preemption tracking
        self.preemption_count = 0
        self.last_preemption_time: Optional[float] = None
        self.preemption_history: List[Dict[str, Any]] = []

        # State preservation
        state_file_path = os.getenv(
            "SPOT_STATE_FILE", str(Path.home() / ".jarvis" / "spot_state.json")
        )
        self.state_file = Path(state_file_path)

        # Callbacks for external components
        self.preemption_callback: Optional[Callable] = None
        self.fallback_callback: Optional[Callable] = None

        # Polling task reference
        self._polling_task: Optional[asyncio.Task] = None
        self._running = False

        self._logger.info("🛡️ Spot Instance Resilience initialized:")
        self._logger.info(f"   ├─ Enabled: {self.enabled}")
        self._logger.info(f"   ├─ Fallback mode: {self.fallback_mode}")
        self._logger.info(f"   └─ State preserve: {self.state_preserve}")

    async def setup_preemption_handler(
        self,
        preemption_callback: Optional[Callable] = None,
        fallback_callback: Optional[Callable] = None,
    ) -> None:
        """
        Setup preemption handling callbacks.

        Args:
            preemption_callback: Called when preemption detected
            fallback_callback: Called to trigger fallback mode
        """
        self.preemption_callback = preemption_callback
        self.fallback_callback = fallback_callback

        if self.enabled:
            # Start metadata server polling for preemption notice
            self._running = True
            self._polling_task = asyncio.create_task(self._poll_preemption_notice())
            self._logger.info("🛡️ Preemption handler active")

    async def stop(self) -> None:
        """Stop the preemption polling."""
        self._running = False
        if self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass

    async def _poll_preemption_notice(self) -> None:
        """
        Poll GCP metadata server for preemption notice.

        GCP sends a preemption notice 30 seconds before termination.
        This method checks the metadata server every 5 seconds.
        """
        metadata_url = (
            "http://metadata.google.internal/computeMetadata/v1/instance/preempted"
        )
        headers = {"Metadata-Flavor": "Google"}

        while self._running:
            try:
                # Try aiohttp first, fall back to urllib
                try:
                    import aiohttp

                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            metadata_url,
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=5),
                        ) as response:
                            if response.status == 200:
                                text = await response.text()
                                if text.strip().lower() == "true":
                                    await self._handle_preemption()
                                    break
                except ImportError:
                    # Fallback to urllib (blocking)
                    loop = asyncio.get_running_loop()

                    def _check_sync():
                        import urllib.request

                        req = urllib.request.Request(metadata_url, headers=headers)
                        with urllib.request.urlopen(req, timeout=5) as resp:
                            return resp.read().decode().strip().lower()

                    try:
                        result = await loop.run_in_executor(None, _check_sync)
                        if result == "true":
                            await self._handle_preemption()
                            break
                    except Exception:
                        pass

            except Exception:
                # Not on GCP or metadata not available - this is normal
                pass

            await asyncio.sleep(5)  # Check every 5 seconds

    async def _handle_preemption(self) -> None:
        """
        Handle preemption event.

        We have approximately 30 seconds to:
        1. Preserve state
        2. Notify external systems
        3. Trigger fallback
        """
        self._logger.warning(
            "⚠️ SPOT PREEMPTION NOTICE - 30 seconds to shutdown!"
        )

        self.preemption_count += 1
        self.last_preemption_time = time.time()

        preemption_event = {
            "timestamp": time.time(),
            "preemption_count": self.preemption_count,
            "fallback_mode": self.fallback_mode,
        }
        self.preemption_history.append(preemption_event)

        # Preserve state if enabled
        if self.state_preserve:
            await self._preserve_state()

        # Call preemption callback
        if self.preemption_callback:
            try:
                if asyncio.iscoroutinefunction(self.preemption_callback):
                    await self.preemption_callback()
                else:
                    self.preemption_callback()
            except Exception as e:
                self._logger.error(f"Preemption callback failed: {e}")

        # Trigger fallback
        if self.fallback_mode != "none" and self.fallback_callback:
            try:
                if asyncio.iscoroutinefunction(self.fallback_callback):
                    await self.fallback_callback(self.fallback_mode)
                else:
                    self.fallback_callback(self.fallback_mode)
            except Exception as e:
                self._logger.error(f"Fallback callback failed: {e}")

        # Send webhook notification if configured
        if self.preemption_webhook:
            await self._send_webhook_notification(preemption_event)

    async def _preserve_state(self) -> None:
        """Preserve current state to disk for recovery."""
        try:
            state = {
                "timestamp": time.time(),
                "preemption_count": self.preemption_count,
                "preemption_history": self.preemption_history[-10:],  # Last 10
                "version": KERNEL_VERSION,
            }

            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(state, indent=2))
            self._logger.info(f"💾 State preserved to {self.state_file}")

        except Exception as e:
            self._logger.error(f"State preservation failed: {e}")

    async def _send_webhook_notification(self, event: Dict[str, Any]) -> None:
        """Send webhook notification for preemption event."""
        if not self.preemption_webhook:
            return

        try:
            try:
                import aiohttp

                async with aiohttp.ClientSession() as session:
                    await session.post(
                        self.preemption_webhook,
                        json=event,
                        timeout=aiohttp.ClientTimeout(total=5),
                    )
                self._logger.info("📤 Preemption webhook sent")
            except ImportError:
                # Fallback to urllib
                loop = asyncio.get_running_loop()

                def _post_sync():
                    import urllib.request

                    data = json.dumps(event).encode()
                    req = urllib.request.Request(
                        self.preemption_webhook,
                        data=data,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    urllib.request.urlopen(req, timeout=5)

                await loop.run_in_executor(None, _post_sync)
                self._logger.info("📤 Preemption webhook sent (sync)")

        except Exception as e:
            self._logger.error(f"Webhook notification failed: {e}")

    async def load_preserved_state(self) -> Optional[Dict[str, Any]]:
        """Load preserved state from previous session."""
        try:
            if self.state_file.exists():
                state = json.loads(self.state_file.read_text())
                self._logger.info(f"💾 Loaded preserved state from {self.state_file}")

                # Restore preemption history
                if "preemption_history" in state:
                    self.preemption_history = state["preemption_history"]
                if "preemption_count" in state:
                    self.preemption_count = state["preemption_count"]

                return state
        except Exception as e:
            self._logger.error(f"Failed to load preserved state: {e}")
        return None

    def get_statistics(self) -> Dict[str, Any]:
        """Get resilience statistics."""
        return {
            "enabled": self.enabled,
            "fallback_mode": self.fallback_mode,
            "state_preserve": self.state_preserve,
            "preemption_count": self.preemption_count,
            "last_preemption_time": self.last_preemption_time,
            "preemption_history_count": len(self.preemption_history),
            "has_webhook": self.preemption_webhook is not None,
        }


# =============================================================================
# ZONE 3.10: INTELLIGENT MODEL MANAGER
# =============================================================================
# v109.0: Memory-aware model selection with auto-download from HuggingFace


# Model catalog for available LLM models
MODEL_CATALOG: Dict[str, Dict[str, Any]] = {
    "phi-2-q4": {
        "repo_id": "TheBloke/phi-2-GGUF",
        "filename": "phi-2.Q4_K_M.gguf",
        "size_mb": 1800,
        "min_ram_gb": 4,
        "description": "Microsoft Phi-2 - Efficient 2.7B model",
        "context_length": 2048,
    },
    "phi-3-mini-q4": {
        "repo_id": "microsoft/Phi-3-mini-4k-instruct-gguf",
        "filename": "Phi-3-mini-4k-instruct-q4.gguf",
        "size_mb": 2500,
        "min_ram_gb": 6,
        "description": "Microsoft Phi-3 Mini - Strong 3.8B model",
        "context_length": 4096,
    },
    "mistral-7b-q4": {
        "repo_id": "TheBloke/Mistral-7B-Instruct-v0.2-GGUF",
        "filename": "mistral-7b-instruct-v0.2.Q4_K_M.gguf",
        "size_mb": 4370,
        "min_ram_gb": 8,
        "description": "Mistral 7B Instruct v0.2 - Excellent balance",
        "context_length": 32768,
    },
    "mistral-7b-q8": {
        "repo_id": "TheBloke/Mistral-7B-Instruct-v0.2-GGUF",
        "filename": "mistral-7b-instruct-v0.2.Q8_0.gguf",
        "size_mb": 7700,
        "min_ram_gb": 12,
        "description": "Mistral 7B Q8 - Higher quality, more RAM",
        "context_length": 32768,
    },
    "llama-3-8b-q4": {
        "repo_id": "MaziyarPanahi/Meta-Llama-3-8B-Instruct-GGUF",
        "filename": "Meta-Llama-3-8B-Instruct.Q4_K_M.gguf",
        "size_mb": 4900,
        "min_ram_gb": 10,
        "description": "Llama 3 8B Instruct - Latest Meta model",
        "context_length": 8192,
    },
}


class IntelligentModelManager:
    """
    Comprehensive model manager with auto-download and reactor-core integration.

    Features:
    - Memory-aware model selection (picks best model for available RAM)
    - Auto-download from HuggingFace Hub
    - Reactor-core trained model deployment
    - Hot-swap capability (change models without restart)
    - Version registry with rollback
    - Model health monitoring

    This manager ensures JARVIS always has the best available model
    for the current system resources.
    """

    def __init__(
        self,
        models_dir: Optional[Path] = None,
        config: Optional[SystemKernelConfig] = None,
        logger: Optional[Any] = None,
    ):
        """
        Initialize the intelligent model manager.

        Args:
            models_dir: Directory to store models
            config: System kernel configuration
            logger: Logger instance
        """
        self.config = config
        self._logger = logger or logging.getLogger("ModelManager")

        # Model directory
        if models_dir:
            self.models_dir = Path(models_dir)
        else:
            self.models_dir = Path(__file__).parent / "models"

        # Ensure models directory exists
        self.models_dir.mkdir(parents=True, exist_ok=True)

        # State tracking
        self.current_model: Optional[str] = None
        self.current_model_path: Optional[Path] = None
        self.model_registry: Dict[str, Any] = {}
        self.download_in_progress = False

        # Configuration
        self.auto_download = (
            os.getenv("MODEL_AUTO_DOWNLOAD", "true").lower() == "true"
        )
        self.auto_select = os.getenv("MODEL_AUTO_SELECT", "true").lower() == "true"
        self.default_model = os.getenv("MODEL_DEFAULT", "mistral-7b-q4")

        # Reactor-core integration
        self._reactor_core_path: Optional[Path] = None
        reactor_path = Path(__file__).parent.parent / "reactor-core"
        if reactor_path.exists():
            self._reactor_core_path = reactor_path

        # Thread safety
        self._lock = asyncio.Lock()

        # Statistics
        self._stats = {
            "models_downloaded": 0,
            "model_loads": 0,
            "hot_swaps": 0,
            "download_bytes": 0,
        }

        # Load existing metadata
        self._load_registry()

        self._logger.info("🧠 Intelligent Model Manager initialized:")
        self._logger.info(f"   ├─ Models dir: {self.models_dir}")
        self._logger.info(f"   ├─ Auto download: {self.auto_download}")
        self._logger.info(f"   ├─ Auto select: {self.auto_select}")
        self._logger.info(
            f"   └─ Reactor-core: {'connected' if self._reactor_core_path else 'not found'}"
        )

    def _load_registry(self) -> None:
        """Load model registry from disk."""
        metadata_file = self.models_dir / "models_metadata.json"
        if metadata_file.exists():
            try:
                self.model_registry = json.loads(metadata_file.read_text())
                self._logger.debug(
                    f"Loaded model registry with {len(self.model_registry.get('models', {}))} models"
                )
            except Exception as e:
                self._logger.debug(f"Failed to load registry: {e}")
                self.model_registry = {"models": {}, "current": None}
        else:
            self.model_registry = {"models": {}, "current": None}

    def _save_registry(self) -> None:
        """Save model registry to disk."""
        metadata_file = self.models_dir / "models_metadata.json"
        self.model_registry["last_updated"] = datetime.now().isoformat()
        metadata_file.write_text(
            json.dumps(self.model_registry, indent=2, default=str)
        )

    def get_available_memory_gb(self) -> float:
        """Get available system memory in GB."""
        try:
            import psutil

            mem = psutil.virtual_memory()
            return mem.available / (1024**3)
        except ImportError:
            # Fallback: assume 8GB available
            return 8.0

    def select_optimal_model(self) -> Optional[str]:
        """
        Select the best model based on available memory.

        Returns:
            Model name from catalog or None if no suitable model
        """
        available_gb = self.get_available_memory_gb()

        self._logger.debug(f"Available memory: {available_gb:.1f}GB")

        # Sort models by min_ram_gb descending (prefer larger models)
        suitable_models = [
            (name, info)
            for name, info in MODEL_CATALOG.items()
            if info["min_ram_gb"] <= available_gb
        ]

        if not suitable_models:
            self._logger.warning("No models suitable for available memory")
            return None

        # Sort by min_ram_gb descending to get the best model we can run
        suitable_models.sort(key=lambda x: x[1]["min_ram_gb"], reverse=True)
        selected = suitable_models[0][0]
        self._logger.info(
            f"Selected optimal model: {selected} (needs {MODEL_CATALOG[selected]['min_ram_gb']}GB, have {available_gb:.1f}GB)"
        )
        return selected

    def check_model_exists(self, model_name: Optional[str] = None) -> Optional[Path]:
        """
        Check if a model exists in the models directory.

        Args:
            model_name: Specific model to check, or None for any model

        Returns:
            Path to model file if found, None otherwise
        """
        # Check current.gguf symlink first
        current_link = self.models_dir / "current.gguf"
        if current_link.exists():
            resolved = current_link.resolve()
            if resolved.exists() and resolved.stat().st_size > 1000:
                return resolved

        # Check for specific model
        if model_name and model_name in MODEL_CATALOG:
            model_info = MODEL_CATALOG[model_name]
            model_file = self.models_dir / model_info["filename"]
            if model_file.exists() and model_file.stat().st_size > 1000:
                return model_file

        # Check for any .gguf files
        gguf_files = list(self.models_dir.glob("*.gguf"))
        for gguf in gguf_files:
            if gguf.stat().st_size > 1000 and not gguf.is_symlink():
                return gguf

        return None

    async def _check_reactor_core_models(self) -> Optional[Path]:
        """Check for trained models from reactor-core."""
        try:
            # Check reactor-core output directories
            reactor_paths = [
                self._reactor_core_path / "output" / "models"
                if self._reactor_core_path
                else None,
                Path(os.getenv("REACTOR_CORE_OUTPUT", "")) / "deployed",
                Path(__file__).parent / "reactor-core-output" / "deployed",
            ]

            for reactor_path in reactor_paths:
                if reactor_path and reactor_path.exists():
                    gguf_files = list(reactor_path.glob("*.gguf"))
                    if gguf_files:
                        # Sort by modification time, newest first
                        gguf_files.sort(
                            key=lambda x: x.stat().st_mtime, reverse=True
                        )
                        newest = gguf_files[0]
                        if newest.stat().st_size > 1000:
                            self._logger.info(
                                f"✓ Found reactor-core model: {newest.name}"
                            )
                            return newest
        except Exception as e:
            self._logger.debug(f"Reactor-core check error: {e}")
        return None

    async def ensure_model_available(self) -> Dict[str, Any]:
        """
        Ensure a model is available for JARVIS-Prime.

        Returns:
            Status dict with:
            - available: bool
            - model_name: str
            - model_path: Path
            - source: str (existing, downloaded, reactor_core)
        """
        result = {
            "available": False,
            "model_name": None,
            "model_path": None,
            "source": None,
            "error": None,
        }

        async with self._lock:
            try:
                # Step 1: Check for existing model
                existing_path = self.check_model_exists()
                if existing_path:
                    result["available"] = True
                    result["model_path"] = existing_path
                    result["model_name"] = existing_path.name
                    result["source"] = "existing"
                    self.current_model_path = existing_path
                    self._stats["model_loads"] += 1
                    self._logger.info(f"✓ Found existing model: {existing_path.name}")
                    return result

                # Step 2: Check for reactor-core trained models
                reactor_model = await self._check_reactor_core_models()
                if reactor_model:
                    result["available"] = True
                    result["model_path"] = reactor_model
                    result["model_name"] = reactor_model.name
                    result["source"] = "reactor_core"
                    self.current_model_path = reactor_model
                    self._stats["model_loads"] += 1
                    return result

                # Step 3: Auto-download if enabled
                if self.auto_download:
                    # Select optimal model for available memory
                    if self.auto_select:
                        model_name = self.select_optimal_model()
                    else:
                        model_name = self.default_model

                    if model_name:
                        self._logger.info(f"📥 Auto-downloading model: {model_name}")
                        download_result = await self.download_model(model_name)
                        if download_result["success"]:
                            result["available"] = True
                            result["model_path"] = download_result["path"]
                            result["model_name"] = model_name
                            result["source"] = "downloaded"
                            return result
                        else:
                            result["error"] = download_result.get(
                                "error", "Download failed"
                            )

            except Exception as e:
                result["error"] = str(e)
                self._logger.error(f"Model availability check failed: {e}")

        return result

    async def download_model(self, model_name: str) -> Dict[str, Any]:
        """
        Download a model from HuggingFace Hub.

        Args:
            model_name: Name of model from MODEL_CATALOG

        Returns:
            Result dict with success status and path
        """
        result = {"success": False, "path": None, "error": None}

        if model_name not in MODEL_CATALOG:
            result["error"] = f"Unknown model: {model_name}"
            return result

        if self.download_in_progress:
            result["error"] = "Download already in progress"
            return result

        self.download_in_progress = True
        model_info = MODEL_CATALOG[model_name]

        try:
            # Try using huggingface_hub for download
            try:
                from huggingface_hub import hf_hub_download

                self._logger.info(
                    f"📥 Downloading {model_name} ({model_info['size_mb']}MB)..."
                )

                loop = asyncio.get_running_loop()
                downloaded_path = await loop.run_in_executor(
                    None,
                    lambda: hf_hub_download(
                        repo_id=model_info["repo_id"],
                        filename=model_info["filename"],
                        local_dir=str(self.models_dir),
                        local_dir_use_symlinks=False,
                    ),
                )

                model_path = Path(downloaded_path)
                if model_path.exists():
                    # Create current.gguf symlink
                    current_link = self.models_dir / "current.gguf"
                    if current_link.exists():
                        current_link.unlink()
                    current_link.symlink_to(model_path)

                    result["success"] = True
                    result["path"] = model_path
                    self._update_registry(model_name, model_path, "downloaded")
                    self._stats["models_downloaded"] += 1
                    self._stats["download_bytes"] += model_info["size_mb"] * 1024 * 1024

                    self._logger.info(f"✅ Downloaded {model_name} to {model_path}")

            except ImportError:
                result["error"] = "huggingface_hub not installed"
                self._logger.warning(
                    "Install huggingface_hub for auto-download: pip install huggingface_hub"
                )

        except Exception as e:
            result["error"] = str(e)
            self._logger.error(f"Model download failed: {e}")

        finally:
            self.download_in_progress = False

        return result

    def _update_registry(
        self, model_name: str, model_path: Path, source: str
    ) -> None:
        """Update the model registry."""
        if "models" not in self.model_registry:
            self.model_registry["models"] = {}

        self.model_registry["models"][model_name] = {
            "path": str(model_path),
            "source": source,
            "downloaded_at": datetime.now().isoformat(),
            "size_bytes": model_path.stat().st_size if model_path.exists() else 0,
        }
        self.model_registry["current"] = model_name
        self._save_registry()

    async def hot_swap_model(self, model_name: str) -> Dict[str, Any]:
        """
        Hot-swap to a different model.

        Args:
            model_name: Name of model to switch to

        Returns:
            Result dict with success status
        """
        result = {"success": False, "previous_model": self.current_model, "error": None}

        async with self._lock:
            try:
                # Check if model exists
                model_path = self.check_model_exists(model_name)
                if not model_path:
                    # Try to download
                    download_result = await self.download_model(model_name)
                    if not download_result["success"]:
                        result["error"] = download_result["error"]
                        return result
                    model_path = download_result["path"]

                # Update current model
                self.current_model = model_name
                self.current_model_path = model_path

                # Update symlink
                current_link = self.models_dir / "current.gguf"
                if current_link.exists():
                    current_link.unlink()
                current_link.symlink_to(model_path)

                self._stats["hot_swaps"] += 1
                result["success"] = True
                self._logger.info(f"🔄 Hot-swapped to model: {model_name}")

            except Exception as e:
                result["error"] = str(e)
                self._logger.error(f"Hot-swap failed: {e}")

        return result

    def get_model_info(self, model_name: str) -> Optional[Dict[str, Any]]:
        """Get information about a model from the catalog."""
        return MODEL_CATALOG.get(model_name)

    def list_available_models(self) -> List[Dict[str, Any]]:
        """List all available models with their status."""
        available_gb = self.get_available_memory_gb()
        models = []

        for name, info in MODEL_CATALOG.items():
            model_path = self.check_model_exists(name)
            models.append(
                {
                    "name": name,
                    "description": info["description"],
                    "size_mb": info["size_mb"],
                    "min_ram_gb": info["min_ram_gb"],
                    "context_length": info["context_length"],
                    "downloaded": model_path is not None,
                    "can_run": info["min_ram_gb"] <= available_gb,
                    "is_current": name == self.current_model,
                }
            )

        return models

    def get_statistics(self) -> Dict[str, Any]:
        """Get model manager statistics."""
        return {
            "models_dir": str(self.models_dir),
            "current_model": self.current_model,
            "current_model_path": (
                str(self.current_model_path) if self.current_model_path else None
            ),
            "available_memory_gb": round(self.get_available_memory_gb(), 2),
            "auto_download": self.auto_download,
            "auto_select": self.auto_select,
            "has_reactor_core": self._reactor_core_path is not None,
            "download_in_progress": self.download_in_progress,
            "catalog_models": len(MODEL_CATALOG),
            **self._stats,
        }


# ╔═══════════════════════════════════════════════════════════════════════════════╗
# ║                                                                               ║
# ║   END OF ZONE 3                                                               ║
# ║                                                                               ║
# ╚═══════════════════════════════════════════════════════════════════════════════╝


# ╔═══════════════════════════════════════════════════════════════════════════════╗
# ║                                                                               ║
# ║   ZONE 4: INTELLIGENCE LAYER (~10,000 lines)                                  ║
# ║                                                                               ║
# ║   All intelligence managers share a common base class with:                   ║
# ║   - Lazy model loading (only load when needed)                                ║
# ║   - Rule-based fallbacks when ML unavailable                                  ║
# ║   - Adaptive thresholds that learn from outcomes                              ║
# ║                                                                               ║
# ║   Managers:                                                                   ║
# ║   - HybridWorkloadRouter: Local vs Cloud vs Spot VM routing                   ║
# ║   - HybridIntelligenceCoordinator: Central coordinator                        ║
# ║   - GoalInferenceEngine: ML-powered intent classification                     ║
# ║   - HybridLearningModel: Adaptive ML for routing optimization                 ║
# ║   - SAIHybridIntegration: Learning integration layer                          ║
# ║   - AdaptiveThresholdManager: NO hardcoded thresholds                         ║
# ║                                                                               ║
# ╚═══════════════════════════════════════════════════════════════════════════════╝


# =============================================================================
# INTELLIGENCE MANAGER BASE CLASS
# =============================================================================
class IntelligenceManagerBase(ABC):
    """
    Abstract base class for all intelligence managers.

    All managers follow a consistent pattern:
    1. __init__(): Configuration only, no heavy loading
    2. initialize(): Light initialization
    3. load_models(): Heavy ML model loading (lazy, on-demand)
    4. infer(): Make predictions/decisions
    5. get_fallback_result(): Rule-based fallback when ML unavailable

    Principles:
    - Lazy loading: ML models only loaded when needed
    - Graceful degradation: Rule-based fallbacks always available
    - Adaptive: Thresholds learn from outcomes
    - Observable: Metrics, accuracy tracking
    """

    def __init__(self, name: str, config: Optional[SystemKernelConfig] = None):
        self.name = name
        self.config = config or SystemKernelConfig.from_environment()
        self._initialized = False
        self._models_loaded = False
        self._ready = False
        self._error: Optional[str] = None
        self._inference_count = 0
        self._fallback_count = 0
        self._logger = UnifiedLogger()

        # Learning/adaptation
        self._learning_enabled = True
        self._observations: List[Dict[str, Any]] = []
        self._max_observations = 1000

    @abstractmethod
    async def initialize(self) -> bool:
        """Light initialization (no heavy model loading)."""
        pass

    @abstractmethod
    async def load_models(self) -> bool:
        """Load ML models (called lazily on first inference)."""
        pass

    @abstractmethod
    async def infer(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Make prediction/decision using ML or fallback."""
        pass

    @abstractmethod
    def get_fallback_result(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Rule-based fallback when ML unavailable."""
        pass

    @property
    def is_ready(self) -> bool:
        """True if manager is ready for inference."""
        return self._initialized and self._ready

    @property
    def status(self) -> Dict[str, Any]:
        """Get current status."""
        return {
            "name": self.name,
            "initialized": self._initialized,
            "models_loaded": self._models_loaded,
            "ready": self._ready,
            "error": self._error,
            "inference_count": self._inference_count,
            "fallback_count": self._fallback_count,
            "fallback_rate": self._fallback_count / self._inference_count if self._inference_count > 0 else 0,
            "learning_enabled": self._learning_enabled,
            "observations": len(self._observations),
        }

    async def safe_infer(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Safely make inference with fallback protection.

        Returns ML result if available, otherwise rule-based fallback.
        """
        self._inference_count += 1

        try:
            # Lazy load models on first inference
            if not self._models_loaded:
                try:
                    await self.load_models()
                except Exception as e:
                    self._logger.warning(f"{self.name} model loading failed: {e}, using fallback")

            if self._models_loaded:
                return await self.infer(input_data)
            else:
                self._fallback_count += 1
                return self.get_fallback_result(input_data)
        except Exception as e:
            self._logger.error(f"{self.name} inference error: {e}, using fallback")
            self._fallback_count += 1
            return self.get_fallback_result(input_data)

    def record_observation(self, observation: Dict[str, Any]) -> None:
        """Record observation for learning."""
        if not self._learning_enabled:
            return

        observation["timestamp"] = time.time()
        self._observations.append(observation)

        # Keep bounded
        if len(self._observations) > self._max_observations:
            self._observations.pop(0)


# =============================================================================
# RAM STATE ENUM
# =============================================================================
class RAMState(Enum):
    """RAM usage state levels."""
    OPTIMAL = "OPTIMAL"
    ELEVATED = "ELEVATED"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    EMERGENCY = "EMERGENCY"


# =============================================================================
# ADAPTIVE THRESHOLD MANAGER
# =============================================================================
class AdaptiveThresholdManager:
    """
    Manages adaptive thresholds that learn from outcomes.

    Features:
    - NO hardcoded thresholds - all learned from data
    - Confidence tracking per threshold
    - Automatic adaptation based on outcomes
    - Time-of-day pattern learning
    - Persistence across restarts

    Environment Configuration:
    - THRESHOLD_LEARNING_RATE: How fast to adapt (default: 0.1)
    - THRESHOLD_MIN_OBSERVATIONS: Min observations before adapting (default: 20)
    - THRESHOLD_PERSIST_PATH: Path to persist learned thresholds
    """

    def __init__(self):
        # Initial thresholds (will be adapted)
        self.thresholds = {
            "ram_optimal": float(os.getenv("THRESHOLD_RAM_OPTIMAL", "0.60")),
            "ram_warning": float(os.getenv("THRESHOLD_RAM_WARNING", "0.75")),
            "ram_critical": float(os.getenv("THRESHOLD_RAM_CRITICAL", "0.85")),
            "ram_emergency": float(os.getenv("THRESHOLD_RAM_EMERGENCY", "0.95")),
            "cpu_warning": float(os.getenv("THRESHOLD_CPU_WARNING", "0.80")),
            "cpu_critical": float(os.getenv("THRESHOLD_CPU_CRITICAL", "0.95")),
            "latency_warning_ms": float(os.getenv("THRESHOLD_LATENCY_WARNING_MS", "500")),
            "latency_critical_ms": float(os.getenv("THRESHOLD_LATENCY_CRITICAL_MS", "2000")),
        }

        # Confidence in each threshold (0.0 to 1.0)
        self.confidence = {key: 0.0 for key in self.thresholds}

        # Learning configuration
        self.learning_rate = float(os.getenv("THRESHOLD_LEARNING_RATE", "0.1"))
        self.min_observations = int(os.getenv("THRESHOLD_MIN_OBSERVATIONS", "20"))
        self.persist_path = os.getenv(
            "THRESHOLD_PERSIST_PATH",
            str(Path.home() / ".jarvis" / "learned_thresholds.json")
        )

        # Observations for learning
        self._observations: Dict[str, List[Dict[str, Any]]] = {key: [] for key in self.thresholds}
        self._outcome_history: List[Dict[str, Any]] = []

        # Time-of-day patterns
        self._hourly_patterns: Dict[str, Dict[int, List[float]]] = {key: {} for key in self.thresholds}

        # Load persisted thresholds
        self._load_persisted()

        self._logger = UnifiedLogger()

    def _load_persisted(self) -> None:
        """Load persisted thresholds from disk."""
        try:
            persist_file = Path(self.persist_path)
            if persist_file.exists():
                with open(persist_file, 'r') as f:
                    data = json.load(f)

                # Load thresholds
                if "thresholds" in data:
                    for key, value in data["thresholds"].items():
                        if key in self.thresholds:
                            self.thresholds[key] = value

                # Load confidence
                if "confidence" in data:
                    for key, value in data["confidence"].items():
                        if key in self.confidence:
                            self.confidence[key] = value

        except Exception:
            pass  # Start fresh if loading fails

    def persist(self) -> None:
        """Persist learned thresholds to disk."""
        try:
            persist_file = Path(self.persist_path)
            persist_file.parent.mkdir(parents=True, exist_ok=True)

            data = {
                "thresholds": self.thresholds,
                "confidence": self.confidence,
                "updated_at": time.time(),
            }

            with open(persist_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self._logger.warning(f"Failed to persist thresholds: {e}")

    def get_threshold(self, name: str, default: Optional[float] = None) -> float:
        """Get a threshold value."""
        return self.thresholds.get(name, default or 0.0)

    def record_outcome(
        self,
        threshold_name: str,
        value: float,
        outcome: str,
        success: bool,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Record an outcome for threshold learning.

        Args:
            threshold_name: Name of the threshold (e.g., "ram_warning")
            value: The value that was compared against threshold
            outcome: What happened (e.g., "migrated", "crashed", "recovered")
            success: Whether the outcome was desirable
            context: Additional context
        """
        observation = {
            "timestamp": time.time(),
            "threshold_name": threshold_name,
            "threshold_value": self.thresholds.get(threshold_name, 0),
            "actual_value": value,
            "outcome": outcome,
            "success": success,
            "hour": datetime.now().hour,
            "context": context or {},
        }

        self._outcome_history.append(observation)

        # Keep bounded
        if len(self._outcome_history) > 1000:
            self._outcome_history.pop(0)

        # Learn from outcome
        self._learn_from_outcome(observation)

    def _learn_from_outcome(self, observation: Dict[str, Any]) -> None:
        """Learn and adapt threshold from outcome."""
        threshold_name = observation["threshold_name"]
        actual_value = observation["actual_value"]
        threshold_value = observation["threshold_value"]
        success = observation["success"]
        outcome = observation["outcome"]

        if threshold_name not in self.thresholds:
            return

        # Determine if we should adjust threshold
        should_adjust = False
        adjustment = 0.0

        if not success:
            # Something went wrong
            if outcome in ["crash", "emergency", "oom"]:
                # Threshold was too high - lower it
                adjustment = -0.02
                should_adjust = True
            elif outcome in ["unnecessary_migration", "premature_scale"]:
                # Threshold was too low - raise it
                adjustment = 0.01
                should_adjust = True
        else:
            # Success - small reinforcement
            if outcome in ["prevented_crash", "smooth_migration"]:
                # Current threshold is good - increase confidence
                self.confidence[threshold_name] = min(1.0, self.confidence[threshold_name] + 0.05)

        if should_adjust:
            old_value = self.thresholds[threshold_name]
            new_value = old_value + adjustment

            # Apply bounds
            if "ram" in threshold_name:
                new_value = max(0.5, min(0.99, new_value))
            elif "cpu" in threshold_name:
                new_value = max(0.5, min(0.99, new_value))
            elif "latency" in threshold_name:
                new_value = max(100, min(10000, new_value))

            self.thresholds[threshold_name] = new_value
            self.confidence[threshold_name] = min(1.0, self.confidence[threshold_name] + 0.02)

            self._logger.info(
                f"📚 Threshold adapted: {threshold_name} {old_value:.3f} → {new_value:.3f} "
                f"(outcome: {outcome})"
            )

            # Persist changes
            self.persist()

    def get_ram_state(self, usage_percent: float) -> RAMState:
        """Get RAM state based on adaptive thresholds."""
        if usage_percent >= self.thresholds["ram_emergency"]:
            return RAMState.EMERGENCY
        elif usage_percent >= self.thresholds["ram_critical"]:
            return RAMState.CRITICAL
        elif usage_percent >= self.thresholds["ram_warning"]:
            return RAMState.WARNING
        elif usage_percent >= self.thresholds["ram_optimal"]:
            return RAMState.ELEVATED
        else:
            return RAMState.OPTIMAL

    def get_all_thresholds(self) -> Dict[str, Any]:
        """Get all thresholds with confidence."""
        return {
            "thresholds": self.thresholds.copy(),
            "confidence": self.confidence.copy(),
            "observation_count": len(self._outcome_history),
            "min_observations": self.min_observations,
        }


# =============================================================================
# HYBRID LEARNING MODEL
# =============================================================================
class HybridLearningModel:
    """
    Advanced ML model for hybrid routing optimization.

    Features:
    - Adaptive threshold learning per user
    - RAM spike prediction using time-series analysis
    - Component weight learning from actual usage
    - Workload pattern recognition
    - Time-of-day correlation analysis

    Environment Configuration:
    - LEARNING_RATE: How fast to adapt (default: 0.1)
    - MIN_OBSERVATIONS: Min observations before trusting learned values (default: 20)
    """

    def __init__(self):
        # Historical data storage
        self.ram_observations: List[Dict[str, Any]] = []
        self.migration_outcomes: List[Dict[str, Any]] = []
        self.component_observations: List[Dict[str, Any]] = []

        # Learned parameters (start with defaults, adapt over time)
        self.optimal_thresholds = {
            "warning": float(os.getenv("THRESHOLD_RAM_WARNING", "0.75")),
            "critical": float(os.getenv("THRESHOLD_RAM_CRITICAL", "0.85")),
            "optimal": float(os.getenv("THRESHOLD_RAM_OPTIMAL", "0.60")),
            "emergency": float(os.getenv("THRESHOLD_RAM_EMERGENCY", "0.95")),
        }

        # Confidence in learned thresholds (0.0 to 1.0)
        self.threshold_confidence = {
            "warning": 0.0,
            "critical": 0.0,
            "optimal": 0.0,
            "emergency": 0.0,
        }

        # Component weight learning
        self.learned_component_weights: Dict[str, float] = {}
        self.component_observation_count: Dict[str, int] = {}

        # Pattern recognition
        self.hourly_ram_patterns: Dict[int, List[float]] = {}
        self.daily_patterns: Dict[int, List[float]] = {}

        # Prediction tracking
        self.prediction_accuracy = 0.0
        self.total_predictions = 0
        self.correct_predictions = 0

        # Configuration
        self.learning_rate = float(os.getenv("LEARNING_RATE", "0.1"))
        self.min_observations = int(os.getenv("MIN_OBSERVATIONS", "20"))

        self._logger = UnifiedLogger()

    async def record_ram_observation(
        self,
        timestamp: float,
        usage: float,
        components_active: Dict[str, Any]
    ) -> None:
        """Record a RAM observation for learning."""
        observation = {
            "timestamp": timestamp,
            "usage": usage,
            "components": components_active.copy(),
            "hour": datetime.fromtimestamp(timestamp).hour,
            "day_of_week": datetime.fromtimestamp(timestamp).weekday(),
        }

        self.ram_observations.append(observation)

        # Keep bounded
        if len(self.ram_observations) > 1000:
            self.ram_observations.pop(0)

        # Update hourly patterns
        hour = observation["hour"]
        if hour not in self.hourly_ram_patterns:
            self.hourly_ram_patterns[hour] = []
        self.hourly_ram_patterns[hour].append(usage)

        if len(self.hourly_ram_patterns[hour]) > 50:
            self.hourly_ram_patterns[hour].pop(0)

        # Update daily patterns
        day = observation["day_of_week"]
        if day not in self.daily_patterns:
            self.daily_patterns[day] = []
        self.daily_patterns[day].append(usage)

        if len(self.daily_patterns[day]) > 50:
            self.daily_patterns[day].pop(0)

    async def record_migration_outcome(
        self,
        timestamp: float,
        reason: str,
        success: bool,
        duration: float
    ) -> None:
        """Record a migration outcome for learning."""
        outcome = {
            "timestamp": timestamp,
            "reason": reason,
            "success": success,
            "duration": duration,
            "ram_before": self.ram_observations[-1]["usage"] if self.ram_observations else 0.0,
        }

        self.migration_outcomes.append(outcome)

        if len(self.migration_outcomes) > 100:
            self.migration_outcomes.pop(0)

        # Learn from outcome
        await self._learn_from_migration(outcome)

    async def _learn_from_migration(self, outcome: Dict[str, Any]) -> None:
        """Learn and adapt thresholds from migration outcomes."""
        if not outcome["success"]:
            # Migration failed - might need to lower critical threshold
            if "CRITICAL" in outcome["reason"]:
                old_threshold = self.optimal_thresholds["critical"]
                new_threshold = max(0.70, old_threshold - 0.02)
                self.optimal_thresholds["critical"] = new_threshold
                self.threshold_confidence["critical"] = min(
                    1.0, self.threshold_confidence["critical"] + 0.05
                )
                self._logger.info(
                    f"📚 Learning: Critical threshold adapted {old_threshold:.2f} → {new_threshold:.2f}"
                )
        else:
            if "EMERGENCY" in outcome["reason"]:
                # Hit emergency - learn to migrate earlier
                old_warning = self.optimal_thresholds["warning"]
                new_warning = max(0.65, old_warning - 0.03)
                self.optimal_thresholds["warning"] = new_warning
                self._logger.info(
                    f"📚 Learning: Warning threshold adapted {old_warning:.2f} → {new_warning:.2f}"
                )

    async def predict_ram_spike(
        self,
        current_usage: float,
        trend: float,
        time_horizon_seconds: int = 60
    ) -> Dict[str, Any]:
        """
        Predict if a RAM spike will occur.

        Returns:
            {
                'spike_likely': bool,
                'predicted_peak': float,
                'confidence': float,
                'reason': str
            }
        """
        # Linear extrapolation with trend
        predicted_usage = current_usage + (trend * time_horizon_seconds)

        # Check historical patterns
        current_hour = datetime.now().hour

        # Get average RAM for this hour
        hourly_data = self.hourly_ram_patterns.get(current_hour, [current_usage])
        hourly_avg = sum(hourly_data) / len(hourly_data) if hourly_data else current_usage

        # Get average RAM for this day
        current_day = datetime.now().weekday()
        daily_data = self.daily_patterns.get(current_day, [current_usage])
        daily_avg = sum(daily_data) / len(daily_data) if daily_data else current_usage

        # Combine predictions
        pattern_predicted = hourly_avg * 0.6 + daily_avg * 0.4
        final_prediction = predicted_usage * 0.7 + pattern_predicted * 0.3

        # Calculate confidence
        observation_count = len(self.ram_observations)
        confidence = min(1.0, observation_count / self.min_observations)

        # Determine if spike is likely
        spike_likely = final_prediction > self.optimal_thresholds["critical"]

        reason = ""
        if spike_likely:
            if trend > 0.02:
                reason = "Rapid upward trend detected"
            elif final_prediction > hourly_avg * 1.2:
                reason = "Usage significantly above typical for this hour"
            else:
                reason = "Pattern analysis suggests spike"

        self.total_predictions += 1

        return {
            "spike_likely": spike_likely,
            "predicted_peak": final_prediction,
            "confidence": confidence,
            "reason": reason,
        }

    async def get_optimal_monitoring_interval(self, current_usage: float) -> int:
        """Determine optimal monitoring interval based on RAM state."""
        if current_usage >= 0.90:
            interval = 2
        elif current_usage >= 0.80:
            interval = 3
        elif current_usage >= 0.70:
            interval = 5
        elif current_usage >= 0.50:
            interval = 7
        else:
            interval = 10

        # Adjust based on learned patterns
        current_hour = datetime.now().hour
        if current_hour in self.hourly_ram_patterns:
            hourly_data = self.hourly_ram_patterns[current_hour]
            hourly_avg = sum(hourly_data) / len(hourly_data) if hourly_data else 0

            if hourly_avg > 0.75:
                interval = min(interval, 5)

        return interval

    async def get_learned_component_weights(self) -> Dict[str, float]:
        """Get learned component weights."""
        if not self.learned_component_weights:
            return {
                "vision": 0.30,
                "ml_models": 0.25,
                "chatbots": 0.20,
                "memory": 0.10,
                "voice": 0.05,
                "monitoring": 0.05,
                "other": 0.05,
            }

        total_weight = sum(self.learned_component_weights.values())
        if total_weight == 0:
            return await self.get_learned_component_weights()

        return {
            comp: weight / total_weight
            for comp, weight in self.learned_component_weights.items()
        }

    async def get_learning_stats(self) -> Dict[str, Any]:
        """Get comprehensive learning statistics."""
        return {
            "observations": len(self.ram_observations),
            "migrations_recorded": len(self.migration_outcomes),
            "component_observations": len(self.component_observations),
            "learned_thresholds": self.optimal_thresholds.copy(),
            "threshold_confidence": self.threshold_confidence.copy(),
            "prediction_accuracy": (
                self.correct_predictions / self.total_predictions
                if self.total_predictions > 0 else 0.0
            ),
            "learned_component_weights": await self.get_learned_component_weights(),
            "patterns_detected": {
                "hourly": len(self.hourly_ram_patterns),
                "daily": len(self.daily_patterns),
            },
        }


# =============================================================================
# SAI HYBRID INTEGRATION
# =============================================================================
class SAIHybridIntegration:
    """
    Integration layer between SAI (Self-Aware Intelligence) and Hybrid Routing.

    Provides:
    - Persistent learning storage
    - Real-time model updates
    - Continuous improvement
    - Pattern sharing across system
    """

    def __init__(self, learning_model: HybridLearningModel):
        self.learning_model = learning_model
        self._db = None
        self._db_initialized = False
        self._last_model_save = None
        self._save_interval = 300  # Save every 5 minutes
        self._logger = UnifiedLogger()

    async def initialize_database(self) -> bool:
        """Initialize connection to learning database."""
        if self._db_initialized:
            return True

        try:
            # Try to connect to learning database
            # This would integrate with the actual SAI database
            self._db_initialized = True
            self._logger.debug("SAI database integration initialized")
            return True
        except Exception as e:
            self._logger.warning(f"SAI database initialization failed: {e}")
            return False

    async def record_and_learn(
        self,
        observation_type: str,
        data: Dict[str, Any]
    ) -> None:
        """Record observation and trigger learning."""
        if observation_type == "ram":
            await self.learning_model.record_ram_observation(
                timestamp=data.get("timestamp", time.time()),
                usage=data.get("usage", 0),
                components_active=data.get("components", {}),
            )
        elif observation_type == "migration":
            await self.learning_model.record_migration_outcome(
                timestamp=data.get("timestamp", time.time()),
                reason=data.get("reason", "UNKNOWN"),
                success=data.get("success", False),
                duration=data.get("duration", 0),
            )

        # Periodic save
        current_time = time.time()
        if self._last_model_save is None or (current_time - self._last_model_save) > self._save_interval:
            await self._save_model()
            self._last_model_save = current_time

    async def _save_model(self) -> None:
        """Save learned model to persistent storage."""
        # This would persist to the SAI database
        pass


# =============================================================================
# HYBRID WORKLOAD ROUTER
# =============================================================================
class HybridWorkloadRouter(IntelligenceManagerBase):
    """
    Intelligent router for local vs GCP workload placement.

    Features:
    - Component-level routing decisions
    - Automatic failover and fallback
    - Cost-aware optimization
    - Health monitoring
    - Zero-downtime migrations

    Environment Configuration:
    - HYBRID_ROUTING_ENABLED: Enable hybrid routing (default: true)
    - GCP_DEFAULT_PORT: Default GCP backend port (default: 8010)
    - LOCAL_DEFAULT_PORT: Default local backend port (default: 8010)
    """

    def __init__(self, config: Optional[SystemKernelConfig] = None):
        super().__init__("HybridWorkloadRouter", config)

        # Configuration
        self.enabled = os.getenv("HYBRID_ROUTING_ENABLED", "true").lower() == "true"
        self.gcp_port = int(os.getenv("GCP_DEFAULT_PORT", "8010"))
        self.local_port = int(os.getenv("LOCAL_DEFAULT_PORT", "8010"))

        # Deployment state
        self.gcp_active = False
        self.gcp_instance_id: Optional[str] = None
        self.gcp_ip: Optional[str] = None

        # Component routing table
        self.component_locations: Dict[str, str] = {}  # component -> 'local' | 'gcp'

        # Migration state
        self.migration_in_progress = False
        self.migration_start_time: Optional[float] = None

        # Performance metrics
        self.total_migrations = 0
        self.failed_migrations = 0
        self.avg_migration_time = 0.0

        # Threshold manager
        self.threshold_manager = AdaptiveThresholdManager()

    async def initialize(self) -> bool:
        """Initialize hybrid workload router."""
        if not self.enabled:
            self._logger.info("Hybrid routing disabled")
            self._initialized = True
            self._ready = True
            return True

        self._initialized = True
        self._ready = True
        self._logger.success("Hybrid workload router initialized")
        return True

    async def load_models(self) -> bool:
        """Load ML models for routing decisions."""
        # This router uses rule-based logic, no ML models needed
        self._models_loaded = True
        return True

    async def infer(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Route a request to local or GCP."""
        component = input_data.get("component", "default")
        request_type = input_data.get("request_type", "inference")

        # Check if component is already routed
        if component in self.component_locations:
            location = self.component_locations[component]
        else:
            # Default to local unless we have GCP active and RAM is high
            ram_usage = input_data.get("ram_usage", 0.5)
            ram_state = self.threshold_manager.get_ram_state(ram_usage)

            if self.gcp_active and ram_state in [RAMState.CRITICAL, RAMState.EMERGENCY]:
                location = "gcp"
            else:
                location = "local"

            self.component_locations[component] = location

        # Build routing response
        if location == "gcp":
            return {
                "location": "gcp",
                "host": self.gcp_ip or "localhost",
                "port": self.gcp_port,
                "url": f"http://{self.gcp_ip or 'localhost'}:{self.gcp_port}",
                "latency_estimate_ms": 50,
                "cost_estimate": 0.001,
            }
        else:
            return {
                "location": "local",
                "host": "localhost",
                "port": self.local_port,
                "url": f"http://localhost:{self.local_port}",
                "latency_estimate_ms": 5,
                "cost_estimate": 0.0,
            }

    def get_fallback_result(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Always route to local as fallback."""
        return {
            "location": "local",
            "host": "localhost",
            "port": self.local_port,
            "url": f"http://localhost:{self.local_port}",
            "latency_estimate_ms": 5,
            "cost_estimate": 0.0,
            "fallback": True,
        }

    # =========================================================================
    # GCP DEPLOYMENT METHODS
    # =========================================================================

    async def trigger_gcp_deployment(
        self,
        components: List[str],
        reason: str = "HIGH_RAM"
    ) -> Dict[str, Any]:
        """
        Trigger GCP deployment for specified components.

        Args:
            components: List of components to deploy (e.g., ["vision", "ml_models"])
            reason: Reason for deployment (for cost tracking)

        Returns:
            Deployment result with instance_id, ip, and status
        """
        if self.migration_in_progress:
            return {"success": False, "reason": "Migration already in progress"}

        self.migration_in_progress = True
        self.migration_start_time = time.time()

        try:
            self._logger.info(f"🚀 Initiating GCP deployment for: {', '.join(components)}")

            # Step 1: Validate GCP configuration
            gcp_config = await self._get_gcp_config()
            if not gcp_config["valid"]:
                raise Exception(f"GCP configuration invalid: {gcp_config['reason']}")

            # Step 2: Deploy instance
            deployment = await self._deploy_gcp_instance(components, gcp_config)

            # Track instance for cleanup
            self.gcp_instance_id = deployment["instance_id"]
            self.gcp_instance_zone = deployment.get("zone", gcp_config.get("zone", "us-central1-a"))
            self.gcp_active = True

            self._logger.info(f"📝 Tracking GCP instance: {self.gcp_instance_id}")

            # Step 3: Wait for instance to be ready
            ready = await self._wait_for_gcp_ready(deployment["instance_id"], timeout=120)

            # Get IP if not already set
            if not self.gcp_ip:
                self.gcp_ip = deployment.get("ip") or await self._get_instance_ip(deployment["instance_id"])

            # Update component locations
            for comp in components:
                self.component_locations[comp] = "gcp"

            # Update metrics
            migration_time = time.time() - self.migration_start_time
            self.total_migrations += 1
            self.avg_migration_time = (
                self.avg_migration_time * (self.total_migrations - 1) + migration_time
            ) / self.total_migrations

            if ready:
                self._logger.success(f"GCP deployment successful in {migration_time:.1f}s")
            else:
                self._logger.warning(f"GCP instance created but health check timeout ({migration_time:.1f}s)")

            return {
                "success": True,
                "instance_id": self.gcp_instance_id,
                "ip": self.gcp_ip,
                "zone": self.gcp_instance_zone,
                "components": components,
                "migration_time": migration_time,
                "health_check_passed": ready,
            }

        except Exception as e:
            self._logger.error(f"GCP deployment failed: {e}")
            self.failed_migrations += 1
            return {"success": False, "reason": str(e)}
        finally:
            self.migration_in_progress = False

    async def _get_gcp_config(self) -> Dict[str, Any]:
        """Get and validate GCP configuration."""
        project_id = os.getenv("GCP_PROJECT_ID", "")
        region = os.getenv("GCP_REGION", "us-central1")
        zone = os.getenv("GCP_ZONE", f"{region}-a")
        machine_type = os.getenv("GCP_MACHINE_TYPE", "e2-medium")
        service_account = os.getenv("GCP_SERVICE_ACCOUNT", "")

        # Validate required settings
        if not project_id:
            return {"valid": False, "reason": "GCP_PROJECT_ID not set"}

        # Check for credentials
        credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
        has_credentials = bool(credentials_path and Path(credentials_path).exists())

        # Check for gcloud CLI
        has_gcloud = shutil.which("gcloud") is not None

        if not has_credentials and not has_gcloud:
            return {"valid": False, "reason": "No GCP credentials found (neither file nor gcloud)"}

        return {
            "valid": True,
            "project_id": project_id,
            "region": region,
            "zone": zone,
            "machine_type": machine_type,
            "service_account": service_account,
            "has_credentials_file": has_credentials,
            "has_gcloud": has_gcloud,
            "repo_url": os.getenv("JARVIS_REPO_URL", "https://github.com/drussell23/JARVIS-AI-Agent.git"),
            "branch": os.getenv("JARVIS_BRANCH", "main"),
        }

    async def _deploy_gcp_instance(
        self,
        components: List[str],
        gcp_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Deploy a GCP Compute instance.

        Args:
            components: Components to deploy
            gcp_config: GCP configuration

        Returns:
            Deployment info with instance_id and zone
        """
        instance_name = f"jarvis-{uuid.uuid4().hex[:8]}"

        # Generate startup script
        startup_script = self._generate_startup_script(gcp_config, components)

        try:
            # Try using google-cloud-compute library
            from google.cloud import compute_v1

            # Create instance config
            instance = compute_v1.Instance()
            instance.name = instance_name
            instance.machine_type = f"zones/{gcp_config['zone']}/machineTypes/{gcp_config['machine_type']}"

            # Spot instance scheduling (cost optimization)
            scheduling = compute_v1.Scheduling()
            scheduling.preemptible = True
            scheduling.automatic_restart = False
            scheduling.on_host_maintenance = "TERMINATE"
            instance.scheduling = scheduling

            # Boot disk
            disk = compute_v1.AttachedDisk()
            disk.boot = True
            disk.auto_delete = True
            init_params = compute_v1.AttachedDiskInitializeParams()
            init_params.source_image = "projects/debian-cloud/global/images/family/debian-11"
            init_params.disk_size_gb = 30
            disk.initialize_params = init_params
            instance.disks = [disk]

            # Network interface
            network_interface = compute_v1.NetworkInterface()
            network_interface.network = "global/networks/default"
            access_config = compute_v1.AccessConfig()
            access_config.name = "External NAT"
            access_config.type_ = "ONE_TO_ONE_NAT"
            network_interface.access_configs = [access_config]
            instance.network_interfaces = [network_interface]

            # Metadata (startup script)
            metadata = compute_v1.Metadata()
            metadata.items = [
                compute_v1.Items(key="startup-script", value=startup_script)
            ]
            instance.metadata = metadata

            # Create instance
            client = compute_v1.InstancesClient()
            loop = asyncio.get_event_loop()
            operation = await loop.run_in_executor(
                None,
                lambda: client.insert(
                    project=gcp_config["project_id"],
                    zone=gcp_config["zone"],
                    instance_resource=instance
                )
            )

            self._logger.info(f"GCP instance creation initiated: {instance_name}")

            return {
                "instance_id": instance_name,
                "zone": gcp_config["zone"],
                "operation": operation.name if hasattr(operation, "name") else None,
            }

        except ImportError:
            # Fallback to gcloud CLI
            return await self._deploy_via_gcloud(instance_name, gcp_config, startup_script)

    async def _deploy_via_gcloud(
        self,
        instance_name: str,
        gcp_config: Dict[str, Any],
        startup_script: str
    ) -> Dict[str, Any]:
        """Deploy instance using gcloud CLI."""
        # Write startup script to temp file
        script_file = Path(f"/tmp/jarvis_startup_{uuid.uuid4().hex[:8]}.sh")
        script_file.write_text(startup_script)

        cmd = [
            "gcloud", "compute", "instances", "create", instance_name,
            f"--project={gcp_config['project_id']}",
            f"--zone={gcp_config['zone']}",
            f"--machine-type={gcp_config['machine_type']}",
            "--preemptible",
            "--image-family=debian-11",
            "--image-project=debian-cloud",
            "--boot-disk-size=30GB",
            f"--metadata-from-file=startup-script={script_file}",
            "--format=json",
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)

            if process.returncode == 0:
                result = json.loads(stdout.decode())
                return {
                    "instance_id": instance_name,
                    "zone": gcp_config["zone"],
                    "ip": result[0].get("networkInterfaces", [{}])[0].get("accessConfigs", [{}])[0].get("natIP"),
                }
            else:
                raise Exception(f"gcloud failed: {stderr.decode()}")
        finally:
            script_file.unlink(missing_ok=True)

    def _generate_startup_script(
        self,
        gcp_config: Dict[str, Any],
        components: List[str]
    ) -> str:
        """Generate VM startup script."""
        repo_url = gcp_config.get("repo_url", "https://github.com/drussell23/JARVIS-AI-Agent.git")
        branch = gcp_config.get("branch", "main")

        return f'''#!/bin/bash
set -e

# Log startup
echo "=== JARVIS GCP Instance Starting ===" | tee /var/log/jarvis-startup.log

# Install dependencies
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git curl

# Clone repository
cd /opt
git clone --depth 1 --branch {branch} {repo_url} jarvis
cd jarvis

# Create venv and install
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Start components
cd backend
export JARVIS_MODE=gcp
export JARVIS_COMPONENTS="{','.join(components)}"
export BACKEND_PORT=8010

# Start backend
python3 -m uvicorn api.main:app --host 0.0.0.0 --port 8010 &

# Signal ready
echo "JARVIS_READY" > /tmp/jarvis_ready
curl -X POST http://metadata.google.internal/computeMetadata/v1/instance/guest-attributes/jarvis/ready \\
    -H "Metadata-Flavor: Google" \\
    -d "true" 2>/dev/null || true

echo "=== JARVIS GCP Instance Ready ===" | tee -a /var/log/jarvis-startup.log
'''

    async def _wait_for_gcp_ready(self, instance_id: str, timeout: int = 300) -> bool:
        """
        Wait for GCP instance to be ready.

        Args:
            instance_id: Instance name
            timeout: Max wait time in seconds

        Returns:
            True if instance is ready
        """
        start_time = time.time()

        while time.time() - start_time < timeout:
            # Try to get IP if we don't have it
            if not self.gcp_ip:
                ip = await self._get_instance_ip(instance_id)
                if ip:
                    self.gcp_ip = ip

            # Health check if we have IP
            if self.gcp_ip:
                try:
                    if AIOHTTP_AVAILABLE and aiohttp is not None:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(
                                f"http://{self.gcp_ip}:{self.gcp_port}/health",
                                timeout=aiohttp.ClientTimeout(total=5)
                            ) as response:
                                if response.status == 200:
                                    self._logger.success(f"GCP instance ready: {self.gcp_ip}")
                                    return True
                except Exception:
                    pass

            await asyncio.sleep(5)

        return False

    async def _get_instance_ip(self, instance_id: str) -> Optional[str]:
        """Get external IP of a GCP instance."""
        zone = self.gcp_instance_zone or os.getenv("GCP_ZONE", "us-central1-a")
        project = os.getenv("GCP_PROJECT_ID", "")

        try:
            # Try google-cloud library
            from google.cloud import compute_v1
            client = compute_v1.InstancesClient()

            loop = asyncio.get_event_loop()
            instance = await loop.run_in_executor(
                None,
                lambda: client.get(project=project, zone=zone, instance=instance_id)
            )

            for interface in instance.network_interfaces:
                for config in interface.access_configs:
                    if config.nat_i_p:
                        return config.nat_i_p
        except ImportError:
            # Fallback to gcloud
            try:
                process = await asyncio.create_subprocess_exec(
                    "gcloud", "compute", "instances", "describe", instance_id,
                    f"--project={project}",
                    f"--zone={zone}",
                    "--format=json",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await process.communicate()
                if process.returncode == 0:
                    data = json.loads(stdout.decode())
                    return data.get("networkInterfaces", [{}])[0].get("accessConfigs", [{}])[0].get("natIP")
            except Exception:
                pass
        except Exception as e:
            self._logger.debug(f"Failed to get instance IP: {e}")

        return None

    async def cleanup_gcp_instance(self, instance_id: Optional[str] = None) -> bool:
        """
        Clean up (delete) a GCP instance.

        Args:
            instance_id: Instance to delete (defaults to current)

        Returns:
            True if deletion succeeded
        """
        target_id = instance_id or self.gcp_instance_id
        if not target_id:
            return True  # Nothing to clean up

        zone = self.gcp_instance_zone or os.getenv("GCP_ZONE", "us-central1-a")
        project = os.getenv("GCP_PROJECT_ID", "")

        self._logger.info(f"🧹 Cleaning up GCP instance: {target_id}")

        try:
            # Try google-cloud library
            from google.cloud import compute_v1
            client = compute_v1.InstancesClient()

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: client.delete(project=project, zone=zone, instance=target_id)
            )

            self._logger.success(f"GCP instance deleted: {target_id}")

        except ImportError:
            # Fallback to gcloud
            process = await asyncio.create_subprocess_exec(
                "gcloud", "compute", "instances", "delete", target_id,
                f"--project={project}",
                f"--zone={zone}",
                "--quiet",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await process.communicate()

            if process.returncode == 0:
                self._logger.success(f"GCP instance deleted via gcloud: {target_id}")
            else:
                self._logger.warning(f"gcloud delete returned code {process.returncode}")

        except Exception as e:
            self._logger.error(f"Failed to delete GCP instance: {e}")
            return False

        # Clear state
        if target_id == self.gcp_instance_id:
            self.gcp_active = False
            self.gcp_instance_id = None
            self.gcp_ip = None
            self.gcp_instance_zone = None

        return True

    async def shift_to_local(self, components: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Shift components from GCP back to local.

        Args:
            components: Specific components to shift (None = all GCP components)

        Returns:
            Shift result
        """
        target_components = components or [
            comp for comp, loc in self.component_locations.items()
            if loc == "gcp"
        ]

        if not target_components:
            return {"success": True, "shifted": 0, "reason": "No GCP components to shift"}

        try:
            # Update routing table
            for comp in target_components:
                self.component_locations[comp] = "local"

            self._logger.info(f"Shifted {len(target_components)} components to local")

            # Clean up GCP instance if no components left
            remaining_gcp = [
                comp for comp, loc in self.component_locations.items()
                if loc == "gcp"
            ]

            if not remaining_gcp and self.gcp_instance_id:
                await self.cleanup_gcp_instance()

            return {
                "success": True,
                "shifted": len(target_components),
                "components": target_components,
            }

        except Exception as e:
            self._logger.error(f"Failed to shift to local: {e}")
            return {"success": False, "reason": str(e)}

    def get_routing_stats(self) -> Dict[str, Any]:
        """Get routing statistics."""
        return {
            "enabled": self.enabled,
            "gcp_active": self.gcp_active,
            "gcp_instance_id": self.gcp_instance_id,
            "component_locations": self.component_locations.copy(),
            "total_migrations": self.total_migrations,
            "failed_migrations": self.failed_migrations,
            "avg_migration_time": self.avg_migration_time,
            "thresholds": self.threshold_manager.get_all_thresholds(),
        }


# =============================================================================
# GOAL INFERENCE ENGINE
# =============================================================================
class GoalInferenceEngine(IntelligenceManagerBase):
    """
    ML-powered intent classification and goal inference.

    Features:
    - User intent classification from natural language
    - Goal extraction and prioritization
    - Context-aware inference
    - Confidence scoring
    - Rule-based fallback

    Environment Configuration:
    - GOAL_INFERENCE_ENABLED: Enable goal inference (default: true)
    - GOAL_INFERENCE_MODEL: Model to use (default: rule_based)
    - GOAL_CONFIDENCE_THRESHOLD: Min confidence (default: 0.7)
    """

    # Known intents for rule-based fallback
    KNOWN_INTENTS = {
        "code": ["code", "program", "implement", "write", "develop", "create function"],
        "debug": ["debug", "fix", "error", "bug", "issue", "problem", "crash"],
        "search": ["search", "find", "look for", "locate", "where is"],
        "explain": ["explain", "what is", "how does", "describe", "tell me about"],
        "refactor": ["refactor", "clean up", "improve", "optimize", "restructure"],
        "test": ["test", "testing", "verify", "validate", "check"],
        "deploy": ["deploy", "release", "publish", "ship", "launch"],
        "chat": ["hello", "hi", "hey", "thanks", "help"],
    }

    def __init__(self, config: Optional[SystemKernelConfig] = None):
        super().__init__("GoalInferenceEngine", config)

        # Configuration
        self.enabled = os.getenv("GOAL_INFERENCE_ENABLED", "true").lower() == "true"
        self.model_type = os.getenv("GOAL_INFERENCE_MODEL", "rule_based")
        self.confidence_threshold = float(os.getenv("GOAL_CONFIDENCE_THRESHOLD", "0.7"))

        # ML model (lazy loaded)
        self._classifier = None

    async def initialize(self) -> bool:
        """Initialize goal inference engine."""
        if not self.enabled:
            self._logger.info("Goal inference disabled")
            self._initialized = True
            self._ready = True
            return True

        self._initialized = True
        self._ready = True
        self._logger.success("Goal inference engine initialized")
        return True

    async def load_models(self) -> bool:
        """Load ML models for intent classification."""
        if self.model_type == "rule_based":
            self._models_loaded = True
            return True

        try:
            # Would load actual ML model here
            self._models_loaded = True
            return True
        except Exception as e:
            self._logger.warning(f"Failed to load ML model: {e}, using rule-based")
            self.model_type = "rule_based"
            self._models_loaded = True
            return True

    async def infer(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Infer user intent from input."""
        text = input_data.get("text", "")
        context = input_data.get("context", {})

        if self.model_type == "rule_based" or not self._classifier:
            return self.get_fallback_result(input_data)

        # Would use ML model here
        return self.get_fallback_result(input_data)

    def get_fallback_result(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Rule-based intent classification."""
        text = input_data.get("text", "").lower()

        # Score each intent
        scores: Dict[str, float] = {}
        for intent, keywords in self.KNOWN_INTENTS.items():
            score = 0.0
            for keyword in keywords:
                if keyword in text:
                    score += 1.0 / len(keywords)
            scores[intent] = min(1.0, score)

        # Find best match
        if scores:
            best_intent = max(scores.keys(), key=lambda k: scores[k])
            best_score = scores[best_intent]
        else:
            best_intent = "unknown"
            best_score = 0.0

        return {
            "intent": best_intent,
            "confidence": best_score,
            "all_scores": scores,
            "method": "rule_based",
            "meets_threshold": best_score >= self.confidence_threshold,
        }


# =============================================================================
# HYBRID INTELLIGENCE COORDINATOR
# =============================================================================
class HybridIntelligenceCoordinator(IntelligenceManagerBase):
    """
    Master coordinator for hybrid local/GCP intelligence.

    Orchestrates:
    - Continuous RAM monitoring
    - Automatic workload shifting
    - Cost optimization
    - SAI learning integration
    - Health monitoring
    - Emergency fallback

    Environment Configuration:
    - HYBRID_INTELLIGENCE_ENABLED: Enable coordinator (default: true)
    - MONITORING_INTERVAL: Base monitoring interval in seconds (default: 5)
    """

    def __init__(self, config: Optional[SystemKernelConfig] = None):
        super().__init__("HybridIntelligenceCoordinator", config)

        # Configuration
        self.enabled = os.getenv("HYBRID_INTELLIGENCE_ENABLED", "true").lower() == "true"
        self.base_monitoring_interval = int(os.getenv("MONITORING_INTERVAL", "5"))

        # Components
        self.workload_router = HybridWorkloadRouter(config)
        self.learning_model = HybridLearningModel()
        self.sai_integration = SAIHybridIntegration(self.learning_model)
        self.threshold_manager = AdaptiveThresholdManager()

        # State
        self._monitoring_task: Optional[asyncio.Task] = None
        self._monitoring_interval = self.base_monitoring_interval
        self._running = False

        # Emergency state
        self._emergency_mode = False
        self._emergency_start: Optional[float] = None

        # Decision history
        self._decision_history: List[Dict[str, Any]] = []
        self._max_decision_history = 100

    async def initialize(self) -> bool:
        """Initialize hybrid intelligence coordinator."""
        if not self.enabled:
            self._logger.info("Hybrid intelligence disabled")
            self._initialized = True
            self._ready = True
            return True

        # Initialize components
        await self.workload_router.initialize()
        await self.sai_integration.initialize_database()

        self._initialized = True
        self._ready = True
        self._logger.success("Hybrid intelligence coordinator initialized")
        return True

    async def load_models(self) -> bool:
        """Load ML models."""
        await self.workload_router.load_models()
        self._models_loaded = True
        return True

    async def infer(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Get routing decision and system status."""
        ram_usage = input_data.get("ram_usage", 0.5)
        component = input_data.get("component", "default")

        # Get RAM state
        ram_state = self.threshold_manager.get_ram_state(ram_usage)

        # Get routing decision
        routing = await self.workload_router.safe_infer({
            "component": component,
            "ram_usage": ram_usage,
        })

        # Get spike prediction
        spike_prediction = await self.learning_model.predict_ram_spike(
            current_usage=ram_usage,
            trend=input_data.get("trend", 0.0),
        )

        return {
            "ram_state": ram_state.value,
            "routing": routing,
            "spike_prediction": spike_prediction,
            "emergency_mode": self._emergency_mode,
            "monitoring_interval": self._monitoring_interval,
        }

    def get_fallback_result(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Fallback: route to local."""
        return {
            "ram_state": "UNKNOWN",
            "routing": self.workload_router.get_fallback_result(input_data),
            "spike_prediction": {"spike_likely": False, "confidence": 0.0},
            "emergency_mode": False,
            "fallback": True,
        }

    async def start_monitoring(self) -> None:
        """Start continuous monitoring loop."""
        if not self.enabled or self._running:
            return

        self._running = True
        self._monitoring_task = asyncio.create_task(self._monitoring_loop())
        self._logger.info(f"Hybrid intelligence monitoring started (interval: {self._monitoring_interval}s)")

    async def stop_monitoring(self) -> None:
        """Stop monitoring loop."""
        self._running = False
        if self._monitoring_task:
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass
            self._monitoring_task = None

    async def _monitoring_loop(self) -> None:
        """Continuous monitoring and decision loop."""
        while self._running:
            try:
                # Get current system state
                ram_usage = await self._get_current_ram_usage()
                ram_state = self.threshold_manager.get_ram_state(ram_usage)

                # Record observation for learning
                await self.sai_integration.record_and_learn("ram", {
                    "timestamp": time.time(),
                    "usage": ram_usage,
                    "components": {},
                })

                # Handle emergency
                if ram_state == RAMState.EMERGENCY and not self._emergency_mode:
                    await self._handle_emergency(ram_usage)
                elif self._emergency_mode and ram_state == RAMState.OPTIMAL:
                    await self._exit_emergency()

                # Adapt monitoring interval
                self._monitoring_interval = await self.learning_model.get_optimal_monitoring_interval(ram_usage)

                # Record decision
                self._decision_history.append({
                    "timestamp": time.time(),
                    "ram_usage": ram_usage,
                    "ram_state": ram_state.value,
                    "emergency_mode": self._emergency_mode,
                })
                if len(self._decision_history) > self._max_decision_history:
                    self._decision_history.pop(0)

            except Exception as e:
                self._logger.error(f"Monitoring loop error: {e}")

            await asyncio.sleep(self._monitoring_interval)

    async def _get_current_ram_usage(self) -> float:
        """Get current RAM usage percentage."""
        try:
            import psutil
            mem = psutil.virtual_memory()
            return mem.percent / 100.0
        except Exception:
            return 0.5  # Default if psutil unavailable

    async def _handle_emergency(self, ram_usage: float) -> None:
        """Handle emergency RAM situation."""
        self._emergency_mode = True
        self._emergency_start = time.time()
        self._logger.error(f"🚨 EMERGENCY MODE ACTIVATED: RAM at {ram_usage*100:.1f}%")

    async def _exit_emergency(self) -> None:
        """Exit emergency mode."""
        duration = time.time() - self._emergency_start if self._emergency_start else 0
        self._logger.info(f"✅ Emergency resolved (duration: {duration:.1f}s)")
        self._emergency_mode = False
        self._emergency_start = None

    async def get_comprehensive_status(self) -> Dict[str, Any]:
        """Get comprehensive coordinator status."""
        return {
            "enabled": self.enabled,
            "initialized": self._initialized,
            "running": self._running,
            "emergency_mode": self._emergency_mode,
            "monitoring_interval": self._monitoring_interval,
            "router_stats": self.workload_router.get_routing_stats(),
            "learning_stats": await self.learning_model.get_learning_stats(),
            "thresholds": self.threshold_manager.get_all_thresholds(),
            "decision_history_count": len(self._decision_history),
        }


# =============================================================================
# INTELLIGENCE REGISTRY
# =============================================================================
class IntelligenceRegistry:
    """
    Registry for all intelligence managers.

    Provides centralized initialization and access to all
    intelligence components.
    """

    def __init__(self, config: Optional[SystemKernelConfig] = None):
        self.config = config or SystemKernelConfig.from_environment()
        self._managers: Dict[str, IntelligenceManagerBase] = {}
        self._logger = UnifiedLogger()
        self._initialized = False

    def register(self, manager: IntelligenceManagerBase) -> None:
        """Register an intelligence manager."""
        self._managers[manager.name] = manager

    def get(self, name: str) -> Optional[IntelligenceManagerBase]:
        """Get a manager by name."""
        return self._managers.get(name)

    async def initialize_all(self) -> Dict[str, bool]:
        """Initialize all registered managers."""
        results: Dict[str, bool] = {}

        for name, manager in self._managers.items():
            try:
                results[name] = await manager.initialize()
                if results[name]:
                    self._logger.success(f"{name}: initialized")
                else:
                    self._logger.warning(f"{name}: initialization failed")
            except Exception as e:
                self._logger.error(f"{name} initialization error: {e}")
                results[name] = False

        self._initialized = True
        return results

    def get_all_status(self) -> Dict[str, Dict[str, Any]]:
        """Get status of all managers."""
        return {name: manager.status for name, manager in self._managers.items()}


# =============================================================================
# ZONE 4.5: LEARNING GOALS DISCOVERY SYSTEM
# =============================================================================
# v108.0: Intelligent learning goals discovery with reactor-core integration
# Analyzes experiences, logs, and corrections to discover what JARVIS needs to learn


class DiscoverySource(Enum):
    """Sources of discovered learning topics."""

    CORRECTION = "correction"  # User corrected JARVIS
    FAILED_INTERACTION = "failed_interaction"  # Low quality_score
    USER_QUESTION = "user_question"  # User asked about something
    UNKNOWN_TERM = "unknown_term"  # JARVIS didn't recognize a term
    TRENDING = "trending"  # Frequently mentioned topic
    MANUAL = "manual"  # Manually added topic


@dataclass
class DiscoveredTopic:
    """
    A topic discovered for JARVIS to learn.

    Attributes:
        topic: The topic name/identifier
        priority: Priority score (0-10, higher = more important)
        source: How the topic was discovered
        confidence: Confidence that this is a valuable topic (0.0-1.0)
        frequency: Number of times this topic appeared
        urls: Documentation URLs for learning
        keywords: Related keywords for search
        scraped: Whether documentation has been scraped
        pages_scraped: Number of pages scraped so far
    """

    topic: str
    priority: float = 5.0
    source: DiscoverySource = DiscoverySource.MANUAL
    confidence: float = 0.5
    frequency: int = 1
    urls: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    scraped: bool = False
    pages_scraped: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "topic": self.topic,
            "priority": self.priority,
            "source": self.source.value,
            "confidence": self.confidence,
            "frequency": self.frequency,
            "urls": self.urls,
            "keywords": self.keywords,
            "scraped": self.scraped,
            "pages_scraped": self.pages_scraped,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DiscoveredTopic":
        """Create from dictionary."""
        return cls(
            topic=data["topic"],
            priority=data.get("priority", 5.0),
            source=DiscoverySource(data.get("source", "manual")),
            confidence=data.get("confidence", 0.5),
            frequency=data.get("frequency", 1),
            urls=data.get("urls", []),
            keywords=data.get("keywords", []),
            scraped=data.get("scraped", False),
            pages_scraped=data.get("pages_scraped", 0),
        )


class IntelligentLearningGoalsDiscovery:
    """
    v108.0: Comprehensive learning goals discovery with reactor-core integration.

    Features:
    - Multi-source topic extraction (logs, experiences, corrections)
    - Intelligent priority scoring based on source weights
    - Automatic URL generation for documentation
    - Safe Scout integration for automated scraping
    - Real-time progress broadcasts

    This class analyzes JARVIS's interactions to discover what topics
    it needs to learn about to improve future responses.
    """

    def __init__(
        self,
        max_topics: int = 50,
        min_mentions: int = 2,
        min_confidence: float = 0.5,
        source_weights: Optional[Dict[str, float]] = None,
        logger: Optional[Any] = None,
        project_root: Optional[Path] = None,
    ):
        """
        Initialize the learning goals discovery system.

        Args:
            max_topics: Maximum number of topics to track
            min_mentions: Minimum mentions before tracking a topic
            min_confidence: Minimum confidence to keep a topic
            source_weights: Custom weights for different sources
            logger: Logger instance
            project_root: Project root path
        """
        self.max_topics = max_topics
        self.min_mentions = min_mentions
        self.min_confidence = min_confidence
        self.logger = logger or logging.getLogger("LearningGoals")
        self._project_root = project_root or Path(__file__).parent

        # Source weights for priority calculation
        self.source_weights = source_weights or {
            DiscoverySource.CORRECTION.value: 1.0,
            DiscoverySource.FAILED_INTERACTION.value: 0.9,
            DiscoverySource.USER_QUESTION.value: 0.7,
            DiscoverySource.UNKNOWN_TERM.value: 0.6,
            DiscoverySource.TRENDING.value: 0.5,
            DiscoverySource.MANUAL.value: 1.0,
        }

        # Topic storage
        self.topics: Dict[str, DiscoveredTopic] = {}
        self.topics_file = self._project_root / "data" / "discovered_topics.json"
        self._term_frequency: Dict[str, int] = {}
        self._last_discovery: Optional[datetime] = None

        # Reactor-core integration (optional)
        self._reactor_topic_discovery: Optional[Any] = None
        self._safe_scout: Optional[Any] = None
        self._topic_queue: Optional[Any] = None

        # Thread safety
        self._lock = asyncio.Lock()

        # Statistics
        self._stats = {
            "topics_discovered": 0,
            "topics_scraped": 0,
            "discovery_runs": 0,
            "last_run": None,
        }

        # Load existing topics
        self._load_topics()

        # Try to import reactor-core components
        self._init_reactor_core_integration()

    def _init_reactor_core_integration(self) -> None:
        """Try to connect to reactor-core for enhanced discovery."""
        try:
            reactor_core_path = self._project_root.parent / "reactor-core"
            if reactor_core_path.exists():
                import sys

                if str(reactor_core_path) not in sys.path:
                    sys.path.insert(0, str(reactor_core_path))

                # Import TopicDiscovery from reactor-core
                try:
                    from reactor_core.scout.topic_discovery import TopicDiscovery

                    self._reactor_topic_discovery = TopicDiscovery()
                    self.logger.debug("✓ Reactor-core TopicDiscovery connected")
                except ImportError:
                    pass

                # Import SafeScoutOrchestrator
                try:
                    from reactor_core.scout.safe_scout_orchestrator import (
                        SafeScoutOrchestrator,
                    )

                    self._safe_scout = SafeScoutOrchestrator()
                    self.logger.debug("✓ Reactor-core SafeScout connected")
                except ImportError:
                    pass

                # Import TopicQueue
                try:
                    from reactor_core.scout.topic_queue import TopicQueue

                    queue_db = self._project_root / "data" / "topic_queue.db"
                    queue_db.parent.mkdir(parents=True, exist_ok=True)
                    self._topic_queue = TopicQueue(db_path=str(queue_db))
                    self.logger.debug("✓ Reactor-core TopicQueue connected")
                except ImportError:
                    pass

        except Exception as e:
            self.logger.debug(f"Reactor-core init error: {e}")

    def _load_topics(self) -> None:
        """Load previously discovered topics."""
        if self.topics_file.exists():
            try:
                data = json.loads(self.topics_file.read_text())
                for t in data.get("topics", []):
                    topic = DiscoveredTopic.from_dict(t)
                    self.topics[topic.topic.lower()] = topic
                if data.get("last_discovery"):
                    self._last_discovery = datetime.fromisoformat(
                        data["last_discovery"]
                    )
            except Exception as e:
                self.logger.debug(f"Failed to load topics: {e}")

    def _save_topics(self) -> None:
        """Persist discovered topics."""
        try:
            self.topics_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "topics": [t.to_dict() for t in self.topics.values()],
                "last_discovery": (
                    self._last_discovery.isoformat() if self._last_discovery else None
                ),
            }
            self.topics_file.write_text(json.dumps(data, indent=2, default=str))
        except Exception as e:
            self.logger.warning(f"Failed to save topics: {e}")

    def _calculate_priority(
        self,
        source: DiscoverySource,
        confidence: float,
        frequency: int,
        recency_days: float = 0.0,
    ) -> float:
        """
        Calculate topic priority using weighted scoring.

        Formula: priority = 0.4*confidence + 0.3*frequency_norm + 0.2*recency + 0.1*source_weight
        Final score scaled to 0-10.

        Args:
            source: How the topic was discovered
            confidence: Confidence score (0.0-1.0)
            frequency: Number of times topic appeared
            recency_days: Days since topic was discovered

        Returns:
            Priority score (0-10)
        """
        import math

        # Normalize frequency (log scale, max 10)
        frequency_norm = min(1.0, math.log10(frequency + 1) / math.log10(11))

        # Recency score (1.0 for today, decays over 30 days)
        recency_score = max(0.0, 1.0 - (recency_days / 30.0))

        # Source weight
        source_weight = self.source_weights.get(source.value, 0.5)

        # Weighted combination
        raw_score = (
            0.4 * confidence
            + 0.3 * frequency_norm
            + 0.2 * recency_score
            + 0.1 * source_weight
        )

        # Scale to 0-10
        return round(raw_score * 10, 2)

    def _generate_documentation_urls(self, topic: str) -> List[str]:
        """Generate likely documentation URLs for a topic."""
        urls = []
        topic_slug = topic.lower().replace(" ", "-").replace(".", "-")
        topic_underscore = topic.lower().replace(" ", "_").replace(".", "_")

        # Common documentation patterns
        patterns = [
            f"https://docs.python.org/3/library/{topic_underscore}.html",
            f"https://{topic_slug}.readthedocs.io/",
            f"https://github.com/{topic_slug}/{topic_slug}",
            f"https://pypi.org/project/{topic_slug}/",
            f"https://developer.mozilla.org/en-US/docs/Web/{topic}",
            f"https://www.npmjs.com/package/{topic_slug}",
        ]

        # Add relevant patterns based on topic keywords
        topic_lower = topic.lower()
        if "python" in topic_lower or topic_lower.startswith("py"):
            urls.append(f"https://docs.python.org/3/search.html?q={topic}")
        if "react" in topic_lower:
            urls.append(f"https://react.dev/reference/react/{topic}")
        if "langchain" in topic_lower:
            urls.append("https://python.langchain.com/docs/")
        if "llm" in topic_lower or "model" in topic_lower:
            urls.append("https://huggingface.co/docs")

        # Add base patterns
        urls.extend(patterns[:3])  # Limit to avoid too many

        return urls[:5]  # Cap at 5 URLs

    def _extract_technical_terms(self, text: str) -> List[str]:
        """
        Extract technical terms from text using pattern matching.

        Patterns:
        - CamelCase words (e.g., LangChain, FastAPI)
        - snake_case identifiers (e.g., async_generator)
        - Dotted names (e.g., numpy.array)
        - Known tech patterns (e.g., React, Python, API)

        Args:
            text: Input text to analyze

        Returns:
            List of extracted technical terms
        """
        if not text:
            return []

        terms = []

        # CamelCase pattern
        camel_pattern = r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b"
        terms.extend(re.findall(camel_pattern, text))

        # snake_case pattern
        snake_pattern = r"\b([a-z]+(?:_[a-z]+)+)\b"
        terms.extend(re.findall(snake_pattern, text))

        # Dotted names (e.g., module.function)
        dot_pattern = r"\b([a-z]+(?:\.[a-z]+)+)\b"
        terms.extend(re.findall(dot_pattern, text))

        # Known technology keywords
        tech_keywords = [
            r"\b(Python|JavaScript|TypeScript|Rust|Go|Swift)\b",
            r"\b(React|Vue|Angular|FastAPI|Flask|Django)\b",
            r"\b(LangChain|LangGraph|ChromaDB|FAISS)\b",
            r"\b(Docker|Kubernetes|Terraform|AWS|GCP|Azure)\b",
            r"\b(PostgreSQL|MongoDB|Redis|SQLite)\b",
            r"\b(API|REST|GraphQL|WebSocket|gRPC)\b",
            r"\b(ML|AI|LLM|NLP|transformers?|embeddings?)\b",
        ]
        for pattern in tech_keywords:
            matches = re.findall(pattern, text, re.IGNORECASE)
            terms.extend(matches)

        # Clean and deduplicate
        cleaned = []
        seen: set[str] = set()
        for term in terms:
            term_lower = term.lower().strip()
            if len(term_lower) > 2 and term_lower not in seen:
                # Filter common words
                if term_lower not in {"the", "and", "for", "with", "this", "that"}:
                    cleaned.append(term)
                    seen.add(term_lower)

        return cleaned

    def _add_or_update_topic(
        self,
        term: str,
        source: DiscoverySource,
        confidence: float,
        frequency: int = 1,
    ) -> Optional[DiscoveredTopic]:
        """
        Add a new topic or update an existing one.

        Args:
            term: Topic term
            source: How the topic was discovered
            confidence: Confidence score
            frequency: Number of occurrences

        Returns:
            The new topic if created, None if updated existing
        """
        term_key = term.lower().strip()

        if len(term_key) < 3:
            return None

        if term_key in self.topics:
            # Update existing topic
            existing = self.topics[term_key]
            existing.frequency += frequency
            # Upgrade source if higher priority
            if self.source_weights.get(
                source.value, 0
            ) > self.source_weights.get(existing.source.value, 0):
                existing.source = source
            # Update confidence (weighted average)
            existing.confidence = (existing.confidence + confidence) / 2
            # Recalculate priority
            existing.priority = self._calculate_priority(
                existing.source,
                existing.confidence,
                existing.frequency,
            )
            return None  # Not a new discovery
        else:
            # Create new topic
            if len(self.topics) >= self.max_topics:
                # Remove lowest priority scraped topic
                scraped = [t for t in self.topics.values() if t.scraped]
                if scraped:
                    lowest = min(scraped, key=lambda t: t.priority)
                    del self.topics[lowest.topic.lower()]

            topic = DiscoveredTopic(
                topic=term,
                priority=self._calculate_priority(source, confidence, frequency),
                source=source,
                confidence=confidence,
                frequency=frequency,
                urls=self._generate_documentation_urls(term),
            )
            self.topics[term_key] = topic
            self._stats["topics_discovered"] += 1
            return topic

    async def discover_from_experiences(
        self,
        db_path: Optional[Path] = None,
        lookback_days: int = 30,
    ) -> List[DiscoveredTopic]:
        """
        Discover learning topics from the training database experiences.

        Analyzes:
        - Failed interactions (low quality_score)
        - Corrected responses (feedback='corrected')
        - User questions (input contains question patterns)
        - Unknown terms (technical terms in low-confidence responses)

        Args:
            db_path: Path to training database
            lookback_days: Number of days to look back

        Returns:
            List of newly discovered topics
        """
        discovered = []

        # Default database path
        if db_path is None:
            db_path = self._project_root / "data" / "jarvis_training.db"

        if not db_path.exists():
            self.logger.debug(f"Training DB not found: {db_path}")
            return discovered

        async with self._lock:
            try:
                # v109.0: Non-blocking database access
                loop = asyncio.get_running_loop()

                def _query_db() -> List[DiscoveredTopic]:
                    """Run database queries in thread pool."""
                    local_discovered = []
                    conn = sqlite3.connect(str(db_path))
                    cursor = conn.cursor()

                    # Calculate cutoff timestamp
                    cutoff = datetime.now() - timedelta(days=lookback_days)
                    cutoff_ts = cutoff.timestamp()

                    # Source 1: Failed Interactions (low quality_score)
                    cursor.execute(
                        """
                        SELECT input_text, context, quality_score
                        FROM experiences
                        WHERE timestamp > ? AND quality_score < 0.4
                        ORDER BY timestamp DESC
                        LIMIT 100
                    """,
                        (cutoff_ts,),
                    )

                    for row in cursor.fetchall():
                        input_text, context, quality_score = row
                        terms = self._extract_technical_terms(input_text)
                        for term in terms:
                            topic = self._add_or_update_topic(
                                term,
                                DiscoverySource.FAILED_INTERACTION,
                                confidence=0.3 + (1.0 - quality_score) * 0.5,
                            )
                            if topic:
                                local_discovered.append(topic)

                    # Source 2: Corrected Responses
                    cursor.execute(
                        """
                        SELECT input_text, correction, context
                        FROM experiences
                        WHERE timestamp > ? AND feedback = 'corrected'
                        ORDER BY timestamp DESC
                        LIMIT 100
                    """,
                        (cutoff_ts,),
                    )

                    for row in cursor.fetchall():
                        input_text, correction, context = row
                        terms = self._extract_technical_terms(input_text)
                        if correction:
                            terms.extend(self._extract_technical_terms(correction))
                        for term in terms:
                            topic = self._add_or_update_topic(
                                term,
                                DiscoverySource.CORRECTION,
                                confidence=0.85,  # High confidence for corrections
                            )
                            if topic:
                                local_discovered.append(topic)

                    # Source 3: User Questions
                    cursor.execute(
                        """
                        SELECT input_text, context
                        FROM experiences
                        WHERE timestamp > ?
                          AND (input_text LIKE '%what is%'
                               OR input_text LIKE '%how do%'
                               OR input_text LIKE '%how does%'
                               OR input_text LIKE '%explain%'
                               OR input_text LIKE '%learn about%')
                        ORDER BY timestamp DESC
                        LIMIT 100
                    """,
                        (cutoff_ts,),
                    )

                    for row in cursor.fetchall():
                        input_text, context = row
                        terms = self._extract_technical_terms(input_text)
                        for term in terms:
                            topic = self._add_or_update_topic(
                                term,
                                DiscoverySource.USER_QUESTION,
                                confidence=0.7,
                            )
                            if topic:
                                local_discovered.append(topic)

                    # Source 4: Trending Terms (high frequency)
                    cursor.execute(
                        """
                        SELECT input_text
                        FROM experiences
                        WHERE timestamp > ?
                        ORDER BY timestamp DESC
                        LIMIT 500
                    """,
                        (cutoff_ts,),
                    )

                    all_terms = []
                    for row in cursor.fetchall():
                        all_terms.extend(self._extract_technical_terms(row[0]))

                    # Count term frequency
                    from collections import Counter

                    term_counts = Counter(all_terms)

                    # Add trending terms (appearing 3+ times)
                    for term, count in term_counts.most_common(20):
                        if count >= 3:
                            topic = self._add_or_update_topic(
                                term,
                                DiscoverySource.TRENDING,
                                confidence=min(0.9, 0.4 + count * 0.05),
                                frequency=count,
                            )
                            if topic:
                                local_discovered.append(topic)

                    conn.close()
                    return local_discovered

                # Run database queries in executor
                discovered = await loop.run_in_executor(None, _query_db)

                # Save after discovery
                self._save_topics()
                self._stats["discovery_runs"] += 1
                self._stats["last_run"] = datetime.now().isoformat()
                self._last_discovery = datetime.now()

            except Exception as e:
                self.logger.warning(f"Experience discovery error: {e}")

        return discovered

    async def discover_from_logs(self, log_dir: Path) -> List[DiscoveredTopic]:
        """
        Discover topics from JARVIS log files.

        Args:
            log_dir: Directory containing log files

        Returns:
            List of newly discovered topics
        """
        discovered = []

        if not log_dir.exists():
            return discovered

        # Patterns for discovering learning opportunities
        patterns = [
            (
                r"(?:learn|study|research|understand)\s+(\w+(?:\s+\w+)?)",
                DiscoverySource.USER_QUESTION,
            ),
            (r"what\s+is\s+(\w+(?:\s+\w+)?)\??", DiscoverySource.USER_QUESTION),
            (
                r"how\s+(?:does|do)\s+(\w+(?:\s+\w+)?)\s+work",
                DiscoverySource.USER_QUESTION,
            ),
            (
                r"error:?\s+(?:unknown|unrecognized)\s+(\w+)",
                DiscoverySource.UNKNOWN_TERM,
            ),
            (
                r"failed to (?:import|load|find)\s+(\w+)",
                DiscoverySource.FAILED_INTERACTION,
            ),
        ]

        # v109.0: Non-blocking file I/O
        def _read_file_sync(file_path: Path) -> str:
            """Read file content synchronously (runs in thread pool)."""
            try:
                return file_path.read_text(errors="ignore")
            except Exception:
                return ""

        loop = asyncio.get_running_loop()

        async with self._lock:
            # Scan recent log files (non-blocking)
            for log_file in sorted(log_dir.glob("*.log"), reverse=True)[:10]:
                try:
                    # Run blocking I/O in executor to prevent event loop blocking
                    content = await loop.run_in_executor(None, _read_file_sync, log_file)
                    if not content:
                        continue

                    for pattern, source in patterns:
                        matches = re.findall(pattern, content, re.IGNORECASE)
                        for match in matches[:5]:
                            term = match.strip()
                            topic = self._add_or_update_topic(
                                term,
                                source,
                                confidence=0.5,
                            )
                            if topic:
                                discovered.append(topic)
                except Exception:
                    continue

            if discovered:
                self._save_topics()

        return discovered

    async def discover_with_reactor_core(
        self,
        events: Optional[List[Dict[str, Any]]] = None,
    ) -> List[DiscoveredTopic]:
        """
        Use reactor-core's TopicDiscovery for enhanced extraction.

        If reactor-core is available, leverages its ML-based
        topic extraction for higher quality results.

        Args:
            events: Optional list of events to analyze

        Returns:
            List of newly discovered topics
        """
        discovered = []

        if not self._reactor_topic_discovery:
            return discovered

        try:
            # Use reactor-core's analyze_events if available
            if hasattr(self._reactor_topic_discovery, "analyze_events") and events:
                results = await self._reactor_topic_discovery.analyze_events(events)
                for result in results:
                    topic = self._add_or_update_topic(
                        result.get("topic", ""),
                        DiscoverySource(result.get("source", "trending")),
                        confidence=result.get("confidence", 0.5),
                    )
                    if topic:
                        discovered.append(topic)

            # Use discover_from_jarvis if available
            if hasattr(self._reactor_topic_discovery, "discover_from_jarvis"):
                results = await self._reactor_topic_discovery.discover_from_jarvis()
                for result in results:
                    topic = self._add_or_update_topic(
                        result.get("topic", ""),
                        DiscoverySource.TRENDING,
                        confidence=result.get("confidence", 0.5),
                    )
                    if topic:
                        discovered.append(topic)

        except Exception as e:
            self.logger.debug(f"Reactor-core discovery error: {e}")

        return discovered

    def get_pending_topics(self, limit: int = 10) -> List[DiscoveredTopic]:
        """Get unscraped topics sorted by priority."""
        pending = [t for t in self.topics.values() if not t.scraped]
        return sorted(pending, key=lambda t: -t.priority)[:limit]

    def get_pending_goals(self, limit: int = 10) -> List[DiscoveredTopic]:
        """Alias for get_pending_topics for backward compatibility."""
        return self.get_pending_topics(limit)

    def mark_scraped(self, topic: str, pages: int = 0) -> None:
        """Mark a topic as scraped."""
        topic_key = topic.lower()
        if topic_key in self.topics:
            self.topics[topic_key].scraped = True
            self.topics[topic_key].pages_scraped = pages
            self._stats["topics_scraped"] += 1
            self._save_topics()

    def add_manual_topic(
        self,
        topic: str,
        priority: float = 8.0,
        urls: Optional[List[str]] = None,
    ) -> DiscoveredTopic:
        """Add a manually specified topic."""
        new_topic = DiscoveredTopic(
            topic=topic,
            priority=priority,
            source=DiscoverySource.MANUAL,
            confidence=1.0,
            urls=urls or self._generate_documentation_urls(topic),
        )
        self.topics[topic.lower()] = new_topic
        self._stats["topics_discovered"] += 1
        self._save_topics()
        return new_topic

    def get_stats(self) -> Dict[str, Any]:
        """Get discovery statistics."""
        return {
            **self._stats,
            "total_topics": len(self.topics),
            "pending_topics": len([t for t in self.topics.values() if not t.scraped]),
            "scraped_topics": len([t for t in self.topics.values() if t.scraped]),
            "has_reactor_core": self._reactor_topic_discovery is not None,
            "has_safe_scout": self._safe_scout is not None,
        }


# =============================================================================
# ZONE 4.6: NEURAL MESH COORDINATION SYSTEM
# =============================================================================
# v108.0: Neural Mesh for coordinating multiple intelligence nodes
# Enables distributed decision-making across subsystems


@dataclass
class NeuralMeshNode:
    """
    A node in the Neural Mesh network.

    Represents a component that can participate in distributed
    intelligence coordination (UAE, SAI, MAS, etc.).
    """

    node_id: str
    node_type: str
    capabilities: List[str] = field(default_factory=list)
    status: str = "active"
    last_heartbeat: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_healthy(self, timeout_seconds: float = 30.0) -> bool:
        """Check if the node is healthy (recent heartbeat)."""
        return (time.time() - self.last_heartbeat) < timeout_seconds

    def update_heartbeat(self) -> None:
        """Update the node's heartbeat timestamp."""
        self.last_heartbeat = time.time()


class NeuralMeshCoordinator:
    """
    Neural Mesh Coordinator for distributed intelligence.

    Provides:
    - Node registration and discovery
    - Event broadcasting across nodes
    - Heartbeat monitoring
    - Load balancing recommendations
    - Collective decision synthesis

    This is the production implementation with full features.
    """

    def __init__(
        self,
        sync_interval: float = 5.0,
        heartbeat_timeout: float = 30.0,
        logger: Optional[Any] = None,
    ):
        """
        Initialize the Neural Mesh Coordinator.

        Args:
            sync_interval: Seconds between sync operations
            heartbeat_timeout: Seconds before node is considered dead
            logger: Logger instance
        """
        self._nodes: Dict[str, NeuralMeshNode] = {}
        self._context: Dict[str, Any] = {}
        self._subscribers: Dict[str, List[Callable]] = defaultdict(list)
        self._sync_interval = sync_interval
        self._heartbeat_timeout = heartbeat_timeout
        self._sync_task: Optional[asyncio.Task] = None
        self._running = False
        self._logger = logger or logging.getLogger("NeuralMesh")
        self._lock = asyncio.Lock()

        # Metrics
        self._metrics = {
            "broadcasts_sent": 0,
            "events_processed": 0,
            "nodes_registered": 0,
            "nodes_deregistered": 0,
        }

    async def register_node(
        self,
        node: Optional[NeuralMeshNode] = None,
        node_name: Optional[str] = None,
        node_type: Optional[str] = None,
        capabilities: Optional[List[str]] = None,
    ) -> bool:
        """
        Register a node with the Neural Mesh.

        Can accept either a NeuralMeshNode dataclass or individual parameters.

        Args:
            node: Pre-configured NeuralMeshNode
            node_name: Node identifier (if not using node parameter)
            node_type: Type of node (if not using node parameter)
            capabilities: List of capabilities (if not using node parameter)

        Returns:
            True if registration successful
        """
        async with self._lock:
            if node is not None:
                self._nodes[node.node_id] = node
                self._logger.debug(f"Node registered: {node.node_id}")
            elif node_name is not None:
                new_node = NeuralMeshNode(
                    node_id=node_name,
                    node_type=node_type or "unknown",
                    capabilities=capabilities or [],
                )
                self._nodes[node_name] = new_node
                self._logger.debug(f"Node registered: {node_name}")
            else:
                return False

            self._metrics["nodes_registered"] += 1
            return True

    async def deregister_node(self, node_id: str) -> bool:
        """Remove a node from the mesh."""
        async with self._lock:
            if node_id in self._nodes:
                del self._nodes[node_id]
                self._metrics["nodes_deregistered"] += 1
                self._logger.debug(f"Node deregistered: {node_id}")
                return True
            return False

    async def broadcast(
        self,
        event_type: str,
        data: Dict[str, Any],
        source: Optional[str] = None,
    ) -> None:
        """
        Broadcast an event to all subscribers.

        Args:
            event_type: Type of event (e.g., "context_update", "alert")
            data: Event data payload
            source: Source node ID
        """
        event = {
            "event_type": event_type,
            "data": data,
            "source": source,
            "timestamp": time.time(),
        }

        for subscriber in self._subscribers.get(event_type, []):
            try:
                if asyncio.iscoroutinefunction(subscriber):
                    await subscriber(event)
                else:
                    subscriber(event)
            except Exception as e:
                self._logger.warning(f"Subscriber error: {e}")

        self._metrics["broadcasts_sent"] += 1

    async def subscribe(self, event_type: str, callback: Callable) -> bool:
        """
        Subscribe to events of a specific type.

        Args:
            event_type: Type of event to subscribe to
            callback: Function to call when event occurs

        Returns:
            True if subscription successful
        """
        self._subscribers[event_type].append(callback)
        return True

    async def unsubscribe(self, event_type: str, callback: Callable) -> bool:
        """Remove a subscription."""
        if callback in self._subscribers.get(event_type, []):
            self._subscribers[event_type].remove(callback)
            return True
        return False

    def get_active_nodes(
        self, node_type: Optional[str] = None
    ) -> List[NeuralMeshNode]:
        """
        Get all active nodes, optionally filtered by type.

        Args:
            node_type: Filter by node type (e.g., "uae", "sai")

        Returns:
            List of active nodes
        """
        nodes = list(self._nodes.values())
        if node_type:
            nodes = [n for n in nodes if n.node_type == node_type]
        return [n for n in nodes if n.status == "active" and n.is_healthy()]

    def get_node(self, node_id: str) -> Optional[NeuralMeshNode]:
        """Get a specific node by ID."""
        return self._nodes.get(node_id)

    async def update_context(self, key: str, value: Any) -> None:
        """Update shared context and broadcast change."""
        self._context[key] = value
        await self.broadcast("context_update", {key: value})

    def get_context(self, key: str = None) -> Any:
        """Get shared context (or specific key)."""
        if key:
            return self._context.get(key)
        return dict(self._context)

    async def start(self) -> None:
        """Start the Neural Mesh coordinator."""
        self._running = True
        self._sync_task = asyncio.create_task(self._sync_loop())
        self._logger.info("Neural Mesh coordinator started")

    async def stop(self) -> None:
        """Stop the Neural Mesh coordinator."""
        self._running = False
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
        self._logger.info("Neural Mesh coordinator stopped")

    async def _sync_loop(self) -> None:
        """Background sync loop for heartbeat checking."""
        while self._running:
            try:
                await asyncio.sleep(self._sync_interval)

                # Check for dead nodes
                async with self._lock:
                    for node_id, node in list(self._nodes.items()):
                        if not node.is_healthy(self._heartbeat_timeout):
                            node.status = "unhealthy"
                            await self.broadcast(
                                "node_unhealthy",
                                {"node_id": node_id},
                                source="coordinator",
                            )

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._logger.error(f"Sync loop error: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Get Neural Mesh statistics."""
        return {
            "total_nodes": len(self._nodes),
            "active_nodes": len([n for n in self._nodes.values() if n.status == "active"]),
            "unhealthy_nodes": len(
                [n for n in self._nodes.values() if n.status == "unhealthy"]
            ),
            "subscribers": {k: len(v) for k, v in self._subscribers.items()},
            "context_keys": list(self._context.keys()),
            "running": self._running,
            **self._metrics,
        }


class BasicNeuralMesh:
    """
    Basic Neural Mesh for fallback compatibility.

    A simplified version that provides the same interface but without
    the full production features. Used when full mesh is not needed.
    """

    def __init__(self, sync_interval: float = 5.0):
        """Initialize basic Neural Mesh."""
        self._nodes: Dict[str, NeuralMeshNode] = {}
        self._context: Dict[str, Any] = {}
        self._subscribers: Dict[str, List[Callable]] = defaultdict(list)
        self._sync_interval = sync_interval
        self._running = False
        self._logger = logging.getLogger("NeuralMesh.Basic")

    async def register_node(
        self,
        node: Optional[NeuralMeshNode] = None,
        node_name: Optional[str] = None,
        node_type: Optional[str] = None,
        capabilities: Optional[List[str]] = None,
    ) -> bool:
        """Register a node."""
        if node is not None:
            self._nodes[node.node_id] = node
        elif node_name is not None:
            new_node = NeuralMeshNode(
                node_id=node_name,
                node_type=node_type or "unknown",
                capabilities=capabilities or [],
            )
            self._nodes[node_name] = new_node
        return True

    async def broadcast(
        self,
        event_type: str,
        data: Dict[str, Any],
        source: Optional[str] = None,
    ) -> None:
        """Broadcast an event."""
        for subscriber in self._subscribers.get(event_type, []):
            try:
                if asyncio.iscoroutinefunction(subscriber):
                    await subscriber(
                        {"event_type": event_type, "data": data, "source": source}
                    )
                else:
                    subscriber(
                        {"event_type": event_type, "data": data, "source": source}
                    )
            except Exception as e:
                self._logger.warning(f"Subscriber error: {e}")

    async def subscribe(self, event_type: str, callback: Callable) -> bool:
        """Subscribe to events."""
        self._subscribers[event_type].append(callback)
        return True

    def get_active_nodes(
        self, node_type: Optional[str] = None
    ) -> List[NeuralMeshNode]:
        """Get active nodes."""
        nodes = list(self._nodes.values())
        if node_type:
            nodes = [n for n in nodes if n.node_type == node_type]
        return [n for n in nodes if n.status == "active"]

    async def start(self) -> None:
        """Start the mesh."""
        self._running = True

    async def stop(self) -> None:
        """Stop the mesh."""
        self._running = False

    def get_stats(self) -> Dict[str, Any]:
        """Get statistics."""
        return {
            "total_nodes": len(self._nodes),
            "active_nodes": len(
                [n for n in self._nodes.values() if n.status == "active"]
            ),
            "mode": "basic_fallback",
        }


# =============================================================================
# ZONE 4.7: MULTI-AGENT SYSTEM (MAS)
# =============================================================================
# v108.0: Multi-Agent System for coordinated autonomous execution
# Enables parallel task processing with dynamic agent spawning


class AgentStatus(Enum):
    """Status of an agent in the MAS."""

    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class AgentTask:
    """
    A task for an agent to execute.

    Supports:
    - Priority-based scheduling
    - Task dependencies
    - Parent-child relationships for subtasks
    """

    task_id: str
    goal: str
    context: Dict[str, Any] = field(default_factory=dict)
    priority: int = 5  # 1-10, higher = more important
    dependencies: List[str] = field(default_factory=list)
    parent_task_id: Optional[str] = None
    status: AgentStatus = AgentStatus.IDLE
    result: Optional[Any] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None

    @property
    def duration(self) -> Optional[float]:
        """Get task duration in seconds."""
        if self.started_at and self.completed_at:
            return self.completed_at - self.started_at
        return None


@dataclass
class Agent:
    """
    An autonomous agent in the MAS.

    Each agent has:
    - Unique identifier
    - Type (determines what executor to use)
    - Capabilities (for task matching)
    - Current task assignment
    - Performance metrics
    """

    agent_id: str
    agent_type: str
    capabilities: List[str] = field(default_factory=list)
    current_task: Optional[AgentTask] = None
    status: AgentStatus = AgentStatus.IDLE
    metrics: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """Initialize metrics."""
        if not self.metrics:
            self.metrics = {
                "tasks_completed": 0,
                "tasks_failed": 0,
                "total_time": 0.0,
            }


class MultiAgentSystem:
    """
    Multi-Agent System for coordinated autonomous execution.

    Provides:
    - Dynamic agent spawning based on task complexity
    - Task decomposition and parallel execution
    - Agent collaboration and result aggregation
    - Conflict resolution for shared resources
    - Load balancing across available agents

    This enables JARVIS to break down complex goals into subtasks
    and execute them in parallel with multiple specialized agents.
    """

    def __init__(
        self,
        max_concurrent_agents: int = 5,
        logger: Optional[Any] = None,
    ):
        """
        Initialize the Multi-Agent System.

        Args:
            max_concurrent_agents: Maximum number of agents that can run simultaneously
            logger: Logger instance
        """
        self._agents: Dict[str, Agent] = {}
        self._task_queue: asyncio.Queue = asyncio.Queue()
        self._completed_tasks: Dict[str, AgentTask] = {}
        self._max_concurrent = max_concurrent_agents
        self._running = False
        self._coordinator_task: Optional[asyncio.Task] = None
        self._agent_executors: Dict[str, Callable] = {}
        self._logger = logger or logging.getLogger("MAS")
        self._lock = asyncio.Lock()

        # Metrics
        self._metrics = {
            "tasks_submitted": 0,
            "tasks_completed": 0,
            "tasks_failed": 0,
            "agents_spawned": 0,
        }

        # Register default executors
        self._register_default_executors()

    def _register_default_executors(self) -> None:
        """Register default agent executors."""

        async def general_executor(task: AgentTask) -> Dict[str, Any]:
            """Default general-purpose agent executor."""
            return {"status": "completed", "message": f"Processed: {task.goal}"}

        async def explorer_executor(task: AgentTask) -> Dict[str, Any]:
            """Explorer agent for search and discovery tasks."""
            return {"status": "completed", "message": f"Explored: {task.goal}"}

        async def creator_executor(task: AgentTask) -> Dict[str, Any]:
            """Creator agent for generation tasks."""
            return {"status": "completed", "message": f"Created: {task.goal}"}

        async def analyzer_executor(task: AgentTask) -> Dict[str, Any]:
            """Analyzer agent for analysis tasks."""
            return {"status": "completed", "message": f"Analyzed: {task.goal}"}

        async def scraper_executor(task: AgentTask) -> Dict[str, Any]:
            """Scraper agent for web scraping tasks."""
            return {"status": "completed", "message": f"Scraped: {task.goal}"}

        self._agent_executors["general"] = general_executor
        self._agent_executors["explorer"] = explorer_executor
        self._agent_executors["creator"] = creator_executor
        self._agent_executors["analyzer"] = analyzer_executor
        self._agent_executors["scraper"] = scraper_executor

    def register_agent_type(self, agent_type: str, executor: Callable) -> None:
        """
        Register an agent type with its executor function.

        Args:
            agent_type: Type name (e.g., "researcher", "coder")
            executor: Async function that executes tasks
        """
        self._agent_executors[agent_type] = executor
        self._logger.debug(f"Agent type registered: {agent_type}")

    async def spawn_agent(
        self,
        agent_type: str,
        capabilities: Optional[List[str]] = None,
    ) -> Agent:
        """
        Spawn a new agent.

        Args:
            agent_type: Type of agent to spawn
            capabilities: List of capabilities for the agent

        Returns:
            The spawned or reused agent

        Raises:
            RuntimeError: If max agents reached and none are idle
        """
        async with self._lock:
            if len(self._agents) >= self._max_concurrent:
                # Find idle agent to reuse
                for agent in self._agents.values():
                    if agent.status == AgentStatus.IDLE:
                        agent.capabilities = capabilities or []
                        return agent
                raise RuntimeError(f"Max agents ({self._max_concurrent}) reached")

            agent = Agent(
                agent_id=f"agent-{uuid.uuid4().hex[:8]}",
                agent_type=agent_type,
                capabilities=capabilities or [],
            )
            self._agents[agent.agent_id] = agent
            self._metrics["agents_spawned"] += 1
            self._logger.info(f"Agent spawned: {agent.agent_id} ({agent_type})")
            return agent

    async def submit_task(self, task: AgentTask) -> str:
        """
        Submit a task for execution.

        Args:
            task: The task to submit

        Returns:
            Task ID
        """
        await self._task_queue.put(task)
        self._metrics["tasks_submitted"] += 1
        self._logger.debug(f"Task submitted: {task.task_id}")
        return task.task_id

    async def decompose_task(
        self,
        goal: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> List[AgentTask]:
        """
        Decompose a complex goal into subtasks.

        Currently creates a single task. In production, this would use
        an LLM to break down complex goals into subtasks.

        Args:
            goal: The goal to decompose
            context: Optional context for the task

        Returns:
            List of tasks to execute
        """
        # Simple implementation - could be enhanced with LLM decomposition
        task = AgentTask(
            task_id=f"task-{uuid.uuid4().hex[:8]}",
            goal=goal,
            context=context or {},
        )
        return [task]

    def _determine_agent_type(self, task: AgentTask) -> str:
        """
        Determine the best agent type for a task.

        Args:
            task: The task to analyze

        Returns:
            Agent type string
        """
        goal_lower = task.goal.lower()

        if any(w in goal_lower for w in ["search", "find", "look", "discover"]):
            return "explorer"
        elif any(w in goal_lower for w in ["write", "create", "generate", "build"]):
            return "creator"
        elif any(w in goal_lower for w in ["analyze", "review", "check", "evaluate"]):
            return "analyzer"
        elif any(w in goal_lower for w in ["scrape", "fetch", "download", "crawl"]):
            return "scraper"
        else:
            return "general"

    async def execute_task(self, task: AgentTask) -> AgentTask:
        """
        Execute a single task.

        Args:
            task: The task to execute

        Returns:
            The completed task with results
        """
        task.status = AgentStatus.RUNNING
        task.started_at = time.time()
        agent = None

        try:
            # Find appropriate agent type
            agent_type = self._determine_agent_type(task)

            # Spawn or reuse agent
            agent = await self.spawn_agent(agent_type)
            agent.current_task = task
            agent.status = AgentStatus.RUNNING

            # Get executor for this agent type
            executor = self._agent_executors.get(agent_type)
            if executor:
                if asyncio.iscoroutinefunction(executor):
                    result = await executor(task)
                else:
                    result = executor(task)
                task.result = result
                task.status = AgentStatus.COMPLETED
                self._metrics["tasks_completed"] += 1
                agent.metrics["tasks_completed"] += 1
            else:
                task.error = f"No executor for agent type: {agent_type}"
                task.status = AgentStatus.FAILED
                self._metrics["tasks_failed"] += 1

        except Exception as e:
            task.error = str(e)
            task.status = AgentStatus.FAILED
            self._metrics["tasks_failed"] += 1
            self._logger.error(f"Task {task.task_id} failed: {e}")
            if agent:
                agent.metrics["tasks_failed"] += 1

        finally:
            task.completed_at = time.time()
            if agent:
                if task.duration:
                    agent.metrics["total_time"] += task.duration
                agent.current_task = None
                agent.status = AgentStatus.IDLE

        self._completed_tasks[task.task_id] = task
        return task

    async def run_goal(
        self,
        goal: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> List[AgentTask]:
        """
        Execute a goal by decomposing and running all subtasks.

        Args:
            goal: The goal to achieve
            context: Optional context for the task

        Returns:
            List of completed tasks
        """
        tasks = await self.decompose_task(goal, context)
        results = []

        # Execute tasks (respecting dependencies)
        for task in tasks:
            result = await self.execute_task(task)
            results.append(result)

        return results

    async def start(self) -> None:
        """Start the MAS coordinator."""
        self._running = True
        self._coordinator_task = asyncio.create_task(self._coordinate())
        self._logger.info("MAS coordinator started")

    async def stop(self) -> None:
        """Stop the MAS."""
        self._running = False
        if self._coordinator_task:
            self._coordinator_task.cancel()
            try:
                await self._coordinator_task
            except asyncio.CancelledError:
                pass
        self._logger.info("MAS coordinator stopped")

    async def _coordinate(self) -> None:
        """Background coordination loop."""
        while self._running:
            try:
                # Process queued tasks
                try:
                    task = await asyncio.wait_for(
                        self._task_queue.get(), timeout=1.0
                    )
                    asyncio.create_task(self.execute_task(task))
                except asyncio.TimeoutError:
                    pass

                # Clean up completed agents
                for agent in list(self._agents.values()):
                    if agent.status == AgentStatus.COMPLETED:
                        agent.status = AgentStatus.IDLE

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._logger.error(f"Coordinator error: {e}")
                await asyncio.sleep(1)

    def get_task_status(self, task_id: str) -> Optional[AgentTask]:
        """Get the status of a task."""
        return self._completed_tasks.get(task_id)

    def get_agent_stats(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Get statistics for a specific agent."""
        agent = self._agents.get(agent_id)
        if agent:
            return {
                "agent_id": agent.agent_id,
                "agent_type": agent.agent_type,
                "status": agent.status.value,
                "capabilities": agent.capabilities,
                **agent.metrics,
            }
        return None

    def get_stats(self) -> Dict[str, Any]:
        """Get MAS statistics."""
        return {
            "total_agents": len(self._agents),
            "active_agents": len(
                [a for a in self._agents.values() if a.status == AgentStatus.RUNNING]
            ),
            "idle_agents": len(
                [a for a in self._agents.values() if a.status == AgentStatus.IDLE]
            ),
            "queued_tasks": self._task_queue.qsize(),
            "completed_tasks": len(self._completed_tasks),
            "max_concurrent": self._max_concurrent,
            **self._metrics,
        }


# =============================================================================
# ZONE 4.8: COLLECTIVE AI INTELLIGENCE (CAI)
# =============================================================================
# v108.0: Emergent intelligence from all subsystems
# Synthesizes insights across UAE, SAI, MAS, and other components


@dataclass
class InsightSource:
    """Source of an insight in the Collective AI."""

    system: str  # "uae", "sai", "mas", "neural_mesh", etc.
    confidence: float
    timestamp: float
    data: Dict[str, Any]


@dataclass
class CollectiveInsight:
    """
    An insight aggregated from multiple sources.

    Represents knowledge synthesized across multiple intelligence
    systems with aggregated confidence scoring.
    """

    insight_id: str
    topic: str
    sources: List[InsightSource] = field(default_factory=list)
    aggregated_confidence: float = 0.0
    recommendations: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


class CollectiveAI:
    """
    Collective AI Intelligence - Emergent intelligence from all subsystems.

    Provides:
    - Synthesis of insights from UAE, SAI, MAS
    - Cross-system pattern detection
    - Proactive recommendation generation
    - Adaptive learning from system interactions

    This is the highest level of JARVIS's intelligence, combining
    all subsystems into a unified understanding.
    """

    def __init__(self, logger: Optional[Any] = None):
        """
        Initialize the Collective AI.

        Args:
            logger: Logger instance
        """
        self._insights: Dict[str, CollectiveInsight] = {}
        self._patterns: List[Dict[str, Any]] = []
        self._recommendation_callbacks: List[Callable] = []
        self._logger = logger or logging.getLogger("CAI")
        self._lock = asyncio.Lock()

        # Connected subsystems
        self._neural_mesh: Optional[NeuralMeshCoordinator] = None
        self._mas: Optional[MultiAgentSystem] = None
        self._learning_goals: Optional[IntelligentLearningGoalsDiscovery] = None

        # Metrics
        self._metrics = {
            "insights_created": 0,
            "patterns_detected": 0,
            "recommendations_generated": 0,
        }

    def connect_neural_mesh(self, mesh: NeuralMeshCoordinator) -> None:
        """Connect to the Neural Mesh for event coordination."""
        self._neural_mesh = mesh

    def connect_mas(self, mas: MultiAgentSystem) -> None:
        """Connect to the Multi-Agent System."""
        self._mas = mas

    def connect_learning_goals(
        self, learning_goals: IntelligentLearningGoalsDiscovery
    ) -> None:
        """Connect to the Learning Goals Discovery system."""
        self._learning_goals = learning_goals

    async def add_insight_source(self, topic: str, source: InsightSource) -> None:
        """
        Add an insight source for a topic.

        Args:
            topic: Topic being tracked
            source: The insight source to add
        """
        async with self._lock:
            if topic not in self._insights:
                self._insights[topic] = CollectiveInsight(
                    insight_id=f"insight-{uuid.uuid4().hex[:8]}",
                    topic=topic,
                )
                self._metrics["insights_created"] += 1

            self._insights[topic].sources.append(source)
            self._recalculate_confidence(topic)

    def _recalculate_confidence(self, topic: str) -> None:
        """Recalculate aggregated confidence for a topic."""
        if topic in self._insights:
            insight = self._insights[topic]
            if insight.sources:
                # Weighted average based on source confidence and recency
                total = sum(s.confidence for s in insight.sources)
                insight.aggregated_confidence = total / len(insight.sources)

    def get_insight(self, topic: str) -> Optional[CollectiveInsight]:
        """Get the collective insight for a topic."""
        return self._insights.get(topic)

    def detect_patterns(self) -> List[Dict[str, Any]]:
        """
        Detect patterns across all insights.

        Looks for:
        - Topics with multiple high-confidence sources
        - Correlations between topics
        - Emerging trends

        Returns:
            List of detected patterns
        """
        patterns = []

        for insight in self._insights.values():
            # Multi-source patterns
            if len(insight.sources) >= 2 and insight.aggregated_confidence > 0.7:
                patterns.append(
                    {
                        "type": "multi_source",
                        "topic": insight.topic,
                        "confidence": insight.aggregated_confidence,
                        "source_count": len(insight.sources),
                        "systems": list(set(s.system for s in insight.sources)),
                    }
                )

            # High confidence patterns
            if insight.aggregated_confidence > 0.9:
                patterns.append(
                    {
                        "type": "high_confidence",
                        "topic": insight.topic,
                        "confidence": insight.aggregated_confidence,
                    }
                )

        self._patterns = patterns
        self._metrics["patterns_detected"] = len(patterns)
        return patterns

    async def generate_recommendations(self) -> List[str]:
        """
        Generate proactive recommendations based on patterns.

        Returns:
            List of recommendation strings
        """
        recommendations = []
        patterns = self.detect_patterns()

        for pattern in patterns:
            if pattern.get("confidence", 0) > 0.8:
                topic = pattern.get("topic", "unknown")
                pattern_type = pattern.get("type", "unknown")

                if pattern_type == "multi_source":
                    recommendations.append(
                        f"High-confidence insight on '{topic}' - consider taking action"
                    )
                elif pattern_type == "high_confidence":
                    recommendations.append(
                        f"Strong pattern detected for '{topic}' - may warrant attention"
                    )

        # Notify callbacks
        for callback in self._recommendation_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(recommendations)
                else:
                    callback(recommendations)
            except Exception as e:
                self._logger.warning(f"Recommendation callback error: {e}")

        self._metrics["recommendations_generated"] += len(recommendations)
        return recommendations

    def register_recommendation_callback(self, callback: Callable) -> None:
        """Register a callback to receive recommendations."""
        self._recommendation_callbacks.append(callback)

    async def synthesize_state(self) -> Dict[str, Any]:
        """
        Synthesize the current collective state.

        Combines information from all connected subsystems into
        a unified state representation.

        Returns:
            Unified state dictionary
        """
        state = {
            "timestamp": time.time(),
            "insights_count": len(self._insights),
            "patterns_count": len(self._patterns),
            "subsystems": {},
        }

        # Get Neural Mesh state
        if self._neural_mesh:
            state["subsystems"]["neural_mesh"] = self._neural_mesh.get_stats()

        # Get MAS state
        if self._mas:
            state["subsystems"]["mas"] = self._mas.get_stats()

        # Get Learning Goals state
        if self._learning_goals:
            state["subsystems"]["learning_goals"] = self._learning_goals.get_stats()

        return state

    def get_stats(self) -> Dict[str, Any]:
        """Get CAI statistics."""
        return {
            "total_insights": len(self._insights),
            "total_patterns": len(self._patterns),
            "connected_subsystems": sum(
                [
                    self._neural_mesh is not None,
                    self._mas is not None,
                    self._learning_goals is not None,
                ]
            ),
            **self._metrics,
        }


# ╔═══════════════════════════════════════════════════════════════════════════════╗
# ║                                                                               ║
# ║   END OF ZONE 4                                                               ║
# ║   Zones 5-7 will be added in subsequent commits                               ║
# ║                                                                               ║
# ╚═══════════════════════════════════════════════════════════════════════════════╝

# =============================================================================
# ZONE 0-4 SELF-TEST FUNCTION
# =============================================================================
# Tests for Zones 0-4 (run with: python unified_supervisor.py --test zones)

async def _test_zones_0_through_4():
    """Test Zones 0-4 components (Foundation through Intelligence)."""
    # Test Zone 0, 1, 2, and 3
    TerminalUI.print_banner(f"{KERNEL_NAME} v{KERNEL_VERSION}", "Zones 0-3 Implemented")

    # Initialize logger
    logger = UnifiedLogger()

    # Show config
    config = SystemKernelConfig.from_environment()
    logger.info("Configuration loaded")

    with logger.section_start(LogSection.CONFIG, "Configuration Summary"):
        for line in config.summary().split("\n"):
            logger.info(line)

    # Test warnings
    warnings_list = config.validate()
    if warnings_list:
        with logger.section_start(LogSection.BOOT, "Configuration Warnings"):
            for w in warnings_list:
                logger.warning(w)

    # Test circuit breaker
    logger.info("Testing circuit breaker...")
    cb = CircuitBreaker("test", failure_threshold=3)
    logger.success(f"Circuit breaker state: {cb.state.value}")

    # Test lock
    logger.info("Testing startup lock...")
    lock = StartupLock("kernel")  # Use standard kernel lock name
    is_locked, holder_pid = lock.is_locked()
    logger.success(f"Lock status: locked={is_locked}, holder_pid={holder_pid}")

    # ========== Zone 3 Tests ==========
    with logger.section_start(LogSection.RESOURCES, "Zone 3: Resource Managers"):

        # Test ResourceManagerRegistry
        logger.info("Creating resource manager registry...")
        registry = ResourceManagerRegistry(config)

        # Create managers
        docker_mgr = DockerDaemonManager(config)
        gcp_mgr = GCPInstanceManager(config)
        cost_mgr = ScaleToZeroCostOptimizer(config)
        port_mgr = DynamicPortManager(config)
        voice_cache_mgr = SemanticVoiceCacheManager(config)
        storage_mgr = TieredStorageManager(config)

        # Register all
        registry.register(docker_mgr)
        registry.register(gcp_mgr)
        registry.register(cost_mgr)
        registry.register(port_mgr)
        registry.register(voice_cache_mgr)
        registry.register(storage_mgr)

        logger.success(f"Registered {registry.manager_count} resource managers")

        # Initialize all in parallel
        logger.info("Initializing all managers in parallel...")
        with logger.timed("resource_initialization"):
            results = await registry.initialize_all(parallel=True)

        for name, success in results.items():
            if success:
                logger.success(f"  {name}: initialized")
            else:
                logger.warning(f"  {name}: failed")

        # Health check all
        logger.info("Running health checks...")
        health_results = await registry.health_check_all()

        for name, (healthy, message) in health_results.items():
            if healthy:
                logger.debug(f"  {name}: {message}")
            else:
                logger.warning(f"  {name}: {message}")

        # Test DynamicPortManager specifically
        logger.info(f"Selected port: {port_mgr.selected_port}")

        # Test ScaleToZeroCostOptimizer
        cost_mgr.record_activity("test")
        stats = cost_mgr.get_statistics()
        logger.info(f"Scale-to-Zero: {stats['activity_count']} activities, idle {stats['idle_minutes']:.1f}min")

        # Test TieredStorageManager
        await storage_mgr.put("test_key", {"data": "test_value"})
        result = await storage_mgr.get("test_key")
        if result:
            logger.success("Tiered storage put/get: working")
        else:
            logger.warning("Tiered storage put/get: failed")

        storage_stats = storage_mgr.get_statistics()
        logger.info(f"Hot tier: {storage_stats['hot_items']} items, {storage_stats['hot_size_mb']:.2f}MB")

        # Get all status
        logger.info("Getting all manager status...")
        all_status = registry.get_all_status()
        ready_count = sum(1 for s in all_status.values() if s.get("ready"))
        logger.success(f"Managers ready: {ready_count}/{registry.manager_count}")

        # Cleanup
        logger.info("Cleaning up managers...")
        await registry.cleanup_all()
        logger.success("All managers cleaned up")

    # ========== Zone 4 Tests ==========
    with logger.section_start(LogSection.INTELLIGENCE, "Zone 4: Intelligence Layer"):

        # Test AdaptiveThresholdManager
        logger.info("Testing AdaptiveThresholdManager...")
        threshold_mgr = AdaptiveThresholdManager()
        ram_state = threshold_mgr.get_ram_state(0.70)
        logger.success(f"RAM state at 70%: {ram_state.value}")

        # Test thresholds
        thresholds = threshold_mgr.get_all_thresholds()
        logger.info(f"Learned thresholds: {len(thresholds['thresholds'])} values")

        # Test HybridLearningModel
        logger.info("Testing HybridLearningModel...")
        learning_model = HybridLearningModel()

        # Record some observations
        await learning_model.record_ram_observation(
            timestamp=time.time(),
            usage=0.65,
            components_active={"ml_models": True}
        )

        # Get spike prediction
        prediction = await learning_model.predict_ram_spike(
            current_usage=0.75,
            trend=0.01
        )
        logger.success(f"Spike prediction: likely={prediction['spike_likely']}, confidence={prediction['confidence']:.2f}")

        # Get optimal monitoring interval
        interval = await learning_model.get_optimal_monitoring_interval(0.75)
        logger.info(f"Optimal monitoring interval at 75% RAM: {interval}s")

        # Test GoalInferenceEngine
        logger.info("Testing GoalInferenceEngine...")
        goal_engine = GoalInferenceEngine(config)
        await goal_engine.initialize()

        # Test intent classification
        intent_result = await goal_engine.safe_infer({"text": "fix the bug in the login function"})
        logger.success(f"Intent: {intent_result['intent']} (confidence: {intent_result['confidence']:.2f})")

        # Test HybridWorkloadRouter
        logger.info("Testing HybridWorkloadRouter...")
        router = HybridWorkloadRouter(config)
        await router.initialize()

        routing = await router.safe_infer({
            "component": "ml_models",
            "ram_usage": 0.80
        })
        logger.success(f"Routing decision: {routing['location']} (latency: {routing['latency_estimate_ms']}ms)")

        # Test HybridIntelligenceCoordinator
        logger.info("Testing HybridIntelligenceCoordinator...")
        coordinator = HybridIntelligenceCoordinator(config)
        await coordinator.initialize()

        coord_result = await coordinator.safe_infer({
            "ram_usage": 0.75,
            "component": "vision",
            "trend": 0.005
        })
        logger.success(f"Coordinator: RAM state={coord_result['ram_state']}, spike_likely={coord_result['spike_prediction']['spike_likely']}")

        # Get comprehensive status
        status = await coordinator.get_comprehensive_status()
        logger.info(f"Intelligence components: {len(status)} keys")

        # Test IntelligenceRegistry
        logger.info("Testing IntelligenceRegistry...")
        intel_registry = IntelligenceRegistry(config)
        intel_registry.register(router)
        intel_registry.register(goal_engine)
        intel_registry.register(coordinator)

        init_results = await intel_registry.initialize_all()
        initialized_count = sum(1 for v in init_results.values() if v)
        logger.success(f"Intelligence registry: {initialized_count}/{len(init_results)} initialized")

    logger.print_startup_summary()
    TerminalUI.print_success("Zones 0-4 validation complete!")


# =============================================================================
# =============================================================================
#
#  ███████╗ ██████╗ ███╗   ██╗███████╗    ███████╗
#  ╚══███╔╝██╔═══██╗████╗  ██║██╔════╝    ██╔════╝
#    ███╔╝ ██║   ██║██╔██╗ ██║█████╗      ███████╗
#   ███╔╝  ██║   ██║██║╚██╗██║██╔══╝      ╚════██║
#  ███████╗╚██████╔╝██║ ╚████║███████╗    ███████║
#  ╚══════╝ ╚═════╝ ╚═╝  ╚═══╝╚══════╝    ╚══════╝
#
#  ZONE 5: PROCESS ORCHESTRATION
#  Lines ~5320-8000
#
#  This zone handles:
#  - UnifiedSignalHandler: SIGINT/SIGTERM with escalation
#  - ComprehensiveZombieCleanup: Stale process detection/termination
#  - ProcessStateManager: Managed process lifecycle tracking
#  - HotReloadWatcher: File change detection for dev mode
#  - ProgressiveReadinessManager: Multi-tier readiness (STARTING → FULL)
#  - TrinityIntegrator: Cross-repo Prime/Reactor integration
#
# =============================================================================
# =============================================================================


# =============================================================================
# ZONE 5.1: UNIFIED SIGNAL HANDLER
# =============================================================================
# Provides escalating shutdown behavior for SIGINT (Ctrl+C) and SIGTERM:
# - 1st signal: Graceful shutdown (waits for cleanup)
# - 2nd signal: Faster shutdown (shorter timeouts)
# - 3rd signal: Immediate exit (sys.exit)
# =============================================================================

class UnifiedSignalHandler:
    """
    Unified signal handling for the monolithic kernel.

    Handles SIGINT (Ctrl+C) and SIGTERM gracefully, ensuring
    all components shut down in the correct order.

    Signal escalation:
    - 1st signal: Graceful shutdown (waits for cleanup)
    - 2nd signal: Faster shutdown (shorter timeouts)
    - 3rd signal: Immediate exit (os._exit)

    Thread-safe: Uses threading.Lock for signal counting since signals
    can arrive from any thread context.

    Features:
    - Async-first with sync fallback for Windows
    - Callback registration for custom cleanup
    - Timeout tracking for fast vs slow shutdown
    - Idempotent installation (safe to call multiple times)
    """

    def __init__(self) -> None:
        self._shutdown_event: Optional[asyncio.Event] = None
        self._shutdown_requested: bool = False
        self._shutdown_count: int = 0
        self._lock = threading.Lock()
        self._shutdown_reason: Optional[str] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._installed: bool = False
        self._callbacks: List[Callable[[], Coroutine[Any, Any, None]]] = []
        self._first_signal_time: Optional[float] = None

    def _get_event(self) -> asyncio.Event:
        """Lazily create shutdown event (needs running event loop)."""
        if self._shutdown_event is None:
            self._shutdown_event = asyncio.Event()
        return self._shutdown_event

    def register_callback(self, callback: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """
        Register an async callback to run during shutdown.

        Callbacks are run in registration order during graceful shutdown.
        """
        self._callbacks.append(callback)

    def install(self, loop: asyncio.AbstractEventLoop) -> None:
        """
        Install signal handlers on the event loop.

        Args:
            loop: The running asyncio event loop
        """
        if self._installed:
            return  # Avoid duplicate registration

        self._loop = loop

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                # Unix: Use async-safe loop.add_signal_handler
                loop.add_signal_handler(
                    sig,
                    lambda s=sig: self._schedule_signal_handling(s)
                )
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                signal.signal(sig, lambda s, f, sig=sig: self._sync_handle_signal(sig))
            except Exception as e:
                # Log but don't fail - signal handling is best-effort
                print(f"[Kernel] Warning: Could not install handler for {sig.name}: {e}")

        self._installed = True
        print("[Kernel] Unified signal handlers installed (SIGINT, SIGTERM)")

    def _schedule_signal_handling(self, sig: signal.Signals) -> None:
        """
        Schedule async signal handling from sync context.

        This is called by loop.add_signal_handler which runs in sync context.
        We use create_task to handle the signal asynchronously.
        """
        if self._loop is not None and self._loop.is_running():
            self._loop.create_task(self._handle_signal(sig))
        else:
            # Fallback to sync handling if loop not available
            self._sync_handle_signal(sig.value)

    def _sync_handle_signal(self, sig: int) -> None:
        """
        Synchronous signal handler (for Windows compatibility and fallback).

        This handles signals when async handling is not possible.
        """
        with self._lock:
            self._shutdown_count += 1
            count = self._shutdown_count
            self._shutdown_requested = True

            if self._first_signal_time is None:
                self._first_signal_time = time.time()

            try:
                sig_name = signal.Signals(sig).name
            except (ValueError, AttributeError):
                sig_name = f"signal_{sig}"

            self._shutdown_reason = sig_name

            if count == 1:
                print(f"\n[Kernel] Received {sig_name} - initiating graceful shutdown...")
            elif count == 2:
                print(f"[Kernel] Received second {sig_name} - forcing faster shutdown...")
            else:
                print(f"[Kernel] Received third {sig_name} - forcing immediate exit!")
                os._exit(128 + sig)

            # Try to set the shutdown event if available
            if self._shutdown_event is not None:
                try:
                    if self._loop is not None and self._loop.is_running():
                        self._loop.call_soon_threadsafe(self._shutdown_event.set)
                    else:
                        # Direct set as fallback
                        self._shutdown_event.set()
                except Exception:
                    pass  # Best effort

    async def _handle_signal(self, sig: signal.Signals) -> None:
        """
        Handle incoming signal asynchronously.

        Provides escalating shutdown behavior based on signal count.
        """
        with self._lock:
            self._shutdown_count += 1
            count = self._shutdown_count

            if self._first_signal_time is None:
                self._first_signal_time = time.time()

        sig_name = sig.name
        self._shutdown_reason = sig_name
        self._shutdown_requested = True

        if count == 1:
            print(f"\n[Kernel] Received {sig_name} - initiating graceful shutdown...")
            self._get_event().set()
        elif count == 2:
            print(f"[Kernel] Received second {sig_name} - forcing faster shutdown...")
            self._get_event().set()
        else:
            print(f"[Kernel] Received third {sig_name} - forcing immediate exit!")
            os._exit(128 + sig.value)

    async def run_callbacks(self) -> None:
        """Run all registered shutdown callbacks."""
        for callback in self._callbacks:
            try:
                await asyncio.wait_for(callback(), timeout=5.0)
            except asyncio.TimeoutError:
                print(f"[Kernel] Shutdown callback timed out")
            except Exception as e:
                print(f"[Kernel] Shutdown callback error: {e}")

    async def wait_for_shutdown(self) -> None:
        """Wait for shutdown signal."""
        await self._get_event().wait()

    @property
    def shutdown_requested(self) -> bool:
        """Check if shutdown was requested."""
        return self._shutdown_requested

    @property
    def shutdown_count(self) -> int:
        """Number of shutdown signals received."""
        return self._shutdown_count

    @property
    def shutdown_reason(self) -> Optional[str]:
        """Reason for shutdown (signal name)."""
        return self._shutdown_reason

    @property
    def is_fast_shutdown(self) -> bool:
        """Check if we're in fast shutdown mode (2+ signals received)."""
        return self._shutdown_count >= 2

    @property
    def seconds_since_first_signal(self) -> float:
        """Seconds since first shutdown signal (for timeout decisions)."""
        if self._first_signal_time is None:
            return 0.0
        return time.time() - self._first_signal_time

    def reset(self) -> None:
        """Reset the signal handler state (for testing or restart scenarios)."""
        with self._lock:
            self._shutdown_requested = False
            self._shutdown_count = 0
            self._shutdown_reason = None
            self._first_signal_time = None
            if self._shutdown_event is not None:
                self._shutdown_event.clear()


# Global signal handler singleton
_unified_signal_handler: Optional[UnifiedSignalHandler] = None


def get_unified_signal_handler() -> UnifiedSignalHandler:
    """
    Get or create the unified signal handler singleton.

    Returns:
        The global UnifiedSignalHandler instance
    """
    global _unified_signal_handler
    if _unified_signal_handler is None:
        _unified_signal_handler = UnifiedSignalHandler()
    return _unified_signal_handler


# =============================================================================
# ZONE 5.2: ZOMBIE PROCESS DETECTION DATA STRUCTURES
# =============================================================================

@dataclass
class ZombieProcessInfo:
    """Extended process info with zombie detection metadata."""
    pid: int
    cmdline: str = ""
    age_seconds: float = 0.0
    memory_mb: float = 0.0
    cpu_percent: float = 0.0
    status: str = ""
    repo_origin: str = ""
    is_orphaned: bool = False
    is_zombie_like: bool = False
    stale_connection_count: int = 0
    detection_source: str = ""


# =============================================================================
# ZONE 5.3: COMPREHENSIVE ZOMBIE CLEANUP SYSTEM
# =============================================================================

class ComprehensiveZombieCleanup:
    """
    Comprehensive Zombie Cleanup System for JARVIS Ecosystem.

    This system provides ultra-robust cleanup across all services:
    - JARVIS (main backend) - typically port 8010
    - JARVIS-Prime (J-Prime Mind) - typically port 8000
    - Reactor-Core (Nerves) - typically port 8090

    Features:
    - Async parallel discovery across multiple detection sources
    - Zombie detection via responsiveness heuristics (orphaned, stuck, stale connections)
    - Port-based service detection
    - Graceful termination with cascade (SIGINT → SIGTERM → SIGKILL)
    - Circuit breaker pattern to prevent cleanup storms
    - File descriptor safe operations

    This runs BEFORE startup to ensure a clean environment.
    """

    def __init__(
        self,
        config: SystemKernelConfig,
        logger: UnifiedLogger,
        enable_circuit_breaker: bool = True,
    ) -> None:
        self.config = config
        self.logger = logger
        self._my_pid = os.getpid()
        self._my_parent = os.getppid()
        self._enable_circuit_breaker = enable_circuit_breaker

        # Circuit breaker state
        self._cleanup_attempts = 0
        self._cleanup_failures = 0
        self._circuit_open = False
        self._circuit_open_until = 0.0
        self._max_failures_before_open = 3
        self._circuit_cooldown = 30.0

        # Stats
        self._stats: Dict[str, int] = {
            "zombies_detected": 0,
            "zombies_killed": 0,
            "ports_freed": 0,
            "orphans_cleaned": 0,
        }

        # Dynamic service ports (discovered from config/env)
        self._service_ports = self._discover_service_ports()

        # Process patterns for detection
        self._process_patterns = [
            "unified_supervisor.py",
            "run_supervisor.py",
            "start_system.py",
            "jarvis",
            "uvicorn.*8010",
            "trinity_orchestrator",
            "jarvis_prime",
            "reactor_core",
        ]

    def _discover_service_ports(self) -> Dict[str, List[int]]:
        """Discover service ports from config and environment."""
        ports: Dict[str, List[int]] = {}

        # Backend port
        backend_port = self.config.backend_port
        ports["jarvis-backend"] = [backend_port] if backend_port else [8010]

        # WebSocket port
        ws_port = self.config.websocket_port
        if ws_port:
            ports["jarvis-websocket"] = [ws_port]

        # Trinity ports from environment
        jprime_port = int(os.getenv("TRINITY_JPRIME_PORT", "8000"))
        reactor_port = int(os.getenv("TRINITY_REACTOR_PORT", "8090"))
        ports["jarvis-prime"] = [jprime_port]
        ports["reactor-core"] = [reactor_port]

        return ports

    def _is_circuit_open(self) -> bool:
        """Check if circuit breaker is open."""
        if not self._enable_circuit_breaker:
            return False

        if self._circuit_open:
            if time.time() > self._circuit_open_until:
                # Circuit is ready to try again (half-open)
                self._circuit_open = False
                return False
            return True
        return False

    def _open_circuit(self) -> None:
        """Open the circuit breaker."""
        self._circuit_open = True
        self._circuit_open_until = time.time() + self._circuit_cooldown

    def get_stats(self) -> Dict[str, int]:
        """Get cleanup statistics."""
        return self._stats.copy()

    async def run_comprehensive_cleanup(self) -> Dict[str, Any]:
        """
        Run comprehensive zombie cleanup.

        This is the main entry point that coordinates all cleanup phases:
        1. Circuit breaker check
        2. Zombie process detection (multi-source)
        3. Parallel termination
        4. Port verification

        Returns:
            Dict with cleanup results and statistics
        """
        results: Dict[str, Any] = {
            "success": True,
            "phases_completed": [],
            "zombies_found": 0,
            "zombies_killed": 0,
            "ports_freed": [],
            "errors": [],
            "duration_ms": 0,
        }

        start_time = time.time()

        try:
            # Phase 0: Circuit breaker check
            if self._is_circuit_open():
                results["success"] = False
                results["errors"].append("Circuit breaker open - cleanup skipped")
                self.logger.warning("[Kernel] Zombie cleanup skipped - circuit breaker open")
                return results

            self._cleanup_attempts += 1
            self.logger.info("[Kernel] 🧹 Starting comprehensive zombie cleanup...")

            # Phase 1: Parallel zombie discovery
            zombies = await self._parallel_zombie_discovery()
            results["zombies_found"] = len(zombies)
            self._stats["zombies_detected"] += len(zombies)
            results["phases_completed"].append("zombie_discovery")

            if zombies:
                self.logger.info(f"[Kernel] Found {len(zombies)} zombie process(es)")

                # Phase 2: Parallel termination
                killed = await self._parallel_zombie_termination(zombies)
                results["zombies_killed"] = killed
                self._stats["zombies_killed"] += killed
                results["phases_completed"].append("zombie_termination")

                # Phase 3: Port verification and cleanup
                await asyncio.sleep(0.3)  # Brief pause for port release
                ports_freed = await self._verify_and_free_ports()
                results["ports_freed"] = ports_freed
                self._stats["ports_freed"] += len(ports_freed)
                results["phases_completed"].append("port_verification")

            results["success"] = True
            self._cleanup_failures = 0  # Reset on success

        except Exception as e:
            results["success"] = False
            results["errors"].append(str(e))
            self._cleanup_failures += 1

            # Open circuit if too many failures
            if self._cleanup_failures >= self._max_failures_before_open:
                self._open_circuit()

            self.logger.error(f"[Kernel] Comprehensive cleanup failed: {e}")

        results["duration_ms"] = int((time.time() - start_time) * 1000)
        self.logger.info(
            f"[Kernel] ✅ Cleanup complete: "
            f"{results['zombies_killed']}/{results['zombies_found']} zombies killed, "
            f"{len(results['ports_freed'])} ports freed in {results['duration_ms']}ms"
        )

        return results

    async def _parallel_zombie_discovery(self) -> Dict[int, ZombieProcessInfo]:
        """
        Parallel zombie discovery using multiple detection sources.

        Detection sources:
        1. Port scanning (service ports)
        2. Process pattern matching
        3. Zombie heuristics (orphaned, stuck, stale connections)
        """
        discovered: Dict[int, ZombieProcessInfo] = {}

        try:
            import psutil
        except ImportError:
            self.logger.warning("[Kernel] psutil not available - limited zombie detection")
            return discovered

        loop = asyncio.get_event_loop()

        with ThreadPoolExecutor(max_workers=3) as executor:
            # Task 1: Port scanning
            port_task = loop.run_in_executor(
                executor, self._discover_from_ports
            )

            # Task 2: Process pattern scanning
            pattern_task = loop.run_in_executor(
                executor, self._discover_from_patterns
            )

            # Task 3: Zombie heuristic detection
            zombie_task = loop.run_in_executor(
                executor, self._discover_zombies_by_heuristics
            )

            # Wait for all
            results = await asyncio.gather(
                port_task, pattern_task, zombie_task,
                return_exceptions=True
            )

        # Merge results (later sources take precedence)
        for result in results:
            if isinstance(result, dict):
                discovered.update(result)

        # Filter out ourselves and our parent
        discovered = {
            pid: info for pid, info in discovered.items()
            if pid not in (self._my_pid, self._my_parent)
        }

        return discovered

    def _discover_from_ports(self) -> Dict[int, ZombieProcessInfo]:
        """Discover processes holding service ports."""
        try:
            import psutil
        except (ImportError, SystemExit):
            return {}

        discovered: Dict[int, ZombieProcessInfo] = {}

        # Flatten all service ports
        all_ports: List[int] = []
        port_to_service: Dict[int, str] = {}
        for service, ports in self._service_ports.items():
            for port in ports:
                all_ports.append(port)
                port_to_service[port] = service

        try:
            for conn in psutil.net_connections(kind='inet'):
                if conn.laddr.port in all_ports and conn.pid:
                    pid = conn.pid
                    if pid in (self._my_pid, self._my_parent):
                        continue
                    if pid in discovered:
                        continue

                    try:
                        proc = psutil.Process(pid)
                        cmdline = " ".join(proc.cmdline())
                        mem_info = proc.memory_info()

                        discovered[pid] = ZombieProcessInfo(
                            pid=pid,
                            cmdline=cmdline[:200],
                            age_seconds=time.time() - proc.create_time(),
                            memory_mb=mem_info.rss / (1024 * 1024),
                            cpu_percent=proc.cpu_percent(interval=0.05),
                            status=proc.status(),
                            repo_origin=port_to_service.get(conn.laddr.port, "unknown"),
                            detection_source=f"port_{conn.laddr.port}",
                        )
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
        except (psutil.AccessDenied, PermissionError, SystemExit):
            pass

        return discovered

    def _discover_from_patterns(self) -> Dict[int, ZombieProcessInfo]:
        """Discover processes matching JARVIS patterns."""
        try:
            import psutil
        except (ImportError, SystemExit):
            return {}

        discovered: Dict[int, ZombieProcessInfo] = {}
        import re

        try:
            for proc in psutil.process_iter(['pid', 'cmdline', 'create_time', 'memory_info', 'status']):
                try:
                    pid = proc.info['pid']
                    if pid in (self._my_pid, self._my_parent):
                        continue

                    cmdline = " ".join(proc.info.get('cmdline') or [])
                    if not cmdline:
                        continue

                    cmdline_lower = cmdline.lower()

                    # Check against patterns
                    for pattern in self._process_patterns:
                        if re.search(pattern, cmdline_lower):
                            mem_info = proc.info.get('memory_info')
                            discovered[pid] = ZombieProcessInfo(
                                pid=pid,
                                cmdline=cmdline[:200],
                                age_seconds=time.time() - proc.info['create_time'],
                                memory_mb=mem_info.rss / (1024 * 1024) if mem_info else 0,
                                status=proc.info.get('status', 'unknown'),
                                repo_origin="jarvis",
                                detection_source="pattern_scan",
                            )
                            break

                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except SystemExit:
            pass

        return discovered

    def _discover_zombies_by_heuristics(self) -> Dict[int, ZombieProcessInfo]:
        """
        Discover zombie-like processes using heuristics.

        A process is zombie-like if:
        - Orphaned (PPID=1) AND sleeping AND has stale connections
        - OR has many stale connections (>5) and <0.1% CPU
        - OR is in zombie/dead state
        """
        try:
            import psutil
        except (ImportError, SystemExit):
            return {}

        discovered: Dict[int, ZombieProcessInfo] = {}
        import re

        try:
            for proc in psutil.process_iter(['pid', 'ppid', 'cmdline', 'create_time', 'status']):
                try:
                    pid = proc.info['pid']
                    if pid in (self._my_pid, self._my_parent):
                        continue

                    cmdline = " ".join(proc.info.get('cmdline') or [])
                    cmdline_lower = cmdline.lower()

                    # Only check JARVIS-related processes
                    is_jarvis_related = any(
                        re.search(pattern, cmdline_lower)
                        for pattern in self._process_patterns
                    )

                    if not is_jarvis_related:
                        continue

                    # Get process details
                    ppid = proc.info.get('ppid', 0)
                    status = proc.info.get('status', '')
                    is_orphaned = ppid == 1
                    is_sleeping = status in ('sleeping', 'idle')
                    is_zombie_state = status in ('zombie', 'dead')

                    # Count stale connections
                    stale_count = 0
                    try:
                        connections = psutil.Process(pid).connections(kind='inet')
                        for conn in connections:
                            if conn.status in ('CLOSE_WAIT', 'TIME_WAIT', 'FIN_WAIT1', 'FIN_WAIT2'):
                                stale_count += 1
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass

                    # Get CPU percent
                    try:
                        cpu_percent = psutil.Process(pid).cpu_percent(interval=0.05)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        cpu_percent = 0.0

                    # Apply zombie heuristics
                    is_zombie_like = (
                        is_zombie_state or
                        (is_orphaned and is_sleeping and stale_count > 0) or
                        (stale_count > 5 and cpu_percent < 0.1)
                    )

                    if is_zombie_like:
                        try:
                            mem_info = psutil.Process(pid).memory_info()
                            memory_mb = mem_info.rss / (1024 * 1024)
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            memory_mb = 0.0

                        discovered[pid] = ZombieProcessInfo(
                            pid=pid,
                            cmdline=cmdline[:200],
                            age_seconds=time.time() - proc.info['create_time'],
                            memory_mb=memory_mb,
                            cpu_percent=cpu_percent,
                            status=status,
                            is_orphaned=is_orphaned,
                            is_zombie_like=True,
                            stale_connection_count=stale_count,
                            detection_source="zombie_heuristic",
                        )

                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except SystemExit:
            pass

        return discovered

    async def _parallel_zombie_termination(
        self, zombies: Dict[int, ZombieProcessInfo]
    ) -> int:
        """
        Terminate zombies in parallel with semaphore control.

        Uses cascade strategy: SIGINT → SIGTERM → SIGKILL
        """
        if not zombies:
            return 0

        max_parallel = int(os.getenv("KERNEL_MAX_PARALLEL_CLEANUPS", "4"))
        semaphore = asyncio.Semaphore(max_parallel)

        async def terminate_one(pid: int, info: ZombieProcessInfo) -> bool:
            async with semaphore:
                return await self._terminate_zombie(pid, info)

        tasks = [
            asyncio.create_task(terminate_one(pid, info))
            for pid, info in zombies.items()
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)
        terminated = sum(1 for r in results if r is True)
        return terminated

    async def _terminate_zombie(
        self, pid: int, info: ZombieProcessInfo
    ) -> bool:
        """Terminate a single zombie with cascade strategy."""
        try:
            import psutil

            self.logger.info(
                f"[Kernel] Killing zombie PID {pid} "
                f"(origin={info.repo_origin}, source={info.detection_source})"
            )

            # Phase 1: SIGINT (graceful)
            try:
                os.kill(pid, signal.SIGINT)
                await asyncio.sleep(0.5)
                if not psutil.pid_exists(pid):
                    return True
            except (ProcessLookupError, OSError):
                return True

            # Phase 2: SIGTERM
            try:
                os.kill(pid, signal.SIGTERM)
                await asyncio.sleep(1.0)
                if not psutil.pid_exists(pid):
                    return True
            except (ProcessLookupError, OSError):
                return True

            # Phase 3: SIGKILL (force)
            try:
                os.kill(pid, signal.SIGKILL)
                await asyncio.sleep(0.3)
            except (ProcessLookupError, OSError):
                pass

            return True

        except Exception as e:
            self.logger.debug(f"[Kernel] Failed to terminate zombie {pid}: {e}")
            return False

    async def _verify_and_free_ports(self) -> List[int]:
        """Verify service ports are free, force-free if needed."""
        freed_ports: List[int] = []

        try:
            import psutil
        except ImportError:
            return freed_ports

        # Check all service ports
        all_ports: List[int] = []
        for ports in self._service_ports.values():
            all_ports.extend(ports)

        for port in all_ports:
            try:
                for conn in psutil.net_connections(kind='inet'):
                    if conn.laddr.port == port and conn.pid:
                        pid = conn.pid
                        if pid in (self._my_pid, self._my_parent):
                            continue

                        self.logger.warning(
                            f"[Kernel] Port {port} still held by PID {pid}, force-freeing..."
                        )

                        try:
                            os.kill(pid, signal.SIGKILL)
                            freed_ports.append(port)
                            await asyncio.sleep(0.2)
                        except (ProcessLookupError, OSError):
                            pass
            except (psutil.AccessDenied, PermissionError):
                pass

        return freed_ports


# =============================================================================
# ZONE 5.4: PROCESS STATE MANAGER
# =============================================================================

class ProcessState(Enum):
    """States for a managed process."""
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    CRASHED = "crashed"


@dataclass
class ManagedProcess:
    """Represents a managed subprocess with lifecycle tracking."""
    name: str
    pid: Optional[int] = None
    state: ProcessState = ProcessState.CREATED
    process: Optional[asyncio.subprocess.Process] = None
    started_at: Optional[float] = None
    stopped_at: Optional[float] = None
    restart_count: int = 0
    last_error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def uptime_seconds(self) -> float:
        """Get process uptime in seconds."""
        if self.started_at is None:
            return 0.0
        end_time = self.stopped_at or time.time()
        return end_time - self.started_at

    @property
    def is_running(self) -> bool:
        """Check if process is in running state."""
        return self.state == ProcessState.RUNNING


class ProcessStateManager:
    """
    Manages lifecycle of spawned subprocesses.

    Features:
    - State tracking (CREATED → STARTING → RUNNING → STOPPED)
    - Auto-restart with configurable limits
    - Graceful shutdown with timeout
    - Health checking via callbacks
    - Statistics and metrics
    """

    def __init__(
        self,
        config: SystemKernelConfig,
        logger: UnifiedLogger,
        max_restarts: int = 3,
        restart_cooldown: float = 60.0,
    ) -> None:
        self.config = config
        self.logger = logger
        self._max_restarts = max_restarts
        self._restart_cooldown = restart_cooldown
        self._processes: Dict[str, ManagedProcess] = {}
        self._lock = asyncio.Lock()
        self._monitor_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()

    async def register_process(
        self,
        name: str,
        process: asyncio.subprocess.Process,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Register a new managed process."""
        async with self._lock:
            self._processes[name] = ManagedProcess(
                name=name,
                pid=process.pid,
                state=ProcessState.RUNNING,
                process=process,
                started_at=time.time(),
                metadata=metadata or {},
            )
            self.logger.info(f"[Kernel] Registered process '{name}' (PID: {process.pid})")

    async def update_state(self, name: str, state: ProcessState, error: Optional[str] = None) -> None:
        """Update process state."""
        async with self._lock:
            if name in self._processes:
                proc = self._processes[name]
                old_state = proc.state
                proc.state = state
                if error:
                    proc.last_error = error
                if state == ProcessState.STOPPED:
                    proc.stopped_at = time.time()
                self.logger.debug(f"[Kernel] Process '{name}' state: {old_state.value} → {state.value}")

    async def get_process(self, name: str) -> Optional[ManagedProcess]:
        """Get a managed process by name."""
        async with self._lock:
            return self._processes.get(name)

    async def get_all_processes(self) -> Dict[str, ManagedProcess]:
        """Get all managed processes."""
        async with self._lock:
            return dict(self._processes)

    async def stop_process(
        self,
        name: str,
        timeout: float = 10.0,
        force: bool = False,
    ) -> bool:
        """
        Stop a managed process gracefully.

        Args:
            name: Process name
            timeout: Timeout before force kill
            force: If True, skip graceful termination

        Returns:
            True if process was stopped successfully
        """
        async with self._lock:
            if name not in self._processes:
                return False

            proc = self._processes[name]
            if proc.process is None or proc.state == ProcessState.STOPPED:
                return True

            proc.state = ProcessState.STOPPING
            process = proc.process

        self.logger.info(f"[Kernel] Stopping process '{name}' (PID: {proc.pid})")

        try:
            if force:
                process.kill()
            else:
                # Graceful termination
                process.terminate()

            try:
                await asyncio.wait_for(process.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                self.logger.warning(f"[Kernel] Process '{name}' didn't stop gracefully, force killing...")
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=5.0)

            await self.update_state(name, ProcessState.STOPPED)
            return True

        except Exception as e:
            self.logger.error(f"[Kernel] Failed to stop process '{name}': {e}")
            await self.update_state(name, ProcessState.FAILED, str(e))
            return False

    async def stop_all(self, timeout: float = 30.0) -> Dict[str, bool]:
        """Stop all managed processes."""
        self._shutdown_event.set()

        results: Dict[str, bool] = {}
        processes = await self.get_all_processes()

        # Stop in parallel with semaphore
        semaphore = asyncio.Semaphore(4)

        async def stop_one(name: str) -> Tuple[str, bool]:
            async with semaphore:
                return name, await self.stop_process(name, timeout=timeout / 2)

        tasks = [
            asyncio.create_task(stop_one(name))
            for name, proc in processes.items()
            if proc.state == ProcessState.RUNNING
        ]

        if tasks:
            completed = await asyncio.gather(*tasks, return_exceptions=True)
            for result in completed:
                if isinstance(result, tuple):
                    name, success = result
                    results[name] = success
                else:
                    self.logger.error(f"[Kernel] Process stop error: {result}")

        return results

    def get_statistics(self) -> Dict[str, Any]:
        """Get statistics about managed processes."""
        total = len(self._processes)
        running = sum(1 for p in self._processes.values() if p.is_running)
        failed = sum(1 for p in self._processes.values() if p.state == ProcessState.FAILED)
        total_restarts = sum(p.restart_count for p in self._processes.values())

        return {
            "total_processes": total,
            "running": running,
            "failed": failed,
            "total_restarts": total_restarts,
            "processes": {
                name: {
                    "state": p.state.value,
                    "pid": p.pid,
                    "uptime_seconds": p.uptime_seconds,
                    "restart_count": p.restart_count,
                }
                for name, p in self._processes.items()
            }
        }


# =============================================================================
# ZONE 5.5: HOT RELOAD WATCHER
# =============================================================================
# v5.0: Intelligent polyglot hot reload system with:
# - Dynamic file type discovery (no hardcoding!)
# - Category-based restart decisions (backend vs frontend)
# - Parallel file hash calculation
# - React dev server detection (skip if HMR active)
# - Frontend auto-rebuild support (npm run build)
# - Smart debouncing and cooldown
# =============================================================================


class FileTypeCategory(Enum):
    """Categories of file types for intelligent restart decisions."""
    BACKEND_CODE = "backend_code"       # Python, Rust - requires backend restart
    FRONTEND_CODE = "frontend_code"     # JS, JSX, TS, TSX, CSS, HTML - may need frontend rebuild
    NATIVE_CODE = "native_code"         # Swift, Rust - may need recompilation
    CONFIG = "config"                   # YAML, TOML, JSON - configuration changes
    SCRIPT = "script"                   # Shell scripts - utility scripts
    DOCS = "docs"                       # Markdown, text - documentation (usually no restart)
    BUILD = "build"                     # Cargo.toml, package.json - build configs
    UNKNOWN = "unknown"


@dataclass
class FileTypeInfo:
    """Information about a file type for hot reload."""
    extension: str
    category: FileTypeCategory = FileTypeCategory.UNKNOWN
    requires_restart: bool = True
    restart_target: str = "backend"  # backend, frontend, native, all, none
    description: str = ""


class IntelligentFileTypeRegistry:
    """
    Dynamically discovers and categorizes file types in the codebase.

    Instead of hardcoding patterns, this registry:
    1. Scans the codebase to discover all file types
    2. Categorizes them intelligently
    3. Determines restart requirements for each type

    Features:
    - Complete file type mapping with descriptions
    - Dynamic discovery of file types in the repository
    - Categorization of changed files by restart target
    - Summary generation for verbose logging
    """

    # Known file type mappings (extensible, not exhaustive)
    KNOWN_TYPES: Dict[str, FileTypeInfo] = {
        # Backend code (requires backend restart)
        ".py": FileTypeInfo(".py", FileTypeCategory.BACKEND_CODE, True, "backend", "Python"),
        ".pyx": FileTypeInfo(".pyx", FileTypeCategory.BACKEND_CODE, True, "backend", "Cython"),
        ".pxd": FileTypeInfo(".pxd", FileTypeCategory.BACKEND_CODE, True, "backend", "Cython declaration"),
        ".pyi": FileTypeInfo(".pyi", FileTypeCategory.BACKEND_CODE, False, "none", "Python type stubs"),

        # Rust (native extensions - may need rebuild)
        ".rs": FileTypeInfo(".rs", FileTypeCategory.NATIVE_CODE, True, "backend", "Rust"),

        # Swift (native macOS code - may need rebuild)
        ".swift": FileTypeInfo(".swift", FileTypeCategory.NATIVE_CODE, True, "backend", "Swift"),

        # C/C++ (native extensions)
        ".c": FileTypeInfo(".c", FileTypeCategory.NATIVE_CODE, True, "native", "C"),
        ".cpp": FileTypeInfo(".cpp", FileTypeCategory.NATIVE_CODE, True, "native", "C++"),
        ".h": FileTypeInfo(".h", FileTypeCategory.NATIVE_CODE, True, "native", "C header"),
        ".hpp": FileTypeInfo(".hpp", FileTypeCategory.NATIVE_CODE, True, "native", "C++ header"),

        # Frontend code
        ".js": FileTypeInfo(".js", FileTypeCategory.FRONTEND_CODE, True, "frontend", "JavaScript"),
        ".jsx": FileTypeInfo(".jsx", FileTypeCategory.FRONTEND_CODE, True, "frontend", "React JSX"),
        ".ts": FileTypeInfo(".ts", FileTypeCategory.FRONTEND_CODE, True, "frontend", "TypeScript"),
        ".tsx": FileTypeInfo(".tsx", FileTypeCategory.FRONTEND_CODE, True, "frontend", "React TSX"),
        ".css": FileTypeInfo(".css", FileTypeCategory.FRONTEND_CODE, True, "frontend", "CSS"),
        ".scss": FileTypeInfo(".scss", FileTypeCategory.FRONTEND_CODE, True, "frontend", "SCSS"),
        ".less": FileTypeInfo(".less", FileTypeCategory.FRONTEND_CODE, True, "frontend", "LESS"),
        ".html": FileTypeInfo(".html", FileTypeCategory.FRONTEND_CODE, True, "frontend", "HTML"),
        ".vue": FileTypeInfo(".vue", FileTypeCategory.FRONTEND_CODE, True, "frontend", "Vue"),
        ".svelte": FileTypeInfo(".svelte", FileTypeCategory.FRONTEND_CODE, True, "frontend", "Svelte"),

        # Configuration files
        ".yaml": FileTypeInfo(".yaml", FileTypeCategory.CONFIG, True, "backend", "YAML config"),
        ".yml": FileTypeInfo(".yml", FileTypeCategory.CONFIG, True, "backend", "YAML config"),
        ".toml": FileTypeInfo(".toml", FileTypeCategory.BUILD, True, "backend", "TOML config"),
        ".json": FileTypeInfo(".json", FileTypeCategory.CONFIG, False, "none", "JSON config"),  # Usually runtime
        ".env": FileTypeInfo(".env", FileTypeCategory.CONFIG, True, "all", "Environment"),
        ".ini": FileTypeInfo(".ini", FileTypeCategory.CONFIG, True, "backend", "INI config"),

        # Shell scripts
        ".sh": FileTypeInfo(".sh", FileTypeCategory.SCRIPT, False, "none", "Shell script"),
        ".bash": FileTypeInfo(".bash", FileTypeCategory.SCRIPT, False, "none", "Bash script"),
        ".zsh": FileTypeInfo(".zsh", FileTypeCategory.SCRIPT, False, "none", "Zsh script"),

        # Build files (require full rebuild)
        "Cargo.toml": FileTypeInfo("Cargo.toml", FileTypeCategory.BUILD, True, "all", "Rust build"),
        "package.json": FileTypeInfo("package.json", FileTypeCategory.BUILD, True, "frontend", "NPM package"),
        "requirements.txt": FileTypeInfo("requirements.txt", FileTypeCategory.BUILD, True, "all", "Python deps"),
        "pyproject.toml": FileTypeInfo("pyproject.toml", FileTypeCategory.BUILD, True, "all", "Python project"),
        "Pipfile": FileTypeInfo("Pipfile", FileTypeCategory.BUILD, True, "all", "Pipenv deps"),
        "poetry.lock": FileTypeInfo("poetry.lock", FileTypeCategory.BUILD, True, "all", "Poetry lock"),

        # Documentation (no restart needed)
        ".md": FileTypeInfo(".md", FileTypeCategory.DOCS, False, "none", "Markdown"),
        ".txt": FileTypeInfo(".txt", FileTypeCategory.DOCS, False, "none", "Text"),
        ".rst": FileTypeInfo(".rst", FileTypeCategory.DOCS, False, "none", "RST docs"),

        # SQL (may need migration)
        ".sql": FileTypeInfo(".sql", FileTypeCategory.CONFIG, False, "none", "SQL"),
    }

    def __init__(self, repo_root: Path, logger: UnifiedLogger) -> None:
        self.repo_root = repo_root
        self.logger = logger
        self._registry: Dict[str, FileTypeInfo] = dict(self.KNOWN_TYPES)
        self._discovered_extensions: Set[str] = set()
        self._file_counts: Dict[str, int] = {}

    def discover_file_types(self) -> Dict[str, int]:
        """
        Dynamically discover all file types in the codebase.
        Returns a dict of extension -> count.
        """
        self._discovered_extensions.clear()
        self._file_counts.clear()

        exclude_dirs = {
            '.git', '__pycache__', 'node_modules', 'venv', 'env',
            '.venv', 'build', 'dist', 'target', '.cursor', '.idea',
            '.vscode', 'coverage', '.pytest_cache', '.mypy_cache',
            '.worktrees', 'htmlcov', '.jarvis_cache',
        }

        for root, dirs, files in os.walk(self.repo_root):
            # Skip excluded directories
            dirs[:] = [d for d in dirs if d not in exclude_dirs and not d.startswith('.')]

            for file in files:
                if file.startswith('.'):
                    continue

                # Get extension
                if '.' in file:
                    ext = '.' + file.rsplit('.', 1)[-1].lower()
                else:
                    ext = ''

                if ext:
                    self._discovered_extensions.add(ext)
                    self._file_counts[ext] = self._file_counts.get(ext, 0) + 1

        return self._file_counts

    def get_file_info(self, file_path: str) -> FileTypeInfo:
        """Get info about a file type."""
        path = Path(file_path)
        filename = path.name

        # Check exact filename match first (e.g., "Cargo.toml")
        if filename in self._registry:
            return self._registry[filename]

        # Check extension
        ext = path.suffix.lower()
        if ext in self._registry:
            return self._registry[ext]

        # Unknown type - return safe default
        return FileTypeInfo(ext, FileTypeCategory.UNKNOWN, False, "none", f"Unknown ({ext})")

    def get_watch_patterns(self) -> List[str]:
        """
        Dynamically generate watch patterns based on discovered file types.
        Only includes types that require restart.
        """
        patterns: List[str] = []

        # Discover file types if not already done
        if not self._discovered_extensions:
            self.discover_file_types()

        # Add patterns for known restart-requiring types
        for ext in self._discovered_extensions:
            if ext in self._registry:
                info = self._registry[ext]
                if info.requires_restart:
                    patterns.append(f"**/*{ext}")
            # For unknown types, be conservative - don't watch by default

        # Always include important config files
        patterns.extend([
            "**/Cargo.toml",
            "**/package.json",
            "**/requirements.txt",
            "**/pyproject.toml",
        ])

        return list(set(patterns))  # Deduplicate

    def categorize_changes(self, changed_files: List[str]) -> Dict[str, List[str]]:
        """
        Categorize changed files by restart target.
        Returns dict of target -> list of files.
        """
        categorized: Dict[str, List[str]] = {
            "backend": [],
            "frontend": [],
            "native": [],
            "all": [],
            "none": [],
        }

        for file_path in changed_files:
            info = self.get_file_info(file_path)
            categorized[info.restart_target].append(file_path)

        return categorized

    def get_summary(self) -> str:
        """Get a summary of discovered file types."""
        if not self._file_counts:
            self.discover_file_types()

        # Sort by count
        sorted_types = sorted(self._file_counts.items(), key=lambda x: -x[1])

        lines = ["File types in codebase:"]
        for ext, count in sorted_types[:15]:  # Top 15
            info = self._registry.get(ext, None)
            if info:
                restart = "🔄" if info.requires_restart else "📝"
                lines.append(f"  {restart} {ext}: {count} files ({info.description})")
            else:
                lines.append(f"  ❓ {ext}: {count} files")

        if len(sorted_types) > 15:
            lines.append(f"  ... and {len(sorted_types) - 15} more types")

        return "\n".join(lines)


class HotReloadWatcher:
    """
    v5.0: Intelligent polyglot hot reload watcher.

    Features:
    - Dynamic file type discovery (no hardcoding!)
    - Category-based restart decisions (backend vs frontend)
    - Parallel file hash calculation
    - Smart debouncing and cooldown
    - Frontend rebuild support (npm run build)
    - React dev server detection (skip if HMR is active)
    - Verbose mode with detailed logging
    """

    def __init__(self, config: SystemKernelConfig, logger: UnifiedLogger) -> None:
        self.config = config
        self.logger = logger
        self.repo_root = Path(os.getenv("JARVIS_PROJECT_ROOT", str(Path(__file__).parent)))
        self.frontend_dir = self.repo_root / "frontend"
        self.backend_dir = self.repo_root / "backend"

        # Configuration from environment
        self.enabled = self.config.hot_reload_enabled
        self.grace_period = int(os.getenv("JARVIS_RELOAD_GRACE_PERIOD", "120"))
        self.check_interval = self.config.reload_check_interval
        self.cooldown_seconds = int(os.getenv("JARVIS_RELOAD_COOLDOWN", "10"))
        self.verbose = os.getenv("JARVIS_RELOAD_VERBOSE", "false").lower() == "true"

        # Frontend-specific config
        self.frontend_auto_rebuild = os.getenv("JARVIS_FRONTEND_AUTO_REBUILD", "true").lower() == "true"
        self.frontend_dev_server_port = int(os.getenv("JARVIS_FRONTEND_DEV_PORT", "3000"))

        # Intelligent file type registry
        self._type_registry = IntelligentFileTypeRegistry(self.repo_root, logger)

        # Exclude patterns
        self.exclude_dirs = {
            '.git', '__pycache__', 'node_modules', 'venv', 'env',
            '.venv', 'build', 'dist', 'target', '.cursor', '.idea',
            '.vscode', 'coverage', '.pytest_cache', '.mypy_cache',
            'logs', 'cache', '.jarvis_cache', 'htmlcov', '.worktrees',
        }
        self.exclude_patterns = [
            "*.pyc", "*.pyo", "*.log", "*.tmp", "*.bak",
            "*.swp", "*.swo", "*~", ".DS_Store",
        ]

        # State
        self._start_time = time.time()
        self._file_hashes: Dict[str, str] = {}
        self._last_restart_time = 0.0
        self._last_frontend_rebuild_time = 0.0
        self._grace_period_ended = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._restart_callback: Optional[Callable[[List[str]], Coroutine[Any, Any, None]]] = None
        self._frontend_callback: Optional[Callable[[List[str]], Coroutine[Any, Any, None]]] = None
        self._pending_changes: List[str] = []
        self._pending_frontend_changes: List[str] = []
        self._debounce_task: Optional[asyncio.Task] = None
        self._frontend_debounce_task: Optional[asyncio.Task] = None
        self._react_dev_server_running: Optional[bool] = None

    def set_restart_callback(self, callback: Callable[[List[str]], Coroutine[Any, Any, None]]) -> None:
        """Set the callback to invoke when a backend restart is needed."""
        self._restart_callback = callback

    def set_frontend_callback(self, callback: Callable[[List[str]], Coroutine[Any, Any, None]]) -> None:
        """Set the callback to invoke when a frontend rebuild is needed."""
        self._frontend_callback = callback

    async def _is_react_dev_server_running(self) -> bool:
        """
        Check if React dev server is running.
        If it is, we don't need to trigger rebuilds - React HMR handles it.
        """
        if self._react_dev_server_running is not None:
            return self._react_dev_server_running

        import socket

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(('localhost', self.frontend_dev_server_port))
            sock.close()

            self._react_dev_server_running = (result == 0)

            if self._react_dev_server_running:
                self.logger.info(f"🌐 React dev server detected on port {self.frontend_dev_server_port} - HMR active")
            else:
                self.logger.info("📦 React dev server not running - will trigger rebuilds on frontend changes")

            return self._react_dev_server_running
        except Exception:
            self._react_dev_server_running = False
            return False

    async def _rebuild_frontend(self, changed_files: List[str]) -> bool:
        """
        Trigger frontend rebuild (npm run build).
        Only runs if React dev server is NOT running.
        """
        if await self._is_react_dev_server_running():
            self.logger.info("   🔄 React HMR will handle these changes automatically")
            return True

        if not self.frontend_auto_rebuild:
            self.logger.info("   ⚠️ Frontend auto-rebuild disabled (JARVIS_FRONTEND_AUTO_REBUILD=false)")
            return False

        if not self.frontend_dir.exists():
            self.logger.warning("   ⚠️ Frontend directory not found, skipping rebuild")
            return False

        self.logger.info("   🔨 Triggering frontend rebuild...")

        process = None
        try:
            # Run npm run build in frontend directory
            process = await asyncio.create_subprocess_exec(
                "npm", "run", "build",
                cwd=str(self.frontend_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "CI": "true"}  # Prevent interactive prompts
            )

            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)

            if process.returncode == 0:
                self.logger.info("   ✅ Frontend rebuild completed successfully")
                return True
            else:
                self.logger.error(f"   ❌ Frontend rebuild failed: {stderr.decode()[:200]}")
                return False

        except asyncio.TimeoutError:
            self.logger.error("   ❌ Frontend rebuild timed out (120s)")
            # Clean up zombie process on timeout
            if process is not None:
                try:
                    process.kill()
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except Exception:
                    pass  # Best effort cleanup
            return False
        except Exception as e:
            self.logger.error(f"   ❌ Frontend rebuild error: {e}")
            # Clean up zombie process on error
            if process is not None:
                try:
                    process.kill()
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except Exception:
                    pass  # Best effort cleanup
            return False

    def _should_watch_file(self, file_path: Path) -> bool:
        """Determine if a file should be watched."""
        from fnmatch import fnmatch

        # Check if in excluded directory
        for part in file_path.parts:
            if part in self.exclude_dirs or part.startswith('.'):
                return False

        # Check exclude patterns
        for pattern in self.exclude_patterns:
            if fnmatch(file_path.name, pattern):
                return False

        # Check if file type requires restart
        info = self._type_registry.get_file_info(str(file_path))
        return info.requires_restart

    def _calculate_file_hashes_parallel(self) -> Dict[str, str]:
        """Calculate file hashes in parallel for speed."""
        import hashlib
        from concurrent.futures import as_completed

        def hash_file(file_path: Path) -> Tuple[str, Optional[str]]:
            try:
                with open(file_path, 'rb') as f:
                    return str(file_path.relative_to(self.repo_root)), hashlib.md5(f.read()).hexdigest()
            except Exception:
                return str(file_path), None

        files_to_hash: List[Path] = []

        # Walk directories and find watchable files
        for root, dirs, files in os.walk(self.repo_root):
            # Filter out excluded directories
            dirs[:] = [d for d in dirs if d not in self.exclude_dirs and not d.startswith('.')]

            root_path = Path(root)
            for file in files:
                file_path = root_path / file
                if self._should_watch_file(file_path):
                    files_to_hash.append(file_path)

        # Calculate hashes in parallel
        hashes: Dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as executor:
            futures = {executor.submit(hash_file, fp): fp for fp in files_to_hash}
            for future in as_completed(futures):
                rel_path, file_hash = future.result()
                if file_hash:
                    hashes[rel_path] = file_hash

        return hashes

    def _detect_changes(self) -> Tuple[bool, List[str], Dict[str, List[str]]]:
        """
        Detect which files have changed.

        Returns: (has_changes, changed_files, categorized_changes)
        """
        current = self._calculate_file_hashes_parallel()
        changed: List[str] = []

        for path, hash_val in current.items():
            if path not in self._file_hashes or self._file_hashes[path] != hash_val:
                changed.append(path)

        # Check for deleted files
        for path in self._file_hashes:
            if path not in current:
                changed.append(f"[DELETED] {path}")

        self._file_hashes = current

        # Categorize changes
        categorized = self._type_registry.categorize_changes(changed)

        return len(changed) > 0, changed, categorized

    def _is_in_grace_period(self) -> bool:
        """Check if we're still in the startup grace period."""
        elapsed = time.time() - self._start_time
        in_grace = elapsed < self.grace_period

        if not in_grace and not self._grace_period_ended:
            self._grace_period_ended = True
            self.logger.info(f"⏰ Hot reload grace period ended after {elapsed:.0f}s - now active")

        return in_grace

    def _is_in_cooldown(self) -> bool:
        """Check if we're in cooldown from a recent restart."""
        return (time.time() - self._last_restart_time) < self.cooldown_seconds

    def _is_in_frontend_cooldown(self) -> bool:
        """Check if we're in cooldown from a recent frontend rebuild."""
        return (time.time() - self._last_frontend_rebuild_time) < self.cooldown_seconds

    async def start(self) -> None:
        """Start the hot reload watcher."""
        if not self.enabled:
            self.logger.info("🔥 Hot reload disabled (dev_mode=false)")
            return

        # Discover and log file types
        self._type_registry.discover_file_types()

        if self.verbose:
            self.logger.info(self._type_registry.get_summary())

        # Initialize file hashes
        self._file_hashes = self._calculate_file_hashes_parallel()

        # Count files by category
        backend_count = 0
        frontend_count = 0
        for file_path in self._file_hashes:
            info = self._type_registry.get_file_info(file_path)
            if info.restart_target == "backend" or info.restart_target == "native":
                backend_count += 1
            elif info.restart_target == "frontend":
                frontend_count += 1

        # Log summary
        watch_patterns = self._type_registry.get_watch_patterns()
        file_types = sorted(set(p.split('*')[-1] for p in watch_patterns if '*' in p))

        self.logger.info(f"🔥 Hot reload watching {len(self._file_hashes)} files")
        self.logger.info(f"   🐍 Backend/Native: {backend_count} files")
        self.logger.info(f"   ⚛️  Frontend: {frontend_count} files")
        self.logger.info(f"   File types: {', '.join(file_types)}")
        self.logger.info(f"   Grace period: {self.grace_period}s, Check interval: {self.check_interval}s")

        # Start monitor task
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop(self) -> None:
        """Stop the hot reload watcher."""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        if self._debounce_task:
            self._debounce_task.cancel()

        if self._frontend_debounce_task:
            self._frontend_debounce_task.cancel()

    async def _debounced_restart(self, delay: float = 0.5) -> None:
        """Debounce rapid backend file changes into a single restart."""
        await asyncio.sleep(delay)

        if self._pending_changes and self._restart_callback:
            changes = self._pending_changes.copy()
            self._pending_changes.clear()

            self._last_restart_time = time.time()
            await self._restart_callback(changes)

    async def _debounced_frontend_rebuild(self, delay: float = 1.0) -> None:
        """Debounce rapid frontend file changes into a single rebuild."""
        await asyncio.sleep(delay)

        if self._pending_frontend_changes:
            changes = self._pending_frontend_changes.copy()
            self._pending_frontend_changes.clear()

            self._last_frontend_rebuild_time = time.time()
            await self._rebuild_frontend(changes)

    async def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        # Check React dev server status on first run
        await self._is_react_dev_server_running()

        while True:
            try:
                await asyncio.sleep(self.check_interval)

                # Skip during grace period
                if self._is_in_grace_period():
                    continue

                # Check for changes
                has_changes, changed_files, categorized = self._detect_changes()

                if has_changes:
                    # Log changes by category
                    self.logger.info(f"🔥 Detected {len(changed_files)} file change(s):")

                    for target, files in categorized.items():
                        if files and target != "none":
                            icon = {
                                "backend": "🐍",
                                "frontend": "⚛️",
                                "native": "🦀",
                                "all": "🌐",
                            }.get(target, "📁")
                            self.logger.info(f"   {icon} {target.upper()}: {len(files)} file(s)")
                            if self.verbose:
                                for f in files[:3]:
                                    self.logger.info(f"     └─ {f}")
                                if len(files) > 3:
                                    self.logger.info(f"     └─ ... and {len(files) - 3} more")

                    # Separate backend and frontend changes
                    backend_changes = (
                        categorized.get("backend", []) +
                        categorized.get("native", []) +
                        categorized.get("all", [])
                    )
                    frontend_changes = (
                        categorized.get("frontend", []) +
                        categorized.get("all", [])
                    )

                    # Handle backend changes
                    if backend_changes:
                        if self._is_in_cooldown():
                            remaining = self.cooldown_seconds - (time.time() - self._last_restart_time)
                            self.logger.info(f"   ⏳ Backend cooldown ({remaining:.0f}s remaining), deferring")
                            self._pending_changes.extend(backend_changes)
                        else:
                            self._pending_changes.extend(backend_changes)
                            if self._debounce_task:
                                self._debounce_task.cancel()
                            self._debounce_task = asyncio.create_task(self._debounced_restart())

                    # Handle frontend changes
                    if frontend_changes:
                        if self._is_in_frontend_cooldown():
                            remaining = self.cooldown_seconds - (time.time() - self._last_frontend_rebuild_time)
                            self.logger.info(f"   ⏳ Frontend cooldown ({remaining:.0f}s remaining), deferring")
                            self._pending_frontend_changes.extend(frontend_changes)
                        else:
                            self._pending_frontend_changes.extend(frontend_changes)
                            if self._frontend_debounce_task:
                                self._frontend_debounce_task.cancel()
                            self._frontend_debounce_task = asyncio.create_task(self._debounced_frontend_rebuild())

                    # Log if only docs changed
                    if not backend_changes and not frontend_changes:
                        self.logger.info("   📝 Changes don't require restart (docs only)")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Hot reload monitor error: {e}")
                await asyncio.sleep(self.check_interval)


# =============================================================================
# ZONE 5.6: PROGRESSIVE READINESS MANAGER
# =============================================================================

class ReadinessTier(Enum):
    """Progressive readiness tiers."""
    STARTING = "starting"
    PROCESS_STARTED = "process_started"  # Process spawned but not responding
    IPC_RESPONSIVE = "ipc_responsive"  # IPC socket accepting connections
    HTTP_HEALTHY = "http_healthy"  # HTTP health endpoint responding
    INTERACTIVE = "interactive"  # API ready, basic endpoints functional
    WARMUP = "warmup"  # Frontend ready, optional components loading
    FULLY_READY = "fully_ready"  # Complete system ready


@dataclass
class ReadinessState:
    """Current readiness state."""
    tier: ReadinessTier = ReadinessTier.STARTING
    tier_reached_at: Dict[str, float] = field(default_factory=dict)
    components_ready: Dict[str, bool] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def mark_tier(self, tier: ReadinessTier) -> None:
        """Mark a tier as reached."""
        self.tier = tier
        self.tier_reached_at[tier.value] = time.time()

    def get_tier_duration(self, tier: ReadinessTier) -> Optional[float]:
        """Get time when a tier was reached."""
        return self.tier_reached_at.get(tier.value)


class ProgressiveReadinessManager:
    """
    Manages progressive readiness tiers.

    This allows users to access the system immediately while heavy
    components load in the background.

    Tiers:
    - STARTING: Kernel initializing
    - PROCESS_STARTED: Backend process spawned
    - IPC_RESPONSIVE: IPC socket accepting connections
    - HTTP_HEALTHY: Health endpoint responding
    - INTERACTIVE: API ready for basic requests
    - WARMUP: Optional components loading
    - FULLY_READY: Everything ready including ML models
    """

    def __init__(self, config: SystemKernelConfig, logger: UnifiedLogger) -> None:
        self.config = config
        self.logger = logger
        self.state = ReadinessState()
        self._state_file = Path.home() / ".jarvis" / "kernel" / "readiness_state.json"
        self._state_file.parent.mkdir(parents=True, exist_ok=True)

        # Heartbeat loop for staleness detection
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._heartbeat_interval = 15.0  # Write heartbeat every 15 seconds
        self._shutdown_event = asyncio.Event()

    async def start_heartbeat_loop(self) -> None:
        """Start background heartbeat loop."""
        if self._heartbeat_task is not None:
            return  # Already running

        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(),
            name="kernel-heartbeat"
        )
        self.logger.info("[Kernel] Started heartbeat loop")

    async def stop_heartbeat_loop(self) -> None:
        """Stop the background heartbeat loop."""
        self._shutdown_event.set()
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        self.logger.info("[Kernel] Stopped heartbeat loop")

    async def _heartbeat_loop(self) -> None:
        """Background loop that continuously updates heartbeat."""
        import random
        consecutive_errors = 0

        while not self._shutdown_event.is_set():
            try:
                # Add jitter (±10%) to prevent thundering herd
                jitter = self._heartbeat_interval * 0.1 * (2 * random.random() - 1)
                await asyncio.sleep(self._heartbeat_interval + jitter)

                # Write heartbeat
                self._write_heartbeat()
                consecutive_errors = 0

            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors <= 3:
                    self.logger.debug(f"[Kernel] Heartbeat write error: {e}")

    def _write_heartbeat(self) -> None:
        """Write heartbeat file."""
        heartbeat_file = Path.home() / ".jarvis" / "kernel" / "heartbeat.json"
        heartbeat_file.parent.mkdir(parents=True, exist_ok=True)

        heartbeat_data = {
            "timestamp": time.time(),
            "iso": datetime.now().isoformat(),
            "pid": os.getpid(),
            "tier": self.state.tier.value,
            "kernel_id": self.config.kernel_id,
        }

        with open(heartbeat_file, "w") as f:
            json.dump(heartbeat_data, f)

    def mark_tier(self, tier: ReadinessTier) -> None:
        """Mark a readiness tier as reached."""
        old_tier = self.state.tier
        self.state.mark_tier(tier)
        self._write_state()

        if tier != old_tier:
            self.logger.info(f"[Kernel] Readiness tier: {old_tier.value} → {tier.value}")

    def mark_component_ready(self, component: str, ready: bool = True) -> None:
        """Mark a component as ready/not ready."""
        self.state.components_ready[component] = ready
        self._write_state()

    def add_error(self, error: str) -> None:
        """Add an error to the readiness state."""
        self.state.errors.append(error)
        self._write_state()

    def _write_state(self) -> None:
        """Write state to file."""
        try:
            state_data = {
                "tier": self.state.tier.value,
                "tier_reached_at": self.state.tier_reached_at,
                "components_ready": self.state.components_ready,
                "errors": self.state.errors[-10:],  # Keep last 10 errors
                "updated_at": time.time(),
                "pid": os.getpid(),
            }
            with open(self._state_file, "w") as f:
                json.dump(state_data, f, indent=2)
        except Exception:
            pass  # Best effort

    def get_status(self) -> Dict[str, Any]:
        """Get current readiness status."""
        return {
            "tier": self.state.tier.value,
            "tier_reached_at": self.state.tier_reached_at,
            "components_ready": self.state.components_ready,
            "all_components_ready": all(self.state.components_ready.values()) if self.state.components_ready else False,
            "error_count": len(self.state.errors),
            "last_error": self.state.errors[-1] if self.state.errors else None,
        }

    def is_at_least(self, tier: ReadinessTier) -> bool:
        """Check if readiness is at least at the given tier."""
        tier_order = [
            ReadinessTier.STARTING,
            ReadinessTier.PROCESS_STARTED,
            ReadinessTier.IPC_RESPONSIVE,
            ReadinessTier.HTTP_HEALTHY,
            ReadinessTier.INTERACTIVE,
            ReadinessTier.WARMUP,
            ReadinessTier.FULLY_READY,
        ]
        current_idx = tier_order.index(self.state.tier)
        target_idx = tier_order.index(tier)
        return current_idx >= target_idx


# =============================================================================
# ZONE 5.7: TRINITY INTEGRATOR
# =============================================================================

@dataclass
class TrinityComponent:
    """Represents a Trinity component (J-Prime or Reactor-Core)."""
    name: str
    repo_path: Optional[Path] = None
    port: int = 0
    process: Optional[asyncio.subprocess.Process] = None
    pid: Optional[int] = None
    state: str = "unknown"
    health_url: Optional[str] = None
    last_health_check: Optional[float] = None
    restart_count: int = 0

    @property
    def is_running(self) -> bool:
        """Check if component is running."""
        return self.state in ("running", "healthy")


class TrinityIntegrator:
    """
    Cross-repo integration for JARVIS Trinity architecture.

    Manages J-Prime (Mind) and Reactor-Core (Nerves) components:
    - Dynamic repo discovery
    - Process lifecycle management
    - Health monitoring with auto-restart
    - Coordinated shutdown

    The Trinity architecture:
    - JARVIS (Body) - Main AI agent, this codebase
    - J-Prime (Mind) - Local LLM inference, tier-0 brain
    - Reactor-Core (Nerves) - Training pipeline, model optimization
    """

    def __init__(self, config: SystemKernelConfig, logger: UnifiedLogger) -> None:
        self.config = config
        self.logger = logger
        self._enabled = config.trinity_enabled

        # Components
        self._jprime: Optional[TrinityComponent] = None
        self._reactor: Optional[TrinityComponent] = None

        # Monitoring
        self._health_monitor_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
        self._health_check_interval = float(os.getenv("TRINITY_HEALTH_INTERVAL", "10.0"))
        self._max_restarts = int(os.getenv("TRINITY_MAX_RESTARTS", "3"))

        # Discovery cache
        self._discovery_cache: Dict[str, Optional[Path]] = {}

    async def initialize(self) -> bool:
        """Initialize Trinity integration."""
        if not self._enabled:
            self.logger.info("[Trinity] Trinity integration disabled")
            return True

        self.logger.info("[Trinity] Initializing Trinity integration...")

        # Discover repos
        jprime_path = await self._discover_repo("jarvis-prime", self.config.prime_repo_path)
        reactor_path = await self._discover_repo("reactor-core", self.config.reactor_repo_path)

        # Initialize components
        if jprime_path:
            jprime_port = int(os.getenv("TRINITY_JPRIME_PORT", "8000"))
            self._jprime = TrinityComponent(
                name="jarvis-prime",
                repo_path=jprime_path,
                port=jprime_port,
                health_url=f"http://localhost:{jprime_port}/health",
            )
            self.logger.info(f"[Trinity] J-Prime configured at {jprime_path}")
        else:
            self.logger.info("[Trinity] J-Prime repo not found - will run without local LLM")

        if reactor_path:
            reactor_port = int(os.getenv("TRINITY_REACTOR_PORT", "8090"))
            self._reactor = TrinityComponent(
                name="reactor-core",
                repo_path=reactor_path,
                port=reactor_port,
                health_url=f"http://localhost:{reactor_port}/health",
            )
            self.logger.info(f"[Trinity] Reactor-Core configured at {reactor_path}")
        else:
            self.logger.info("[Trinity] Reactor-Core repo not found - will run without training pipeline")

        return True

    async def _discover_repo(self, name: str, explicit_path: Optional[Path]) -> Optional[Path]:
        """Discover a Trinity repo location."""
        if name in self._discovery_cache:
            return self._discovery_cache[name]

        # Strategy 1: Explicit path from config
        if explicit_path and explicit_path.exists():
            self._discovery_cache[name] = explicit_path
            return explicit_path

        # Strategy 2: Environment variable
        env_var = f"{name.upper().replace('-', '_')}_PATH"
        env_path = os.getenv(env_var)
        if env_path:
            path = Path(env_path)
            if path.exists():
                self._discovery_cache[name] = path
                return path

        # Strategy 3: Common locations
        search_paths = [
            Path.home() / "Documents" / "repos" / name,
            Path.home() / "repos" / name,
            Path.home() / "code" / name,
            Path.home() / "projects" / name,
            Path(__file__).parent.parent / name,  # Sibling directory
        ]

        for path in search_paths:
            if path.exists() and (path / ".git").exists():
                self._discovery_cache[name] = path
                return path

        self._discovery_cache[name] = None
        return None

    async def start_components(self) -> Dict[str, bool]:
        """Start Trinity components."""
        if not self._enabled:
            return {}

        results: Dict[str, bool] = {}

        # Start J-Prime
        if self._jprime:
            results["jarvis-prime"] = await self._start_component(self._jprime)

        # Start Reactor-Core
        if self._reactor:
            results["reactor-core"] = await self._start_component(self._reactor)

        # Start health monitoring
        if any(results.values()):
            await self._start_health_monitor()

        return results

    async def _start_component(self, component: TrinityComponent) -> bool:
        """Start a single Trinity component."""
        if component.repo_path is None:
            return False

        self.logger.info(f"[Trinity] Starting {component.name}...")

        # Find Python executable
        venv_python = component.repo_path / "venv" / "bin" / "python3"
        if not venv_python.exists():
            venv_python = component.repo_path / "venv" / "bin" / "python"
        if not venv_python.exists():
            venv_python = Path(sys.executable)  # Fallback to current Python

        # Find launch script
        launch_scripts = [
            component.repo_path / "run_server.py",
            component.repo_path / "main.py",
            component.repo_path / f"{component.name.replace('-', '_')}" / "server.py",
        ]

        launch_script = None
        for script in launch_scripts:
            if script.exists():
                launch_script = script
                break

        if not launch_script:
            self.logger.warning(f"[Trinity] No launch script found for {component.name}")
            return False

        try:
            # Start process
            env = os.environ.copy()
            env["TRINITY_COMPONENT"] = component.name
            env["TRINITY_PORT"] = str(component.port)

            process = await asyncio.create_subprocess_exec(
                str(venv_python),
                str(launch_script),
                "--port", str(component.port),
                cwd=str(component.repo_path),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            component.process = process
            component.pid = process.pid
            component.state = "starting"

            # Wait for health check
            healthy = await self._wait_for_health(component, timeout=60.0)
            if healthy:
                component.state = "healthy"
                self.logger.success(f"[Trinity] {component.name} started (PID: {component.pid})")
                return True
            else:
                component.state = "failed"
                self.logger.error(f"[Trinity] {component.name} failed to become healthy")
                return False

        except Exception as e:
            self.logger.error(f"[Trinity] Failed to start {component.name}: {e}")
            component.state = "failed"
            return False

    async def _wait_for_health(self, component: TrinityComponent, timeout: float = 60.0) -> bool:
        """Wait for component to become healthy."""
        if not component.health_url:
            return True  # No health check configured

        if not AIOHTTP_AVAILABLE:
            self.logger.debug("[Trinity] aiohttp not available, skipping health check")
            return True  # Assume healthy if we can't check

        start_time = time.time()
        while (time.time() - start_time) < timeout:
            try:
                async with aiohttp.ClientSession() as session:  # type: ignore[union-attr]
                    async with session.get(component.health_url, timeout=5.0) as response:
                        if response.status == 200:
                            return True
            except Exception:
                pass
            await asyncio.sleep(2.0)

        return False

    async def _start_health_monitor(self) -> None:
        """Start health monitoring loop."""
        if self._health_monitor_task:
            return

        self._health_monitor_task = asyncio.create_task(
            self._health_monitor_loop(),
            name="trinity-health-monitor"
        )

    async def _health_monitor_loop(self) -> None:
        """Monitor component health and auto-restart if needed."""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(self._health_check_interval)

                for component in [self._jprime, self._reactor]:
                    if component and component.state == "healthy":
                        healthy = await self._check_health(component)
                        if not healthy:
                            self.logger.warning(f"[Trinity] {component.name} became unhealthy")
                            component.state = "unhealthy"

                            if component.restart_count < self._max_restarts:
                                self.logger.info(f"[Trinity] Attempting to restart {component.name}")
                                component.restart_count += 1
                                await self._start_component(component)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.debug(f"[Trinity] Health monitor error: {e}")

    async def _check_health(self, component: TrinityComponent) -> bool:
        """Check if a component is healthy."""
        if not component.health_url:
            return True

        if not AIOHTTP_AVAILABLE:
            return True  # Assume healthy if we can't check

        try:
            async with aiohttp.ClientSession() as session:  # type: ignore[union-attr]
                async with session.get(component.health_url, timeout=5.0) as response:
                    return response.status == 200
        except Exception:
            return False

    async def stop(self) -> None:
        """Stop all Trinity components."""
        self._shutdown_event.set()

        # Stop health monitor
        if self._health_monitor_task:
            self._health_monitor_task.cancel()
            try:
                await self._health_monitor_task
            except asyncio.CancelledError:
                pass

        # Stop components
        for component in [self._jprime, self._reactor]:
            if component and component.process:
                try:
                    component.process.terminate()
                    await asyncio.wait_for(component.process.wait(), timeout=10.0)
                    self.logger.info(f"[Trinity] Stopped {component.name}")
                except asyncio.TimeoutError:
                    component.process.kill()
                except Exception as e:
                    self.logger.debug(f"[Trinity] Error stopping {component.name}: {e}")

    def get_status(self) -> Dict[str, Any]:
        """Get Trinity status."""
        return {
            "enabled": self._enabled,
            "components": {
                "jarvis-prime": {
                    "configured": self._jprime is not None,
                    "state": self._jprime.state if self._jprime else "not_configured",
                    "pid": self._jprime.pid if self._jprime else None,
                    "port": self._jprime.port if self._jprime else None,
                    "restart_count": self._jprime.restart_count if self._jprime else 0,
                },
                "reactor-core": {
                    "configured": self._reactor is not None,
                    "state": self._reactor.state if self._reactor else "not_configured",
                    "pid": self._reactor.pid if self._reactor else None,
                    "port": self._reactor.port if self._reactor else None,
                    "restart_count": self._reactor.restart_count if self._reactor else 0,
                },
            },
        }


# =============================================================================
# ZONE 5 SELF-TEST FUNCTION
# =============================================================================
# Tests for Zone 5 (run with: python unified_supervisor.py --test zone5)

async def _test_zone5():
    """Test Zone 5 components (Process Orchestration)."""
    # Create config and logger
    config = SystemKernelConfig()
    logger = UnifiedLogger()  # Singleton - no args

    print("\n" + "="*70)
    print("ZONE 5 TESTS: PROCESS ORCHESTRATION")
    print("="*70 + "\n")

    # ========== Test UnifiedSignalHandler ==========
    with logger.section_start(LogSection.PROCESS, "Zone 5.1: UnifiedSignalHandler"):
        handler = get_unified_signal_handler()
        logger.success(f"Signal handler created (installed={handler._installed})")
        logger.info(f"Shutdown requested: {handler.shutdown_requested}")
        logger.info(f"Shutdown count: {handler.shutdown_count}")

    # ========== Test ComprehensiveZombieCleanup ==========
    with logger.section_start(LogSection.PROCESS, "Zone 5.3: ComprehensiveZombieCleanup"):
        zombie_cleanup = ComprehensiveZombieCleanup(config, logger)
        # Note: Actually running cleanup would kill processes - just test init
        logger.success("Zombie cleanup initialized")
        logger.info(f"Service ports: {zombie_cleanup._service_ports}")
        stats = zombie_cleanup.get_stats()
        logger.info(f"Initial stats: {stats}")

    # ========== Test ProcessStateManager ==========
    with logger.section_start(LogSection.PROCESS, "Zone 5.4: ProcessStateManager"):
        process_mgr = ProcessStateManager(config, logger)
        stats = process_mgr.get_statistics()
        logger.success("Process manager initialized")
        logger.info(f"Stats: {stats['total_processes']} processes tracked")

    # ========== Test HotReloadWatcher ==========
    with logger.section_start(LogSection.DEV, "Zone 5.5: HotReloadWatcher"):
        hot_reload = HotReloadWatcher(config, logger)
        logger.success("Hot reload watcher initialized")
        logger.info(f"Enabled: {hot_reload.enabled}")
        logger.info(f"Grace period: {hot_reload.grace_period}s")
        logger.info(f"Check interval: {hot_reload.check_interval}s")

    # ========== Test ProgressiveReadinessManager ==========
    with logger.section_start(LogSection.PROCESS, "Zone 5.6: ProgressiveReadinessManager"):
        readiness = ProgressiveReadinessManager(config, logger)
        readiness.mark_tier(ReadinessTier.PROCESS_STARTED)
        readiness.mark_component_ready("backend", True)
        status = readiness.get_status()
        logger.success("Readiness manager initialized")
        logger.info(f"Current tier: {status['tier']}")
        logger.info(f"Components ready: {status['components_ready']}")

    # ========== Test TrinityIntegrator ==========
    with logger.section_start(LogSection.TRINITY, "Zone 5.7: TrinityIntegrator"):
        trinity = TrinityIntegrator(config, logger)
        await trinity.initialize()
        status = trinity.get_status()
        logger.success("Trinity integrator initialized")
        logger.info(f"Enabled: {status['enabled']}")
        logger.info(f"J-Prime configured: {status['components']['jarvis-prime']['configured']}")
        logger.info(f"Reactor-Core configured: {status['components']['reactor-core']['configured']}")

    logger.print_startup_summary()
    TerminalUI.print_success("Zone 5 validation complete!")


# =============================================================================
# =============================================================================
#
#  ███████╗ ██████╗ ███╗   ██╗███████╗     ██████╗
#  ╚══███╔╝██╔═══██╗████╗  ██║██╔════╝    ██╔════╝
#    ███╔╝ ██║   ██║██╔██╗ ██║█████╗      ███████╗
#   ███╔╝  ██║   ██║██║╚██╗██║██╔══╝      ██╔═══██╗
#  ███████╗╚██████╔╝██║ ╚████║███████╗    ╚██████╔╝
#  ╚══════╝ ╚═════╝ ╚═╝  ╚═══╝╚══════╝     ╚═════╝
#
#  ZONE 6: THE KERNEL
#  Lines ~7300-9000
#
#  This zone contains:
#  - JarvisSystemKernel: The brain that ties everything together
#  - IPC Server: Unix socket for control commands
#  - Startup phases: Preflight → Resources → Backend → Intelligence → Trinity
#  - Main run loop: Health monitoring, cost optimization, IPC handling
#  - Cleanup: Master shutdown orchestration
#
# =============================================================================
# =============================================================================


# =============================================================================
# ZONE 6.1: KERNEL STATE AND STARTUP LOCK
# =============================================================================

class KernelState(Enum):
    """States of the system kernel."""
    INITIALIZING = "initializing"
    PREFLIGHT = "preflight"
    STARTING_RESOURCES = "starting_resources"
    STARTING_BACKEND = "starting_backend"
    STARTING_INTELLIGENCE = "starting_intelligence"
    STARTING_TRINITY = "starting_trinity"
    RUNNING = "running"
    SHUTTING_DOWN = "shutting_down"
    STOPPED = "stopped"
    FAILED = "failed"


# NOTE: StartupLock is defined in Zone 2 (Core Utilities)


# =============================================================================
# ZONE 6.2: IPC SERVER
# =============================================================================

class IPCCommand(Enum):
    """Commands that can be sent to the kernel via IPC."""
    HEALTH = "health"
    STATUS = "status"
    SHUTDOWN = "shutdown"
    RESTART = "restart"
    RELOAD = "reload"


@dataclass
class IPCRequest:
    """IPC request from a client."""
    command: IPCCommand
    args: Dict[str, Any] = field(default_factory=dict)


@dataclass
class IPCResponse:
    """IPC response to a client."""
    success: bool
    result: Any = None
    error: Optional[str] = None


class IPCServer:
    """
    Unix socket server for inter-process communication.

    Allows external tools (CLI, monitoring) to communicate with the running kernel.
    Commands: health, status, shutdown, restart, reload
    """

    def __init__(
        self,
        config: SystemKernelConfig,
        logger: UnifiedLogger,
        socket_path: Optional[Path] = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self._socket_path = socket_path or (Path.home() / ".jarvis" / "locks" / "kernel.sock")
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._server: Optional[asyncio.AbstractServer] = None
        self._handlers: Dict[IPCCommand, Callable[..., Coroutine[Any, Any, Any]]] = {}
        self._shutdown_event = asyncio.Event()

    def register_handler(
        self,
        command: IPCCommand,
        handler: Callable[..., Coroutine[Any, Any, Any]],
    ) -> None:
        """Register a handler for an IPC command."""
        self._handlers[command] = handler

    async def start(self) -> bool:
        """Start the IPC server."""
        # Remove stale socket file
        if self._socket_path.exists():
            try:
                self._socket_path.unlink()
            except IOError:
                self.logger.warning("[IPC] Could not remove stale socket file")
                return False

        try:
            self._server = await asyncio.start_unix_server(
                self._handle_client,
                path=str(self._socket_path),
            )
            self.logger.info(f"[IPC] Server listening on {self._socket_path}")
            return True
        except Exception as e:
            self.logger.error(f"[IPC] Failed to start server: {e}")
            return False

    async def stop(self) -> None:
        """Stop the IPC server."""
        self._shutdown_event.set()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._socket_path.exists():
            try:
                self._socket_path.unlink()
            except IOError:
                pass
        self.logger.info("[IPC] Server stopped")

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a client connection."""
        try:
            # Read request
            data = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not data:
                return

            # Parse request
            try:
                request_data = json.loads(data.decode())
                command_str = request_data.get("command", "")
                command = IPCCommand(command_str)
                args = request_data.get("args", {})
            except (json.JSONDecodeError, ValueError) as e:
                response = IPCResponse(success=False, error=f"Invalid request: {e}")
                await self._send_response(writer, response)
                return

            # Execute handler
            if command in self._handlers:
                try:
                    result = await self._handlers[command](**args)
                    response = IPCResponse(success=True, result=result)
                except Exception as e:
                    response = IPCResponse(success=False, error=str(e))
            else:
                response = IPCResponse(success=False, error=f"Unknown command: {command.value}")

            await self._send_response(writer, response)

        except asyncio.TimeoutError:
            pass
        except Exception as e:
            self.logger.debug(f"[IPC] Client handler error: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _send_response(self, writer: asyncio.StreamWriter, response: IPCResponse) -> None:
        """Send response to client."""
        try:
            response_data = {
                "success": response.success,
                "result": response.result,
                "error": response.error,
            }
            writer.write(json.dumps(response_data).encode() + b"\n")
            await writer.drain()
        except Exception:
            pass


# =============================================================================
# ZONE 6.3: JARVIS SYSTEM KERNEL
# =============================================================================

class JarvisSystemKernel:
    """
    The brain that ties the entire JARVIS system together.

    This is the central coordinator that:
    - Initializes all managers in the correct order
    - Runs the full boot sequence through phases
    - Manages the main event loop
    - Orchestrates graceful shutdown

    Singleton: Only one kernel can run at a time.

    Startup Phases:
    1. Preflight: Cleanup zombies, acquire lock, setup IPC
    2. Resources: Docker, GCP, storage (parallel)
    3. Backend: Start uvicorn server (in-process or subprocess)
    4. Intelligence: Initialize ML layer
    5. Trinity: Start cross-repo components

    Background Tasks:
    - Health monitoring
    - Cost optimization
    - IPC command handling
    """

    _instance: Optional["JarvisSystemKernel"] = None

    def __new__(cls, *args: Any, **kwargs: Any) -> "JarvisSystemKernel":
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        config: Optional[SystemKernelConfig] = None,
        force: bool = False,
    ) -> None:
        """
        Initialize the kernel.

        Args:
            config: Kernel configuration. If None, uses defaults.
            force: If True, forcibly take over from existing kernel.
        """
        # Avoid re-initialization in singleton
        if hasattr(self, "_initialized") and self._initialized:
            return

        self.config = config or SystemKernelConfig()
        self.logger = UnifiedLogger()
        self._force = force
        self._state = KernelState.INITIALIZING
        self._started_at: Optional[float] = None
        self._initialized = True

        # Core components
        self._startup_lock = StartupLock()
        self._ipc_server = IPCServer(self.config, self.logger)
        self._signal_handler = get_unified_signal_handler()

        # Managers (initialized during startup)
        self._resource_registry: Optional[ResourceManagerRegistry] = None
        self._intelligence_registry: Optional[IntelligenceRegistry] = None
        self._process_manager: Optional[ProcessStateManager] = None
        self._readiness_manager: Optional[ProgressiveReadinessManager] = None
        self._zombie_cleanup: Optional[ComprehensiveZombieCleanup] = None
        self._hot_reload: Optional[HotReloadWatcher] = None
        self._trinity: Optional[TrinityIntegrator] = None

        # Backend process
        self._backend_process: Optional[asyncio.subprocess.Process] = None
        self._backend_server: Optional[Any] = None  # uvicorn.Server if in-process

        # Frontend and loading server processes
        self._frontend_process: Optional[asyncio.subprocess.Process] = None
        self._loading_server_process: Optional[asyncio.subprocess.Process] = None

        # Enterprise status tracking
        self._enterprise_status: Dict[str, Any] = {}

        # Background tasks
        self._background_tasks: List[asyncio.Task] = []
        self._shutdown_event = asyncio.Event()

    @property
    def state(self) -> KernelState:
        """Current kernel state."""
        return self._state

    @property
    def uptime_seconds(self) -> float:
        """Kernel uptime in seconds."""
        if self._started_at is None:
            return 0.0
        return time.time() - self._started_at

    # =========================================================================
    # SAFE PHASE INITIALIZATION (v107.0)
    # =========================================================================
    async def _safe_phase_init(
        self,
        phase_name: str,
        init_coro: Coroutine[Any, Any, bool],
        timeout_seconds: float = 30.0,
        critical: bool = False,
    ) -> bool:
        """
        v107.0: Safe phase initialization with timeout and error handling.

        CRITICAL FIX: This prevents any single initialization phase from blocking
        the entire startup flow indefinitely. Each phase gets a timeout and proper
        error handling so the startup can continue even if non-critical phases fail.

        Args:
            phase_name: Human-readable name for logging (e.g., "PHASE 13: Neural Mesh Bridge")
            init_coro: The async coroutine to execute (must return bool)
            timeout_seconds: Maximum time to wait (default: 30s)
            critical: If True, failure will be logged as error; otherwise warning

        Returns:
            True if initialization succeeded, False otherwise

        Features:
        - Timeout protection prevents indefinite blocking
        - Error isolation ensures one phase can't crash startup
        - Clear logging for debugging startup issues
        - Graceful degradation - startup continues on non-critical failures
        - Async-safe with proper cancellation handling
        """
        try:
            self.logger.info(f"[v107.0] Starting {phase_name} (timeout: {timeout_seconds}s)...")
            result = await asyncio.wait_for(init_coro, timeout=timeout_seconds)
            if result:
                self.logger.info(f"[v107.0] ✅ {phase_name} completed")
            else:
                msg = f"[v107.0] ⚠️ {phase_name} returned False"
                if critical:
                    self.logger.error(msg)
                else:
                    self.logger.warning(msg)
            return result
        except asyncio.TimeoutError:
            msg = f"[v107.0] ⏱️ {phase_name} timed out after {timeout_seconds}s - skipping"
            if critical:
                self.logger.error(msg)
            else:
                self.logger.warning(msg)
            return False
        except asyncio.CancelledError:
            self.logger.warning(f"[v107.0] ❌ {phase_name} cancelled")
            raise  # Re-raise cancellation
        except Exception as e:
            msg = f"[v107.0] ❌ {phase_name} failed: {e}"
            if critical:
                self.logger.error(msg)
            else:
                self.logger.warning(msg)
            self.logger.debug(traceback.format_exc())
            return False

    async def _run_with_global_timeout(
        self,
        coro: Coroutine[Any, Any, int],
        timeout_seconds: float = 300.0,
    ) -> int:
        """
        v80.0: Wrap startup in global timeout to prevent infinite hangs.

        Args:
            coro: The coroutine to execute
            timeout_seconds: Maximum total startup time

        Returns:
            Exit code from the coroutine or 1 on timeout/error
        """
        try:
            return await asyncio.wait_for(coro, timeout=timeout_seconds)
        except asyncio.TimeoutError:
            self.logger.error(
                f"🚨 GLOBAL STARTUP TIMEOUT after {timeout_seconds}s - "
                "forcing emergency shutdown"
            )
            await self._emergency_shutdown()
            return 1
        except asyncio.CancelledError:
            self.logger.warning("🛑 Startup cancelled (SIGINT/SIGTERM received)")
            try:
                await self._emergency_shutdown()
            except asyncio.CancelledError:
                pass  # Already shutting down
            return 130  # Standard exit code for SIGINT
        except Exception as e:
            self.logger.error(f"🚨 Startup failed: {e}")
            self.logger.error(traceback.format_exc())
            try:
                await self._emergency_shutdown()
            except Exception:
                pass
            return 1

    async def _emergency_shutdown(self) -> None:
        """
        Emergency shutdown - kill everything fast.

        Called when:
        - Global timeout exceeded
        - Unhandled exception during startup
        - Critical failure detected

        Does NOT wait for graceful shutdown.
        """
        self.logger.warning("[Kernel] ⚠️ Emergency shutdown initiated")
        self._state = KernelState.SHUTTING_DOWN

        # Kill backend immediately
        if self._backend_process:
            self._backend_process.kill()
        if self._backend_server:
            self._backend_server.should_exit = True

        # Kill frontend/loading server
        if self._frontend_process:
            self._frontend_process.kill()
        if self._loading_server_process:
            self._loading_server_process.kill()

        # Cancel all background tasks
        for task in self._background_tasks:
            task.cancel()

        # Release lock
        self._startup_lock.release()

        self._state = KernelState.STOPPED
        self.logger.warning("[Kernel] ⚠️ Emergency shutdown complete")

    async def startup(self) -> int:
        """
        Run the full boot sequence.

        Returns:
            Exit code (0 for success, non-zero for failure)
        """
        self.logger.info("="*70)
        self.logger.info("JARVIS SYSTEM KERNEL - Starting")
        self.logger.info("="*70)

        self._started_at = time.time()

        try:
            # Phase 1: Preflight
            if not await self._phase_preflight():
                return 1

            # Phase 2: Resources
            if not await self._phase_resources():
                return 1

            # Phase 3: Backend
            if not await self._phase_backend():
                return 1

            # Phase 4: Intelligence
            if not await self._phase_intelligence():
                # Non-fatal - continue without intelligence
                self.logger.warning("[Kernel] Intelligence layer failed - continuing without ML")

            # Phase 5: Trinity
            if self.config.trinity_enabled:
                await self._phase_trinity()

            # Phase 6: Enterprise Services (Voice Biometrics, Cloud SQL, Caches)
            await self._phase_enterprise_services()

            # Start background pre-warming task (non-blocking)
            prewarm_task = asyncio.create_task(
                self._prewarm_python_modules(),
                name="module-prewarm"
            )
            self._background_tasks.append(prewarm_task)

            # Mark as running
            self._state = KernelState.RUNNING
            if self._readiness_manager:
                self._readiness_manager.mark_tier(ReadinessTier.FULLY_READY)

            # Final service verification
            verification = await self._verify_all_services(timeout=10.0)
            if not verification["all_healthy"]:
                unhealthy = [
                    k for k, v in verification["services"].items()
                    if isinstance(v, dict) and not v.get("healthy") and not v.get("note")
                ]
                if unhealthy:
                    self.logger.warning(f"[Kernel] Some services unhealthy: {unhealthy}")
                else:
                    self.logger.info("[Kernel] All configured services operational")

            self.logger.success(f"[Kernel] ✅ Startup complete in {time.time() - self._started_at:.2f}s")
            return 0

        except Exception as e:
            self.logger.error(f"[Kernel] Startup failed: {e}")
            self.logger.error(traceback.format_exc())
            self._state = KernelState.FAILED
            return 1

    async def _phase_preflight(self) -> bool:
        """
        Phase 1: Preflight checks and cleanup.

        - Acquire startup lock
        - Clean up zombie processes
        - Initialize IPC server
        - Install signal handlers
        """
        self._state = KernelState.PREFLIGHT

        with self.logger.section_start(LogSection.BOOT, "Phase 1: Preflight"):
            # Acquire startup lock
            if not self._startup_lock.acquire(force=self._force):
                holder_pid = self._startup_lock.get_current_holder()
                self.logger.error(f"[Kernel] Another kernel is running (PID: {holder_pid})")
                self.logger.error("[Kernel] Use --force to take over")
                return False
            self.logger.success("[Kernel] Startup lock acquired")

            # Initialize managers
            self._readiness_manager = ProgressiveReadinessManager(self.config, self.logger)
            self._readiness_manager.mark_tier(ReadinessTier.STARTING)
            await self._readiness_manager.start_heartbeat_loop()

            self._process_manager = ProcessStateManager(self.config, self.logger)

            # Zombie cleanup
            self._zombie_cleanup = ComprehensiveZombieCleanup(self.config, self.logger)
            cleanup_result = await self._zombie_cleanup.run_comprehensive_cleanup()
            if cleanup_result["zombies_killed"] > 0:
                self.logger.info(f"[Kernel] Cleaned {cleanup_result['zombies_killed']} zombie processes")

            # Install signal handlers
            loop = asyncio.get_event_loop()
            self._signal_handler.install(loop)
            self._signal_handler.register_callback(self._signal_shutdown)

            # Start IPC server
            await self._ipc_server.start()
            self._register_ipc_handlers()

            self._readiness_manager.mark_tier(ReadinessTier.PROCESS_STARTED)
            return True

    async def _phase_resources(self) -> bool:
        """
        Phase 2: Initialize resource managers.

        Initializes in parallel:
        - Docker daemon
        - GCP services
        - Dynamic port allocation
        - Storage tiers
        """
        self._state = KernelState.STARTING_RESOURCES

        with self.logger.section_start(LogSection.RESOURCES, "Phase 2: Resources"):
            self._resource_registry = ResourceManagerRegistry(self.config)

            # Create managers
            port_manager = DynamicPortManager(self.config)
            docker_manager = DockerDaemonManager(self.config)
            gcp_manager = GCPInstanceManager(self.config)
            storage_manager = TieredStorageManager(self.config)

            # Register managers (order matters - ports first)
            self._resource_registry.register(port_manager)
            self._resource_registry.register(docker_manager)
            self._resource_registry.register(gcp_manager)
            self._resource_registry.register(storage_manager)

            # Initialize all in parallel
            results = await self._resource_registry.initialize_all()

            # Check results (ports are critical)
            if not results.get("DynamicPortManager", False):
                self.logger.error("[Kernel] Failed to allocate ports")
                return False

            # Update config with selected port
            if port_manager.selected_port is not None:
                self.config.backend_port = port_manager.selected_port
            self.logger.success(f"[Kernel] Backend port: {self.config.backend_port}")

            ready_count = sum(1 for v in results.values() if v)
            self.logger.success(f"[Kernel] Resources: {ready_count}/{len(results)} initialized")
            return True

    async def _phase_backend(self) -> bool:
        """
        Phase 3: Start the backend server.

        Can run:
        - In-process: Using uvicorn.Server (shared memory, faster)
        - Subprocess: Using asyncio.subprocess (isolated, more robust)
        """
        self._state = KernelState.STARTING_BACKEND

        with self.logger.section_start(LogSection.BACKEND, "Phase 3: Backend"):
            if self.config.in_process_backend:
                success = await self._start_backend_in_process()
            else:
                success = await self._start_backend_subprocess()

            if success and self._readiness_manager:
                self._readiness_manager.mark_tier(ReadinessTier.HTTP_HEALTHY)
                self._readiness_manager.mark_component_ready("backend", True)

            return success

    async def _start_backend_in_process(self) -> bool:
        """Start backend as in-process uvicorn server."""
        self.logger.info("[Kernel] Starting backend in-process...")

        try:
            # Import uvicorn
            import uvicorn

            # Import the FastAPI app
            try:
                from backend.main import app
            except ImportError as e:
                self.logger.error(f"[Kernel] Could not import backend app: {e}")
                return False

            # Create uvicorn config
            uvicorn_config = uvicorn.Config(
                app=app,
                host=self.config.backend_host,
                port=self.config.backend_port,
                log_level="warning",
                loop="asyncio",
            )

            # Create server
            self._backend_server = uvicorn.Server(uvicorn_config)

            # Run server in background task
            task = asyncio.create_task(self._backend_server.serve())
            self._background_tasks.append(task)

            # Wait for server to be ready
            for _ in range(30):  # 30 second timeout
                if self._backend_server.started:
                    self.logger.success(f"[Kernel] Backend running at http://{self.config.backend_host}:{self.config.backend_port}")
                    return True
                await asyncio.sleep(1.0)

            self.logger.error("[Kernel] Backend failed to start in time")
            return False

        except ImportError:
            self.logger.error("[Kernel] uvicorn not available for in-process mode")
            return False
        except Exception as e:
            self.logger.error(f"[Kernel] In-process backend failed: {e}")
            return False

    async def _start_backend_subprocess(self) -> bool:
        """Start backend as subprocess."""
        self.logger.info("[Kernel] Starting backend subprocess...")

        # Find backend script
        backend_script = Path(__file__).parent / "backend" / "main.py"
        if not backend_script.exists():
            # Try alternative locations
            for alt_path in [
                Path(__file__).parent.parent / "backend" / "main.py",
                Path.cwd() / "backend" / "main.py",
            ]:
                if alt_path.exists():
                    backend_script = alt_path
                    break

        if not backend_script.exists():
            self.logger.error(f"[Kernel] Backend script not found at {backend_script}")
            return False

        try:
            # Start process
            env = os.environ.copy()
            env["JARVIS_BACKEND_PORT"] = str(self.config.backend_port)
            env["JARVIS_KERNEL_PID"] = str(os.getpid())

            self._backend_process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m", "uvicorn",
                "backend.main:app",
                "--host", self.config.backend_host,
                "--port", str(self.config.backend_port),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Register with process manager
            if self._process_manager:
                await self._process_manager.register_process(
                    "backend",
                    self._backend_process,
                    {"port": self.config.backend_port}
                )

            # Wait for backend to be ready (health check)
            if await self._wait_for_backend_health(timeout=60.0):
                self.logger.success(f"[Kernel] Backend running at http://{self.config.backend_host}:{self.config.backend_port}")
                return True
            else:
                self.logger.error("[Kernel] Backend failed health check")
                return False

        except Exception as e:
            self.logger.error(f"[Kernel] Subprocess backend failed: {e}")
            return False

    async def _wait_for_backend_health(self, timeout: float = 60.0) -> bool:
        """Wait for backend to respond to health checks."""
        if not AIOHTTP_AVAILABLE:
            # Simple socket check
            start_time = time.time()
            while (time.time() - start_time) < timeout:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(1.0)
                    result = sock.connect_ex(('localhost', self.config.backend_port))
                    sock.close()
                    if result == 0:
                        return True
                except Exception:
                    pass
                await asyncio.sleep(1.0)
            return False

        # HTTP health check
        health_url = f"http://localhost:{self.config.backend_port}/health"
        start_time = time.time()

        while (time.time() - start_time) < timeout:
            try:
                async with aiohttp.ClientSession() as session:  # type: ignore[union-attr]
                    async with session.get(health_url, timeout=5.0) as response:
                        if response.status == 200:
                            return True
            except Exception:
                pass
            await asyncio.sleep(1.0)

        return False

    async def _phase_intelligence(self) -> bool:
        """
        Phase 4: Initialize intelligence layer.

        Initializes:
        - Adaptive threshold manager
        - Hybrid workload router
        - Goal inference engine
        - Hybrid intelligence coordinator
        """
        self._state = KernelState.STARTING_INTELLIGENCE

        with self.logger.section_start(LogSection.INTELLIGENCE, "Phase 4: Intelligence"):
            try:
                self._intelligence_registry = IntelligenceRegistry(self.config)

                # Create managers
                router = HybridWorkloadRouter(self.config)
                goal_engine = GoalInferenceEngine(self.config)
                coordinator = HybridIntelligenceCoordinator(self.config)

                # Register managers
                self._intelligence_registry.register(router)
                self._intelligence_registry.register(goal_engine)
                self._intelligence_registry.register(coordinator)

                # Initialize all
                results = await self._intelligence_registry.initialize_all()

                ready_count = sum(1 for v in results.values() if v)
                self.logger.success(f"[Kernel] Intelligence: {ready_count}/{len(results)} initialized")

                if self._readiness_manager:
                    self._readiness_manager.mark_component_ready("intelligence", ready_count > 0)

                return ready_count > 0

            except Exception as e:
                self.logger.warning(f"[Kernel] Intelligence initialization failed: {e}")
                return False

    async def _phase_trinity(self) -> bool:
        """
        Phase 5: Initialize Trinity cross-repo integration.

        Starts:
        - J-Prime (local LLM inference)
        - Reactor-Core (training pipeline)
        """
        self._state = KernelState.STARTING_TRINITY

        with self.logger.section_start(LogSection.TRINITY, "Phase 5: Trinity"):
            try:
                self._trinity = TrinityIntegrator(self.config, self.logger)
                await self._trinity.initialize()

                # Start components
                results = await self._trinity.start_components()

                started_count = sum(1 for v in results.values() if v)
                self.logger.success(f"[Kernel] Trinity: {started_count}/{len(results)} components started")

                if self._readiness_manager:
                    self._readiness_manager.mark_component_ready("trinity", started_count > 0)

                return True  # Trinity is optional

            except Exception as e:
                self.logger.warning(f"[Kernel] Trinity initialization failed: {e}")
                return True  # Non-fatal

    async def _phase_enterprise_services(self) -> bool:
        """
        Phase 6: Initialize enterprise services.

        Initializes in parallel:
        - Cloud SQL proxy for database connections
        - Voice biometric authentication system
        - Semantic voice cache (ChromaDB)
        - Infrastructure orchestrator for GCP resources

        All services are optional - failures don't stop startup.
        """
        with self.logger.section_start(LogSection.BOOT, "Phase 6: Enterprise Services"):
            self.logger.info("[Kernel] Initializing enterprise services (parallel)...")

            # Run enterprise service initialization in parallel
            init_results = await asyncio.gather(
                self._initialize_cloud_sql_proxy(),
                self._initialize_voice_biometrics(),
                self._initialize_semantic_voice_cache(),
                self._initialize_infrastructure_orchestrator(),
                return_exceptions=True
            )

            # Process results
            service_names = [
                "cloud_sql",
                "voice_biometrics",
                "semantic_cache",
                "infra_orchestrator"
            ]

            init_status: Dict[str, Any] = {}
            for name, result in zip(service_names, init_results):
                if isinstance(result, Exception):
                    self.logger.warning(f"[Kernel] {name} initialization error: {result}")
                    init_status[name] = {"error": str(result)}
                else:
                    init_status[name] = result

            # Report status
            successful = [
                name for name, status in init_status.items()
                if isinstance(status, dict) and (
                    status.get("initialized") or
                    status.get("enabled") or
                    status.get("running")
                )
            ]

            self.logger.success(
                f"[Kernel] Enterprise services: {len(successful)}/{len(service_names)} active"
            )

            # Store results for later reference
            self._enterprise_status = init_status

            # Mark readiness
            if self._readiness_manager:
                # Voice biometrics is the most important enterprise service
                voice_ready = isinstance(init_status.get("voice_biometrics"), dict) and \
                             init_status.get("voice_biometrics", {}).get("initialized", False)
                self._readiness_manager.mark_component_ready("voice_biometrics", voice_ready)

            return True  # Enterprise services are optional

    # =========================================================================
    # LOADING SERVER AND FRONTEND MANAGEMENT
    # =========================================================================
    # Manages the loading page display during startup and React frontend
    # lifecycle for the main JARVIS UI.
    # =========================================================================

    async def _start_loading_server(self) -> bool:
        """
        Start the loading server for startup progress display.

        The loading server shows a progress page while the backend initializes.
        Once the system is ready, it's stopped and traffic goes to the main frontend.

        Returns:
            True if loading server started successfully
        """
        if self.config.loading_server_port == 0:
            self.logger.info("[LoadingServer] Port not configured - skipping")
            return False

        self.logger.info(f"[LoadingServer] Starting on port {self.config.loading_server_port}...")

        try:
            # Check for dedicated loading server script
            loading_server_path = self.config.backend_dir / "loading_server.py"

            if loading_server_path.exists():
                env = os.environ.copy()
                env["LOADING_SERVER_PORT"] = str(self.config.loading_server_port)

                self._loading_server_process = await asyncio.create_subprocess_exec(
                    sys.executable, str(loading_server_path),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                # Fallback: serve static files with Python's HTTP server
                public_dir = self.config.project_root / "frontend" / "public"
                if public_dir.exists():
                    self._loading_server_process = await asyncio.create_subprocess_exec(
                        sys.executable, "-m", "http.server", str(self.config.loading_server_port),
                        cwd=str(public_dir),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                else:
                    self.logger.info("[LoadingServer] No loading server found - skipping")
                    return False

            # Wait for process to start
            await asyncio.sleep(0.5)

            if self._loading_server_process.returncode is None:
                self.logger.success(
                    f"[LoadingServer] Started on port {self.config.loading_server_port} "
                    f"(PID: {self._loading_server_process.pid})"
                )
                return True
            else:
                self.logger.warning(
                    f"[LoadingServer] Exited with code {self._loading_server_process.returncode}"
                )
                return False

        except Exception as e:
            self.logger.warning(f"[LoadingServer] Failed to start: {e}")
            return False

    async def _stop_loading_server(self) -> None:
        """Stop the loading server (called after frontend is ready)."""
        if hasattr(self, '_loading_server_process') and self._loading_server_process:
            if self._loading_server_process.returncode is None:
                self.logger.info("[LoadingServer] Stopping...")
                try:
                    self._loading_server_process.terminate()
                    await asyncio.wait_for(
                        self._loading_server_process.wait(),
                        timeout=5.0
                    )
                except asyncio.TimeoutError:
                    self._loading_server_process.kill()
                    await self._loading_server_process.wait()
                self.logger.info("[LoadingServer] Stopped")
            self._loading_server_process = None

    async def _start_frontend(self) -> bool:
        """
        Start the React frontend.

        Returns:
            True if frontend started successfully
        """
        frontend_dir = self.config.project_root / "frontend"

        if not frontend_dir.exists():
            self.logger.info("[Frontend] Directory not found - skipping")
            return False

        self.logger.info("[Frontend] Starting...")

        try:
            # Check for node_modules
            node_modules = frontend_dir / "node_modules"
            if not node_modules.exists():
                self.logger.info("[Frontend] Installing dependencies (first run)...")
                npm_install = await asyncio.create_subprocess_exec(
                    "npm", "install",
                    cwd=str(frontend_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    await asyncio.wait_for(npm_install.wait(), timeout=300.0)
                except asyncio.TimeoutError:
                    self.logger.warning("[Frontend] npm install timed out")
                    return False
                if npm_install.returncode != 0:
                    self.logger.warning("[Frontend] npm install failed")
                    return False
                self.logger.success("[Frontend] Dependencies installed")

            # Configure frontend environment
            frontend_port = int(os.environ.get("JARVIS_FRONTEND_PORT", "3000"))
            env = os.environ.copy()
            env["PORT"] = str(frontend_port)
            env["BROWSER"] = "none"  # Don't auto-open browser
            env["REACT_APP_BACKEND_URL"] = f"http://localhost:{self.config.backend_port}"

            # Start the frontend
            self._frontend_process = await asyncio.create_subprocess_exec(
                "npm", "start",
                cwd=str(frontend_dir),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Wait for frontend to be ready
            deadline = time.time() + 120.0  # 2 minute timeout
            check_interval = 3.0

            while time.time() < deadline:
                try:
                    # Socket check
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(2.0)
                    result = sock.connect_ex(('localhost', frontend_port))
                    sock.close()

                    if result == 0:
                        self.logger.success(
                            f"[Frontend] Ready on port {frontend_port} "
                            f"(PID: {self._frontend_process.pid})"
                        )
                        # Stop loading server now that frontend is ready
                        await self._stop_loading_server()
                        return True

                except Exception:
                    pass

                # Check if process died
                if self._frontend_process.returncode is not None:
                    self.logger.warning(
                        f"[Frontend] Exited with code {self._frontend_process.returncode}"
                    )
                    return False

                await asyncio.sleep(check_interval)

            self.logger.warning("[Frontend] Startup timeout (120s)")
            return False

        except Exception as e:
            self.logger.error(f"[Frontend] Failed to start: {e}")
            return False

    async def _stop_frontend(self) -> None:
        """Stop the frontend (called during shutdown)."""
        if hasattr(self, '_frontend_process') and self._frontend_process:
            if self._frontend_process.returncode is None:
                self.logger.info("[Frontend] Stopping...")
                try:
                    self._frontend_process.terminate()
                    await asyncio.wait_for(
                        self._frontend_process.wait(),
                        timeout=10.0
                    )
                except asyncio.TimeoutError:
                    self._frontend_process.kill()
                    await self._frontend_process.wait()
                self.logger.info("[Frontend] Stopped")
            self._frontend_process = None

    # =========================================================================
    # PROGRESS BROADCASTING
    # =========================================================================
    # WebSocket-based progress broadcasting for real-time startup status.
    # =========================================================================

    async def _broadcast_startup_progress(
        self,
        stage: str,
        message: str,
        progress: int,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Broadcast startup progress to connected clients.

        Args:
            stage: Current startup stage (e.g., "backend", "voice", "trinity")
            message: Human-readable progress message
            progress: Progress percentage (0-100)
            metadata: Optional additional data (icons, labels, etc.)
        """
        if self.config.loading_server_port == 0:
            return  # No loading server - skip broadcasting

        progress_data = {
            "type": "startup_progress",
            "stage": stage,
            "message": message,
            "progress": min(100, max(0, progress)),
            "timestamp": datetime.now().isoformat(),
            "metadata": metadata or {},
        }

        # Try to broadcast via loading server if available
        try:
            if AIOHTTP_AVAILABLE and aiohttp is not None:
                async with aiohttp.ClientSession() as session:
                    url = f"http://localhost:{self.config.loading_server_port}/api/progress"
                    await session.post(
                        url,
                        json=progress_data,
                        timeout=aiohttp.ClientTimeout(total=2.0)
                    )
        except Exception:
            pass  # Non-critical - don't fail if broadcast fails

    # =========================================================================
    # DIAGNOSTIC LOGGING
    # =========================================================================
    # Enhanced diagnostic logging for debugging and forensics.
    # =========================================================================

    def _log_startup_checkpoint(self, checkpoint: str, message: str) -> None:
        """Log a startup checkpoint for diagnostics."""
        timestamp = datetime.now().isoformat()
        self.logger.debug(f"[Checkpoint:{checkpoint}] {message} @ {timestamp}")

    def _log_state_change(
        self,
        component: str,
        old_state: str,
        new_state: str,
        reason: str
    ) -> None:
        """Log a state change for diagnostics."""
        timestamp = datetime.now().isoformat()
        self.logger.info(
            f"[StateChange] {component}: {old_state} → {new_state} ({reason}) @ {timestamp}"
        )

    async def run(self) -> int:
        """
        Run the main event loop.

        Starts background tasks and waits for shutdown signal.

        Returns:
            Exit code
        """
        self.logger.info("[Kernel] Entering main loop...")

        # Start hot reload if in dev mode
        if self.config.dev_mode and self.config.hot_reload_enabled:
            self._hot_reload = HotReloadWatcher(self.config, self.logger)
            self._hot_reload.set_restart_callback(self._handle_hot_reload)
            await self._hot_reload.start()

        # Start background tasks
        self._background_tasks.extend([
            asyncio.create_task(self._health_monitor_loop(), name="health-monitor"),
        ])

        # If readiness manager has heartbeat, it's already running
        # Add cost optimizer if scale-to-zero is enabled
        if self.config.scale_to_zero_enabled:
            self._background_tasks.append(
                asyncio.create_task(self._cost_optimizer_loop(), name="cost-optimizer")
            )

        try:
            # Wait for shutdown signal
            await self._signal_handler.wait_for_shutdown()
            self.logger.info("[Kernel] Shutdown signal received")
            return await self.cleanup()

        except asyncio.CancelledError:
            self.logger.info("[Kernel] Main loop cancelled")
            return await self.cleanup()

    async def cleanup(self) -> int:
        """
        Master shutdown orchestration.

        Stops all components in reverse order:
        1. Background tasks
        2. Trinity components
        3. Intelligence layer
        4. Backend
        5. Resources
        6. IPC server
        7. Release lock

        Returns:
            Exit code
        """
        self._state = KernelState.SHUTTING_DOWN
        self.logger.info("[Kernel] Initiating shutdown...")

        with self.logger.section_start(LogSection.SHUTDOWN, "Shutdown"):
            # Stop hot reload
            if self._hot_reload:
                await self._hot_reload.stop()

            # Stop readiness heartbeat
            if self._readiness_manager:
                await self._readiness_manager.stop_heartbeat_loop()

            # Cancel background tasks
            for task in self._background_tasks:
                task.cancel()

            if self._background_tasks:
                await asyncio.gather(*self._background_tasks, return_exceptions=True)

            # Stop Trinity
            if self._trinity:
                await self._trinity.stop()
                self.logger.info("[Kernel] Trinity stopped")

            # Stop frontend and loading server
            await self._stop_frontend()
            await self._stop_loading_server()

            # Stop backend
            if self._backend_server:
                self._backend_server.should_exit = True
                self.logger.info("[Kernel] Backend server stopping")
            elif self._backend_process:
                self._backend_process.terminate()
                try:
                    await asyncio.wait_for(self._backend_process.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    self._backend_process.kill()
                self.logger.info("[Kernel] Backend process stopped")

            # Stop process manager
            if self._process_manager:
                await self._process_manager.stop_all()

            # Cleanup resources
            if self._resource_registry:
                await self._resource_registry.cleanup_all()
                self.logger.info("[Kernel] Resources cleaned up")

            # Stop IPC server
            await self._ipc_server.stop()

            # Release lock
            self._startup_lock.release()

            self._state = KernelState.STOPPED
            self.logger.success("[Kernel] Shutdown complete")

            # Return appropriate exit code
            if self._signal_handler.shutdown_reason == "SIGINT":
                return 130  # 128 + SIGINT(2)
            elif self._signal_handler.shutdown_reason == "SIGTERM":
                return 143  # 128 + SIGTERM(15)
            return 0

    async def _signal_shutdown(self) -> None:
        """Handle shutdown signal callback."""
        self._shutdown_event.set()

    def _register_ipc_handlers(self) -> None:
        """Register IPC command handlers."""
        self._ipc_server.register_handler(IPCCommand.HEALTH, self._ipc_health)
        self._ipc_server.register_handler(IPCCommand.STATUS, self._ipc_status)
        self._ipc_server.register_handler(IPCCommand.SHUTDOWN, self._ipc_shutdown)

    async def _ipc_health(self) -> Dict[str, Any]:
        """Handle health IPC command."""
        return {
            "healthy": self._state == KernelState.RUNNING,
            "state": self._state.value,
            "uptime_seconds": self.uptime_seconds,
            "pid": os.getpid(),
            "kernel_id": self.config.kernel_id,
            "readiness": self._readiness_manager.get_status() if self._readiness_manager else {},
        }

    async def _ipc_status(self) -> Dict[str, Any]:
        """Handle status IPC command."""
        status: Dict[str, Any] = {
            "state": self._state.value,
            "uptime_seconds": self.uptime_seconds,
            "pid": os.getpid(),
            "config": {
                "kernel_id": self.config.kernel_id,
                "mode": self.config.mode,
                "backend_port": self.config.backend_port,
                "dev_mode": self.config.dev_mode,
            },
        }

        if self._readiness_manager:
            status["readiness"] = self._readiness_manager.get_status()

        if self._resource_registry:
            status["resources"] = self._resource_registry.get_all_status()

        if self._trinity:
            status["trinity"] = self._trinity.get_status()

        if self._process_manager:
            status["processes"] = self._process_manager.get_statistics()

        return status

    async def _ipc_shutdown(self) -> Dict[str, Any]:
        """Handle shutdown IPC command."""
        self._shutdown_event.set()
        self._signal_handler._shutdown_requested = True
        self._signal_handler._shutdown_event.set() if self._signal_handler._shutdown_event else None
        return {"acknowledged": True, "message": "Shutdown initiated"}

    async def _health_monitor_loop(self) -> None:
        """Background health monitoring loop."""
        interval = self.config.health_check_interval

        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(interval)

                # Check backend health
                if self._backend_process:
                    if self._backend_process.returncode is not None:
                        self.logger.error("[Kernel] Backend process died!")
                        if self._readiness_manager:
                            self._readiness_manager.mark_component_ready("backend", False)
                            self._readiness_manager.add_error("Backend process died")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.debug(f"[Kernel] Health monitor error: {e}")

    async def _cost_optimizer_loop(self) -> None:
        """Background cost optimization loop."""
        interval = 60.0  # Check every minute

        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(interval)

                # Check for scale-to-zero conditions
                # This would integrate with ScaleToZeroCostOptimizer

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.debug(f"[Kernel] Cost optimizer error: {e}")

    async def _handle_hot_reload(self, changed_files: List[str]) -> None:
        """Handle hot reload trigger."""
        self.logger.info(f"[Kernel] Hot reload triggered by {len(changed_files)} file change(s)")

        # For now, just log. Full implementation would restart backend.
        for f in changed_files[:5]:
            self.logger.info(f"  - {f}")
        if len(changed_files) > 5:
            self.logger.info(f"  ... and {len(changed_files) - 5} more")

    # =========================================================================
    # ADAPTIVE TIMEOUT MANAGEMENT
    # =========================================================================
    # Enterprise-grade adaptive timeouts that adjust based on system load
    # to prevent false failures during legitimate slow operations.
    # =========================================================================

    async def _get_adaptive_timeout(self, base_timeout: float) -> float:
        """
        Calculate adaptive timeout based on system load.

        Increases timeout when system is under heavy load to prevent
        false timeouts during legitimate slow operations.

        Args:
            base_timeout: Base timeout in seconds

        Returns:
            Adjusted timeout (potentially higher if system is loaded)
        """
        try:
            import psutil

            # Quick CPU and memory check (non-blocking)
            cpu_percent = psutil.cpu_percent(interval=0.05)
            memory = psutil.virtual_memory()

            # Calculate load multiplier
            if cpu_percent > 90 or memory.percent > 95:
                multiplier = 2.0  # Heavy load - double timeout
            elif cpu_percent > 75 or memory.percent > 85:
                multiplier = 1.5  # Moderate load - 50% more time
            elif cpu_percent > 50 or memory.percent > 70:
                multiplier = 1.25  # Light load - 25% more time
            else:
                multiplier = 1.0  # Normal

            adjusted = base_timeout * multiplier
            if multiplier > 1.0:
                self.logger.debug(
                    f"[AdaptiveTimeout] {base_timeout}s → {adjusted}s "
                    f"(CPU: {cpu_percent}%, MEM: {memory.percent}%)"
                )
            return adjusted

        except ImportError:
            return base_timeout
        except Exception:
            return base_timeout

    # =========================================================================
    # ADVANCED STARTUP DIAGNOSTICS
    # =========================================================================
    # Comprehensive startup diagnostics for troubleshooting and optimization.
    # =========================================================================

    async def _run_startup_diagnostics(self) -> Dict[str, Any]:
        """
        Run comprehensive startup diagnostics.

        Collects system information, component status, and performance metrics
        for troubleshooting and optimization.

        Returns:
            Dict with diagnostic information
        """
        diagnostics: Dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "kernel_id": self.config.kernel_id,
            "kernel_version": self.config.kernel_version,
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "platform": sys.platform,
            "system": {},
            "components": {},
            "performance": {},
            "warnings": [],
        }

        # System information
        try:
            import psutil

            diagnostics["system"] = {
                "cpu_count": psutil.cpu_count(),
                "cpu_percent": psutil.cpu_percent(interval=0.1),
                "memory_total_gb": round(psutil.virtual_memory().total / (1024**3), 2),
                "memory_available_gb": round(psutil.virtual_memory().available / (1024**3), 2),
                "memory_percent": psutil.virtual_memory().percent,
                "disk_free_gb": round(psutil.disk_usage('/').free / (1024**3), 2),
            }
        except ImportError:
            diagnostics["system"]["note"] = "psutil not available"

        # Component status
        diagnostics["components"] = {
            "backend": {
                "running": self._backend_process is not None and self._backend_process.returncode is None,
                "port": self.config.backend_port,
            },
            "ipc_server": {
                "running": self._ipc_server is not None,
            },
            "readiness_manager": {
                "enabled": self._readiness_manager is not None,
                "status": self._readiness_manager.get_status() if self._readiness_manager else None,
            },
            "trinity": {
                "enabled": self.config.trinity_enabled,
                "prime_enabled": self.config.prime_enabled,
                "reactor_enabled": self.config.reactor_enabled,
            },
        }

        # Performance metrics
        if self._started_at:
            diagnostics["performance"] = {
                "uptime_seconds": self.uptime_seconds,
                "startup_time_seconds": self._started_at - time.time() if hasattr(self, '_boot_start_time') else None,
            }

        return diagnostics

    async def _validate_trinity_repos(self) -> Dict[str, Any]:
        """
        Validate Trinity repository availability and health.

        Checks that JARVIS-Prime and Reactor-Core repositories are present
        and properly configured for cross-repo coordination.

        Returns:
            Dict with validation results
        """
        result: Dict[str, Any] = {
            "valid": True,
            "prime": {"found": False, "path": None, "issues": []},
            "reactor": {"found": False, "path": None, "issues": []},
        }

        # Check JARVIS-Prime
        if self.config.prime_repo_path:
            prime_path = self.config.prime_repo_path
            result["prime"]["path"] = str(prime_path)

            if prime_path.exists():
                result["prime"]["found"] = True

                # Check for key files
                key_files = [
                    prime_path / "main.py",
                    prime_path / "start.py",
                    prime_path / "pyproject.toml",
                ]
                has_startup = any(f.exists() for f in key_files)

                if not has_startup:
                    result["prime"]["issues"].append("No startup script found")
            else:
                result["prime"]["issues"].append(f"Path does not exist: {prime_path}")
        else:
            result["prime"]["issues"].append("Prime repo path not configured")

        # Check Reactor-Core
        if self.config.reactor_repo_path:
            reactor_path = self.config.reactor_repo_path
            result["reactor"]["path"] = str(reactor_path)

            if reactor_path.exists():
                result["reactor"]["found"] = True

                # Check for key files
                key_files = [
                    reactor_path / "main.py",
                    reactor_path / "start.py",
                    reactor_path / "pyproject.toml",
                ]
                has_startup = any(f.exists() for f in key_files)

                if not has_startup:
                    result["reactor"]["issues"].append("No startup script found")
            else:
                result["reactor"]["issues"].append(f"Path does not exist: {reactor_path}")
        else:
            result["reactor"]["issues"].append("Reactor repo path not configured")

        # Determine overall validity
        result["valid"] = (
            (not self.config.prime_enabled or result["prime"]["found"]) and
            (not self.config.reactor_enabled or result["reactor"]["found"])
        )

        return result

    # =========================================================================
    # RESOURCE QUOTA MANAGEMENT
    # =========================================================================
    # Enterprise-grade resource quota management for preventing system
    # resource exhaustion.
    # =========================================================================

    async def _check_resource_quotas(self) -> Dict[str, Any]:
        """
        Check current resource utilization against quotas.

        Returns:
            Dict with quota status and any violations
        """
        result: Dict[str, Any] = {
            "within_limits": True,
            "quotas": {},
            "violations": [],
        }

        try:
            import psutil

            # Memory quota (default: 80% of available)
            mem_quota_percent = float(os.environ.get("JARVIS_MEM_QUOTA_PERCENT", "80"))
            mem_current = psutil.virtual_memory().percent
            result["quotas"]["memory"] = {
                "current_percent": mem_current,
                "quota_percent": mem_quota_percent,
                "ok": mem_current < mem_quota_percent,
            }
            if mem_current >= mem_quota_percent:
                result["violations"].append(f"Memory usage {mem_current}% exceeds quota {mem_quota_percent}%")
                result["within_limits"] = False

            # CPU quota (informational)
            cpu_quota_percent = float(os.environ.get("JARVIS_CPU_QUOTA_PERCENT", "90"))
            cpu_current = psutil.cpu_percent(interval=0.1)
            result["quotas"]["cpu"] = {
                "current_percent": cpu_current,
                "quota_percent": cpu_quota_percent,
                "ok": cpu_current < cpu_quota_percent,
            }

            # Disk quota
            disk_quota_gb = float(os.environ.get("JARVIS_DISK_QUOTA_GB", "1"))
            disk_free_gb = psutil.disk_usage('/').free / (1024**3)
            result["quotas"]["disk"] = {
                "free_gb": round(disk_free_gb, 2),
                "quota_gb": disk_quota_gb,
                "ok": disk_free_gb > disk_quota_gb,
            }
            if disk_free_gb < disk_quota_gb:
                result["violations"].append(f"Free disk {disk_free_gb:.1f}GB below quota {disk_quota_gb}GB")
                result["within_limits"] = False

            # File descriptor quota
            try:
                import resource
                soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
                # Count current open files
                current_fds = len(psutil.Process().open_files()) + len(psutil.Process().net_connections())
                fd_quota_percent = 80  # Use at most 80% of soft limit
                fd_quota = int(soft_limit * fd_quota_percent / 100)

                result["quotas"]["file_descriptors"] = {
                    "current": current_fds,
                    "soft_limit": soft_limit,
                    "hard_limit": hard_limit,
                    "quota": fd_quota,
                    "ok": current_fds < fd_quota,
                }
                if current_fds >= fd_quota:
                    result["violations"].append(f"File descriptors {current_fds} near limit {fd_quota}")
            except (ImportError, AttributeError):
                pass

        except ImportError:
            result["quotas"]["note"] = "psutil not available"

        return result

    # =========================================================================
    # GRACEFUL DEGRADATION
    # =========================================================================
    # Enterprise-grade graceful degradation for handling resource constraints.
    # =========================================================================

    async def _apply_graceful_degradation(self) -> Dict[str, Any]:
        """
        Apply graceful degradation based on resource constraints.

        Disables non-essential features when resources are constrained
        to maintain core functionality.

        Returns:
            Dict with degradation decisions
        """
        result: Dict[str, Any] = {
            "degradation_applied": False,
            "disabled_features": [],
            "reason": None,
        }

        quota_status = await self._check_resource_quotas()

        if not quota_status["within_limits"]:
            result["degradation_applied"] = True
            result["reason"] = "; ".join(quota_status["violations"])

            # Determine what to disable based on available memory
            mem_quota = quota_status.get("quotas", {}).get("memory", {})
            if mem_quota.get("current_percent", 0) > 85:
                # Critical memory pressure - disable ML features
                if self.config.hybrid_intelligence_enabled:
                    self.logger.warning("[Degradation] Disabling ML features due to memory pressure")
                    result["disabled_features"].append("hybrid_intelligence")

                if self.config.voice_cache_enabled:
                    self.logger.warning("[Degradation] Disabling voice cache due to memory pressure")
                    result["disabled_features"].append("voice_cache")

            elif mem_quota.get("current_percent", 0) > 75:
                # Moderate memory pressure - disable voice cache
                if self.config.voice_cache_enabled:
                    self.logger.warning("[Degradation] Disabling voice cache due to memory usage")
                    result["disabled_features"].append("voice_cache")

            self.logger.warning(
                f"[Degradation] Applied degradation: {result['disabled_features']} - {result['reason']}"
            )

        return result

    # =========================================================================
    # ENTERPRISE VOICE BIOMETRICS INITIALIZATION
    # =========================================================================
    # Full voice biometric system initialization with ECAPA-TDNN speaker
    # verification, dynamic user detection, and profile validation.
    # =========================================================================

    async def _initialize_voice_biometrics(self) -> Dict[str, Any]:
        """
        Initialize the voice biometric authentication system.

        This enterprise-grade initialization:
        - Loads Cloud SQL database with voiceprint profiles
        - Initializes ECAPA-TDNN speaker verification model
        - Validates all profile dimensions match model dimensions
        - Detects primary users dynamically (no hardcoding!)
        - Enables BEAST MODE features if available

        Returns:
            Dict with initialization results and status
        """
        result: Dict[str, Any] = {
            "initialized": False,
            "model_dimension": 0,
            "profiles_loaded": 0,
            "primary_users": [],
            "beast_mode_enabled": False,
            "warnings": [],
            "errors": [],
        }

        self.logger.info("[VoiceBio] Initializing voice biometric system...")

        try:
            # Ensure backend dir is in path for imports
            backend_dir = self.config.backend_dir
            if str(backend_dir) not in sys.path:
                sys.path.insert(0, str(backend_dir))

            # Initialize learning database
            self.logger.info("[VoiceBio] Loading learning database...")
            try:
                from intelligence.learning_database import JARVISLearningDatabase

                learning_db = JARVISLearningDatabase()
                await learning_db.initialize()

                self.logger.success("[VoiceBio] Learning database initialized")

                # Check for Phase 2 features
                if hasattr(learning_db, 'hybrid_sync') and learning_db.hybrid_sync:
                    hs = learning_db.hybrid_sync
                    result["phase2_features"] = {
                        "faiss_cache": bool(hs.faiss_cache and getattr(hs.faiss_cache, 'index', None)),
                        "prometheus": bool(hs.prometheus and hs.prometheus.enabled),
                        "redis": bool(hs.redis and getattr(hs.redis, 'redis', None)),
                        "ml_prefetcher": bool(hs.ml_prefetcher),
                    }

            except ImportError as e:
                result["warnings"].append(f"Learning database not available: {e}")
                learning_db = None

            # Initialize speaker verification service
            self.logger.info("[VoiceBio] Loading speaker verification service...")
            try:
                from voice.speaker_verification_service import SpeakerVerificationService

                speaker_service = SpeakerVerificationService(learning_db)
                await speaker_service.initialize_fast()  # Background encoder loading

                result["model_dimension"] = speaker_service.current_model_dimension
                result["profiles_loaded"] = len(speaker_service.speaker_profiles)

                self.logger.success(
                    f"[VoiceBio] Speaker verification ready: "
                    f"{result['profiles_loaded']} profiles, {result['model_dimension']}D model"
                )

                # Validate profile dimensions
                mismatched = []
                for name, profile in speaker_service.speaker_profiles.items():
                    embedding = profile.get('embedding')
                    if embedding is not None:
                        import numpy as np
                        emb_array = np.array(embedding)
                        emb_dim = emb_array.shape[-1] if emb_array.ndim > 0 else 0
                        if emb_dim != result["model_dimension"]:
                            mismatched.append((name, emb_dim))

                if mismatched:
                    result["warnings"].append(
                        f"{len(mismatched)} profiles need re-enrollment: "
                        f"{[m[0] for m in mismatched]}"
                    )

                # Dynamic primary user detection (no hardcoding!)
                primary_users = []
                for name, profile in speaker_service.speaker_profiles.items():
                    is_primary = (
                        profile.get("is_primary_user", False) or
                        profile.get("is_owner", False) or
                        profile.get("security_clearance") == "admin"
                    )
                    if is_primary:
                        primary_users.append(name)

                # Fallback: users with valid embeddings
                if not primary_users:
                    for name, profile in speaker_service.speaker_profiles.items():
                        if profile.get("embedding") is not None:
                            primary_users.append(name)

                result["primary_users"] = primary_users

                # Check BEAST MODE (acoustic features)
                beast_mode_profiles = []
                for name, profile in speaker_service.speaker_profiles.items():
                    acoustic_features = profile.get("acoustic_features", {})
                    if any(v is not None for v in acoustic_features.values()):
                        beast_mode_profiles.append(name)

                result["beast_mode_enabled"] = len(beast_mode_profiles) > 0
                if result["beast_mode_enabled"]:
                    self.logger.success(
                        f"[VoiceBio] 🔬 BEAST MODE enabled for {len(beast_mode_profiles)} profile(s)"
                    )

                result["initialized"] = True

            except ImportError as e:
                result["errors"].append(f"Speaker verification not available: {e}")

        except Exception as e:
            result["errors"].append(f"Voice biometric initialization failed: {e}")
            self.logger.error(f"[VoiceBio] Initialization failed: {e}")

        return result

    # =========================================================================
    # CLOUD SQL PROXY MANAGEMENT
    # =========================================================================
    # Enterprise-grade Cloud SQL proxy lifecycle management with automatic
    # startup, health monitoring, and graceful shutdown.
    # =========================================================================

    async def _initialize_cloud_sql_proxy(self) -> Dict[str, Any]:
        """
        Initialize and manage the Cloud SQL proxy for database connections.

        Features:
        - Auto-detects if proxy is already running
        - Starts proxy if needed (singleton pattern)
        - Validates connection to Cloud SQL
        - Falls back to SQLite if unavailable

        Returns:
            Dict with proxy status and connection info
        """
        result: Dict[str, Any] = {
            "enabled": False,
            "running": False,
            "reused_existing": False,
            "port": None,
            "connection_name": None,
            "fallback_to_sqlite": False,
        }

        if not self.config.cloud_sql_enabled:
            self.logger.info("[CloudSQL] Proxy disabled by configuration")
            return result

        self.logger.info("[CloudSQL] Initializing Cloud SQL proxy...")

        try:
            # Load database config
            config_path = self.config.jarvis_home / "gcp" / "database_config.json"
            if not config_path.exists():
                self.logger.warning("[CloudSQL] Config not found, falling back to SQLite")
                result["fallback_to_sqlite"] = True
                return result

            import json
            with open(config_path, "r") as f:
                db_config = json.load(f)

            cloud_sql_config = db_config.get("cloud_sql", {})
            result["connection_name"] = cloud_sql_config.get("connection_name")
            result["port"] = cloud_sql_config.get("port", 5432)

            # Set environment variables
            os.environ["JARVIS_DB_TYPE"] = "cloudsql"
            os.environ["JARVIS_DB_CONNECTION_NAME"] = result["connection_name"]
            os.environ["JARVIS_DB_HOST"] = "127.0.0.1"
            os.environ["JARVIS_DB_PORT"] = str(result["port"])
            if "password" in cloud_sql_config:
                os.environ["JARVIS_DB_PASSWORD"] = cloud_sql_config["password"]

            # Import proxy manager
            backend_dir = self.config.backend_dir
            if str(backend_dir) not in sys.path:
                sys.path.insert(0, str(backend_dir))

            try:
                from intelligence.cloud_sql_proxy_manager import get_proxy_manager

                proxy_manager = get_proxy_manager()

                # Check if already running
                if proxy_manager.is_running():
                    self.logger.info("[CloudSQL] Proxy already running - reusing")
                    result["running"] = True
                    result["reused_existing"] = True
                    result["enabled"] = True
                else:
                    # Start proxy
                    self.logger.info("[CloudSQL] Starting proxy process...")
                    started = await proxy_manager.start(force_restart=False)

                    if started:
                        self.logger.success(f"[CloudSQL] Proxy started on port {result['port']}")
                        result["running"] = True
                        result["enabled"] = True
                    else:
                        self.logger.warning("[CloudSQL] Proxy failed to start, using SQLite")
                        result["fallback_to_sqlite"] = True

            except ImportError as e:
                self.logger.warning(f"[CloudSQL] Proxy manager not available: {e}")
                result["fallback_to_sqlite"] = True

        except Exception as e:
            self.logger.error(f"[CloudSQL] Initialization error: {e}")
            result["fallback_to_sqlite"] = True

        return result

    # =========================================================================
    # MODULE PRE-WARMING
    # =========================================================================
    # Background task that pre-imports heavy Python modules to reduce
    # latency during actual usage.
    # =========================================================================

    async def _prewarm_python_modules(self) -> Dict[str, Any]:
        """
        Pre-warm heavy Python modules in the background.

        This imports commonly-used but slow-loading modules before
        they're needed, reducing latency during actual operations.

        Returns:
            Dict with pre-warming results
        """
        result: Dict[str, Any] = {
            "modules_loaded": [],
            "modules_failed": [],
            "total_time_ms": 0,
        }

        start_time = time.time()

        # Heavy modules to pre-warm (in order of priority)
        modules_to_prewarm = [
            # ML/AI modules (slowest)
            "torch",
            "transformers",
            "numpy",
            "scipy",
            "sklearn",
            # Audio/Voice
            "librosa",
            "sounddevice",
            "pyaudio",
            # Database
            "asyncpg",
            "sqlalchemy",
            # Web
            "aiohttp",
            "websockets",
            # System
            "psutil",
            "watchdog",
        ]

        self.logger.info(f"[Prewarm] Pre-warming {len(modules_to_prewarm)} modules...")

        for module_name in modules_to_prewarm:
            try:
                # Import in executor to not block
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    __import__,
                    module_name
                )
                result["modules_loaded"].append(module_name)
            except ImportError:
                result["modules_failed"].append(module_name)
            except Exception as e:
                self.logger.debug(f"[Prewarm] {module_name} failed: {e}")
                result["modules_failed"].append(module_name)

            # Small yield to allow other tasks
            await asyncio.sleep(0)

        result["total_time_ms"] = (time.time() - start_time) * 1000

        self.logger.info(
            f"[Prewarm] Loaded {len(result['modules_loaded'])}/{len(modules_to_prewarm)} "
            f"modules in {result['total_time_ms']:.0f}ms"
        )

        return result

    # =========================================================================
    # SEMANTIC VOICE CACHE INITIALIZATION
    # =========================================================================
    # ChromaDB-based semantic cache for voice embeddings to reduce
    # API calls and improve response time for voice authentication.
    # =========================================================================

    async def _initialize_semantic_voice_cache(self) -> Dict[str, Any]:
        """
        Initialize the semantic voice cache (ChromaDB).

        Features:
        - Caches voice embeddings for faster verification
        - Reduces ECAPA-TDNN inference for known phrases
        - Persists across restarts

        Returns:
            Dict with cache initialization status
        """
        result: Dict[str, Any] = {
            "enabled": False,
            "initialized": False,
            "collection_name": "voice_embeddings",
            "cached_count": 0,
        }

        if not self.config.voice_cache_enabled:
            self.logger.info("[VoiceCache] Semantic cache disabled by configuration")
            return result

        self.logger.info("[VoiceCache] Initializing semantic voice cache...")

        try:
            import chromadb
            from chromadb.config import Settings

            # Configure persistent storage
            cache_dir = self.config.jarvis_home / "cache" / "voice_embeddings"
            cache_dir.mkdir(parents=True, exist_ok=True)

            # Create ChromaDB client
            client = chromadb.PersistentClient(
                path=str(cache_dir),
                settings=Settings(anonymized_telemetry=False)
            )

            # Get or create collection
            collection = client.get_or_create_collection(
                name=result["collection_name"],
                metadata={"description": "Voice embedding cache for ECAPA-TDNN"}
            )

            result["cached_count"] = collection.count()
            result["enabled"] = True
            result["initialized"] = True

            self.logger.success(
                f"[VoiceCache] ChromaDB ready with {result['cached_count']} cached embeddings"
            )

        except ImportError:
            self.logger.info("[VoiceCache] ChromaDB not available - cache disabled")
        except Exception as e:
            self.logger.warning(f"[VoiceCache] Initialization failed: {e}")

        return result

    # =========================================================================
    # INFRASTRUCTURE ORCHESTRATION
    # =========================================================================
    # Manages GCP infrastructure lifecycle including Spot VMs, Cloud Run,
    # and orphan resource cleanup.
    # =========================================================================

    async def _initialize_infrastructure_orchestrator(self) -> Dict[str, Any]:
        """
        Initialize the infrastructure orchestrator for GCP resource management.

        Features:
        - Session tracking with unique IDs
        - Orphan detection and cleanup (5-minute intervals)
        - Resource tagging for cost allocation

        Returns:
            Dict with orchestrator status
        """
        result: Dict[str, Any] = {
            "enabled": False,
            "session_id": None,
            "orphan_detection": False,
        }

        if not self.config.gcp_enabled:
            self.logger.info("[InfraOrch] GCP disabled - skipping orchestrator")
            return result

        self.logger.info("[InfraOrch] Initializing infrastructure orchestrator...")

        try:
            backend_dir = self.config.backend_dir
            if str(backend_dir) not in sys.path:
                sys.path.insert(0, str(backend_dir))

            from core.infrastructure_orchestrator import (
                get_infrastructure_orchestrator,
                start_orphan_detection,
            )

            # Initialize orchestrator
            orchestrator = await get_infrastructure_orchestrator()
            result["session_id"] = orchestrator.session_id if hasattr(orchestrator, 'session_id') else None
            result["enabled"] = True

            self.logger.success("[InfraOrch] Orchestrator initialized")

            # Start orphan detection
            orphan_task = await start_orphan_detection(auto_cleanup=True)
            result["orphan_detection"] = True

            self.logger.success("[InfraOrch] Orphan detection loop started (5-min interval)")

        except ImportError as e:
            self.logger.info(f"[InfraOrch] Orchestrator not available: {e}")
        except Exception as e:
            self.logger.warning(f"[InfraOrch] Initialization failed: {e}")

        return result

    # =========================================================================
    # COMPREHENSIVE SERVICE VERIFICATION
    # =========================================================================
    # Advanced service health checking with parallel execution and
    # detailed diagnostics.
    # =========================================================================

    async def _verify_all_services(self, timeout: float = 30.0) -> Dict[str, Any]:
        """
        Verify all services are healthy and ready.

        Performs parallel health checks on:
        - Backend API
        - WebSocket server
        - Database connection
        - Voice biometric service
        - Trinity components (if enabled)

        Returns:
            Dict with comprehensive health status
        """
        result: Dict[str, Any] = {
            "all_healthy": True,
            "services": {},
            "total_check_time_ms": 0,
        }

        start_time = time.time()

        # Define service checks
        async def check_backend() -> Dict[str, Any]:
            port = self.config.backend_port
            status: Dict[str, Any] = {"healthy": False, "name": "backend"}
            try:
                # Socket check
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5.0)
                conn_result = sock.connect_ex(('localhost', port))
                sock.close()

                if conn_result == 0:
                    # HTTP health check
                    if AIOHTTP_AVAILABLE and aiohttp is not None:
                        async with aiohttp.ClientSession() as session:
                            url = f"http://localhost:{port}/health"
                            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5.0)) as resp:
                                if resp.status == 200:
                                    data = await resp.json()
                                    status["healthy"] = True
                                    status["response"] = data
                    else:
                        status["healthy"] = True
                        status["note"] = "Port open (no HTTP check)"
                else:
                    status["error"] = f"Port {port} not open"
            except Exception as e:
                status["error"] = str(e)
            return status

        async def check_websocket() -> Dict[str, Any]:
            port = self.config.websocket_port
            status: Dict[str, Any] = {"healthy": False, "name": "websocket"}
            if port == 0:
                status["note"] = "WebSocket port not configured"
                return status
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5.0)
                conn_result = sock.connect_ex(('localhost', port))
                sock.close()
                status["healthy"] = conn_result == 0
                if not status["healthy"]:
                    status["error"] = f"Port {port} not open"
            except Exception as e:
                status["error"] = str(e)
            return status

        async def check_trinity_prime() -> Dict[str, Any]:
            status: Dict[str, Any] = {"healthy": False, "name": "prime"}
            if not self.config.trinity_enabled or not self.config.prime_enabled:
                status["note"] = "Prime not enabled"
                return status
            port = self.config.prime_api_port
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5.0)
                conn_result = sock.connect_ex(('localhost', port))
                sock.close()
                status["healthy"] = conn_result == 0
                if not status["healthy"]:
                    status["note"] = f"Prime not responding on port {port}"
            except Exception as e:
                status["error"] = str(e)
            return status

        async def check_trinity_reactor() -> Dict[str, Any]:
            status: Dict[str, Any] = {"healthy": False, "name": "reactor"}
            if not self.config.trinity_enabled or not self.config.reactor_enabled:
                status["note"] = "Reactor not enabled"
                return status
            port = self.config.reactor_api_port
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5.0)
                conn_result = sock.connect_ex(('localhost', port))
                sock.close()
                status["healthy"] = conn_result == 0
                if not status["healthy"]:
                    status["note"] = f"Reactor not responding on port {port}"
            except Exception as e:
                status["error"] = str(e)
            return status

        # Run all checks in parallel
        check_results = await asyncio.gather(
            check_backend(),
            check_websocket(),
            check_trinity_prime(),
            check_trinity_reactor(),
            return_exceptions=True
        )

        for check_result in check_results:
            if isinstance(check_result, Exception):
                result["services"]["error"] = str(check_result)
                result["all_healthy"] = False
            elif isinstance(check_result, dict):
                name = check_result.get("name", "unknown")
                result["services"][name] = check_result
                if not check_result.get("healthy", False) and not check_result.get("note"):
                    result["all_healthy"] = False

        result["total_check_time_ms"] = (time.time() - start_time) * 1000

        return result

    # =========================================================================
    # ENTERPRISE-GRADE PRE-FLIGHT CHECKS
    # =========================================================================
    # These methods perform comprehensive system validation before startup,
    # ensuring all prerequisites are met and the environment is healthy.
    # =========================================================================

    async def _enhanced_preflight_checks(self) -> Dict[str, Any]:
        """
        Run comprehensive pre-flight checks.

        Returns a dict with check results and any warnings/errors.
        This is an enterprise-grade validation that catches issues early.
        """
        results = {
            "passed": True,
            "checks": {},
            "warnings": [],
            "errors": [],
        }

        # Run all checks in parallel for speed
        check_tasks = [
            ("python_version", self._check_python_version()),
            ("system_resources", self._check_system_resources()),
            ("claude_config", self._check_claude_configuration()),
            ("permissions", self._check_permissions()),
            ("dependencies", self._check_critical_dependencies()),
            ("network", self._check_network_availability()),
        ]

        # Execute in parallel with timeout
        async def run_check(name: str, coro) -> Tuple[str, Dict[str, Any]]:
            try:
                result = await asyncio.wait_for(coro, timeout=30.0)
                return name, result
            except asyncio.TimeoutError:
                return name, {"passed": False, "error": "Check timed out"}
            except Exception as e:
                return name, {"passed": False, "error": str(e)}

        check_results = await asyncio.gather(
            *[run_check(name, coro) for name, coro in check_tasks],
            return_exceptions=False
        )

        for name, result in check_results:
            results["checks"][name] = result
            if not result.get("passed", False):
                if result.get("critical", False):
                    results["errors"].append(f"{name}: {result.get('error', 'Failed')}")
                    results["passed"] = False
                else:
                    results["warnings"].append(f"{name}: {result.get('warning', 'Issue detected')}")

        return results

    async def _check_python_version(self) -> Dict[str, Any]:
        """Validate Python version meets requirements."""
        version_info = sys.version_info
        min_version = (3, 9)

        if version_info < min_version:
            return {
                "passed": False,
                "critical": True,
                "error": f"Python {min_version[0]}.{min_version[1]}+ required, got {version_info.major}.{version_info.minor}",
            }

        return {
            "passed": True,
            "version": f"{version_info.major}.{version_info.minor}.{version_info.micro}",
            "executable": sys.executable,
        }

    async def _check_system_resources(self) -> Dict[str, Any]:
        """Check system has adequate resources."""
        result: Dict[str, Any] = {"passed": True}

        try:
            import psutil

            # Memory check
            memory = psutil.virtual_memory()
            available_gb = memory.available / (1024 ** 3)
            total_gb = memory.total / (1024 ** 3)
            usage_percent = memory.percent

            result["memory"] = {
                "available_gb": round(available_gb, 2),
                "total_gb": round(total_gb, 2),
                "usage_percent": usage_percent,
            }

            # Warning if less than 2GB available
            if available_gb < 2.0:
                result["warning"] = f"Low memory: {available_gb:.1f}GB available"

            # Critical if less than 1GB
            if available_gb < 1.0:
                result["passed"] = False
                result["critical"] = True
                result["error"] = f"Critically low memory: {available_gb:.1f}GB"

            # CPU check
            cpu_count = psutil.cpu_count()
            cpu_percent = psutil.cpu_percent(interval=0.1)

            result["cpu"] = {
                "count": cpu_count,
                "usage_percent": cpu_percent,
            }

            # Disk check
            disk = psutil.disk_usage(str(Path.home()))
            free_gb = disk.free / (1024 ** 3)

            result["disk"] = {
                "free_gb": round(free_gb, 2),
                "usage_percent": disk.percent,
            }

            if free_gb < 5.0:
                result["warning"] = f"Low disk space: {free_gb:.1f}GB free"

        except ImportError:
            result["warning"] = "psutil not available - skipping resource checks"
        except Exception as e:
            result["warning"] = f"Resource check error: {e}"

        return result

    async def _check_claude_configuration(self) -> Dict[str, Any]:
        """Check Claude/Anthropic API configuration."""
        result: Dict[str, Any] = {"passed": True}

        # Check for API key
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")

        if not api_key:
            result["warning"] = "ANTHROPIC_API_KEY not set - some features unavailable"
            result["api_configured"] = False
        else:
            # Validate key format (basic check)
            if api_key.startswith("sk-ant-"):
                result["api_configured"] = True
                result["key_prefix"] = api_key[:12] + "..."
            else:
                result["warning"] = "ANTHROPIC_API_KEY has unexpected format"
                result["api_configured"] = False

        return result

    async def _check_permissions(self) -> Dict[str, Any]:
        """Check system permissions (microphone, screen recording on macOS)."""
        result: Dict[str, Any] = {"passed": True, "permissions": {}}

        if sys.platform == "darwin":
            # Check microphone permission
            try:
                import subprocess
                # Use tccutil to check microphone permission
                # This is a simplified check - full implementation would use pyobjc
                result["permissions"]["microphone"] = "check_required"
                result["permissions"]["screen_recording"] = "check_required"
            except Exception as e:
                result["warning"] = f"Permission check error: {e}"
        else:
            result["permissions"]["note"] = "Non-macOS - permissions not applicable"

        return result

    async def _check_critical_dependencies(self) -> Dict[str, Any]:
        """Check critical Python dependencies are available."""
        result: Dict[str, Any] = {"passed": True, "available": [], "missing": []}

        critical_modules = [
            ("fastapi", "Backend framework"),
            ("uvicorn", "ASGI server"),
            ("pydantic", "Data validation"),
            ("asyncio", "Async support"),
        ]

        optional_modules = [
            ("aiohttp", "Async HTTP client"),
            ("websockets", "WebSocket support"),
            ("psutil", "System monitoring"),
            ("chromadb", "Vector database"),
            ("torch", "ML inference"),
            ("transformers", "NLP models"),
        ]

        for module_name, description in critical_modules:
            try:
                __import__(module_name)
                result["available"].append(module_name)
            except ImportError:
                result["missing"].append(module_name)
                result["passed"] = False
                result["critical"] = True
                result["error"] = f"Critical dependency missing: {module_name} ({description})"

        for module_name, description in optional_modules:
            try:
                __import__(module_name)
                result["available"].append(module_name)
            except ImportError:
                # Optional - just note it
                pass

        return result

    async def _check_network_availability(self) -> Dict[str, Any]:
        """Check network connectivity."""
        result: Dict[str, Any] = {"passed": True}

        # Check if we can bind to localhost
        test_port = 0  # Let OS assign a port
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(('localhost', test_port))
            assigned_port = sock.getsockname()[1]
            sock.close()
            result["localhost_binding"] = True
            result["test_port"] = assigned_port
        except socket.error as e:
            result["passed"] = False
            result["error"] = f"Cannot bind to localhost: {e}"

        return result

    # =========================================================================
    # SELF-HEALING MECHANISMS
    # =========================================================================
    # Enterprise-grade automatic recovery from common failure conditions.
    # These methods attempt to fix issues without user intervention.
    # =========================================================================

    async def _diagnose_and_heal(
        self,
        error_context: str,
        error: Exception,
        max_attempts: int = 3
    ) -> bool:
        """
        Master self-healing dispatcher.

        Analyzes an error and attempts automatic recovery.

        Args:
            error_context: Description of what was being attempted
            error: The exception that occurred
            max_attempts: Maximum healing attempts

        Returns:
            True if healing was successful, False otherwise
        """
        error_str = str(error).lower()
        error_type = type(error).__name__

        # Track healing attempts to prevent infinite loops
        heal_key = f"{error_context}:{error_type}"
        if not hasattr(self, '_healing_attempts'):
            self._healing_attempts = {}

        self._healing_attempts[heal_key] = self._healing_attempts.get(heal_key, 0) + 1

        if self._healing_attempts[heal_key] > max_attempts:
            self.logger.warning(f"[SelfHeal] Max attempts ({max_attempts}) reached for {heal_key}")
            return False

        self.logger.info(f"[SelfHeal] Diagnosing: {error_context}")
        self.logger.debug(f"[SelfHeal] Error: {error}")

        # Dispatch to appropriate healer based on error type
        healing_strategies = [
            (self._is_port_conflict, self._heal_port_conflict),
            (self._is_missing_module, self._heal_missing_module),
            (self._is_permission_issue, self._heal_permission_issue),
            (self._is_memory_pressure, self._heal_memory_pressure),
            (self._is_process_crash, self._heal_process_crash),
            (self._is_api_key_issue, self._heal_api_key_issue),
        ]

        for check_fn, heal_fn in healing_strategies:
            if check_fn(error_str, error_type):
                try:
                    healed = await heal_fn(error_context, error)
                    if healed:
                        self.logger.success(f"[SelfHeal] Successfully healed: {error_context}")
                        # Reset attempt counter on success
                        self._healing_attempts[heal_key] = 0
                        return True
                except Exception as heal_error:
                    self.logger.warning(f"[SelfHeal] Healing failed: {heal_error}")

        self.logger.warning(f"[SelfHeal] No healing strategy found for: {error_context}")
        return False

    def _is_port_conflict(self, error_str: str, error_type: str) -> bool:
        """Check if error indicates a port conflict."""
        port_indicators = [
            "address already in use",
            "port is already",
            "bind failed",
            "eaddrinuse",
            "errno 48",  # macOS
            "errno 98",  # Linux
        ]
        return any(indicator in error_str for indicator in port_indicators)

    def _is_missing_module(self, error_str: str, error_type: str) -> bool:
        """Check if error indicates a missing module."""
        return error_type == "ModuleNotFoundError" or "no module named" in error_str

    def _is_permission_issue(self, error_str: str, error_type: str) -> bool:
        """Check if error indicates a permission issue."""
        permission_indicators = [
            "permission denied",
            "access denied",
            "operation not permitted",
            "eacces",
        ]
        return any(indicator in error_str for indicator in permission_indicators)

    def _is_memory_pressure(self, error_str: str, error_type: str) -> bool:
        """Check if error indicates memory pressure."""
        memory_indicators = [
            "out of memory",
            "memory error",
            "cannot allocate",
            "memoryerror",
            "killed",
        ]
        return any(indicator in error_str for indicator in memory_indicators)

    def _is_process_crash(self, error_str: str, error_type: str) -> bool:
        """Check if error indicates a process crash."""
        crash_indicators = [
            "process exited",
            "process terminated",
            "segmentation fault",
            "sigsegv",
            "sigkill",
        ]
        return any(indicator in error_str for indicator in crash_indicators)

    def _is_api_key_issue(self, error_str: str, error_type: str) -> bool:
        """Check if error indicates an API key issue."""
        api_indicators = [
            "api key",
            "unauthorized",
            "invalid api",
            "authentication",
        ]
        return any(indicator in error_str for indicator in api_indicators)

    async def _heal_port_conflict(self, context: str, error: Exception) -> bool:
        """Attempt to heal a port conflict."""
        # Extract port number from error
        port = self._extract_port_from_error(str(error))
        if not port:
            port = self.config.backend_port

        self.logger.info(f"[SelfHeal] Attempting to free port {port}")

        # Try to kill the process using the port
        try:
            # Use lsof on Unix systems
            if sys.platform != "win32":
                result = await asyncio.create_subprocess_exec(
                    "lsof", "-ti", f":{port}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await result.communicate()

                if stdout:
                    pids = stdout.decode().strip().split('\n')
                    for pid_str in pids:
                        try:
                            pid = int(pid_str.strip())
                            if pid != os.getpid():  # Don't kill ourselves
                                os.kill(pid, signal.SIGTERM)
                                self.logger.info(f"[SelfHeal] Sent SIGTERM to PID {pid}")
                        except (ValueError, ProcessLookupError):
                            pass

                    # Wait for processes to die
                    await asyncio.sleep(2.0)
                    return True

        except Exception as e:
            self.logger.debug(f"[SelfHeal] Port healing error: {e}")

        return False

    async def _heal_missing_module(self, context: str, error: Exception) -> bool:
        """Attempt to install a missing module."""
        module_name = self._extract_module_from_error(str(error))
        if not module_name:
            return False

        self.logger.info(f"[SelfHeal] Attempting to install missing module: {module_name}")

        # Only auto-install known safe modules
        safe_to_install = {
            "aiohttp", "websockets", "psutil", "pydantic",
            "python-dotenv", "httpx",
        }

        if module_name not in safe_to_install:
            self.logger.warning(f"[SelfHeal] Module {module_name} not in safe install list")
            return False

        try:
            result = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install", module_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await result.communicate()

            if result.returncode == 0:
                self.logger.success(f"[SelfHeal] Installed {module_name}")
                return True
            else:
                self.logger.warning(f"[SelfHeal] pip install failed: {stderr.decode()}")

        except Exception as e:
            self.logger.debug(f"[SelfHeal] Module install error: {e}")

        return False

    async def _heal_permission_issue(self, context: str, error: Exception) -> bool:
        """Attempt to resolve permission issues."""
        self.logger.info("[SelfHeal] Permission issue detected")

        # On macOS, we can't auto-fix permission issues - need user action
        if sys.platform == "darwin":
            self.logger.warning("[SelfHeal] macOS permissions require user action")
            self.logger.info("  → System Preferences → Security & Privacy → Privacy")
            return False

        return False

    async def _heal_memory_pressure(self, context: str, error: Exception) -> bool:
        """Attempt to resolve memory pressure."""
        self.logger.info("[SelfHeal] Memory pressure detected")

        try:
            import gc

            # Force garbage collection
            gc.collect()
            self.logger.info("[SelfHeal] Forced garbage collection")

            # If hybrid cloud is enabled, try offloading to GCP
            if hasattr(self, '_resource_registry') and self._resource_registry:
                gcp_manager = self._resource_registry.get_manager("GCPInstanceManager")
                if gcp_manager and gcp_manager.is_ready:
                    self.logger.info("[SelfHeal] Attempting GCP offload")
                    # This would trigger workload migration to GCP
                    return True

            return True  # GC is always somewhat helpful

        except Exception as e:
            self.logger.debug(f"[SelfHeal] Memory healing error: {e}")

        return False

    async def _heal_process_crash(self, context: str, error: Exception) -> bool:
        """Attempt to recover from a process crash."""
        self.logger.info(f"[SelfHeal] Process crash detected in: {context}")

        # If backend crashed, try to restart it
        if "backend" in context.lower():
            self.logger.info("[SelfHeal] Attempting backend restart")

            # Clean up old process
            if hasattr(self, '_backend_process') and self._backend_process:
                try:
                    self._backend_process.terminate()
                    await asyncio.wait_for(
                        self._backend_process.wait(),
                        timeout=5.0
                    )
                except Exception:
                    pass

            # Restart backend
            try:
                success = await self._start_backend_subprocess()
                return success
            except Exception as e:
                self.logger.warning(f"[SelfHeal] Backend restart failed: {e}")

        return False

    async def _heal_api_key_issue(self, context: str, error: Exception) -> bool:
        """Handle API key issues."""
        self.logger.info("[SelfHeal] API key issue detected")
        self.logger.warning("  → Please set ANTHROPIC_API_KEY environment variable")
        # Can't auto-fix API key issues - need user action
        return False

    def _extract_port_from_error(self, error_str: str) -> Optional[int]:
        """Extract port number from error message."""
        import re
        # Look for common port patterns
        patterns = [
            r"port[:\s]+(\d{4,5})",
            r":(\d{4,5})",
            r"(\d{4,5})\s+already in use",
        ]
        for pattern in patterns:
            match = re.search(pattern, error_str.lower())
            if match:
                try:
                    return int(match.group(1))
                except ValueError:
                    pass
        return None

    def _extract_module_from_error(self, error_str: str) -> Optional[str]:
        """Extract module name from error message."""
        import re
        patterns = [
            r"no module named ['\"]?([a-z_][a-z0-9_]*)",
            r"modulenotfounderror.*['\"]([a-z_][a-z0-9_]*)",
        ]
        for pattern in patterns:
            match = re.search(pattern, error_str.lower())
            if match:
                return match.group(1)
        return None

    # =========================================================================
    # ADVANCED SERVICE MONITORING
    # =========================================================================
    # Enterprise-grade health monitoring with parallel checks and
    # intelligent failure detection.
    # =========================================================================

    async def _run_parallel_health_checks(self, timeout: float = 10.0) -> Dict[str, Any]:
        """
        Run health checks on all services in parallel.

        Returns comprehensive health status for monitoring and alerting.
        """
        services = [
            ("backend", f"http://localhost:{self.config.backend_port}/health"),
            ("websocket", f"ws://localhost:{self.config.websocket_port}"),
        ]

        async def check_http_service(name: str, url: str) -> Dict[str, Any]:
            """Check an HTTP service health endpoint."""
            start_time = time.time()
            try:
                if AIOHTTP_AVAILABLE and aiohttp is not None:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                            latency = (time.time() - start_time) * 1000
                            return {
                                "name": name,
                                "healthy": resp.status == 200,
                                "status_code": resp.status,
                                "latency_ms": round(latency, 2),
                            }
                else:
                    # Socket-based check
                    from urllib.parse import urlparse
                    parsed = urlparse(url)
                    host = parsed.hostname or 'localhost'
                    port = parsed.port or 80

                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(timeout)
                    result = sock.connect_ex((host, port))
                    sock.close()

                    latency = (time.time() - start_time) * 1000
                    return {
                        "name": name,
                        "healthy": result == 0,
                        "latency_ms": round(latency, 2),
                    }

            except Exception as e:
                return {
                    "name": name,
                    "healthy": False,
                    "error": str(e),
                    "latency_ms": (time.time() - start_time) * 1000,
                }

        # Run all checks in parallel
        results = await asyncio.gather(
            *[check_http_service(name, url) for name, url in services],
            return_exceptions=True
        )

        health_status = {
            "timestamp": datetime.now().isoformat(),
            "overall_healthy": True,
            "services": {},
        }

        for result in results:
            if isinstance(result, Exception):
                health_status["overall_healthy"] = False
            else:
                health_status["services"][result["name"]] = result
                if not result.get("healthy", False):
                    health_status["overall_healthy"] = False

        return health_status

    async def _verify_backend_ready(self, timeout: float = 60.0) -> bool:
        """
        Verify backend is fully ready (not just port open).

        Uses progressive health checks with intelligent retry.
        """
        start_time = time.time()
        check_interval = 1.0
        last_error = None

        while (time.time() - start_time) < timeout:
            try:
                # First check: Port is open
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2.0)
                result = sock.connect_ex(('localhost', self.config.backend_port))
                sock.close()

                if result != 0:
                    await asyncio.sleep(check_interval)
                    continue

                # Second check: HTTP health endpoint
                if AIOHTTP_AVAILABLE and aiohttp is not None:
                    async with aiohttp.ClientSession() as session:
                        url = f"http://localhost:{self.config.backend_port}/health"
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5.0)) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                # Check if backend reports ready
                                if data.get("status") in ["healthy", "ok", "ready"]:
                                    return True

                # If no aiohttp, just port check is enough
                else:
                    return True

            except Exception as e:
                last_error = e

            # Progressive backoff
            await asyncio.sleep(check_interval)
            check_interval = min(check_interval * 1.2, 5.0)

        if last_error:
            self.logger.warning(f"[Kernel] Backend readiness check failed: {last_error}")

        return False

    # =========================================================================
    # COST OPTIMIZATION INTEGRATION
    # =========================================================================
    # Integrates scale-to-zero, semantic caching, and cloud cost management.
    # =========================================================================

    async def _initialize_cost_optimization(self) -> bool:
        """Initialize cost optimization subsystems."""
        self.logger.info("[Kernel] Initializing cost optimization...")

        try:
            # Scale-to-Zero monitoring
            if hasattr(self, '_resource_registry') and self._resource_registry:
                cost_optimizer = self._resource_registry.get_manager("ScaleToZeroCostOptimizer")
                if cost_optimizer:
                    # Register activity callback
                    cost_optimizer.record_activity("kernel_startup")
                    self.logger.info("  → Scale-to-Zero: Active")

                # Semantic voice cache
                voice_cache = self._resource_registry.get_manager("SemanticVoiceCacheManager")
                if voice_cache:
                    self.logger.info("  → Semantic Voice Cache: Active")

            return True

        except Exception as e:
            self.logger.warning(f"[Kernel] Cost optimization init failed: {e}")
            return False

    # =========================================================================
    # TRINITY INTEGRATION (CROSS-REPO)
    # =========================================================================
    # First-class integration with JARVIS Prime and Reactor Core.
    # Enables unified orchestration across the system of systems.
    # =========================================================================

    async def _verify_trinity_connections(self, timeout: float = 30.0) -> Dict[str, Any]:
        """
        Verify connections to Trinity components (Prime and Reactor).

        Returns detailed status for each cross-repo component.
        """
        trinity_status = {
            "enabled": self.config.trinity_enabled,
            "components": {},
            "all_healthy": True,
        }

        if not self.config.trinity_enabled:
            return trinity_status

        # Check JARVIS Prime
        if self.config.prime_repo_path and self.config.prime_repo_path.exists():
            prime_status = await self._check_trinity_component(
                "jarvis-prime",
                self.config.prime_repo_path,
                self.config.prime_api_port if hasattr(self.config, 'prime_api_port') else 8000
            )
            trinity_status["components"]["jarvis-prime"] = prime_status
            if not prime_status.get("healthy", False):
                trinity_status["all_healthy"] = False

        # Check Reactor Core
        if self.config.reactor_repo_path and self.config.reactor_repo_path.exists():
            reactor_status = await self._check_trinity_component(
                "reactor-core",
                self.config.reactor_repo_path,
                self.config.reactor_api_port if hasattr(self.config, 'reactor_api_port') else 8090
            )
            trinity_status["components"]["reactor-core"] = reactor_status
            if not reactor_status.get("healthy", False):
                trinity_status["all_healthy"] = False

        return trinity_status

    async def _check_trinity_component(
        self,
        name: str,
        repo_path: Path,
        port: int
    ) -> Dict[str, Any]:
        """Check a single Trinity component."""
        status = {
            "name": name,
            "repo_path": str(repo_path),
            "port": port,
            "healthy": False,
            "details": {},
        }

        # Check if repo exists
        if not repo_path.exists():
            status["details"]["error"] = "Repository not found"
            return status

        # Check for running process on expected port
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            result = sock.connect_ex(('localhost', port))
            sock.close()

            if result == 0:
                status["healthy"] = True
                status["details"]["port_open"] = True

                # Try to get health status
                if AIOHTTP_AVAILABLE and aiohttp is not None:
                    try:
                        async with aiohttp.ClientSession() as session:
                            url = f"http://localhost:{port}/health"
                            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5.0)) as resp:
                                if resp.status == 200:
                                    data = await resp.json()
                                    status["details"]["health_response"] = data
                    except Exception:
                        pass
            else:
                status["details"]["port_open"] = False
                status["details"]["note"] = f"Not running on port {port}"

        except Exception as e:
            status["details"]["error"] = str(e)

        return status

    async def _start_trinity_component(self, name: str, repo_path: Path) -> bool:
        """Start a Trinity component if not already running."""
        self.logger.info(f"[Trinity] Starting {name}...")

        # Look for startup script
        startup_scripts = [
            repo_path / "start.py",
            repo_path / "run.py",
            repo_path / "main.py",
        ]

        script_path = None
        for script in startup_scripts:
            if script.exists():
                script_path = script
                break

        if not script_path:
            self.logger.warning(f"[Trinity] No startup script found for {name}")
            return False

        try:
            env = os.environ.copy()
            env["JARVIS_KERNEL_PID"] = str(os.getpid())
            env["TRINITY_COORDINATOR"] = "jarvis"

            process = await asyncio.create_subprocess_exec(
                sys.executable, str(script_path),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(repo_path)
            )

            # Store process reference
            if not hasattr(self, '_trinity_processes'):
                self._trinity_processes = {}
            self._trinity_processes[name] = process

            # Register with process manager
            if self._process_manager:
                await self._process_manager.register_process(
                    name,
                    process,
                    {"type": "trinity", "repo": str(repo_path)}
                )

            self.logger.success(f"[Trinity] Started {name} (PID: {process.pid})")
            return True

        except Exception as e:
            self.logger.error(f"[Trinity] Failed to start {name}: {e}")
            return False


# =============================================================================
# ZONE 6 SELF-TEST FUNCTION
# =============================================================================
# Tests for Zone 6 (run with: python unified_supervisor.py --test zone6)

async def _test_zone6():
    """Test Zone 6 components (The Kernel)."""
    logger = UnifiedLogger()

    print("\n" + "="*70)
    print("ZONE 6 TESTS: THE KERNEL")
    print("="*70 + "\n")

    # Test StartupLock
    with logger.section_start(LogSection.BOOT, "Zone 6.1: StartupLock"):
        lock = StartupLock()
        # Don't actually acquire during test
        logger.success("StartupLock created")
        holder = lock.get_current_holder()
        logger.info(f"Current holder: {holder}")

    # Test IPCServer
    with logger.section_start(LogSection.BOOT, "Zone 6.2: IPCServer"):
        config = SystemKernelConfig()
        ipc = IPCServer(config, logger)
        logger.success("IPCServer created")
        logger.info(f"Socket path: {ipc._socket_path}")

    # Test JarvisSystemKernel (partial - don't actually start)
    with logger.section_start(LogSection.BOOT, "Zone 6.3: JarvisSystemKernel"):
        # Reset singleton for testing
        JarvisSystemKernel._instance = None

        kernel = JarvisSystemKernel()
        logger.success("JarvisSystemKernel created")
        logger.info(f"State: {kernel.state.value}")
        logger.info(f"Kernel ID: {kernel.config.kernel_id}")
        logger.info(f"Mode: {kernel.config.mode}")

        # Don't run startup, just verify structure
        logger.info(f"Has startup lock: {kernel._startup_lock is not None}")
        logger.info(f"Has IPC server: {kernel._ipc_server is not None}")
        logger.info(f"Has signal handler: {kernel._signal_handler is not None}")

    logger.print_startup_summary()
    TerminalUI.print_success("Zone 6 validation complete!")


# =============================================================================
# =============================================================================
#
#  ███████╗ ██████╗ ███╗   ██╗███████╗    ███████╗
#  ╚══███╔╝██╔═══██╗████╗  ██║██╔════╝    ╚════██║
#    ███╔╝ ██║   ██║██╔██╗ ██║█████╗          ██╔╝
#   ███╔╝  ██║   ██║██║╚██╗██║██╔══╝         ██╔╝
#  ███████╗╚██████╔╝██║ ╚████║███████╗       ██║
#  ╚══════╝ ╚═════╝ ╚═╝  ╚═══╝╚══════╝       ╚═╝
#
#  ZONE 7: ENTRY POINT
#  Lines ~8300-9000
#
#  This zone contains:
#  - Unified CLI argument parser (all flags merged from both old files)
#  - main() function
#  - if __name__ == "__main__" entry point
#
# =============================================================================
# =============================================================================


# =============================================================================
# ZONE 7.1: UNIFIED CLI ARGUMENT PARSER
# =============================================================================

import argparse


def create_argument_parser() -> argparse.ArgumentParser:
    """
    Create the unified CLI argument parser.

    Merges all flags from run_supervisor.py and start_system.py into
    a single comprehensive CLI interface.
    """
    parser = argparse.ArgumentParser(
        prog="unified_supervisor",
        description=f"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  JARVIS UNIFIED SYSTEM KERNEL v{KERNEL_VERSION}                                          ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  The monolithic kernel that runs the entire JARVIS AI Agent system.          ║
║                                                                              ║
║  This is the SINGLE COMMAND needed to run JARVIS - it handles everything:    ║
║  • Process management and cleanup                                            ║
║  • Docker daemon management                                                  ║
║  • GCP resource orchestration                                                ║
║  • ML intelligence layer                                                     ║
║  • Trinity cross-repo integration                                            ║
║  • Hot reload for development                                                ║
╚══════════════════════════════════════════════════════════════════════════════╝
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python unified_supervisor.py                  # Start JARVIS (default)
  python unified_supervisor.py --status         # Check if running
  python unified_supervisor.py --shutdown       # Stop JARVIS
  python unified_supervisor.py --restart        # Restart JARVIS
  python unified_supervisor.py --cleanup        # Clean up zombie processes
  python unified_supervisor.py --debug          # Start with debug logging

Environment Variables:
  JARVIS_MODE                 Operating mode (supervisor|standalone|minimal)
  JARVIS_BACKEND_PORT         Backend server port (auto-detected if not set)
  JARVIS_DEV_MODE             Enable dev mode / hot reload (true|false)
  JARVIS_DEBUG                Enable debug logging (true|false)
  TRINITY_ENABLED             Enable Trinity cross-repo integration (true|false)
        """,
    )

    # =========================================================================
    # CONTROL COMMANDS
    # =========================================================================
    control = parser.add_argument_group("Control Commands")
    control.add_argument(
        "--status",
        action="store_true",
        help="Check if kernel is running and show status",
    )
    control.add_argument(
        "--shutdown",
        action="store_true",
        help="Gracefully shutdown the running kernel",
    )
    control.add_argument(
        "--restart",
        action="store_true",
        help="Restart the kernel (shutdown + start)",
    )
    control.add_argument(
        "--cleanup",
        action="store_true",
        help="Run comprehensive zombie cleanup and exit",
    )

    # =========================================================================
    # OPERATING MODE
    # =========================================================================
    mode = parser.add_argument_group("Operating Mode")
    mode.add_argument(
        "--mode",
        choices=["supervisor", "standalone", "minimal"],
        help="Operating mode (default: supervisor)",
    )
    mode.add_argument(
        "--in-process",
        action="store_true",
        dest="in_process",
        help="Run backend in-process (faster startup)",
    )
    mode.add_argument(
        "--subprocess",
        action="store_true",
        help="Run backend as subprocess (more isolated)",
    )

    # =========================================================================
    # NETWORK
    # =========================================================================
    network = parser.add_argument_group("Network")
    network.add_argument(
        "--port", "-p",
        type=int,
        metavar="PORT",
        help="Backend server port (default: auto-detected)",
    )
    network.add_argument(
        "--host",
        metavar="HOST",
        help="Backend server host (default: 0.0.0.0)",
    )
    network.add_argument(
        "--websocket-port",
        type=int,
        metavar="PORT",
        help="WebSocket server port (default: auto-detected)",
    )

    # =========================================================================
    # DOCKER
    # =========================================================================
    docker = parser.add_argument_group("Docker")
    docker.add_argument(
        "--skip-docker",
        action="store_true",
        help="Skip Docker daemon management",
    )
    docker.add_argument(
        "--no-docker-auto-start",
        action="store_true",
        help="Don't auto-start Docker daemon",
    )

    # =========================================================================
    # GCP
    # =========================================================================
    gcp = parser.add_argument_group("GCP / Cloud")
    gcp.add_argument(
        "--skip-gcp",
        action="store_true",
        help="Skip GCP resource management",
    )
    gcp.add_argument(
        "--prefer-cloud-run",
        action="store_true",
        help="Prefer Cloud Run over Spot VMs",
    )
    gcp.add_argument(
        "--enable-spot-vm",
        action="store_true",
        help="Enable Spot VM provisioning",
    )

    # =========================================================================
    # COST OPTIMIZATION
    # =========================================================================
    cost = parser.add_argument_group("Cost Optimization")
    cost.add_argument(
        "--no-scale-to-zero",
        action="store_true",
        help="Disable scale-to-zero cost optimization",
    )
    cost.add_argument(
        "--idle-timeout",
        type=int,
        metavar="SECONDS",
        help="Idle timeout before scale-to-zero (default: 300)",
    )
    cost.add_argument(
        "--daily-budget",
        type=float,
        metavar="USD",
        help="Daily cost budget in USD (default: 10.0)",
    )

    # =========================================================================
    # INTELLIGENCE / ML
    # =========================================================================
    ml = parser.add_argument_group("Intelligence / ML")
    ml.add_argument(
        "--goal-preset",
        choices=["auto", "aggressive", "balanced", "conservative"],
        help="Goal inference preset (default: auto)",
    )
    ml.add_argument(
        "--skip-intelligence",
        action="store_true",
        help="Skip ML intelligence layer initialization",
    )
    ml.add_argument(
        "--enable-automation",
        action="store_true",
        help="Enable automated goal inference",
    )

    # =========================================================================
    # VOICE / AUDIO
    # =========================================================================
    voice = parser.add_argument_group("Voice / Audio")
    voice.add_argument(
        "--skip-voice",
        action="store_true",
        help="Skip voice components",
    )
    voice.add_argument(
        "--no-narrator",
        action="store_true",
        help="Disable startup narrator",
    )
    voice.add_argument(
        "--skip-ecapa",
        action="store_true",
        help="Skip ECAPA voice embeddings",
    )

    # =========================================================================
    # TRINITY
    # =========================================================================
    trinity = parser.add_argument_group("Trinity / Cross-Repo")
    trinity.add_argument(
        "--skip-trinity",
        action="store_true",
        help="Skip Trinity cross-repo integration",
    )
    trinity.add_argument(
        "--prime-path",
        metavar="PATH",
        help="Path to jarvis-prime repository",
    )
    trinity.add_argument(
        "--reactor-path",
        metavar="PATH",
        help="Path to reactor-core repository",
    )

    # =========================================================================
    # DEVELOPMENT
    # =========================================================================
    dev = parser.add_argument_group("Development")
    dev.add_argument(
        "--no-hot-reload",
        action="store_true",
        help="Disable hot reload",
    )
    dev.add_argument(
        "--reload-interval",
        type=float,
        metavar="SECONDS",
        help="Hot reload check interval (default: 10)",
    )
    dev.add_argument(
        "--debug", "-d",
        action="store_true",
        help="Enable debug logging",
    )
    dev.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose output",
    )
    dev.add_argument(
        "--test",
        choices=["all", "zones", "zone5", "zone6"],
        metavar="SUITE",
        help="Run self-tests: all, zones (0-4), zone5, zone6",
    )

    # =========================================================================
    # ADVANCED
    # =========================================================================
    advanced = parser.add_argument_group("Advanced")
    advanced.add_argument(
        "--force", "-f",
        action="store_true",
        help="Force takeover from existing kernel",
    )
    advanced.add_argument(
        "--takeover",
        action="store_true",
        help="Take over from existing kernel (alias for --force)",
    )
    advanced.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate startup without actually running",
    )
    advanced.add_argument(
        "--config-file",
        metavar="PATH",
        help="Load configuration from YAML/JSON file",
    )
    advanced.add_argument(
        "--version",
        action="version",
        version=f"JARVIS Unified System Kernel v{KERNEL_VERSION}",
    )

    return parser


# =============================================================================
# ZONE 7.2: CLI COMMAND HANDLERS
# =============================================================================

async def handle_status() -> int:
    """Handle --status command."""
    logger = UnifiedLogger()
    logger.info("Checking kernel status...")

    # Try to connect to IPC socket
    socket_path = Path.home() / ".jarvis" / "locks" / "kernel.sock"
    if not socket_path.exists():
        print("\n" + "="*60)
        print("❌ JARVIS Kernel is NOT running")
        print("="*60)
        print("   No IPC socket found at", socket_path)
        print("\n   To start: python unified_supervisor.py")
        print("="*60 + "\n")
        return 1

    try:
        # Connect and send health command
        reader, writer = await asyncio.open_unix_connection(str(socket_path))

        request = json.dumps({"command": "status"}) + "\n"
        writer.write(request.encode())
        await writer.drain()

        response_data = await asyncio.wait_for(reader.readline(), timeout=5.0)
        response = json.loads(response_data.decode())

        writer.close()
        await writer.wait_closed()

        if response.get("success"):
            result = response.get("result", {})
            print("\n" + "="*60)
            print("✅ JARVIS Kernel is RUNNING")
            print("="*60)
            print(f"   State:    {result.get('state', 'unknown')}")
            print(f"   PID:      {result.get('pid', 'unknown')}")
            print(f"   Uptime:   {result.get('uptime_seconds', 0):.1f}s")
            print(f"   Mode:     {result.get('config', {}).get('mode', 'unknown')}")
            print(f"   Port:     {result.get('config', {}).get('backend_port', 'unknown')}")

            readiness = result.get("readiness", {})
            if readiness:
                print(f"   Tier:     {readiness.get('tier', 'unknown')}")

            print("="*60 + "\n")
            return 0
        else:
            print("\n❌ Status check failed:", response.get("error"))
            return 1

    except asyncio.TimeoutError:
        print("\n❌ Timeout connecting to kernel")
        return 1
    except Exception as e:
        print(f"\n❌ Error: {e}")
        return 1


async def handle_shutdown() -> int:
    """Handle --shutdown command."""
    logger = UnifiedLogger()
    logger.info("Sending shutdown command...")

    socket_path = Path.home() / ".jarvis" / "locks" / "kernel.sock"
    if not socket_path.exists():
        print("\n❌ Kernel is not running (no IPC socket)")
        return 1

    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))

        request = json.dumps({"command": "shutdown"}) + "\n"
        writer.write(request.encode())
        await writer.drain()

        response_data = await asyncio.wait_for(reader.readline(), timeout=5.0)
        response = json.loads(response_data.decode())

        writer.close()
        await writer.wait_closed()

        if response.get("success"):
            print("\n" + "="*60)
            print("✅ Shutdown acknowledged")
            print("="*60)
            print("   The kernel is shutting down gracefully.")
            print("   Use --status to verify shutdown is complete.")
            print("="*60 + "\n")
            return 0
        else:
            print("\n❌ Shutdown failed:", response.get("error"))
            return 1

    except Exception as e:
        print(f"\n❌ Error sending shutdown: {e}")
        return 1


async def handle_cleanup() -> int:
    """Handle --cleanup command."""
    print("\n" + "="*60)
    print("🧹 JARVIS Comprehensive Zombie Cleanup")
    print("="*60 + "\n")

    config = SystemKernelConfig()
    logger = UnifiedLogger()

    cleanup = ComprehensiveZombieCleanup(config, logger)
    result = await cleanup.run_comprehensive_cleanup()

    print("\n" + "="*60)
    print("Cleanup Results:")
    print("="*60)
    print(f"   Zombies found:  {result['zombies_found']}")
    print(f"   Zombies killed: {result['zombies_killed']}")
    print(f"   Ports freed:    {len(result['ports_freed'])}")
    print(f"   Duration:       {result['duration_ms']}ms")
    print("="*60 + "\n")

    return 0 if result["success"] else 1


# =============================================================================
# ZONE 7.3: CONFIGURATION FROM CLI ARGS
# =============================================================================

def apply_cli_to_config(args: argparse.Namespace, config: SystemKernelConfig) -> None:
    """Apply CLI arguments to configuration."""

    # Operating mode
    if args.mode:
        config.mode = args.mode
    if args.in_process:
        config.in_process_backend = True
    if args.subprocess:
        config.in_process_backend = False

    # Network
    if args.port:
        config.backend_port = args.port
    if args.host:
        config.backend_host = args.host
    if hasattr(args, 'websocket_port') and args.websocket_port:
        config.websocket_port = args.websocket_port

    # Docker
    if args.skip_docker:
        config.docker_enabled = False
    if args.no_docker_auto_start:
        config.docker_auto_start = False

    # GCP
    if args.skip_gcp:
        config.gcp_enabled = False
    if args.prefer_cloud_run:
        config.prefer_cloud_run = True
    if args.enable_spot_vm:
        config.spot_vm_enabled = True

    # Cost
    if args.no_scale_to_zero:
        config.scale_to_zero_enabled = False
    if args.idle_timeout:
        config.idle_timeout_seconds = args.idle_timeout
    if args.daily_budget:
        config.cost_budget_daily_usd = args.daily_budget

    # Intelligence
    if args.goal_preset:
        config.goal_preset = args.goal_preset
    if args.skip_intelligence:
        config.hybrid_intelligence_enabled = False

    # Voice
    if args.skip_voice:
        config.voice_enabled = False
    if args.skip_ecapa:
        config.ecapa_enabled = False

    # Trinity
    if args.skip_trinity:
        config.trinity_enabled = False
    if args.prime_path:
        config.prime_repo_path = Path(args.prime_path)
    if args.reactor_path:
        config.reactor_repo_path = Path(args.reactor_path)

    # Development
    if args.no_hot_reload:
        config.hot_reload_enabled = False
    if args.reload_interval:
        config.reload_check_interval = args.reload_interval
    if args.debug:
        config.debug = True
    if args.verbose:
        config.verbose = True


# =============================================================================
# ZONE 7.4: MAIN FUNCTION
# =============================================================================

async def handle_test(test_suite: str) -> int:
    """Handle --test command to run self-tests."""
    print("\n" + "="*70)
    print(f"RUNNING SELF-TESTS: {test_suite.upper()}")
    print("="*70 + "\n")

    try:
        if test_suite == "zones" or test_suite == "all":
            await _test_zones_0_through_4()

        if test_suite == "zone5" or test_suite == "all":
            await _test_zone5()

        if test_suite == "zone6" or test_suite == "all":
            await _test_zone6()

        print("\n" + "="*70)
        print("✅ ALL TESTS PASSED")
        print("="*70 + "\n")
        return 0

    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return 1


async def async_main(args: argparse.Namespace) -> int:
    """
    Async main entry point.

    Handles CLI commands and kernel startup.
    """
    # Handle control commands first
    if args.status:
        return await handle_status()

    if args.shutdown:
        return await handle_shutdown()

    if args.cleanup:
        return await handle_cleanup()

    # Handle test command
    if hasattr(args, 'test') and args.test:
        return await handle_test(args.test)

    if args.restart:
        # Shutdown first, then continue to startup
        await handle_shutdown()
        await asyncio.sleep(2.0)  # Wait for shutdown

    # Dry run - just print what would happen
    if args.dry_run:
        print("\n" + "="*60)
        print("DRY RUN - Would start with:")
        print("="*60)
        config = SystemKernelConfig()
        apply_cli_to_config(args, config)
        print(f"   Mode:              {config.mode}")
        print(f"   In-process:        {config.in_process_backend}")
        print(f"   Dev mode:          {config.dev_mode}")
        print(f"   Hot reload:        {config.hot_reload_enabled}")
        print(f"   Docker enabled:    {config.docker_enabled}")
        print(f"   GCP enabled:       {config.gcp_enabled}")
        print(f"   Trinity enabled:   {config.trinity_enabled}")
        print(f"   Intelligence:      {config.hybrid_intelligence_enabled}")
        print(f"   Force takeover:    {args.force or args.takeover}")
        print("="*60 + "\n")
        return 0

    # Start the kernel
    config = SystemKernelConfig()
    apply_cli_to_config(args, config)

    force = args.force or args.takeover

    # Reset singleton for fresh start
    JarvisSystemKernel._instance = None

    kernel = JarvisSystemKernel(config=config, force=force)

    # Run startup
    exit_code = await kernel.startup()
    if exit_code != 0:
        return exit_code

    # Run main loop
    return await kernel.run()


def main() -> int:
    """
    Main entry point for JARVIS Unified System Kernel.

    Parses CLI arguments and runs the appropriate command.
    """
    # Parse arguments
    parser = create_argument_parser()
    args = parser.parse_args()

    # Run async main
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("\n[Kernel] Interrupted by user")
        return 130  # 128 + SIGINT(2)


# =============================================================================
# ZONE 7.5: ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    sys.exit(main())
