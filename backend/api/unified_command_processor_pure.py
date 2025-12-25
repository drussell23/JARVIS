"""
Unified Command Processor - Pure Intelligence Version
Simplified to use Claude's natural understanding instead of pattern matching

The old way: Complex routing logic, pattern matching, multiple handlers
The new way: Claude understands everything naturally

v6.0 Enhancements (Cross-Repo Integration):
- SOP Enforcement (MetaGPT-inspired) for structured task execution
- Wisdom Patterns (Fabric-inspired) for enhanced prompts
- Context enrichment via Cross-Repo Intelligence Hub
"""

import logging
import os
from typing import Dict, Any, Optional, Tuple, List
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

# =============================================================================
# SOP ENFORCEMENT INTEGRATION (v6.0 - MetaGPT-Inspired)
# =============================================================================
try:
    from backend.intelligence.sop_enforcement import (
        StandardOperatingProcedure,
        ActionNode,
        ExecutionMode,
        create_code_review_sop,
        create_feature_implementation_sop,
        SOPConfig,
    )
    SOP_AVAILABLE = True
except ImportError:
    SOP_AVAILABLE = False

# =============================================================================
# CROSS-REPO HUB INTEGRATION (v6.0)
# =============================================================================
try:
    from backend.intelligence.cross_repo_hub import (
        get_intelligence_hub,
        CrossRepoIntelligenceHub,
    )
    HUB_AVAILABLE = True
except ImportError:
    HUB_AVAILABLE = False

# Environment-driven configuration
SOP_ENABLED = os.getenv("JARVIS_SOP_ENABLED", "true").lower() == "true"
HUB_ENRICHMENT_ENABLED = os.getenv("JARVIS_HUB_ENRICHMENT_ENABLED", "true").lower() == "true"


@dataclass
class PureUnifiedContext:
    """Simplified unified context - Claude maintains the real context"""
    last_command: Optional[str] = None
    last_response: Optional[str] = None
    last_command_time: Optional[datetime] = None
    session_start: datetime = None
    
    def __post_init__(self):
        if not self.session_start:
            self.session_start = datetime.now()


class PureUnifiedCommandProcessor:
    """
    Unified processor using pure Claude intelligence.
    No routing tables, no pattern matching - Claude understands intent naturally.

    v6.0 Enhancements:
    - SOP Enforcement for structured multi-step tasks
    - Cross-Repo Hub for context enrichment
    - Available SOPs: code_review, feature_implementation
    """

    # Keywords that trigger SOP-based processing
    SOP_TRIGGERS = {
        "review": "code_review",
        "code review": "code_review",
        "review this code": "code_review",
        "review my code": "code_review",
        "implement": "feature_implementation",
        "add feature": "feature_implementation",
        "create feature": "feature_implementation",
        "build feature": "feature_implementation",
    }

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key
        self.context = PureUnifiedContext()
        self.vision_handler = None
        self._initialized = False

        # v6.0: SOP Enforcement
        self._sop_registry: Dict[str, StandardOperatingProcedure] = {}
        self._hub: Optional[CrossRepoIntelligenceHub] = None
        self._hub_initialized = False

    async def _ensure_sop_initialized(self):
        """Lazily initialize available SOPs."""
        if self._sop_registry:
            return

        if SOP_AVAILABLE and SOP_ENABLED:
            try:
                config = SOPConfig()
                self._sop_registry["code_review"] = create_code_review_sop(config)
                self._sop_registry["feature_implementation"] = create_feature_implementation_sop(config)
                logger.info(f"✅ SOP Enforcement initialized: {list(self._sop_registry.keys())}")
            except Exception as e:
                logger.debug(f"SOP initialization failed: {e}")

    async def _ensure_hub_initialized(self):
        """Lazily initialize Cross-Repo Hub."""
        if self._hub_initialized:
            return self._hub is not None

        if HUB_AVAILABLE and HUB_ENRICHMENT_ENABLED:
            try:
                self._hub = await get_intelligence_hub()
                self._hub_initialized = True
                logger.info("✅ Cross-Repo Hub connected for context enrichment")
                return True
            except Exception as e:
                logger.debug(f"Hub initialization failed: {e}")
                self._hub_initialized = True
                return False
        return False

    def _detect_sop_trigger(self, command: str) -> Optional[str]:
        """Detect if command should trigger an SOP."""
        command_lower = command.lower()
        for trigger, sop_name in self.SOP_TRIGGERS.items():
            if trigger in command_lower:
                return sop_name
        return None
        
    async def _ensure_initialized(self):
        """Lazy initialization of handlers"""
        if not self._initialized:
            try:
                # Initialize pure vision handler
                from .vision_command_handler import vision_command_handler
                self.vision_handler = vision_command_handler
                
                # Initialize with API key if available
                if self.api_key and hasattr(self.vision_handler, 'initialize_intelligence'):
                    await self.vision_handler.initialize_intelligence(self.api_key)
                    
                self._initialized = True
                logger.info("[PURE PROCESSOR] Initialized with pure intelligence")
                
            except Exception as e:
                logger.error(f"Failed to initialize pure processor: {e}")
                
    async def process_command(self, command_text: str, websocket=None, llm=None) -> Dict[str, Any]:
        """
        Process any command using pure Claude intelligence.
        Claude figures out what the user wants - no pattern matching needed.

        v6.0: Now supports SOP-based execution for complex tasks like:
        - Code review (structured analysis with security, performance, quality checks)
        - Feature implementation (requirements -> design -> code -> tests)
        """
        logger.info(f"[PURE] Processing: '{command_text}'")

        # Ensure initialized
        await self._ensure_initialized()
        await self._ensure_sop_initialized()

        # Update context
        self.context.last_command = command_text
        self.context.last_command_time = datetime.now()

        try:
            # v6.0: Check if this command should trigger an SOP
            sop_name = self._detect_sop_trigger(command_text)
            if sop_name and sop_name in self._sop_registry and llm:
                logger.info(f"[SOP] Triggering '{sop_name}' SOP for: '{command_text}'")

                # Get enriched context from hub if available
                enriched_context = command_text
                if await self._ensure_hub_initialized() and self._hub:
                    try:
                        enrichment = await self._hub.enrich_context(command_text)
                        if enrichment.get("enrichments"):
                            enriched_context = f"{command_text}\n\n## Intelligence Context\n{enrichment['enrichments']}"
                    except Exception as enrich_err:
                        logger.debug(f"Context enrichment skipped: {enrich_err}")

                # Execute the SOP
                sop = self._sop_registry[sop_name]
                sop_result = await sop.execute(llm, enriched_context, ExecutionMode.BY_ORDER)

                # Format the response
                steps_completed = sum(1 for r in sop_result.values() if r.is_success())
                total_steps = len(sop_result)

                response_parts = [f"Completed {steps_completed}/{total_steps} steps of {sop_name}:"]
                for step_name, result in sop_result.items():
                    status_emoji = "✅" if result.is_success() else "❌"
                    response_parts.append(f"  {status_emoji} {step_name}")
                    if result.output:
                        # Include key parts of output
                        if isinstance(result.output, dict):
                            for key, value in list(result.output.items())[:3]:
                                response_parts.append(f"      • {key}: {str(value)[:100]}")

                self.context.last_response = "\n".join(response_parts)

                return {
                    'success': steps_completed == total_steps,
                    'response': "\n".join(response_parts),
                    'command_type': f'sop_{sop_name}',
                    'sop_results': {k: v.to_dict() if hasattr(v, 'to_dict') else str(v) for k, v in sop_result.items()},
                    'steps_completed': steps_completed,
                    'total_steps': total_steps,
                    'pure_intelligence': True,
                    'sop_enhanced': True,
                }

            # For vision-related queries, use vision intelligence
            # But we don't need to detect this - Claude will understand
            if self._might_be_vision_related(command_text) and self.vision_handler:
                result = await self.vision_handler.handle_command(command_text)

                # Update context
                self.context.last_response = result.get('response', '')

                return {
                    'success': True,
                    'response': result.get('response', ''),
                    'command_type': 'vision_intelligence',
                    'pure_intelligence': True,
                    **result
                }
            else:
                # For non-vision commands, we would use other pure intelligence handlers
                # For now, indicate it needs implementation
                return {
                    'success': False,
                    'response': await self._get_natural_fallback_response(command_text),
                    'command_type': 'not_implemented',
                    'pure_intelligence': True,
                    'available_sops': list(self._sop_registry.keys()) if self._sop_registry else [],
                }

        except Exception as e:
            logger.error(f"Pure processor error: {e}", exc_info=True)
            return {
                'success': False,
                'response': await self._get_natural_error_response(command_text, str(e)),
                'command_type': 'error',
                'error': str(e),
                'pure_intelligence': True
            }
            
    def _might_be_vision_related(self, command: str) -> bool:
        """
        Simple heuristic to decide if we should try vision handler.
        In a fully pure system, we wouldn't even need this - Claude would route.
        """
        # Very broad check - let Claude figure out the specifics
        vision_indicators = [
            'see', 'look', 'screen', 'monitor', 'show', 'what', 
            'window', 'open', 'error', 'terminal', 'battery',
            'can you', 'do you', 'analyze', 'tell me'
        ]
        
        command_lower = command.lower()
        return any(indicator in command_lower for indicator in vision_indicators)
        
    async def _get_natural_fallback_response(self, command: str) -> str:
        """
        Even fallback responses are natural and varied.
        In production, this would use Claude to generate a natural response.
        """
        # This would be replaced with actual Claude call
        responses = [
            f"I understand you're asking about '{command}', but I don't have that capability enabled yet.",
            f"That's an interesting request about '{command}'. This feature is still being developed.",
            f"I heard '{command}', but I'm not equipped to handle that type of request yet.",
        ]
        
        # In production: return await claude.generate_natural_response(command, context="capability_not_available")
        import random
        return random.choice(responses)
        
    async def _get_natural_error_response(self, command: str, error: str) -> str:
        """
        Natural error responses - no templates.
        In production, Claude would generate these based on the error context.
        """
        # This would be replaced with actual Claude call
        return f"I encountered an issue while processing your request about '{command}'. Let me help you troubleshoot this."
        
    def get_session_stats(self) -> Dict[str, Any]:
        """Get session statistics"""
        session_duration = (datetime.now() - self.context.session_start).total_seconds()
        
        stats = {
            'session_duration_seconds': session_duration,
            'last_command': self.context.last_command,
            'last_command_age': (
                (datetime.now() - self.context.last_command_time).total_seconds()
                if self.context.last_command_time else None
            ),
            'pure_intelligence': True
        }
        
        # Add vision intelligence stats if available
        if self.vision_handler and hasattr(self.vision_handler, 'get_intelligence_stats'):
            stats['vision_intelligence'] = self.vision_handler.get_intelligence_stats()
            
        return stats


# Singleton instance
_pure_unified_processor = None

def get_pure_unified_processor(api_key: Optional[str] = None) -> PureUnifiedCommandProcessor:
    """Get or create the pure unified command processor"""
    global _pure_unified_processor
    if _pure_unified_processor is None:
        _pure_unified_processor = PureUnifiedCommandProcessor(api_key)
    return _pure_unified_processor