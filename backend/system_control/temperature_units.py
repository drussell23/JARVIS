"""
Temperature Unit Detection for macOS
Determines whether to use Fahrenheit or Celsius based on locale
"""

import subprocess
import locale
import os

def get_temperature_unit():
    """Get the preferred temperature unit based on system settings"""
    
    # Check explicit temperature unit setting
    try:
        result = subprocess.run(
            ['defaults', 'read', '-g', 'AppleTemperatureUnit'],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            unit = result.stdout.strip()
            if unit.lower() == 'fahrenheit':
                return 'F'
            elif unit.lower() == 'celsius':
                return 'C'
    except Exception:
        pass

    # Check locale
    try:
        result = subprocess.run(
            ['defaults', 'read', '-g', 'AppleLocale'],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            locale_str = result.stdout.strip()
            # US, Liberia, Myanmar use Fahrenheit
            if any(country in locale_str for country in ['_US', '_LR', '_MM']):
                return 'F'
    except Exception:
        pass

    # Check measurement units
    try:
        result = subprocess.run(
            ['defaults', 'read', '-g', 'AppleMeasurementUnits'],
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            units = result.stdout.strip()
            if units.lower() == 'inches':  # Imperial units
                return 'F'
    except Exception:
        pass

    # Default to Celsius for most of the world
    return 'C'

def should_use_fahrenheit():
    """Check if we should use Fahrenheit"""
    return get_temperature_unit() == 'F'