"""
macOS Weather Provider for JARVIS
Uses system commands and intelligent location detection
"""

import os
import json
import asyncio
import subprocess
import logging
from typing import Dict, Optional, Tuple
from datetime import datetime
import aiohttp
import re

logger = logging.getLogger(__name__)

class MacOSWeatherProvider:
    """Weather provider that uses macOS system capabilities and intelligent APIs"""
    
    def __init__(self):
        # Try to use existing weather service as primary source
        self._init_weather_service()
        
        # Initialize location service
        from .location_service import MacOSLocationService
        self.location_service = MacOSLocationService()
        
        # Location cache
        self._location_cache = None
        self._location_cache_time = None
        self._cache_duration = 3600  # 1 hour for location
        
    def _init_weather_service(self):
        """Initialize weather service if available"""
        try:
            # Try different import paths
            try:
                from backend.services.weather_service import WeatherService
            except ImportError:
                try:
                    from services.weather_service import WeatherService
                except ImportError:
                    from ..services.weather_service import WeatherService
                    
            api_key = os.getenv("OPENWEATHER_API_KEY")
            if api_key:
                self.weather_service = WeatherService(api_key)
                logger.info("Weather service initialized with API key")
            else:
                self.weather_service = None
                logger.info("No weather API key found - will use alternative methods")
        except Exception as e:
            self.weather_service = None
            logger.debug(f"Weather service not available: {e}")
    
    async def get_system_location(self) -> Optional[Tuple[float, float, str]]:
        """Get actual device location using location services"""
        try:
            # Use the location service to get actual location
            location_data = await self.location_service.get_current_location()
            
            if location_data:
                lat = location_data.get("latitude")
                lon = location_data.get("longitude")
                
                if lat and lon:
                    # Check if we have city from location data
                    city = location_data.get("city", "")
                    
                    # If no city, reverse geocode
                    if not city:
                        city = await self._reverse_geocode(lat, lon)
                    
                    logger.info(f"Got actual location: {city} ({lat}, {lon})")
                    return lat, lon, city
            
            logger.debug("No location data from location service")
                    
        except Exception as e:
            logger.debug(f"Error getting system location: {e}")
        
        return None
    
    async def _reverse_geocode(self, lat: float, lon: float) -> str:
        """Get city name from coordinates"""
        try:
            url = f"https://nominatim.openstreetmap.org/reverse"
            params = {
                "lat": lat,
                "lon": lon,
                "format": "json",
                "limit": 1,
                "zoom": 10  # City-level detail
            }
            headers = {
                "User-Agent": "JARVIS-Weather/1.0"
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers, timeout=5) as response:
                    if response.status == 200:
                        data = await response.json()
                        address = data.get("address", {})
                        
                        # Try to get the most appropriate location name
                        city = (address.get("city") or 
                               address.get("town") or 
                               address.get("village") or 
                               address.get("suburb") or
                               address.get("county") or
                               address.get("state") or 
                               "your location")
                        
                        # Add state/country for context if available
                        state = address.get("state", "")
                        country = address.get("country", "")
                        
                        if city != "your location":
                            if state and country and country == "United States":
                                return f"{city}, {state}"
                            elif country and country != city:
                                return f"{city}, {country}"
                        
                        return city
        except Exception as e:
            logger.debug(f"Reverse geocoding failed: {e}")
        
        return "your area"
    
    async def get_location_from_ip(self) -> Tuple[float, float, str]:
        """Get location from IP address"""
        try:
            # Try multiple IP geolocation services
            services = [
                ("https://ipapi.co/json/", {"lat": "latitude", "lon": "longitude", "city": "city", "region": "region"}),
                ("https://ip-api.com/json/", {"lat": "lat", "lon": "lon", "city": "city", "region": "regionName"}),
                ("https://ipinfo.io/json", {"city": "city", "region": "region", "loc": "loc"})
            ]
            
            for service_url, fields in services:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(service_url, timeout=3) as response:
                            if response.status == 200:
                                data = await response.json()
                                
                                # Handle different response formats
                                if "loc" in fields and fields["loc"] in data:
                                    # ipinfo.io format: "lat,lon"
                                    loc_parts = data[fields["loc"]].split(',')
                                    if len(loc_parts) == 2:
                                        lat = float(loc_parts[0])
                                        lon = float(loc_parts[1])
                                    else:
                                        continue
                                else:
                                    # Standard format
                                    lat = data.get(fields.get("lat"))
                                    lon = data.get(fields.get("lon"))
                                    if lat:
                                        lat = float(lat)
                                    if lon:
                                        lon = float(lon)
                                
                                city = data.get(fields.get("city", "city"), "")
                                region = data.get(fields.get("region", "region"), "")
                                
                                if lat and lon and city:
                                    # Format city name nicely
                                    if region and region != city:
                                        city_name = f"{city}, {region}"
                                    else:
                                        city_name = city
                                    return lat, lon, city_name
                except Exception:
                    continue
                    
        except Exception as e:
            logger.debug(f"Error getting IP location: {e}")
        
        # Get more accurate location from IP
        try:
            # Try ip-api.com for better accuracy
            url = "http://ip-api.com/json/?fields=status,city,regionName,country,lat,lon,timezone"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=3) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get("status") == "success":
                            city = data.get("city", "Unknown")
                            region = data.get("regionName", "")
                            lat = data.get("lat", 0)
                            lon = data.get("lon", 0)
                            
                            if city and city != "Unknown":
                                if region:
                                    return lat, lon, f"{city}, {region}"
                                else:
                                    return lat, lon, city
        except Exception:
            pass

        # Final fallback - return None to indicate failure
        return None, None, None
    
    async def get_current_location(self) -> Tuple[float, float, str]:
        """Get current location with caching"""
        # Check cache
        if self._location_cache and self._location_cache_time:
            age = (datetime.now() - self._location_cache_time).seconds
            if age < self._cache_duration:
                return self._location_cache
        
        # Try system location first (most accurate)
        location = await self.get_system_location()
        
        # Fallback to IP location
        if not location or location == (None, None, None):
            location = await self.get_location_from_ip()
        
        # Only cache valid results
        if location and location != (None, None, None):
            self._location_cache = location
            self._location_cache_time = datetime.now()
            return location
        
        # Return a reasonable default if all methods fail
        return None, None, "Current Location"
    
    async def get_weather_data(self, location: Optional[str] = None) -> Dict:
        """Get weather data for current or specified location"""
        try:
            # If we have weather service, use it
            if self.weather_service:
                if location:
                    return await self.weather_service.get_weather_by_city(location)
                else:
                    return await self.weather_service.get_current_weather()
            
            # Otherwise, try to get basic weather info
            lat, lon, city = await self.get_current_location()
            
            # Check if we got valid location
            if lat is None or lon is None:
                logger.warning("Could not determine location")
                return self._get_fallback_weather(location or "your location")
            
            # Use detected location if no specific location requested
            final_location = location if location else city
            logger.info(f"Getting weather for: {final_location}")
            
            # Try free weather API without key
            return await self._get_weather_from_wttr(final_location)
            
        except Exception as e:
            logger.error(f"Error getting weather data: {e}")
            return self._get_fallback_weather(location or "your location")
    
    async def _get_weather_from_wttr(self, location: str) -> Dict:
        """Get weather from wttr.in (no API key required)"""
        try:
            # Clean location string
            location = location.replace(' ', '+')
            
            url = f"https://wttr.in/{location}?format=j1"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=5) as response:
                    if response.status == 200:
                        data = await response.json()
                        current = data.get("current_condition", [{}])[0]
                        
                        # Get the location query from response
                        query_location = data.get("request", [{}])[0].get("query", location)
                        
                        # Clean up location if it's in "Lat X and Lon Y" format
                        if query_location.startswith("Lat ") and " and Lon " in query_location:
                            # Try to get nearest area name
                            nearest_area = data.get("nearest_area", [{}])[0]
                            area_name = nearest_area.get("areaName", [{}])[0].get("value", "")
                            region = nearest_area.get("region", [{}])[0].get("value", "")
                            country = nearest_area.get("country", [{}])[0].get("value", "")
                            
                            if area_name:
                                if region and country == "United States":
                                    query_location = f"{area_name}, {region}"
                                elif country:
                                    query_location = f"{area_name}, {country}"
                                else:
                                    query_location = area_name
                            else:
                                query_location = location if location != "+" else "your location"
                        
                        return {
                            "location": query_location,
                            "temperature": int(current.get("temp_C", 0)),
                            "feels_like": int(current.get("FeelsLikeC", 0)),
                            "description": current.get("weatherDesc", [{}])[0].get("value", "Unknown"),
                            "humidity": int(current.get("humidity", 0)),
                            "wind_speed": float(current.get("windspeedKmph", 0)),
                            "pressure": int(current.get("pressure", 0)),
                            "visibility": float(current.get("visibility", 10)),
                            "uv_index": int(current.get("uvIndex", 0)),
                            "cloud_cover": int(current.get("cloudcover", 0)),
                            "source": "wttr.in",
                            "timestamp": datetime.now().isoformat()
                        }
                        
        except Exception as e:
            logger.debug(f"Error getting weather from wttr.in: {e}")
        
        return self._get_fallback_weather(location)
    
    def _get_fallback_weather(self, location: str) -> Dict:
        """Fallback weather data when APIs are unavailable"""
        return {
            "location": location,
            "temperature": 20,
            "feels_like": 18,
            "description": "Weather data temporarily unavailable",
            "humidity": 50,
            "wind_speed": 10,
            "source": "fallback",
            "message": "I'm unable to access live weather data at the moment. Please check your internet connection.",
            "timestamp": datetime.now().isoformat()
        }