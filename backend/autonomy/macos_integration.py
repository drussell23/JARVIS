#!/usr/bin/env python3
"""
Advanced macOS Integration for JARVIS
Provides deep system control and hardware management
Powered by Anthropic's Claude API for intelligent decision making
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
import psutil
import platform

logger = logging.getLogger(__name__)


class SystemResource(Enum):
    """System resources that can be managed"""
    CPU = "cpu"
    MEMORY = "memory"
    DISK = "disk"
    NETWORK = "network"
    DISPLAY = "display"
    AUDIO = "audio"
    CAMERA = "camera"
    POWER = "power"
    BLUETOOTH = "bluetooth"
    WIFI = "wifi"


class ControlAction(Enum):
    """Types of control actions"""
    OPTIMIZE = "optimize"
    ADJUST = "adjust"
    ENABLE = "enable"
    DISABLE = "disable"
    RESTART = "restart"
    MONITOR = "monitor"
    CONFIGURE = "configure"


@dataclass
class SystemState:
    """Current system state"""
    cpu_usage: float
    memory_usage: float
    disk_usage: float
    active_apps: List[str]
    network_status: Dict[str, Any]
    display_config: Dict[str, Any]
    power_status: Dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class ControlDecision:
    """A control decision made by the AI"""
    resource: SystemResource
    action: ControlAction
    parameters: Dict[str, Any]
    reasoning: str
    confidence: float
    impact_prediction: Dict[str, Any]
    reversible: bool = True


class AdvancedMacOSIntegration:
    """
    Deep macOS integration with AI-powered control
    """
    
    def __init__(self, anthropic_api_key: str):
        self.claude = anthropic.Anthropic(api_key=anthropic_api_key)
        
        # System monitoring
        self.system_state = None
        self.monitoring_active = False
        self.monitoring_interval = 30  # seconds
        
        # Control history
        self.control_history = []
        self.optimization_rules = {}
        
        # Safety limits
        self.safety_limits = {
            'max_cpu_throttle': 0.5,  # Don't throttle CPU below 50%
            'min_memory_free': 2048,  # Keep at least 2GB free
            'max_brightness_change': 0.3,  # Max 30% brightness change
            'protected_apps': ['Finder', 'SystemUIServer', 'loginwindow']
        }
        
    async def start_system_monitoring(self):
        """Start continuous system monitoring"""
        self.monitoring_active = True
        asyncio.create_task(self._system_monitoring_loop())
        logger.info("Advanced macOS monitoring started")
        
    async def stop_system_monitoring(self):
        """Stop system monitoring"""
        self.monitoring_active = False
        
    async def _system_monitoring_loop(self):
        """Monitor system state continuously"""
        while self.monitoring_active:
            try:
                # Gather system state
                self.system_state = await self._gather_system_state()
                
                # Check for optimization opportunities
                opportunities = await self._identify_optimization_opportunities()
                
                # Execute high-confidence optimizations
                for opportunity in opportunities:
                    if opportunity.confidence > 0.8 and opportunity.reversible:
                        await self._execute_control_action(opportunity)
                
                await asyncio.sleep(self.monitoring_interval)
                
            except Exception as e:
                logger.error(f"Error in system monitoring: {e}")
                await asyncio.sleep(60)
    
    async def _gather_system_state(self) -> SystemState:
        """Gather comprehensive system state"""
        # CPU usage
        cpu_usage = psutil.cpu_percent(interval=1)
        
        # Memory usage
        memory = psutil.virtual_memory()
        memory_usage = memory.percent
        
        # Disk usage
        disk = psutil.disk_usage('/')
        disk_usage = disk.percent
        
        # Active applications
        active_apps = await self._get_active_applications()
        
        # Network status
        network_status = await self._get_network_status()
        
        # Display configuration
        display_config = await self._get_display_configuration()
        
        # Power status
        power_status = await self._get_power_status()
        
        return SystemState(
            cpu_usage=cpu_usage,
            memory_usage=memory_usage,
            disk_usage=disk_usage,
            active_apps=active_apps,
            network_status=network_status,
            display_config=display_config,
            power_status=power_status
        )
    
    async def _get_active_applications(self) -> List[str]:
        """Get list of active applications"""
        try:
            # Use AppleScript to get running applications
            script = '''
            tell application "System Events"
                set appList to name of every application process whose visible is true
            end tell
            return appList
            '''
            
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                apps = result.stdout.strip().split(', ')
                return apps
            
        except Exception as e:
            logger.error(f"Error getting active applications: {e}")
        
        return []
    
    async def _get_network_status(self) -> Dict[str, Any]:
        """Get network status information"""
        status = {
            'wifi_connected': False,
            'wifi_ssid': None,
            'ethernet_connected': False,
            'vpn_connected': False
        }
        
        try:
            # Check WiFi status
            wifi_result = subprocess.run(
                ['networksetup', '-getairportnetwork', 'en0'],
                capture_output=True,
                text=True
            )
            
            if 'Current Wi-Fi Network:' in wifi_result.stdout:
                status['wifi_connected'] = True
                status['wifi_ssid'] = wifi_result.stdout.split(':')[1].strip()
            
            # Check for VPN
            vpn_result = subprocess.run(
                ['scutil', '--nwi'],
                capture_output=True,
                text=True
            )
            
            if 'utun' in vpn_result.stdout:
                status['vpn_connected'] = True
                
        except Exception as e:
            logger.error(f"Error getting network status: {e}")
        
        return status
    
    async def _get_display_configuration(self) -> Dict[str, Any]:
        """Get display configuration"""
        config = {
            'brightness': None,
            'night_shift': False,
            'display_count': 1,
            'resolution': None
        }
        
        try:
            # Get display brightness (requires additional tools)
            # This is a placeholder - actual implementation would use
            # tools like brightness CLI or system APIs
            pass
            
        except Exception as e:
            logger.error(f"Error getting display config: {e}")
        
        return config
    
    async def _get_power_status(self) -> Dict[str, Any]:
        """Get power and battery status"""
        status = {
            'on_battery': False,
            'battery_percent': None,
            'power_adapter': False,
            'low_power_mode': False
        }
        
        try:
            # Get power info
            result = subprocess.run(
                ['pmset', '-g', 'batt'],
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                output = result.stdout
                
                # Parse battery percentage
                if '%' in output:
                    import re
                    match = re.search(r'(\d+)%', output)
                    if match:
                        status['battery_percent'] = int(match.group(1))
                
                # Check power source
                if 'AC Power' in output:
                    status['power_adapter'] = True
                else:
                    status['on_battery'] = True
                    
        except Exception as e:
            logger.error(f"Error getting power status: {e}")
        
        return status
    
    async def _identify_optimization_opportunities(self) -> List[ControlDecision]:
        """Use AI to identify optimization opportunities"""
        if not self.system_state:
            return []
        
        try:
            # Prepare system context for Claude
            context = {
                'cpu_usage': self.system_state.cpu_usage,
                'memory_usage': self.system_state.memory_usage,
                'disk_usage': self.system_state.disk_usage,
                'active_apps': self.system_state.active_apps[:10],  # Top 10 apps
                'network': self.system_state.network_status,
                'power': self.system_state.power_status,
                'timestamp': self.system_state.timestamp.isoformat()
            }
            
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-3-opus-20240229",
                max_tokens=1000,
                messages=[{
                    "role": "user",
                    "content": f"""As JARVIS system optimizer, analyze this macOS state and identify optimization opportunities:

System State:
{json.dumps(context, indent=2)}

Safety Limits:
- Don't throttle CPU below 50%
- Keep at least 2GB memory free
- Protected apps: {self.safety_limits['protected_apps']}

Identify optimizations that would:
1. Improve performance
2. Save battery (if on battery)
3. Reduce resource usage
4. Enhance user experience

For each optimization provide:
- Resource to optimize (cpu/memory/disk/network/display/power)
- Action to take
- Specific parameters
- Reasoning
- Confidence (0-1)
- Expected impact
- Is it reversible?

Focus on safe, beneficial optimizations."""
                }]
            )
            
            # Parse response
            opportunities = self._parse_optimization_response(response.content[0].text)
            return opportunities
            
        except Exception as e:
            logger.error(f"Error identifying optimizations: {e}")
            return []
    
    def _parse_optimization_response(self, response_text: str) -> List[ControlDecision]:
        """Parse AI response into control decisions"""
        decisions = []
        
        # Simple parsing - would be more sophisticated in practice
        lines = response_text.split('\n')
        current_decision = None
        
        for line in lines:
            line_lower = line.lower()
            
            if 'resource:' in line_lower:
                if current_decision:
                    decisions.append(current_decision)
                
                # Start new decision
                resource_str = line.split(':')[1].strip().upper()
                try:
                    resource = SystemResource[resource_str]
                    current_decision = ControlDecision(
                        resource=resource,
                        action=ControlAction.OPTIMIZE,
                        parameters={},
                        reasoning="",
                        confidence=0.5,
                        impact_prediction={}
                    )
                except:
                    current_decision = None
                    
            elif current_decision:
                if 'action:' in line_lower:
                    action_str = line.split(':')[1].strip().upper()
                    try:
                        current_decision.action = ControlAction[action_str]
                    except:
                        pass
                elif 'confidence:' in line_lower:
                    try:
                        conf = float(line.split(':')[1].strip().strip('%')) / 100
                        current_decision.confidence = conf
                    except:
                        pass
                elif 'reasoning:' in line_lower:
                    current_decision.reasoning = line.split(':')[1].strip()
                elif 'reversible:' in line_lower:
                    current_decision.reversible = 'yes' in line_lower or 'true' in line_lower
        
        if current_decision:
            decisions.append(current_decision)
        
        return decisions
    
    async def _execute_control_action(self, decision: ControlDecision):
        """Execute a control decision"""
        logger.info(f"Executing control action: {decision.resource.value} - {decision.action.value}")
        
        try:
            if decision.resource == SystemResource.MEMORY:
                await self._optimize_memory(decision)
            elif decision.resource == SystemResource.CPU:
                await self._optimize_cpu(decision)
            elif decision.resource == SystemResource.POWER:
                await self._optimize_power(decision)
            elif decision.resource == SystemResource.NETWORK:
                await self._optimize_network(decision)
            elif decision.resource == SystemResource.DISPLAY:
                await self._optimize_display(decision)
            
            # Record action
            self.control_history.append({
                'decision': decision,
                'timestamp': datetime.now(),
                'success': True
            })
            
        except Exception as e:
            logger.error(f"Error executing control action: {e}")
            self.control_history.append({
                'decision': decision,
                'timestamp': datetime.now(),
                'success': False,
                'error': str(e)
            })
    
    async def _optimize_memory(self, decision: ControlDecision):
        """Optimize memory usage"""
        if decision.action == ControlAction.OPTIMIZE:
            # Purge inactive memory
            subprocess.run(['sudo', 'purge'], capture_output=True)
            logger.info("Purged inactive memory")
    
    async def _optimize_cpu(self, decision: ControlDecision):
        """Optimize CPU usage"""
        if decision.action == ControlAction.OPTIMIZE:
            # This would implement CPU optimization
            # For safety, we're being conservative here
            logger.info("CPU optimization requested - monitoring high-usage processes")
    
    async def _optimize_power(self, decision: ControlDecision):
        """Optimize power settings"""
        if decision.action == ControlAction.OPTIMIZE:
            if self.system_state.power_status.get('on_battery'):
                # Enable power saving mode
                subprocess.run(['pmset', '-a', 'lowpowermode', '1'], capture_output=True)
                logger.info("Enabled low power mode")
    
    async def _optimize_network(self, decision: ControlDecision):
        """Optimize network settings"""
        if decision.action == ControlAction.OPTIMIZE:
            # This would implement network optimization
            logger.info("Network optimization requested")
    
    async def _optimize_display(self, decision: ControlDecision):
        """Optimize display settings"""
        if decision.action == ControlAction.ADJUST:
            # This would adjust display brightness/settings
            logger.info("Display optimization requested")
    
    async def control_application(self, app_name: str, action: str) -> bool:
        """Control a specific application"""
        try:
            # Validate app is not protected
            if app_name in self.safety_limits['protected_apps']:
                logger.warning(f"Cannot control protected app: {app_name}")
                return False
            
            # Use Claude to determine safe action
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-3-haiku-20240307",
                max_tokens=200,
                messages=[{
                    "role": "user",
                    "content": f"""Is it safe to {action} the application "{app_name}"?
Consider:
- System stability
- User workflow disruption
- Data loss risk

Respond with: SAFE or UNSAFE and brief reason."""
                }]
            )
            
            if 'SAFE' in response.content[0].text:
                # Execute action
                if action == 'quit':
                    script = f'tell application "{app_name}" to quit'
                elif action == 'hide':
                    script = f'tell application "System Events" to set visible of process "{app_name}" to false'
                elif action == 'activate':
                    script = f'tell application "{app_name}" to activate'
                else:
                    return False
                
                result = subprocess.run(
                    ['osascript', '-e', script],
                    capture_output=True
                )
                
                return result.returncode == 0
            
        except Exception as e:
            logger.error(f"Error controlling application: {e}")
        
        return False
    
    async def optimize_for_context(self, context: str) -> Dict[str, Any]:
        """Optimize system for specific context (meeting, focus, gaming, etc)"""
        try:
            # Get current state
            state = await self._gather_system_state()
            
            # Use Claude to determine optimal settings
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-3-opus-20240229",
                max_tokens=800,
                messages=[{
                    "role": "user",
                    "content": f"""Optimize macOS for context: {context}

Current System State:
- CPU Usage: {state.cpu_usage}%
- Memory Usage: {state.memory_usage}%
- Active Apps: {', '.join(state.active_apps[:5])}
- On Battery: {state.power_status.get('on_battery', False)}

Provide specific optimization actions for:
1. Application management (which to close/minimize)
2. System settings (power, display, etc)
3. Network configuration
4. Resource allocation

Be specific and safe."""
                }]
            )
            
            # Parse and execute optimizations
            optimizations = self._parse_context_optimizations(response.content[0].text)
            
            results = {
                'context': context,
                'optimizations_applied': [],
                'state_before': state,
                'success': True
            }
            
            for opt in optimizations:
                try:
                    await self._apply_context_optimization(opt)
                    results['optimizations_applied'].append(opt)
                except Exception as e:
                    logger.error(f"Failed to apply optimization: {e}")
            
            return results
            
        except Exception as e:
            logger.error(f"Error optimizing for context: {e}")
            return {'success': False, 'error': str(e)}
    
    def _parse_context_optimizations(self, response_text: str) -> List[Dict[str, Any]]:
        """Parse context optimization response"""
        optimizations = []
        
        # Extract optimization instructions
        lines = response_text.split('\n')
        current_opt = None
        
        for line in lines:
            if any(marker in line for marker in ['1.', '2.', '3.', '-', '•']):
                if current_opt:
                    optimizations.append(current_opt)
                current_opt = {
                    'description': line.strip().lstrip('1234567890.-• '),
                    'type': 'general'
                }
        
        if current_opt:
            optimizations.append(current_opt)
        
        return optimizations
    
    async def _apply_context_optimization(self, optimization: Dict[str, Any]):
        """Apply a specific context optimization"""
        desc = optimization['description'].lower()
        
        if 'close' in desc or 'quit' in desc:
            # Extract app name and close it
            for app in self.system_state.active_apps:
                if app.lower() in desc:
                    await self.control_application(app, 'quit')
                    
        elif 'minimize' in desc or 'hide' in desc:
            # Minimize applications
            for app in self.system_state.active_apps:
                if app.lower() in desc:
                    await self.control_application(app, 'hide')
                    
        elif 'power' in desc and 'save' in desc:
            # Enable power saving
            subprocess.run(['pmset', '-a', 'lowpowermode', '1'], capture_output=True)
            
        elif 'notification' in desc and 'disable' in desc:
            # Disable notifications (Do Not Disturb)
            script = '''
            tell application "System Events"
                keystroke "d" using {command down, option down}
            end tell
            '''
            subprocess.run(['osascript', '-e', script], capture_output=True)
    
    def get_system_status(self) -> Dict[str, Any]:
        """Get current system status"""
        if not self.system_state:
            return {'status': 'Not monitoring'}
        
        return {
            'monitoring_active': self.monitoring_active,
            'last_update': self.system_state.timestamp.isoformat(),
            'system_health': {
                'cpu_usage': f"{self.system_state.cpu_usage:.1f}%",
                'memory_usage': f"{self.system_state.memory_usage:.1f}%",
                'disk_usage': f"{self.system_state.disk_usage:.1f}%"
            },
            'active_apps': len(self.system_state.active_apps),
            'network_connected': self.system_state.network_status.get('wifi_connected', False),
            'on_battery': self.system_state.power_status.get('on_battery', False),
            'optimizations_applied': len(self.control_history),
            'last_optimization': self.control_history[-1] if self.control_history else None
        }
    
    async def emergency_optimization(self) -> Dict[str, Any]:
        """Emergency optimization when system is under stress"""
        logger.warning("Emergency optimization triggered")
        
        # Immediate actions
        actions_taken = []
        
        # 1. Purge memory
        subprocess.run(['sudo', 'purge'], capture_output=True)
        actions_taken.append("Purged inactive memory")
        
        # 2. Enable low power mode if on battery
        if self.system_state and self.system_state.power_status.get('on_battery'):
            subprocess.run(['pmset', '-a', 'lowpowermode', '1'], capture_output=True)
            actions_taken.append("Enabled low power mode")
        
        # 3. Close non-essential apps
        essential_apps = self.safety_limits['protected_apps'] + ['Code', 'Terminal', 'Chrome', 'Safari']
        
        if self.system_state:
            for app in self.system_state.active_apps:
                if app not in essential_apps and app not in ['JARVIS', 'Python']:
                    success = await self.control_application(app, 'quit')
                    if success:
                        actions_taken.append(f"Closed {app}")
        
        # 4. Disable visual effects (reduced transparency)
        subprocess.run(
            ['defaults', 'write', 'com.apple.universalaccess', 'reduceTransparency', '-bool', 'true'],
            capture_output=True
        )
        actions_taken.append("Reduced visual effects")
        
        return {
            'success': True,
            'actions_taken': actions_taken,
            'timestamp': datetime.now().isoformat()
        }


# Singleton instance manager
_macos_integration: Optional[AdvancedMacOSIntegration] = None


def get_macos_integration(api_key: Optional[str] = None) -> AdvancedMacOSIntegration:
    """Get or create macOS integration instance"""
    global _macos_integration
    if _macos_integration is None:
        api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("Anthropic API key required")
        _macos_integration = AdvancedMacOSIntegration(api_key)
    return _macos_integration


# Export main class
__all__ = ['AdvancedMacOSIntegration', 'get_macos_integration', 'SystemResource', 'ControlAction']