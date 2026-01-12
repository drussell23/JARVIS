"""
JARVIS Voice Unlock Integration Helper
=====================================

Integrates voice unlock with the main JARVIS system startup.
"""

import os
import sys
import asyncio
import logging
from pathlib import Path
from typing import Optional, Dict, Any

# Add parent directory to path
sys.path.append(str(Path(__file__).parent.parent.parent))

logger = logging.getLogger(__name__)


def check_voice_unlock_requirements() -> Dict[str, bool]:
    """Check if voice unlock requirements are met"""
    requirements = {
        'microphone': False,
        'dependencies': False,
        'permissions': False,
        'apple_watch': False,
        'memory': False
    }
    
    # Check microphone
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        requirements['microphone'] = any(d['max_input_channels'] > 0 for d in devices)
    except Exception:
        pass
        
    # Check dependencies
    try:
        from backend.voice_unlock import check_dependencies
        deps = check_dependencies()
        requirements['dependencies'] = all(deps.values())
    except Exception:
        pass
        
    # Check permissions (macOS)
    if sys.platform == 'darwin':
        # Check if we have microphone permission (simplified check)
        requirements['permissions'] = True  # Would need proper check
        
    # Check Apple Watch (Bluetooth)
    try:
        import bleak
        requirements['apple_watch'] = True
    except Exception:
        pass
        
    # Check memory
    try:
        import psutil
        vm = psutil.virtual_memory()
        requirements['memory'] = vm.available > 2 * 1024 * 1024 * 1024  # 2GB available
    except Exception:
        pass
        
    return requirements


def integrate_with_jarvis_startup():
    """Add voice unlock to JARVIS startup sequence"""
    startup_code = '''
# Voice Unlock Integration
try:
    from backend.voice_unlock.jarvis_integration import setup_voice_unlock
    
    # Check if voice unlock should be enabled
    if os.getenv('JARVIS_VOICE_UNLOCK', 'true').lower() == 'true':
        print(f"\\n{Colors.BLUE}Initializing Voice Unlock...{Colors.ENDC}")
        
        voice_status = setup_voice_unlock()
        
        if voice_status['enabled']:
            print(f"{Colors.GREEN}✓ Voice Unlock ready{Colors.ENDC}")
            print(f"  • Say 'Hey JARVIS, unlock my Mac'")
            if voice_status.get('apple_watch'):
                print(f"  • Apple Watch proximity enabled")
        else:
            print(f"{Colors.YELLOW}○ Voice Unlock disabled: {voice_status.get('reason', 'Unknown')}{Colors.ENDC}")
            
except Exception as e:
    print(f"{Colors.YELLOW}○ Voice Unlock not available: {str(e)}{Colors.ENDC}")
'''
    
    return startup_code


def setup_voice_unlock() -> Dict[str, Any]:
    """Setup voice unlock for JARVIS"""
    result = {
        'enabled': False,
        'reason': None,
        'apple_watch': False
    }
    
    try:
        # Get configuration (will auto-apply optimizations)
        from backend.voice_unlock.config import get_config
        config = get_config()
        
        # Log system info
        sys_info = config.get_system_info()
        logger.info(f"System: {sys_info['ram_gb']}GB RAM, {sys_info['ram_available_gb']}GB available")
        logger.info(f"Optimizations: {', '.join(sys_info['optimizations_applied'])}")
        
        # Check requirements
        reqs = check_voice_unlock_requirements()
        
        if not reqs['dependencies']:
            result['reason'] = "Missing dependencies (run install_voice_unlock_deps.sh)"
            return result
            
        if not reqs['microphone']:
            result['reason'] = "No microphone detected"
            return result
            
        if not reqs['memory']:
            result['reason'] = "Insufficient memory"
            return result
            
        # Initialize voice unlock
        from backend.voice_unlock import get_voice_unlock_system
        
        system = get_voice_unlock_system()
        if system:
            result['enabled'] = True
            result['apple_watch'] = reqs['apple_watch']
            
            # Start in background
            asyncio.create_task(_start_voice_unlock_async(system))
            
        else:
            result['reason'] = "Failed to initialize system"
            
    except Exception as e:
        logger.error(f"Voice unlock setup error: {e}")
        result['reason'] = str(e)
        
    return result


async def _start_voice_unlock_async(system):
    """Start voice unlock system asynchronously"""
    try:
        if hasattr(system, 'start'):
            await system.start()
            logger.info("Voice Unlock system started in background")
    except Exception as e:
        logger.error(f"Failed to start Voice Unlock: {e}")


def add_voice_commands_to_jarvis():
    """Add voice unlock commands to JARVIS command system"""
    commands = {
        'unlock_mac': {
            'patterns': [
                r"(?:hey |hi )?jarvis[,.]? (?:please )?unlock (?:my |the )?mac",
                r"jarvis[,.]? (?:this is |it's) (\w+)",
                r"jarvis[,.]? authenticate (?:me|user)?\s*(\w+)?"
            ],
            'handler': 'voice_unlock.authenticate',
            'description': 'Unlock Mac with voice authentication'
        },
        
        'lock_mac': {
            'patterns': [
                r"jarvis[,.]? (?:please )?lock (?:my |the )?(?:mac|computer)",
                r"jarvis[,.]? (?:activate |enable )?(?:security|lock)"
            ],
            'handler': 'voice_unlock.lock',
            'description': 'Lock the Mac'
        },
        
        'enroll_voice': {
            'patterns': [
                r"jarvis[,.]? (?:please )?(?:enroll|register|add) (?:my )?voice",
                r"jarvis[,.]? (?:create|setup) voice profile"
            ],
            'handler': 'voice_unlock.enroll',
            'description': 'Enroll voice for authentication'
        }
    }
    
    return commands


# Quick test function
def test_voice_unlock_integration():
    """Test voice unlock integration"""
    print("Testing Voice Unlock Integration...")
    print("=" * 50)
    
    # Check requirements
    reqs = check_voice_unlock_requirements()
    print("\nRequirements:")
    for req, status in reqs.items():
        icon = "✅" if status else "❌"
        print(f"  {icon} {req}")
        
    # Test setup
    print("\nSetting up Voice Unlock...")
    result = setup_voice_unlock()
    
    print(f"\nResult:")
    print(f"  Enabled: {result['enabled']}")
    if result['reason']:
        print(f"  Reason: {result['reason']}")
    if result['apple_watch']:
        print(f"  Apple Watch: Available")
        
    print("\nIntegration code:")
    print("-" * 50)
    print(integrate_with_jarvis_startup())


if __name__ == "__main__":
    test_voice_unlock_integration()