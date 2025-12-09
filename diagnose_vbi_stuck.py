#!/usr/bin/env python3
"""
VBI Processing Stuck Diagnostic Tool
=====================================

Diagnoses why voice biometric processing might be getting stuck.
Checks all components in the VBI pipeline.

Run this while JARVIS is running:
    python3 diagnose_vbi_stuck.py
"""

import asyncio
import aiohttp
import json
import sys
import os
from datetime import datetime

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backend'))

BACKEND_URL = "http://localhost:8010"
COLORS = {
    'GREEN': '\033[92m',
    'RED': '\033[91m',
    'YELLOW': '\033[93m',
    'CYAN': '\033[96m',
    'BOLD': '\033[1m',
    'END': '\033[0m'
}


def print_header(text):
    print(f"\n{COLORS['CYAN']}{'='*60}{COLORS['END']}")
    print(f"{COLORS['BOLD']}{COLORS['CYAN']}{text}{COLORS['END']}")
    print(f"{COLORS['CYAN']}{'='*60}{COLORS['END']}\n")


def print_status(component, status, details=""):
    icon = "✅" if status else "❌"
    color = COLORS['GREEN'] if status else COLORS['RED']
    print(f"{icon} {color}{component}{COLORS['END']}: {details}")


async def check_backend_health():
    """Check if backend is responding."""
    print_header("1. Backend Health Check")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BACKEND_URL}/health", timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    print_status("Backend API", True, f"Status: {data.get('status', 'ok')}")
                    return True, data
                else:
                    print_status("Backend API", False, f"HTTP {resp.status}")
                    return False, None
    except Exception as e:
        print_status("Backend API", False, f"Not responding: {e}")
        return False, None


async def check_circuit_breakers():
    """Check circuit breaker states via API."""
    print_header("2. Circuit Breaker Status")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{BACKEND_URL}/api/voice-unlock/circuit-breaker-status",
                timeout=5
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    
                    for name, status in data.items():
                        if isinstance(status, dict):
                            state = status.get('state', 'unknown')
                            is_open = state == 'OPEN' or status.get('is_open', False)
                            failures = status.get('failure_count', 0)
                            
                            if is_open:
                                print_status(name, False, f"CIRCUIT OPEN (failures: {failures})")
                            else:
                                print_status(name, True, f"State: {state}")
                        else:
                            print(f"   {name}: {status}")
                    
                    return data
                else:
                    print_status("Circuit Breakers", False, f"API returned {resp.status}")
                    return None
    except aiohttp.ClientError:
        print(f"{COLORS['YELLOW']}⚠️  Circuit breaker API not available{COLORS['END']}")
        return None
    except Exception as e:
        print_status("Circuit Breakers", False, str(e))
        return None


async def check_vbi_status():
    """Check VBI system status."""
    print_header("3. Voice Biometric Intelligence (VBI) Status")
    try:
        async with aiohttp.ClientSession() as session:
            # Try VBI status endpoint
            endpoints = [
                f"{BACKEND_URL}/api/voice-unlock/vbi-status",
                f"{BACKEND_URL}/api/voice-unlock/status",
                f"{BACKEND_URL}/voice/status"
            ]
            
            for endpoint in endpoints:
                try:
                    async with session.get(endpoint, timeout=5) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            print(f"   Endpoint: {endpoint}")
                            
                            # Parse VBI status
                            initialized = data.get('initialized', data.get('vbi_initialized', False))
                            print_status("VBI Initialized", initialized)
                            
                            if 'stats' in data:
                                stats = data['stats']
                                print(f"   Total verifications: {stats.get('total_verifications', 0)}")
                                print(f"   Cache hits: {stats.get('hot_cache_hits', 0)}")
                                print(f"   Early exits: {stats.get('early_exits', 0)}")
                            
                            return data
                except:
                    continue
            
            print(f"{COLORS['YELLOW']}⚠️  No VBI status endpoint available{COLORS['END']}")
            return None
    except Exception as e:
        print_status("VBI Status", False, str(e))
        return None


async def check_ecapa_service():
    """Check ECAPA model service status."""
    print_header("4. ECAPA-TDNN Model Status")
    try:
        async with aiohttp.ClientSession() as session:
            # Check ECAPA status
            async with session.get(f"{BACKEND_URL}/api/ml/status", timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    
                    ecapa_ready = data.get('ecapa_ready', False)
                    print_status("ECAPA Model Loaded", ecapa_ready)
                    
                    if 'ecapa_status' in data:
                        ecapa = data['ecapa_status']
                        print(f"   State: {ecapa.get('state', 'unknown')}")
                        print(f"   Inference count: {ecapa.get('inference_count', 0)}")
                        if ecapa.get('last_error'):
                            print(f"   {COLORS['RED']}Last error: {ecapa['last_error']}{COLORS['END']}")
                    
                    return data
                else:
                    print_status("ECAPA Status", False, f"HTTP {resp.status}")
                    return None
    except aiohttp.ClientError:
        print(f"{COLORS['YELLOW']}⚠️  ML status API not available{COLORS['END']}")
        return None
    except Exception as e:
        print_status("ECAPA Status", False, str(e))
        return None


async def check_websocket_connections():
    """Check WebSocket connectivity."""
    print_header("5. WebSocket Connectivity")
    try:
        async with aiohttp.ClientSession() as session:
            ws_url = f"ws://localhost:8010/ws/unified"
            
            async with session.ws_connect(ws_url, timeout=5) as ws:
                # Send ping
                await ws.send_json({"type": "ping", "timestamp": datetime.now().isoformat()})
                
                # Wait for pong
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=3)
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        if data.get('type') == 'pong':
                            print_status("WebSocket", True, "Connected and responding")
                            return True
                except asyncio.TimeoutError:
                    print_status("WebSocket", False, "No pong received (timeout)")
                    return False
                
    except Exception as e:
        print_status("WebSocket", False, str(e))
        return False


async def check_voice_profiles():
    """Check if voice profiles are loaded."""
    print_header("6. Voice Profiles")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{BACKEND_URL}/api/voice-unlock/profiles",
                timeout=5
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    profiles = data.get('profiles', [])
                    
                    if profiles:
                        print_status("Voice Profiles", True, f"Found {len(profiles)} profile(s)")
                        for profile in profiles:
                            name = profile.get('name', 'Unknown')
                            embeddings = profile.get('embedding_count', 0)
                            print(f"   • {name}: {embeddings} embeddings")
                    else:
                        print_status("Voice Profiles", False, "No profiles found!")
                        print(f"   {COLORS['YELLOW']}You need to enroll a voice profile first{COLORS['END']}")
                    
                    return profiles
                else:
                    # Try alternate endpoint
                    async with session.get(
                        f"{BACKEND_URL}/api/voice-unlock/enrollment/status",
                        timeout=5
                    ) as resp2:
                        if resp2.status == 200:
                            data = await resp2.json()
                            print(f"   Enrollment status: {json.dumps(data, indent=2)}")
                            return data
                    
                    print_status("Voice Profiles", False, f"HTTP {resp.status}")
                    return None
    except aiohttp.ClientError:
        print(f"{COLORS['YELLOW']}⚠️  Voice profiles API not available{COLORS['END']}")
        return None
    except Exception as e:
        print_status("Voice Profiles", False, str(e))
        return None


async def check_transcription_service():
    """Check if transcription/STT is working."""
    print_header("7. Transcription Service (STT)")
    try:
        async with aiohttp.ClientSession() as session:
            endpoints = [
                f"{BACKEND_URL}/api/stt/status",
                f"{BACKEND_URL}/voice/stt-status",
                f"{BACKEND_URL}/api/voice/stt-status"
            ]
            
            for endpoint in endpoints:
                try:
                    async with session.get(endpoint, timeout=5) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            print(f"   Endpoint: {endpoint}")
                            print_status("STT Service", True, json.dumps(data, indent=2)[:200])
                            return data
                except:
                    continue
            
            print(f"{COLORS['YELLOW']}⚠️  STT status endpoint not available{COLORS['END']}")
            return None
    except Exception as e:
        print_status("STT Service", False, str(e))
        return None


async def check_memory_pressure():
    """Check system memory pressure."""
    print_header("8. System Resources")
    try:
        import psutil
        
        memory = psutil.virtual_memory()
        available_gb = memory.available / (1024**3)
        used_percent = memory.percent
        
        status = available_gb >= 2.0
        print_status(
            "Memory",
            status,
            f"{available_gb:.1f}GB available ({used_percent:.1f}% used)"
        )
        
        if not status:
            print(f"   {COLORS['RED']}⚠️  Low memory may cause processing issues{COLORS['END']}")
        
        # Check CPU
        cpu_percent = psutil.cpu_percent(interval=1)
        print(f"   CPU Usage: {cpu_percent}%")
        
        return {"memory_gb": available_gb, "memory_percent": used_percent, "cpu_percent": cpu_percent}
        
    except ImportError:
        print(f"{COLORS['YELLOW']}⚠️  psutil not available for resource check{COLORS['END']}")
        return None
    except Exception as e:
        print_status("Resources", False, str(e))
        return None


async def provide_diagnosis(results):
    """Provide diagnosis based on check results."""
    print_header("📋 DIAGNOSIS")
    
    issues = []
    fixes = []
    
    # Check each result
    if not results.get('backend_healthy'):
        issues.append("Backend is not responding")
        fixes.append("Start JARVIS: ./start_jarvis.sh")
    
    cb_status = results.get('circuit_breakers', {})
    for name, status in (cb_status or {}).items():
        if isinstance(status, dict):
            if status.get('state') == 'OPEN' or status.get('is_open'):
                issues.append(f"Circuit breaker OPEN: {name}")
                fixes.append(f"Reset circuit breaker: curl -X POST http://localhost:8010/api/voice-unlock/reset-circuit-breakers")
    
    if not results.get('vbi_initialized'):
        issues.append("VBI not initialized")
        fixes.append("Restart JARVIS: python3 start_system.py --restart")
    
    if not results.get('voice_profiles'):
        issues.append("No voice profiles enrolled")
        fixes.append("Enroll your voice profile in the JARVIS settings")
    
    if not results.get('websocket_connected'):
        issues.append("WebSocket not connected")
        fixes.append("Refresh the frontend or restart JARVIS")
    
    resources = results.get('resources', {})
    if resources and resources.get('memory_gb', 10) < 2.0:
        issues.append(f"Low memory: {resources.get('memory_gb', 0):.1f}GB available")
        fixes.append("Close other applications to free memory")
    
    if issues:
        print(f"{COLORS['RED']}Issues Found:{COLORS['END']}")
        for i, issue in enumerate(issues, 1):
            print(f"  {i}. {issue}")
        
        print(f"\n{COLORS['GREEN']}Suggested Fixes:{COLORS['END']}")
        for i, fix in enumerate(fixes, 1):
            print(f"  {i}. {fix}")
    else:
        print(f"{COLORS['GREEN']}✅ All systems appear healthy!{COLORS['END']}")
        print("\nIf still experiencing issues:")
        print("  1. Check the terminal running JARVIS for error messages")
        print("  2. Try saying 'Hey JARVIS' to trigger wake word detection")
        print("  3. Look at the browser console (F12) for frontend errors")


async def main():
    print(f"\n{COLORS['BOLD']}🔍 VBI Processing Diagnostic Tool{COLORS['END']}")
    print(f"Time: {datetime.now().isoformat()}")
    
    results = {}
    
    # Run all checks
    backend_ok, health_data = await check_backend_health()
    results['backend_healthy'] = backend_ok
    
    if backend_ok:
        results['circuit_breakers'] = await check_circuit_breakers()
        
        vbi_data = await check_vbi_status()
        results['vbi_initialized'] = vbi_data.get('initialized', False) if vbi_data else False
        
        await check_ecapa_service()
        
        results['websocket_connected'] = await check_websocket_connections()
        
        profiles = await check_voice_profiles()
        results['voice_profiles'] = profiles
        
        await check_transcription_service()
    
    results['resources'] = await check_memory_pressure()
    
    # Provide diagnosis
    await provide_diagnosis(results)
    
    print(f"\n{COLORS['CYAN']}{'='*60}{COLORS['END']}")


if __name__ == "__main__":
    asyncio.run(main())
