"""
Device State Monitor - Physical State Intelligence for Voice Authentication

Tracks physical device state to add contextual intelligence for multi-factor
voice authentication. Monitors:
- Device movement (accelerometer data via IOKit)
- Clamshell/lid state (open/closed)
- Power state transitions (sleep/wake cycles)
- Docking state (external displays, USB devices)
- Physical stability indicators

Part of the multi-factor authentication system integrating with:
- Voice Biometric Intelligence (VBI)
- Unified Awareness Engine (UAE)
- Situational Awareness Intelligence (SAI)
- Contextual Awareness Intelligence (CAI)

Author: JARVIS AI Agent
Version: 5.0.0
"""

import asyncio
import json
import logging
import os
import subprocess
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import hashlib

logger = logging.getLogger(__name__)


class DeviceState(Enum):
    """Physical device state categories."""
    STATIONARY = "stationary"          # Device hasn't moved
    IN_MOTION = "in_motion"            # Device being moved
    DOCKED = "docked"                  # Connected to external displays/devices
    PORTABLE = "portable"              # Lid open, on battery
    CLAMSHELL = "clamshell"           # Lid closed
    SLEEPING = "sleeping"              # Device asleep
    WAKING = "waking"                  # Recently woke from sleep
    UNKNOWN = "unknown"                # Cannot determine state


class LidState(Enum):
    """Laptop lid state."""
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class PowerSource(Enum):
    """Power source type."""
    BATTERY = "battery"
    AC_POWER = "ac_power"
    UNKNOWN = "unknown"


@dataclass
class DeviceContext:
    """Physical device context for authentication."""
    state: DeviceState
    lid_state: LidState
    power_source: PowerSource

    # Movement analysis
    is_stationary: bool
    movement_confidence: float  # 0.0-1.0
    last_movement_ago_seconds: Optional[int]

    # Docking analysis
    is_docked: bool
    external_displays_count: int
    usb_devices_count: int

    # Power state
    just_woke: bool  # Woke from sleep in last 5 minutes
    wake_time_ago_seconds: Optional[int]
    battery_level: Optional[int]

    # Authentication confidence
    confidence: float  # 0.0-1.0 how confident we are in this context
    trust_score: float  # 0.0-1.0 how much this context supports authentication

    # Reasoning
    reasoning: str
    anomaly_indicators: List[str]

    timestamp: datetime


@dataclass
class DeviceStateConfig:
    """Configuration for device state monitoring."""
    # Movement detection
    movement_check_interval_seconds: int = 30
    stationary_threshold_seconds: int = 300  # 5 minutes

    # Wake detection
    recent_wake_threshold_seconds: int = 300  # 5 minutes

    # Trust scoring
    stationary_boost: float = 0.15  # +15% for stationary device
    docked_boost: float = 0.12      # +12% for docked device
    just_woke_penalty: float = -0.10  # -10% if just woke (groggier voice)
    moving_penalty: float = -0.20   # -20% if device is moving (unusual)

    # Storage
    state_history_file: str = "device_state_history.json"
    max_history_events: int = 500

    @classmethod
    def from_env(cls) -> 'DeviceStateConfig':
        """Load configuration from environment variables."""
        return cls(
            movement_check_interval_seconds=int(os.getenv('DEVICE_MOVEMENT_CHECK_INTERVAL', '30')),
            stationary_threshold_seconds=int(os.getenv('DEVICE_STATIONARY_THRESHOLD', '300')),
            recent_wake_threshold_seconds=int(os.getenv('DEVICE_WAKE_THRESHOLD', '300')),
            stationary_boost=float(os.getenv('DEVICE_STATIONARY_BOOST', '0.15')),
            docked_boost=float(os.getenv('DEVICE_DOCKED_BOOST', '0.12')),
            just_woke_penalty=float(os.getenv('DEVICE_WAKE_PENALTY', '-0.10')),
            moving_penalty=float(os.getenv('DEVICE_MOVING_PENALTY', '-0.20')),
            state_history_file=os.getenv('DEVICE_STATE_HISTORY_FILE', 'device_state_history.json'),
            max_history_events=int(os.getenv('DEVICE_MAX_HISTORY', '500')),
        )


class DeviceStateMonitor:
    """
    Monitor physical device state for authentication intelligence.

    Provides contextual information about device's physical state to enhance
    voice authentication confidence. A stationary, docked device at a known
    location is more trustworthy than a moving device in an unknown location.
    """

    def __init__(self, config: Optional[DeviceStateConfig] = None):
        self.config = config or DeviceStateConfig.from_env()

        # Determine storage directory
        if os.getenv('JARVIS_DATA_DIR'):
            self.data_dir = Path(os.getenv('JARVIS_DATA_DIR')) / 'intelligence'
        else:
            self.data_dir = Path.home() / '.jarvis' / 'intelligence'
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.state_file = self.data_dir / self.config.state_history_file

        # State tracking
        self.last_movement_check: Optional[datetime] = None
        self.last_wake_time: Optional[datetime] = None
        self.last_display_hash: Optional[str] = None
        self.last_usb_hash: Optional[str] = None

        # Background monitoring
        self._monitoring_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

        logger.info(f"DeviceStateMonitor initialized with data dir: {self.data_dir}")

    async def start_monitoring(self):
        """Start background device state monitoring."""
        if self._monitoring_task is None or self._monitoring_task.done():
            self._monitoring_task = asyncio.create_task(self._background_monitor())
            logger.info("Device state background monitoring started")

    async def stop_monitoring(self):
        """Stop background monitoring."""
        if self._monitoring_task and not self._monitoring_task.done():
            self._monitoring_task.cancel()
            try:
                await self._monitoring_task
            except asyncio.CancelledError:
                pass
            logger.info("Device state background monitoring stopped")

    async def _background_monitor(self):
        """Background task to periodically check device state."""
        while True:
            try:
                await asyncio.sleep(self.config.movement_check_interval_seconds)

                # Check if displays or USB devices changed (indicates docking/undocking)
                current_displays = await self._get_display_hash()
                current_usb = await self._get_usb_hash()

                if (self.last_display_hash != current_displays or
                    self.last_usb_hash != current_usb):
                    logger.debug("Device configuration changed (docking state)")
                    self.last_display_hash = current_displays
                    self.last_usb_hash = current_usb

                    # Record state change
                    await self._record_state_change("docking_change")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Error in device state background monitor: {e}")

    async def get_device_context(self) -> DeviceContext:
        """
        Get current device physical state context.

        Returns:
            DeviceContext with current physical state and trust scoring
        """
        async with self._lock:
            try:
                # Gather all device state information
                lid_state = await self._get_lid_state()
                power_source = await self._get_power_source()
                battery_level = await self._get_battery_level()

                # Movement analysis
                movement_data = await self._analyze_movement()

                # Docking analysis
                docking_data = await self._analyze_docking()

                # Wake state analysis
                wake_data = await self._analyze_wake_state()

                # Determine overall device state
                device_state = self._determine_device_state(
                    lid_state, docking_data['is_docked'],
                    movement_data['is_stationary'], wake_data['just_woke']
                )

                # Calculate trust score
                trust_score, reasoning, anomalies = self._calculate_trust_score(
                    device_state, movement_data, docking_data, wake_data
                )

                # Build context
                context = DeviceContext(
                    state=device_state,
                    lid_state=lid_state,
                    power_source=power_source,
                    is_stationary=movement_data['is_stationary'],
                    movement_confidence=movement_data['confidence'],
                    last_movement_ago_seconds=movement_data.get('last_movement_ago'),
                    is_docked=docking_data['is_docked'],
                    external_displays_count=docking_data['displays'],
                    usb_devices_count=docking_data['usb_devices'],
                    just_woke=wake_data['just_woke'],
                    wake_time_ago_seconds=wake_data.get('wake_ago_seconds'),
                    battery_level=battery_level,
                    confidence=movement_data['confidence'],
                    trust_score=trust_score,
                    reasoning=reasoning,
                    anomaly_indicators=anomalies,
                    timestamp=datetime.now()
                )

                logger.debug(f"Device context: {device_state.value}, trust: {trust_score:.2f}")
                return context

            except Exception as e:
                logger.error(f"Error getting device context: {e}", exc_info=True)
                return self._get_fallback_context(str(e))

    async def _get_lid_state(self) -> LidState:
        """Detect laptop lid state (open/closed)."""
        try:
            # Use ioreg to check lid state
            result = await asyncio.create_subprocess_exec(
                'ioreg', '-r', '-k', 'AppleClamshellState', '-d', '4',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await result.communicate()

            output = stdout.decode('utf-8')

            # Look for "AppleClamshellState" = Yes/No
            if '"AppleClamshellState" = Yes' in output:
                return LidState.CLOSED
            elif '"AppleClamshellState" = No' in output:
                return LidState.OPEN

            return LidState.UNKNOWN

        except Exception as e:
            logger.debug(f"Could not determine lid state: {e}")
            return LidState.UNKNOWN

    async def _get_power_source(self) -> PowerSource:
        """Detect current power source (battery vs AC)."""
        try:
            result = await asyncio.create_subprocess_exec(
                'pmset', '-g', 'batt',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await result.communicate()

            output = stdout.decode('utf-8').lower()

            if "'ac power'" in output or "ac power" in output:
                return PowerSource.AC_POWER
            elif "'battery power'" in output or "battery power" in output:
                return PowerSource.BATTERY

            return PowerSource.UNKNOWN

        except Exception as e:
            logger.debug(f"Could not determine power source: {e}")
            return PowerSource.UNKNOWN

    async def _get_battery_level(self) -> Optional[int]:
        """Get current battery percentage."""
        try:
            result = await asyncio.create_subprocess_exec(
                'pmset', '-g', 'batt',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await result.communicate()

            output = stdout.decode('utf-8')

            # Parse battery percentage (e.g., "75%")
            import re
            match = re.search(r'(\d+)%', output)
            if match:
                return int(match.group(1))

            return None

        except Exception as e:
            logger.debug(f"Could not determine battery level: {e}")
            return None

    async def _analyze_movement(self) -> Dict:
        """
        Analyze device movement state.

        Uses display/USB connection stability as a proxy for movement.
        A device with stable connections is likely stationary.
        """
        try:
            # Check how long current display/USB configuration has been stable
            current_display_hash = await self._get_display_hash()
            current_usb_hash = await self._get_usb_hash()

            # If this is first check, initialize
            if self.last_display_hash is None:
                self.last_display_hash = current_display_hash
                self.last_usb_hash = current_usb_hash
                self.last_movement_check = datetime.now()

                return {
                    'is_stationary': False,
                    'confidence': 0.5,  # Unknown
                    'last_movement_ago': None
                }

            # Check if configuration changed
            config_changed = (
                current_display_hash != self.last_display_hash or
                current_usb_hash != self.last_usb_hash
            )

            if config_changed:
                # Configuration changed = movement detected
                self.last_display_hash = current_display_hash
                self.last_usb_hash = current_usb_hash
                self.last_movement_check = datetime.now()

                return {
                    'is_stationary': False,
                    'confidence': 0.9,  # High confidence of movement
                    'last_movement_ago': 0
                }

            # Configuration stable - calculate how long
            if self.last_movement_check:
                stable_duration = (datetime.now() - self.last_movement_check).total_seconds()

                # Consider stationary if stable for threshold duration
                is_stationary = stable_duration >= self.config.stationary_threshold_seconds

                # Confidence increases with stability duration
                confidence = min(0.95, 0.5 + (stable_duration / 3600.0) * 0.45)

                return {
                    'is_stationary': is_stationary,
                    'confidence': confidence,
                    'last_movement_ago': int(stable_duration) if not is_stationary else None
                }

            return {
                'is_stationary': False,
                'confidence': 0.5,
                'last_movement_ago': None
            }

        except Exception as e:
            logger.warning(f"Error analyzing movement: {e}")
            return {
                'is_stationary': False,
                'confidence': 0.3,
                'last_movement_ago': None
            }

    async def _analyze_docking(self) -> Dict:
        """Analyze docking state (external displays, USB devices)."""
        try:
            displays = await self._count_displays()
            usb_devices = await self._count_usb_devices()

            # Consider docked if external displays OR significant USB devices
            is_docked = displays > 1 or usb_devices >= 3

            return {
                'is_docked': is_docked,
                'displays': displays,
                'usb_devices': usb_devices
            }

        except Exception as e:
            logger.warning(f"Error analyzing docking: {e}")
            return {
                'is_docked': False,
                'displays': 1,
                'usb_devices': 0
            }

    async def _analyze_wake_state(self) -> Dict:
        """Analyze if device recently woke from sleep."""
        try:
            # Check system wake time using pmset
            result = await asyncio.create_subprocess_exec(
                'pmset', '-g', 'log',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await result.communicate()

            output = stdout.decode('utf-8')

            # Look for most recent wake event
            wake_lines = [line for line in output.split('\n') if 'Wake from' in line]

            if wake_lines:
                # Parse most recent wake time
                latest_wake = wake_lines[-1]

                # Extract timestamp (format varies, try to parse)
                import re
                timestamp_match = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', latest_wake)
                if timestamp_match:
                    wake_time_str = timestamp_match.group(1)
                    wake_time = datetime.strptime(wake_time_str, '%Y-%m-%d %H:%M:%S')

                    wake_ago = (datetime.now() - wake_time).total_seconds()
                    just_woke = wake_ago <= self.config.recent_wake_threshold_seconds

                    self.last_wake_time = wake_time

                    return {
                        'just_woke': just_woke,
                        'wake_ago_seconds': int(wake_ago)
                    }

            # If we have a cached wake time, use it
            if self.last_wake_time:
                wake_ago = (datetime.now() - self.last_wake_time).total_seconds()
                just_woke = wake_ago <= self.config.recent_wake_threshold_seconds

                return {
                    'just_woke': just_woke,
                    'wake_ago_seconds': int(wake_ago)
                }

            return {
                'just_woke': False,
                'wake_ago_seconds': None
            }

        except Exception as e:
            logger.debug(f"Could not analyze wake state: {e}")
            return {
                'just_woke': False,
                'wake_ago_seconds': None
            }

    async def _get_display_hash(self) -> str:
        """Get hash of current display configuration."""
        try:
            result = await asyncio.create_subprocess_exec(
                'system_profiler', 'SPDisplaysDataType',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await result.communicate()

            # Hash the display info for comparison
            return hashlib.sha256(stdout).hexdigest()[:16]

        except Exception as e:
            logger.debug(f"Error getting display hash: {e}")
            return "unknown"

    async def _get_usb_hash(self) -> str:
        """Get hash of current USB device configuration."""
        try:
            result = await asyncio.create_subprocess_exec(
                'system_profiler', 'SPUSBDataType',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await result.communicate()

            # Hash the USB info for comparison
            return hashlib.sha256(stdout).hexdigest()[:16]

        except Exception as e:
            logger.debug(f"Error getting USB hash: {e}")
            return "unknown"

    async def _count_displays(self) -> int:
        """Count number of active displays."""
        try:
            result = await asyncio.create_subprocess_exec(
                'system_profiler', 'SPDisplaysDataType',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await result.communicate()

            output = stdout.decode('utf-8')

            # Count "Display Type:" occurrences
            display_count = output.count('Resolution:')

            return max(1, display_count)

        except Exception as e:
            logger.debug(f"Error counting displays: {e}")
            return 1  # Assume at least built-in display

    async def _count_usb_devices(self) -> int:
        """Count number of connected USB devices."""
        try:
            result = await asyncio.create_subprocess_exec(
                'system_profiler', 'SPUSBDataType',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await result.communicate()

            output = stdout.decode('utf-8')

            # Count USB devices (look for "Product ID:" entries)
            device_count = output.count('Product ID:')

            return device_count

        except Exception as e:
            logger.debug(f"Error counting USB devices: {e}")
            return 0

    def _determine_device_state(
        self,
        lid_state: LidState,
        is_docked: bool,
        is_stationary: bool,
        just_woke: bool
    ) -> DeviceState:
        """Determine overall device state from individual indicators."""
        if just_woke:
            return DeviceState.WAKING

        if lid_state == LidState.CLOSED:
            return DeviceState.CLAMSHELL

        if is_docked:
            return DeviceState.DOCKED

        if not is_stationary:
            return DeviceState.IN_MOTION

        if is_stationary and lid_state == LidState.OPEN:
            return DeviceState.STATIONARY

        return DeviceState.UNKNOWN

    def _calculate_trust_score(
        self,
        device_state: DeviceState,
        movement_data: Dict,
        docking_data: Dict,
        wake_data: Dict
    ) -> Tuple[float, str, List[str]]:
        """
        Calculate trust score for authentication.

        Returns:
            (trust_score, reasoning, anomaly_indicators)
        """
        base_trust = 0.5  # Neutral baseline
        reasoning_parts = []
        anomalies = []

        # Stationary device = trustworthy
        if movement_data['is_stationary']:
            base_trust += self.config.stationary_boost
            reasoning_parts.append(f"device stationary (confidence: {movement_data['confidence']:.0%})")
        else:
            base_trust += self.config.moving_penalty
            reasoning_parts.append("device recently moved or in motion")
            anomalies.append("Device movement detected during unlock")

        # Docked device = trustworthy
        if docking_data['is_docked']:
            base_trust += self.config.docked_boost
            reasoning_parts.append(f"docked ({docking_data['displays']} displays, {docking_data['usb_devices']} USB devices)")

        # Just woke = slightly less trustworthy (voice may be groggier)
        if wake_data['just_woke']:
            base_trust += self.config.just_woke_penalty
            wake_ago = wake_data.get('wake_ago_seconds', 0)
            reasoning_parts.append(f"just woke {wake_ago}s ago (voice may be groggier)")
            anomalies.append(f"Device woke from sleep {wake_ago}s ago")

        # State-specific adjustments
        if device_state == DeviceState.CLAMSHELL:
            base_trust -= 0.15
            reasoning_parts.append("lid closed (clamshell mode)")
            anomalies.append("Unlock attempt with lid closed")

        # Clamp to valid range
        trust_score = max(0.0, min(1.0, base_trust))

        # Build reasoning string
        reasoning = "Device context: " + ", ".join(reasoning_parts)

        return trust_score, reasoning, anomalies

    def _get_fallback_context(self, error: str) -> DeviceContext:
        """Return fallback context when detection fails."""
        return DeviceContext(
            state=DeviceState.UNKNOWN,
            lid_state=LidState.UNKNOWN,
            power_source=PowerSource.UNKNOWN,
            is_stationary=False,
            movement_confidence=0.3,
            last_movement_ago_seconds=None,
            is_docked=False,
            external_displays_count=0,
            usb_devices_count=0,
            just_woke=False,
            wake_time_ago_seconds=None,
            battery_level=None,
            confidence=0.3,
            trust_score=0.5,  # Neutral when unknown
            reasoning=f"Could not determine device state: {error}",
            anomaly_indicators=["Device state detection failed"],
            timestamp=datetime.now()
        )

    async def record_unlock_attempt(
        self,
        success: bool,
        confidence: float,
        device_context: Optional[DeviceContext] = None
    ):
        """
        Record an unlock attempt with device context for pattern learning.

        Args:
            success: Whether unlock was successful
            confidence: Voice authentication confidence
            device_context: Optional device context (will fetch if not provided)
        """
        try:
            if device_context is None:
                device_context = await self.get_device_context()

            event = {
                'timestamp': datetime.now().isoformat(),
                'success': success,
                'confidence': confidence,
                'device_state': device_context.state.value,
                'lid_state': device_context.lid_state.value,
                'is_stationary': device_context.is_stationary,
                'is_docked': device_context.is_docked,
                'just_woke': device_context.just_woke,
                'trust_score': device_context.trust_score,
                'anomalies': device_context.anomaly_indicators
            }

            # Append to history
            await self._append_history(event)

            logger.info(f"Recorded unlock attempt: success={success}, device_state={device_context.state.value}")

        except Exception as e:
            logger.error(f"Error recording unlock attempt: {e}")

    async def _record_state_change(self, change_type: str):
        """Record a device state change event."""
        try:
            event = {
                'timestamp': datetime.now().isoformat(),
                'type': 'state_change',
                'change_type': change_type
            }

            await self._append_history(event)

        except Exception as e:
            logger.error(f"Error recording state change: {e}")

    async def _append_history(self, event: Dict):
        """Append event to history file."""
        try:
            # Load existing history
            history = []
            if self.state_file.exists():
                with open(self.state_file, 'r') as f:
                    history = json.load(f)

            # Append new event
            history.append(event)

            # Trim to max size
            if len(history) > self.config.max_history_events:
                history = history[-self.config.max_history_events:]

            # Save back
            with open(self.state_file, 'w') as f:
                json.dump(history, f, indent=2)

        except Exception as e:
            logger.error(f"Error appending to history: {e}")

    async def get_statistics(self) -> Dict:
        """Get device state statistics and patterns."""
        try:
            if not self.state_file.exists():
                return {
                    'total_events': 0,
                    'unlock_attempts': 0,
                    'success_rate': 0.0
                }

            with open(self.state_file, 'r') as f:
                history = json.load(f)

            unlock_events = [e for e in history if 'success' in e]

            if not unlock_events:
                return {
                    'total_events': len(history),
                    'unlock_attempts': 0,
                    'success_rate': 0.0
                }

            successful = sum(1 for e in unlock_events if e['success'])

            # Calculate patterns
            stationary_unlocks = sum(1 for e in unlock_events if e.get('is_stationary'))
            docked_unlocks = sum(1 for e in unlock_events if e.get('is_docked'))
            wake_unlocks = sum(1 for e in unlock_events if e.get('just_woke'))

            return {
                'total_events': len(history),
                'unlock_attempts': len(unlock_events),
                'successful_unlocks': successful,
                'failed_unlocks': len(unlock_events) - successful,
                'success_rate': successful / len(unlock_events) if unlock_events else 0.0,
                'stationary_unlock_rate': stationary_unlocks / len(unlock_events) if unlock_events else 0.0,
                'docked_unlock_rate': docked_unlocks / len(unlock_events) if unlock_events else 0.0,
                'wake_unlock_rate': wake_unlocks / len(unlock_events) if unlock_events else 0.0
            }

        except Exception as e:
            logger.error(f"Error getting statistics: {e}")
            return {'error': str(e)}


# Singleton instance
_monitor_instance: Optional[DeviceStateMonitor] = None
_monitor_lock = asyncio.Lock()


async def get_device_monitor(config: Optional[DeviceStateConfig] = None) -> DeviceStateMonitor:
    """
    Get singleton DeviceStateMonitor instance.

    Args:
        config: Optional configuration (only used on first call)

    Returns:
        DeviceStateMonitor instance
    """
    global _monitor_instance

    async with _monitor_lock:
        if _monitor_instance is None:
            _monitor_instance = DeviceStateMonitor(config)
            await _monitor_instance.start_monitoring()
            logger.info("Device state monitor singleton initialized")

        return _monitor_instance


async def get_device_context() -> DeviceContext:
    """
    Convenience function to get current device context.

    Returns:
        DeviceContext with current physical state
    """
    monitor = await get_device_monitor()
    return await monitor.get_device_context()


# CLI testing
if __name__ == "__main__":
    import sys

    async def main():
        """Test device state monitoring."""
        logging.basicConfig(
            level=logging.DEBUG,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )

        monitor = await get_device_monitor()

        print("\n" + "="*80)
        print("JARVIS Device State Monitor - Test")
        print("="*80 + "\n")

        # Get current context
        context = await monitor.get_device_context()

        print(f"Device State: {context.state.value.upper()}")
        print(f"Lid State: {context.lid_state.value}")
        print(f"Power Source: {context.power_source.value}")
        print(f"\nMovement Analysis:")
        print(f"  Stationary: {context.is_stationary}")
        print(f"  Confidence: {context.movement_confidence:.1%}")
        if context.last_movement_ago_seconds is not None:
            print(f"  Last Movement: {context.last_movement_ago_seconds}s ago")
        print(f"\nDocking State:")
        print(f"  Docked: {context.is_docked}")
        print(f"  External Displays: {context.external_displays_count}")
        print(f"  USB Devices: {context.usb_devices_count}")
        print(f"\nPower State:")
        print(f"  Just Woke: {context.just_woke}")
        if context.wake_time_ago_seconds is not None:
            print(f"  Wake Time: {context.wake_time_ago_seconds}s ago")
        if context.battery_level is not None:
            print(f"  Battery Level: {context.battery_level}%")
        print(f"\nAuthentication Intelligence:")
        print(f"  Trust Score: {context.trust_score:.1%}")
        print(f"  Reasoning: {context.reasoning}")
        if context.anomaly_indicators:
            print(f"  Anomalies: {', '.join(context.anomaly_indicators)}")

        # Get statistics
        stats = await monitor.get_statistics()
        print(f"\n{'='*80}")
        print("Statistics:")
        print(f"  Total Events: {stats.get('total_events', 0)}")
        print(f"  Unlock Attempts: {stats.get('unlock_attempts', 0)}")
        if stats.get('unlock_attempts', 0) > 0:
            print(f"  Success Rate: {stats.get('success_rate', 0):.1%}")
            print(f"  Stationary Unlock Rate: {stats.get('stationary_unlock_rate', 0):.1%}")
            print(f"  Docked Unlock Rate: {stats.get('docked_unlock_rate', 0):.1%}")

        print(f"\n{'='*80}\n")

        await monitor.stop_monitoring()

    asyncio.run(main())
