"""
JARVIS Voice Unlock Module
========================

Voice-based biometric authentication system for macOS unlocking.
Provides hands-free, secure access to Mac devices using voice recognition.

Features:
- Voice enrollment and profile management
- Real-time voice authentication
- Anti-spoofing protection
- System integration (screensaver, PAM)
- Multi-user support
- Apple Watch proximity detection
- ML optimization for 16GB RAM systems

Version: 2.0.0 - Clean Architecture (No Placeholders)
"""

__version__ = "2.0.0"
__author__ = "JARVIS Team"

import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# ============================================================================
# Lazy-loaded Service Instances
# ============================================================================
# Services are initialized on first access to reduce startup time and
# prevent import errors from affecting module loading.
# ============================================================================

_intelligent_unlock_service = None
_voice_unlock_system = None


async def get_intelligent_unlock_service():
    """
    Get the IntelligentVoiceUnlockService instance.

    This is the main service for voice-authenticated screen unlocking.
    Lazy-loaded to prevent blocking on import.
    """
    global _intelligent_unlock_service

    if _intelligent_unlock_service is None:
        try:
            from .intelligent_voice_unlock_service import (
                get_intelligent_unlock_service as _get_service
            )
            _intelligent_unlock_service = _get_service()
            await _intelligent_unlock_service.initialize()
            logger.info("✅ IntelligentVoiceUnlockService initialized")
        except Exception as e:
            logger.error(f"Failed to initialize IntelligentVoiceUnlockService: {e}")
            raise

    return _intelligent_unlock_service


def get_voice_unlock_system():
    """
    Get or create the voice unlock system instance (synchronous).

    For backwards compatibility with code expecting synchronous access.
    """
    global _voice_unlock_system

    if _voice_unlock_system is None:
        try:
            from .voice_unlock_integration import VoiceUnlockSystem
            _voice_unlock_system = VoiceUnlockSystem()
            logger.info("Voice Unlock System initialized")
        except Exception as e:
            logger.error(f"Failed to initialize Voice Unlock System: {e}")
            return None

    return _voice_unlock_system


async def initialize_voice_unlock():
    """Initialize the voice unlock system asynchronously."""
    try:
        service = await get_intelligent_unlock_service()
        if service:
            logger.info("Voice Unlock System started successfully")
            return service
    except Exception as e:
        logger.error(f"Failed to start Voice Unlock System: {e}")
        return None


async def cleanup_voice_unlock():
    """Cleanup voice unlock system resources."""
    global _intelligent_unlock_service, _voice_unlock_system

    try:
        if _intelligent_unlock_service:
            # No explicit cleanup needed for intelligent service
            _intelligent_unlock_service = None

        if _voice_unlock_system and hasattr(_voice_unlock_system, 'stop'):
            await _voice_unlock_system.stop()
            _voice_unlock_system = None

        logger.info("Voice Unlock System cleaned up")
    except Exception as e:
        logger.error(f"Error during Voice Unlock cleanup: {e}")


def get_voice_unlock_status() -> Dict[str, Any]:
    """Get current voice unlock status."""
    try:
        if _intelligent_unlock_service:
            stats = _intelligent_unlock_service.get_stats()
            return {
                'available': True,
                'initialized': _intelligent_unlock_service.initialized,
                'stats': stats
            }
        else:
            return {
                'available': False,
                'error': 'Voice Unlock Service not initialized'
            }
    except Exception as e:
        logger.error(f"Failed to get Voice Unlock status: {e}")
        return {
            'available': False,
            'error': str(e)
        }


def check_dependencies() -> Dict[str, bool]:
    """Check if all required dependencies are available."""
    dependencies = {
        'numpy': False,
        'scipy': False,
        'scikit-learn': False,
        'torch': False,
        'torchaudio': False,
        'speechbrain': False,
        'sounddevice': False,
    }

    for dep in dependencies:
        try:
            if dep == 'scikit-learn':
                __import__('sklearn')
            else:
                __import__(dep.replace('-', '_'))
            dependencies[dep] = True
        except ImportError:
            pass

    return dependencies


# ============================================================================
# Public API
# ============================================================================
__all__ = [
    # Async services
    'get_intelligent_unlock_service',
    'initialize_voice_unlock',
    'cleanup_voice_unlock',
    # Sync compatibility
    'get_voice_unlock_system',
    # Status & utilities
    'get_voice_unlock_status',
    'check_dependencies',
]
