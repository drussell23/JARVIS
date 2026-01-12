"""
Swift Weather Provider for JARVIS
Uses native macOS WeatherKit via Swift CLI tool
"""

import json
import asyncio
import subprocess
import logging
import os
from pathlib import Path
from typing import Dict, Optional, List, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)


class SwiftWeatherProvider:
    """Weather provider using native Swift CLI tool for WeatherKit access"""
    
    def __init__(self):
        # Find the Swift tool or fallback
        self.native_dir = Path(__file__).parent / "native"
        # Prioritize CoreLocation-enabled tool
        self.corelocation_path = self.native_dir / "jarvis-weather-corelocation"
        self.tool_path = self.native_dir / "jarvis-weather"
        self.fallback_path = self.native_dir / "jarvis-weather-fallback.py"
        self.tool_available = False
        self.use_fallback = False
        
        # Skip CoreLocation tool for now - it needs app bundle to work properly
        # Check for working jarvis-weather tool instead
        
        # Check if Swift tool exists and is executable
        if self.tool_path.exists():
            if not self.tool_path.is_file():
                logger.error(f"jarvis-weather exists but is not a file: {self.tool_path}")
            elif not os.access(self.tool_path, os.X_OK):
                logger.warning(f"jarvis-weather is not executable, attempting to fix...")
                try:
                    self.tool_path.chmod(0o755)
                    self.tool_available = True
                except Exception as e:
                    logger.error(f"Failed to make jarvis-weather executable: {e}")
            else:
                self.tool_available = True
                logger.info(f"Swift weather tool available at: {self.tool_path}")
        
        # Always check for fallback as backup
        if self.fallback_path.exists():
            if not self.tool_available:
                self.use_fallback = True
                self.tool_path = self.fallback_path
                self.tool_available = True
                logger.info(f"Using Python fallback weather tool at: {self.fallback_path}")
            else:
                logger.info(f"Python fallback available at: {self.fallback_path}")
            logger.info("Run './build.sh' in the native directory to build the Swift tool")
    
    async def get_weather_data(self, location: Optional[str] = None) -> Optional[Dict]:
        """
        Get weather data using Swift tool
        
        Args:
            location: Optional city name. If None, uses current location
            
        Returns:
            Weather data dict or None if failed
        """
        if not self.tool_available:
            logger.error("Swift weather tool not available")
            return None
        
        try:
            # Build command
            if location:
                # City weather
                cmd = [str(self.tool_path), "city", location]
            else:
                # Current location weather
                cmd = [str(self.tool_path), "current"]
            
            # Run the Swift tool
            logger.debug(f"Running command: {' '.join(cmd)}")
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            # Wait for completion with timeout
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), 
                    timeout=10.0  # 10 seconds timeout
                )
            except asyncio.TimeoutError:
                logger.error("Swift weather tool timed out")
                try:
                    process.terminate()
                    await process.wait()
                except Exception:
                    pass
                return None
            
            if process.returncode != 0:
                error_msg = stderr.decode('utf-8', errors='ignore')
                logger.error(f"Swift weather tool failed: {error_msg}")
                
                # Try to parse error response
                try:
                    error_data = json.loads(stdout.decode('utf-8'))
                    logger.error(f"Error details: {error_data.get('message', 'Unknown error')}")
                except Exception:
                    pass

                return None
            
            # Parse JSON response
            weather_data = json.loads(stdout.decode('utf-8'))
            
            # Enhance data with additional fields for compatibility
            weather_data = self._enhance_weather_data(weather_data)
            
            logger.info(f"Got weather from Swift tool: {weather_data.get('location')} - "
                       f"{weather_data.get('temperature')}°C, {weather_data.get('condition')}")
            
            return weather_data
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Swift tool output: {e}")
            # Try fallback if available
            if self.fallback_path.exists() and not self.use_fallback:
                logger.info("Falling back to Python weather tool")
                self.use_fallback = True
                self.tool_path = self.fallback_path
                return await self.get_weather_data(location)
            return None
        except Exception as e:
            logger.error(f"Error running Swift weather tool: {e}")
            # Try fallback if available
            if self.fallback_path.exists() and not self.use_fallback:
                logger.info("Falling back to Python weather tool")
                self.use_fallback = True
                self.tool_path = self.fallback_path
                return await self.get_weather_data(location)
            return None
    
    async def get_weather_with_forecast(self, location: Optional[str] = None) -> Optional[Dict]:
        """Get weather with hourly and daily forecast"""
        if not self.tool_available:
            return None
        
        try:
            # Build command with forecast flag
            if location:
                cmd = [str(self.tool_path), "city", location, "--forecast"]
            else:
                cmd = [str(self.tool_path), "current", "--forecast"]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15.0)
            
            if process.returncode == 0:
                weather_data = json.loads(stdout.decode('utf-8'))
                return self._enhance_weather_data(weather_data)
                
        except Exception as e:
            logger.error(f"Error getting weather with forecast: {e}")
        
        return None
    
    async def get_current_location(self) -> Optional[Dict]:
        """Get current location information"""
        if not self.tool_available:
            return None
        
        try:
            cmd = [str(self.tool_path), "location"]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5.0)
            
            if process.returncode == 0:
                return json.loads(stdout.decode('utf-8'))
                
        except Exception as e:
            logger.error(f"Error getting location: {e}")
        
        return None
    
    async def get_temperature_only(self) -> Optional[Dict]:
        """Get just temperature data (faster)"""
        if not self.tool_available:
            return None
        
        try:
            cmd = [str(self.tool_path), "temperature"]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5.0)
            
            if process.returncode == 0:
                return json.loads(stdout.decode('utf-8'))
                
        except Exception as e:
            logger.error(f"Error getting temperature: {e}")
        
        return None
    
    def _enhance_weather_data(self, data: Dict) -> Dict:
        """Enhance weather data for compatibility with existing JARVIS code"""
        if not data:
            return data
        
        # Add temperature_unit for display
        data['temperature_unit'] = '°C'
        
        # Add weather insights based on conditions
        insights = []
        
        temp = data.get('temperature', 20)
        humidity = data.get('humidity', 50)
        uv_index = data.get('uv_index', 0)
        wind_speed = data.get('wind_speed', 0)
        
        # Temperature insights
        if temp > 30:
            insights.append("Stay hydrated in this hot weather")
        elif temp < 5:
            insights.append("Bundle up warmly in the cold")
        elif 20 <= temp <= 25:
            insights.append("Perfect temperature for outdoor activities")
        
        # UV insights
        if uv_index >= 8:
            insights.append("Very high UV - wear sunscreen and protective clothing")
        elif uv_index >= 6:
            insights.append("High UV levels - consider sun protection")
        
        # Humidity insights
        if humidity > 80:
            insights.append("High humidity may make it feel warmer")
        elif humidity < 20:
            insights.append("Very dry conditions - stay hydrated")
        
        # Wind insights  
        if wind_speed > 40:
            insights.append("Strong winds - secure loose items")
        elif wind_speed > 25:
            insights.append("Breezy conditions")
        
        # Rain/snow insights
        condition = data.get('condition', '').lower()
        if 'rain' in condition:
            insights.append("Don't forget your umbrella")
        elif 'snow' in condition:
            insights.append("Drive carefully in snowy conditions")
        elif 'storm' in condition or 'thunder' in condition:
            insights.append("Stay indoors during the storm if possible")
        
        # Weather alerts
        alerts = data.get('alerts', [])
        if alerts:
            for alert in alerts[:2]:  # Show max 2 alerts
                severity = alert.get('severity', 'unknown')
                summary = alert.get('summary', '')
                if severity in ['severe', 'extreme']:
                    insights.insert(0, f"⚠️ {summary}")
                elif severity == 'moderate':
                    insights.append(f"Weather alert: {summary}")
        
        data['insights'] = insights[:3]  # Limit to 3 most relevant insights
        
        # Add icon mapping for common conditions
        icon_map = {
            'clear': '☀️',
            'sunny': '☀️',
            'partly cloudy': '⛅',
            'cloudy': '☁️',
            'overcast': '☁️',
            'rain': '🌧️',
            'heavy rain': '🌧️',
            'drizzle': '🌦️',
            'snow': '❄️',
            'heavy snow': '❄️',
            'thunderstorm': '⛈️',
            'fog': '🌫️',
            'mist': '🌫️',
            'windy': '💨'
        }
        
        condition_lower = data.get('condition', '').lower()
        for key, icon in icon_map.items():
            if key in condition_lower:
                data['condition_icon'] = icon
                break
        
        # Ensure all expected fields exist
        data.setdefault('source', 'WeatherKit')
        data.setdefault('timestamp', datetime.now().isoformat())
        
        return data
    
    async def check_availability(self) -> bool:
        """Check if Swift weather tool is available and working"""
        if not self.tool_available:
            return False
        
        try:
            # Try to get version
            process = await asyncio.create_subprocess_exec(
                str(self.tool_path), "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=2.0)
            
            if process.returncode == 0:
                version = stdout.decode('utf-8').strip()
                logger.info(f"Swift weather tool version: {version}")
                return True
                
        except Exception as e:
            logger.error(f"Swift weather tool check failed: {e}")
        
        return False
    
    async def build_tool(self) -> bool:
        """Attempt to build the Swift tool if not available"""
        build_script = self.tool_path.parent / "build.sh"
        
        if not build_script.exists():
            logger.error("Build script not found")
            return False
        
        try:
            logger.info("Attempting to build Swift weather tool...")
            
            process = await asyncio.create_subprocess_exec(
                str(build_script),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(build_script.parent)
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode == 0:
                logger.info("Swift weather tool built successfully")
                self.tool_available = self.tool_path.exists()
                return self.tool_available
            else:
                logger.error(f"Build failed: {stderr.decode('utf-8')}")
                return False
                
        except Exception as e:
            logger.error(f"Failed to build Swift tool: {e}")
            return False


# Import os for file permission checks
import os