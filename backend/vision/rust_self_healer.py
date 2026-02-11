"""
Rust Component Self-Healer for JARVIS.
Automatically diagnoses and fixes issues preventing Rust components from loading.
"""

import os
import sys
import asyncio
import subprocess
import logging
import json
import shutil
import multiprocessing
import importlib
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timedelta
from enum import Enum
import psutil
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor

# Import managed executor for clean shutdown
try:
    from core.thread_manager import ManagedThreadPoolExecutor
    _HAS_MANAGED_EXECUTOR = True
except ImportError:
    _HAS_MANAGED_EXECUTOR = False

# v95.12: Import multiprocessing cleanup tracker
try:
    from core.resilience.graceful_shutdown import register_executor_for_cleanup
    _HAS_MP_TRACKER = True
except ImportError:
    _HAS_MP_TRACKER = False
    def register_executor_for_cleanup(*args, **kwargs):
        pass  # No-op fallback

import fcntl
import aiofiles

logger = logging.getLogger(__name__)

# v237.0: File-lock guard for maturin/cargo builds.
# Prevents concurrent builds from corrupting the target directory.
_BUILD_LOCK_PATH = Path(__file__).parent / "jarvis-rust-core" / ".build.lock"


class _BuildLock:
    """
    Non-blocking, advisory file lock for Rust builds.

    Usage::

        async with _BuildLock() as acquired:
            if not acquired:
                logger.info("Build already in progress elsewhere")
                return
            # ... safe to build ...
    """

    def __init__(self, timeout: float = 0):
        self._fd: Optional[int] = None
        self._timeout = timeout

    async def __aenter__(self) -> bool:
        _BUILD_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(_BUILD_LOCK_PATH), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, BlockingIOError):
            # Another build holds the lock.
            if self._timeout > 0:
                # Wait with periodic retry.
                deadline = asyncio.get_event_loop().time() + self._timeout
                while asyncio.get_event_loop().time() < deadline:
                    await asyncio.sleep(2)
                    try:
                        fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        return True
                    except (OSError, BlockingIOError):
                        continue
            os.close(self._fd)
            self._fd = None
            return False

    async def __aexit__(self, *exc_info) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self._fd)
            self._fd = None

class RustIssueType(Enum):
    """Types of issues that can prevent Rust from working."""
    NOT_BUILT = "not_built"
    BUILD_FAILED = "build_failed"
    MISSING_DEPENDENCIES = "missing_dependencies"
    MISSING_PYPROJECT = "missing_pyproject"
    INCOMPATIBLE_VERSION = "incompatible_version"
    CORRUPTED_BINARY = "corrupted_binary"
    MISSING_RUSTUP = "missing_rustup"
    WRONG_TARGET = "wrong_target"
    PERMISSION_ERROR = "permission_error"
    OUT_OF_MEMORY = "out_of_memory"
    UNKNOWN = "unknown"

class FixStrategy(Enum):
    """Strategies for fixing Rust issues."""
    BUILD = "build"
    REBUILD = "rebuild"
    INSTALL_DEPS = "install_dependencies"
    INSTALL_RUST = "install_rust"
    CLEAN_BUILD = "clean_and_build"
    UPDATE_RUST = "update_rust"
    FIX_PERMISSIONS = "fix_permissions"
    FREE_MEMORY = "free_memory"
    RETRY_LATER = "retry_later"

class RustSelfHealer:
    """
    Automatically diagnoses and fixes Rust component issues.
    Monitors component health and attempts to restore functionality.
    """
    
    def __init__(self, check_interval: int = 300, max_retries: int = 3):
        """
        Initialize the self-healer.
        
        Args:
            check_interval: Seconds between health checks (default: 5 minutes)
            max_retries: Maximum fix attempts before giving up
        """
        self.check_interval = check_interval
        self.max_retries = max_retries
        self.vision_dir = Path(__file__).parent
        self.rust_core_dir = self.vision_dir / "jarvis-rust-core"
        self.backend_dir = self.vision_dir.parent
        self._rust_module_name = "jarvis_rust_core"
        self._required_symbols = ("RustAdvancedMemoryPool", "RustImageProcessor")

        self._running = False
        self._check_task: Optional[asyncio.Task] = None
        self._fix_history: List[Dict[str, Any]] = []
        self._retry_counts: Dict[RustIssueType, int] = {}
        self._last_successful_build: Optional[datetime] = None
        
        # Track whether we've already logged the stub message (to avoid spam)
        self._stub_logged: bool = False
        
        # Thread pools for concurrent operations
        if _HAS_MANAGED_EXECUTOR:
            self._thread_pool = ManagedThreadPoolExecutor(max_workers=4, name='rust_healer')
        else:
            self._thread_pool = ThreadPoolExecutor(max_workers=4)
        self._process_pool = ProcessPoolExecutor(max_workers=2)

        # v95.12: Register executors for cleanup
        register_executor_for_cleanup(self._thread_pool, "rust_healer_thread_pool")
        register_executor_for_cleanup(self._process_pool, "rust_healer_process_pool", is_process_pool=True)

    def _stub_module_file(self) -> Path:
        return self.rust_core_dir / "jarvis_rust_core.py"

    def _is_stub_module(self, module: Any) -> bool:
        if module is None:
            return False
        if getattr(module, "__rust_available__", None) is False:
            return True
        origin = getattr(module, "__file__", None)
        if origin is None:
            return False
        try:
            return Path(origin).resolve() == self._stub_module_file().resolve()
        except Exception:
            return False

    def _validate_module_symbols(self, module: Any) -> Tuple[bool, List[str]]:
        missing = [symbol for symbol in self._required_symbols if not hasattr(module, symbol)]
        return len(missing) == 0, missing

    def _strip_stub_path(self) -> List[Tuple[int, str]]:
        removed: List[Tuple[int, str]] = []
        stub_dir = str(self.rust_core_dir.resolve())
        for idx in range(len(sys.path) - 1, -1, -1):
            candidate = sys.path[idx]
            try:
                resolved = str(Path(candidate).resolve())
            except Exception:
                resolved = candidate
            if resolved == stub_dir:
                removed.append((idx, sys.path.pop(idx)))
        removed.reverse()
        return removed

    def _restore_sys_path(self, removed: List[Tuple[int, str]]) -> None:
        for idx, value in removed:
            if idx < 0 or idx > len(sys.path):
                sys.path.append(value)
            else:
                sys.path.insert(idx, value)

    def _import_real_rust_module(self) -> Tuple[Optional[Any], Optional[str]]:
        existing = sys.modules.get(self._rust_module_name)
        existing_is_stub = self._is_stub_module(existing)

        if existing is not None and not existing_is_stub:
            return existing, None

        if existing_is_stub:
            sys.modules.pop(self._rust_module_name, None)

        removed = self._strip_stub_path()
        try:
            module = importlib.import_module(self._rust_module_name)
            if self._is_stub_module(module):
                module_path = getattr(module, "__file__", "<unknown>")
                return None, f"stub module loaded from {module_path}"
            return module, None
        except ImportError as exc:
            return None, str(exc)
        finally:
            self._restore_sys_path(removed)
            if self._rust_module_name not in sys.modules and existing is not None:
                sys.modules[self._rust_module_name] = existing

    def _ensure_maturin_project(self) -> bool:
        """
        Ensure pyproject.toml exists for maturin-based installs.
        """
        pyproject_path = self.rust_core_dir / "pyproject.toml"
        if pyproject_path.exists():
            return True

        cargo_toml_path = self.rust_core_dir / "Cargo.toml"
        if not cargo_toml_path.exists():
            logger.error("Cannot create pyproject.toml: missing %s", cargo_toml_path)
            return False

        package_name = "jarvis-rust-core"
        package_version = "0.1.0"
        module_name = "jarvis_rust_core"

        try:
            import tomllib
            cargo_data = tomllib.loads(cargo_toml_path.read_text())
            package_name = cargo_data.get("package", {}).get("name", package_name)
            package_version = cargo_data.get("package", {}).get("version", package_version)
            module_name = cargo_data.get("lib", {}).get("name", module_name)
        except Exception as exc:
            logger.warning("Failed to parse Cargo.toml for pyproject synthesis: %s", exc)

        pyproject_content = (
            "[build-system]\n"
            "requires = [\"maturin>=1.4,<2.0\"]\n"
            "build-backend = \"maturin\"\n\n"
            "[project]\n"
            f"name = \"{package_name}\"\n"
            f"version = \"{package_version}\"\n"
            "description = \"JARVIS Rust core Python bindings\"\n"
            "requires-python = \">=3.8\"\n\n"
            "[tool.maturin]\n"
            f"module-name = \"{module_name}\"\n"
            "bindings = \"pyo3\"\n"
            "features = [\"python-bindings\", \"simd\"]\n"
        )

        try:
            pyproject_path.write_text(pyproject_content)
            logger.info("Created missing pyproject.toml at %s", pyproject_path)
            return True
        except Exception as exc:
            logger.error("Failed to create pyproject.toml at %s: %s", pyproject_path, exc)
            return False

    def _get_build_artifacts(self) -> List[Path]:
        patterns = [
            "jarvis_rust_core*.so",
            "jarvis_rust_core*.pyd",
            "jarvis_rust_core*.dylib",
            "libjarvis_rust_core*.so",
            "libjarvis_rust_core*.dylib",
        ]

        search_roots = [
            self.rust_core_dir / "target" / "release",
            self.rust_core_dir / "target" / "release" / "deps",
            self.rust_core_dir,
        ]

        artifacts: List[Path] = []
        for root in search_roots:
            if not root.exists():
                continue
            for pattern in patterns:
                artifacts.extend(root.glob(pattern))

        return [artifact for artifact in artifacts if artifact.exists()]
        
    async def start(self):
        """Start the self-healing system."""
        if self._running:
            return
            
        self._running = True
        logger.info("Rust self-healer started")
        
        # Do initial diagnosis
        await self.diagnose_and_fix()
        
        # Start periodic checks
        self._check_task = asyncio.create_task(self._periodic_check())
        
    async def stop(self):
        """v95.12: Stop the self-healing system with proper cleanup."""
        self._running = False

        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass

        # v95.12: Proper executor cleanup to prevent semaphore leaks
        executor_shutdown_timeout = 5.0

        # Shutdown thread pool
        try:
            await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._thread_pool.shutdown(wait=True, cancel_futures=True)
                ),
                timeout=executor_shutdown_timeout
            )
        except asyncio.TimeoutError:
            self._thread_pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

        # Shutdown process pool (critical for semaphore cleanup)
        try:
            await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._process_pool.shutdown(wait=True, cancel_futures=True)
                ),
                timeout=executor_shutdown_timeout
            )
        except asyncio.TimeoutError:
            self._process_pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

        logger.info("Rust self-healer stopped")
        
    async def _periodic_check(self):
        """Periodically check and fix Rust components."""
        while self._running:
            try:
                await asyncio.sleep(self.check_interval)
                
                if self._running:
                    # Check if Rust is working
                    if not await self._is_rust_working():
                        logger.info("Rust components not working, attempting to fix...")
                        await self.diagnose_and_fix()
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in periodic check: {e}")
                
    async def _is_rust_working(self) -> bool:
        """Check if Rust components are currently working."""
        loop = asyncio.get_event_loop()
        module, import_error = await loop.run_in_executor(
            self._thread_pool,
            self._import_real_rust_module,
        )

        if module is None:
            if self._stub_module_file().exists() and not self._stub_logged:
                logger.warning(
                    "Rust extension not active (stub detected). Import error: %s",
                    import_error or "unknown",
                )
                self._stub_logged = True
            return False

        valid, missing = self._validate_module_symbols(module)
        if not valid:
            logger.warning(
                "Rust module imported but missing required symbols: %s",
                ", ".join(missing),
            )
            return False

        self._stub_logged = False
        return True
        
    async def diagnose_and_fix(self) -> bool:
        """
        Diagnose issues and attempt to fix them.
        Returns True if fixed successfully.
        """
        # Optional escape hatch for explicitly running in stub-only mode.
        if os.getenv("JARVIS_ALLOW_RUST_STUB_ONLY", "").lower() == "true":
            if self._stub_module_file().exists():
                logger.info("Stub-only mode enabled via JARVIS_ALLOW_RUST_STUB_ONLY=true")
                return True

        logger.info("Diagnosing Rust component issues...")

        # Diagnose the issue
        issue_type, details = await self._diagnose_issue()

        if details.get("status") == "rust_module_loaded":
            logger.info("Rust module is already healthy; no fix required")
            return True
        
        logger.info(f"Diagnosed issue: {issue_type.value} - {details}")
        
        # Check retry limit
        retry_count = self._retry_counts.get(issue_type, 0)
        if retry_count >= self.max_retries:
            logger.warning(f"Max retries ({self.max_retries}) reached for {issue_type.value}")
            return False
            
        # Determine fix strategy
        strategy = self._determine_fix_strategy(issue_type, details)
        
        logger.info(f"Applying fix strategy: {strategy.value}")
        
        # Apply the fix
        success = await self._apply_fix(strategy, issue_type, details)
        
        # Update retry count
        if success:
            self._retry_counts[issue_type] = 0
            self._last_successful_build = datetime.now()
            logger.info("✅ Fix applied successfully!")
        else:
            self._retry_counts[issue_type] = retry_count + 1
            logger.warning(f"Fix failed, retry count: {self._retry_counts[issue_type]}")
            
        # Record in history
        self._fix_history.append({
            'timestamp': datetime.now(),
            'issue': issue_type.value,
            'strategy': strategy.value,
            'success': success,
            'details': details
        })
        
        # Cleanup old history (keep last 100 entries)
        if len(self._fix_history) > 100:
            self._fix_history = self._fix_history[-100:]
            
        return success
        
    async def _diagnose_issue(self) -> Tuple[RustIssueType, Dict[str, Any]]:
        """Diagnose what's preventing Rust from working."""
        details = {}
        
        # Check if Rust is installed
        if not await self._is_rust_installed():
            return RustIssueType.MISSING_RUSTUP, {'error': 'Rust not installed'}

        # Ensure maturin metadata exists for Python installation.
        pyproject_path = self.rust_core_dir / "pyproject.toml"
        if not pyproject_path.exists():
            return RustIssueType.MISSING_PYPROJECT, {'error': 'Missing pyproject.toml for maturin'}

        # Check if a working module is already importable.
        loop = asyncio.get_event_loop()
        module, import_error = await loop.run_in_executor(
            self._thread_pool,
            self._import_real_rust_module,
        )
        if module is not None:
            valid, missing = self._validate_module_symbols(module)
            if valid:
                return RustIssueType.UNKNOWN, {'status': 'rust_module_loaded'}
            return RustIssueType.INCOMPATIBLE_VERSION, {'missing_components': missing}
            
        # Check if target directory exists
        target_dir = self.rust_core_dir / "target"
        if not target_dir.exists():
            return RustIssueType.NOT_BUILT, {'error': 'Never built'}
            
        # Check for library file
        lib_path = self._get_library_path()
        if not lib_path:
            # Check if there's a build log
            build_log = self.rust_core_dir / "build.log"
            if build_log.exists():
                # Analyze build log
                log_content = build_log.read_text()
                if "error[E0463]" in log_content or "can't find crate" in log_content:
                    missing_crates = self._extract_missing_crates(log_content)
                    return RustIssueType.MISSING_DEPENDENCIES, {'missing_crates': missing_crates}
                elif "error: could not compile" in log_content:
                    return RustIssueType.BUILD_FAILED, {'log': log_content[-1000:]}  # Last 1000 chars
                    
            return RustIssueType.NOT_BUILT, {'error': 'Library file missing'}
            
        # Import still failing despite artifacts; classify by import error.
        error_msg = import_error or "Rust module exists but import failed"
        if "symbol not found" in error_msg:
            return RustIssueType.INCOMPATIBLE_VERSION, {'error': error_msg}
        if "Permission denied" in error_msg:
            return RustIssueType.PERMISSION_ERROR, {'file': lib_path}
        if "image not found" in error_msg or "Library not loaded" in error_msg:
            return RustIssueType.CORRUPTED_BINARY, {'error': error_msg}
        return RustIssueType.UNKNOWN, {'error': error_msg}
                
    def _determine_fix_strategy(self, issue: RustIssueType, details: Dict[str, Any]) -> FixStrategy:
        """Determine the best fix strategy for an issue."""
        strategy_map = {
            RustIssueType.NOT_BUILT: FixStrategy.BUILD,
            RustIssueType.BUILD_FAILED: FixStrategy.CLEAN_BUILD,
            RustIssueType.MISSING_DEPENDENCIES: FixStrategy.INSTALL_DEPS,
            RustIssueType.MISSING_PYPROJECT: FixStrategy.BUILD,
            RustIssueType.INCOMPATIBLE_VERSION: FixStrategy.REBUILD,
            RustIssueType.CORRUPTED_BINARY: FixStrategy.CLEAN_BUILD,
            RustIssueType.MISSING_RUSTUP: FixStrategy.INSTALL_RUST,
            RustIssueType.WRONG_TARGET: FixStrategy.REBUILD,
            RustIssueType.PERMISSION_ERROR: FixStrategy.FIX_PERMISSIONS,
            RustIssueType.OUT_OF_MEMORY: FixStrategy.FREE_MEMORY,
            RustIssueType.UNKNOWN: FixStrategy.CLEAN_BUILD
        }
        
        # Check if we have enough memory for building
        available_memory = psutil.virtual_memory().available / (1024**3)  # GB
        if available_memory < 2.0:  # Need at least 2GB for Rust builds
            return FixStrategy.FREE_MEMORY
            
        return strategy_map.get(issue, FixStrategy.RETRY_LATER)
        
    async def _apply_fix(self, strategy: FixStrategy, issue: RustIssueType, details: Dict[str, Any]) -> bool:
        """Apply a fix strategy."""
        try:
            if strategy == FixStrategy.BUILD:
                return await self._build_rust_components()
                
            elif strategy == FixStrategy.REBUILD:
                # Clean and build concurrently where possible
                await self._clean_build_artifacts()
                return await self._build_rust_components()
                
            elif strategy == FixStrategy.CLEAN_BUILD:
                # Run cleanup tasks concurrently
                await asyncio.gather(
                    self._clean_build_artifacts(),
                    self._reset_cargo_cache(),
                    return_exceptions=True
                )
                return await self._build_rust_components()
                
            elif strategy == FixStrategy.INSTALL_DEPS:
                missing_crates = details.get('missing_crates', [])
                await self._install_missing_crates(missing_crates)
                return await self._build_rust_components()
                
            elif strategy == FixStrategy.INSTALL_RUST:
                if await self._install_rust():
                    return await self._build_rust_components()
                return False
                
            elif strategy == FixStrategy.UPDATE_RUST:
                if await self._update_rust():
                    return await self._build_rust_components()
                return False
                
            elif strategy == FixStrategy.FIX_PERMISSIONS:
                lib_path = details.get('file')
                if lib_path and await self._fix_permissions(lib_path):
                    return True
                return False
                
            elif strategy == FixStrategy.FREE_MEMORY:
                await self._free_memory()
                # Brief pause for memory to be freed
                await asyncio.sleep(1)
                return await self._build_rust_components()
                
            elif strategy == FixStrategy.RETRY_LATER:
                # Just wait and let the periodic check try again
                return False
                
            else:
                logger.warning(f"Unknown fix strategy: {strategy}")
                return False
                
        except Exception as e:
            logger.error(f"Error applying fix {strategy.value}: {e}")
            return False
            
    async def _is_rust_installed(self) -> bool:
        """Check if Rust is installed."""
        try:
            result = await self._run_command(["rustc", "--version"])
            return result.returncode == 0
        except FileNotFoundError:
            return False
            
    async def _install_rust(self) -> bool:
        """Install Rust using rustup."""
        logger.info("Installing Rust...")
        
        # Download and run rustup
        if sys.platform == "win32":
            # Windows
            installer_url = "https://win.rustup.rs"
            installer_path = "rustup-init.exe"
        else:
            # Unix-like (macOS, Linux)
            installer_url = "https://sh.rustup.rs"
            installer_path = "rustup-init.sh"
            
        try:
            # Download installer
            import urllib.request
            urllib.request.urlretrieve(installer_url, installer_path)
            
            if sys.platform != "win32":
                # Make executable on Unix
                os.chmod(installer_path, 0o755)
                
            # Run installer
            cmd = [installer_path, "-y", "--default-toolchain", "stable"]
            if sys.platform != "win32":
                cmd = ["sh", installer_path, "-y", "--default-toolchain", "stable"]
                
            result = await self._run_command(cmd)
            
            # Cleanup
            os.remove(installer_path)
            
            return result.returncode == 0
            
        except Exception as e:
            logger.error(f"Failed to install Rust: {e}")
            return False
            
    async def _update_rust(self) -> bool:
        """Update Rust to latest stable."""
        logger.info("Updating Rust...")
        result = await self._run_command(["rustup", "update", "stable"])
        return result.returncode == 0
        
    async def _build_rust_components(self, retry_count: int = 0, max_retries: int = 3) -> bool:
        """Build Rust components with concurrent/async execution for faster startup.
        Runs multiple build strategies in parallel and uses the first successful one.
        """
        logger.info("Building Rust components with concurrent strategies...")
        
        # Skip if environment says to
        if os.environ.get('JARVIS_SKIP_RUST_BUILD', '').lower() == 'true':
            logger.info("Skipping Rust build (JARVIS_SKIP_RUST_BUILD=true)")
            return False

        # Ensure maturin metadata is present so `maturin develop` can succeed.
        if not self._ensure_maturin_project():
            logger.error("Cannot build Rust components without pyproject.toml")
            return False

        # v237.0: Acquire file-lock to prevent concurrent maturin/cargo builds.
        # Replaces the racy psutil.process_iter() check with a proper advisory lock.
        async with _BuildLock(timeout=120) as acquired:
            if not acquired:
                logger.info("Another Rust build is in progress (lock held); skipping")
                # The other build may succeed — validate before giving up.
                await asyncio.sleep(5)
                return await self._quick_validate_rust()

            return await self._build_rust_components_locked(retry_count, max_retries)

    async def _build_rust_components_locked(self, retry_count: int, max_retries: int) -> bool:
        """Inner build logic — must be called with _BuildLock held."""
        # Check if we can do a quick validation first
        if await self._quick_validate_rust():
            logger.info("✅ Rust components already built and working!")
            return True

        # Prepare multiple build strategies to run concurrently
        build_strategies = [
            self._try_cached_build(),
            self._try_incremental_build(),
            self._try_full_build() if retry_count > 0 else None
        ]
        
        # Filter out None strategies
        build_strategies = [s for s in build_strategies if s is not None]
        
        # Run build strategies concurrently
        logger.info(f"Running {len(build_strategies)} build strategies concurrently...")
        
        try:
            # Create tasks for all strategies
            tasks = [asyncio.create_task(strategy) for strategy in build_strategies]
            build_succeeded = False

            for completed in asyncio.as_completed(tasks):
                try:
                    if await completed:
                        build_succeeded = True
                        break
                except Exception as e:
                    logger.debug(f"Build strategy failed: {e}")

            if build_succeeded:
                # Cancel stragglers once we have a success signal.
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

                if await self._quick_validate_rust():
                    logger.info("✅ Build successful with concurrent strategy!")
                    self._last_successful_build = datetime.now()
                    return True

                logger.warning("Build reported success but module validation failed")
            else:
                await asyncio.gather(*tasks, return_exceptions=True)

            # If we get here, all strategies failed
            if retry_count < max_retries - 1:
                logger.info(f"All strategies failed, retrying ({retry_count + 1}/{max_retries})...")
                await asyncio.sleep(1)
                return await self._build_rust_components(retry_count + 1, max_retries)
            
        except Exception as e:
            logger.error(f"Concurrent build error: {e}")
        
        logger.error(f"Build failed after all strategies")
        return False
    
    async def _quick_validate_rust(self) -> bool:
        """Quick validation to check if Rust is already working."""
        try:
            # Try quick import test
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                self._thread_pool,
                self._test_rust_import
            )
            return result
        except Exception:
            return False

    def _test_rust_import(self) -> bool:
        """Test if we can import Rust components."""
        module, _ = self._import_real_rust_module()
        if module is None:
            return False
        valid, _missing = self._validate_module_symbols(module)
        return valid
    
    async def _try_cached_build(self) -> bool:
        """Try to use cached build if available."""
        logger.info("Strategy 1: Checking for cached build...")
        
        # Check if recent build exists
        lib_path = self._get_library_path()
        if lib_path:
            # Check modification time
            mtime = datetime.fromtimestamp(lib_path.stat().st_mtime)
            if datetime.now() - mtime < timedelta(hours=24):
                logger.info("Found recent cached build")
                
                # Try maturin develop to ensure Python bindings
                result = await self._run_command(
                    ["maturin", "develop", "--release"],
                    cwd=str(self.rust_core_dir),
                    timeout=120
                )
                if result.returncode == 0 and await self._quick_validate_rust():
                    logger.info("✅ Cached build validated")
                    return True
        
        return False
    
    async def _try_incremental_build(self) -> bool:
        """Try incremental build (faster)."""
        logger.info("Strategy 2: Attempting incremental build...")
        
        env = os.environ.copy()
        env['CARGO_BUILD_JOBS'] = str(multiprocessing.cpu_count())
        env['CARGO_INCREMENTAL'] = '1'
        env['CARGO_TERM_PROGRESS_WHEN'] = 'always'
        env['CARGO_TERM_PROGRESS_WIDTH'] = '80'
        
        # First try cargo check to validate
        logger.info("Running cargo check to validate dependencies...")
        check_result = await self._run_command(
            ["cargo", "check", "--release"],
            cwd=str(self.rust_core_dir),
            capture_output=True,
            env=env,
            timeout=120
        )
        
        if check_result.returncode != 0:
            # Check failed, might need dependencies
            if "error[E0463]" in check_result.stderr:
                missing = self._extract_missing_crates(check_result.stderr)
                if missing:
                    logger.info(f"Installing missing crates: {missing}")
                    await self._install_missing_crates(missing)
            else:
                logger.warning("Cargo check failed, skipping incremental build")
                return False
        
        # Now build
        logger.info(f"Building with {env['CARGO_BUILD_JOBS']} parallel jobs...")
        result = await self._run_command(
            ["cargo", "build", "--release", "--features", "python-bindings,simd"],
            cwd=str(self.rust_core_dir),
            capture_output=True,
            env=env,
            timeout=900  # 15 minutes for incremental build
        )
        
        if result.returncode == 0:
            # Save build log asynchronously
            asyncio.create_task(self._save_build_log(result.stdout + "\n" + result.stderr))
            
            # Run maturin
            maturin_result = await self._run_command(
                ["maturin", "develop", "--release"],
                cwd=str(self.rust_core_dir),
                timeout=120
            )
            return maturin_result.returncode == 0 and await self._quick_validate_rust()
        
        return False
    
    async def _try_full_build(self) -> bool:
        """Try full clean build (slower but more reliable)."""
        logger.info("Strategy 3: Attempting full clean build...")
        
        # Clean in parallel with prep work
        clean_task = asyncio.create_task(self._clean_build_artifacts())
        
        # Prepare environment
        env = os.environ.copy()
        env['CARGO_BUILD_JOBS'] = str(multiprocessing.cpu_count())
        env['CARGO_INCREMENTAL'] = '0'  # Disable incremental for clean build
        
        # Wait for clean to finish
        await clean_task
        
        # Full build
        result = await self._run_command(
            ["cargo", "build", "--release", "--features", "python-bindings,simd"],
            cwd=str(self.rust_core_dir),
            capture_output=True,
            env=env,
            timeout=600
        )
        
        if result.returncode == 0:
            # Save build log asynchronously
            asyncio.create_task(self._save_build_log(result.stdout + "\n" + result.stderr))
            
            # Run maturin
            maturin_result = await self._run_command(
                ["maturin", "develop", "--release"],
                cwd=str(self.rust_core_dir),
                timeout=120
            )
            return maturin_result.returncode == 0 and await self._quick_validate_rust()
        
        return False
    
    async def _save_build_log(self, content: str):
        """Save build log asynchronously."""
        try:
            build_log = self.rust_core_dir / "build.log"
            async with aiofiles.open(build_log, 'w') as f:
                await f.write(content)
        except Exception as e:
            logger.debug(f"Failed to save build log: {e}")
            
    async def _clean_build_artifacts(self):
        """Clean build artifacts."""
        logger.info("Cleaning build artifacts...")
        
        target_dir = self.rust_core_dir / "target"
        if target_dir.exists():
            shutil.rmtree(target_dir)
            
        # Remove any .so/.dylib/.dll files
        for pattern in ["*.so", "*.dylib", "*.dll", "*.pyd"]:
            for file in self.rust_core_dir.glob(pattern):
                file.unlink()
                
    async def _reset_cargo_cache(self):
        """Reset Cargo cache for this project."""
        logger.info("Resetting Cargo cache...")
        
        # Remove Cargo.lock
        cargo_lock = self.rust_core_dir / "Cargo.lock"
        if cargo_lock.exists():
            cargo_lock.unlink()
            
    async def _install_missing_crates(self, crates: List[str]):
        """Install missing crates with automatic version resolution."""
        if not crates:
            return
            
        logger.info(f"Installing missing crates: {crates}")
        
        # Add to Cargo.toml if needed
        cargo_toml = self.rust_core_dir / "Cargo.toml"
        if cargo_toml.exists():
            try:
                import toml
            except ImportError:
                # Install toml if not available
                await self._run_command([sys.executable, "-m", "pip", "install", "toml"])
                import toml
            
            # Load existing Cargo.toml
            cargo_data = toml.load(cargo_toml)
            
            # Common crate versions that work well together
            crate_versions = {
                'pyo3': '0.20',
                'numpy': '0.20',
                'ndarray': '0.15',
                'rayon': '1.8',
                'serde': '1.0',
                'serde_json': '1.0',
                'tokio': '1.35',
                'async-trait': '0.1',
                'anyhow': '1.0',
                'thiserror': '1.0',
                'log': '0.4',
                'env_logger': '0.10',
                'crossbeam': '0.8',
                'parking_lot': '0.12',
                'once_cell': '1.19',
                'metal': '0.27',
                'objc': '0.2',
                'cocoa': '0.25',
                'core-foundation': '0.9',
                'fnv': '1.0',
                'ordered-float': '4.2',
                'nalgebra-sparse': '0.9',
                'maturin': '1.4'
            }
            
            # Ensure dependencies section exists
            if 'dependencies' not in cargo_data:
                cargo_data['dependencies'] = {}
            
            modified = False
            for crate in crates:
                if crate not in cargo_data['dependencies']:
                    # Add with known good version or latest
                    version = crate_versions.get(crate, '*')
                    cargo_data['dependencies'][crate] = version
                    logger.info(f"Adding {crate} = \"{version}\" to Cargo.toml")
                    modified = True
            
            if modified:
                # Write back to Cargo.toml
                with open(cargo_toml, 'w') as f:
                    toml.dump(cargo_data, f)
                logger.info("Updated Cargo.toml with missing dependencies")
                    
    def _extract_missing_crates(self, build_log: str) -> List[str]:
        """Extract missing crate names from build log."""
        import re
        
        # Pattern to match missing crates
        pattern = r"can't find crate for `(\w+)`"
        matches = re.findall(pattern, build_log)
        
        return list(set(matches))
        
    async def _fix_permissions(self, file_path: Path) -> bool:
        """Fix file permissions."""
        logger.info(f"Fixing permissions for {file_path}")
        
        try:
            # Make readable and executable
            os.chmod(file_path, 0o755)
            return True
        except Exception as e:
            logger.error(f"Failed to fix permissions: {e}")
            return False
            
    async def _free_memory(self):
        """Try to free up memory for building."""
        logger.info("Attempting to free memory...")
        
        # Clear Python caches
        import gc
        gc.collect()
        
        # Clear system caches (macOS specific)
        if sys.platform == "darwin":
            await self._run_command(["sudo", "purge"], check=False)
            
    def _get_library_path(self) -> Optional[Path]:
        """Get the newest known build artifact path."""
        artifacts = self._get_build_artifacts()
        if not artifacts:
            return None
        return max(artifacts, key=lambda p: p.stat().st_mtime)
        
    async def _run_command(self, cmd: List[str], **kwargs) -> subprocess.CompletedProcess:
        """Run a command asynchronously."""
        if 'capture_output' not in kwargs:
            kwargs['capture_output'] = True
        if 'text' not in kwargs:
            kwargs['text'] = True
            
        # Get timeout from kwargs or set default
        timeout = kwargs.get('timeout', 300)  # 5 minutes default
        if timeout is None and ('cargo' in cmd[0] or 'maturin' in cmd[0] or 'build' in str(cmd)):
            timeout = 1200  # 20 minutes for build commands (first build is slow)
        
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE if kwargs.get('capture_output') else None,
            stderr=asyncio.subprocess.PIPE if kwargs.get('capture_output') else None,
            cwd=kwargs.get('cwd'),
            env=kwargs.get('env'),
        )
        
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise Exception(f"Command timed out after {timeout} seconds: {' '.join(cmd)}")
        
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=proc.returncode,
            stdout=stdout.decode() if stdout else "",
            stderr=stderr.decode() if stderr else ""
        )
        
    def get_health_report(self) -> Dict[str, Any]:
        """Get a health report of the self-healing system."""
        # Recent fixes
        recent_fixes = self._fix_history[-10:] if self._fix_history else []
        
        # Success rate
        if self._fix_history:
            success_count = sum(1 for f in self._fix_history if f['success'])
            success_rate = success_count / len(self._fix_history)
        else:
            success_rate = 0.0
            
        return {
            'running': self._running,
            'last_successful_build': self._last_successful_build.isoformat() if self._last_successful_build else None,
            'retry_counts': dict(self._retry_counts),
            'recent_fixes': recent_fixes,
            'total_fix_attempts': len(self._fix_history),
            'success_rate': success_rate,
            'is_rust_working': asyncio.create_task(self._is_rust_working())
        }


# Global instance
_self_healer: Optional[RustSelfHealer] = None

def get_self_healer() -> RustSelfHealer:
    """Get the global self-healer instance."""
    global _self_healer
    if _self_healer is None:
        _self_healer = RustSelfHealer()
    return _self_healer

async def start_self_healing():
    """Start the self-healing system."""
    healer = get_self_healer()
    await healer.start()
    return healer
