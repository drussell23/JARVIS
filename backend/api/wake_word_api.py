"""
Wake Word API
=============

REST and WebSocket API endpoints for wake word detection.
"""

import asyncio
import json
import logging
import os
from typing import Dict, Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException

logger = logging.getLogger(__name__)

# Create router
router = APIRouter(prefix="/api/wake-word", tags=["wake_word"])

# Try to import full wake word module, fallback to stub if dependencies missing
wake_service: Optional[Any] = None
wake_api: Optional[Any] = None
wake_word_enabled = False
using_stub = True

try:
    from wake_word import WakeWordService, get_config
    from wake_word.services.wake_service import WakeWordAPI
    WAKE_WORD_AVAILABLE = True
    logger.info("Wake word module imported successfully")
except ImportError as e:
    logger.warning(f"Wake word module not available, using stub API: {e}")
    WAKE_WORD_AVAILABLE = False


def initialize_wake_word(activation_callback=None):
    """Initialize wake word service"""
    global wake_service, wake_api, wake_word_enabled, using_stub

    if not WAKE_WORD_AVAILABLE:
        logger.info("Wake word detection not available - using stub API")
        using_stub = True
        wake_word_enabled = False
        return False

    try:
        config = get_config()

        if not config.enabled:
            logger.info("Wake word detection disabled in configuration")
            using_stub = True
            wake_word_enabled = False
            return False

        wake_service = WakeWordService()
        wake_api = WakeWordAPI(wake_service)
        using_stub = False
        wake_word_enabled = True

        logger.info("Wake word API initialized successfully")
        return True

    except Exception as e:
        logger.error(f"Failed to initialize wake word API: {e}")
        using_stub = True
        wake_word_enabled = False
        return False


@router.get("/status")
async def get_status() -> Dict[str, Any]:
    """Get wake word service status"""
    if using_stub or not wake_api:
        # Return stub status
        return {
            'enabled': False,
            'available': False,
            'state': 'not_available',
            'is_listening': False,
            'engines': {},
            'activation_count': 0,
            'last_activation': None,
            'wake_words': ['hey jarvis'],
            'sensitivity': 'medium',
            'message': 'Wake word detection not available - missing dependencies'
        }

    return await wake_api.get_status()


@router.post("/enable")
async def enable_wake_word() -> Dict[str, Any]:
    """Enable wake word detection"""
    if using_stub or not wake_api:
        return {
            'success': False,
            'message': 'Wake word service not available - missing dependencies'
        }

    return await wake_api.enable()


@router.post("/disable")
async def disable_wake_word() -> Dict[str, Any]:
    """Disable wake word detection"""
    if using_stub or not wake_api:
        return {
            'success': False,
            'message': 'Wake word service not available'
        }

    return await wake_api.disable()


@router.post("/test")
async def test_activation() -> Dict[str, Any]:
    """Test wake word activation response"""
    if using_stub or not wake_api:
        return {
            'success': False,
            'message': 'Wake word service not available'
        }

    return await wake_api.test_activation()


@router.put("/settings")
async def update_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Update wake word settings"""
    if using_stub or not wake_api:
        return {
            'success': False,
            'message': 'Wake word service not available'
        }

    # Validate settings
    valid_settings = {}

    if 'wake_words' in settings:
        if isinstance(settings['wake_words'], list):
            valid_settings['wake_words'] = settings['wake_words']
        else:
            raise HTTPException(status_code=400, detail="wake_words must be a list")

    if 'sensitivity' in settings:
        if settings['sensitivity'] in ['very_low', 'low', 'medium', 'high', 'very_high']:
            valid_settings['sensitivity'] = settings['sensitivity']
        else:
            raise HTTPException(status_code=400, detail="Invalid sensitivity level")

    if 'activation_responses' in settings:
        if isinstance(settings['activation_responses'], list):
            valid_settings['activation_responses'] = settings['activation_responses']

    return await wake_api.update_settings(valid_settings)


@router.get("/config")
async def get_configuration() -> Dict[str, Any]:
    """Get current wake word configuration"""
    if using_stub or not WAKE_WORD_AVAILABLE:
        return {
            'enabled': False,
            'available': False,
            'wake_words': ['hey jarvis'],
            'sensitivity': 'medium'
        }

    config = get_config()
    return config.to_dict()


@router.post("/feedback/false-positive")
async def report_false_positive() -> Dict[str, Any]:
    """Report a false positive detection"""
    if using_stub or not wake_service:
        return {
            'success': False,
            'message': 'Wake word service not available'
        }

    wake_service.report_false_positive()
    return {
        'success': True,
        'message': 'False positive reported. Adjusting sensitivity.'
    }


@router.websocket("/stream")
async def wake_word_websocket(websocket: WebSocket):
    """WebSocket for real-time wake word events"""
    await websocket.accept()

    if using_stub or not wake_service:
        await websocket.send_json({
            'type': 'error',
            'message': 'Wake word service not available'
        })
        await websocket.close()
        return
    
    # Subscribe to wake word events
    async def send_event(event: Dict[str, Any]):
        """Send event to WebSocket client"""
        try:
            await websocket.send_json(event)
        except Exception as e:
            logger.error(f"Failed to send WebSocket event: {e}")
    
    # Set up event callback
    original_callback = wake_service.activation_callback
    wake_service.activation_callback = send_event
    
    # WebSocket idle timeout protection
    idle_timeout = float(os.getenv("TIMEOUT_WEBSOCKET_IDLE", "300.0"))  # 5 min default

    try:
        while True:
            # Receive messages from client with timeout
            try:
                message = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=idle_timeout
                )

                if message.get('type') == 'ping':
                    await websocket.send_json({'type': 'pong'})

                elif message.get('type') == 'command_received':
                    await wake_service.handle_command_received()

                elif message.get('type') == 'command_complete':
                    await wake_service.handle_command_complete()

            except asyncio.TimeoutError:
                logger.info("Wake word WebSocket idle timeout, closing connection")
                break
            except json.JSONDecodeError:
                await websocket.send_json({
                    'type': 'error',
                    'message': 'Invalid JSON message'
                })

    except WebSocketDisconnect:
        logger.info("Wake word WebSocket disconnected")
    finally:
        # Restore original callback
        wake_service.activation_callback = original_callback


@router.get("/statistics")
async def get_statistics() -> Dict[str, Any]:
    """Get detection statistics"""
    if not wake_service or not wake_service.detector:
        raise HTTPException(status_code=503, detail="Wake word service not available")
    
    stats = wake_service.detector.get_statistics()
    
    # Add service-level stats
    stats['service'] = {
        'state': wake_service.state,
        'activation_history_count': len(wake_service.activation_history),
        'config': {
            'wake_words': wake_service.config.detection.wake_words,
            'sensitivity': wake_service.config.detection.sensitivity,
            'engine': wake_service.config.detection.engine
        }
    }
    
    return stats


# Export for main app
__all__ = ['router', 'initialize_wake_word', 'wake_service']