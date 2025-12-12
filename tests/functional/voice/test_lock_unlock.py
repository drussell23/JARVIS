#!/usr/bin/env python3
"""
Test script to verify lock/unlock commands work properly in JARVIS
"""

import asyncio
import sys
import subprocess

# Add backend to path
sys.path.insert(0, 'backend')

async def test_lock_unlock():
    """Test lock and unlock commands"""

    print("=" * 60)
    print("JARVIS LOCK/UNLOCK TEST")
    print("=" * 60)

    # Test 1: Direct lock command
    print("\n1. Testing direct lock command...")
    try:
        script = 'tell application "System Events" to keystroke "q" using {command down, control down}'
        result = subprocess.run(['osascript', '-e', script], capture_output=True, text=True)

        if result.returncode == 0:
            print("   ✅ Lock command works!")
        else:
            print(f"   ❌ Lock failed: {result.stderr}")
    except Exception as e:
        print(f"   ❌ Error: {e}")

    # Test 2: Check unified command processor
    print("\n2. Testing unified command processor...")
    try:
        from backend.api.unified_command_processor import UnifiedCommandProcessor

        processor = UnifiedCommandProcessor()

        # Test classification
        cmd_type, score = await processor._classify_command("lock my screen")
        print(f"   Classification: {cmd_type.name} (score: {score:.2f})")

        if cmd_type.name == "SCREEN_LOCK":
            print("   ✅ Command correctly classified as SCREEN_LOCK")
        else:
            print(f"   ⚠️  Command classified as {cmd_type.name} instead of SCREEN_LOCK")

    except Exception as e:
        print(f"   ❌ Error: {e}")

    # Test 3: Check simple unlock handler
    print("\n3. Testing simple unlock handler...")
    try:
        from backend.api.simple_unlock_handler import handle_unlock_command

        result = await handle_unlock_command("lock my screen")

        if result.get('success'):
            print(f"   ✅ Handler works! Response: {result.get('response')}")
        else:
            print(f"   ❌ Handler failed: {result}")

    except Exception as e:
        print(f"   ❌ Error: {e}")

    # Test 4: Check JARVIS voice handler
    print("\n4. Checking JARVIS voice handler routing...")
    try:
        # Check the code to see if our fix is in place
        with open('backend/voice/jarvis_agent_voice.py', 'r') as f:
            content = f.read()

        if 'CHECK FOR LOCK/UNLOCK COMMANDS FIRST' in content:
            print("   ✅ Lock/unlock bypass code is present")
        else:
            print("   ❌ Lock/unlock bypass code is missing")

        if 'not any(word in text_lower for word in ["lock", "unlock"])' in content:
            print("   ✅ Vision trigger exclusion is present")
        else:
            print("   ❌ Vision trigger exclusion is missing")

    except Exception as e:
        print(f"   ❌ Error: {e}")

    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    print("\nThe lock/unlock functionality is properly configured if:")
    print("1. Lock command works ✅")
    print("2. Commands are classified correctly")
    print("3. Handler processes commands successfully")
    print("4. JARVIS voice handler has the bypass code")
    print("\nWhen you say 'lock my screen' to JARVIS, it should now:")
    print("- Skip vision analysis")
    print("- Execute the lock command immediately")
    print("- Respond with 'Locking your screen now, Sir' or similar")

if __name__ == "__main__":
    asyncio.run(test_lock_unlock())