#!/usr/bin/env python3
"""
Legacy entry point — delegates to unified_supervisor.py.

v223.0: All supervisor logic now lives in unified_supervisor.py.
This shim preserves backward compatibility for anyone using
`python run_supervisor.py` while ensuring all logic is centralized.

Usage:
    python3 run_supervisor.py [args]
    → equivalent to: python3 unified_supervisor.py [args]
"""
import os
import subprocess
import sys

script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "unified_supervisor.py")
sys.exit(subprocess.call([sys.executable, script] + sys.argv[1:]))
