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


# ╔═══════════════════════════════════════════════════════════════════════════════╗
# ║                                                                               ║
# ║   END OF ZONE 3                                                               ║
# ║   Zones 4-7 will be added in subsequent commits                               ║
# ║                                                                               ║
# ╚═══════════════════════════════════════════════════════════════════════════════╝

# Placeholder for remaining zones
# ZONE 4: Intelligence Layer (routing, goal inference, SAI)
# ZONE 5: Process Orchestration (signals, cleanup, hot reload, Trinity)
# ZONE 6: The Kernel (JarvisSystemKernel class)
# ZONE 7: Entry Point (CLI, main)

if __name__ == "__main__":
    import asyncio

    async def test_zones():
        """Test Zones 0-3."""
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
        lock = StartupLock("test")
        is_locked, holder = lock.is_locked()
        logger.success(f"Lock status: locked={is_locked}, holder={holder}")

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

        logger.print_startup_summary()
        TerminalUI.print_success("Zone 3 validation complete!")

    # Run async tests
    asyncio.run(test_zones())
