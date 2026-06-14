"""
Voice Unlock Test Suite
======================

Comprehensive tests for all voice unlock components.
"""

# Make tests discoverable (guarded to allow ci/ subpackage to import cleanly)
try:
    from .test_voiceprint import *
except ImportError:
    pass
try:
    from .test_authentication import *
except ImportError:
    pass