"""
macOS Location Service Integration
Gets actual device location using Core Location
"""

import subprocess
import json
import logging
import asyncio
from typing import Optional, Tuple, Dict
from datetime import datetime
import os

logger = logging.getLogger(__name__)

class MacOSLocationService:
    """Get actual device location using macOS location services"""
    
    def __init__(self):
        self._location_cache = None
        self._cache_time = None
        self._cache_duration = 300  # 5 minutes
        
    async def get_current_location(self) -> Optional[Dict]:
        """Get current location using macOS location services"""
        # Check cache
        if self._location_cache and self._cache_time:
            age = (datetime.now() - self._cache_time).seconds
            if age < self._cache_duration:
                return self._location_cache
        
        # Try multiple methods to get actual location
        location = None
        
        # Method 1: Use shortcuts to get location
        location = await self._get_location_via_shortcuts()
        
        # Method 2: Use CoreLocationCLI if available
        if not location:
            location = await self._get_location_via_corelocation()
        
        # Method 3: Use location from Wi-Fi
        if not location:
            location = await self._get_location_via_wifi()
        
        # Method 4: Use Apple's location services via script
        if not location:
            location = await self._get_location_via_apple_script()
        
        if location:
            self._location_cache = location
            self._cache_time = datetime.now()
            
        return location
    
    async def _get_location_via_shortcuts(self) -> Optional[Dict]:
        """Get location using macOS shortcuts"""
        try:
            # Create a shortcut command to get current location
            shortcut_script = '''
            tell application "Shortcuts"
                run shortcut "Get Current Location"
            end tell
            '''
            
            # Alternative: Use shortcuts CLI if available
            result = subprocess.run(
                ['shortcuts', 'run', 'Get Current Location', '--output-type', 'json'],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout)
                return {
                    "latitude": data.get("latitude"),
                    "longitude": data.get("longitude"),
                    "city": data.get("city", ""),
                    "state": data.get("state", ""),
                    "country": data.get("country", ""),
                    "method": "shortcuts"
                }
        except Exception:
            pass

        return None

    async def _get_location_via_corelocation(self) -> Optional[Dict]:
        """Get location using CoreLocationCLI"""
        try:
            # Check if CoreLocationCLI is installed
            if not os.path.exists('/usr/local/bin/CoreLocationCLI'):
                # Try to find it in other locations
                locations = [
                    '/usr/local/bin/CoreLocationCLI',
                    '/opt/homebrew/bin/CoreLocationCLI',
                    '/usr/bin/CoreLocationCLI'
                ]
                
                cli_path = None
                for path in locations:
                    if os.path.exists(path):
                        cli_path = path
                        break
                
                if not cli_path:
                    logger.debug("CoreLocationCLI not found")
                    return None
            else:
                cli_path = '/usr/local/bin/CoreLocationCLI'
            
            # Get location with timeout
            result = subprocess.run(
                [cli_path, '-json', '-once', '-timeout', '5'],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0 and result.stdout:
                data = json.loads(result.stdout)
                
                # Get address information
                address_result = subprocess.run(
                    [cli_path, '-format', '%address', '-once'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                
                address = ""
                if address_result.returncode == 0:
                    address = address_result.stdout.strip()
                
                # Parse address to get city
                city = ""
                state = ""
                if address:
                    parts = address.split(',')
                    if len(parts) >= 2:
                        city = parts[0].strip()
                        if len(parts) >= 3:
                            state = parts[1].strip()
                
                return {
                    "latitude": data.get("latitude"),
                    "longitude": data.get("longitude"),
                    "city": city,
                    "state": state,
                    "address": address,
                    "altitude": data.get("altitude"),
                    "accuracy": data.get("horizontalAccuracy"),
                    "method": "corelocation"
                }
                
        except Exception as e:
            logger.debug(f"CoreLocationCLI error: {e}")
        
        return None
    
    async def _get_location_via_wifi(self) -> Optional[Dict]:
        """Get approximate location from Wi-Fi network"""
        try:
            # Get current Wi-Fi SSID
            wifi_result = subprocess.run(
                ["/System/Library/PrivateFrameworks/Apple80211.framework/Resources/airport", "-I"],
                capture_output=True,
                text=True
            )
            
            if wifi_result.returncode == 0:
                # Parse Wi-Fi info
                ssid = None
                for line in wifi_result.stdout.split('\n'):
                    if 'SSID:' in line:
                        ssid = line.split('SSID:')[1].strip()
                        break
                
                if ssid:
                    logger.debug(f"Connected to Wi-Fi: {ssid}")
                    # Could use Wi-Fi SSID to infer location if it contains location info
                    # For now, continue to other methods
            
        except Exception:
            pass

        return None

    async def _get_location_via_apple_script(self) -> Optional[Dict]:
        """Get location using AppleScript and system services"""
        try:
            # AppleScript to get location via system
            script = '''
            use framework "CoreLocation"
            use framework "Foundation"
            
            -- Create location manager
            set locationManager to current application's CLLocationManager's alloc()'s init()
            
            -- Check authorization
            set authStatus to current application's CLLocationManager's authorizationStatus()
            if authStatus is not current application's kCLAuthorizationStatusAuthorizedAlways and authStatus is not current application's kCLAuthorizationStatusAuthorizedWhenInUse then
                return "Location access not authorized"
            end if
            
            -- Request location
            locationManager's setDesiredAccuracy:(current application's kCLLocationAccuracyBest)
            locationManager's startUpdatingLocation()
            
            -- Wait briefly for location
            delay 2
            
            -- Get location
            set currentLocation to locationManager's location()
            locationManager's stopUpdatingLocation()
            
            if currentLocation is not missing value then
                set lat to currentLocation's coordinate()'s latitude()
                set lon to currentLocation's coordinate()'s longitude()
                return (lat as string) & "," & (lon as string)
            else
                return "No location available"
            end if
            '''
            
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0 and "," in result.stdout:
                parts = result.stdout.strip().split(',')
                if len(parts) == 2:
                    return {
                        "latitude": float(parts[0]),
                        "longitude": float(parts[1]),
                        "method": "applescript"
                    }
                    
        except Exception as e:
            logger.debug(f"AppleScript location error: {e}")
        
        return None
    
    async def ensure_location_permissions(self) -> bool:
        """Check and prompt for location permissions if needed"""
        try:
            # Check location services status
            check_script = '''
            use framework "CoreLocation"
            
            set authStatus to current application's CLLocationManager's authorizationStatus()
            
            if authStatus is current application's kCLAuthorizationStatusNotDetermined then
                return "not determined"
            else if authStatus is current application's kCLAuthorizationStatusRestricted then
                return "restricted"
            else if authStatus is current application's kCLAuthorizationStatusDenied then
                return "denied"
            else if authStatus is current application's kCLAuthorizationStatusAuthorizedAlways then
                return "authorized always"
            else if authStatus is current application's kCLAuthorizationStatusAuthorizedWhenInUse then
                return "authorized when in use"
            else
                return "unknown"
            end if
            '''
            
            result = subprocess.run(
                ['osascript', '-e', check_script],
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                status = result.stdout.strip()
                logger.info(f"Location authorization status: {status}")
                
                if "authorized" in status:
                    return True
                elif status == "not determined":
                    # Prompt for permission
                    prompt_script = '''
                    use framework "CoreLocation"
                    
                    set locationManager to current application's CLLocationManager's alloc()'s init()
                    locationManager's requestWhenInUseAuthorization()
                    '''
                    
                    subprocess.run(['osascript', '-e', prompt_script])
                    await asyncio.sleep(2)
                    return True
                else:
                    logger.warning("Location access denied. Please enable in System Settings > Privacy & Security > Location Services")
                    return False
                    
        except Exception as e:
            logger.error(f"Error checking location permissions: {e}")
            
        return False