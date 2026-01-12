"""
JARVIS Workflow Execution Engine - Advanced Multi-Step Task Orchestrator
Executes complex workflows with dependency management, error recovery, and learning
"""

import asyncio
import json
import os
from typing import Dict, List, Any, Optional, Callable, Union, Tuple
from dataclasses import dataclass, field, asdict
from enum import Enum
from datetime import datetime, timedelta
import logging
from concurrent.futures import ThreadPoolExecutor
import aiofiles
import numpy as np
from collections import defaultdict, deque

from .workflow_parser import Workflow, WorkflowAction, ActionType

# Import managed executor for clean shutdown
try:
    from core.thread_manager import ManagedThreadPoolExecutor
    _HAS_MANAGED_EXECUTOR = True
except ImportError:
    _HAS_MANAGED_EXECUTOR = False

logger = logging.getLogger(__name__)


class ExecutionStatus(Enum):
    """Status of workflow or action execution"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRY = "retry"
    CANCELLED = "cancelled"
    PAUSED = "paused"


class ExecutionStrategy(Enum):
    """Strategy for executing workflow actions"""
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
    CONDITIONAL = "conditional"
    ADAPTIVE = "adaptive"


@dataclass
class ExecutionContext:
    """Maintains context across workflow execution"""
    workflow_id: str
    start_time: datetime
    user_id: str
    variables: Dict[str, Any] = field(default_factory=dict)
    results: Dict[int, Any] = field(default_factory=dict)  # action_index -> result
    errors: List[Dict[str, Any]] = field(default_factory=list)
    state: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def set_variable(self, key: str, value: Any):
        """Set a context variable"""
        self.variables[key] = value
        
    def get_variable(self, key: str, default: Any = None) -> Any:
        """Get a context variable"""
        return self.variables.get(key, default)
        
    def add_result(self, action_index: int, result: Any):
        """Store action result"""
        self.results[action_index] = result
        
    def add_error(self, action_index: int, error: Exception, retry_count: int = 0):
        """Record an error"""
        self.errors.append({
            'action_index': action_index,
            'error': str(error),
            'type': type(error).__name__,
            'timestamp': datetime.now(),
            'retry_count': retry_count
        })


@dataclass
class ActionResult:
    """Result of executing a single action"""
    action_index: int
    status: ExecutionStatus
    result: Any = None
    error: Optional[str] = None
    duration: float = 0.0  # seconds
    retry_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowResult:
    """Result of executing a complete workflow"""
    workflow_id: str
    status: ExecutionStatus
    total_duration: float
    action_results: List[ActionResult]
    context: ExecutionContext
    success_rate: float = 0.0
    error_recovery_count: int = 0
    

class WorkflowLearningEngine:
    """ML-based learning engine for workflow optimization"""
    
    def __init__(self, learning_config_path: str = None):
        """Initialize learning engine with configuration"""
        self.config_path = learning_config_path or os.path.join(
            os.path.dirname(__file__), 'config', 'workflow_learning.json'
        )
        self.patterns = defaultdict(lambda: {'success': 0, 'failure': 0, 'avg_duration': 0})
        self.user_preferences = defaultdict(dict)
        self.workflow_history = deque(maxlen=1000)
        self.action_success_rates = defaultdict(lambda: {'success': 0, 'total': 0})
        self.load_learning_data()
        
    def load_learning_data(self):
        """Load historical learning data"""
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, 'r') as f:
                    data = json.load(f)
                    self.patterns = defaultdict(lambda: {'success': 0, 'failure': 0, 'avg_duration': 0}, data.get('patterns', {}))
                    self.user_preferences = defaultdict(dict, data.get('preferences', {}))
                    self.action_success_rates = defaultdict(lambda: {'success': 0, 'total': 0}, data.get('success_rates', {}))
        except Exception as e:
            logger.error(f"Failed to load learning data: {e}")
            
    async def save_learning_data(self):
        """Save learning data asynchronously"""
        try:
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            data = {
                'patterns': dict(self.patterns),
                'preferences': dict(self.user_preferences),
                'success_rates': dict(self.action_success_rates),
                'last_updated': datetime.now().isoformat()
            }
            async with aiofiles.open(self.config_path, 'w') as f:
                await f.write(json.dumps(data, indent=2))
        except Exception as e:
            logger.error(f"Failed to save learning data: {e}")
            
    def record_workflow_execution(self, workflow: Workflow, result: WorkflowResult):
        """Record workflow execution for learning"""
        # Update patterns
        pattern_key = self._get_pattern_key(workflow)
        if result.status == ExecutionStatus.COMPLETED:
            self.patterns[pattern_key]['success'] += 1
        else:
            self.patterns[pattern_key]['failure'] += 1
            
        # Update average duration
        current_avg = self.patterns[pattern_key]['avg_duration']
        current_count = self.patterns[pattern_key]['success'] + self.patterns[pattern_key]['failure'] - 1
        if current_count > 0:
            self.patterns[pattern_key]['avg_duration'] = (
                (current_avg * current_count + result.total_duration) / (current_count + 1)
            )
        else:
            self.patterns[pattern_key]['avg_duration'] = result.total_duration
            
        # Update action success rates
        for action_result in result.action_results:
            action_key = f"{workflow.actions[action_result.action_index].action_type.value}"
            self.action_success_rates[action_key]['total'] += 1
            if action_result.status == ExecutionStatus.COMPLETED:
                self.action_success_rates[action_key]['success'] += 1
                
        # Add to history
        self.workflow_history.append({
            'workflow': asdict(workflow),
            'result': asdict(result),
            'timestamp': datetime.now().isoformat()
        })
        
        # Async save
        asyncio.create_task(self.save_learning_data())
        
    def get_workflow_success_probability(self, workflow: Workflow) -> float:
        """Predict workflow success probability based on historical data"""
        pattern_key = self._get_pattern_key(workflow)
        pattern_data = self.patterns.get(pattern_key, {'success': 0, 'failure': 0})
        
        total = pattern_data['success'] + pattern_data['failure']
        if total == 0:
            # No history, use action-level predictions
            return self._predict_from_actions(workflow)
            
        return pattern_data['success'] / total
        
    def _predict_from_actions(self, workflow: Workflow) -> float:
        """Predict success based on individual action success rates"""
        if not workflow.actions:
            return 0.0
            
        probabilities = []
        for action in workflow.actions:
            action_key = action.action_type.value
            action_data = self.action_success_rates.get(action_key, {'success': 0, 'total': 0})
            
            if action_data['total'] > 0:
                probabilities.append(action_data['success'] / action_data['total'])
            else:
                # Default probability for unknown actions
                probabilities.append(0.8)
                
        # Workflow succeeds if all actions succeed (simplified model)
        return np.prod(probabilities)
        
    def get_optimized_execution_strategy(self, workflow: Workflow, context: ExecutionContext) -> ExecutionStrategy:
        """Determine optimal execution strategy based on learning"""
        # Check if workflow has dependencies
        has_dependencies = any(action.dependencies for action in workflow.actions)
        
        if not has_dependencies and len(workflow.actions) > 2:
            # Can parallelize if success rate is high
            success_prob = self.get_workflow_success_probability(workflow)
            if success_prob > 0.85:
                return ExecutionStrategy.PARALLEL
                
        # Check user preferences
        user_prefs = self.user_preferences.get(context.user_id, {})
        if user_prefs.get('prefer_fast_execution') and not has_dependencies:
            return ExecutionStrategy.PARALLEL
            
        # Default to adaptive for complex workflows
        if workflow.complexity == "complex":
            return ExecutionStrategy.ADAPTIVE
            
        return ExecutionStrategy.SEQUENTIAL
        
    def suggest_alternative_actions(self, failed_action: WorkflowAction) -> List[WorkflowAction]:
        """Suggest alternative actions based on learning"""
        alternatives = []
        
        # Find successful patterns with similar intent
        action_type = failed_action.action_type.value
        for pattern_key, pattern_data in self.patterns.items():
            if action_type in pattern_key and pattern_data['success'] > pattern_data['failure']:
                # This is a simplified suggestion - in production, use ML
                alternatives.append(WorkflowAction(
                    action_type=failed_action.action_type,
                    target=failed_action.target,
                    parameters=failed_action.parameters,
                    description=f"Alternative: {pattern_key}"
                ))
                
        return alternatives[:3]  # Return top 3 alternatives
        
    def _get_pattern_key(self, workflow: Workflow) -> str:
        """Generate pattern key for workflow"""
        action_types = [action.action_type.value for action in workflow.actions]
        return "_".join(action_types)


class ActionExecutorRegistry:
    """Registry for action executors - configuration-driven, no hardcoding"""
    
    def __init__(self, config_path: str = None):
        """Initialize with configuration file"""
        self.executors: Dict[ActionType, Callable] = {}
        self.config_path = config_path or os.path.join(
            os.path.dirname(__file__), 'config', 'action_executors.json'
        )
        self.load_executors()
        
    def load_executors(self):
        """Load executor configurations"""
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, 'r') as f:
                    config = json.load(f)
                    
                # Dynamically load executors based on config
                for action_type_str, executor_config in config.items():
                    try:
                        action_type = ActionType(action_type_str)
                        module_name = executor_config['module']
                        function_name = executor_config['function']
                        
                        # Dynamic import
                        module = __import__(module_name, fromlist=[function_name])
                        executor_func = getattr(module, function_name)
                        
                        self.register(action_type, executor_func)
                    except Exception as e:
                        logger.error(f"Failed to load executor for {action_type_str}: {e}")
        except Exception as e:
            logger.error(f"Failed to load executor config: {e}")
            
        # Register default executors if not loaded from config
        self._register_default_executors()
        
    def _register_default_executors(self):
        """Register default executors for core actions"""
        from .action_executors import (
            unlock_system, open_application, perform_search,
            check_resource, create_item, mute_notifications
        )
        
        default_executors = {
            ActionType.UNLOCK: unlock_system,
            ActionType.OPEN_APP: open_application,
            ActionType.SEARCH: perform_search,
            ActionType.CHECK: check_resource,
            ActionType.CREATE: create_item,
            ActionType.MUTE: mute_notifications
        }
        
        for action_type, executor in default_executors.items():
            if action_type not in self.executors:
                self.register(action_type, executor)
                
    def register(self, action_type: ActionType, executor: Callable):
        """Register an executor for an action type"""
        self.executors[action_type] = executor
        logger.info(f"Registered executor for {action_type.value}")
        
    def get_executor(self, action_type: ActionType) -> Optional[Callable]:
        """Get executor for action type"""
        return self.executors.get(action_type)


class WorkflowExecutionEngine:
    """Advanced workflow execution engine with learning and optimization"""
    
    def __init__(self, config_path: str = None):
        """Initialize execution engine"""
        self.config_path = config_path or os.path.join(
            os.path.dirname(__file__), 'config', 'workflow_engine.json'
        )
        self.config = self._load_config()
        
        # Initialize components
        self.executor_registry = ActionExecutorRegistry()
        self.learning_engine = WorkflowLearningEngine()
        if _HAS_MANAGED_EXECUTOR:
            self.thread_pool = ManagedThreadPoolExecutor(max_workers=self.config.get('max_workers', 10), name='workflow_engine')
        else:
            self.thread_pool = ThreadPoolExecutor(max_workers=self.config.get('max_workers', 10))
        self.active_workflows: Dict[str, Workflow] = {}
        self.execution_contexts: Dict[str, ExecutionContext] = {}
        
        # WebSocket connections for real-time updates
        self.websocket_connections: Dict[str, Any] = {}
        
    def _load_config(self) -> Dict[str, Any]:
        """Load engine configuration"""
        default_config = {
            'max_workers': 10,
            'default_timeout': 30,
            'max_retries': 3,
            'parallel_threshold': 3,
            'learning_enabled': True,
            'real_time_updates': True,
            'error_recovery_strategies': ['retry', 'alternative', 'skip', 'abort']
        }
        
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, 'r') as f:
                    loaded_config = json.load(f)
                    default_config.update(loaded_config)
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            
        return default_config
        
    async def execute_workflow(self, workflow: Workflow, user_id: str, 
                             websocket: Optional[Any] = None) -> WorkflowResult:
        """Execute a complete workflow"""
        workflow_id = f"{user_id}_{datetime.now().timestamp()}"
        
        # Initialize context
        context = ExecutionContext(
            workflow_id=workflow_id,
            start_time=datetime.now(),
            user_id=user_id,
            metadata={
                'original_command': workflow.original_command,
                'complexity': workflow.complexity,
                'predicted_success': self.learning_engine.get_workflow_success_probability(workflow)
            }
        )
        
        # Store for monitoring
        self.active_workflows[workflow_id] = workflow
        self.execution_contexts[workflow_id] = context
        if websocket:
            self.websocket_connections[workflow_id] = websocket
            
        try:
            # Send initial update
            await self._send_update(workflow_id, {
                'type': 'workflow_started',
                'workflow_id': workflow_id,
                'total_actions': len(workflow.actions),
                'estimated_duration': workflow.estimated_duration,
                'complexity': workflow.complexity
            })
            
            # Determine execution strategy
            strategy = self.learning_engine.get_optimized_execution_strategy(workflow, context)
            logger.info(f"Using {strategy.value} execution strategy for workflow {workflow_id}")
            
            # Execute based on strategy
            if strategy == ExecutionStrategy.SEQUENTIAL:
                action_results = await self._execute_sequential(workflow, context)
            elif strategy == ExecutionStrategy.PARALLEL:
                action_results = await self._execute_parallel(workflow, context)
            elif strategy == ExecutionStrategy.ADAPTIVE:
                action_results = await self._execute_adaptive(workflow, context)
            else:
                action_results = await self._execute_sequential(workflow, context)
                
            # Calculate final status
            failed_count = sum(1 for r in action_results if r.status == ExecutionStatus.FAILED)
            completed_count = sum(1 for r in action_results if r.status == ExecutionStatus.COMPLETED)
            
            if failed_count == 0:
                final_status = ExecutionStatus.COMPLETED
            elif completed_count > 0:
                final_status = ExecutionStatus.COMPLETED  # Partial success
            else:
                final_status = ExecutionStatus.FAILED
                
            # Create result
            total_duration = (datetime.now() - context.start_time).total_seconds()
            result = WorkflowResult(
                workflow_id=workflow_id,
                status=final_status,
                total_duration=total_duration,
                action_results=action_results,
                context=context,
                success_rate=completed_count / len(workflow.actions) if workflow.actions else 0,
                error_recovery_count=sum(r.retry_count for r in action_results)
            )
            
            # Record for learning
            if self.config.get('learning_enabled', True):
                self.learning_engine.record_workflow_execution(workflow, result)
                
            # Send completion update
            await self._send_update(workflow_id, {
                'type': 'workflow_completed',
                'workflow_id': workflow_id,
                'status': final_status.value,
                'total_duration': total_duration,
                'success_rate': result.success_rate
            })
            
            return result
            
        except Exception as e:
            logger.error(f"Workflow execution failed: {e}")
            return WorkflowResult(
                workflow_id=workflow_id,
                status=ExecutionStatus.FAILED,
                total_duration=(datetime.now() - context.start_time).total_seconds(),
                action_results=[],
                context=context,
                success_rate=0.0
            )
        finally:
            # Cleanup
            self.active_workflows.pop(workflow_id, None)
            self.execution_contexts.pop(workflow_id, None)
            self.websocket_connections.pop(workflow_id, None)
            
    async def _execute_sequential(self, workflow: Workflow, context: ExecutionContext) -> List[ActionResult]:
        """Execute actions sequentially"""
        results = []
        
        for i, action in enumerate(workflow.actions):
            # Check dependencies
            if not self._can_execute(action, i, results):
                result = ActionResult(
                    action_index=i,
                    status=ExecutionStatus.SKIPPED,
                    error="Dependencies not satisfied"
                )
                results.append(result)
                continue
                
            # Execute action
            result = await self._execute_action(action, i, context)
            results.append(result)
            
            # Stop on critical failure
            if result.status == ExecutionStatus.FAILED and not action.optional:
                break
                
        return results
        
    async def _execute_parallel(self, workflow: Workflow, context: ExecutionContext) -> List[ActionResult]:
        """Execute independent actions in parallel"""
        results = [None] * len(workflow.actions)
        
        # Group actions by dependencies
        execution_groups = self._group_by_dependencies(workflow)
        
        for group in execution_groups:
            # Execute group in parallel
            tasks = []
            for action_index in group:
                action = workflow.actions[action_index]
                task = self._execute_action(action, action_index, context)
                tasks.append((action_index, task))
                
            # Wait for group completion
            for action_index, task in tasks:
                result = await task
                results[action_index] = result
                
        return results
        
    async def _execute_adaptive(self, workflow: Workflow, context: ExecutionContext) -> List[ActionResult]:
        """Adaptive execution based on real-time conditions"""
        results = []
        execution_plan = list(range(len(workflow.actions)))
        
        while execution_plan:
            # Find next executable actions
            executable = []
            for idx in execution_plan:
                if self._can_execute(workflow.actions[idx], idx, results):
                    executable.append(idx)
                    
            if not executable:
                # No actions can execute - dependency deadlock
                logger.error("Dependency deadlock detected")
                break
                
            # Decide how many to run in parallel based on system load
            system_load = await self._get_system_load()
            parallel_count = min(len(executable), self._get_parallel_capacity(system_load))
            
            # Execute batch
            batch = executable[:parallel_count]
            tasks = []
            
            for idx in batch:
                action = workflow.actions[idx]
                task = self._execute_action(action, idx, context)
                tasks.append((idx, task))
                execution_plan.remove(idx)
                
            # Collect results
            for idx, task in tasks:
                result = await task
                results.append(result)
                
        return results
        
    async def _execute_action(self, action: WorkflowAction, index: int, 
                            context: ExecutionContext) -> ActionResult:
        """Execute a single action with error handling and retries"""
        start_time = datetime.now()
        retry_count = 0
        
        # Send action start update
        await self._send_update(context.workflow_id, {
            'type': 'action_started',
            'action_index': index,
            'action_type': action.action_type.value,
            'description': action.description
        })
        
        while retry_count <= action.retry_count:
            try:
                # Get executor
                executor = self.executor_registry.get_executor(action.action_type)
                if not executor:
                    raise ValueError(f"No executor for action type {action.action_type}")
                    
                # Execute with timeout
                result = await asyncio.wait_for(
                    executor(action, context),
                    timeout=action.timeout
                )
                
                # Success
                duration = (datetime.now() - start_time).total_seconds()
                action_result = ActionResult(
                    action_index=index,
                    status=ExecutionStatus.COMPLETED,
                    result=result,
                    duration=duration,
                    retry_count=retry_count
                )
                
                # Store result in context
                context.add_result(index, result)
                
                # Send success update
                await self._send_update(context.workflow_id, {
                    'type': 'action_completed',
                    'action_index': index,
                    'duration': duration,
                    'retry_count': retry_count
                })
                
                return action_result
                
            except asyncio.TimeoutError:
                error = "Action timed out"
                logger.error(f"Action {index} timed out after {action.timeout}s")
                
            except Exception as e:
                error = str(e)
                logger.error(f"Action {index} failed: {e}")
                
            # Record error
            context.add_error(index, Exception(error), retry_count)
            
            # Try alternative or retry
            if retry_count < action.retry_count:
                retry_count += 1
                await asyncio.sleep(1)  # Brief pause before retry
                
                # Send retry update
                await self._send_update(context.workflow_id, {
                    'type': 'action_retry',
                    'action_index': index,
                    'retry_count': retry_count,
                    'error': error
                })
            else:
                # Try alternative actions if available
                alternatives = self.learning_engine.suggest_alternative_actions(action)
                if alternatives and self.config.get('use_alternatives', True):
                    logger.info(f"Trying alternative action for {index}")
                    # Execute first alternative (simplified)
                    action = alternatives[0]
                    retry_count = 0
                    continue
                    
        # Failed after all retries
        duration = (datetime.now() - start_time).total_seconds()
        
        # Send failure update
        await self._send_update(context.workflow_id, {
            'type': 'action_failed',
            'action_index': index,
            'error': error,
            'duration': duration
        })
        
        return ActionResult(
            action_index=index,
            status=ExecutionStatus.FAILED,
            error=error,
            duration=duration,
            retry_count=retry_count
        )
        
    def _can_execute(self, action: WorkflowAction, index: int, 
                    completed_results: List[ActionResult]) -> bool:
        """Check if action can be executed based on dependencies"""
        if not action.dependencies:
            return True
            
        completed_indices = {r.action_index for r in completed_results 
                           if r and r.status == ExecutionStatus.COMPLETED}
        
        return all(dep in completed_indices for dep in action.dependencies)
        
    def _group_by_dependencies(self, workflow: Workflow) -> List[List[int]]:
        """Group actions by dependency levels for parallel execution"""
        groups = []
        remaining = set(range(len(workflow.actions)))
        completed = set()
        
        while remaining:
            # Find actions that can execute now
            current_group = []
            for idx in remaining:
                action = workflow.actions[idx]
                if all(dep in completed for dep in action.dependencies):
                    current_group.append(idx)
                    
            if not current_group:
                # Circular dependency or error
                logger.error("Circular dependency detected")
                break
                
            groups.append(current_group)
            completed.update(current_group)
            remaining -= set(current_group)
            
        return groups
        
    async def _get_system_load(self) -> float:
        """Get current system load for adaptive execution"""
        try:
            # Simplified - in production, use proper system metrics
            import psutil
            return psutil.cpu_percent(interval=0.1) / 100.0
        except Exception:
            return 0.5  # Default medium load
            
    def _get_parallel_capacity(self, system_load: float) -> int:
        """Determine parallel execution capacity based on load"""
        max_parallel = self.config.get('max_workers', 10)
        
        if system_load < 0.3:
            return max_parallel
        elif system_load < 0.6:
            return max(1, max_parallel // 2)
        else:
            return max(1, max_parallel // 4)
            
    async def _send_update(self, workflow_id: str, update: Dict[str, Any]):
        """Send real-time update via WebSocket"""
        if not self.config.get('real_time_updates', True):
            return
            
        websocket = self.websocket_connections.get(workflow_id)
        if websocket:
            try:
                update['timestamp'] = datetime.now().isoformat()
                await websocket.send_json(update)
            except Exception as e:
                logger.error(f"Failed to send update: {e}")
                
    async def pause_workflow(self, workflow_id: str) -> bool:
        """Pause a running workflow"""
        if workflow_id in self.active_workflows:
            # Implementation for pausing
            logger.info(f"Pausing workflow {workflow_id}")
            return True
        return False
        
    async def resume_workflow(self, workflow_id: str) -> bool:
        """Resume a paused workflow"""
        if workflow_id in self.active_workflows:
            # Implementation for resuming
            logger.info(f"Resuming workflow {workflow_id}")
            return True
        return False
        
    async def cancel_workflow(self, workflow_id: str) -> bool:
        """Cancel a running workflow"""
        if workflow_id in self.active_workflows:
            # Implementation for cancellation
            logger.info(f"Cancelling workflow {workflow_id}")
            self.active_workflows.pop(workflow_id, None)
            return True
        return False