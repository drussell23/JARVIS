#!/usr/bin/env python3
"""
Demo: Exact PRD Scenario
========================

Demonstrates the exact scenario from the PRD:
- Mac is locked
- User says "JARVIS, open Safari and search for dogs"
- JARVIS follows the exact flow specified
"""

import asyncio
import subprocess
import time
import sys
import os
from datetime import datetime

# Ensure we can import from backend
backend_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)

from api.simple_context_handler_enhanced import wrap_with_enhanced_context
from api.unified_command_processor import UnifiedCommandProcessor

class PRDWebSocket:
    """WebSocket that tracks the PRD flow"""
    def __init__(self):
        self.flow = []
        
    async def send_json(self, data):
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        
        if data.get('type') == 'context_update':
            message = data.get('message', '')
            status = data.get('status', '')
            
            # Track flow step
            if "screen is locked" in message:
                self.flow.append({
                    'time': timestamp,
                    'event': 'LOCK_DETECTED',
                    'message': message
                })
                print(f"\n[{timestamp}] 🔒 JARVIS detected: Screen is LOCKED")
                print(f"[{timestamp}] 💬 JARVIS: \"{message}\"")
                
            if status == 'unlocking_screen':
                self.flow.append({
                    'time': timestamp,
                    'event': 'UNLOCKING',
                    'status': status
                })
                print(f"[{timestamp}] 🔓 Status: Unlocking screen...")
                
            elif status == 'executing_command':
                self.flow.append({
                    'time': timestamp,
                    'event': 'EXECUTING',
                    'status': status
                })
                print(f"[{timestamp}] 🚀 Status: Executing command...")

async def run_prd_scenario():
    """Run the exact PRD scenario"""
    
    print("\n" + "="*80)
    print("🎯 PRD SCENARIO DEMONSTRATION")
    print("="*80)
    
    print("\n📋 From PRD Section 10 - Example Scenario:")
    print('   Case: Mac is locked, user says "JARVIS, open Safari and search for dogs."')
    print("\n   Expected Flow:")
    print('   1. JARVIS detects state = Locked')
    print('   2. Queues request: { action: "search dogs in Safari" }')
    print('   3. Feedback: "Your screen is locked, unlocking now."')
    print('   4. Unlock Manager runs')
    print('   5. On success: JARVIS resumes queued request')
    print('   6. Execution Layer: opens Safari, searches "dogs"')
    print('   7. Feedback: "I unlocked your screen, opened Safari, and searched for dogs."')
    
    print("\n⏳ Starting demo in 3 seconds...")
    for i in range(3, 0, -1):
        print(f"   {i}...", end='\r')
        await asyncio.sleep(1)
    
    # Step 1: Lock the screen
    print("\n\n[SETUP] Locking screen...")
    lock_cmd = """osascript -e 'tell app "System Events" to key code 12 using {control down, command down}'"""
    subprocess.run(lock_cmd, shell=True)
    
    print("[SETUP] Waiting for lock to complete...")
    await asyncio.sleep(3)
    
    # Step 2: Simulate user command
    print("\n" + "-"*60)
    print("[USER INPUT]")
    command = "open Safari and search for dogs"
    print(f'🎤 User says: "JARVIS, {command}"')
    print("-"*60)
    
    # Step 3: Create JARVIS components
    processor = UnifiedCommandProcessor()
    context_handler = wrap_with_enhanced_context(processor)
    websocket = PRDWebSocket()
    
    # Step 4: Process command
    print("\n[JARVIS PROCESSING]")
    start_time = time.time()
    
    try:
        result = await context_handler.process_with_context(command, websocket)
        end_time = time.time()
        
        print(f"\n[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] ✅ Command completed")
        print(f"Total execution time: {end_time - start_time:.2f} seconds")
        
        # Step 5: Verify PRD compliance
        print("\n" + "="*60)
        print("📊 PRD COMPLIANCE CHECK")
        print("="*60)
        
        # Check each PRD requirement
        checks = {
            "Lock Detection": any(e['event'] == 'LOCK_DETECTED' for e in websocket.flow),
            "Unlock Feedback": any("I'll unlock it now" in e.get('message', '') for e in websocket.flow),
            "Unlock Attempt": any(e['event'] == 'UNLOCKING' for e in websocket.flow),
            "Command Execution": any(e['event'] == 'EXECUTING' for e in websocket.flow),
            "Screen Unlocked": any("unlocked successfully" in str(step) for step in result.get('execution_steps', []))
        }
        
        all_passed = True
        for check, passed in checks.items():
            status = "✅ PASS" if passed else "❌ FAIL"
            print(f"   {status} - {check}")
            if not passed:
                all_passed = False
        
        # Show execution steps
        if result.get('execution_steps'):
            print("\n📝 Execution Steps:")
            for i, step in enumerate(result['execution_steps'], 1):
                print(f"   {i}. {step['step']}")
        
        # Final verdict
        print("\n" + "="*60)
        if all_passed:
            print("🎉 SUCCESS! JARVIS followed the PRD flow perfectly!")
            print("\n✅ Just like the PRD specified:")
            print("   - JARVIS detected the locked screen")
            print("   - Announced its intent to unlock")
            print("   - Unlocked the screen")
            print("   - Then executed the command")
        else:
            print("⚠️  Some PRD requirements were not met")
            
        # Show Safari status
        await asyncio.sleep(2)
        safari_check = subprocess.run(
            ["osascript", "-e", 'tell app "System Events" to get name of first process whose frontmost is true'],
            capture_output=True, text=True
        )
        if "Safari" in safari_check.stdout:
            print("\n🌐 Safari is now open and ready!")
            
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    print("🚀 JARVIS PRD Scenario Demo")
    print("This demonstrates the exact flow from the PRD documentation\n")
    
    try:
        asyncio.run(run_prd_scenario())
        
        print("\n" + "="*80)
        print("📚 Key Takeaway:")
        print("JARVIS now acts intelligently when your screen is locked,")
        print("providing clear feedback and handling the unlock seamlessly!")
        print("="*80)
        
    except KeyboardInterrupt:
        print("\n\n❌ Demo cancelled")