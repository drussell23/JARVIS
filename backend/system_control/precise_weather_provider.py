"""
Precise Weather Provider for JARVIS
Uses Core Location for accurate GPS-based weather
"""

import json
import asyncio
import subprocess
import logging
from pathlib import Path
from typing import Dict, Optional, Tuple
from datetime import datetime
import aiohttp

# Import the location service
from .native.location_service import LocationService

logger = logging.getLogger(__name__)


class PreciseWeatherProvider:
    """Weather provider using precise Core Location data"""
    
    def __init__(self):
        self.location_service = LocationService()
        self.weather_cache = {}
        self.cache_duration = 300  # 5 minutes
        
    async def get_weather_data(self, location: Optional[str] = None) -> Optional[Dict]:
        """Get weather data using precise location or city name"""
        try:
            if location:
                # City name provided - use weather API directly
                return await self._fetch_weather_by_city(location)
            else:
                # No location provided - use Core Location
                return await self._fetch_weather_by_coordinates()
                
        except Exception as e:
            logger.error(f"Error getting weather data: {e}")
            return None
            
    async def _fetch_weather_by_coordinates(self) -> Optional[Dict]:
        """Get weather using precise GPS coordinates"""
        # Try multiple location methods
        location_data = None
        
        # Method 1: Try our location service
        try:
            location_data = await asyncio.get_event_loop().run_in_executor(
                None, self.location_service.get_current_location
            )
        except Exception as e:
            logger.debug(f"Location service failed: {e}")
        
        # Method 2: Try direct GPS if available
        if not location_data or location_data.get('status') != 'success':
            try:
                # Try to get GPS coordinates directly
                result = subprocess.run(
                    ['./get-gps-coords'],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    cwd=str(Path(__file__).parent / 'native')
                )
                if result.returncode == 0 and ',' in result.stdout:
                    parts = result.stdout.strip().split(',')
                    if len(parts) >= 2:
                        location_data = {
                            'latitude': float(parts[0]),
                            'longitude': float(parts[1]),
                            'accuracy': float(parts[2]) if len(parts) > 2 else 100,
                            'status': 'success',
                            'source': 'GPS'
                        }
            except Exception:
                pass
        
        # Method 3: Fall back to IP location
        if not location_data or location_data.get('status') != 'success':
            logger.warning("Failed to get precise location, falling back to IP")
            return await self._fallback_to_ip_location()
            
        # Extract coordinates
        lat = location_data['latitude']
        lon = location_data['longitude']
        city = location_data.get('city', 'Unknown')
        region = location_data.get('region', '')
        
        logger.info(f"Got precise location: {city}, {region} ({lat}, {lon})")
        
        # Check cache
        cache_key = f"{lat:.4f},{lon:.4f}"
        if cached := self._get_cached_weather(cache_key):
            return cached
            
        # Fetch weather for exact coordinates
        weather_data = await self._call_weather_api(lat, lon)
        
        if weather_data:
            # Enhance with location info
            weather_data['location_source'] = location_data.get('source', 'GPS')
            weather_data['accuracy_meters'] = location_data.get('accuracy', 0)
            weather_data['gps_coordinates'] = {
                'latitude': lat,
                'longitude': lon
            }
            
            # Override generic location with precise one
            if city and city != 'Unknown':
                if region:
                    weather_data['location'] = f"{city}, {region}"
                else:
                    weather_data['location'] = city
                    
            # Cache the result
            self._cache_weather(cache_key, weather_data)
            
        return weather_data
        
    async def _fetch_weather_by_city(self, city: str) -> Optional[Dict]:
        """Get weather for a specific city"""
        # Check cache
        if cached := self._get_cached_weather(city.lower()):
            return cached
            
        # Use wttr.in API
        url = f"https://wttr.in/{city}?format=j1"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        weather = self._parse_wttr_response(data, city)
                        
                        if weather:
                            weather['location_source'] = 'city_name'
                            self._cache_weather(city.lower(), weather)
                            
                        return weather
        except Exception as e:
            logger.error(f"Error fetching weather for {city}: {e}")
            
        return None
        
    async def _call_weather_api(self, lat: float, lon: float) -> Optional[Dict]:
        """Call weather API with coordinates"""
        url = f"https://wttr.in/{lat},{lon}?format=j1"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        return self._parse_wttr_response(data, f"{lat},{lon}")
        except Exception as e:
            logger.error(f"Error calling weather API: {e}")
            
        return None
        
    async def _fallback_to_ip_location(self) -> Optional[Dict]:
        """Fallback to IP-based location"""
        try:
            # Use IP geolocation API
            async with aiohttp.ClientSession() as session:
                async with session.get("http://ipapi.co/json/") as response:
                    if response.status == 200:
                        ip_data = await response.json()
                        
                        lat = ip_data.get('latitude')
                        lon = ip_data.get('longitude')
                        city = ip_data.get('city', 'Unknown')
                        region = ip_data.get('region', '')
                        
                        if lat and lon:
                            weather = await self._call_weather_api(lat, lon)
                            if weather:
                                weather['location_source'] = 'IP'
                                weather['location'] = f"{city}, {region}" if region else city
                                return weather
                                
        except Exception as e:
            logger.error(f"IP location fallback failed: {e}")
            
        return None
        
    def _parse_wttr_response(self, data: Dict, location_str: str) -> Optional[Dict]:
        """Parse wttr.in API response"""
        try:
            current = data['current_condition'][0]
            area = data['nearest_area'][0]
            
            # Get location details
            city = area['areaName'][0]['value']
            country = area['country'][0]['value']
            lat = float(area['latitude'])
            lon = float(area['longitude'])
            
            # Parse weather data
            return {
                'location': f"{city}, {country}",
                'temperature': int(current['temp_C']),
                'temperature_f': int(current['temp_F']),
                'condition': current['weatherDesc'][0]['value'],
                'humidity': int(current['humidity']),
                'wind_speed': float(current['windspeedKmph']),
                'wind_speed_mph': float(current['windspeedMiles']),
                'wind_direction': current['winddir16Point'],
                'pressure': int(current['pressure']),
                'visibility': float(current['visibility']),
                'uv_index': int(current['uvIndex']),
                'feels_like': int(current['FeelsLikeC']),
                'feels_like_f': int(current['FeelsLikeF']),
                'cloud_cover': int(current['cloudcover']),
                'description': current['weatherDesc'][0]['value'].lower(),
                'latitude': lat,
                'longitude': lon,
                'timestamp': datetime.now().isoformat(),
                'source': 'wttr.in'
            }
        except Exception as e:
            logger.error(f"Error parsing weather response: {e}")
            return None
            
    def _get_cached_weather(self, key: str) -> Optional[Dict]:
        """Get weather from cache if valid"""
        if key in self.weather_cache:
            cached_data, timestamp = self.weather_cache[key]
            if (datetime.now() - timestamp).seconds < self.cache_duration:
                logger.info(f"Using cached weather for {key}")
                return cached_data
        return None
        
    def _cache_weather(self, key: str, data: Dict):
        """Cache weather data"""
        self.weather_cache[key] = (data, datetime.now())
        
    async def get_location_info(self) -> Optional[Dict]:
        """Get current location information"""
        location_data = await asyncio.get_event_loop().run_in_executor(
            None, self.location_service.get_city_info
        )
        
        if location_data:
            return {
                'city': location_data['city'],
                'region': location_data['region'],
                'country': location_data['country'],
                'coordinates': location_data['coordinates'],
                'source': 'GPS'
            }
            
        # Fallback to IP
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("http://ipapi.co/json/") as response:
                    if response.status == 200:
                        data = await response.json()
                        return {
                            'city': data.get('city'),
                            'region': data.get('region'),
                            'country': data.get('country_name'),
                            'coordinates': (data.get('latitude'), data.get('longitude')),
                            'source': 'IP'
                        }
        except Exception:
            pass

        return None


# Test function
async def test_precise_weather():
    """Test the precise weather provider"""
    provider = PreciseWeatherProvider()
    
    print("Testing precise weather provider...")
    print("=" * 50)
    
    # Test current location weather
    print("\n1. Testing current location weather:")
    weather = await provider.get_weather_data()
    if weather:
        print(f"   Location: {weather['location']} (via {weather.get('location_source', 'unknown')})")
        print(f"   Temperature: {weather['temperature']}°C / {weather['temperature_f']}°F")
        print(f"   Condition: {weather['condition']}")
        print(f"   Accuracy: {weather.get('accuracy_meters', 'N/A')} meters")
    else:
        print("   Failed to get weather")
        
    # Test city weather
    print("\n2. Testing city weather (Toronto):")
    weather = await provider.get_weather_data("Toronto")
    if weather:
        print(f"   Location: {weather['location']}")
        print(f"   Temperature: {weather['temperature']}°C / {weather['temperature_f']}°F")
        print(f"   Condition: {weather['condition']}")
    else:
        print("   Failed to get weather")
        
    # Test location info
    print("\n3. Testing location info:")
    location = await provider.get_location_info()
    if location:
        print(f"   City: {location['city']}")
        print(f"   Region: {location['region']}")
        print(f"   Country: {location['country']}")
        print(f"   Coordinates: {location['coordinates']}")
        print(f"   Source: {location['source']}")
    else:
        print("   Failed to get location")


if __name__ == "__main__":
    # Run test
    asyncio.run(test_precise_weather())