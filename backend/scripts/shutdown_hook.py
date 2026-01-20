"""
JARVIS Shutdown Hook - Triple-Lock VM Cleanup System
=====================================================

This module provides robust, guaranteed cleanup of GCP resources when JARVIS shuts down.
It's designed to work in all shutdown scenarios: normal exit, SIGTERM, SIGINT, atexit.

Triple-Lock Safety System:
1. Platform-Level (GCP max-run-duration) - VMs auto-delete after 3 hours
2. VM-Side (startup script self-destruct) - VM shuts down if backend process dies
3. Local Cleanup (this module) - Cleanup called on normal/signal-based shutdown

This module is the "Local Cleanup" layer and ensures VMs are cleaned up BEFORE
the platform-level timeout triggers, saving you money.

Features:
- Async-safe with proper event loop handling
- Thread-safe with locks
- Idempotent (safe to call multiple times)
- Signal handler integration (SIGTERM, SIGINT)
- atexit registration for guaranteed cleanup
- Multiple fallback approaches
- Works even if called from different contexts

Usage:
    # Automatic - just import the module to register handlers
    import backend.scripts.shutdown_hook
    
    # Manual async cleanup
    from backend.scripts.shutdown_hook import cleanup_remote_resources
    await cleanup_remote_resources()
    
    # Manual sync cleanup (for signal handlers)
    from backend.scripts.shutdown_hook import cleanup_remote_resources_sync
    cleanup_remote_resources_sync()

Author: JARVIS System
Version: 2.0.0
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("jarvis.shutdown_hook")

# ============================================================================
# STATE TRACKING
# ============================================================================

_cleanup_started = False
_cleanup_completed = False
_cleanup_lock = threading.Lock()
_original_sigterm: Optional[Callable] = None
_original_sigint: Optional[Callable] = None
_handlers_registered = False


# ============================================================================
# CORE CLEANUP FUNCTIONS
# ============================================================================

async def cleanup_remote_resources(
    timeout: float = 30.0,
    reason: str = "JARVIS shutdown",
) -> Dict[str, Any]:
    """
    Force cleanup of all GCP resources (async version).

    This is the primary cleanup function that should be called on shutdown.
    It's designed to be robust and work even if some components are unavailable.

    v3.0: Enhanced with infrastructure orchestrator integration for:
    - Terraform-managed resources (Cloud Run, Redis, etc.)
    - Session lock release
    - Orphan detection loop stop

    Args:
        timeout: Maximum time to wait for cleanup (seconds)
        reason: Reason for cleanup (for logging)

    Returns:
        Dict with cleanup results: {
            "success": bool,
            "vms_cleaned": int,
            "terraform_destroyed": int,
            "errors": List[str],
            "method": str
        }
    """
    global _cleanup_started, _cleanup_completed

    # Idempotency check
    with _cleanup_lock:
        if _cleanup_completed:
            logger.info("🔄 Cleanup already completed, skipping")
            return {"success": True, "vms_cleaned": 0, "terraform_destroyed": 0, "errors": [], "method": "cached"}

        if _cleanup_started:
            logger.info("⏳ Cleanup already in progress, waiting...")
            # Wait for existing cleanup to complete
            for _ in range(int(timeout)):
                if _cleanup_completed:
                    return {"success": True, "vms_cleaned": 0, "terraform_destroyed": 0, "errors": [], "method": "cached"}
                await asyncio.sleep(1)
            return {"success": False, "vms_cleaned": 0, "terraform_destroyed": 0, "errors": ["Timeout waiting for existing cleanup"], "method": "timeout"}

        _cleanup_started = True

    logger.info(f"🪝 Shutdown Hook v3.0: Cleaning remote resources (reason: {reason})...")

    results = {
        "success": False,
        "vms_cleaned": 0,
        "terraform_destroyed": 0,
        "errors": [],
        "method": "none",
    }

    try:
        # Step 0: Cleanup Infrastructure Orchestrator (Terraform-managed resources)
        # This MUST run first to destroy Cloud Run/Redis before VM cleanup
        terraform_count = await _cleanup_via_infrastructure_orchestrator(timeout / 3)
        if terraform_count >= 0:
            results["terraform_destroyed"] = terraform_count
            logger.info(f"   Terraform cleanup: {terraform_count} resource(s)")

        # Step 1: Try using the VM Manager (preferred for VMs)
        cleaned = await _cleanup_via_vm_manager(timeout / 3)
        if cleaned >= 0:
            results["vms_cleaned"] = cleaned
            results["method"] = "vm_manager"
            results["success"] = True

        # Step 2: If VM Manager failed/unavailable, try gcloud CLI
        if not results["success"]:
            cleaned, errors = await _cleanup_via_gcloud(timeout / 3)
            results["vms_cleaned"] = cleaned
            results["errors"].extend(errors)
            results["method"] = "gcloud_cli" if cleaned > 0 else "none"
            results["success"] = cleaned >= 0

        # If we cleaned any Terraform resources, consider it a success
        if results["terraform_destroyed"] > 0:
            results["success"] = True
            results["method"] = f"terraform+{results['method']}" if results["method"] != "none" else "terraform"

    except Exception as e:
        logger.error(f"❌ Cleanup failed: {e}")
        results["errors"].append(str(e))

    finally:
        with _cleanup_lock:
            _cleanup_completed = True

    if results["success"]:
        total_cleaned = results["vms_cleaned"] + results["terraform_destroyed"]
        logger.info(f"✅ Shutdown hook complete: {total_cleaned} resource(s) cleaned ({results['method']})")
    else:
        logger.warning(f"⚠️ Shutdown hook completed with issues: {results}")

    # v2.0: Notify cost tracker of cleanup for accurate cost tracking
    await _notify_cost_tracker(results, reason)

    # v93.6: Close all client sessions to prevent "Unclosed client session" warnings
    await _close_client_sessions()

    return results


async def _close_client_sessions() -> None:
    """
    v93.6: Close all aiohttp client sessions to prevent "Unclosed client session" errors.

    This is called during shutdown to ensure all HTTP clients are properly closed
    before the event loop terminates.
    """
    sessions_closed = 0

    # Close prime client session
    try:
        for module_path in ["core.prime_client", "backend.core.prime_client"]:
            try:
                import importlib
                module = importlib.import_module(module_path)
                close_func = getattr(module, "close_prime_client", None)
                if close_func:
                    await close_func()
                    sessions_closed += 1
                    logger.debug("   Closed prime client session")
                    break
            except ImportError:
                continue
    except Exception as e:
        logger.debug(f"   Prime client close error (non-critical): {e}")

    # Close cost tracker
    try:
        for module_path in ["core.cost_tracker", "backend.core.cost_tracker"]:
            try:
                import importlib
                module = importlib.import_module(module_path)
                get_tracker = getattr(module, "get_cost_tracker", None)
                if get_tracker:
                    tracker = get_tracker()
                    if tracker:
                        await tracker.shutdown()
                        sessions_closed += 1
                        logger.debug("   Closed cost tracker")
                        break
            except ImportError:
                continue
    except Exception as e:
        logger.debug(f"   Cost tracker close error (non-critical): {e}")

    # Close GCP VM manager
    try:
        for module_path in ["core.gcp_vm_manager", "backend.core.gcp_vm_manager"]:
            try:
                import importlib
                module = importlib.import_module(module_path)
                cleanup_func = getattr(module, "cleanup_vm_manager", None)
                if cleanup_func:
                    await cleanup_func()
                    sessions_closed += 1
                    logger.debug("   Closed GCP VM manager")
                    break
            except ImportError:
                continue
    except Exception as e:
        logger.debug(f"   GCP VM manager close error (non-critical): {e}")

    # Close reactor core client
    try:
        for module_path in ["clients.reactor_core_client", "backend.clients.reactor_core_client"]:
            try:
                import importlib
                module = importlib.import_module(module_path)
                # Try shutdown_reactor_client first (the correct function name)
                close_func = getattr(module, "shutdown_reactor_client", None)
                if close_func:
                    await close_func()
                    sessions_closed += 1
                    logger.debug("   Closed reactor core client")
                    break
            except ImportError:
                continue
    except Exception as e:
        logger.debug(f"   Reactor core client close error (non-critical): {e}")

    # Close jarvis prime client
    try:
        for module_path in ["clients.jarvis_prime_client", "backend.clients.jarvis_prime_client"]:
            try:
                import importlib
                module = importlib.import_module(module_path)
                close_func = getattr(module, "close_jarvis_prime_client", None)
                if not close_func:
                    close_func = getattr(module, "shutdown_jarvis_prime_client", None)
                if close_func:
                    await close_func()
                    sessions_closed += 1
                    logger.debug("   Closed jarvis prime client")
                    break
            except ImportError:
                continue
    except Exception as e:
        logger.debug(f"   Jarvis prime client close error (non-critical): {e}")

    if sessions_closed > 0:
        logger.debug(f"   Closed {sessions_closed} client session(s)")


async def _cleanup_via_infrastructure_orchestrator(timeout: float) -> int:
    """
    Cleanup using the Infrastructure Orchestrator (Terraform-managed resources).

    This handles:
    - Cloud Run services (JARVIS Prime, Backend)
    - Redis/Memorystore
    - Any other Terraform-managed resources

    Returns:
        Number of resources cleaned, or -1 if unavailable
    """
    try:
        backend_path = Path(__file__).parent.parent
        if str(backend_path) not in sys.path:
            sys.path.insert(0, str(backend_path))

        # Try to import the orchestrator
        for module_path in ["core.infrastructure_orchestrator", "backend.core.infrastructure_orchestrator"]:
            try:
                import importlib
                module = importlib.import_module(module_path)
                cleanup_func = getattr(module, "cleanup_infrastructure_on_shutdown", None)
                get_orchestrator = getattr(module, "get_infrastructure_orchestrator", None)
                if cleanup_func:
                    break
            except ImportError:
                continue
        else:
            logger.debug("   Infrastructure Orchestrator not available")
            return -1

        logger.info("   Running Infrastructure Orchestrator cleanup...")

        # Run cleanup with timeout
        await asyncio.wait_for(cleanup_func(), timeout=timeout)

        # Try to get stats on what was cleaned
        try:
            # Get the orchestrator to check stats (if available)
            _orchestrator_instance = getattr(module, "_orchestrator_instance", None)
            if _orchestrator_instance:
                status = _orchestrator_instance.get_status()
                return status.get("terraform_operations", {}).get("destroy_count", 0)
        except Exception:
            pass

        return 0  # Cleanup ran but can't get count

    except asyncio.TimeoutError:
        logger.warning("   Infrastructure Orchestrator cleanup timed out")
        return -1
    except Exception as e:
        logger.debug(f"   Infrastructure Orchestrator cleanup error: {e}")
        return -1


async def _notify_cost_tracker(
    cleanup_result: Dict[str, Any],
    reason: str,
) -> None:
    """
    Notify the cost tracker about the cleanup event.
    
    This ensures all VM terminations are recorded for accurate cost tracking,
    even if the VMs were cleaned up via gcloud CLI fallback.
    """
    try:
        # Import cost tracker
        backend_path = Path(__file__).parent.parent
        if str(backend_path) not in sys.path:
            sys.path.insert(0, str(backend_path))
        
        for module_path in ["core.cost_tracker", "backend.core.cost_tracker"]:
            try:
                import importlib
                module = importlib.import_module(module_path)
                get_cost_tracker = getattr(module, "get_cost_tracker", None)
                if get_cost_tracker:
                    break
            except ImportError:
                continue
        else:
            return  # Cost tracker not available
        
        # Get cost tracker instance (don't create if not exists)
        try:
            tracker = get_cost_tracker()
            if hasattr(tracker, 'record_shutdown_cleanup'):
                await tracker.record_shutdown_cleanup(cleanup_result, reason)
        except Exception:
            pass  # Cost tracker not initialized
            
    except Exception as e:
        logger.debug(f"Cost tracker notification failed (non-critical): {e}")


async def _cleanup_via_vm_manager(timeout: float) -> int:
    """
    Cleanup using the GCPVMManager instance.
    
    Returns:
        Number of VMs cleaned, or -1 if manager unavailable
    """
    try:
        # Add backend path to imports
        backend_path = Path(__file__).parent.parent
        if str(backend_path) not in sys.path:
            sys.path.insert(0, str(backend_path))
        
        # Import with multiple fallback paths
        get_gcp_vm_manager_safe = None
        cleanup_vm_manager = None
        
        for module_path in ["core.gcp_vm_manager", "backend.core.gcp_vm_manager"]:
            try:
                import importlib
                module = importlib.import_module(module_path)
                get_gcp_vm_manager_safe = getattr(module, "get_gcp_vm_manager_safe", None)
                cleanup_vm_manager = getattr(module, "cleanup_vm_manager", None)
                if get_gcp_vm_manager_safe:
                    break
            except ImportError:
                continue
        
        if not get_gcp_vm_manager_safe:
            logger.debug("GCP VM Manager module not found")
            return -1
        
        # Get manager instance (don't initialize if not already running)
        manager = await asyncio.wait_for(
            get_gcp_vm_manager_safe(),
            timeout=5.0
        )
        
        if not manager:
            logger.info("   GCP VM Manager not initialized, nothing to clean up.")
            return 0
        
        # Count VMs before cleanup
        vm_count = len(manager.managed_vms) if hasattr(manager, 'managed_vms') else 0
        
        if vm_count == 0:
            logger.info("   No managed VMs to clean up.")
            return 0
        
        logger.info(f"   Found {vm_count} managed VM(s), initiating cleanup...")
        
        # Cleanup all VMs
        await asyncio.wait_for(
            manager.cleanup_all_vms(reason="Local JARVIS shutdown"),
            timeout=timeout - 5
        )
        
        # Also call the global cleanup function
        if cleanup_vm_manager:
            await asyncio.wait_for(
                cleanup_vm_manager(),
                timeout=5.0
            )
        
        logger.info(f"   ✅ Cleaned up {vm_count} VM(s) via VM Manager")
        return vm_count
        
    except asyncio.TimeoutError:
        logger.warning("   VM Manager cleanup timed out")
        return -1
    except Exception as e:
        logger.debug(f"   VM Manager cleanup failed: {e}")
        return -1


async def _cleanup_via_gcloud(timeout: float) -> tuple[int, List[str]]:
    """
    Fallback cleanup using gcloud CLI.
    
    This is a safety net if the VM Manager is unavailable.
    
    Returns:
        Tuple of (vms_cleaned, errors)
    """
    errors = []
    cleaned = 0
    
    try:
        project_id = os.getenv("GCP_PROJECT_ID", os.getenv("GOOGLE_CLOUD_PROJECT"))
        zone = os.getenv("GCP_ZONE", "us-central1-a")
        
        if not project_id:
            logger.debug("   No GCP project configured, skipping gcloud cleanup")
            return 0, []
        
        logger.info(f"   Attempting gcloud CLI cleanup (project: {project_id})...")
        
        # List JARVIS VMs
        list_cmd = [
            "gcloud", "compute", "instances", "list",
            f"--project={project_id}",
            "--filter=labels.created-by=jarvis",
            "--format=value(name,zone)",
        ]
        
        process = await asyncio.create_subprocess_exec(
            *list_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=15.0
        )
        
        if process.returncode != 0:
            errors.append(f"gcloud list failed: {stderr.decode()}")
            return 0, errors
        
        # Parse VM list
        vms = []
        for line in stdout.decode().strip().split('\n'):
            if line.strip():
                parts = line.split()
                if len(parts) >= 2:
                    vms.append({"name": parts[0], "zone": parts[1]})
        
        if not vms:
            logger.info("   No JARVIS VMs found via gcloud")
            return 0, []
        
        logger.info(f"   Found {len(vms)} JARVIS VM(s) via gcloud, deleting...")
        
        # Delete VMs in parallel
        delete_tasks = []
        for vm in vms:
            delete_cmd = [
                "gcloud", "compute", "instances", "delete",
                vm["name"],
                f"--project={project_id}",
                f"--zone={vm['zone']}",
                "--quiet",
            ]
            delete_tasks.append(_run_gcloud_delete(delete_cmd, vm["name"]))
        
        results = await asyncio.gather(*delete_tasks, return_exceptions=True)
        
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                errors.append(f"Failed to delete {vms[i]['name']}: {result}")
            elif result:
                cleaned += 1
        
        logger.info(f"   ✅ Deleted {cleaned}/{len(vms)} VM(s) via gcloud")
        return cleaned, errors
        
    except asyncio.TimeoutError:
        errors.append("gcloud cleanup timed out")
        return cleaned, errors
    except Exception as e:
        errors.append(f"gcloud cleanup error: {e}")
        return cleaned, errors


async def _run_gcloud_delete(cmd: List[str], vm_name: str) -> bool:
    """Run a gcloud delete command."""
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        
        _, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=60.0
        )
        
        if process.returncode == 0:
            logger.debug(f"   Deleted VM: {vm_name}")
            return True
        else:
            logger.warning(f"   Failed to delete {vm_name}: {stderr.decode()}")
            return False
            
    except Exception as e:
        logger.warning(f"   Error deleting {vm_name}: {e}")
        return False


# ============================================================================
# SYNCHRONOUS WRAPPERS (for signal handlers and atexit)
# ============================================================================

def cleanup_remote_resources_sync(
    timeout: float = 30.0,
    reason: str = "JARVIS shutdown (sync)",
) -> Dict[str, Any]:
    """
    Synchronous wrapper for cleanup_remote_resources.
    
    This is used by signal handlers and atexit which can't use async functions directly.
    Creates a new event loop or uses ThreadPoolExecutor for safety.
    """
    global _cleanup_completed
    
    # Quick check to avoid work if already done
    if _cleanup_completed:
        return {"success": True, "vms_cleaned": 0, "method": "cached"}
    
    logger.info(f"🪝 Sync cleanup triggered: {reason}")
    
    try:
        # Try to get existing event loop
        try:
            loop = asyncio.get_running_loop()
            # We're in an async context - can't run sync here
            logger.warning("   Cannot run sync cleanup in async context")
            return {"success": False, "vms_cleaned": 0, "errors": ["Async context"], "method": "none"}
        except RuntimeError:
            # No running loop - we can create one
            pass
        
        # Method 1: Create new event loop
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(
                    asyncio.wait_for(
                        cleanup_remote_resources(timeout=timeout, reason=reason),
                        timeout=timeout
                    )
                )
                return result
            finally:
                loop.close()
        except Exception as e:
            logger.debug(f"   Event loop method failed: {e}")
        
        # Method 2: Use ThreadPoolExecutor
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_run_cleanup_in_new_loop, timeout, reason)
                result = future.result(timeout=timeout + 5)
                return result
        except FuturesTimeoutError:
            logger.warning("   ThreadPoolExecutor cleanup timed out")
            return {"success": False, "vms_cleaned": 0, "errors": ["Timeout"], "method": "none"}
        except Exception as e:
            logger.debug(f"   ThreadPoolExecutor method failed: {e}")
        
        return {"success": False, "vms_cleaned": 0, "errors": ["All methods failed"], "method": "none"}
        
    except Exception as e:
        logger.error(f"❌ Sync cleanup failed: {e}")
        return {"success": False, "vms_cleaned": 0, "errors": [str(e)], "method": "none"}


def _run_cleanup_in_new_loop(timeout: float, reason: str) -> Dict[str, Any]:
    """Helper to run cleanup in a fresh event loop within a thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(
            cleanup_remote_resources(timeout=timeout, reason=reason)
        )
    finally:
        loop.close()


# ============================================================================
# SIGNAL HANDLERS
# ============================================================================

def _signal_handler(signum: int, frame: Any) -> None:
    """
    Signal handler for SIGTERM and SIGINT.
    
    Triggers cleanup and then calls the original handler.
    """
    signal_name = signal.Signals(signum).name
    logger.info(f"🛑 Received {signal_name} - triggering cleanup...")
    
    # Run synchronous cleanup
    cleanup_remote_resources_sync(timeout=15.0, reason=f"Signal {signal_name}")
    
    # Call original handler
    if signum == signal.SIGTERM and _original_sigterm:
        if callable(_original_sigterm) and _original_sigterm not in (signal.SIG_DFL, signal.SIG_IGN):
            _original_sigterm(signum, frame)
    elif signum == signal.SIGINT and _original_sigint:
        if callable(_original_sigint) and _original_sigint not in (signal.SIG_DFL, signal.SIG_IGN):
            _original_sigint(signum, frame)


def _atexit_handler() -> None:
    """
    atexit handler for final cleanup.
    
    This is the last line of defense for cleanup.
    """
    logger.info("🔚 atexit handler: Final cleanup check...")
    
    if not _cleanup_completed:
        cleanup_remote_resources_sync(timeout=10.0, reason="atexit handler")


# ============================================================================
# REGISTRATION
# ============================================================================

def register_handlers() -> None:
    """
    Register signal and atexit handlers.
    
    This is called automatically when the module is imported,
    but can also be called manually to re-register after fork.
    """
    global _original_sigterm, _original_sigint, _handlers_registered
    
    if _handlers_registered:
        logger.debug("Handlers already registered")
        return
    
    try:
        # Register signal handlers (preserve originals for chaining)
        _original_sigterm = signal.signal(signal.SIGTERM, _signal_handler)
        _original_sigint = signal.signal(signal.SIGINT, _signal_handler)
        
        # Register atexit handler
        atexit.register(_atexit_handler)
        
        _handlers_registered = True
        logger.debug("✅ Shutdown hook handlers registered (SIGTERM, SIGINT, atexit)")
        
    except Exception as e:
        logger.warning(f"⚠️ Failed to register some handlers: {e}")


def unregister_handlers() -> None:
    """
    Unregister signal handlers and restore originals.
    
    Useful for testing or when forking processes.
    """
    global _original_sigterm, _original_sigint, _handlers_registered
    
    if not _handlers_registered:
        return
    
    try:
        if _original_sigterm is not None:
            signal.signal(signal.SIGTERM, _original_sigterm)
        if _original_sigint is not None:
            signal.signal(signal.SIGINT, _original_sigint)
        
        # Note: atexit handlers can't be easily unregistered
        
        _handlers_registered = False
        logger.debug("✅ Shutdown hook handlers unregistered")
        
    except Exception as e:
        logger.warning(f"⚠️ Failed to unregister handlers: {e}")


# ============================================================================
# AUTO-REGISTRATION ON IMPORT
# ============================================================================

# Only auto-register if we're not being imported in a subprocess
# (check for environment variable set by main process)
if os.getenv("JARVIS_SHUTDOWN_HOOK_REGISTERED") != "1":
    # Mark as registered to prevent duplicate registration in subprocesses
    os.environ["JARVIS_SHUTDOWN_HOOK_REGISTERED"] = "1"
    register_handlers()


# ============================================================================
# CLI ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    """
    CLI entry point for manual cleanup.
    
    Usage:
        python -m backend.scripts.shutdown_hook
        python backend/scripts/shutdown_hook.py
    """
    import argparse
    
    parser = argparse.ArgumentParser(description="JARVIS GCP Resource Cleanup")
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Cleanup timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--reason",
        type=str,
        default="Manual CLI cleanup",
        help="Reason for cleanup (for logging)",
    )
    
    args = parser.parse_args()
    
    print(f"🧹 Running manual GCP resource cleanup...")
    print(f"   Timeout: {args.timeout}s")
    print(f"   Reason: {args.reason}")
    print()
    
    result = cleanup_remote_resources_sync(
        timeout=args.timeout,
        reason=args.reason,
    )
    
    print()
    print(f"Result: {result}")
    
    sys.exit(0 if result.get("success") else 1)
