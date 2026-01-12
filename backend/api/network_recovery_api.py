"""
Network Recovery API - Advanced network error recovery endpoints
Provides intelligent recovery strategies for network failures
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
import asyncio
import logging
import json
import os
import subprocess
import platform
from datetime import datetime
import random

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/network", tags=["Network Recovery"])

class NetworkDiagnosisRequest(BaseModel):
    error: str
    timestamp: int
    userAgent: str

class ConnectionHealthRequest(BaseModel):
    error: str
    connectionHealth: Dict[str, Any]
    recoveryAttempts: int
    browserInfo: Dict[str, str]

class RecoverySuccessLog(BaseModel):
    strategy: str
    result: Dict[str, Any]
    connectionHealth: Dict[str, Any]
    timestamp: int

class NetworkRecoveryAPI:
    """Advanced network recovery system"""
    
    def __init__(self):
        self.recovery_history = []
        self.successful_strategies = {}
        self.network_status = {
            "dns_servers": ["8.8.8.8", "1.1.1.1", "208.67.222.222"],
            "test_endpoints": [
                "https://www.google.com/generate_204",
                "https://connectivity-check.ubuntu.com/",
                "https://www.cloudflare.com/"
            ]
        }
        
    async def diagnose_network(self, request: NetworkDiagnosisRequest) -> Dict:
        """Diagnose network issues and attempt recovery"""
        logger.info(f"Network diagnosis requested for error: {request.error}")
        
        diagnosis = {
            "recovered": False,
            "issue": "unknown",
            "suggestions": [],
            "timestamp": datetime.now().isoformat()
        }
        
        # Check system network status
        if platform.system() == "Darwin":  # macOS
            try:
                # Check network connectivity
                result = subprocess.run(
                    ["ping", "-c", "1", "-t", "2", "8.8.8.8"],
                    capture_output=True,
                    text=True
                )
                
                if result.returncode != 0:
                    diagnosis["issue"] = "no_internet"
                    diagnosis["suggestions"].append("Check network connection")
                    
                    # Try to reset network
                    subprocess.run(["sudo", "dscacheutil", "-flushcache"], capture_output=True)
                    subprocess.run(["sudo", "killall", "-HUP", "mDNSResponder"], capture_output=True)
                    
                    # Check again
                    result = subprocess.run(
                        ["ping", "-c", "1", "-t", "2", "8.8.8.8"],
                        capture_output=True,
                        text=True
                    )
                    
                    if result.returncode == 0:
                        diagnosis["recovered"] = True
                        diagnosis["issue"] = "dns_cache"
                else:
                    # Network is fine, might be service issue
                    diagnosis["issue"] = "service_issue"
                    diagnosis["suggestions"].append("Try alternative speech service")
                    
            except Exception as e:
                logger.error(f"Network diagnosis error: {e}")
                
        return diagnosis
    
    async def get_advanced_recovery_strategy(self, request: ConnectionHealthRequest) -> Dict:
        """Provide advanced recovery strategy based on ML analysis"""
        logger.info(f"Advanced recovery requested, attempts: {request.recoveryAttempts}")
        
        # Analyze browser and error pattern
        browser = request.browserInfo.get("userAgent", "").lower()
        is_chrome = "chrome" in browser and "edg" not in browser
        is_safari = "safari" in browser and "chrome" not in browser
        
        strategy = {
            "customScript": None,
            "proxyEndpoint": None,
            "recommendations": [],
            "alternativeServices": []
        }
        
        # Check recovery history for successful strategies
        if request.error in self.successful_strategies:
            prev_strategy = self.successful_strategies[request.error]
            logger.info(f"Using previously successful strategy: {prev_strategy}")
            return prev_strategy
        
        # Chrome-specific recovery
        if is_chrome and request.recoveryAttempts > 2:
            strategy["customScript"] = """
                // Chrome-specific recovery
                recognition.stop();
                
                // Clear Chrome's speech cache
                if ('speechSynthesis' in window) {
                    window.speechSynthesis.cancel();
                }
                
                // Wait for Chrome to reset
                await new Promise(resolve => setTimeout(resolve, 3000));
                
                // Create fresh instance with different settings
                const newRecognition = new (window.webkitSpeechRecognition)();
                newRecognition.continuous = false;  // Start with non-continuous
                newRecognition.interimResults = false;
                newRecognition.maxAlternatives = 1;
                
                // Test with simple recognition first
                return new Promise((resolve) => {
                    newRecognition.onstart = () => {
                        newRecognition.stop();
                        
                        // If test works, create full instance
                        setTimeout(() => {
                            const finalRecognition = new (window.webkitSpeechRecognition)();
                            finalRecognition.continuous = true;
                            finalRecognition.interimResults = true;
                            resolve({ 
                                success: true, 
                                newRecognition: finalRecognition,
                                message: 'Chrome speech service recovered'
                            });
                        }, 500);
                    };
                    
                    newRecognition.onerror = () => {
                        resolve({ success: false });
                    };
                    
                    try {
                        newRecognition.start();
                    } catch (e) {
                        resolve({ success: false });
                    }
                });
            """
        
        # Safari-specific recovery
        elif is_safari:
            strategy["recommendations"].append("Enable Develop menu and check console")
            strategy["alternativeServices"].append({
                "name": "Safari Experimental Features",
                "action": "Enable SpeechRecognition in Develop > Experimental Features"
            })
        
        # Proxy endpoint for severe cases
        if request.recoveryAttempts >= 4:
            # Enable proxy mode through our backend
            strategy["proxyEndpoint"] = f"{os.getenv('BACKEND_URL', 'http://localhost:8000')}/audio/proxy/stream"
            strategy["recommendations"].append("Using backend proxy for speech processing")
            
            # Also provide recovery script for proxy mode
            strategy["customScript"] = """
                // Switch to proxy mode
                const proxyMode = {
                    active: true,
                    endpoint: context.proxyEndpoint,
                    ws: null
                };
                
                // Create WebSocket for audio streaming
                proxyMode.ws = new WebSocket(proxyMode.endpoint.replace('http', 'ws'));
                
                proxyMode.ws.onopen = () => {
                    console.log('Proxy mode activated');
                };
                
                return {
                    success: true,
                    useProxy: true,
                    proxyEndpoint: proxyMode.endpoint,
                    proxyWebSocket: proxyMode.ws,
                    message: 'Switched to backend proxy for speech processing'
                };
            """
        
        return strategy
    
    async def log_recovery_success(self, log: RecoverySuccessLog) -> Dict:
        """Log successful recovery strategy for future use"""
        logger.info(f"Logging successful recovery: {log.strategy}")
        
        # Store in history
        self.recovery_history.append({
            "strategy": log.strategy,
            "timestamp": log.timestamp,
            "result": log.result
        })
        
        # Learn from success
        error_key = log.connectionHealth.get("lastError", "network")
        if error_key not in self.successful_strategies:
            self.successful_strategies[error_key] = {
                "strategy": log.strategy,
                "result": log.result,
                "success_count": 1
            }
        else:
            self.successful_strategies[error_key]["success_count"] += 1
        
        return {
            "logged": True,
            "message": f"Recovery strategy '{log.strategy}' logged successfully"
        }
    
    async def test_connectivity(self) -> Dict:
        """Test various connectivity endpoints"""
        results = {
            "overall": False,
            "endpoints": {},
            "dns_servers": {},
            "latency": {}
        }
        
        # Test endpoints
        for endpoint in self.network_status["test_endpoints"]:
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    start = datetime.now()
                    async with session.get(endpoint, timeout=5) as response:
                        latency = (datetime.now() - start).total_seconds() * 1000
                        results["endpoints"][endpoint] = response.status == 200
                        results["latency"][endpoint] = latency
                        if response.status == 200:
                            results["overall"] = True
            except Exception:
                results["endpoints"][endpoint] = False
                results["latency"][endpoint] = -1
        
        return results

# Create API instance
recovery_api = NetworkRecoveryAPI()

# Network diagnosis endpoint
@router.post("/diagnose")
async def diagnose_network(request: NetworkDiagnosisRequest):
    """Diagnose network issues and attempt recovery"""
    return await recovery_api.diagnose_network(request)

# Advanced recovery strategy endpoint
@router.post("/ml/advanced-recovery")
async def get_advanced_recovery(request: ConnectionHealthRequest):
    """Get ML-powered recovery strategy"""
    return await recovery_api.get_advanced_recovery_strategy(request)

# Log successful recovery
@router.post("/ml/recovery-success")
async def log_recovery_success(log: RecoverySuccessLog):
    """Log successful recovery for learning"""
    return await recovery_api.log_recovery_success(log)

# Test connectivity
@router.get("/test")
async def test_connectivity():
    """Test network connectivity to various endpoints"""
    return await recovery_api.test_connectivity()

# Audio proxy endpoint for fallback
@router.websocket("/proxy/stream")
async def audio_proxy_stream(websocket):
    """WebSocket endpoint for proxied audio streaming"""
    await websocket.accept()
    
    try:
        # This would implement audio proxying through the backend
        # For now, just acknowledge connection
        await websocket.send_json({
            "type": "connected",
            "message": "Audio proxy stream connected"
        })
        
        # WebSocket idle timeout protection
        idle_timeout = float(os.getenv("TIMEOUT_WEBSOCKET_IDLE", "300.0"))  # 5 min default

        while True:
            try:
                data = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=idle_timeout
                )
            except asyncio.TimeoutError:
                logger.info("Audio proxy WebSocket idle timeout, closing connection")
                break

            if data.get("type") == "audio":
                # Process audio through backend speech recognition
                # This would use server-side speech recognition
                audio_data = data.get("data")  # Base64 encoded
                
                # Simulate processing
                await asyncio.sleep(0.1)
                
                # Send back transcription
                await websocket.send_json({
                    "type": "transcription",
                    "text": "Processed through proxy",
                    "confidence": 0.95
                })
                
    except Exception as e:
        logger.error(f"Audio proxy error: {e}")
    finally:
        await websocket.close()