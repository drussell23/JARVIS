# DEPRECATED: superseded by governance/preemption_fsm.py. Quarantine date: 2026-03-11
#!/usr/bin/env python3
"""
System States and Transitions for JARVIS Autonomous System

This module manages the overall state machine for autonomous operations, providing
a comprehensive framework for tracking system states, component health, and state
transitions. It includes timeout handling, callback mechanisms, and health monitoring
for all system components.

The state machine follows a hierarchical approach where the system has an overall
state (SystemState) and individual components have their own states (ComponentState).
State transitions are validated and logged for debugging and monitoring purposes.

Example:
    >>> from autonomy.system_states import state_manager
    >>> await state_manager.initialize_system()
    >>> await state_manager.start_monitoring()
    >>> status = state_manager.get_system_status()
"""

import asyncio
import logging
from typing import Dict, List, Optional, Any, Callable, Set
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
import json

logger = logging.getLogger(__name__)

class SystemState(Enum):
    """Overall system states for the autonomous system.
    
    Defines the high-level operational states that the system can be in.
    Each state represents a different phase of autonomous operation with
    specific behaviors and valid transitions.
    """
    INITIALIZING = auto()  # System is starting up and initializing components
    IDLE = auto()          # System is ready but not actively processing
    MONITORING = auto()    # System is actively monitoring for inputs/events
    PROCESSING = auto()    # System is processing input data
    DECIDING = auto()      # System is making decisions based on processed data
    EXECUTING = auto()     # System is executing decided actions
    ERROR_RECOVERY = auto() # System is recovering from an error state
    PAUSED = auto()        # System is temporarily paused by user
    SHUTDOWN = auto()      # System is shutting down (terminal state)

class ComponentState(Enum):
    """Individual component states within the system.
    
    Represents the operational state of individual system components
    such as vision pipeline, decision engine, etc.
    """
    NOT_INITIALIZED = auto()  # Component has not been initialized
    READY = auto()           # Component is ready for operation
    ACTIVE = auto()          # Component is actively processing
    BUSY = auto()            # Component is busy and cannot accept new tasks
    ERROR = auto()           # Component has encountered an error
    OFFLINE = auto()         # Component is offline or disconnected

class TransitionReason(Enum):
    """Reasons for state transitions.
    
    Provides context for why a state transition occurred, useful for
    debugging, logging, and understanding system behavior.
    """
    USER_REQUEST = "user_request"        # User explicitly requested the transition
    AUTOMATIC = "automatic"              # System automatically transitioned
    ERROR = "error"                      # Transition due to error condition
    RECOVERY = "recovery"                # Transition as part of error recovery
    TIMEOUT = "timeout"                  # Transition due to state timeout
    COMPLETION = "completion"            # Transition due to task completion
    EXTERNAL_TRIGGER = "external_trigger" # External event triggered transition

@dataclass
class StateTransition:
    """Record of a state transition.
    
    Captures all relevant information about a state transition including
    the states involved, reason for transition, timing, and additional metadata.
    
    Attributes:
        from_state: The state being transitioned from
        to_state: The state being transitioned to
        reason: The reason for the transition
        timestamp: When the transition occurred
        metadata: Additional context-specific information
    """
    from_state: SystemState
    to_state: SystemState
    reason: TransitionReason
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)
    

@dataclass
class ComponentStatus:
    """Status of a system component.
    
    Tracks the current state and health metrics of individual system components.
    Provides methods to assess component health and determine if attention is needed.
    
    Attributes:
        name: Unique identifier for the component
        state: Current operational state of the component
        last_update: Timestamp of the last status update
        health_score: Health metric from 0.0 (unhealthy) to 1.0 (perfect health)
        error_count: Number of errors encountered by this component
        metadata: Additional component-specific information
    """
    name: str
    state: ComponentState
    last_update: datetime = field(default_factory=datetime.now)
    health_score: float = 1.0  # 0.0 to 1.0
    error_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def is_healthy(self) -> bool:
        """Check if the component is in a healthy state.
        
        Returns:
            bool: True if component is healthy (ready/active with good health score)
        """
        return self.state in [ComponentState.READY, ComponentState.ACTIVE] and self.health_score > 0.7
        
    @property
    def needs_attention(self) -> bool:
        """Check if the component needs attention.
        
        Returns:
            bool: True if component is in error state, has low health, or high error count
        """
        return self.state == ComponentState.ERROR or self.health_score < 0.5 or self.error_count > 5

class SystemStateManager:
    """Manages system states and transitions.
    
    The central state management system that coordinates overall system state,
    component states, state transitions, timeouts, and health monitoring.
    Provides callback mechanisms for state changes and comprehensive status reporting.
    
    Attributes:
        current_state: The current system state
        previous_state: The previous system state (None if first state)
        state_history: List of all state transitions
        components: Dictionary of registered components and their status
        valid_transitions: Mapping of valid state transitions
        state_callbacks: Callbacks to execute when entering specific states
        transition_callbacks: Callbacks to execute on any state transition
        state_timeouts: Timeout values for each state in seconds
        state_entered_time: When the current state was entered
        timeout_task: Current timeout monitoring task
    """
    
    def __init__(self):
        """Initialize the system state manager.
        
        Sets up the initial state, transition rules, callbacks, and timeout handling.
        """
        self.current_state = SystemState.INITIALIZING
        self.previous_state = None
        self.state_history: List[StateTransition] = []
        self.components: Dict[str, ComponentStatus] = {}
        
        # State transition rules
        self.valid_transitions = {
            SystemState.INITIALIZING: {
                SystemState.IDLE,
                SystemState.ERROR_RECOVERY
            },
            SystemState.IDLE: {
                SystemState.MONITORING,
                SystemState.PAUSED,
                SystemState.SHUTDOWN
            },
            SystemState.MONITORING: {
                SystemState.PROCESSING,
                SystemState.IDLE,
                SystemState.PAUSED,
                SystemState.ERROR_RECOVERY
            },
            SystemState.PROCESSING: {
                SystemState.DECIDING,
                SystemState.MONITORING,
                SystemState.ERROR_RECOVERY
            },
            SystemState.DECIDING: {
                SystemState.EXECUTING,
                SystemState.MONITORING,
                SystemState.ERROR_RECOVERY
            },
            SystemState.EXECUTING: {
                SystemState.MONITORING,
                SystemState.ERROR_RECOVERY,
                SystemState.PROCESSING
            },
            SystemState.ERROR_RECOVERY: {
                SystemState.IDLE,
                SystemState.MONITORING,
                SystemState.SHUTDOWN
            },
            SystemState.PAUSED: {
                SystemState.MONITORING,
                SystemState.IDLE,
                SystemState.SHUTDOWN
            },
            SystemState.SHUTDOWN: set()  # Terminal state
        }
        
        # State callbacks
        self.state_callbacks: Dict[SystemState, List[Callable]] = {
            state: [] for state in SystemState
        }
        self.transition_callbacks: List[Callable] = []
        
        # State timeouts
        self.state_timeouts = {
            SystemState.INITIALIZING: 30,  # seconds
            SystemState.PROCESSING: 10,
            SystemState.DECIDING: 5,
            SystemState.EXECUTING: 60,
            SystemState.ERROR_RECOVERY: 120
        }
        
        # Timeout tracking
        self.state_entered_time = datetime.now()
        self.timeout_task = None
        
    def register_component(self, name: str, initial_state: ComponentState = ComponentState.NOT_INITIALIZED):
        """Register a system component for monitoring.
        
        Args:
            name: Unique identifier for the component
            initial_state: Initial state of the component
            
        Example:
            >>> manager.register_component('vision_pipeline', ComponentState.READY)
        """
        self.components[name] = ComponentStatus(
            name=name,
            state=initial_state
        )
        logger.info(f"Registered component: {name}")
        
    def update_component_state(self, name: str, state: ComponentState, 
                             health_score: Optional[float] = None,
                             metadata: Optional[Dict[str, Any]] = None):
        """Update a component's state and health metrics.
        
        Args:
            name: Name of the component to update
            state: New state for the component
            health_score: New health score (0.0-1.0), if provided
            metadata: Additional metadata to update
            
        Example:
            >>> manager.update_component_state(
            ...     'vision_pipeline', 
            ...     ComponentState.ACTIVE,
            ...     health_score=0.95,
            ...     metadata={'fps': 30}
            ... )
        """
        if name not in self.components:
            logger.warning(f"Unknown component: {name}")
            return
            
        component = self.components[name]
        component.state = state
        component.last_update = datetime.now()
        
        if health_score is not None:
            component.health_score = max(0.0, min(1.0, health_score))
            
        if metadata:
            component.metadata.update(metadata)
            
        # Track errors
        if state == ComponentState.ERROR:
            component.error_count += 1
            
        logger.debug(f"Component {name} state updated to {state.name}")
        
        # Check if component issues should trigger system state change
        self._check_component_health()
        
    def _check_component_health(self):
        """Check overall component health and trigger state changes if needed.
        
        Monitors all components and triggers error recovery if critical components
        are unhealthy. Critical components are those essential for system operation.
        """
        unhealthy_components = [
            c for c in self.components.values() 
            if c.needs_attention
        ]
        
        # If critical components are unhealthy, trigger error recovery
        critical_components = ['vision_pipeline', 'decision_engine', 'action_queue']
        critical_unhealthy = [
            c for c in unhealthy_components 
            if c.name in critical_components
        ]
        
        if critical_unhealthy and self.current_state not in [
            SystemState.ERROR_RECOVERY, 
            SystemState.SHUTDOWN
        ]:
            logger.warning(f"Critical components unhealthy: {[c.name for c in critical_unhealthy]}")
            asyncio.create_task(
                self.transition_to(SystemState.ERROR_RECOVERY, TransitionReason.ERROR)
            )
            
    async def transition_to(self, new_state: SystemState, 
                          reason: TransitionReason,
                          metadata: Optional[Dict[str, Any]] = None) -> bool:
        """Transition to a new system state.
        
        Validates the transition, updates the current state, manages timeouts,
        and executes registered callbacks.
        
        Args:
            new_state: The state to transition to
            reason: The reason for the transition
            metadata: Additional context for the transition
            
        Returns:
            bool: True if transition was successful, False otherwise
            
        Example:
            >>> success = await manager.transition_to(
            ...     SystemState.MONITORING, 
            ...     TransitionReason.USER_REQUEST
            ... )
        """
        if new_state == self.current_state:
            logger.debug(f"Already in state {new_state.name}")
            return True
            
        # Check if transition is valid
        if new_state not in self.valid_transitions.get(self.current_state, set()):
            logger.error(
                f"Invalid transition: {self.current_state.name} -> {new_state.name}"
            )
            return False
            
        # Create transition record
        transition = StateTransition(
            from_state=self.current_state,
            to_state=new_state,
            reason=reason,
            metadata=metadata or {}
        )
        
        # Execute transition
        self.previous_state = self.current_state
        self.current_state = new_state
        self.state_entered_time = datetime.now()
        self.state_history.append(transition)
        
        # Cancel previous timeout
        if self.timeout_task:
            self.timeout_task.cancel()
            
        # Set new timeout if applicable
        if new_state in self.state_timeouts:
            self.timeout_task = asyncio.create_task(
                self._handle_state_timeout(new_state)
            )
            
        logger.info(
            f"State transition: {transition.from_state.name} -> {transition.to_state.name} "
            f"(reason: {reason.value})"
        )
        
        # Execute callbacks
        await self._execute_state_callbacks(new_state)
        await self._execute_transition_callbacks(transition)
        
        return True
        
    async def _handle_state_timeout(self, state: SystemState):
        """Handle state timeout by transitioning to appropriate recovery state.
        
        Args:
            state: The state that timed out
        """
        timeout = self.state_timeouts[state]
        await asyncio.sleep(timeout)
        
        # Check if still in the same state
        if self.current_state == state:
            logger.warning(f"State {state.name} timed out after {timeout}s")
            
            # Determine recovery action
            if state == SystemState.INITIALIZING:
                await self.transition_to(SystemState.ERROR_RECOVERY, TransitionReason.TIMEOUT)
            elif state in [SystemState.PROCESSING, SystemState.DECIDING]:
                await self.transition_to(SystemState.MONITORING, TransitionReason.TIMEOUT)
            elif state == SystemState.EXECUTING:
                await self.transition_to(SystemState.ERROR_RECOVERY, TransitionReason.TIMEOUT)
                
    async def _execute_state_callbacks(self, state: SystemState):
        """Execute callbacks for entering a state.
        
        Args:
            state: The state that was entered
        """
        for callback in self.state_callbacks.get(state, []):
            try:
                await callback(state)
            except Exception as e:
                logger.error(f"State callback error: {e}")
                
    async def _execute_transition_callbacks(self, transition: StateTransition):
        """Execute callbacks for state transitions.
        
        Args:
            transition: The transition that occurred
        """
        for callback in self.transition_callbacks:
            try:
                await callback(transition)
            except Exception as e:
                logger.error(f"Transition callback error: {e}")
                
    def add_state_callback(self, state: SystemState, callback: Callable):
        """Add callback for entering a specific state.
        
        Args:
            state: The state to monitor
            callback: Async function to call when entering the state
            
        Example:
            >>> async def on_monitoring(state):
            ...     print(f"Started monitoring")
            >>> manager.add_state_callback(SystemState.MONITORING, on_monitoring)
        """
        self.state_callbacks[state].append(callback)
        
    def add_transition_callback(self, callback: Callable):
        """Add callback for any state transition.
        
        Args:
            callback: Async function to call on any transition
            
        Example:
            >>> async def log_transition(transition):
            ...     print(f"Transitioned: {transition.from_state} -> {transition.to_state}")
            >>> manager.add_transition_callback(log_transition)
        """
        self.transition_callbacks.append(callback)
        
    def get_system_status(self) -> Dict[str, Any]:
        """Get current system status including state and component information.
        
        Returns:
            Dict containing comprehensive system status information including:
            - current_state: Name of current system state
            - previous_state: Name of previous system state
            - state_duration: How long in current state (seconds)
            - components: Status of all registered components
            - healthy_components: Count of healthy components
            - unhealthy_components: Count of components needing attention
            - recent_transitions: Last 5 state transitions
            
        Example:
            >>> status = manager.get_system_status()
            >>> print(f"System is {status['current_state']}")
            >>> print(f"Healthy components: {status['healthy_components']}")
        """
        return {
            'current_state': self.current_state.name,
            'previous_state': self.previous_state.name if self.previous_state else None,
            'state_duration': (datetime.now() - self.state_entered_time).total_seconds(),
            'components': {
                name: {
                    'state': comp.state.name,
                    'health': comp.health_score,
                    'errors': comp.error_count,
                    'last_update': comp.last_update.isoformat()
                }
                for name, comp in self.components.items()
            },
            'healthy_components': sum(1 for c in self.components.values() if c.is_healthy),
            'unhealthy_components': sum(1 for c in self.components.values() if c.needs_attention),
            'recent_transitions': [
                {
                    'from': t.from_state.name,
                    'to': t.to_state.name,
                    'reason': t.reason.value,
                    'timestamp': t.timestamp.isoformat()
                }
                for t in self.state_history[-5:]  # Last 5 transitions
            ]
        }
        
    def can_transition_to(self, state: SystemState) -> bool:
        """Check if transition to given state is valid from current state.
        
        Args:
            state: The state to check transition validity for
            
        Returns:
            bool: True if transition is valid, False otherwise
            
        Example:
            >>> if manager.can_transition_to(SystemState.MONITORING):
            ...     await manager.transition_to(SystemState.MONITORING, TransitionReason.USER_REQUEST)
        """
        return state in self.valid_transitions.get(self.current_state, set())
        
    def get_available_transitions(self) -> Set[SystemState]:
        """Get all valid transitions from current state.
        
        Returns:
            Set of SystemState values that are valid transitions from current state
            
        Example:
            >>> available = manager.get_available_transitions()
            >>> print(f"Can transition to: {[s.name for s in available]}")
        """
        return self.valid_transitions.get(self.current_state, set())
        
    async def initialize_system(self) -> bool:
        """Initialize the system and all core components.
        
        Registers core components, simulates initialization process, and transitions
        to IDLE state when complete.
        
        Returns:
            bool: True if initialization was successful
            
        Example:
            >>> success = await manager.initialize_system()
            >>> if success:
            ...     print("System initialized successfully")
        """
        logger.info("Initializing system...")
        
        # Register core components
        core_components = [
            'vision_pipeline',
            'decision_engine',
            'action_queue',
            'websocket_manager',
            'behavior_manager'
        ]
        
        for component in core_components:
            self.register_component(component)
            
        # Simulate initialization
        await asyncio.sleep(1)
        
        # Update component states
        for component in core_components:
            self.update_component_state(
                component, 
                ComponentState.READY,
                health_score=1.0
            )
            
        # Transition to idle
        success = await self.transition_to(
            SystemState.IDLE,
            TransitionReason.COMPLETION
        )
        
        return success
        
    async def start_monitoring(self) -> bool:
        """Start system monitoring mode.
        
        Transitions from IDLE to MONITORING state if currently idle.
        
        Returns:
            bool: True if monitoring started successfully
            
        Example:
            >>> if await manager.start_monitoring():
            ...     print("Monitoring started")
        """
        if self.current_state != SystemState.IDLE:
            logger.warning(f"Cannot start monitoring from state {self.current_state.name}")
            return False
            
        return await self.transition_to(
            SystemState.MONITORING,
            TransitionReason.USER_REQUEST
        )
        
    async def pause_system(self) -> bool:
        """Pause the system operations.
        
        Transitions to PAUSED state from most operational states.
        
        Returns:
            bool: True if system was paused successfully
            
        Example:
            >>> await manager.pause_system()
        """
        if self.current_state in [SystemState.SHUTDOWN, SystemState.INITIALIZING]:
            return False
            
        return await self.transition_to(
            SystemState.PAUSED,
            TransitionReason.USER_REQUEST
        )
        
    async def resume_system(self) -> bool:
        """Resume from pause state.
        
        Transitions from PAUSED back to MONITORING state.
        
        Returns:
            bool: True if system was resumed successfully
            
        Example:
            >>> await manager.resume_system()
        """
        if self.current_state != SystemState.PAUSED:
            return False
            
        # Return to monitoring
        return await self.transition_to(
            SystemState.MONITORING,
            TransitionReason.USER_REQUEST
        )
        
    async def shutdown_system(self) -> bool:
        """Shutdown the system gracefully.
        
        Transitions to SHUTDOWN state, which is terminal.
        
        Returns:
            bool: True if shutdown was initiated successfully
            
        Example:
            >>> await manager.shutdown_system()
        """
        return await self.transition_to(
            SystemState.SHUTDOWN,
            TransitionReason.USER_REQUEST
        )

# Global state manager instance
state_manager = SystemStateManager()

async def test_state_system():
    """Test the state management system functionality.
    
    Comprehensive test that exercises state transitions, component management,
    callbacks, error handling, and status reporting.
    
    Example:
        >>> await test_state_system()
        🔄 Testing System State Manager
        ==========================================
        ...
        ✅ State system test complete!
    """
    print("🔄 Testing System State Manager")
    print("=" * 50)
    
    manager = SystemStateManager()
    
    # Add callbacks
    async def state_callback(state):
        print(f"   Entered state: {state.name}")
        
    async def transition_callback(transition):
        print(f"   Transition: {transition.from_state.name} -> {transition.to_state.name}")
        
    manager.add_state_callback(SystemState.MONITORING, state_callback)
    manager.add_transition_callback(transition_callback)
    
    # Initialize system
    print("\n🚀 Initializing system...")
    await manager.initialize_system()
    
    # Get status
    status = manager.get_system_status()
    print(f"\n📊 System Status:")
    print(f"   Current State: {status['current_state']}")
    print(f"   Components: {status['healthy_components']} healthy, {status['unhealthy_components']} unhealthy")
    
    # Test transitions
    print("\n🔄 Testing state transitions...")
    
    # Start monitoring
    success = await manager.start_monitoring()
    print(f"   Start monitoring: {'✓' if success else '✗'}")
    
    # Simulate processing
    await manager.transition_to(SystemState.PROCESSING, TransitionReason.AUTOMATIC)
    await asyncio.sleep(0.5)
    
    await manager.transition_to(SystemState.DECIDING, TransitionReason.COMPLETION)
    await asyncio.sleep(0.5)
    
    await manager.transition_to(SystemState.EXECUTING, TransitionReason.COMPLETION)
    
    # Simulate component error
    print("\n⚠️ Simulating component error...")
    manager.update_component_state(
        'vision_pipeline',
        ComponentState.ERROR,
        health_score=0.2
    )
    
    # Wait for error recovery
    await asyncio.sleep(1)
    
    # Final status
    final_status = manager.get_system_status()
    print(f"\n📊 Final Status:")
    print(f"   Current State: {final_status['current_state']}")
    print(f"   State History: {len(manager.state_history)} transitions")
    
    print("\n✅ State system test complete!")

if __name__ == "__main__":
    asyncio.run(test_state_system())