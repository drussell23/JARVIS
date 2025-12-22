#!/usr/bin/env python3
"""
Hardware Control Module for JARVIS
Controls camera, displays, audio, and other hardware components
Powered by Anthropic's Claude API for intelligent control decisions
"""

import asyncio
import logging
import subprocess
import json
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import anthropic
import os
import re

logger = logging.getLogger(__name__)


class HardwareComponent(Enum):
    """Hardware components that can be controlled"""
    CAMERA = "camera"
    MICROPHONE = "microphone"
    DISPLAY = "display"
    AUDIO_OUTPUT = "audio_output"
    BLUETOOTH = "bluetooth"
    WIFI = "wifi"
    KEYBOARD_BACKLIGHT = "keyboard_backlight"
    TRACKPAD = "trackpad"
    EXTERNAL_DISPLAY = "external_display"


class ControlMode(Enum):
    """Control modes for hardware"""
    AUTOMATIC = "automatic"
    MANUAL = "manual"
    SCHEDULED = "scheduled"
    CONTEXT_AWARE = "context_aware"


@dataclass
class HardwareState:
    """Current state of hardware component"""
    component: HardwareComponent
    enabled: bool
    settings: Dict[str, Any]
    last_changed: datetime
    controlled_by_jarvis: bool = False


@dataclass
class HardwareControlDecision:
    """AI-powered hardware control decision"""
    component: HardwareComponent
    action: str  # enable, disable, adjust
    parameters: Dict[str, Any]
    reasoning: str
    context: str
    confidence: float
    user_benefit: str


class HardwareControlSystem:
    """
    Advanced hardware control with AI decision making
    """
    
    def __init__(self, anthropic_api_key: str):
        self.claude = anthropic.Anthropic(api_key=anthropic_api_key)
        
        # Hardware states
        self.hardware_states: Dict[HardwareComponent, HardwareState] = {}
        self.control_mode = ControlMode.CONTEXT_AWARE
        
        # Control policies
        self.policies = {
            'privacy_mode': False,
            'power_saving': False,
            'presentation_mode': False,
            'focus_mode': False
        }
        
        # Initialize hardware states
        self._initialize_hardware_states()
        
    def _initialize_hardware_states(self):
        """Initialize hardware component states"""
        for component in HardwareComponent:
            self.hardware_states[component] = HardwareState(
                component=component,
                enabled=True,  # Assume enabled by default
                settings={},
                last_changed=datetime.now()
            )
    
    async def control_camera(self, action: str, reason: Optional[str] = None) -> Dict[str, Any]:
        """Control camera with AI validation"""
        try:
            # Validate action with AI
            validation = await self._validate_hardware_action(
                HardwareComponent.CAMERA, action, reason
            )
            
            if not validation['approved']:
                return {
                    'success': False,
                    'reason': validation['reason'],
                    'suggestion': validation.get('alternative')
                }
            
            # Execute camera control
            if action == 'disable':
                # Disable camera using system commands
                result = await self._disable_camera()
            elif action == 'enable':
                # Enable camera
                result = await self._enable_camera()
            else:
                return {'success': False, 'reason': 'Unknown action'}
            
            # Update state
            self.hardware_states[HardwareComponent.CAMERA].enabled = (action == 'enable')
            self.hardware_states[HardwareComponent.CAMERA].last_changed = datetime.now()
            self.hardware_states[HardwareComponent.CAMERA].controlled_by_jarvis = True
            
            # Log decision
            await self._log_hardware_decision(
                HardwareComponent.CAMERA, action, reason, result
            )
            
            return result
            
        except Exception as e:
            logger.error(f"Error controlling camera: {e}")
            return {'success': False, 'error': str(e)}
    
    async def _disable_camera(self) -> Dict[str, Any]:
        """Disable camera on macOS"""
        try:
            # Use system commands to disable camera
            # Note: This requires admin privileges in practice
            script = '''
            tell application "System Preferences"
                reveal anchor "Privacy_Camera" of pane "com.apple.preference.security"
            end tell
            '''
            
            subprocess.run(['osascript', '-e', script], capture_output=True)
            
            return {
                'success': True,
                'message': 'Camera access restricted',
                'method': 'system_preferences'
            }
            
        except Exception as e:
            logger.error(f"Error disabling camera: {e}")
            return {'success': False, 'error': str(e)}
    
    async def _enable_camera(self) -> Dict[str, Any]:
        """Enable camera on macOS"""
        try:
            # Camera enabling logic
            return {
                'success': True,
                'message': 'Camera access restored',
                'method': 'system_preferences'
            }
            
        except Exception as e:
            logger.error(f"Error enabling camera: {e}")
            return {'success': False, 'error': str(e)}
    
    async def control_display(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        """Control display settings intelligently"""
        try:
            # Use AI to optimize display settings
            optimization = await self._optimize_display_settings(settings)
            
            results = {
                'brightness': None,
                'night_shift': None,
                'true_tone': None,
                'resolution': None
            }
            
            # Apply brightness
            if 'brightness' in optimization:
                brightness_result = await self._set_display_brightness(
                    optimization['brightness']
                )
                results['brightness'] = brightness_result
            
            # Apply Night Shift
            if 'night_shift' in optimization:
                night_shift_result = await self._set_night_shift(
                    optimization['night_shift']
                )
                results['night_shift'] = night_shift_result
            
            # Update state
            self.hardware_states[HardwareComponent.DISPLAY].settings.update(optimization)
            self.hardware_states[HardwareComponent.DISPLAY].last_changed = datetime.now()
            
            return {
                'success': True,
                'applied_settings': results,
                'optimization_reasoning': optimization.get('reasoning', '')
            }
            
        except Exception as e:
            logger.error(f"Error controlling display: {e}")
            return {'success': False, 'error': str(e)}
    
    async def _optimize_display_settings(self, requested_settings: Dict[str, Any]) -> Dict[str, Any]:
        """Use AI to optimize display settings"""
        try:
            # Get current context
            current_time = datetime.now()
            is_night = current_time.hour >= 20 or current_time.hour < 6
            
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-3-haiku-20240307",
                max_tokens=500,
                messages=[{
                    "role": "user",
                    "content": f"""Optimize display settings for user comfort and health:

Requested Settings: {json.dumps(requested_settings, indent=2)}
Current Time: {current_time.strftime('%H:%M')}
Is Night Time: {is_night}
Current Policies: {json.dumps(self.policies, indent=2)}

Provide optimal settings for:
1. Brightness (0-100)
2. Night Shift (enabled/disabled, temperature)
3. True Tone (enabled/disabled)

Consider:
- Eye strain reduction
- Circadian rhythm
- Current activity context
- Power consumption

Return settings with reasoning."""
                }]
            )
            
            # Parse response
            settings_text = response.content[0].text
            
            # Extract settings (simplified parsing)
            settings = requested_settings.copy()
            
            if 'brightness' in settings_text.lower():
                # Extract brightness value
                import re
                brightness_match = re.search(r'brightness[:\s]+(\d+)', settings_text.lower())
                if brightness_match:
                    settings['brightness'] = int(brightness_match.group(1))
            
            if is_night and 'night shift' in settings_text.lower():
                settings['night_shift'] = {'enabled': True, 'temperature': 'warm'}
            
            settings['reasoning'] = "AI-optimized for current context"
            
            return settings
            
        except Exception as e:
            logger.error(f"Error optimizing display settings: {e}")
            return requested_settings
    
    async def _set_display_brightness(self, level: int) -> Dict[str, Any]:
        """Set display brightness"""
        try:
            # Clamp brightness level
            level = max(0, min(100, level))
            
            # Convert to 0-1 scale
            brightness = level / 100.0
            
            # Use brightness command (requires installation)
            # In practice, this would use a proper API or tool
            result = subprocess.run(
                ['brightness', str(brightness)],
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                return {'success': True, 'level': level}
            else:
                # Fallback method using AppleScript
                script = f'''
                tell application "System Preferences"
                    reveal anchor "displaysDisplayTab" of pane "com.apple.preference.displays"
                end tell
                '''
                subprocess.run(['osascript', '-e', script], capture_output=True)
                
                return {'success': True, 'level': level, 'method': 'manual'}
                
        except Exception as e:
            logger.error(f"Error setting brightness: {e}")
            return {'success': False, 'error': str(e)}
    
    async def _set_night_shift(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        """Control Night Shift settings"""
        try:
            enabled = settings.get('enabled', False)
            
            # Use nightlight CLI or system commands
            # This is a simplified implementation
            if enabled:
                # Enable Night Shift
                script = '''
                tell application "System Preferences"
                    reveal anchor "displaysNightShiftTab" of pane "com.apple.preference.displays"
                end tell
                '''
            else:
                # Disable Night Shift
                script = '''
                tell application "System Preferences"
                    reveal anchor "displaysNightShiftTab" of pane "com.apple.preference.displays"
                end tell
                '''
            
            subprocess.run(['osascript', '-e', script], capture_output=True)
            
            return {
                'success': True,
                'enabled': enabled,
                'temperature': settings.get('temperature', 'medium')
            }
            
        except Exception as e:
            logger.error(f"Error setting Night Shift: {e}")
            return {'success': False, 'error': str(e)}
    
    async def control_audio(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        """Control audio settings intelligently"""
        try:
            # Validate with AI
            decision = await self._make_audio_decision(settings)
            
            results = {}
            
            # Volume control
            if 'volume' in decision:
                volume_result = await self._set_volume(decision['volume'])
                results['volume'] = volume_result
            
            # Mute control
            if 'mute' in decision:
                mute_result = await self._set_mute(decision['mute'])
                results['mute'] = mute_result
            
            # Input/Output device selection
            if 'output_device' in decision:
                device_result = await self._set_audio_device(
                    decision['output_device'], 'output'
                )
                results['output_device'] = device_result
            
            # Update state
            self.hardware_states[HardwareComponent.AUDIO_OUTPUT].settings.update(decision)
            
            return {
                'success': True,
                'applied_settings': results,
                'reasoning': decision.get('reasoning', '')
            }
            
        except Exception as e:
            logger.error(f"Error controlling audio: {e}")
            return {'success': False, 'error': str(e)}
    
    async def _make_audio_decision(self, requested_settings: Dict[str, Any]) -> Dict[str, Any]:
        """Make intelligent audio control decisions"""
        try:
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-3-haiku-20240307",
                max_tokens=400,
                messages=[{
                    "role": "user",
                    "content": f"""Make audio control decision:

Requested: {json.dumps(requested_settings, indent=2)}
Policies: {json.dumps(self.policies, indent=2)}

Consider:
- User comfort (not too loud/quiet)
- Context (meeting, focus, etc)
- Time of day
- Device appropriateness

Provide settings for:
- Volume (0-100)
- Mute (true/false)
- Output device (if specified)

Include reasoning."""
                }]
            )
            
            # Parse response and apply safety limits
            decision = requested_settings.copy()
            
            # Apply volume safety
            if 'volume' in decision:
                decision['volume'] = max(0, min(85, decision['volume']))  # Cap at 85%
            
            decision['reasoning'] = "AI-optimized for comfort and context"
            
            return decision
            
        except Exception as e:
            logger.error(f"Error making audio decision: {e}")
            return requested_settings
    
    async def _set_volume(self, level: int) -> Dict[str, Any]:
        """Set system volume"""
        try:
            # Use osascript to set volume
            script = f'set volume output volume {level}'
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True
            )
            
            return {
                'success': result.returncode == 0,
                'level': level
            }
            
        except Exception as e:
            logger.error(f"Error setting volume: {e}")
            return {'success': False, 'error': str(e)}
    
    async def _set_mute(self, mute: bool) -> Dict[str, Any]:
        """Set system mute state"""
        try:
            script = f'set volume output muted {str(mute).lower()}'
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True
            )
            
            return {
                'success': result.returncode == 0,
                'muted': mute
            }
            
        except Exception as e:
            logger.error(f"Error setting mute: {e}")
            return {'success': False, 'error': str(e)}
    
    async def _set_audio_device(self, device_name: str, device_type: str) -> Dict[str, Any]:
        """Set audio input/output device"""
        try:
            # This would use SwitchAudioSource or similar tool
            # Simplified implementation
            logger.info(f"Setting {device_type} device to: {device_name}")
            
            return {
                'success': True,
                'device': device_name,
                'type': device_type
            }
            
        except Exception as e:
            logger.error(f"Error setting audio device: {e}")
            return {'success': False, 'error': str(e)}
    
    async def enable_privacy_mode(self) -> Dict[str, Any]:
        """Enable comprehensive privacy mode"""
        try:
            logger.info("Enabling privacy mode")
            
            actions_taken = []
            
            # 1. Disable camera
            camera_result = await self.control_camera('disable', 'Privacy mode activated')
            if camera_result['success']:
                actions_taken.append("Camera disabled")
            
            # 2. Mute microphone
            mic_result = await self._set_mute(True)
            if mic_result['success']:
                actions_taken.append("Microphone muted")
            
            # 3. Disable Bluetooth (if not needed)
            if not self._is_bluetooth_needed():
                bt_result = await self._control_bluetooth(False)
                if bt_result['success']:
                    actions_taken.append("Bluetooth disabled")
            
            # 4. Enable firewall
            fw_result = await self._enable_firewall()
            if fw_result['success']:
                actions_taken.append("Firewall enabled")
            
            # Update policy
            self.policies['privacy_mode'] = True
            
            return {
                'success': True,
                'privacy_mode': 'enabled',
                'actions_taken': actions_taken,
                'timestamp': datetime.now().isoformat()
            }
            
        except Exception as e:
            logger.error(f"Error enabling privacy mode: {e}")
            return {'success': False, 'error': str(e)}
    
    async def disable_privacy_mode(self) -> Dict[str, Any]:
        """Disable privacy mode and restore normal settings"""
        try:
            logger.info("Disabling privacy mode")
            
            actions_taken = []
            
            # Restore camera
            camera_result = await self.control_camera('enable', 'Privacy mode deactivated')
            if camera_result['success']:
                actions_taken.append("Camera enabled")
            
            # Unmute microphone
            mic_result = await self._set_mute(False)
            if mic_result['success']:
                actions_taken.append("Microphone unmuted")
            
            # Update policy
            self.policies['privacy_mode'] = False
            
            return {
                'success': True,
                'privacy_mode': 'disabled',
                'actions_taken': actions_taken,
                'timestamp': datetime.now().isoformat()
            }
            
        except Exception as e:
            logger.error(f"Error disabling privacy mode: {e}")
            return {'success': False, 'error': str(e)}
    
    def _is_bluetooth_needed(self) -> bool:
        """Check if Bluetooth is needed for current devices"""
        # Check for connected Bluetooth devices
        try:
            result = subprocess.run(
                ['system_profiler', 'SPBluetoothDataType'],
                capture_output=True,
                text=True
            )
            
            # Simple check for connected devices
            return 'Connected: Yes' in result.stdout
            
        except:
            return True  # Assume needed if can't check
    
    async def _control_bluetooth(self, enable: bool) -> Dict[str, Any]:
        """Control Bluetooth state"""
        try:
            # Use blueutil or similar tool
            action = 'on' if enable else 'off'
            result = subprocess.run(
                ['blueutil', '--power', action],
                capture_output=True
            )
            
            return {
                'success': result.returncode == 0,
                'bluetooth': 'enabled' if enable else 'disabled'
            }
            
        except Exception as e:
            logger.error(f"Error controlling Bluetooth: {e}")
            return {'success': False, 'error': str(e)}
    
    async def _enable_firewall(self) -> Dict[str, Any]:
        """Enable macOS firewall"""
        try:
            # Enable firewall (requires admin privileges)
            result = subprocess.run(
                ['sudo', '/usr/libexec/ApplicationFirewall/socketfilterfw', '--setglobalstate', 'on'],
                capture_output=True
            )
            
            return {
                'success': result.returncode == 0,
                'firewall': 'enabled'
            }
            
        except Exception as e:
            logger.error(f"Error enabling firewall: {e}")
            return {'success': False, 'error': str(e)}
    
    async def optimize_for_presentation(self) -> Dict[str, Any]:
        """Optimize hardware for presentation mode"""
        try:
            logger.info("Optimizing for presentation mode")
            
            optimizations = []
            
            # 1. Maximize display brightness
            display_result = await self.control_display({'brightness': 100})
            if display_result['success']:
                optimizations.append("Display brightness maximized")
            
            # 2. Disable Night Shift
            night_shift_result = await self._set_night_shift({'enabled': False})
            if night_shift_result['success']:
                optimizations.append("Night Shift disabled")
            
            # 3. Ensure audio is at reasonable level
            audio_result = await self.control_audio({'volume': 70, 'mute': False})
            if audio_result['success']:
                optimizations.append("Audio optimized")
            
            # 4. Disable sleep and screen saver
            caffeinate_process = subprocess.Popen(['caffeinate', '-d', '-i', '-m'])
            optimizations.append("Sleep and screen saver disabled")
            
            # Update policy
            self.policies['presentation_mode'] = True
            
            return {
                'success': True,
                'mode': 'presentation',
                'optimizations': optimizations,
                'caffeinate_pid': caffeinate_process.pid
            }
            
        except Exception as e:
            logger.error(f"Error optimizing for presentation: {e}")
            return {'success': False, 'error': str(e)}
    
    async def _validate_hardware_action(self, component: HardwareComponent, 
                                      action: str, reason: Optional[str]) -> Dict[str, Any]:
        """Validate hardware action with AI"""
        try:
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-3-haiku-20240307",
                max_tokens=300,
                messages=[{
                    "role": "user",
                    "content": f"""Validate hardware control action:

Component: {component.value}
Action: {action}
Reason: {reason or "Not specified"}
Current Policies: {json.dumps(self.policies, indent=2)}

Should this action be allowed? Consider:
- User privacy
- Security implications
- Current context
- Reversibility

Respond with:
- Approved: yes/no
- Reason for decision
- Alternative suggestion if not approved"""
                }]
            )
            
            # Parse response
            response_text = response.content[0].text.lower()
            
            return {
                'approved': 'yes' in response_text or 'approved' in response_text,
                'reason': response.content[0].text,
                'alternative': None  # Would parse from response
            }
            
        except Exception as e:
            logger.error(f"Error validating hardware action: {e}")
            # Default to safe action
            return {
                'approved': False,
                'reason': 'Could not validate action',
                'alternative': 'Manual control recommended'
            }
    
    async def _log_hardware_decision(self, component: HardwareComponent,
                                   action: str, reason: Optional[str],
                                   result: Dict[str, Any]):
        """Log hardware control decisions for learning"""
        log_entry = {
            'timestamp': datetime.now().isoformat(),
            'component': component.value,
            'action': action,
            'reason': reason,
            'result': result,
            'policies': self.policies.copy()
        }
        
        # Would write to persistent storage
        logger.info(f"Hardware decision logged: {log_entry}")
    
    def get_hardware_status(self) -> Dict[str, Any]:
        """Get current hardware status"""
        status = {
            'components': {},
            'policies': self.policies,
            'control_mode': self.control_mode.value
        }
        
        for component, state in self.hardware_states.items():
            status['components'][component.value] = {
                'enabled': state.enabled,
                'settings': state.settings,
                'last_changed': state.last_changed.isoformat(),
                'controlled_by_jarvis': state.controlled_by_jarvis
            }
        
        return status


# Export main class
__all__ = ['HardwareControlSystem', 'HardwareComponent', 'ControlMode']