#!/usr/bin/env python3
"""
JARVIS Supervisor Entry Point
==============================

This script runs the JARVIS Lifecycle Supervisor, which sits above the main
JARVIS application and manages its lifecycle including updates, restarts,
and rollbacks.

Features:
- Automatic cleanup of existing JARVIS instances
- Graceful termination with TTS announcements
- Self-updating with voice feedback
- Intelligent startup narration (v19.6.0)
- Phase-aware voice feedback during loading

Usage:
    # Run supervisor (recommended way to start JARVIS)
    python run_supervisor.py

    # With custom config
    JARVIS_SUPERVISOR_CONFIG=/path/to/config.yaml python run_supervisor.py

    # Disable voice narration
    STARTUP_NARRATOR_VOICE=false python run_supervisor.py

Author: JARVIS System
Version: 1.2.0
"""

import asyncio
import logging
import os
import platform
import sys
import time
from pathlib import Path

# Add backend to path
backend_path = Path(__file__).parent / "backend"
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))


def setup_logging() -> None:
    """Configure logging for the supervisor."""
    log_level = os.environ.get("JARVIS_SUPERVISOR_LOG_LEVEL", "INFO")
    
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    
    # Reduce noise from libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


def print_banner() -> None:
    """Print an engaging startup banner."""
    print()
    print("\033[36m" + "=" * 65 + "\033[0m")
    print("\033[36m" + " " * 15 + "⚡ JARVIS LIFECYCLE SUPERVISOR ⚡" + " " * 15 + "\033[0m")
    print("\033[36m" + "=" * 65 + "\033[0m")
    print()
    print("  \033[33m🤖 Self-Updating • Self-Healing • Autonomous\033[0m")
    print()
    print("  \033[90mThe Living OS - Manages updates, restarts, and rollbacks")
    print("  while keeping JARVIS online and responsive.\033[0m")
    print()
    print("\033[36m" + "-" * 65 + "\033[0m")
    print()


async def speak_async(text: str, wait: bool = True) -> None:
    """
    Speak text asynchronously using macOS say command.
    
    Args:
        text: Text to speak
        wait: Whether to wait for speech to complete
    """
    if platform.system() != "Darwin":
        return
    
    voice = os.getenv("STARTUP_NARRATOR_VOICE_NAME", "Daniel")
    rate = os.getenv("STARTUP_NARRATOR_RATE", "190")
    
    try:
        process = await asyncio.create_subprocess_exec(
            "say", "-v", voice, "-r", rate, text,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if wait:
            await process.wait()
        else:
            # Fire and forget
            asyncio.create_task(process.wait())
    except Exception:
        pass


async def cleanup_existing_instances() -> bool:
    """
    Automatically discover and cleanup existing JARVIS instances.
    
    Uses psutil for process discovery and graceful termination
    with SIGINT → SIGTERM → SIGKILL cascade.
    
    Includes intelligent voice narration during the cleanup process.
    
    Returns:
        True if any instances were terminated
    """
    try:
        import psutil
        import signal
    except ImportError:
        print("  \033[33m⚠\033[0m psutil not available")
        return False
    
    JARVIS_PATTERNS = ["start_system.py", "main.py"]
    PID_FILES = [Path("/tmp/jarvis_master.pid"), Path("/tmp/jarvis.pid")]
    
    my_pid = os.getpid()
    my_parent = os.getppid()
    discovered = {}
    
    # 1. Check PID files
    for pid_file in PID_FILES:
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                if psutil.pid_exists(pid) and pid not in (my_pid, my_parent):
                    proc = psutil.Process(pid)
                    cmdline = " ".join(proc.cmdline()).lower()
                    if any(p in cmdline for p in JARVIS_PATTERNS):
                        discovered[pid] = {
                            "cmdline": cmdline,
                            "age": time.time() - proc.create_time(),
                        }
            except (ValueError, psutil.NoSuchProcess):
                pid_file.unlink(missing_ok=True)
    
    # 2. Scan process list
    for proc in psutil.process_iter(['pid', 'cmdline', 'create_time']):
        try:
            pid = proc.info['pid']
            if pid in (my_pid, my_parent) or pid in discovered:
                continue
            cmdline = " ".join(proc.info.get('cmdline') or []).lower()
            if any(p in cmdline for p in JARVIS_PATTERNS):
                discovered[pid] = {
                    "cmdline": cmdline,
                    "age": time.time() - proc.info['create_time'],
                }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    
    if not discovered:
        print("  \033[32m●\033[0m No existing JARVIS instances found")
        return False
    
    # Show what we found
    print(f"  \033[33m●\033[0m Found {len(discovered)} existing instance(s):")
    for pid, info in discovered.items():
        age_min = info['age'] / 60
        print(f"    └─ PID {pid} (running {age_min:.1f} min)")
    print()
    
    # TTS announcement - concise, don't over-explain
    # Only speak if voice is enabled (check env)
    voice_enabled = os.getenv("STARTUP_NARRATOR_VOICE", "true").lower() == "true"
    if voice_enabled:
        await speak_async("Cleaning up previous session.", wait=False)
    
    # Terminate with cascade: SIGINT → SIGTERM → SIGKILL
    terminated = 0
    for pid in discovered:
        try:
            ps_proc = psutil.Process(pid)
            
            # Phase 1: SIGINT (graceful)
            os.kill(pid, signal.SIGINT)
            try:
                ps_proc.wait(timeout=10.0)
                terminated += 1
                continue
            except psutil.TimeoutExpired:
                pass
            
            # Phase 2: SIGTERM
            os.kill(pid, signal.SIGTERM)
            try:
                ps_proc.wait(timeout=5.0)
                terminated += 1
                continue
            except psutil.TimeoutExpired:
                pass
            
            # Phase 3: SIGKILL
            os.kill(pid, signal.SIGKILL)
            ps_proc.wait(timeout=2.0)
            terminated += 1
            
        except (psutil.NoSuchProcess, ProcessLookupError):
            terminated += 1
        except Exception as e:
            logging.debug(f"Failed to terminate {pid}: {e}")
    
    # Clean up PID files
    for pid_file in PID_FILES:
        try:
            pid_file.unlink(missing_ok=True)
        except Exception:
            pass
    
    if terminated > 0:
        print(f"  \033[32m✓\033[0m Terminated {terminated} instance(s)")
        # NOTE: Don't narrate again here - the supervisor will announce "Supervisor online" next
        # Having too many announcements is confusing
    
    return terminated > 0


async def main() -> None:
    """
    Main entry point for the supervisor.
    
    Orchestrates the startup process with intelligent voice narration
    and visual loading feedback.
    """
    from core.supervisor import JARVISSupervisor
    
    print_banner()
    
    # Check if voice is enabled
    voice_enabled = os.getenv("STARTUP_NARRATOR_VOICE", "true").lower() == "true"
    
    # Step 1: Automatic cleanup of existing instances
    print("  \033[90m[1/3] Checking for existing instances...\033[0m")
    cleanup_needed = await cleanup_existing_instances()
    
    if cleanup_needed:
        print()
        # Brief pause to let ports release
        await asyncio.sleep(1.0)
    
    # Step 2: Initialize supervisor
    print("  \033[90m[2/3] Initializing supervisor...\033[0m")
    print()
    
    supervisor = JARVISSupervisor()
    
    # Print configuration summary
    config = supervisor.config
    print(f"  \033[32m●\033[0m Mode:          \033[1m{config.mode.value.upper()}\033[0m")
    print(f"  \033[32m●\033[0m Update Check:  {'Enabled (' + str(config.update.check.interval_seconds) + 's)' if config.update.check.enabled else 'Disabled'}")
    print(f"  \033[32m●\033[0m Idle Updates:  {'Enabled (' + str(config.idle.threshold_seconds // 3600) + 'h threshold)' if config.idle.enabled else 'Disabled'}")
    print(f"  \033[32m●\033[0m Auto-Rollback: {'Enabled' if config.rollback.auto_on_boot_failure else 'Disabled'}")
    print(f"  \033[32m●\033[0m Max Retries:   {config.health.max_crash_retries}")
    print(f"  \033[32m●\033[0m Loading Page:  \033[1mEnabled\033[0m (port 3001)")
    print(f"  \033[32m●\033[0m Voice Narration: \033[1m{'Enabled' if voice_enabled else 'Disabled'}\033[0m")
    print()
    print("\033[36m" + "-" * 65 + "\033[0m")
    print()
    print("  \033[90m[3/3] Starting JARVIS with loading page...\033[0m")
    print()
    print("  \033[33m📡 Loading page will open in Chrome Incognito\033[0m")
    print("  \033[33m   Watch real-time progress as JARVIS initializes!\033[0m")
    if voice_enabled:
        print("  \033[33m🔊 Voice narration enabled - JARVIS will speak during startup\033[0m")
    print()
    
    try:
        await supervisor.run()
    except KeyboardInterrupt:
        print("\n\033[33m👋 Supervisor interrupted by user\033[0m")
        # Announce shutdown if voice is enabled
        if voice_enabled:
            await speak_async("Supervisor shutting down. Goodbye.", wait=True)
    except Exception as e:
        logging.error(f"Supervisor error: {e}")
        if voice_enabled:
            await speak_async("An error occurred. Please check the logs.", wait=True)
        raise


if __name__ == "__main__":
    setup_logging()
    asyncio.run(main())

