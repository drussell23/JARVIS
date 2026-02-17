#!/usr/bin/env python3
"""
Automated Voice Demo - No Input Required
========================================
"""

import asyncio
import subprocess
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp
from api.simple_context_handler_enhanced import wrap_with_enhanced_context
from api.unified_command_processor import UnifiedCommandProcessor

class VoiceWebSocket:
    """WebSocket with voice feedback"""
    def __init__(self):
        self.messages = []
        
    async def send_json(self, data):
        self.messages.append(data)
        
        if data.get('type') == 'response':
            text = data.get('text', '')
            speak = data.get('speak', False)
            
            if text:
                print(f"\n💬 JARVIS: {text}")
                
                if speak:
                    print("   🔊 [SPEAKING...]")
                    await self._speak(text)
    
    async def _speak(self, text):
        """Make JARVIS speak"""
        try:
            async with aiohttp.ClientSession() as session:
                url = "http://localhost:8888/api/jarvis/speak"
                payload = {"text": text}
                
                async with session.post(url, json=payload) as response:
                    if response.status == 200:
                        # Wait for speech
                        wait_time = min(len(text) * 0.04, 4.0)
                        await asyncio.sleep(wait_time)
        except Exception as e:
            print(f"   ⚠️  Speech error: {e}")

async def run_demo():
    """Run the automated demo"""
    
    print("\n" + "="*70)
    print("🎤 AUTOMATED VOICE LOCK/UNLOCK DEMO")
    print("="*70)
    
    print("\n📢 This demo will:")
    print("   1. Lock your screen")
    print("   2. JARVIS will speak when detecting the lock")
    print("   3. Unlock and execute a command")
    
    print("\n🔊 Make sure your speakers are on!")
    print("\n⏳ Starting in 5 seconds...")
    
    for i in range(5, 0, -1):
        print(f"   {i}...", end='\r')
        await asyncio.sleep(1)
    
    # Lock screen
    print("\n\n🔒 Locking screen...")
    lock_cmd = """osascript -e 'tell app "System Events" to key code 12 using {control down, command down}'"""
    subprocess.run(lock_cmd, shell=True)
    
    print("   Waiting for lock...")
    await asyncio.sleep(3)
    
    # Set up JARVIS
    print("\n🤖 Setting up JARVIS...")
    processor = UnifiedCommandProcessor()
    handler = wrap_with_enhanced_context(processor)
    websocket = VoiceWebSocket()
    
    # Send command
    command = "open Safari and search for dogs"
    print(f"\n🎯 Sending command: '{command}'")
    print("\n👂 LISTEN FOR JARVIS TO SPEAK...")
    
    # Process
    try:
        result = await handler.process_with_context(command, websocket)
        
        await asyncio.sleep(2)  # Let any final speech complete
        
        print("\n" + "="*50)
        print("✅ Demo completed!")
        
        # Show what was spoken
        spoken_msgs = [m for m in websocket.messages 
                      if m.get('type') == 'response' and m.get('speak')]
        
        if spoken_msgs:
            print(f"\n🔊 JARVIS spoke {len(spoken_msgs)} message(s):")
            for msg in spoken_msgs:
                print(f"   • \"{msg.get('text')}\"")
        
        # Check if Safari opened
        await asyncio.sleep(2)
        ps_check = subprocess.run(
            ["ps", "aux"], 
            capture_output=True, text=True
        )
        safari_open = "Safari" in ps_check.stdout and "search" in ps_check.stdout
        
        print(f"\n🌐 Safari opened: {'Yes' if safari_open else 'Check manually'}")
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    print("🚀 JARVIS Automated Voice Demo")
    
    # Check services
    ws_check = subprocess.run(["lsof", "-i:8765"], capture_output=True)
    jarvis_check = subprocess.run(["lsof", "-i:8888"], capture_output=True)
    
    if ws_check.returncode != 0:
        print("\n❌ Voice unlock WebSocket not running!")
        print("   Start it with:")
        print("   cd voice_unlock/objc/server && python websocket_server.py &")
        sys.exit(1)
    
    if jarvis_check.returncode != 0:
        print("\n❌ JARVIS not running!")
        print("   Start it with: python main.py")
        sys.exit(1)
    
    print("✅ All services running")
    
    asyncio.run(run_demo())