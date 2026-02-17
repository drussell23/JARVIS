#!/usr/bin/env python3
"""
Voice Demo: JARVIS Lock/Unlock with Speech
==========================================

Demonstrates JARVIS speaking the lock detection feedback
"""

import asyncio
import subprocess
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp
from api.simple_context_handler_enhanced import wrap_with_enhanced_context
from api.unified_command_processor import UnifiedCommandProcessor

class VoiceWebSocket:
    """WebSocket that speaks messages"""
    def __init__(self):
        self.messages = []
        self.spoken_count = 0
        
    async def send_json(self, data):
        self.messages.append(data)
        
        # Handle response messages
        if data.get('type') == 'response':
            text = data.get('text', data.get('message'))
            speak = data.get('speak', False)
            
            if text:
                print(f"\n💬 JARVIS: {text}")
                
                if speak:
                    print("   🔊 [SPEAKING NOW]")
                    await self._speak_text(text)
                    self.spoken_count += 1
    
    async def _speak_text(self, text):
        """Call JARVIS TTS API"""
        try:
            async with aiohttp.ClientSession() as session:
                url = "http://localhost:8888/api/jarvis/speak"
                payload = {"text": text}
                
                async with session.post(url, json=payload) as response:
                    if response.status == 200:
                        # Wait for speech to complete (estimate)
                        wait_time = min(len(text) * 0.05, 5.0)  # ~0.05s per char, max 5s
                        await asyncio.sleep(wait_time)
                    else:
                        print(f"   ⚠️  TTS API error: {response.status}")
        except Exception as e:
            print(f"   ⚠️  Could not speak: {e}")

async def run_voice_demo():
    """Run the voice demonstration"""
    
    print("\n" + "="*70)
    print("🎤 JARVIS VOICE FEEDBACK DEMO")
    print("="*70)
    
    print("\n📢 This demo shows JARVIS speaking the lock detection feedback")
    print("   Make sure your speakers are on!")
    
    print("\n⏳ Starting in 3 seconds...")
    for i in range(3, 0, -1):
        print(f"   {i}...")
        await asyncio.sleep(1)
    
    # Step 1: Lock screen
    print("\n" + "-"*50)
    print("📍 Step 1: Locking your screen")
    print("-"*50)
    
    lock_cmd = """osascript -e 'tell app "System Events" to key code 12 using {control down, command down}'"""
    subprocess.run(lock_cmd, shell=True)
    
    print("✅ Screen locked")
    print("⏳ Waiting 3 seconds...")
    await asyncio.sleep(3)
    
    # Step 2: Create JARVIS
    print("\n" + "-"*50)
    print("📍 Step 2: Setting up JARVIS")
    print("-"*50)
    
    processor = UnifiedCommandProcessor()
    handler = wrap_with_enhanced_context(processor)
    websocket = VoiceWebSocket()
    
    print("✅ JARVIS ready with voice feedback")
    
    # Step 3: Send command
    print("\n" + "-"*50)
    print("📍 Step 3: Sending command")
    print("-"*50)
    
    command = "open Safari and search for dogs"
    print(f"🎤 Command: '{command}'")
    print("\n👂 LISTEN FOR JARVIS TO SPEAK...")
    
    # Process command
    try:
        result = await handler.process_with_context(command, websocket)
        
        # Wait a moment for any final speech
        await asyncio.sleep(2)
        
        print("\n" + "-"*50)
        print("📊 RESULTS")
        print("-"*50)
        
        print(f"\n✅ Command completed")
        print(f"🔊 Messages spoken: {websocket.spoken_count}")
        
        if websocket.spoken_count > 0:
            print("\n🎉 SUCCESS! JARVIS spoke the feedback messages!")
            
            # Show what was spoken
            print("\n📝 Spoken messages:")
            for msg in websocket.messages:
                if msg.get('type') == 'response' and msg.get('speak'):
                    text = msg.get('text', msg.get('message'))
                    if text:
                        print(f"   • '{text}'")
        else:
            print("\n⚠️  No messages were spoken")
            
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()

async def quick_voice_test():
    """Quick test of the TTS system"""
    print("\n" + "-"*50)
    print("🔊 Testing Text-to-Speech")
    print("-"*50)
    
    test_message = "Hello, this is JARVIS. Your voice feedback system is working correctly."
    print(f"\nTest message: '{test_message}'")
    print("Speaking...")
    
    try:
        async with aiohttp.ClientSession() as session:
            url = "http://localhost:8888/api/jarvis/speak"
            payload = {"text": test_message}
            
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    print("✅ TTS working!")
                    await asyncio.sleep(3)  # Let it speak
                else:
                    print(f"❌ TTS error: {response.status}")
    except Exception as e:
        print(f"❌ Could not test TTS: {e}")

if __name__ == "__main__":
    print("🚀 JARVIS Voice Lock/Unlock Demo")
    print("\nThis demo will:")
    print("  1. Lock your screen")
    print("  2. Have JARVIS speak the lock detection message")
    print("  3. Unlock and execute the command")
    
    print("\n⚠️  Requirements:")
    print("  • JARVIS must be running (python main.py)")
    print("  • Your speakers must be on")
    print("  • Your password must be stored")
    
    try:
        # Quick TTS test
        asyncio.run(quick_voice_test())
        
        print("\n" + "="*50)
        input("Press Enter to run the full demo or Ctrl+C to exit...")
        
        # Run main demo
        asyncio.run(run_voice_demo())
        
    except KeyboardInterrupt:
        print("\n\n❌ Demo cancelled")