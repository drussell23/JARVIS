#!/usr/bin/env python3
"""
Vision Action Handler for JARVIS - Dynamic ML-Based System
Zero hardcoding - all actions are discovered and learned
"""

import logging
import asyncio
from typing import Dict, Any, Optional, Tuple, List, Callable
from dataclasses import dataclass, field
import json
from pathlib import Path
import inspect
import importlib
from datetime import datetime

logger = logging.getLogger(__name__)

@dataclass
class VisionActionResult:
    """Result of a vision action"""
    success: bool
    description: str
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    confidence: float = 1.0
    alternative_actions: List[str] = None

@dataclass
class DynamicAction:
    """Dynamically discovered vision action"""
    name: str
    handler: Callable
    description: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)
    examples: List[str] = field(default_factory=list)
    success_rate: float = 0.0
    usage_count: int = 0
    learned_patterns: List[str] = field(default_factory=list)

class VisionActionHandler:
    """Dynamic vision action handler with ML-based routing"""
    
    def __init__(self):
        self.vision_system_v2 = None
        self.dynamic_engine = None
        self.discovered_actions: Dict[str, DynamicAction] = {}
        self.learning_data = {"patterns": {}, "feedback": [], "success_rates": {}}
        self.vision_modules = {}
        self.workspace_analyzer = None
        
        # Lazy load Vision System v2.0 to prevent circular imports
        self.vision_system_v2 = None
        self._vision_v2_initialized = False
        
        # Initialize system
        self._init_dynamic_system()
        
    def _ensure_vision_v2_initialized(self):
        """Lazy initialize Vision System v2.0"""
        if self._vision_v2_initialized:
            return
            
        self._vision_v2_initialized = True
        try:
            from vision.vision_system_v2 import get_vision_system_v2
            self.vision_system_v2 = get_vision_system_v2()
            logger.info("Vision System v2.0 initialized with ML-based understanding")
        except ImportError:
            logger.warning("Vision System v2.0 not available, using legacy approach")
        
        # Workspace analyzer will be loaded lazily when needed
        self._workspace_analyzer_initialized = False
        
    def _ensure_workspace_analyzer_initialized(self):
        """Lazy initialize workspace analyzer"""
        if self._workspace_analyzer_initialized:
            return
            
        self._workspace_analyzer_initialized = True
        try:
            from vision.workspace_analyzer import WorkspaceAnalyzer
            self.workspace_analyzer = WorkspaceAnalyzer()
            logger.info("Multi-window workspace analyzer initialized")
        except Exception as e:
            logger.warning(f"Workspace analyzer not available: {e}")
        
    def _init_dynamic_system(self):
        """Initialize dynamic vision system"""
        # Lazy load dynamic engine
        self.dynamic_engine = None
        self._dynamic_engine_initialized = False
            
        # Discover all available vision capabilities
        self._discover_all_vision_capabilities()
        
        # Load any learned data
        self._load_learning_data()
        
    def _ensure_dynamic_engine_initialized(self):
        """Lazy initialize dynamic vision engine"""
        if self._dynamic_engine_initialized:
            return
            
        self._dynamic_engine_initialized = True
        try:
            from vision.dynamic_vision_engine import get_dynamic_vision_engine
            self.dynamic_engine = get_dynamic_vision_engine()
            logger.info("Using dynamic vision engine for ML-based routing")
        except ImportError:
            logger.info("Dynamic engine not available, using autonomous discovery")
        
    def _discover_all_vision_capabilities(self):
        """Discover all vision capabilities from available modules"""
        vision_module_names = [
            'vision.intelligent_vision_integration',
            'vision.screen_vision',
            'vision.jarvis_workspace_integration',
            'vision.enhanced_monitoring',
            'vision.screen_capture_fallback',
            'vision.claude_vision_analyzer',
            'vision.advanced_vision_system'
        ]
        
        for module_name in vision_module_names:
            try:
                module = importlib.import_module(module_name)
                self.vision_modules[module_name] = module
                self._analyze_module_for_actions(module_name, module)
            except ImportError:
                continue
                
        logger.info(f"Discovered {len(self.discovered_actions)} vision actions")
        
    def _analyze_module_for_actions(self, module_name: str, module):
        """Analyze a module and discover vision-related actions"""
        for item_name in dir(module):
            if item_name.startswith('_'):
                continue
                
            item = getattr(module, item_name)
            
            # Check if it's a class
            if inspect.isclass(item):
                self._analyze_class_for_actions(module_name, item_name, item)
                
            # Check if it's a function
            elif inspect.isfunction(item) or inspect.iscoroutinefunction(item):
                if self._is_vision_related(item_name, item):
                    self._register_function_as_action(f"{module_name}.{item_name}", item)
                    
    def _analyze_class_for_actions(self, module_name: str, class_name: str, cls):
        """Analyze a class for vision-related methods"""
        # Try to instantiate the class
        instance = None
        try:
            # Try common initialization patterns
            if 'api_key' in inspect.signature(cls).parameters:
                instance = cls(api_key=os.getenv("ANTHROPIC_API_KEY"))
            else:
                instance = cls()
        except Exception:
            # If we can't instantiate, analyze static/class methods
            pass
            
        # Analyze methods
        for method_name in dir(cls):
            if method_name.startswith('_'):
                continue
                
            method = getattr(cls, method_name)
            
            if inspect.ismethod(method) or inspect.isfunction(method):
                if self._is_vision_related(method_name, method):
                    full_name = f"{module_name}.{class_name}.{method_name}"
                    
                    # Create a wrapper that handles instantiation
                    if instance:
                        bound_method = getattr(instance, method_name)
                        self._register_function_as_action(full_name, bound_method)
                    else:
                        # Create a lazy wrapper
                        def make_wrapper(cls, method_name):
                            async def wrapper(**kwargs):
                                inst = cls()
                                method = getattr(inst, method_name)
                                if asyncio.iscoroutinefunction(method):
                                    return await method(**kwargs)
                                else:
                                    return method(**kwargs)
                            return wrapper
                            
                        wrapper = make_wrapper(cls, method_name)
                        self._register_function_as_action(full_name, wrapper)
                        
    def _is_vision_related(self, name: str, obj) -> bool:
        """Determine if an object is vision-related using NLP"""
        # Check name
        vision_keywords = ['screen', 'vision', 'capture', 'window', 'display', 'visual', 
                          'image', 'analyze', 'describe', 'look', 'see', 'view', 'monitor',
                          'workspace', 'screenshot', 'ocr', 'detect', 'recognition']
        
        name_lower = name.lower()
        if any(keyword in name_lower for keyword in vision_keywords):
            return True
            
        # Check docstring
        doc = inspect.getdoc(obj)
        if doc:
            doc_lower = doc.lower()
            if any(keyword in doc_lower for keyword in vision_keywords):
                return True
                
        return False
        
    def _register_function_as_action(self, name: str, func):
        """Register a function as a vision action"""
        # Extract metadata
        doc = inspect.getdoc(func) or f"Auto-discovered: {name}"
        
        # Extract parameters
        sig = inspect.signature(func)
        params = {}
        for param_name, param in sig.parameters.items():
            if param_name not in ['self', 'cls']:
                params[param_name] = {
                    'type': str(param.annotation) if param.annotation != param.empty else 'Any',
                    'default': param.default if param.default != param.empty else None,
                    'required': param.default == param.empty
                }
                
        action = DynamicAction(
            name=name,
            handler=func,
            description=doc.split('\n')[0],
            parameters=params
        )
        
        self.discovered_actions[name] = action
        logger.debug(f"Registered vision action: {name}")
        
    async def process_vision_action(self, action_name: str, params: Dict[str, Any] = None) -> VisionActionResult:
        """Process any vision action dynamically"""
        params = params or {}
        
        # If Vision System v2.0 is available, use it
        if self.vision_system_v2:
            # Convert action_name to natural language for ML processing
            command = self._action_to_natural_language(action_name, params)
            self._ensure_vision_v2_initialized()
            response = await self.vision_system_v2.process_command(command, params)
            
            return VisionActionResult(
                success=response.success,
                description=response.message,
                data=response.data,
                confidence=response.confidence
            )
        
        # If using dynamic engine, route through it
        elif self.dynamic_engine:
            # Convert action_name to natural language for ML processing
            command = self._action_to_natural_language(action_name, params)
            self._ensure_dynamic_engine_initialized()
            response, metadata = await self.dynamic_engine.process_vision_command(command, params)
            
            return VisionActionResult(
                success=metadata.get('success', True),
                description=response,
                data=metadata,
                confidence=metadata.get('confidence', 1.0)
            )
            
        # Otherwise, try direct action execution
        if action_name in self.discovered_actions:
            return await self._execute_action(action_name, params)
            
        # Try fuzzy matching
        best_match = self._find_best_action_match(action_name)
        if best_match:
            logger.info(f"Fuzzy matched '{action_name}' to '{best_match}'")
            return await self._execute_action(best_match, params)
            
        # Check if it's a multi-window query
        if self.workspace_analyzer and self._is_multi_window_query(params):
            return await self.analyze_multi_windows(params)
        
        # No match found
        alternatives = self._suggest_alternatives(action_name)
        return VisionActionResult(
            success=False,
            description=f"Unknown vision action: {action_name}",
            error="Action not found",
            alternative_actions=alternatives
        )
        
    def _action_to_natural_language(self, action_name: str, params: Dict[str, Any]) -> str:
        """Convert action name and params to natural language"""
        # Convert snake_case to words
        words = action_name.replace('_', ' ').replace('.', ' ')
        
        # Add parameter context
        if 'target' in params:
            words += f" {params['target']}"
        elif 'window' in params:
            words += f" {params['window']} window"
            
        return words
        
    async def _execute_action(self, action_name: str, params: Dict[str, Any]) -> VisionActionResult:
        """Execute a discovered action"""
        action = self.discovered_actions[action_name]
        
        # Update usage stats
        action.usage_count += 1
        
        try:
            # Prepare parameters
            call_params = {}
            for param_name, param_info in action.parameters.items():
                if param_name in params:
                    call_params[param_name] = params[param_name]
                elif param_info.get('default') is not None:
                    call_params[param_name] = param_info['default']
                elif param_info.get('required', False):
                    raise ValueError(f"Required parameter '{param_name}' not provided")
                    
            # Execute handler
            if asyncio.iscoroutinefunction(action.handler):
                result = await action.handler(**call_params)
            else:
                result = action.handler(**call_params)
                
            # Process result
            if isinstance(result, str):
                response = result
                success = True
            elif isinstance(result, dict):
                response = result.get('description', str(result))
                success = result.get('success', True)
            elif hasattr(result, 'description'):
                response = result.description
                success = getattr(result, 'success', True)
            else:
                response = str(result)
                success = True
                
            # Update success rate
            old_rate = action.success_rate
            action.success_rate = (old_rate * (action.usage_count - 1) + (1 if success else 0)) / action.usage_count
            
            # Save learning data
            self._save_execution_result(action_name, params, success)
            
            return VisionActionResult(
                success=success,
                description=response,
                data={'action': action_name, 'params': params}
            )
            
        except Exception as e:
            logger.error(f"Error executing action {action_name}: {e}")
            
            # Update failure rate
            old_rate = action.success_rate
            action.success_rate = (old_rate * (action.usage_count - 1)) / action.usage_count
            
            return VisionActionResult(
                success=False,
                description=f"Failed to execute vision action",
                error=str(e)
            )
            
    def _find_best_action_match(self, query: str) -> Optional[str]:
        """Find best matching action using fuzzy matching"""
        query_lower = query.lower()
        best_match = None
        best_score = 0
        
        for action_name in self.discovered_actions:
            # Simple fuzzy matching - could use more sophisticated algorithms
            action_lower = action_name.lower()
            
            # Check if query is substring
            if query_lower in action_lower:
                score = len(query_lower) / len(action_lower)
                if score > best_score:
                    best_score = score
                    best_match = action_name
                    
            # Check word overlap
            query_words = set(query_lower.split('_'))
            action_words = set(action_lower.replace('.', '_').split('_'))
            overlap = len(query_words & action_words)
            if overlap > 0:
                score = overlap / len(query_words)
                if score > best_score:
                    best_score = score
                    best_match = action_name
                    
        return best_match if best_score > 0.5 else None
        
    def _suggest_alternatives(self, query: str) -> List[str]:
        """Suggest alternative actions"""
        suggestions = []
        query_words = set(query.lower().split('_'))
        
        for action_name, action in self.discovered_actions.items():
            action_words = set(action_name.lower().replace('.', '_').split('_'))
            if query_words & action_words:
                suggestions.append(action_name)
                
        # Sort by usage count
        suggestions.sort(key=lambda x: self.discovered_actions[x].usage_count, reverse=True)
        
        return suggestions[:5]
        
    def _save_execution_result(self, action_name: str, params: Dict[str, Any], success: bool):
        """Save execution result for learning"""
        self.learning_data["patterns"][action_name] = self.learning_data["patterns"].get(action_name, [])
        self.learning_data["patterns"][action_name].append({
            "params": params,
            "success": success,
            "timestamp": datetime.now().isoformat()
        })
        
        # Periodically save to disk
        if sum(len(patterns) for patterns in self.learning_data["patterns"].values()) % 10 == 0:
            self._save_learning_data()
            
    def _save_learning_data(self):
        """Save learning data to disk"""
        save_path = Path("backend/data/vision_action_learning.json")
        save_path.parent.mkdir(exist_ok=True)
        
        data = {
            "patterns": self.learning_data["patterns"],
            "action_stats": {
                name: {
                    "success_rate": action.success_rate,
                    "usage_count": action.usage_count,
                    "learned_patterns": action.learned_patterns
                }
                for name, action in self.discovered_actions.items()
            }
        }
        
        try:
            with open(save_path, 'w') as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Error saving learning data: {e}")
            
    def _load_learning_data(self):
        """Load previously learned data"""
        save_path = Path("backend/data/vision_action_learning.json")
        
        if not save_path.exists():
            return
            
        try:
            with open(save_path, 'r') as f:
                data = json.load(f)
                
            self.learning_data["patterns"] = data.get("patterns", {})
            
            # Apply stats to actions
            for name, stats in data.get("action_stats", {}).items():
                if name in self.discovered_actions:
                    action = self.discovered_actions[name]
                    action.success_rate = stats.get("success_rate", 0.0)
                    action.usage_count = stats.get("usage_count", 0)
                    action.learned_patterns = stats.get("learned_patterns", [])
                    
        except Exception as e:
            logger.error(f"Error loading learning data: {e}")
            
    # Compatibility methods for existing code
    async def describe_screen(self, params: Dict[str, Any] = None) -> VisionActionResult:
        """Describe screen - routes through Vision System v2.0"""
        params = params or {}
        
        # Use Vision System v2.0 if available
        if self.vision_system_v2:
            command = params.get('query', 'describe what is on my screen')
            self._ensure_vision_v2_initialized()
            response = await self.vision_system_v2.process_command(command, params)
            
            return VisionActionResult(
                success=response.success,
                description=response.message,
                data=response.data,
                confidence=response.confidence
            )
        
        # Try unified vision system
        try:
            from vision.unified_vision_system import get_unified_vision_system
            unified = get_unified_vision_system()
            
            # Create request
            command = params.get('query', 'describe what is on my screen')
            response = await unified.process_vision_request(command)
            
            return VisionActionResult(
                success=response.success,
                description=response.description,
                data=response.data,
                confidence=response.confidence
            )
        except Exception:
            # Fallback to direct processing
            return await self.process_vision_action("describe_screen", params)

    async def analyze_window(self, params: Dict[str, Any] = None) -> VisionActionResult:
        """Analyze window - routes through Vision System v2.0"""
        params = params or {}
        
        # Use Vision System v2.0 if available
        if self.vision_system_v2:
            window = params.get('target', 'current')
            command = f"analyze the {window} window"
            self._ensure_vision_v2_initialized()
            response = await self.vision_system_v2.process_command(command, params)
            
            return VisionActionResult(
                success=response.success,
                description=response.message,
                data=response.data,
                confidence=response.confidence
            )
        
        # Try unified vision system
        try:
            from vision.unified_vision_system import get_unified_vision_system
            unified = get_unified_vision_system()
            
            # Create request
            window = params.get('target', 'current')
            command = f"analyze the {window} window"
            response = await unified.process_vision_request(command)
            
            return VisionActionResult(
                success=response.success,
                description=response.description,
                data=response.data,
                confidence=response.confidence
            )
        except Exception:
            # Fallback to direct processing
            return await self.process_vision_action("analyze_window", params)

    async def check_screen(self, params: Dict[str, Any] = None) -> VisionActionResult:
        """Check screen - routes through Vision System v2.0"""
        params = params or {}
        
        # Use Vision System v2.0 if available
        if self.vision_system_v2:
            target = params.get('target', 'notifications')
            command = f"check my screen for {target}"
            self._ensure_vision_v2_initialized()
            response = await self.vision_system_v2.process_command(command, params)
            
            return VisionActionResult(
                success=response.success,
                description=response.message,
                data=response.data,
                confidence=response.confidence
            )
        
        # Try unified vision system
        try:
            from vision.unified_vision_system import get_unified_vision_system
            unified = get_unified_vision_system()
            
            # Create request
            target = params.get('target', 'notifications')
            command = f"check my screen for {target}"
            response = await unified.process_vision_request(command)
            
            return VisionActionResult(
                success=response.success,
                description=response.description,
                data=response.data,
                confidence=response.confidence
            )
        except Exception:
            # Fallback to direct processing
            return await self.process_vision_action("check_screen", params)

    def _is_multi_window_query(self, params: Dict[str, Any]) -> bool:
        """Check if the query is asking about multiple windows"""
        query = params.get('query', '').lower()
        multi_window_keywords = [
            'other window', 'other screen', 'all window', 'all screen',
            'multiple window', 'multiple screen', 'every window', 'every screen',
            'workspace', 'everything', 'all application', 'other application',
            'besides', 'except', 'not looking at', 'not focused', 'entire desktop',
            'all my monitors', 'other monitors', 'different windows'
        ]
        
        return any(keyword in query for keyword in multi_window_keywords)
    
    async def analyze_multi_windows(self, params: Dict[str, Any] = None) -> VisionActionResult:
        """Analyze multiple windows using workspace analyzer"""
        params = params or {}
        query = params.get('query', 'What is happening on all my screens and windows?')
        
        try:
            logger.info(f"Analyzing multi-window workspace with query: {query}")
            
            # Use workspace analyzer for comprehensive analysis
            self._ensure_workspace_analyzer_initialized()
            result = await self.workspace_analyzer.analyze_workspace(query)
            
            # Build comprehensive description
            description_parts = []
            
            # Add primary task
            description_parts.append(result.focused_task)
            
            # Add workspace context
            if result.workspace_context:
                description_parts.append(f"\n\nWorkspace: {result.workspace_context}")
            
            # Add window relationships
            if result.window_relationships:
                description_parts.append("\n\nWindow Relationships:")
                for rel, details in list(result.window_relationships.items())[:3]:
                    if details:
                        description_parts.append(f"- {details[0]}")
            
            # Add notifications
            if result.important_notifications:
                description_parts.append("\n\nNotifications:")
                for notif in result.important_notifications[:3]:
                    description_parts.append(f"- {notif}")
            
            # Add suggestions
            if result.suggestions:
                description_parts.append("\n\nSuggestions:")
                for suggestion in result.suggestions[:2]:
                    description_parts.append(f"- {suggestion}")
            
            description = "\n".join(description_parts)
            
            return VisionActionResult(
                success=True,
                confidence=result.confidence,
                description=description,
                data={
                    'analysis_type': 'multi_window',
                    'windows_analyzed': len(result.window_relationships) if result.window_relationships else 0,
                    'focused_task': result.focused_task,
                    'has_notifications': len(result.important_notifications) > 0
                }
            )
            
        except Exception as e:
            logger.error(f"Error in multi-window analysis: {e}", exc_info=True)
            return VisionActionResult(
                success=False,
                confidence=0.0,
                description=f"I encountered an error analyzing multiple windows: {str(e)}",
                error=str(e),
                data={'analysis_type': 'multi_window'}
            )

# Import os for env vars
import os

# Singleton instance
_vision_handler = None

def get_vision_action_handler() -> VisionActionHandler:
    """Get singleton vision action handler"""
    global _vision_handler
    if _vision_handler is None:
        _vision_handler = VisionActionHandler()
    return _vision_handler