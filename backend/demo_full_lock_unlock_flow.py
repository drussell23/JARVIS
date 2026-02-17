#!/usr/bin/env python3
"""
Full Demo: JARVIS Lock/Unlock Flow with Safari Search
=====================================================

Demonstrates the complete flow:
1. Lock screen
2. Say "JARVIS, open Safari and search for dogs" 
3. JARVIS detects lock, announces intent, unlocks, executes command
4. Safari opens with search results
"""

import asyncio
import json
import subprocess
import time
from datetime import datetime
import sys

# Add backend to path if needed
sys.path.insert(0, '/Users/derekjrussell/Documents/repos/JARVIS-AI-Agent/backend')

# Check for aiohttp (needed for voice)
try:
    import aiohttp
    VOICE_AVAILABLE = True
except ImportError:
    VOICE_AVAILABLE = False
    print("⚠️  aiohttp not installed - voice feedback will be disabled")
    print("   Install with: pip install aiohttp")

from api.simple_context_handler_enhanced import wrap_with_enhanced_context
from api.unified_command_processor import UnifiedCommandProcessor

class DemoWebSocket:
    """WebSocket that displays real-time feedback with voice"""
    def __init__(self, speak_enabled=True):
        self.messages = []
        self.speak_enabled = speak_enabled
        
    async def send_json(self, data):
        self.messages.append(data)
        
        # Display real-time feedback
        if data.get('type') == 'context_update':
            print(f"\n💬 JARVIS: {data.get('message')}")
            if data.get('status'):
                print(f"   Status: {data.get('status')}")
        elif data.get('type') == 'response':
            text = data.get('text', data.get('message'))
            print(f"\n📢 JARVIS: {text}")
            
            # Speak the message if enabled
            if self.speak_enabled and text and data.get('speak', False):
                await self._speak(text)
    
    async def _speak(self, text):
        """Make JARVIS speak using the TTS API"""
        if not VOICE_AVAILABLE:
            return
            
        try:
            async with aiohttp.ClientSession() as session:
                # Call JARVIS speak endpoint
                url = f"http://localhost:8888/api/jarvis/speak"
                payload = {"text": text}
                
                async with session.post(url, json=payload) as response:
                    if response.status == 200:
                        print(f"   🔊 Speaking: '{text[:50]}...'")
                        # Add a small delay to let speech start
                        await asyncio.sleep(0.5)
                    else:
                        print(f"   ⚠️  Could not speak (API status: {response.status})")
        except Exception as e:
            print(f"   ⚠️  Speech error: {e}")

def print_section(title):
    """Print a formatted section header"""
    print(f"\n{'='*80}")
    print(f"✨ {title}")
    print('='*80)

async def demo_complete_flow():
    """Demonstrate the complete lock/unlock/execute flow"""
    
    print_section("JARVIS Lock/Unlock Demo - Full Flow")
    
    print("\n📋 Scenario:")
    print("   1. Your Mac screen will be locked")
    print("   2. You say: 'JARVIS, open Safari and search for dogs'")
    print("   3. JARVIS will:")
    print("      - Detect the lock")
    print("      - Announce what it's doing")
    print("      - Unlock your screen") 
    print("      - Open Safari")
    print("      - Search for dogs")
    
    print("\n⚠️  This demo will:")
    print("   - Lock your screen")
    print("   - Unlock it automatically")
    print("   - Open Safari browser")
    
    print("\n🔐 Make sure your password is stored (run enable_screen_unlock.sh if not)")
    
    # Ask about voice
    enable_voice = False
    if VOICE_AVAILABLE:
        print("\n🔊 Enable voice feedback? (y/n): ", end='')
        try:
            voice_choice = input().strip().lower()
            enable_voice = voice_choice in ['y', 'yes', '']
            print(f"   Voice feedback: {'ENABLED 🔊' if enable_voice else 'DISABLED 🔇'}")
        except KeyboardInterrupt:
            print("\n❌ Demo cancelled")
            return
    else:
        print("\n🔇 Voice feedback disabled (aiohttp not available)")
    
    print("\n▶️  Press Enter to start the demo, or Ctrl+C to cancel...")
    try:
        input()
    except KeyboardInterrupt:
        print("\n❌ Demo cancelled")
        return
    
    # Step 1: Lock the screen
    print_section("Step 1: Locking Screen")
    print("🔒 Locking screen in 3 seconds...")
    for i in range(3, 0, -1):
        print(f"   {i}...")
        await asyncio.sleep(1)

    # Lock command
    lock_cmd = """osascript -e 'tell app "System Events" to key code 12 using {control down, command down}'"""
    subprocess.run(lock_cmd, shell=True)
    
    print("   ✅ Screen locked")
    print("   ⏳ Waiting 3 seconds for lock to complete...")
    await asyncio.sleep(3)
    
    # Step 2: Set up JARVIS
    print_section("Step 2: Initializing JARVIS")
    print("🤖 Setting up JARVIS with enhanced context awareness...")
    
    processor = UnifiedCommandProcessor()
    context_handler = wrap_with_enhanced_context(processor)
    websocket = DemoWebSocket(speak_enabled=enable_voice)
    
    print("   ✅ JARVIS ready")
    
    # Step 3: Send the command
    print_section("Step 3: Sending Voice Command")
    
    command = "open Safari and search for dogs"
    print(f"🎤 You say: 'JARVIS, {command}'")
    print("\n🎯 Watch what happens next...")
    
    try:
        # Process the command
        start_time = time.time()
        result = await context_handler.process_with_context(command, websocket)
        end_time = time.time()
        
        print_section("Step 4: Command Completed")
        
        # Show execution time
        print(f"⏱️  Total execution time: {end_time - start_time:.1f} seconds")
        
        # Show execution steps
        if result.get('execution_steps'):
            print(f"\n📊 Execution Steps ({len(result['execution_steps'])} total):")
            for i, step in enumerate(result['execution_steps'], 1):
                print(f"   {i}. {step['step']}")
                if step.get('details', {}).get('success') is False:
                    print(f"      ⚠️  Failed")
        
        # Verify success
        print("\n🔍 Verification:")
        
        # Check if lock was detected
        lock_detected = any("screen is locked" in msg.get('message', '').lower() 
                          for msg in websocket.messages 
                          if msg.get('type') == 'context_update')
        print(f"   {'✅' if lock_detected else '❌'} Lock detected and announced")
        
        # Check if unlock happened
        unlock_success = any("unlocked successfully" in str(step) 
                           for step in result.get('execution_steps', []))
        print(f"   {'✅' if unlock_success else '❌'} Screen unlocked")
        
        # Check if Safari opened
        # Give Safari a moment to open
        await asyncio.sleep(2)
        safari_check = subprocess.run(
            ["osascript", "-e", 'tell app "System Events" to get name of first process whose frontmost is true'],
            capture_output=True, text=True
        )
        safari_opened = "Safari" in safari_check.stdout
        print(f"   {'✅' if safari_opened else '❌'} Safari opened")
        
        # Final status
        if lock_detected and unlock_success and safari_opened:
            print("\n🎉 SUCCESS! Complete flow worked perfectly!")
            print("   - JARVIS detected the lock")
            print("   - Provided clear feedback before unlocking") 
            print("   - Unlocked the screen")
            print("   - Opened Safari and searched for dogs")
        else:
            print("\n⚠️  Some steps may not have completed as expected")
            
    except Exception as e:
        print(f"\n❌ Error during demo: {e}")
        import traceback
        traceback.print_exc()

async def test_simple_command():
    """Test a simple command without locking"""
    print_section("Bonus: Testing Without Lock")
    
    processor = UnifiedCommandProcessor()  
    context_handler = wrap_with_enhanced_context(processor)
    websocket = DemoWebSocket()
    
    print("🎤 You say: 'JARVIS, what time is it?'")
    
    result = await context_handler.process_with_context("what time is it", websocket)
    
    print(f"\n📄 Result: {result.get('response')}")

if __name__ == "__main__":
    print("🚀 JARVIS Lock/Unlock Flow Demo")
    print("================================")
    print("\nThis demo shows the complete flow of JARVIS handling")
    print("a command when your screen is locked.")
    
    # Run the main demo
    asyncio.run(demo_complete_flow())
    
    # Bonus test
    print("\n" + "="*80)
    print("💡 Bonus: Let's test a command that doesn't need the screen...")
    asyncio.run(test_simple_command())
    
    print("\n✨ Demo completed!")