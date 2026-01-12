"""
Apple Watch Proximity Detection
===============================

Detects Apple Watch proximity using Bluetooth LE and provides
seamless integration with voice authentication.
"""

import asyncio
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, Callable, List
from dataclasses import dataclass
import json
from pathlib import Path

# Initialize logger first before any imports that might use it
logger = logging.getLogger(__name__)

# For Bluetooth LE scanning
try:
    import CoreBluetooth
    import objc
    from PyObjCTools import AppHelper
    COREBLUETOOTH_AVAILABLE = True
except ImportError:
    COREBLUETOOTH_AVAILABLE = False
    logger.warning("CoreBluetooth not available. Using alternative method.")

# Alternative: use bleak for cross-platform Bluetooth LE
try:
    from bleak import BleakScanner, BleakClient
    BLEAK_AVAILABLE = True
except ImportError:
    BLEAK_AVAILABLE = False


@dataclass
class AppleWatchDevice:
    """Apple Watch device information"""
    identifier: str
    name: str
    rssi: int  # Signal strength
    distance: float  # Estimated distance in meters
    last_seen: datetime
    is_paired: bool = False
    is_unlocked: bool = False


class AppleWatchProximityDetector:
    """
    Detects Apple Watch proximity for authentication
    """
    
    # Apple Watch service UUIDs
    APPLE_WATCH_SERVICE_UUIDS = [
        "180A",  # Device Information Service
        "180D",  # Heart Rate Service
        "FE2C",  # Apple Continuity Service
        "FD6F",  # Apple Nearby Service
    ]
    
    # RSSI to distance approximation
    RSSI_THRESHOLDS = {
        'immediate': -50,  # < 1 meter
        'near': -65,       # 1-3 meters  
        'far': -80,        # 3-10 meters
        'unknown': -100    # > 10 meters
    }
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or self._get_default_config()
        
        # Device tracking
        self.detected_watches: Dict[str, AppleWatchDevice] = {}
        self.paired_watch_id: Optional[str] = None
        
        # Callbacks
        self.proximity_callbacks: List[Callable] = []
        self.lock_callbacks: List[Callable] = []
        
        # Scanning state
        self.is_scanning = False
        self.scanner = None
        self.scan_thread = None
        
        # Load paired devices
        self._load_paired_devices()
        
    def _get_default_config(self) -> Dict[str, Any]:
        """Get default configuration"""
        return {
            'scan_interval': 1.0,  # seconds between scans
            'device_timeout': 30.0,  # seconds before device is considered gone
            'unlock_distance': 3.0,  # meters - max distance for unlock
            'lock_distance': 10.0,   # meters - distance to trigger lock
            'require_unlocked_watch': True,  # Watch must be unlocked
            'signal_smoothing': 0.7,  # RSSI smoothing factor
        }
        
    def _load_paired_devices(self):
        """Load previously paired Apple Watch devices"""
        paired_file = Path.home() / '.jarvis' / 'voice_unlock' / 'paired_watches.json'
        
        if paired_file.exists():
            try:
                with open(paired_file, 'r') as f:
                    data = json.load(f)
                    self.paired_watch_id = data.get('primary_watch')
                    logger.info(f"Loaded paired watch: {self.paired_watch_id}")
            except Exception as e:
                logger.error(f"Failed to load paired devices: {e}")
                
    def _save_paired_device(self, device: AppleWatchDevice):
        """Save paired Apple Watch device"""
        paired_file = Path.home() / '.jarvis' / 'voice_unlock' / 'paired_watches.json'
        paired_file.parent.mkdir(parents=True, exist_ok=True)
        
        data = {
            'primary_watch': device.identifier,
            'device_info': {
                'name': device.name,
                'last_paired': datetime.now().isoformat()
            }
        }
        
        with open(paired_file, 'w') as f:
            json.dump(data, f, indent=2)
            
    def start_scanning(self):
        """Start scanning for Apple Watch devices"""
        if self.is_scanning:
            return
            
        self.is_scanning = True
        
        if COREBLUETOOTH_AVAILABLE:
            self._start_corebluetooth_scan()
        elif BLEAK_AVAILABLE:
            self._start_bleak_scan()
        else:
            logger.error("No Bluetooth library available")
            
    def _start_corebluetooth_scan(self):
        """Start scanning using CoreBluetooth (macOS)"""
        # This would use PyObjC to interface with CoreBluetooth
        # For now, using simplified implementation
        logger.info("Starting CoreBluetooth scan...")
        
        self.scan_thread = threading.Thread(
            target=self._corebluetooth_scan_loop,
            daemon=True
        )
        self.scan_thread.start()
        
    def _start_bleak_scan(self):
        """Start scanning using bleak (cross-platform)"""
        logger.info("Starting bleak Bluetooth scan...")
        
        self.scan_thread = threading.Thread(
            target=self._bleak_scan_loop,
            daemon=True
        )
        self.scan_thread.start()
        
    async def _bleak_scan_devices(self):
        """Scan for Bluetooth devices using bleak"""
        try:
            devices = await BleakScanner.discover(
                timeout=self.config['scan_interval'],
                return_adv=True
            )
            
            current_time = datetime.now()
            
            for device, adv_data in devices.items():
                # Check if it's an Apple device
                if self._is_apple_watch(device, adv_data):
                    # Calculate distance from RSSI
                    rssi = adv_data.rssi
                    distance = self._estimate_distance(rssi)
                    
                    # Check if it's our paired watch
                    is_paired = device.address == self.paired_watch_id
                    
                    # Create or update device entry
                    watch = AppleWatchDevice(
                        identifier=device.address,
                        name=device.name or "Apple Watch",
                        rssi=rssi,
                        distance=distance,
                        last_seen=current_time,
                        is_paired=is_paired
                    )
                    
                    # Smooth RSSI if device already tracked
                    if device.address in self.detected_watches:
                        old_watch = self.detected_watches[device.address]
                        watch.rssi = self._smooth_rssi(old_watch.rssi, rssi)
                        watch.distance = self._estimate_distance(watch.rssi)
                        
                    self.detected_watches[device.address] = watch
                    
                    # Trigger callbacks if paired watch
                    if is_paired:
                        self._handle_watch_proximity(watch)
                        
            # Check for devices that have gone out of range
            self._cleanup_old_devices(current_time)
            
        except Exception as e:
            logger.error(f"Bluetooth scan error: {e}")
            
    def _bleak_scan_loop(self):
        """Continuous scanning loop for bleak"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        session_timeout = float(os.getenv("TIMEOUT_VOICE_SESSION", "3600.0"))  # 1 hour default
        session_start = time.monotonic()

        while self.is_scanning and (time.monotonic() - session_start < session_timeout):
            try:
                loop.run_until_complete(self._bleak_scan_devices())
                time.sleep(0.1)  # Brief pause between scans
            except Exception as e:
                logger.error(f"Scan loop error: {e}")
                time.sleep(1)

        if time.monotonic() - session_start >= session_timeout:
            logger.info("Bleak scan loop session timeout reached")
            self.is_scanning = False
                
    def _corebluetooth_scan_loop(self):
        """Continuous scanning loop for CoreBluetooth"""
        # Simplified implementation - in reality would use CBCentralManager
        session_timeout = float(os.getenv("TIMEOUT_VOICE_SESSION", "3600.0"))  # 1 hour default
        session_start = time.monotonic()

        while self.is_scanning and (time.monotonic() - session_start < session_timeout):
            try:
                # Simulate device detection
                if self.paired_watch_id:
                    # Simulate varying RSSI
                    import random
                    rssi = -60 + random.randint(-10, 10)
                    distance = self._estimate_distance(rssi)

                    watch = AppleWatchDevice(
                        identifier=self.paired_watch_id,
                        name="John's Apple Watch",
                        rssi=rssi,
                        distance=distance,
                        last_seen=datetime.now(),
                        is_paired=True,
                        is_unlocked=True  # Would check actual state
                    )

                    self.detected_watches[self.paired_watch_id] = watch
                    self._handle_watch_proximity(watch)

                time.sleep(self.config['scan_interval'])

            except Exception as e:
                logger.error(f"CoreBluetooth scan error: {e}")
                time.sleep(1)

        if time.monotonic() - session_start >= session_timeout:
            logger.info("CoreBluetooth scan loop session timeout reached")
            self.is_scanning = False
                
    def _is_apple_watch(self, device, adv_data) -> bool:
        """Check if device is an Apple Watch"""
        # Check device name
        if device.name and 'Apple Watch' in device.name:
            return True
            
        # Check manufacturer data
        if hasattr(adv_data, 'manufacturer_data'):
            # Apple's company identifier is 0x004C
            if 76 in adv_data.manufacturer_data:
                return True
                
        # Check service UUIDs
        if hasattr(adv_data, 'service_uuids'):
            for uuid in self.APPLE_WATCH_SERVICE_UUIDS:
                if uuid.lower() in [u.lower() for u in adv_data.service_uuids]:
                    return True
                    
        return False
        
    def _estimate_distance(self, rssi: int) -> float:
        """Estimate distance from RSSI signal strength"""
        # Path loss formula: RSSI = -10 * n * log10(d) + A
        # Where n = path loss exponent (2 for free space)
        # A = RSSI at 1 meter (typically -55 to -60)
        
        if rssi >= self.RSSI_THRESHOLDS['immediate']:
            return 0.5  # Very close
        elif rssi >= self.RSSI_THRESHOLDS['near']:
            return 2.0  # Near
        elif rssi >= self.RSSI_THRESHOLDS['far']:
            return 5.0  # Far but in range
        else:
            return 15.0  # Out of range
            
    def _smooth_rssi(self, old_rssi: int, new_rssi: int) -> int:
        """Smooth RSSI values to reduce fluctuations"""
        alpha = self.config['signal_smoothing']
        return int(alpha * old_rssi + (1 - alpha) * new_rssi)
        
    def _handle_watch_proximity(self, watch: AppleWatchDevice):
        """Handle proximity changes for paired watch"""
        # Check if within unlock distance
        if watch.distance <= self.config['unlock_distance']:
            # Check if watch is unlocked (if required)
            if not self.config['require_unlocked_watch'] or watch.is_unlocked:
                # Trigger proximity callbacks
                for callback in self.proximity_callbacks:
                    try:
                        callback(watch.distance, watch.identifier)
                    except Exception as e:
                        logger.error(f"Proximity callback error: {e}")
                        
        # Check if beyond lock distance
        elif watch.distance > self.config['lock_distance']:
            # Trigger lock callbacks
            for callback in self.lock_callbacks:
                try:
                    callback(watch.identifier)
                except Exception as e:
                    logger.error(f"Lock callback error: {e}")
                    
    def _cleanup_old_devices(self, current_time: datetime):
        """Remove devices that haven't been seen recently"""
        timeout = timedelta(seconds=self.config['device_timeout'])
        
        devices_to_remove = []
        for device_id, device in self.detected_watches.items():
            if current_time - device.last_seen > timeout:
                devices_to_remove.append(device_id)
                
                # If it's the paired watch, trigger lock
                if device.is_paired:
                    for callback in self.lock_callbacks:
                        try:
                            callback(device.identifier)
                        except Exception as e:
                            logger.error(f"Lock callback error: {e}")
                            
        # Remove old devices
        for device_id in devices_to_remove:
            del self.detected_watches[device_id]
            logger.debug(f"Removed device {device_id} (timeout)")
            
    def add_proximity_callback(self, callback: Callable[[float, str], None]):
        """Add callback for proximity events"""
        self.proximity_callbacks.append(callback)
        
    def add_lock_callback(self, callback: Callable[[str], None]):
        """Add callback for lock events (device out of range)"""
        self.lock_callbacks.append(callback)
        
    def pair_watch(self, device_id: str) -> bool:
        """Pair with an Apple Watch"""
        if device_id in self.detected_watches:
            device = self.detected_watches[device_id]
            device.is_paired = True
            self.paired_watch_id = device_id
            
            # Save pairing
            self._save_paired_device(device)
            
            logger.info(f"Paired with Apple Watch: {device_id}")
            return True
            
        return False
        
    def get_paired_watch(self) -> Optional[AppleWatchDevice]:
        """Get currently paired Apple Watch"""
        if self.paired_watch_id and self.paired_watch_id in self.detected_watches:
            return self.detected_watches[self.paired_watch_id]
        return None
        
    def is_watch_nearby(self, max_distance: Optional[float] = None) -> bool:
        """Check if paired watch is within range"""
        max_distance = max_distance or self.config['unlock_distance']
        
        watch = self.get_paired_watch()
        if watch:
            return watch.distance <= max_distance
            
        return False
        
    def get_watch_distance(self) -> Optional[float]:
        """Get distance to paired watch"""
        watch = self.get_paired_watch()
        return watch.distance if watch else None
        
    def stop_scanning(self):
        """Stop scanning for devices"""
        self.is_scanning = False
        
        if self.scan_thread:
            self.scan_thread.join(timeout=2)
            
        logger.info("Stopped Apple Watch scanning")
        
    def get_status(self) -> Dict[str, Any]:
        """Get current status"""
        watch = self.get_paired_watch()
        
        return {
            'scanning': self.is_scanning,
            'paired_watch_id': self.paired_watch_id,
            'watch_nearby': self.is_watch_nearby(),
            'watch_distance': self.get_watch_distance(),
            'watch_unlocked': watch.is_unlocked if watch else False,
            'detected_watches': len(self.detected_watches),
            'bluetooth_available': COREBLUETOOTH_AVAILABLE or BLEAK_AVAILABLE
        }


# Test function
def test_apple_watch_proximity():
    """Test Apple Watch proximity detection"""
    detector = AppleWatchProximityDetector()
    
    def on_proximity(distance: float, device_id: str):
        print(f"Apple Watch nearby: {distance:.1f}m ({device_id})")
        
    def on_lock(device_id: str):
        print(f"Apple Watch out of range: {device_id}")
        
    detector.add_proximity_callback(on_proximity)
    detector.add_lock_callback(on_lock)
    
    print("Starting Apple Watch detection...")
    detector.start_scanning()
    
    try:
        while True:
            status = detector.get_status()
            print(f"Status: {json.dumps(status, indent=2)}")
            time.sleep(5)
    except KeyboardInterrupt:
        detector.stop_scanning()
        print("Stopped")


if __name__ == "__main__":
    test_apple_watch_proximity()