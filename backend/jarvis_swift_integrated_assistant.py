#!/usr/bin/env python3
"""
JARVIS Integrated Assistant with Swift Command Classification
Combines Swift NLP, Vision Analysis, and System Control
"""

import asyncio
import logging
import os
import subprocess
import json
from typing import Dict, List, Optional, Tuple, Any, Union
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Import Swift bridge
from swift_bridge.advanced_python_bridge import AdvancedSwiftBridge

# Import existing components
from jarvis_integrated_assistant import (
    JARVISIntegratedAssistant, 
    IntegratedContext, 
    IntegratedResponse
)
from vision.proactive_vision_assistant import NotificationEvent

logger = logging.getLogger(__name__)

class JARVISSwiftIntegratedAssistant(JARVISIntegratedAssistant):
    """
    Enhanced JARVIS assistant that uses Swift for command classification
    while maintaining all vision integration features
    """
    
    def __init__(self, user_name: str = "Sir"):
        # Initialize parent with all vision features
        super().__init__(user_name)
        
        # Initialize Swift bridge for command classification
        try:
            self.swift_bridge = AdvancedSwiftBridge()
            self.swift_enabled = True
            logger.info("Swift command classifier initialized successfully")
        except Exception as e:
            logger.warning(f"Swift bridge not available: {e}")
            self.swift_enabled = False
            self.swift_bridge = None
    
    async def process_vision_command(self, command: str) -> IntegratedResponse:
        """
        Process vision command using Swift classification first,
        then route through vision system
        """
        # Use Swift to classify the command intent
        if self.swift_enabled:
            classification = await self._classify_with_swift(command)
            
            # If Swift identifies this as a vision command with high confidence
            if classification and classification.get('category') == 'vision':
                # Enhance the command with Swift's understanding
                enhanced_command = self._enhance_command_with_swift(
                    command, classification
                )
                
                # Log Swift classification
                logger.info(f"Swift classified vision command: {classification}")
                
                # Process through parent's vision system
                response = await super().process_vision_command(enhanced_command)
                
                # Add Swift metadata to response
                if hasattr(response, 'visual_context'):
                    response.visual_context['swift_classification'] = classification
                
                return response
        
        # Fallback to parent implementation
        return await super().process_vision_command(command)
    
    async def _classify_with_swift(self, command: str) -> Optional[Dict]:
        """Classify command using Swift NLP engine"""
        try:
            result = self.swift_bridge.classify_command(command)
            
            # Convert Swift result to Python dict
            classification = {
                'action': result.action,
                'category': result.category.value,
                'confidence': result.confidence,
                'entities': result.entities,
                'context': result.context
            }
            
            return classification
        except Exception as e:
            logger.error(f"Swift classification failed: {e}")
            return None
    
    def _enhance_command_with_swift(self, original_command: str, 
                                  classification: Dict) -> str:
        """Enhance command based on Swift's understanding"""
        # If Swift detected specific entities or intents
        if classification.get('entities'):
            entities = classification['entities']
            
            # Add context from Swift's NLP
            if 'screen_area' in entities:
                # Swift identified a specific screen area
                return f"{original_command} focusing on {entities['screen_area']}"
            
            if 'app_name' in entities:
                # Swift identified a specific app
                return f"{original_command} specifically for {entities['app_name']}"
            
            if 'notification_type' in entities:
                # Swift identified notification intent
                return f"{original_command} with emphasis on {entities['notification_type']}"
        
        return original_command
    
    async def handle_notification_detected(self, 
                                         notification: NotificationEvent) -> IntegratedResponse:
        """
        Handle notifications with Swift-enhanced understanding
        """
        # First, use Swift to understand the notification context
        if self.swift_enabled and notification.content_preview:
            swift_analysis = await self._analyze_notification_with_swift(notification)
            
            if swift_analysis:
                # Use Swift's understanding to enhance response
                notification.visual_cues['swift_analysis'] = swift_analysis
        
        # Process through parent's notification handler
        return await super().handle_notification_detected(notification)
    
    async def _analyze_notification_with_swift(self, 
                                             notification: NotificationEvent) -> Optional[Dict]:
        """Use Swift to analyze notification content"""
        try:
            # Create a command representing the notification
            notif_command = (
                f"notification from {notification.app_name}: "
                f"{notification.content_preview or 'new message'}"
            )
            
            # Classify with Swift
            analysis = await self._classify_with_swift(notif_command)
            
            if analysis:
                # Add notification-specific analysis
                analysis['urgency'] = self._determine_urgency_with_swift(analysis)
                analysis['suggested_response_type'] = (
                    self._suggest_response_type_with_swift(analysis)
                )
            
            return analysis
        except Exception as e:
            logger.error(f"Swift notification analysis failed: {e}")
            return None
    
    def _determine_urgency_with_swift(self, analysis: Dict) -> str:
        """Use Swift's NLP to determine notification urgency"""
        confidence = analysis.get('confidence', 0)
        entities = analysis.get('entities', {})
        
        # Swift might detect urgency indicators
        if 'urgency_indicator' in entities:
            return entities['urgency_indicator']
        
        # Use confidence as a proxy
        if confidence > 0.9:
            return 'high'
        elif confidence > 0.7:
            return 'medium'
        else:
            return 'low'
    
    def _suggest_response_type_with_swift(self, analysis: Dict) -> str:
        """Suggest response type based on Swift's understanding"""
        category = analysis.get('category', '')
        action = analysis.get('action', '')
        
        if category == 'question':
            return 'answer'
        elif category == 'request':
            return 'acknowledge'
        elif category == 'urgent':
            return 'immediate'
        else:
            return 'general'
    
    def get_system_info(self) -> Dict[str, Any]:
        """Get system information including Swift status"""
        info = {
            'swift_enabled': self.swift_enabled,
            'vision_enabled': True,
            'notification_monitoring': self.notification_monitor_active,
            'learned_patterns': len(self.interaction_patterns),
            'components': {
                'swift_classifier': self.swift_enabled,
                'proactive_vision': True,
                'workspace_analyzer': True,
                'dynamic_engine': True,
                'voice_system': bool(self.voice_system)
            }
        }
        
        # Add Swift-specific info if available
        if self.swift_enabled and self.swift_bridge:
            try:
                swift_info = self.swift_bridge.get_statistics()
                info['swift_stats'] = swift_info
            except Exception:
                pass

        return info

async def test_swift_integration():
    """Test Swift-integrated JARVIS"""
    print("🚀 Testing JARVIS with Swift Integration")
    print("=" * 50)
    
    # Initialize Swift-enhanced JARVIS
    jarvis = JARVISSwiftIntegratedAssistant("Sir")
    
    # Get system info
    info = jarvis.get_system_info()
    print(f"\n📊 System Status:")
    print(f"• Swift Classifier: {'✅' if info['swift_enabled'] else '❌'}")
    print(f"• Vision System: ✅")
    print(f"• Components: {len([v for v in info['components'].values() if v])}/5 active")
    
    # Test vision command
    print("\n🎯 Testing Vision Command with Swift:")
    response = await jarvis.process_vision_command("What can you see on my screen?")
    
    print(f"\n🗣️ JARVIS Response:")
    print(response.verbal_response)
    
    # Check if Swift was used
    if response.visual_context.get('swift_classification'):
        print(f"\n🏃 Swift Classification:")
        swift_data = response.visual_context['swift_classification']
        print(f"• Category: {swift_data.get('category')}")
        print(f"• Confidence: {swift_data.get('confidence', 0):.0%}")
        print(f"• Action: {swift_data.get('action')}")
    
    print("\n✅ Swift integration test complete!")

if __name__ == "__main__":
    asyncio.run(test_swift_integration())