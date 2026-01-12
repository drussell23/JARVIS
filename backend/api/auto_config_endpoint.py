#!/usr/bin/env python3
"""
Auto Configuration Endpoint
Helps clients automatically discover correct API configuration
"""

from fastapi import APIRouter, Request, Response
from typing import Dict, Any, Optional
import os
import socket
import json
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auto-config", tags=["configuration"])

class AutoConfigService:
    """Service for automatic configuration discovery"""
    
    @staticmethod
    def get_server_info() -> Dict[str, Any]:
        """Get current server information"""
        return {
            "hostname": socket.gethostname(),
            "port": int(os.getenv('BACKEND_PORT', '8000')),
            "protocol": "http",
            "base_url": f"http://localhost:{os.getenv('BACKEND_PORT', '8000')}",
            "ws_url": f"ws://localhost:{os.getenv('BACKEND_PORT', '8000')}",
            "api_version": "2.0",
            "features": {
                "websocket": True,
                "ml_audio": True,
                "vision": True,
                "voice": True
            }
        }
    
    @staticmethod
    def get_endpoint_map() -> Dict[str, str]:
        """Get map of all available endpoints"""
        port = os.getenv('BACKEND_PORT', '8000')
        base_url = f"http://localhost:{port}"
        ws_base = f"ws://localhost:{port}"
        
        return {
            # HTTP endpoints
            "health": f"{base_url}/health",
            "ml_audio_config": f"{base_url}/audio/ml/config",
            "ml_audio_predict": f"{base_url}/audio/ml/predict",
            "ml_audio_status": f"{base_url}/audio/ml/status",
            "voice_jarvis_status": f"{base_url}/voice/jarvis/status",
            "voice_jarvis_command": f"{base_url}/voice/jarvis/command",
            
            # WebSocket endpoints
            "ws_main": f"{ws_base}/ws",
            "ws_ml_audio": f"{ws_base}/audio/ml/stream",
            "ws_vision": f"{ws_base}/vision/ws",
            "ws_jarvis": f"{ws_base}/voice/jarvis/stream"
        }
    
    @staticmethod
    def detect_client_expectations(request: Request) -> Dict[str, Any]:
        """Detect what the client is expecting"""
        origin = request.headers.get("origin", "")
        referer = request.headers.get("referer", "")
        user_agent = request.headers.get("user-agent", "")
        
        expectations = {
            "origin": origin,
            "referer": referer,
            "user_agent": user_agent,
            "expected_port": None,
            "client_type": "unknown"
        }
        
        # Detect expected port from origin/referer
        for header in [origin, referer]:
            if header and ":" in header:
                try:
                    port = header.split(":")[-1].split("/")[0]
                    if port.isdigit():
                        expectations["expected_port"] = int(port)
                        break
                except Exception:
                    pass
        
        # Detect client type
        if "React" in user_agent or origin.endswith(":3000"):
            expectations["client_type"] = "react"
        elif "Vue" in user_agent or origin.endswith(":8080"):
            expectations["client_type"] = "vue"
        elif "Angular" in user_agent or origin.endswith(":4200"):
            expectations["client_type"] = "angular"
            
        return expectations

@router.get("/")
async def get_auto_config(request: Request) -> Dict[str, Any]:
    """Get automatic configuration for the client"""
    server_info = AutoConfigService.get_server_info()
    endpoints = AutoConfigService.get_endpoint_map()
    client_expectations = AutoConfigService.detect_client_expectations(request)
    
    # Build configuration response
    config = {
        "server": server_info,
        "endpoints": endpoints,
        "client": client_expectations,
        "recommendations": []
    }
    
    # Add recommendations based on client expectations
    if client_expectations["expected_port"] and client_expectations["expected_port"] != server_info["port"]:
        config["recommendations"].append({
            "type": "port_mismatch",
            "message": f"Your client expects port {client_expectations['expected_port']}, but server is on port {server_info['port']}",
            "solution": f"Update your client configuration to use port {server_info['port']}"
        })
    
    # Add CORS info
    config["cors"] = {
        "enabled": True,
        "dynamic": True,
        "your_origin": client_expectations["origin"],
        "allowed": True
    }
    
    return config

@router.get("/endpoints")
async def list_endpoints() -> Dict[str, Any]:
    """List all available endpoints with descriptions"""
    port = os.getenv('BACKEND_PORT', '8000')
    base_url = f"http://localhost:{port}"
    ws_base = f"ws://localhost:{port}"
    
    return {
        "base_url": base_url,
        "websocket_base": ws_base,
        "categories": {
            "ml_audio": {
                "description": "ML Audio processing endpoints",
                "endpoints": {
                    "config": {
                        "url": f"{base_url}/audio/ml/config",
                        "method": "GET",
                        "description": "Get ML audio configuration"
                    },
                    "predict": {
                        "url": f"{base_url}/audio/ml/predict",
                        "method": "POST",
                        "description": "Predict audio quality issues"
                    },
                    "analyze": {
                        "url": f"{base_url}/audio/ml/analyze",
                        "method": "POST",
                        "description": "Detailed audio analysis"
                    },
                    "stream": {
                        "url": f"{ws_base}/audio/ml/stream",
                        "method": "WebSocket",
                        "description": "Real-time audio streaming"
                    }
                }
            },
            "voice": {
                "description": "Voice and JARVIS endpoints",
                "endpoints": {
                    "status": {
                        "url": f"{base_url}/voice/jarvis/status",
                        "method": "GET",
                        "description": "JARVIS system status"
                    },
                    "command": {
                        "url": f"{base_url}/voice/jarvis/command",
                        "method": "POST",
                        "description": "Send command to JARVIS"
                    }
                }
            },
            "websocket": {
                "description": "WebSocket endpoints",
                "endpoints": {
                    "unified": {
                        "url": f"{ws_base}/ws",
                        "description": "Unified WebSocket for all communication"
                    },
                    "legacy_audio": {
                        "url": f"{ws_base}/audio/ml/stream",
                        "description": "Legacy ML audio WebSocket (redirects to unified)"
                    }
                }
            }
        }
    }

@router.get("/client-config")
async def get_client_config(request: Request) -> Response:
    """Generate JavaScript configuration for client"""
    server_info = AutoConfigService.get_server_info()
    
    # Generate JavaScript config
    js_config = f"""
// Auto-generated JARVIS API Configuration
// Generated at: {server_info['base_url']}/auto-config/client-config

window.JARVIS_CONFIG = {{
    API_BASE_URL: '{server_info['base_url']}',
    WS_BASE_URL: '{server_info['ws_url']}',
    
    // Endpoints
    endpoints: {{
        // ML Audio
        mlAudioConfig: '{server_info['base_url']}/audio/ml/config',
        mlAudioPredict: '{server_info['base_url']}/audio/ml/predict',
        mlAudioStatus: '{server_info['base_url']}/audio/ml/status',
        mlAudioStream: '{server_info['ws_url']}/audio/ml/stream',
        
        // Voice/JARVIS
        jarvisStatus: '{server_info['base_url']}/voice/jarvis/status',
        jarvisCommand: '{server_info['base_url']}/voice/jarvis/command',
        
        // WebSocket
        mainWebSocket: '{server_info['ws_url']}/ws',
        
        // Auto-config
        autoConfig: '{server_info['base_url']}/auto-config'
    }},
    
    // Feature flags
    features: {json.dumps(server_info['features'], indent=8)},
    
    // Port configuration
    port: {server_info['port']},
    
    // Auto-update function
    updateConfig: async function() {{
        try {{
            const response = await fetch('{server_info['base_url']}/auto-config');
            const config = await response.json();
            console.log('Updated JARVIS config:', config);
            return config;
        }} catch (error) {{
            console.error('Failed to update config:', error);
        }}
    }}
}};

console.log('JARVIS API Configuration loaded:', window.JARVIS_CONFIG);
"""
    
    return Response(
        content=js_config,
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache",
            "Access-Control-Allow-Origin": "*"
        }
    )

@router.options("/")
async def options_handler():
    """Handle OPTIONS requests for CORS preflight"""
    return {
        "status": "ok",
        "cors": "enabled",
        "methods": ["GET", "POST", "OPTIONS"]
    }