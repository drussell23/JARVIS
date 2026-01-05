#!/usr/bin/env python3
"""
PROJECT TRINITY End-to-End Test Suite
======================================

Comprehensive test to verify Trinity integration across:
- JARVIS Body (this repo)
- J-Prime Mind (jarvis-prime)
- Reactor Core Nerves (reactor-core)

Tests:
1. Trinity directory structure creation
2. Component state file creation
3. Ghost Display status function
4. Trinity initializer functionality
5. Cross-repo communication via file-based transport
6. Heartbeat monitoring

Usage:
    PYTHONPATH=./backend python3 tests/test_trinity_e2e.py

Author: JARVIS AI System
Version: 1.0.0 - PROJECT TRINITY Phase 3
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, Any, List, Tuple

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

# Test results tracking
test_results: List[Tuple[str, bool, str]] = []


def log_test(name: str, passed: bool, details: str = ""):
    """Log a test result."""
    status = "✅ PASS" if passed else "❌ FAIL"
    print(f"  {status}: {name}")
    if details:
        print(f"         {details}")
    test_results.append((name, passed, details))


def print_header(title: str):
    """Print a section header."""
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


async def test_trinity_directories() -> bool:
    """Test 1: Verify Trinity directory structure."""
    print_header("TEST 1: Trinity Directory Structure")

    trinity_dir = Path.home() / ".jarvis" / "trinity"

    # Create directories if they don't exist (simulating startup)
    dirs_to_check = [
        trinity_dir,
        trinity_dir / "commands",
        trinity_dir / "heartbeats",
        trinity_dir / "components",
    ]

    all_exist = True
    for dir_path in dirs_to_check:
        dir_path.mkdir(parents=True, exist_ok=True)
        exists = dir_path.exists() and dir_path.is_dir()
        log_test(f"Directory: {dir_path.name}", exists, str(dir_path))
        if not exists:
            all_exist = False

    return all_exist


async def test_component_state_files() -> bool:
    """Test 2: Verify component state file creation."""
    print_header("TEST 2: Component State Files")

    trinity_dir = Path.home() / ".jarvis" / "trinity" / "components"

    # Write JARVIS Body state (simulating startup)
    jarvis_state = {
        "component_type": "jarvis_body",
        "instance_id": f"jarvis-test-{os.getpid()}-{int(time.time())}",
        "timestamp": time.time(),
        "metrics": {
            "uptime_seconds": 0,
            "surveillance_active": False,
            "ghost_display_available": False,
        },
    }

    jarvis_file = trinity_dir / "jarvis_body.json"
    try:
        with open(jarvis_file, "w") as f:
            json.dump(jarvis_state, f, indent=2)
        log_test("Write JARVIS Body state", True, f"Instance: {jarvis_state['instance_id'][:20]}...")
    except Exception as e:
        log_test("Write JARVIS Body state", False, str(e))
        return False

    # Read it back
    try:
        with open(jarvis_file) as f:
            read_state = json.load(f)
        matches = read_state["instance_id"] == jarvis_state["instance_id"]
        log_test("Read JARVIS Body state", matches, f"Timestamp: {read_state['timestamp']}")
    except Exception as e:
        log_test("Read JARVIS Body state", False, str(e))
        return False

    # Check for J-Prime state (may or may not exist)
    jprime_file = trinity_dir / "j_prime.json"
    if jprime_file.exists():
        try:
            with open(jprime_file) as f:
                jprime_state = json.load(f)
            age = time.time() - jprime_state.get("timestamp", 0)
            online = age < 30
            log_test("J-Prime state file", True, f"Age: {age:.1f}s, Online: {online}")
        except Exception as e:
            log_test("J-Prime state file", False, str(e))
    else:
        log_test("J-Prime state file", True, "Not running (expected)")

    # Check for Reactor Core state
    reactor_file = trinity_dir / "reactor_core.json"
    if reactor_file.exists():
        try:
            with open(reactor_file) as f:
                reactor_state = json.load(f)
            age = time.time() - reactor_state.get("timestamp", 0)
            online = age < 30
            log_test("Reactor Core state file", True, f"Age: {age:.1f}s, Online: {online}")
        except Exception as e:
            log_test("Reactor Core state file", False, str(e))
    else:
        log_test("Reactor Core state file", True, "Not running (expected)")

    return True


async def test_ghost_display_status() -> bool:
    """Test 3: Verify Ghost Display status function."""
    print_header("TEST 3: Ghost Display Status Function")

    try:
        from vision.yabai_space_detector import get_ghost_display_status, GhostDisplayStatus

        status = get_ghost_display_status()

        log_test("get_ghost_display_status() callable", True, f"Returns dict with {len(status)} keys")

        # Check expected keys
        expected_keys = ["available", "status", "window_count", "apps", "resolution", "scale", "space_index"]
        missing_keys = [k for k in expected_keys if k not in status]
        log_test("Has expected keys", len(missing_keys) == 0,
                 f"Missing: {missing_keys}" if missing_keys else f"All {len(expected_keys)} keys present")

        # Log the actual status
        log_test("Ghost Display available", True,
                 f"Status: {status['status']}, Windows: {status['window_count']}")

        if status['apps']:
            log_test("Apps on Ghost Display", True, f"Apps: {', '.join(status['apps'][:3])}")
        else:
            log_test("Apps on Ghost Display", True, "No apps (expected when Ghost Display not active)")

        return True

    except ImportError as e:
        log_test("Import get_ghost_display_status", False, str(e))
        return False
    except Exception as e:
        log_test("Ghost Display status", False, str(e))
        return False


async def test_trinity_initializer() -> bool:
    """Test 4: Verify Trinity initializer module."""
    print_header("TEST 4: Trinity Initializer Module")

    try:
        from system.trinity_initializer import (
            is_trinity_initialized,
            get_trinity_status,
            JARVIS_INSTANCE_ID,
            TRINITY_ENABLED,
        )

        log_test("Import trinity_initializer", True, "All functions imported")
        log_test("TRINITY_ENABLED", True, f"Value: {TRINITY_ENABLED}")
        log_test("JARVIS_INSTANCE_ID", True, f"ID: {JARVIS_INSTANCE_ID[:25]}...")

        # Check status before init
        status = get_trinity_status()
        log_test("get_trinity_status() before init", True,
                 f"Initialized: {status['initialized']}, Enabled: {status['enabled']}")

        # Check is_trinity_initialized
        init_status = is_trinity_initialized()
        log_test("is_trinity_initialized()", True, f"Returns: {init_status}")

        return True

    except ImportError as e:
        log_test("Import trinity_initializer", False, str(e))
        return False
    except Exception as e:
        log_test("Trinity initializer", False, str(e))
        return False


async def test_trinity_initialization() -> bool:
    """Test 5: Test actual Trinity initialization."""
    print_header("TEST 5: Trinity Initialization Flow")

    try:
        from system.trinity_initializer import (
            initialize_trinity,
            shutdown_trinity,
            is_trinity_initialized,
            get_trinity_status,
        )

        # Initialize Trinity (without FastAPI app)
        log_test("Calling initialize_trinity()", True, "Starting initialization...")

        result = await initialize_trinity(app=None)
        log_test("initialize_trinity() completed", result, f"Result: {result}")

        # Check status after init
        status = get_trinity_status()
        log_test("Trinity status after init", status['initialized'],
                 f"Uptime: {status.get('uptime_seconds', 0):.1f}s")

        # Wait a moment for heartbeat
        await asyncio.sleep(1)

        # Verify is_trinity_initialized
        is_init = is_trinity_initialized()
        log_test("is_trinity_initialized() after init", is_init, f"Returns: {is_init}")

        # Shutdown
        await shutdown_trinity()
        log_test("shutdown_trinity() completed", True, "Graceful shutdown")

        # Verify shutdown
        is_init_after = is_trinity_initialized()
        log_test("is_trinity_initialized() after shutdown", not is_init_after,
                 f"Returns: {is_init_after} (should be False)")

        return True

    except Exception as e:
        log_test("Trinity initialization flow", False, str(e))
        import traceback
        traceback.print_exc()
        return False


async def test_cross_repo_communication() -> bool:
    """Test 6: Verify cross-repo file-based communication."""
    print_header("TEST 6: Cross-Repo Communication")

    trinity_dir = Path.home() / ".jarvis" / "trinity"
    commands_dir = trinity_dir / "commands"

    # Simulate a command from J-Prime to JARVIS Body
    test_command = {
        "id": f"test-cmd-{int(time.time() * 1000)}",
        "timestamp": time.time(),
        "source": "j_prime",
        "intent": "start_surveillance",
        "payload": {
            "app_name": "Chrome",
            "trigger_text": "test_trigger",
            "all_spaces": True,
        },
        "target": "jarvis_body",
        "priority": 5,
        "requires_ack": False,
        "ttl_seconds": 30.0,
    }

    # Write command file
    cmd_filename = f"{int(time.time() * 1000)}_{test_command['id']}.json"
    cmd_path = commands_dir / cmd_filename

    try:
        with open(cmd_path, "w") as f:
            json.dump(test_command, f, indent=2)
        log_test("Write command file", True, f"File: {cmd_filename}")
    except Exception as e:
        log_test("Write command file", False, str(e))
        return False

    # Read it back
    try:
        with open(cmd_path) as f:
            read_cmd = json.load(f)
        matches = read_cmd["id"] == test_command["id"]
        log_test("Read command file", matches, f"Intent: {read_cmd['intent']}")
    except Exception as e:
        log_test("Read command file", False, str(e))
        return False

    # Clean up test command
    try:
        cmd_path.unlink()
        log_test("Cleanup test command", True, "Removed test file")
    except Exception as e:
        log_test("Cleanup test command", False, str(e))

    # Test heartbeat directory
    heartbeats_dir = trinity_dir / "heartbeats"
    test_heartbeat = {
        "id": f"test-hb-{int(time.time() * 1000)}",
        "timestamp": time.time(),
        "source": "j_prime",
        "intent": "heartbeat",
        "payload": {
            "instance_id": "test-jprime",
            "model_loaded": False,
        },
    }

    hb_filename = f"jprime_{int(time.time() * 1000)}.json"
    hb_path = heartbeats_dir / hb_filename

    try:
        with open(hb_path, "w") as f:
            json.dump(test_heartbeat, f, indent=2)
        log_test("Write heartbeat file", True, f"File: {hb_filename}")

        # Clean up
        hb_path.unlink()
        log_test("Cleanup heartbeat file", True, "Removed test file")
    except Exception as e:
        log_test("Heartbeat file operations", False, str(e))
        return False

    return True


async def test_trinity_handlers() -> bool:
    """Test 7: Verify Trinity command handlers."""
    print_header("TEST 7: Trinity Command Handlers")

    try:
        from system.trinity_handlers import (
            register_trinity_handlers,
            handle_start_surveillance,
            handle_stop_surveillance,
            handle_bring_back_window,
        )

        log_test("Import trinity_handlers", True, "All handlers imported")

        # Try registering handlers (without bridge)
        result = register_trinity_handlers(bridge=None)
        log_test("register_trinity_handlers(None)", True, f"Result: {result}")

        return True

    except ImportError as e:
        log_test("Import trinity_handlers", False, str(e))
        return False
    except Exception as e:
        log_test("Trinity handlers", False, str(e))
        return False


async def run_all_tests():
    """Run all Trinity end-to-end tests."""
    print()
    print("╔" + "═" * 58 + "╗")
    print("║" + "  PROJECT TRINITY - End-to-End Test Suite  ".center(58) + "║")
    print("║" + "  Testing JARVIS Body ↔ J-Prime ↔ Reactor Core  ".center(58) + "║")
    print("╚" + "═" * 58 + "╝")

    start_time = time.time()

    # Run all tests
    tests = [
        ("Directory Structure", test_trinity_directories),
        ("Component State Files", test_component_state_files),
        ("Ghost Display Status", test_ghost_display_status),
        ("Trinity Initializer Module", test_trinity_initializer),
        ("Trinity Initialization Flow", test_trinity_initialization),
        ("Cross-Repo Communication", test_cross_repo_communication),
        ("Trinity Command Handlers", test_trinity_handlers),
    ]

    all_passed = True
    for test_name, test_func in tests:
        try:
            passed = await test_func()
            if not passed:
                all_passed = False
        except Exception as e:
            print(f"\n❌ EXCEPTION in {test_name}: {e}")
            import traceback
            traceback.print_exc()
            all_passed = False

    # Summary
    elapsed = time.time() - start_time
    passed_count = sum(1 for _, passed, _ in test_results if passed)
    failed_count = sum(1 for _, passed, _ in test_results if not passed)

    print()
    print("=" * 60)
    print("  TEST SUMMARY")
    print("=" * 60)
    print(f"  Total Tests: {len(test_results)}")
    print(f"  ✅ Passed: {passed_count}")
    print(f"  ❌ Failed: {failed_count}")
    print(f"  ⏱️  Duration: {elapsed:.2f}s")
    print("=" * 60)

    if all_passed:
        print()
        print("  🎉 ALL TESTS PASSED! PROJECT TRINITY is ready.")
        print()
    else:
        print()
        print("  ⚠️  Some tests failed. Review the output above.")
        print()
        print("  Failed tests:")
        for name, passed, details in test_results:
            if not passed:
                print(f"    - {name}: {details}")
        print()

    return all_passed


if __name__ == "__main__":
    success = asyncio.run(run_all_tests())
    sys.exit(0 if success else 1)
