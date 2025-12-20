#!/usr/bin/env python3
"""Simple JARVIS startup script that skips problematic components."""

import os
import sys

# Disable components that might be causing issues
os.environ['SKIP_RUST_BUILD'] = 'true'
os.environ['SKIP_VISION_INTELLIGENCE'] = 'true'
os.environ['OPTIMIZE_STARTUP'] = 'true'
os.environ['BACKEND_PORT'] = '8010'

# Import and run main
sys.path.insert(0, os.path.dirname(__file__))

print("🚀 Starting JARVIS in simplified mode...")
print("   - Skipping Rust build attempts")
print("   - Skipping vision intelligence")
print("   - Audio endpoints will be available")

import main

# The app will run when main.py is imported