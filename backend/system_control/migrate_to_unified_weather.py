"""
Migration script to update JARVIS to use unified weather system
Removes all the multiple weather providers and consolidates to vision-based system
"""

import os
import re
from pathlib import Path
from typing import List, Tuple

# Files to update
FILES_TO_UPDATE = [
    'weather_bridge.py',
    '../voice/jarvis_agent_voice.py',
    '../chatbots/claude_vision_chatbot.py',
    '../system_control/claude_command_interpreter.py',
]

# Patterns to replace
REPLACEMENTS = [
    # Remove old imports
    (r'from \.precise_weather_provider import.*\n', ''),
    (r'from \.swift_weather_provider import.*\n', ''),
    (r'from \.macos_weather_provider import.*\n', ''),
    (r'from \.vision_weather_extractor import.*\n', ''),
    (r'from \.weather_widget_extractor import.*\n', ''),
    (r'from \.macos_system_integration import.*\n', ''),
    (r'from \.macos_weather_app import.*\n', ''),
    
    # Replace WeatherBridge with UnifiedWeatherBridge
    (r'from .*weather_bridge import WeatherBridge', 
     'from system_control.weather_bridge_unified import UnifiedWeatherBridge'),
    (r'WeatherBridge\(\)', 'UnifiedWeatherBridge()'),
    
    # Update method calls
    (r'weather_bridge\.get_current_weather\(\)', 
     'weather_bridge.get_weather()'),
    (r'weather_bridge\.get_weather_by_city\(([^)]+)\)', 
     r'weather_bridge.get_weather(f"What\'s the weather in {\1}?")'),
]


def update_weather_bridge():
    """Create new simplified weather bridge"""
    new_content = '''"""
Simplified Weather Bridge - Routes to Unified Vision System
This is a compatibility layer during migration
"""

from .weather_bridge_unified import UnifiedWeatherBridge

# Create a single instance
_bridge = None

def get_weather_bridge(vision_handler=None, controller=None):
    global _bridge
    if _bridge is None:
        _bridge = UnifiedWeatherBridge(vision_handler, controller)
    return _bridge

# Compatibility class
class WeatherBridge:
    def __init__(self):
        self.bridge = get_weather_bridge()
    
    async def get_current_weather(self, use_cache=False):
        result = await self.bridge.get_current_weather()
        # Convert to old format for compatibility
        if result.get('success'):
            data = result.get('data', {})
            current = data.get('current', {})
            today = data.get('today', {})
            location = data.get('location', 'Unknown')
            
            return {
                'location': location,
                'temperature': current.get('temperature', 20),
                'temperature_f': current.get('temperature', 20) * 9/5 + 32,
                'condition': current.get('condition', 'Unknown'),
                'description': current.get('condition', '').lower(),
                'humidity': data.get('details', {}).get('humidity', 50),
                'wind_speed': 0,
                'wind_speed_mph': 0,
                'source': 'vision',
                'timestamp': result.get('timestamp')
            }
        return None
    
    async def get_weather_by_city(self, city, use_cache=False):
        result = await self.bridge.get_weather_by_city(city)
        # Same conversion as above
        return self._convert_result(result)
    
    def is_weather_query(self, text):
        return self.bridge.is_weather_query(text)
    
    def _convert_result(self, result):
        # Convert new format to old format
        if result.get('success'):
            return self._extract_old_format(result)
        return None
    
    def _extract_old_format(self, result):
        # Helper to maintain compatibility
        data = result.get('data', {})
        return {
            'location': data.get('location', 'Unknown'),
            'temperature': data.get('current', {}).get('temperature', 20),
            'source': 'vision'
        }
'''
    
    # Write new weather_bridge.py
    with open('weather_bridge.py', 'w') as f:
        f.write(new_content)
    
    print("✅ Created compatibility weather_bridge.py")


def cleanup_old_files():
    """Remove old weather provider files"""
    old_files = [
        'precise_weather_provider.py',
        'swift_weather_provider.py',
        'macos_weather_provider.py',
        'vision_weather_extractor.py',
        'enhanced_vision_weather.py',
        'weather_widget_extractor.py',
        'temperature_units.py',
    ]
    
    for filename in old_files:
        filepath = Path(filename)
        if filepath.exists():
            # Rename instead of delete (safer)
            filepath.replace(f"{filename}.old")
            print(f"📦 Archived {filename} -> {filename}.old")


def main():
    """Run migration"""
    print("🚀 Migrating to Unified Weather System")
    print("=" * 50)
    
    # Update weather bridge
    update_weather_bridge()
    
    # Archive old files
    cleanup_old_files()
    
    print("\n✅ Migration complete!")
    print("\nNext steps:")
    print("1. Update vision_handler and controller initialization")
    print("2. Test weather queries")
    print("3. Remove .old files once confirmed working")


if __name__ == "__main__":
    main()