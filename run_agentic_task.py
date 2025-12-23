#!/usr/bin/env python3
"""
JARVIS Agentic Task Runner - CLI Interface
===========================================

Thin CLI wrapper for the unified AgenticTaskRunner.
Connects to the supervisor API when running, falls back to standalone mode.

This CLI can be used for:
- Executing tasks via the supervisor API (recommended)
- Standalone agentic task execution (when supervisor not running)
- Testing Computer Use capabilities
- Interactive task experimentation

Usage:
    # Interactive mode (auto-detects supervisor)
    python run_agentic_task.py

    # Execute single goal via supervisor API
    python run_agentic_task.py --goal "Open Safari and find the weather"

    # Force standalone mode (bypass supervisor)
    python run_agentic_task.py --goal "Open Mail" --standalone

    # Execute with specific mode
    python run_agentic_task.py --goal "Organize my desktop" --mode autonomous

    # With voice narration
    python run_agentic_task.py --goal "Connect to my TV" --narrate

    # Debug mode
    python run_agentic_task.py --goal "Open System Preferences" --debug

Author: JARVIS AI System
Version: 3.0.0 (Unified CLI - Supervisor Integration)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# =============================================================================
# CRITICAL: Setup paths and Python 3.9 compatibility FIRST
# =============================================================================

# Add backend to path FIRST
sys.path.insert(0, str(Path(__file__).parent / "backend"))

# Apply Python 3.9 compatibility patches BEFORE any other imports
# This patches importlib.metadata.packages_distributions() which google-api-core needs
try:
    from utils.python39_compat import ensure_python39_compatibility
    ensure_python39_compatibility()
except ImportError:
    # Fallback: manually patch if the module isn't available
    import importlib.metadata as metadata
    if not hasattr(metadata, 'packages_distributions'):
        def packages_distributions():
            return {}
        metadata.packages_distributions = packages_distributions

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        backend_env = Path(__file__).parent / "backend" / ".env"
        if backend_env.exists():
            load_dotenv(backend_env)
except ImportError:
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, _, value = line.partition('=')
                    value = value.strip().strip('"').strip("'")
                    os.environ.setdefault(key.strip(), value)

# Now safe to import everything else
import argparse
import asyncio
import json
import logging
from typing import Optional

# Default API endpoint
DEFAULT_API_BASE = os.getenv("JARVIS_API_BASE", "http://localhost:8000")


# ============================================================================
# Logging Setup
# ============================================================================

def setup_logging(debug: bool = False, log_file: Path = None) -> logging.Logger:
    """Configure logging for the CLI."""
    level = logging.DEBUG if debug else logging.INFO

    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%H:%M:%S'
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.addHandler(console_handler)

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    return logging.getLogger("agentic_cli")


# ============================================================================
# API Client Functions
# ============================================================================

async def check_supervisor_running(api_base: str, logger: logging.Logger) -> bool:
    """Check if the supervisor/backend API is running."""
    try:
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2.0)) as session:
            async with session.get(f"{api_base}/api/agentic/health") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    status = data.get("status", "")
                    if status in ("healthy", "initializing"):
                        logger.debug(f"Supervisor API healthy: {data}")
                        return True
                    logger.debug(f"Supervisor API status: {status}")
                    return status == "healthy"
        return False
    except ImportError:
        logger.debug("aiohttp not installed, cannot check supervisor")
        return False
    except Exception as e:
        logger.debug(f"Supervisor not available: {e}")
        return False


async def execute_via_api(
    goal: str,
    mode: str,
    narrate: bool,
    api_base: str,
    logger: logging.Logger,
    timeout: float = 300.0,
) -> int:
    """Execute a goal via the supervisor API."""
    import aiohttp

    logger.info(f"Executing via Supervisor API: {api_base}")
    logger.info(f"Goal: {goal}")
    logger.info(f"Mode: {mode}")
    logger.info("-" * 40)

    payload = {
        "goal": goal,
        "mode": mode,
        "narrate": narrate,
        "timeout_seconds": timeout,
    }

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout + 10)
        ) as session:
            async with session.post(
                f"{api_base}/api/agentic/execute",
                json=payload,
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    print_api_result(result)
                    return 0 if result.get("success") else 1
                elif resp.status == 503:
                    error = await resp.json()
                    logger.error(f"API Error (503): {error.get('detail', 'Service unavailable')}")
                    logger.info("Supervisor may still be initializing. Try again in a moment.")
                    return 1
                elif resp.status == 403:
                    error = await resp.json()
                    logger.error(f"Access Denied (403): {error.get('detail', 'Blocked by watchdog')}")
                    return 1
                else:
                    error = await resp.text()
                    logger.error(f"API Error ({resp.status}): {error}")
                    return 1

    except asyncio.TimeoutError:
        logger.error(f"Request timed out after {timeout}s")
        return 1
    except aiohttp.ClientError as e:
        logger.error(f"Connection error: {e}")
        return 1


def print_api_result(result: dict):
    """Print the result from the API."""
    print("\n" + "=" * 40)
    print("RESULT (via Supervisor API)")
    print("=" * 40)
    print(f"Success: {result.get('success')}")
    print(f"Status: {result.get('status')}")
    print(f"Message: {result.get('final_message')}")
    print(f"Time: {result.get('execution_time_ms', 0):.0f}ms")
    print(f"Actions: {result.get('actions_count', 0)}")
    print(f"Mode: {result.get('mode')}")

    if result.get('learning_insights'):
        print("\nInsights:")
        for insight in result['learning_insights']:
            print(f"  - {insight}")

    if result.get('watchdog_status'):
        print(f"\nWatchdog: {result['watchdog_status']}")

    if result.get('error'):
        print(f"\nError: {result['error']}")


async def get_api_status(api_base: str, logger: logging.Logger) -> Optional[dict]:
    """Get the current status from the API."""
    try:
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5.0)) as session:
            async with session.get(f"{api_base}/api/agentic/status") as resp:
                if resp.status == 200:
                    return await resp.json()
        return None
    except Exception as e:
        logger.debug(f"Could not get API status: {e}")
        return None


# ============================================================================
# Standalone Execution (when supervisor not running)
# ============================================================================

async def run_single_task_standalone(
    goal: str,
    mode: str,
    narrate: bool,
    logger: logging.Logger,
) -> int:
    """Execute a single agentic task in standalone mode."""
    from core.agentic_task_runner import (
        AgenticTaskRunner,
        AgenticRunnerConfig,
        RunnerMode,
    )

    logger.info("Running in STANDALONE mode (supervisor not available)")
    logger.info(f"Goal: {goal}")
    logger.info(f"Mode: {mode}")
    logger.info(f"Narrate: {narrate}")
    logger.info("-" * 40)

    # Create TTS callback if narration enabled
    tts_callback = None
    if narrate:
        try:
            from voice.text_to_speech_service import TextToSpeechService
            tts = TextToSpeechService()

            async def speak(text: str):
                await tts.speak(text)

            tts_callback = speak
        except ImportError:
            logger.warning("TTS not available, narration disabled")

    # Create and initialize runner
    config = AgenticRunnerConfig()
    runner = AgenticTaskRunner(
        config=config,
        tts_callback=tts_callback,
        logger=logger,
    )

    try:
        await runner.initialize()

        # Execute the task
        result = await runner.run(
            goal=goal,
            mode=RunnerMode(mode),
            narrate=narrate,
        )

        # Print results
        print("\n" + "=" * 40)
        print("RESULT (Standalone)")
        print("=" * 40)
        print(f"Success: {result.success}")
        print(f"Message: {result.final_message}")
        print(f"Time: {result.execution_time_ms:.0f}ms")
        print(f"Actions: {result.actions_count}")
        print(f"Mode: {result.mode}")

        if result.learning_insights:
            print("\nInsights:")
            for insight in result.learning_insights:
                print(f"  - {insight}")

        if result.error:
            print(f"\nError: {result.error}")

        return 0 if result.success else 1

    finally:
        await runner.shutdown()


async def interactive_mode(
    api_base: str,
    use_api: bool,
    logger: logging.Logger,
):
    """Run in interactive REPL mode."""
    from core.agentic_task_runner import (
        AgenticTaskRunner,
        AgenticRunnerConfig,
        RunnerMode,
    )

    print("\n" + "=" * 60)
    print("JARVIS Agentic Task Runner - Interactive Mode")
    if use_api:
        print(f"  Connected to: {api_base}")
    else:
        print("  Running in STANDALONE mode")
    print("=" * 60)
    print("\nCommands:")
    print("  Type a goal to execute it")
    print("  /mode <autonomous|supervised|direct> - Change mode")
    print("  /status - Show system status")
    print("  /quit - Exit")
    print()

    runner = None
    mode = RunnerMode.SUPERVISED

    if not use_api:
        # Create standalone runner
        config = AgenticRunnerConfig()
        runner = AgenticTaskRunner(config=config, logger=logger)
        await runner.initialize()

    try:
        while True:
            try:
                prompt = f"[{mode.value}]"
                if use_api:
                    prompt += " (API)"
                user_input = input(f"{prompt} Goal> ").strip()

                if not user_input:
                    continue

                if user_input.lower() in ('/quit', '/exit', 'quit', 'exit'):
                    break

                if user_input.startswith('/mode '):
                    mode_str = user_input[6:].strip().lower()
                    try:
                        mode = RunnerMode(mode_str)
                        print(f"Mode changed to: {mode.value}")
                    except ValueError:
                        print("Invalid mode. Options: autonomous, supervised, direct")
                    continue

                if user_input == '/status':
                    if use_api:
                        status = await get_api_status(api_base, logger)
                        if status:
                            print(json.dumps(status, indent=2))
                        else:
                            print("Could not fetch API status")
                    elif runner:
                        stats = runner.get_stats()
                        print(json.dumps(stats, indent=2))
                    continue

                # Execute goal
                print(f"\nExecuting: {user_input}")
                print("-" * 40)

                if use_api:
                    exit_code = await execute_via_api(
                        goal=user_input,
                        mode=mode.value,
                        narrate=True,
                        api_base=api_base,
                        logger=logger,
                    )
                    if exit_code != 0:
                        print("Execution failed. See logs for details.")
                elif runner:
                    result = await runner.run(
                        goal=user_input,
                        mode=mode,
                        narrate=True,
                    )

                    print("-" * 40)
                    print(f"Success: {result.success}")
                    print(f"Message: {result.final_message}")
                    print(f"Time: {result.execution_time_ms:.0f}ms")
                    print(f"Actions: {result.actions_count}")

                    if result.learning_insights:
                        print("Insights:")
                        for insight in result.learning_insights:
                            print(f"  - {insight}")

                print()

            except KeyboardInterrupt:
                print("\nInterrupted")
                break
            except EOFError:
                break

    finally:
        if runner:
            await runner.shutdown()

    print("Goodbye!")


# ============================================================================
# Main Entry Point
# ============================================================================

async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="JARVIS Agentic Task Runner CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_agentic_task.py
  python run_agentic_task.py --goal "Open Safari and find the weather"
  python run_agentic_task.py --goal "Organize my desktop" --mode autonomous
  python run_agentic_task.py --goal "Open Mail" --narrate --debug
  python run_agentic_task.py --goal "Test" --standalone  # Force standalone
"""
    )

    parser.add_argument(
        "--goal", "-g",
        help="Goal to execute (if not provided, enters interactive mode)"
    )

    parser.add_argument(
        "--mode", "-m",
        choices=["direct", "supervised", "autonomous"],
        default="supervised",
        help="Execution mode (default: supervised)"
    )

    parser.add_argument(
        "--narrate", "-n",
        action="store_true",
        help="Enable voice narration"
    )

    parser.add_argument(
        "--standalone", "-s",
        action="store_true",
        help="Force standalone mode (bypass supervisor API)"
    )

    parser.add_argument(
        "--api-base",
        default=DEFAULT_API_BASE,
        help=f"API base URL (default: {DEFAULT_API_BASE})"
    )

    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Task timeout in seconds (default: 300)"
    )

    parser.add_argument(
        "--debug", "-d",
        action="store_true",
        help="Enable debug logging"
    )

    parser.add_argument(
        "--log-file",
        type=Path,
        help="Log to file"
    )

    args = parser.parse_args()

    # Setup logging
    logger = setup_logging(debug=args.debug, log_file=args.log_file)

    # Print banner
    print("\n" + "=" * 60)
    print("JARVIS Agentic Task Runner v3.0 (Unified)")
    print("=" * 60)

    # Determine execution mode
    use_api = False
    if not args.standalone:
        logger.info("Checking for supervisor API...")
        use_api = await check_supervisor_running(args.api_base, logger)
        if use_api:
            logger.info(f"Supervisor API available at {args.api_base}")
        else:
            logger.info("Supervisor not running, using standalone mode")

    # Run
    if args.goal:
        if use_api:
            return await execute_via_api(
                goal=args.goal,
                mode=args.mode,
                narrate=args.narrate,
                api_base=args.api_base,
                logger=logger,
                timeout=args.timeout,
            )
        else:
            return await run_single_task_standalone(
                goal=args.goal,
                mode=args.mode,
                narrate=args.narrate,
                logger=logger,
            )
    else:
        await interactive_mode(
            api_base=args.api_base,
            use_api=use_api,
            logger=logger,
        )
        return 0


if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(1)
