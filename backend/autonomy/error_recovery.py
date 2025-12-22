#!/usr/bin/env python3
"""
Error Handling and Recovery System for JARVIS
Manages errors, retries, and recovery strategies
"""

import asyncio
import logging
from typing import Dict, List, Optional, Any, Callable, Union
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
import traceback
import json

logger = logging.getLogger(__name__)


class ErrorSeverity(Enum):
    """Severity levels for errors"""
    LOW = auto()      # Minor issues, can be ignored
    MEDIUM = auto()   # Should be addressed but not critical
    HIGH = auto()     # Important errors that affect functionality
    CRITICAL = auto() # System-breaking errors requiring immediate action


class ErrorCategory(Enum):
    """Categories of errors"""
    VISION = "vision"
    OCR = "ocr"
    DECISION = "decision"
    EXECUTION = "execution"
    NETWORK = "network"
    PERMISSION = "permission"
    TIMEOUT = "timeout"
    RESOURCE = "resource"
    UNKNOWN = "unknown"


class RecoveryStrategy(Enum):
    """Recovery strategies for different error types"""
    RETRY = "retry"                   # Simple retry
    EXPONENTIAL_BACKOFF = "backoff"   # Retry with exponential backoff
    RESET_COMPONENT = "reset"         # Reset the failed component
    FALLBACK = "fallback"             # Use fallback method
    SKIP = "skip"                     # Skip the operation
    ALERT_USER = "alert"              # Alert user for manual intervention
    SHUTDOWN = "shutdown"             # Shutdown the system


@dataclass
class ErrorRecord:
    """Record of an error occurrence"""
    error_id: str
    category: ErrorCategory
    severity: ErrorSeverity
    message: str
    component: str
    timestamp: datetime = field(default_factory=datetime.now)
    stack_trace: Optional[str] = None
    context: Dict[str, Any] = field(default_factory=dict)
    recovery_attempts: int = 0
    resolved: bool = False
    resolution: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        return {
            'error_id': self.error_id,
            'category': self.category.value,
            'severity': self.severity.name,
            'message': self.message,
            'component': self.component,
            'timestamp': self.timestamp.isoformat(),
            'recovery_attempts': self.recovery_attempts,
            'resolved': self.resolved,
            'resolution': self.resolution
        }


@dataclass 
class RecoveryAction:
    """Action to take for recovery"""
    strategy: RecoveryStrategy
    max_attempts: int = 3
    delay: float = 1.0  # seconds
    backoff_factor: float = 2.0
    timeout: float = 30.0
    fallback_action: Optional[Callable] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class ErrorRecoveryManager:
    """Manages error handling and recovery"""
    
    def __init__(self):
        self.error_history: List[ErrorRecord] = []
        self.active_errors: Dict[str, ErrorRecord] = {}
        self.recovery_actions: Dict[str, RecoveryAction] = {}
        
        # Error patterns and their recovery strategies
        self.error_patterns = {
            # Vision errors
            (ErrorCategory.VISION, ErrorSeverity.HIGH): RecoveryAction(
                strategy=RecoveryStrategy.RESET_COMPONENT,
                max_attempts=2
            ),
            (ErrorCategory.VISION, ErrorSeverity.MEDIUM): RecoveryAction(
                strategy=RecoveryStrategy.RETRY,
                max_attempts=3,
                delay=2.0
            ),
            
            # OCR errors
            (ErrorCategory.OCR, ErrorSeverity.LOW): RecoveryAction(
                strategy=RecoveryStrategy.SKIP
            ),
            (ErrorCategory.OCR, ErrorSeverity.MEDIUM): RecoveryAction(
                strategy=RecoveryStrategy.FALLBACK
            ),
            
            # Network errors
            (ErrorCategory.NETWORK, ErrorSeverity.HIGH): RecoveryAction(
                strategy=RecoveryStrategy.EXPONENTIAL_BACKOFF,
                max_attempts=5,
                delay=1.0,
                backoff_factor=2.0
            ),
            
            # Permission errors
            (ErrorCategory.PERMISSION, ErrorSeverity.HIGH): RecoveryAction(
                strategy=RecoveryStrategy.ALERT_USER
            ),
            
            # Timeout errors
            (ErrorCategory.TIMEOUT, ErrorSeverity.MEDIUM): RecoveryAction(
                strategy=RecoveryStrategy.RETRY,
                max_attempts=2,
                delay=5.0
            ),
            
            # Critical errors
            (ErrorCategory.UNKNOWN, ErrorSeverity.CRITICAL): RecoveryAction(
                strategy=RecoveryStrategy.SHUTDOWN
            )
        }
        
        # Error callbacks
        self.error_callbacks: List[Callable] = []
        self.recovery_callbacks: List[Callable] = []
        
        # Component reset functions
        self.component_resets: Dict[str, Callable] = {}
        
        # Error statistics
        self.error_stats = {
            'total_errors': 0,
            'errors_by_category': {cat.value: 0 for cat in ErrorCategory},
            'errors_by_severity': {sev.name: 0 for sev in ErrorSeverity},
            'recovery_success_rate': 0.0
        }
        
    def register_component_reset(self, component: str, reset_function: Callable):
        """Register a reset function for a component"""
        self.component_resets[component] = reset_function
        logger.info(f"Registered reset function for component: {component}")
        
    async def handle_error(self, 
                          error: Exception,
                          component: str,
                          category: Optional[ErrorCategory] = None,
                          severity: Optional[ErrorSeverity] = None,
                          context: Optional[Dict[str, Any]] = None) -> ErrorRecord:
        """Handle an error and initiate recovery"""
        
        # Determine category and severity if not provided
        if not category:
            category = self._categorize_error(error)
        if not severity:
            severity = self._assess_severity(error, category)
            
        # Create error record
        error_record = ErrorRecord(
            error_id=f"{component}_{datetime.now().timestamp()}",
            category=category,
            severity=severity,
            message=str(error),
            component=component,
            stack_trace=traceback.format_exc(),
            context=context or {}
        )
        
        # Add to history and active errors
        self.error_history.append(error_record)
        self.active_errors[error_record.error_id] = error_record
        
        # Update statistics
        self.error_stats['total_errors'] += 1
        self.error_stats['errors_by_category'][category.value] += 1
        self.error_stats['errors_by_severity'][severity.name] += 1
        
        logger.error(
            f"Error in {component}: {error} "
            f"(Category: {category.value}, Severity: {severity.name})"
        )
        
        # Notify callbacks
        await self._notify_error_callbacks(error_record)
        
        # Initiate recovery
        await self._initiate_recovery(error_record)
        
        return error_record
        
    def _categorize_error(self, error: Exception) -> ErrorCategory:
        """Categorize an error based on its type and message"""
        error_msg = str(error).lower()
        error_type = type(error).__name__
        
        if 'vision' in error_msg or 'screen' in error_msg or 'capture' in error_msg:
            return ErrorCategory.VISION
        elif 'ocr' in error_msg or 'text' in error_msg or 'tesseract' in error_msg:
            return ErrorCategory.OCR
        elif 'decision' in error_msg or 'action' in error_msg:
            return ErrorCategory.DECISION
        elif 'network' in error_msg or 'connection' in error_msg or 'timeout' in error_type:
            return ErrorCategory.NETWORK
        elif 'permission' in error_msg or 'denied' in error_msg or 'unauthorized' in error_msg:
            return ErrorCategory.PERMISSION
        elif 'timeout' in error_msg:
            return ErrorCategory.TIMEOUT
        elif 'memory' in error_msg or 'resource' in error_msg:
            return ErrorCategory.RESOURCE
        else:
            return ErrorCategory.UNKNOWN
            
    def _assess_severity(self, error: Exception, category: ErrorCategory) -> ErrorSeverity:
        """Assess the severity of an error"""
        # Critical errors
        if isinstance(error, (SystemError, MemoryError)):
            return ErrorSeverity.CRITICAL
            
        # Category-based assessment
        if category == ErrorCategory.PERMISSION:
            return ErrorSeverity.HIGH
        elif category == ErrorCategory.NETWORK:
            return ErrorSeverity.MEDIUM
        elif category == ErrorCategory.OCR:
            return ErrorSeverity.LOW
        elif category == ErrorCategory.UNKNOWN:
            return ErrorSeverity.HIGH
            
        # Default to medium
        return ErrorSeverity.MEDIUM
        
    async def _initiate_recovery(self, error_record: ErrorRecord):
        """Initiate recovery for an error"""
        # Get recovery action
        recovery_key = (error_record.category, error_record.severity)
        recovery_action = self.error_patterns.get(
            recovery_key,
            RecoveryAction(strategy=RecoveryStrategy.SKIP)  # Default
        )
        
        # Store recovery action
        self.recovery_actions[error_record.error_id] = recovery_action
        
        # Execute recovery based on strategy
        if recovery_action.strategy == RecoveryStrategy.RETRY:
            await self._retry_recovery(error_record, recovery_action)
        elif recovery_action.strategy == RecoveryStrategy.EXPONENTIAL_BACKOFF:
            await self._backoff_recovery(error_record, recovery_action)
        elif recovery_action.strategy == RecoveryStrategy.RESET_COMPONENT:
            await self._reset_component(error_record, recovery_action)
        elif recovery_action.strategy == RecoveryStrategy.ALERT_USER:
            await self._alert_user(error_record)
        elif recovery_action.strategy == RecoveryStrategy.SKIP:
            await self._skip_operation(error_record)
        elif recovery_action.strategy == RecoveryStrategy.SHUTDOWN:
            await self._emergency_shutdown(error_record)
            
    async def _retry_recovery(self, error_record: ErrorRecord, action: RecoveryAction):
        """Simple retry recovery"""
        for attempt in range(action.max_attempts):
            error_record.recovery_attempts += 1
            
            logger.info(
                f"Retry attempt {attempt + 1}/{action.max_attempts} "
                f"for error {error_record.error_id}"
            )
            
            # Wait before retry
            await asyncio.sleep(action.delay)
            
            # Check if error is still active
            if error_record.error_id not in self.active_errors:
                break
                
            # TODO: Implement actual retry logic based on component
            # For now, mark as resolved after attempts
            if attempt == action.max_attempts - 1:
                await self._mark_resolved(
                    error_record, 
                    f"Exhausted retry attempts ({action.max_attempts})"
                )
                
    async def _backoff_recovery(self, error_record: ErrorRecord, action: RecoveryAction):
        """Exponential backoff recovery"""
        delay = action.delay
        
        for attempt in range(action.max_attempts):
            error_record.recovery_attempts += 1
            
            logger.info(
                f"Backoff attempt {attempt + 1}/{action.max_attempts} "
                f"(delay: {delay}s) for error {error_record.error_id}"
            )
            
            # Wait with exponential backoff
            await asyncio.sleep(delay)
            delay *= action.backoff_factor
            
            # Check if error is still active
            if error_record.error_id not in self.active_errors:
                break
                
    async def _reset_component(self, error_record: ErrorRecord, action: RecoveryAction):
        """Reset a component"""
        component = error_record.component
        
        if component in self.component_resets:
            logger.info(f"Resetting component: {component}")
            
            try:
                reset_func = self.component_resets[component]
                await reset_func()
                
                await self._mark_resolved(
                    error_record,
                    f"Component {component} reset successfully"
                )
            except Exception as e:
                logger.error(f"Failed to reset component {component}: {e}")
                error_record.recovery_attempts += 1
        else:
            logger.warning(f"No reset function registered for component: {component}")
            
    async def _alert_user(self, error_record: ErrorRecord):
        """Alert user for manual intervention"""
        logger.warning(
            f"User intervention required for error: {error_record.message}"
        )
        
        # Notify all error callbacks with alert flag
        for callback in self.error_callbacks:
            try:
                await callback(error_record, alert=True)
            except Exception as e:
                logger.error(f"Error callback failed: {e}")
                
    async def _skip_operation(self, error_record: ErrorRecord):
        """Skip the failed operation"""
        logger.info(f"Skipping operation due to error: {error_record.error_id}")
        
        await self._mark_resolved(
            error_record,
            "Operation skipped"
        )
        
    async def _emergency_shutdown(self, error_record: ErrorRecord):
        """Emergency shutdown due to critical error"""
        logger.critical(
            f"EMERGENCY SHUTDOWN initiated due to critical error: {error_record.message}"
        )
        
        # Notify all callbacks
        for callback in self.error_callbacks:
            try:
                await callback(error_record, shutdown=True)
            except:
                pass  # Ignore errors during shutdown
                
    async def _mark_resolved(self, error_record: ErrorRecord, resolution: str):
        """Mark an error as resolved"""
        error_record.resolved = True
        error_record.resolution = resolution
        
        # Remove from active errors
        if error_record.error_id in self.active_errors:
            del self.active_errors[error_record.error_id]
            
        logger.info(f"Error {error_record.error_id} resolved: {resolution}")
        
        # Notify recovery callbacks
        await self._notify_recovery_callbacks(error_record)
        
    async def _notify_error_callbacks(self, error_record: ErrorRecord):
        """Notify error callbacks"""
        for callback in self.error_callbacks:
            try:
                await callback(error_record)
            except Exception as e:
                logger.error(f"Error callback failed: {e}")
                
    async def _notify_recovery_callbacks(self, error_record: ErrorRecord):
        """Notify recovery callbacks"""
        for callback in self.recovery_callbacks:
            try:
                await callback(error_record)
            except Exception as e:
                logger.error(f"Recovery callback failed: {e}")
                
    def add_error_callback(self, callback: Callable):
        """Add callback for error notifications"""
        self.error_callbacks.append(callback)
        
    def add_recovery_callback(self, callback: Callable):
        """Add callback for recovery notifications"""
        self.recovery_callbacks.append(callback)
        
    def get_error_statistics(self) -> Dict[str, Any]:
        """Get error statistics"""
        # Calculate recovery success rate
        resolved_count = sum(1 for e in self.error_history if e.resolved)
        total_count = len(self.error_history)
        
        if total_count > 0:
            self.error_stats['recovery_success_rate'] = resolved_count / total_count
            
        return {
            **self.error_stats,
            'active_errors': len(self.active_errors),
            'recent_errors': [
                e.to_dict() for e in self.error_history[-10:]
            ]
        }
        
    def get_active_errors(self) -> List[ErrorRecord]:
        """Get list of active errors"""
        return list(self.active_errors.values())
        
    def clear_resolved_errors(self, older_than_hours: int = 24):
        """Clear old resolved errors from history"""
        cutoff = datetime.now() - timedelta(hours=older_than_hours)
        
        self.error_history = [
            e for e in self.error_history
            if not e.resolved or e.timestamp > cutoff
        ]
        
        logger.info(f"Cleared resolved errors older than {older_than_hours} hours")


# Global error recovery manager
error_manager = ErrorRecoveryManager()


async def test_error_recovery():
    """Test error recovery system"""
    print("🛡️ Testing Error Recovery System")
    print("=" * 50)
    
    manager = ErrorRecoveryManager()
    
    # Add callbacks
    async def error_callback(error_record, **kwargs):
        print(f"   Error: {error_record.message} ({error_record.severity.name})")
        if kwargs.get('alert'):
            print("   ⚠️ USER ALERT REQUIRED!")
            
    async def recovery_callback(error_record):
        print(f"   Recovered: {error_record.error_id} - {error_record.resolution}")
        
    manager.add_error_callback(error_callback)
    manager.add_recovery_callback(recovery_callback)
    
    # Test different error types
    print("\n🔴 Testing various error scenarios...")
    
    # Vision error
    await manager.handle_error(
        Exception("Failed to capture screen"),
        component="screen_capture",
        category=ErrorCategory.VISION,
        severity=ErrorSeverity.MEDIUM
    )
    
    # Network error
    await manager.handle_error(
        Exception("Connection timeout"),
        component="websocket",
        category=ErrorCategory.NETWORK,
        severity=ErrorSeverity.HIGH
    )
    
    # Permission error
    await manager.handle_error(
        Exception("Permission denied for action execution"),
        component="action_executor",
        category=ErrorCategory.PERMISSION,
        severity=ErrorSeverity.HIGH
    )
    
    # Wait for recovery attempts
    await asyncio.sleep(3)
    
    # Get statistics
    stats = manager.get_error_statistics()
    print(f"\n📊 Error Statistics:")
    print(f"   Total Errors: {stats['total_errors']}")
    print(f"   Active Errors: {stats['active_errors']}")
    print(f"   Recovery Success Rate: {stats['recovery_success_rate']:.1%}")
    print(f"\n   Errors by Category:")
    for cat, count in stats['errors_by_category'].items():
        if count > 0:
            print(f"     {cat}: {count}")
            
    print("\n✅ Error recovery test complete!")


if __name__ == "__main__":
    asyncio.run(test_error_recovery())