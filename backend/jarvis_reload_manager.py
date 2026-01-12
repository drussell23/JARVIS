#!/usr/bin/env python3
"""
JARVIS Intelligent Reload Manager
==================================
Monitors code changes and automatically reloads JARVIS with updated code.
No hardcoding - uses dynamic discovery and intelligent restart.
"""

import os
import sys
import time
import hashlib
import pickle
import asyncio
import signal
import psutil
import logging
from pathlib import Path
from typing import Dict, Set, Optional, List, Tuple
from datetime import datetime, timedelta
import subprocess
import json

logger = logging.getLogger(__name__)


class JARVISReloadManager:
    """Intelligent reload manager for JARVIS that ensures latest code is always running"""

    def __init__(self):
        self.repo_root = Path(__file__).parent.parent  # JARVIS-AI-Agent root
        self.backend_dir = self.repo_root / "backend"
        self.cache_dir = self.backend_dir / ".jarvis_cache"
        self.cache_dir.mkdir(exist_ok=True)

        # File tracking
        self.code_hash_file = self.cache_dir / "code_hashes.pkl"
        self.last_reload_file = self.cache_dir / "last_reload.json"
        self.config_file = self.cache_dir / "jarvis_config.json"

        # ═══════════════════════════════════════════════════════════════════
        # STARTUP GRACE PERIOD - Critical for preventing reload during init
        # ═══════════════════════════════════════════════════════════════════
        self._startup_time = time.time()
        self._startup_grace_period = int(os.getenv("JARVIS_RELOAD_GRACE_PERIOD", "120"))  # 2 minutes default
        self._startup_complete = False
        self._grace_period_logged = False

        # Monitoring configuration
        self.watch_patterns = [
            "**/*.py",           # Python files
            "**/*.yaml",         # Config files
            "**/*.yml",
        ]

        # ═══════════════════════════════════════════════════════════════════
        # COLD-RESTART FILES - These require full restart, not hot-reload
        # Changes to these files are logged but don't trigger automatic restart
        # ═══════════════════════════════════════════════════════════════════
        self.cold_restart_patterns = [
            "**/requirements*.txt",  # Dependencies require pip install
            "**/*.env",              # Environment changes need full restart
            "**/setup.py",           # Package changes need reinstall
            "**/pyproject.toml",     # Package changes need reinstall
        ]

        # Exclude patterns
        self.exclude_patterns = [
            "**/.*",             # Hidden files/dirs
            "**/__pycache__/**", # Python cache
            "**/node_modules/**", # Node modules
            "**/venv/**",        # Virtual environments
            "**/env/**",
            "**/*.pyc",          # Compiled Python
            "**/*.log",          # Log files
            "**/logs/**",
            "**/cache/**",
            "**/.git/**",        # Git directory
            "**/build/**",       # Build directories
            "**/dist/**",
            "**/*.json",         # JSON files (config, not code)
        ]

        # Process management
        self.jarvis_process: Optional[asyncio.subprocess.Process] = None
        self.monitor_task: Optional[asyncio.Task] = None
        self.restart_cooldown = 10  # Increased cooldown for stability
        self.last_restart_time = 0

        # Dynamic configuration
        self.config = self.load_config()

    def is_in_startup_grace_period(self) -> bool:
        """Check if we're still in the startup grace period.

        During this period, file change detection is active but restarts
        are suppressed to allow the system to fully initialize.
        """
        # Check environment variable from supervisor
        if os.getenv("JARVIS_STARTUP_COMPLETE", "").lower() == "true":
            self._startup_complete = True
            return False

        # Check time-based grace period
        elapsed = time.time() - self._startup_time
        if elapsed >= self._startup_grace_period:
            if not self._grace_period_logged:
                logger.info(f"⏰ Startup grace period ended after {elapsed:.0f}s - hot-reload now active")
                self._grace_period_logged = True
            return False

        return True

    def is_cold_restart_file(self, filepath: str) -> bool:
        """Check if a file requires cold restart (not hot-reload)."""
        from fnmatch import fnmatch
        return any(fnmatch(filepath, pattern) for pattern in self.cold_restart_patterns)

    def mark_startup_complete(self):
        """Mark startup as complete, ending the grace period."""
        self._startup_complete = True
        os.environ["JARVIS_STARTUP_COMPLETE"] = "true"
        logger.info("✅ Startup marked complete - hot-reload fully active")

    def load_config(self) -> Dict:
        """Load or create dynamic configuration"""
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r') as f:
                    return json.load(f)
            except Exception:
                pass

        # Default configuration
        config = {
            "main_script": "main.py",
            "port": None,  # Will be discovered dynamically
            "auto_reload": True,
            "reload_on_error": True,
            "max_retries": 3,
            "health_check_interval": 30,
            "memory_threshold_percent": 80,
            "cpu_threshold_percent": 70
        }

        self.save_config(config)
        return config

    def save_config(self, config: Dict):
        """Save configuration"""
        with open(self.config_file, 'w') as f:
            json.dump(config, f, indent=2)
        self.config = config

    def find_available_port(self, start_port: int = 8000, max_port: int = 9000) -> int:
        """Find an available port dynamically"""
        import socket

        for port in range(start_port, max_port):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(('', port))
                    return port
            except Exception:
                continue

        raise RuntimeError(f"No available ports found between {start_port} and {max_port}")

    def calculate_file_hashes(self) -> Dict[str, str]:
        """Calculate hashes for all monitored files"""
        hashes = {}

        for pattern in self.watch_patterns:
            for file_path in self.backend_dir.glob(pattern):
                # Skip excluded patterns
                if any(file_path.match(exc) for exc in self.exclude_patterns):
                    continue

                if file_path.is_file():
                    try:
                        with open(file_path, 'rb') as f:
                            file_hash = hashlib.md5(f.read()).hexdigest()
                            rel_path = str(file_path.relative_to(self.backend_dir))
                            hashes[rel_path] = file_hash
                    except Exception:
                        continue

        return hashes

    def detect_code_changes(self) -> Tuple[bool, List[str], List[str]]:
        """Detect if any code has changed since last check.

        Returns:
            Tuple of (has_hot_reload_changes, hot_reload_files, cold_restart_files)
            - hot_reload_files: Files that can trigger automatic restart
            - cold_restart_files: Files that need manual restart (requirements, .env, etc.)
        """
        current_hashes = self.calculate_file_hashes()

        # Load previous hashes
        previous_hashes = {}
        if self.code_hash_file.exists():
            try:
                with open(self.code_hash_file, 'rb') as f:
                    previous_hashes = pickle.load(f)
            except Exception:
                pass

        # Separate hot-reload from cold-restart files
        hot_reload_files = []
        cold_restart_files = []

        # Check for modified or new files
        for file_path, current_hash in current_hashes.items():
            if file_path not in previous_hashes or previous_hashes[file_path] != current_hash:
                if self.is_cold_restart_file(file_path):
                    cold_restart_files.append(file_path)
                else:
                    hot_reload_files.append(file_path)

        # Check for deleted files
        for file_path in previous_hashes:
            if file_path not in current_hashes:
                deleted_path = f"[DELETED] {file_path}"
                if self.is_cold_restart_file(file_path):
                    cold_restart_files.append(deleted_path)
                else:
                    hot_reload_files.append(deleted_path)

        # Save current hashes
        with open(self.code_hash_file, 'wb') as f:
            pickle.dump(current_hashes, f)

        # Log cold-restart files (informational only)
        if cold_restart_files:
            logger.info(f"📦 Cold-restart files changed (manual restart needed): {cold_restart_files}")

        return len(hot_reload_files) > 0, hot_reload_files, cold_restart_files

    def _get_protected_pids(self) -> Set[int]:
        """Get PIDs that should never be killed (current process and ancestors).

        This prevents the reload manager from killing itself or the process
        that launched it during startup.
        """
        protected = set()

        try:
            # Add current process
            current_pid = os.getpid()
            protected.add(current_pid)

            # Add all ancestor processes (parent, grandparent, etc.)
            # This protects the entire process chain: run_supervisor.py -> start_system.py -> etc.
            try:
                proc = psutil.Process(current_pid)
                while proc.parent():
                    parent = proc.parent()
                    protected.add(parent.pid)
                    proc = parent
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

            # Also protect any child processes we've spawned
            try:
                current_proc = psutil.Process(current_pid)
                for child in current_proc.children(recursive=True):
                    protected.add(child.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        except Exception as e:
            logger.debug(f"Error getting protected PIDs: {e}")
            # At minimum, protect current process
            protected.add(os.getpid())

        return protected

    async def find_jarvis_process(self, exclude_protected: bool = True) -> Optional[psutil.Process]:
        """Find running JARVIS process dynamically.

        Args:
            exclude_protected: If True, excludes current process and its ancestors/children
                             to prevent killing the startup process chain.

        Returns:
            The JARVIS process if found, None otherwise.
        """
        protected_pids = self._get_protected_pids() if exclude_protected else set()

        if protected_pids:
            logger.debug(f"🛡️ Protected PIDs (will not be killed): {protected_pids}")

        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                pid = proc.info.get('pid')

                # Skip protected processes
                if exclude_protected and pid in protected_pids:
                    continue

                cmdline = proc.info.get('cmdline', [])
                if cmdline and any('main.py' in arg for arg in cmdline):
                    # Check if it's in our backend directory
                    if any(str(self.backend_dir) in arg for arg in cmdline):
                        logger.debug(f"Found JARVIS process: PID {pid}")
                        return proc
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception as e:
                logger.debug(f"Error checking process: {e}")
                continue
        return None

    async def find_all_jarvis_processes(self, exclude_protected: bool = True) -> List[psutil.Process]:
        """Find ALL running JARVIS processes (there might be stale ones).

        Args:
            exclude_protected: If True, excludes current process chain from results.

        Returns:
            List of all JARVIS processes found.
        """
        protected_pids = self._get_protected_pids() if exclude_protected else set()
        jarvis_processes = []

        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                pid = proc.info.get('pid')

                # Skip protected processes
                if exclude_protected and pid in protected_pids:
                    continue

                cmdline = proc.info.get('cmdline', [])
                if cmdline and any('main.py' in arg for arg in cmdline):
                    # Check if it's in our backend directory
                    if any(str(self.backend_dir) in arg for arg in cmdline):
                        jarvis_processes.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception:
                continue

        return jarvis_processes

    async def stop_jarvis(self, force: bool = False, exclude_protected: bool = True):
        """Stop JARVIS process gracefully or forcefully.

        Args:
            force: If True, use SIGKILL instead of SIGTERM for stubborn processes.
            exclude_protected: If True, protects current process chain from being killed.
                             This is critical during startup to prevent self-termination.
        """
        protected_pids = self._get_protected_pids() if exclude_protected else set()
        current_pid = os.getpid()

        logger.info(f"🛑 Stopping JARVIS... (protecting PID {current_pid} and {len(protected_pids)-1} related processes)")

        stopped_count = 0

        # Try to stop our managed process first (if it's not the current process)
        if self.jarvis_process and self.jarvis_process.returncode is None:
            managed_pid = self.jarvis_process.pid
            if managed_pid not in protected_pids:
                try:
                    self.jarvis_process.terminate()
                    await asyncio.wait_for(self.jarvis_process.wait(), timeout=5)
                    logger.info(f"✅ JARVIS managed process stopped gracefully (PID: {managed_pid})")
                    stopped_count += 1
                    return
                except asyncio.TimeoutError:
                    if force:
                        self.jarvis_process.kill()
                        await self.jarvis_process.wait()
                        logger.info(f"✅ JARVIS managed process force killed (PID: {managed_pid})")
                        stopped_count += 1
                        return
            else:
                logger.debug(f"⏭️ Skipping managed process (PID: {managed_pid}) - in protected set")

        # Find and stop ALL stale JARVIS processes (excluding protected ones)
        jarvis_procs = await self.find_all_jarvis_processes(exclude_protected=exclude_protected)

        if not jarvis_procs:
            logger.info("ℹ️ No stale JARVIS processes found to stop")
            return

        logger.info(f"🔍 Found {len(jarvis_procs)} JARVIS process(es) to stop")

        for jarvis_proc in jarvis_procs:
            try:
                pid = jarvis_proc.pid

                # Double-check protection (belt and suspenders)
                if pid in protected_pids:
                    logger.warning(f"⚠️ Skipping protected process (PID: {pid})")
                    continue

                # Try graceful termination first
                jarvis_proc.terminate()

                try:
                    jarvis_proc.wait(timeout=5)
                    logger.info(f"✅ Stopped JARVIS process (PID: {pid})")
                    stopped_count += 1
                except psutil.TimeoutExpired:
                    if force:
                        jarvis_proc.kill()
                        logger.info(f"✅ Force killed JARVIS process (PID: {pid})")
                        stopped_count += 1
                    else:
                        logger.warning(f"⚠️ Process {pid} didn't stop gracefully, use force=True to kill")

            except psutil.NoSuchProcess:
                logger.debug(f"Process already terminated")
            except psutil.AccessDenied:
                logger.warning(f"⚠️ Access denied stopping process (PID: {jarvis_proc.pid})")
            except Exception as e:
                logger.error(f"Error stopping process: {e}")

        if stopped_count > 0:
            logger.info(f"🧹 Cleaned up {stopped_count} JARVIS process(es)")
        else:
            logger.info("ℹ️ No processes were stopped (all protected or already gone)")

    async def start_jarvis(self) -> bool:
        """Start JARVIS with dynamic configuration"""
        try:
            # Find available port if not set
            if not self.config.get('port'):
                self.config['port'] = self.find_available_port()
                self.save_config(self.config)

            # Prepare environment
            env = os.environ.copy()
            env['PYTHONPATH'] = str(self.backend_dir)
            env['BACKEND_PORT'] = str(self.config['port'])
            env['JARVIS_AUTO_RELOAD'] = 'true'

            # Enable optimizations
            env['OPTIMIZE_STARTUP'] = 'true'
            env['LAZY_LOAD_MODELS'] = 'true'
            env['PARALLEL_INIT'] = 'true'

            # Start JARVIS
            logger.info(f"Starting JARVIS on port {self.config['port']}...")

            self.jarvis_process = await asyncio.create_subprocess_exec(
                sys.executable,
                self.config['main_script'],
                '--port', str(self.config['port']),
                cwd=str(self.backend_dir),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )

            # Wait briefly for startup
            await asyncio.sleep(3)

            # Check if process is still running
            if self.jarvis_process.returncode is not None:
                logger.error("JARVIS failed to start")
                return False

            logger.info(f"JARVIS started successfully (PID: {self.jarvis_process.pid})")

            # Save restart time
            self.last_restart_time = time.time()
            with open(self.last_reload_file, 'w') as f:
                json.dump({
                    'time': datetime.now().isoformat(),
                    'port': self.config['port'],
                    'pid': self.jarvis_process.pid
                }, f)

            return True

        except Exception as e:
            logger.error(f"Failed to start JARVIS: {e}")
            return False

    async def restart_jarvis(self, reason: str = "Manual restart"):
        """Restart JARVIS with cooldown protection"""
        # Check cooldown
        time_since_restart = time.time() - self.last_restart_time
        if time_since_restart < self.restart_cooldown:
            wait_time = self.restart_cooldown - time_since_restart
            logger.info(f"Restart cooldown active, waiting {wait_time:.1f}s...")
            await asyncio.sleep(wait_time)

        logger.info(f"Restarting JARVIS: {reason}")

        # Stop current instance
        await self.stop_jarvis()

        # Clear any cached imports (helps with code changes)
        self.clear_python_cache()

        # Start new instance
        success = await self.start_jarvis()

        if not success:
            logger.error("Failed to restart JARVIS")
            # Try alternative startup methods
            await self.try_alternative_startup()

    def clear_python_cache(self):
        """Clear Python import cache for changed modules"""
        # Clear __pycache__ directories
        for cache_dir in self.backend_dir.glob("**/__pycache__"):
            try:
                import shutil
                shutil.rmtree(cache_dir)
            except Exception:
                pass

        # Clear sys.modules for our modules
        modules_to_clear = []
        for module_name in list(sys.modules.keys()):
            if 'jarvis' in module_name.lower() or 'backend' in module_name:
                modules_to_clear.append(module_name)

        for module_name in modules_to_clear:
            sys.modules.pop(module_name, None)

    async def try_alternative_startup(self):
        """Try alternative startup methods if main fails"""
        alternatives = [
            ('main_minimal.py', 'Minimal mode'),
            ('start_backend.py', 'Legacy startup'),
        ]

        for script, mode in alternatives:
            script_path = self.backend_dir / script
            if script_path.exists():
                logger.info(f"Trying {mode} startup...")
                self.config['main_script'] = script
                if await self.start_jarvis():
                    logger.info(f"Started in {mode}")
                    return

    async def monitor_loop(self):
        """Main monitoring loop with startup grace period protection."""
        logger.info("Starting JARVIS monitor loop...")
        logger.info(f"🛡️ Startup grace period: {self._startup_grace_period}s (no auto-restarts during this time)")

        while True:
            try:
                await asyncio.sleep(10)  # Check every 10 seconds

                # ═══════════════════════════════════════════════════════════════════
                # STARTUP GRACE PERIOD CHECK
                # ═══════════════════════════════════════════════════════════════════
                in_grace_period = self.is_in_startup_grace_period()

                # Check for code changes
                if self.config.get('auto_reload', True):
                    has_hot_changes, hot_files, cold_files = self.detect_code_changes()

                    if has_hot_changes:
                        logger.info(f"🔍 Detected {len(hot_files)} hot-reload file changes")
                        for file in hot_files[:5]:  # Show first 5 changes
                            logger.info(f"  - {file}")
                        if len(hot_files) > 5:
                            logger.info(f"  ... and {len(hot_files) - 5} more")

                        # ═══════════════════════════════════════════════════════════════════
                        # CRITICAL: Respect startup grace period
                        # ═══════════════════════════════════════════════════════════════════
                        if in_grace_period:
                            elapsed = time.time() - self._startup_time
                            remaining = self._startup_grace_period - elapsed
                            logger.info(
                                f"⏳ Startup grace period active ({remaining:.0f}s remaining) - "
                                f"deferring restart for {len(hot_files)} changed files"
                            )
                        else:
                            await self.restart_jarvis(f"Code changes detected ({len(hot_files)} files)")

                    # Log cold-restart files separately (informational)
                    if cold_files:
                        logger.info(f"📦 Cold-restart files changed (requires manual restart): {cold_files}")

                # Check if JARVIS is still running (but only act after grace period)
                if self.jarvis_process and self.jarvis_process.returncode is not None:
                    logger.warning("JARVIS process died unexpectedly")
                    if self.config.get('reload_on_error', True) and not in_grace_period:
                        await self.restart_jarvis("Process died")
                    elif in_grace_period:
                        logger.warning("⏳ Process died during grace period - supervisor should handle this")

                # Check system resources (logging only)
                memory = psutil.virtual_memory()
                cpu = psutil.cpu_percent(interval=1)

                if memory.percent > self.config.get('memory_threshold_percent', 80):
                    logger.debug(f"Memory usage: {memory.percent}%")

                if cpu > self.config.get('cpu_threshold_percent', 70):
                    logger.debug(f"CPU usage: {cpu}%")

            except Exception as e:
                logger.error(f"Monitor loop error: {e}")
                await asyncio.sleep(10)

    async def run(self):
        """Main entry point"""
        logger.info("JARVIS Reload Manager starting...")
        logger.info(f"🛡️ Grace period: {self._startup_grace_period}s before hot-reload activates")

        # Check if JARVIS is already running
        existing = await self.find_jarvis_process()
        if existing:
            logger.info(f"Found existing JARVIS process (PID: {existing.pid})")

            # Check for code changes (hot-reload only, not cold-restart files)
            has_hot_changes, hot_files, cold_files = self.detect_code_changes()

            if has_hot_changes:
                logger.info(f"Hot-reload code changes detected ({len(hot_files)} files), restarting...")
                await self.stop_jarvis(force=True)
                await self.start_jarvis()
            elif cold_files:
                logger.info(f"Only cold-restart files changed ({cold_files}) - manual restart recommended")
                logger.info("Attaching to existing process (dependencies may need update)")
            else:
                logger.info("No code changes, attaching to existing process")
                # We could monitor the existing process instead
        else:
            # Start JARVIS
            await self.start_jarvis()

        # Start monitoring
        self.monitor_task = asyncio.create_task(self.monitor_loop())

        # Wait for interrupt
        try:
            await self.monitor_task
        except KeyboardInterrupt:
            logger.info("Shutdown requested...")
            await self.stop_jarvis()


async def main():
    """Main entry point"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    manager = JARVISReloadManager()
    await manager.run()


if __name__ == "__main__":
    asyncio.run(main())