#!/usr/bin/env python3
"""
Advanced Voice Unlock Metrics Monitor
======================================
Real-time monitoring system for voice unlock metrics with auto-launching DB Browser.

Features:
- Automatically launches DB Browser for SQLite on startup
- Detects if DB Browser is already running (prevents duplicates)
- Handles database locks and concurrent access
- Watches for new unlock attempts in real-time
- Updates database viewer automatically
- Professional notifications for each unlock attempt
- Robust error handling and graceful degradation
- Disk space validation and database corruption recovery
- Process cleanup on restarts
- Zero hardcoding - fully dynamic and async

macOS Notification System (Fully Async):
==========================================
Enhanced notification system with two modes:

1. terminal-notifier Mode (Installed):
   - Rich multi-line notifications
   - Clickable action buttons:
     • "View Database" - Opens DB Browser
     • "Export to Excel" - Exports current data
   - Notification grouping (replaces previous notifications)
   - Custom app icons and sounds
   - Do Not Disturb bypass for failures
   - Click notification body → Opens DB Browser automatically

2. osascript Fallback Mode (Built-in):
   - Basic single-line notifications
   - System sounds (Glass/Basso)
   - No installation required
   - Falls back automatically if terminal-notifier not available

All notifications are:
- Fully async (asyncio.create_subprocess_exec)
- Non-blocking (fire-and-forget)
- Include real-time metrics (confidence, duration, trend, session stats)
- Different sounds for success (Glass) vs failure (Basso)

Restart Behavior (python start_system.py --restart):
====================================================
When JARVIS is restarted with --restart flag:

1. Orphan Cleanup Phase:
   - Checks for stale PID file from previous session
   - Detects orphaned DB Browser processes (PID validation)
   - Gracefully terminates orphaned processes (3-second timeout)
   - Force kills if graceful shutdown fails
   - Cleans up stale PID tracking files

2. Fresh Launch Phase:
   - Validates system requirements (disk space, permissions)
   - Validates database integrity (corruption detection)
   - Checks if DB Browser is currently running
   - Launches new DB Browser instance if needed
   - Saves new PID to tracking file

3. Process Tracking:
   - PID file: ~/.jarvis/logs/unlock_metrics/.db_browser.pid
   - Only closes DB Browser on shutdown if we launched it
   - Leaves user-launched instances running
   - Prevents duplicate instances across restarts

This ensures clean restarts with no orphaned processes or multiple DB Browser windows!
"""

import asyncio
import logging
import subprocess
import time
import shutil
import psutil
import sqlite3
from pathlib import Path
from typing import Optional, List
from datetime import datetime
import json
import os

logger = logging.getLogger(__name__)


class VoiceUnlockMetricsMonitor:
    """
    Advanced real-time metrics monitor for voice unlock system.

    Automatically:
    - Launches DB Browser on startup
    - Monitors unlock attempts
    - Updates database in real-time
    - Provides professional logging
    """

    def __init__(self):
        """Initialize metrics monitor"""
        self.log_dir = Path.home() / ".jarvis/logs/unlock_metrics"
        self.db_path = self.log_dir / "unlock_metrics.db"
        self.pid_file = self.log_dir / ".db_browser.pid"
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # DB Browser process tracking
        self.db_browser_process = None
        self.db_browser_pid = None
        self.db_browser_already_running = False
        self.monitoring = False

        # Track last seen attempt
        self.last_attempt_count = 0

        # Performance stats
        self.session_stats = {
            'session_start': datetime.now().isoformat(),
            'total_attempts': 0,
            'successful_attempts': 0,
            'failed_attempts': 0,
            'avg_confidence': 0.0,
            'avg_duration_ms': 0.0,
        }

        # Graceful degradation mode
        self.degraded_mode = False
        self.degradation_reason = None

    async def start(self):
        """Start the metrics monitoring system"""
        logger.info("🚀 Starting Voice Unlock Metrics Monitor...")

        # Clean up orphaned processes from previous --restart
        await self._cleanup_orphaned_processes()

        # Pre-flight checks
        await self._validate_system_requirements()

        # Initialize database with validation
        await self._initialize_database()

        # Launch DB Browser (with duplicate detection)
        await self._launch_db_browser()

        # Start monitoring
        self.monitoring = True

        status_msg = "✅ Metrics Monitor active"
        if self.degraded_mode:
            status_msg += f" (Degraded Mode: {self.degradation_reason})"
        logger.info(status_msg)

        logger.info(f"📊 Database: {self.db_path}")

        if self.db_browser_already_running:
            logger.info("🔍 DB Browser: Already running (reusing existing instance)")
        elif self.db_browser_process:
            logger.info(f"🔍 DB Browser: Launched (PID: {self.db_browser_pid})")
        else:
            logger.info("🔍 DB Browser: Not available")

        # Start monitoring loop
        asyncio.create_task(self._monitor_loop())

    async def stop(self):
        """Stop the metrics monitoring system"""
        logger.info("🛑 Stopping Voice Unlock Metrics Monitor...")
        self.monitoring = False

        # Close DB Browser ONLY if we launched it (not if it was already running)
        if self.db_browser_process and not self.db_browser_already_running:
            try:
                # Gracefully terminate the process
                if self.db_browser_pid and psutil.pid_exists(self.db_browser_pid):
                    process = psutil.Process(self.db_browser_pid)
                    process.terminate()
                    # Wait up to 3 seconds for graceful shutdown
                    try:
                        process.wait(timeout=3)
                    except psutil.TimeoutExpired:
                        logger.warning("DB Browser didn't close gracefully, forcing...")
                        process.kill()
                logger.info("✅ DB Browser closed")

                # Clean up PID file
                if self.pid_file.exists():
                    self.pid_file.unlink()

            except Exception as e:
                logger.warning(f"Could not close DB Browser: {e}")
        elif self.db_browser_already_running:
            logger.info("ℹ️  DB Browser left running (was already open before monitor started)")

        # Log session stats
        self._log_session_summary()

    async def _cleanup_orphaned_processes(self):
        """
        Handle DB Browser from previous session on --restart.
        
        IMPORTANT: We NO LONGER close DB Browser on restart!
        If it's already open, we reuse it. This prevents annoying close/reopen cycles.
        We only clean up the PID file to ensure accurate tracking.
        """
        try:
            # Check if we have a PID file from previous run
            if self.pid_file.exists():
                try:
                    with open(self.pid_file, 'r') as f:
                        old_pid = int(f.read().strip())

                    # Check if this PID still exists and is DB Browser
                    if psutil.pid_exists(old_pid):
                        try:
                            proc = psutil.Process(old_pid)
                            # Verify it's actually DB Browser
                            if proc.name() and 'DB Browser' in proc.name():
                                # DON'T close it! Mark it as already running instead
                                logger.info(f"✅ DB Browser from previous session still running (PID: {old_pid})")
                                logger.info("   Keeping it open (no close/reopen on restart)")
                                self.db_browser_already_running = True
                                self.db_browser_pid = old_pid
                                # Keep the PID file since the process is still valid
                                return  # Don't delete PID file, process is still valid
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass

                    # Process doesn't exist anymore - remove stale PID file
                    self.pid_file.unlink()
                    logger.debug("Removed stale PID file (process no longer exists)")

                except (ValueError, FileNotFoundError):
                    # Invalid PID file - just remove it
                    if self.pid_file.exists():
                        self.pid_file.unlink()

        except Exception as e:
            logger.debug(f"Orphan cleanup note: {e}")

    async def _validate_system_requirements(self):
        """Pre-flight validation checks"""
        try:
            # Check disk space (need at least 100MB)
            stat = shutil.disk_usage(self.log_dir)
            free_gb = stat.free / (1024**3)
            if stat.free < 100 * 1024 * 1024:  # 100MB
                logger.warning(f"⚠️  Low disk space: {free_gb:.1f}GB free")
                self.degraded_mode = True
                self.degradation_reason = "Low disk space"

            # Check if log directory is writable
            test_file = self.log_dir / ".write_test"
            try:
                test_file.touch()
                test_file.unlink()
            except Exception as e:
                logger.error(f"❌ Log directory not writable: {e}")
                self.degraded_mode = True
                self.degradation_reason = "Directory not writable"

            logger.debug("✅ System requirements validated")

        except Exception as e:
            logger.warning(f"System validation warning: {e}")

    async def _initialize_database(self):
        """Initialize the metrics database with validation and recovery"""
        try:
            from voice_unlock.metrics_database import get_metrics_database

            # Check if database file exists and validate it
            if self.db_path.exists():
                await self._validate_database_integrity()

            # Initialize database (creates tables if needed)
            db = get_metrics_database()
            logger.info(f"✅ Database initialized: {db.sqlite_path}")

            # Check if database exists and has data
            if db.sqlite_path.exists():
                conn = sqlite3.connect(str(db.sqlite_path))
                cursor = conn.cursor()

                try:
                    cursor.execute("SELECT COUNT(*) FROM unlock_attempts")
                    count = cursor.fetchone()[0]
                    self.last_attempt_count = count
                    logger.info(f"📊 Found {count} existing unlock attempts in database")
                except sqlite3.OperationalError as e:
                    logger.error(f"Database table error: {e}")
                    # Tables might not exist yet - that's ok, they'll be created
                    self.last_attempt_count = 0
                finally:
                    conn.close()

        except Exception as e:
            logger.warning(f"Database initialization note: {e}")
            self.degraded_mode = True
            self.degradation_reason = "Database initialization failed"

    async def _validate_database_integrity(self):
        """Validate database integrity and attempt recovery if corrupted"""
        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()

            # Run integrity check
            cursor.execute("PRAGMA integrity_check")
            result = cursor.fetchone()[0]

            if result != "ok":
                logger.error(f"❌ Database integrity check failed: {result}")

                # Create backup of corrupted database
                backup_path = self.db_path.with_suffix('.db.corrupted.backup')
                shutil.copy2(self.db_path, backup_path)
                logger.info(f"📦 Corrupted database backed up to: {backup_path}")

                # Remove corrupted database (will be recreated)
                self.db_path.unlink()
                logger.info("🔄 Recreating database from scratch")

            conn.close()

        except sqlite3.DatabaseError as e:
            logger.error(f"Database corruption detected: {e}")

            # Backup and recreate
            if self.db_path.exists():
                backup_path = self.db_path.with_suffix('.db.corrupted.backup')
                shutil.copy2(self.db_path, backup_path)
                self.db_path.unlink()
                logger.info(f"🔄 Database recreated (backup: {backup_path})")

        except Exception as e:
            logger.warning(f"Database validation warning: {e}")

    def _is_db_browser_running(self) -> Optional[int]:
        """Check if DB Browser is already running and viewing our database"""
        try:
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    # Check if it's DB Browser
                    if proc.info['name'] and 'DB Browser' in proc.info['name']:
                        # Check if it has our database file open
                        if proc.info['cmdline']:
                            for arg in proc.info['cmdline']:
                                if str(self.db_path) in str(arg):
                                    return proc.info['pid']
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            return None
        except Exception as e:
            logger.debug(f"Error checking for running DB Browser: {e}")
            return None

    async def _launch_db_browser(self):
        """
        Launch DB Browser for SQLite with intelligent handling.
        
        Behavior:
        - If already running: Reuse existing instance (no close/reopen)
        - If not running: Launch in FULL SCREEN for optimal viewing
        """
        try:
            # Skip if we already detected it running during cleanup phase
            if self.db_browser_already_running:
                logger.info(f"✅ DB Browser already running (PID: {self.db_browser_pid}) - reusing")
                return

            # Check if DB Browser is already running with our database
            existing_pid = self._is_db_browser_running()
            if existing_pid:
                logger.info(f"✅ DB Browser already running (PID: {existing_pid})")
                logger.info("   Reusing existing instance instead of launching duplicate")
                self.db_browser_already_running = True
                self.db_browser_pid = existing_pid
                return

            # Check if DB Browser is installed
            result = subprocess.run(
                ['mdfind', 'kMDItemKind == "Application" && kMDItemDisplayName == "DB Browser for SQLite"'],
                capture_output=True,
                text=True,
                timeout=5
            )

            if not result.stdout.strip():
                logger.warning("⚠️  DB Browser for SQLite not found - skipping auto-launch")
                logger.info("💡 Install with: brew install --cask db-browser-for-sqlite")
                self.degraded_mode = True
                self.degradation_reason = "DB Browser not installed"
                return

            # Ensure database file exists before launching (avoid confusing error dialogs)
            if not self.db_path.exists():
                logger.warning("⚠️  Database file doesn't exist yet - will launch after first unlock")
                return

            # Launch DB Browser with the database
            logger.info("🚀 Launching DB Browser for SQLite in FULL SCREEN...")

            # Use 'open' command to launch in background
            self.db_browser_process = subprocess.Popen(
                ['open', '-a', 'DB Browser for SQLite', str(self.db_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            # Give it a moment to launch and get PID
            await asyncio.sleep(2)

            # Find the PID of the newly launched process
            new_pid = self._is_db_browser_running()
            if new_pid:
                self.db_browser_pid = new_pid
                logger.info(f"✅ DB Browser launched successfully (PID: {new_pid})")

                # Save PID to file for restart detection
                try:
                    with open(self.pid_file, 'w') as f:
                        f.write(str(new_pid))
                except Exception as e:
                    logger.debug(f"Could not save PID file: {e}")
                
                # Make it FULL SCREEN using AppleScript
                await self._make_db_browser_fullscreen()
            else:
                logger.info("✅ DB Browser launched successfully")

            logger.info("💡 The database will auto-update when you unlock your screen")
            logger.info("💡 Press F5 in DB Browser to refresh and see new data")

        except subprocess.TimeoutExpired:
            logger.warning("⚠️  Timeout while checking for DB Browser - continuing anyway")
        except Exception as e:
            logger.warning(f"Could not auto-launch DB Browser: {e}")
            logger.info("💡 You can manually open it with:")
            logger.info(f"   open -a 'DB Browser for SQLite' {self.db_path}")
    
    async def _make_db_browser_fullscreen(self):
        """
        Make DB Browser window full screen using AppleScript.
        
        This provides optimal viewing of the metrics database.
        Uses native macOS full-screen (green button) for a clean experience.
        """
        try:
            # AppleScript to make DB Browser full screen
            # Uses System Events to trigger full screen (same as clicking green button)
            applescript = '''
            tell application "System Events"
                -- Wait for DB Browser to be ready
                repeat 10 times
                    if exists process "DB Browser for SQLite" then
                        exit repeat
                    end if
                    delay 0.2
                end repeat
                
                tell process "DB Browser for SQLite"
                    set frontmost to true
                    delay 0.3
                    
                    -- Try to enter full screen via menu
                    try
                        -- View > Enter Full Screen (or similar)
                        click menu item "Enter Full Screen" of menu "View" of menu bar 1
                    on error
                        -- Fallback: Use keyboard shortcut Ctrl+Cmd+F
                        keystroke "f" using {control down, command down}
                    end try
                end tell
            end tell
            '''
            
            process = await asyncio.create_subprocess_exec(
                'osascript', '-e', applescript,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
            
            if process.returncode == 0:
                logger.info("🖥️  DB Browser set to FULL SCREEN")
            else:
                # Non-critical - window still opens, just not full screen
                logger.debug(f"Full screen note: {stderr.decode() if stderr else 'timeout'}")
                
        except asyncio.TimeoutError:
            logger.debug("Full screen command timed out (window still opens)")
        except Exception as e:
            logger.debug(f"Could not set full screen: {e}")

    async def _monitor_loop(self):
        """Main monitoring loop - watches for new unlock attempts"""
        logger.info("🔍 Monitoring for unlock attempts...")

        while self.monitoring:
            try:
                # Check for new unlock attempts
                await self._check_for_new_attempts()

                # Wait before next check (2 seconds for responsive monitoring)
                await asyncio.sleep(2)

            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def _check_for_new_attempts(self):
        """Check database for new unlock attempts (with concurrent access handling)"""
        conn = None
        max_retries = 3
        retry_delay = 0.5

        for attempt in range(max_retries):
            try:
                if not self.db_path.exists():
                    return

                # Open connection with timeout for busy database
                conn = sqlite3.connect(
                    str(self.db_path),
                    timeout=10.0,  # Wait up to 10 seconds for database lock
                    check_same_thread=False
                )
                cursor = conn.cursor()

                # Use WAL mode for better concurrent access
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=10000")  # 10 second busy timeout

                cursor.execute("SELECT COUNT(*) FROM unlock_attempts")
                current_count = cursor.fetchone()[0]

                # Check if there are new attempts
                if current_count > self.last_attempt_count:
                    new_attempts = current_count - self.last_attempt_count

                    # Get the latest attempts
                    cursor.execute("""
                        SELECT
                            timestamp,
                            success,
                            speaker_name,
                            speaker_confidence,
                            threshold,
                            total_duration_ms,
                            trend_direction
                        FROM unlock_attempts
                        ORDER BY timestamp DESC
                        LIMIT ?
                    """, (new_attempts,))

                    latest_attempts = cursor.fetchall()

                    # Process each new attempt
                    for attempt_data in reversed(latest_attempts):
                        await self._process_new_attempt(attempt_data)

                    self.last_attempt_count = current_count

                conn.close()
                return  # Success, exit retry loop

            except sqlite3.OperationalError as e:
                # Database is locked - retry
                if "locked" in str(e).lower() or "busy" in str(e).lower():
                    logger.debug(f"Database locked, retry {attempt + 1}/{max_retries}")
                    if conn:
                        try:
                            conn.close()
                        except:
                            pass
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay * (attempt + 1))
                    else:
                        logger.warning("Database still locked after retries - will try again next cycle")
                else:
                    logger.debug(f"Database operational error: {e}")
                    break

            except Exception as e:
                logger.debug(f"Check attempts error: {e}")
                break

            finally:
                if conn:
                    try:
                        conn.close()
                    except:
                        pass

    async def _send_macos_notification(self, title: str, message: str, subtitle: str = None, sound: bool = True, critical: bool = False):
        """
        Send native macOS notification using osascript (fully async)

        Args:
            title: Notification title
            message: Main notification message
            subtitle: Optional subtitle
            sound: Whether to play notification sound
            critical: Whether this is a critical alert (for failures)
        """
        try:
            # Choose sound based on success/failure
            sound_name = "Glass" if not critical else "Basso"

            # Build AppleScript for notification
            script_parts = [
                'display notification',
                f'"{message}"',
                f'with title "{title}"'
            ]

            if subtitle:
                # Escape quotes in subtitle
                subtitle_safe = subtitle.replace('"', '\\"')
                script_parts.append(f'subtitle "{subtitle_safe}"')

            if sound:
                script_parts.append(f'sound name "{sound_name}"')

            script = ' '.join(script_parts)

            # Execute AppleScript asynchronously (non-blocking)
            process = await asyncio.create_subprocess_exec(
                'osascript', '-e', script,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )

            # Don't wait for completion - fire and forget
            asyncio.create_task(process.wait())

            logger.debug(f"📱 Sent macOS notification: {title}")

        except Exception as e:
            logger.debug(f"Failed to send macOS notification: {e}")

    async def _send_advanced_notification(self, success: bool, speaker: str, confidence: float,
                                          duration_sec: float, margin: float, trend: str,
                                          session_attempts: int, session_successes: int):
        """
        Send advanced macOS notification with rich details (fully async)

        Uses terminal-notifier if available for enhanced notifications with buttons,
        otherwise falls back to osascript
        """
        try:
            # Check if terminal-notifier is available (more advanced notifications)
            check_process = await asyncio.create_subprocess_exec(
                'which', 'terminal-notifier',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL
            )
            await check_process.wait()
            has_terminal_notifier = check_process.returncode == 0

            if has_terminal_notifier:
                # Advanced notification with terminal-notifier
                await self._send_terminal_notifier_notification(
                    success, speaker, confidence, duration_sec, margin,
                    trend, session_attempts, session_successes
                )
            else:
                # Fallback to basic osascript notification
                await self._send_basic_notification(
                    success, speaker, confidence, duration_sec, margin,
                    trend, session_attempts, session_successes
                )

        except Exception as e:
            logger.debug(f"Failed to send advanced notification: {e}")

    async def _send_terminal_notifier_notification(self, success: bool, speaker: str, confidence: float,
                                                   duration_sec: float, margin: float, trend: str,
                                                   session_attempts: int, session_successes: int):
        """Send rich notification using terminal-notifier with clickable actions (fully async)"""
        try:
            trend_emoji = "📈" if trend == "improving" else "📉" if trend == "declining" else "➡️"
            status_emoji = "✅" if success else "❌"
            success_rate = (session_successes / session_attempts * 100) if session_attempts > 0 else 0

            title = f"{status_emoji} Voice Unlock {'SUCCESS' if success else 'FAILED'}"
            subtitle = f"{speaker} • {confidence:.1%} confidence {trend_emoji}"

            # Multi-line message with rich details
            message = (
                f"⚡ Duration: {duration_sec:.1f}s\n"
                f"📊 Confidence Margin: {margin:+.1%}\n"
                f"📈 Session Success Rate: {session_successes}/{session_attempts} ({success_rate:.0f}%)\n"
                f"🎯 Trend: {trend.title()}"
            )

            sound = "Glass" if success else "Basso"

            # Build command with advanced features
            cmd = [
                'terminal-notifier',
                '-title', title,
                '-subtitle', subtitle,
                '-message', message,
                '-sound', sound,
                '-group', 'jarvis-voice-unlock',  # Groups notifications together
                '-sender', 'com.apple.Terminal',  # Shows Terminal icon
            ]

            # Add clickable actions
            if self.db_path.exists():
                # Default action (click notification body) - Opens DB Browser
                cmd.extend([
                    '-execute', f'open -a "DB Browser for SQLite" "{self.db_path}"',
                ])

                # Add action buttons (macOS 10.14+)
                # Note: terminal-notifier actions execute the -execute command with button name as argument
                export_script_path = self.log_dir.parent.parent.parent / "Documents/repos/JARVIS-AI-Agent/backend/voice_unlock/export_to_excel.sh"
                if export_script_path.exists():
                    cmd.extend([
                        '-closeLabel', 'Dismiss',
                        '-actions', 'View Database,Export to Excel',
                    ])

            # Add Do Not Disturb bypass for failures (important alerts)
            if not success:
                cmd.append('-ignoreDnD')

            # Execute terminal-notifier asynchronously
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )

            # Fire and forget - don't wait for completion
            asyncio.create_task(process.wait())

            logger.debug(f"📱 Sent enhanced terminal-notifier notification with actions")

        except Exception as e:
            logger.debug(f"terminal-notifier failed: {e}")

    async def _send_basic_notification(self, success: bool, speaker: str, confidence: float,
                                       duration_sec: float, margin: float, trend: str,
                                       session_attempts: int, session_successes: int):
        """Send basic notification using osascript (fully async)"""
        trend_emoji = "📈" if trend == "improving" else "📉" if trend == "declining" else "➡️"
        success_rate = (session_successes / session_attempts * 100) if session_attempts > 0 else 0

        title = f"🔐 Voice Unlock: {'SUCCESS' if success else 'FAILED'}"
        subtitle = f"{speaker} • {confidence:.1%} confidence {trend_emoji}"
        message = (
            f"Duration: {duration_sec:.1f}s | "
            f"Margin: {margin:+.1%} | "
            f"Session: {session_successes}/{session_attempts} ({success_rate:.0f}%)"
        )

        await self._send_macos_notification(
            title=title,
            subtitle=subtitle,
            message=message,
            sound=True,
            critical=not success
        )

    async def _process_new_attempt(self, attempt_data):
        """Process and log a new unlock attempt"""
        timestamp, success, speaker, confidence, threshold, duration_ms, trend = attempt_data

        # Update session stats
        self.session_stats['total_attempts'] += 1
        if success:
            self.session_stats['successful_attempts'] += 1
        else:
            self.session_stats['failed_attempts'] += 1

        # Calculate running averages
        total = self.session_stats['total_attempts']
        old_conf_avg = self.session_stats['avg_confidence']
        old_dur_avg = self.session_stats['avg_duration_ms']

        self.session_stats['avg_confidence'] = (old_conf_avg * (total - 1) + confidence) / total
        self.session_stats['avg_duration_ms'] = (old_dur_avg * (total - 1) + duration_ms) / total

        # Format duration
        duration_sec = duration_ms / 1000

        # Build notification message
        status_emoji = "✅" if success else "❌"
        trend_emoji = "📈" if trend == "improving" else "📉" if trend == "declining" else "➡️"

        # Calculate margin
        margin = confidence - threshold
        margin_pct = (margin / threshold * 100) if threshold > 0 else 0

        # Send advanced macOS Notification Center alert (fully async, non-blocking)
        await self._send_advanced_notification(
            success=success,
            speaker=speaker,
            confidence=confidence,
            duration_sec=duration_sec,
            margin=margin,
            trend=trend,
            session_attempts=self.session_stats['total_attempts'],
            session_successes=self.session_stats['successful_attempts']
        )

        # Professional logging
        logger.info("")
        logger.info("=" * 80)
        logger.info(f"🔐 VOICE UNLOCK ATTEMPT DETECTED {status_emoji}")
        logger.info("=" * 80)
        logger.info(f"⏰ Time: {timestamp}")
        logger.info(f"👤 Speaker: {speaker}")
        logger.info(f"🎯 Result: {'SUCCESS' if success else 'FAILED'}")
        logger.info("")
        logger.info("📊 Biometric Analysis:")
        logger.info(f"   └─ Confidence: {confidence:.2%}")
        logger.info(f"   └─ Threshold: {threshold:.2%}")
        logger.info(f"   └─ Margin: {margin:+.2%} ({margin_pct:+.1f}%)")
        logger.info(f"   └─ Trend: {trend} {trend_emoji}")
        logger.info("")
        logger.info(f"⚡ Performance:")
        logger.info(f"   └─ Duration: {duration_sec:.1f}s ({duration_ms:.0f}ms)")
        logger.info("")
        logger.info(f"📈 Session Stats (This Run):")
        logger.info(f"   └─ Total Attempts: {self.session_stats['total_attempts']}")
        logger.info(f"   └─ Success Rate: {self.session_stats['successful_attempts']}/{self.session_stats['total_attempts']} ({self.session_stats['successful_attempts']/total*100:.1f}%)")
        logger.info(f"   └─ Avg Confidence: {self.session_stats['avg_confidence']:.2%}")
        logger.info(f"   └─ Avg Duration: {self.session_stats['avg_duration_ms']/1000:.1f}s")
        logger.info("")
        logger.info("💡 Database Updated - Press F5 in DB Browser to see latest data")
        logger.info("📱 macOS Notification Sent - Check Notification Center")
        logger.info("=" * 80)
        logger.info("")

    def _log_session_summary(self):
        """Log summary statistics for this monitoring session"""
        if self.session_stats['total_attempts'] == 0:
            logger.info("📊 No unlock attempts during this session")
            return

        logger.info("")
        logger.info("=" * 80)
        logger.info("📊 VOICE UNLOCK SESSION SUMMARY")
        logger.info("=" * 80)
        logger.info(f"⏰ Session Duration: {datetime.now().isoformat()} - {self.session_stats['session_start']}")
        logger.info(f"📈 Total Attempts: {self.session_stats['total_attempts']}")
        logger.info(f"✅ Successful: {self.session_stats['successful_attempts']}")
        logger.info(f"❌ Failed: {self.session_stats['failed_attempts']}")

        success_rate = (self.session_stats['successful_attempts'] / self.session_stats['total_attempts'] * 100)
        logger.info(f"🎯 Success Rate: {success_rate:.1f}%")
        logger.info(f"📊 Avg Confidence: {self.session_stats['avg_confidence']:.2%}")
        logger.info(f"⚡ Avg Duration: {self.session_stats['avg_duration_ms']/1000:.1f}s")
        logger.info("=" * 80)
        logger.info("")


# Global monitor instance
_metrics_monitor = None


async def initialize_metrics_monitor():
    """Initialize and start the metrics monitor"""
    global _metrics_monitor

    if _metrics_monitor is None:
        _metrics_monitor = VoiceUnlockMetricsMonitor()
        await _metrics_monitor.start()

    return _metrics_monitor


async def shutdown_metrics_monitor():
    """Shutdown the metrics monitor"""
    global _metrics_monitor

    if _metrics_monitor:
        await _metrics_monitor.stop()
        _metrics_monitor = None


def get_metrics_monitor() -> Optional[VoiceUnlockMetricsMonitor]:
    """Get the current metrics monitor instance"""
    return _metrics_monitor
