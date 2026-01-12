#!/usr/bin/env python3
"""
Dynamic CORS Handler
Automatically detects and configures CORS based on incoming requests
Handles port mismatches dynamically
"""

from fastapi import Request, Response
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Set, Optional
import logging
import re
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

class DynamicCORSMiddleware:
    """
    Dynamic CORS middleware that automatically configures allowed origins
    based on incoming requests and detected patterns
    """
    
    def __init__(self, app):
        self.app = app
        self.known_origins: Set[str] = set()
        self.port_mappings = {}  # Track which ports clients are using
        self.auto_allowed_patterns = [
            r"^https?://localhost(:\d+)?$",
            r"^https?://127\.0\.0\.1(:\d+)?$",
            r"^https?://0\.0\.0\.0(:\d+)?$",
            r"^https?://\[::1\](:\d+)?$",  # IPv6 localhost
        ]
        
        # Initialize with common development origins
        self.known_origins.update([
            "http://localhost:3000",
            "http://localhost:3001", 
            "http://localhost:8000",
            "http://localhost:8010",
            "http://localhost:8080",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:8000",
            "http://127.0.0.1:8010",
        ])
        
    def is_origin_allowed(self, origin: str) -> bool:
        """Check if origin should be automatically allowed"""
        if not origin:
            return True
            
        # Check known origins
        if origin in self.known_origins:
            return True
            
        # Check patterns (localhost, etc)
        for pattern in self.auto_allowed_patterns:
            if re.match(pattern, origin):
                return True
                
        # Check if it's a websocket upgrade from known origin
        if origin.startswith("ws://") or origin.startswith("wss://"):
            http_origin = origin.replace("ws://", "http://").replace("wss://", "https://")
            if http_origin in self.known_origins:
                return True
                
        return False
    
    def add_origin(self, origin: str):
        """Add new origin to allowed list"""
        if origin and self.is_origin_allowed(origin):
            self.known_origins.add(origin)
            # Also add websocket variant
            if origin.startswith("http://"):
                self.known_origins.add(origin.replace("http://", "ws://"))
            elif origin.startswith("https://"):
                self.known_origins.add(origin.replace("https://", "wss://"))
            logger.info(f"Added origin to CORS: {origin}")
    
    def detect_port_mismatch(self, request: Request) -> Optional[int]:
        """Detect if client is expecting different port"""
        # Check referer header
        referer = request.headers.get("referer", "")
        if referer:
            try:
                parsed = urlparse(referer)
                if parsed.port:
                    return parsed.port
            except Exception:
                pass

        # Check origin header
        origin = request.headers.get("origin", "")
        if origin:
            try:
                parsed = urlparse(origin)
                if parsed.port:
                    return parsed.port
            except Exception:
                pass
                
        return None
    
    async def __call__(self, request: Request, call_next):
        """Process request and dynamically update CORS"""
        origin = request.headers.get("origin", "")
        
        # Auto-detect and allow valid origins
        if origin:
            self.add_origin(origin)
            
            # Detect port expectations
            expected_port = self.detect_port_mismatch(request)
            if expected_port:
                client_id = f"{request.client.host if request.client else 'unknown'}"
                self.port_mappings[client_id] = expected_port
                
        # Handle OPTIONS preflight
        if request.method == "OPTIONS":
            response = Response(status_code=200)
            if origin and self.is_origin_allowed(origin):
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Access-Control-Allow-Credentials"] = "true"
                response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS, PATCH, HEAD"
                response.headers["Access-Control-Allow-Headers"] = "*"
                response.headers["Access-Control-Max-Age"] = "3600"
            return response
                
        # Process request
        response = await call_next(request)
        
        # Add CORS headers if origin is allowed
        if origin and self.is_origin_allowed(origin):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS, PATCH, HEAD"
            response.headers["Access-Control-Allow-Headers"] = "*"
            response.headers["Access-Control-Expose-Headers"] = "*"
            
        # Add server info headers for auto-discovery
        import os
        actual_port = os.getenv('BACKEND_PORT', '8000')
        response.headers["X-API-Port"] = actual_port
        response.headers["X-API-Base-URL"] = f"http://localhost:{actual_port}"
        response.headers["X-API-WebSocket-URL"] = f"ws://localhost:{actual_port}/ws"
        response.headers["X-API-Version"] = "2.0"
        
        # If port mismatch detected, add warning header
        if origin:
            try:
                from urllib.parse import urlparse
                parsed = urlparse(origin)
                if parsed.port and str(parsed.port) != actual_port:
                    response.headers["X-Port-Mismatch-Warning"] = f"Client expects port {parsed.port}, API is on port {actual_port}"
                    response.headers["X-Correct-Base-URL"] = f"http://localhost:{actual_port}"
            except Exception:
                pass

        return response


def create_dynamic_cors_middleware(app):
    """
    Create and configure dynamic CORS middleware
    This replaces the static CORS configuration
    """
    import os
    
    # Get additional allowed origins from environment
    env_origins = os.getenv('CORS_ORIGINS', '').split(',')
    env_origins = [o.strip() for o in env_origins if o.strip()]
    
    # Create dynamic middleware
    dynamic_cors = DynamicCORSMiddleware(app)
    
    # Add environment origins
    for origin in env_origins:
        dynamic_cors.add_origin(origin)
    
    # Log configuration
    logger.info("Dynamic CORS Handler initialized")
    logger.info(f"Pre-configured origins: {len(dynamic_cors.known_origins)}")
    logger.info(f"Auto-allow patterns: {len(dynamic_cors.auto_allowed_patterns)}")
    
    return dynamic_cors


# Port detection and redirection helpers
class PortMismatchHandler:
    """Handle port mismatches by providing redirection information"""
    
    @staticmethod
    def get_redirect_info(request: Request, actual_port: int) -> dict:
        """Get information for redirecting to correct port"""
        origin = request.headers.get("origin", "")
        if origin:
            parsed = urlparse(origin)
            expected_port = parsed.port or 80
            
            if expected_port != actual_port:
                return {
                    "error": "port_mismatch",
                    "message": f"This API is running on port {actual_port}, but your client is configured for port {expected_port}",
                    "correct_url": f"{parsed.scheme}://{parsed.hostname}:{actual_port}",
                    "suggestion": f"Please update your frontend configuration to use port {actual_port}",
                    "auto_redirect_available": True,
                    "redirect_endpoint": f"/api/redirect?to={actual_port}"
                }
        return {}
    
    @staticmethod
    async def create_port_redirect_endpoint(app, request: Request, to: int):
        """Create redirect response to correct port"""
        origin = request.headers.get("origin", "")
        if origin:
            parsed = urlparse(origin)
            new_url = f"{parsed.scheme}://{parsed.hostname}:{to}{request.url.path}"
            if request.url.query:
                new_url += f"?{request.url.query}"
                
            return {
                "redirect": True,
                "new_url": new_url,
                "original_port": parsed.port or 80,
                "correct_port": to,
                "headers": {
                    "Location": new_url,
                    "Access-Control-Allow-Origin": origin
                }
            }
        return {"error": "Cannot determine redirect URL"}


# Automatic port discovery
class AutoPortDiscovery:
    """Automatically discover which ports services are running on"""
    
    @staticmethod
    async def discover_services():
        """Discover running services and their ports"""
        import psutil
        import socket
        
        services = {}
        
        # Common service ports to check
        common_ports = {
            3000: "frontend",
            3001: "frontend_alt", 
            8000: "backend",
            8010: "backend_alt",
            8080: "proxy",
            5000: "api",
            5173: "vite",
            4200: "angular"
        }
        
        for port, service_name in common_ports.items():
            try:
                # Check if port is in use
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(0.1)
                result = sock.connect_ex(('localhost', port))
                sock.close()
                
                if result == 0:  # Port is open
                    services[service_name] = {
                        "port": port,
                        "url": f"http://localhost:{port}",
                        "status": "active"
                    }
            except Exception:
                pass

        return services
    
    @staticmethod
    def get_recommended_config(services: dict) -> dict:
        """Get recommended configuration based on discovered services"""
        config = {
            "frontend_url": None,
            "backend_url": None,
            "recommended_cors_origins": []
        }
        
        # Find frontend
        for service_name, info in services.items():
            if "frontend" in service_name:
                config["frontend_url"] = info["url"]
                config["recommended_cors_origins"].append(info["url"])
                
        # Find backend
        for service_name, info in services.items():
            if "backend" in service_name or "api" in service_name:
                config["backend_url"] = info["url"]
                
        return config