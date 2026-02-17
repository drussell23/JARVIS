#!/usr/bin/env python3
"""
Simple Demo: Lock Detection and Feedback
========================================

A simple demo that just shows the lock detection and feedback working
"""

import asyncio
import subprocess

# Direct test of the context handler
from api.simple_context_handler_enhanced import EnhancedSimpleContextHandler
from api.unified_command_processor import UnifiedCommandProcessor

class SimpleWebSocket:
    """Simple websocket for demo"""
    async def send_json(self, data):
        if data.get('type') == 'context_update':
            print(f"\n💬 JARVIS says: {data.get('message')}")
            if data.get('status'):
                print(f"   (Status: {data.get('status')})")

async def simple_demo():
    """Simple demonstration of lock detection"""
    
    print("\n" + "="*60)
    print("🔍 Simple Lock Detection Demo")
    print("="*60)
    
    print("\n📋 What this demo does:")
    print("   1. Checks if screen is locked")
    print("   2. If locked, shows the proper feedback")
    print("   3. Attempts to unlock")
    
    # Create handler
    processor = UnifiedCommandProcessor()
    handler = EnhancedSimpleContextHandler(processor)
    websocket = SimpleWebSocket()
    
    # Test command that requires screen
    command = "open Safari"
    
    print(f"\n🎤 Command: '{command}'")
    
    # First, let's manually check lock status
    from api.direct_unlock_handler_fixed import check_screen_locked_direct
    
    print("\n1️⃣ Checking screen status...")
    is_locked = await check_screen_locked_direct()
    print(f"   Screen is: {'LOCKED 🔒' if is_locked else 'UNLOCKED 🔓'}")
    
    if not is_locked:
        print("\n🔒 Let me lock the screen for the demo...")
        print("   Locking in 3 seconds...")
        for i in range(3, 0, -1):
            print(f"   {i}...")
            await asyncio.sleep(1)
            
        # Lock screen
        lock_cmd = """osascript -e 'tell app "System Events" to key code 12 using {control down, command down}'"""
        subprocess.run(lock_cmd, shell=True)
        
        print("   Waiting for lock...")
        await asyncio.sleep(3)
        
        # Check again
        is_locked = await check_screen_locked_direct()
        print(f"   Screen is now: {'LOCKED 🔒' if is_locked else 'UNLOCKED 🔓'}")
    
    print("\n2️⃣ Processing command with context awareness...")
    
    # Process command
    result = await handler.process_with_context(command, websocket)
    
    print("\n3️⃣ Results:")
    print(f"   Success: {result.get('success')}")
    
    if result.get('execution_steps'):
        print("\n   Steps taken:")
        for step in result.get('execution_steps', []):
            print(f"   - {step['step']}")

async def test_feedback_only():
    """Test just the feedback generation"""
    print("\n" + "="*60)
    print("💬 Testing Feedback Messages")
    print("="*60)
    
    handler = EnhancedSimpleContextHandler(None)
    
    test_commands = [
        "open Safari and search for dogs",
        "open Chrome",
        "show me the weather in Safari",
        "create a new document"
    ]
    
    print("\n🔍 How different commands would be announced:")
    
    for cmd in test_commands:
        action = handler._extract_action_description(cmd)
        message = f"I see your screen is locked. I'll unlock it now by typing in your password so I can {action}."
        
        print(f"\n📌 Command: '{cmd}'")
        print(f"💬 JARVIS would say: '{message}'")

if __name__ == "__main__":
    print("🚀 JARVIS Lock Detection Simple Demo")
    
    # Run feedback test
    asyncio.run(test_feedback_only())
    
    print("\n" + "-"*60)
    print("\n🎯 Now testing with actual lock detection...")
    print("Press Enter to continue or Ctrl+C to skip...")
    
    try:
        input()
        asyncio.run(simple_demo())
    except KeyboardInterrupt:
        print("\n✅ Demo completed!")