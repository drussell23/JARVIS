"""
Security Logger
===============

Logs security events for audit trail and threat detection.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any

logger = logging.getLogger(__name__)


class SecurityLogger:
    """Logs security events for authentication system."""
    
    def __init__(self, log_dir: Path = None):
        if log_dir is None:
            log_dir = Path(__file__).parent.parent / "logs"
        
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        # Security log file
        self.security_log = self.log_dir / f"security_{datetime.now().strftime('%Y%m%d')}.log"
        
    async def log_event(self, event_type: str, **kwargs):
        """
        Log a security event.
        
        Args:
            event_type: Type of security event
            **kwargs: Additional event data
        """
        event = {
            "timestamp": datetime.now().isoformat(),
            "event_type": event_type,
            "data": kwargs
        }
        
        try:
            # Append to log file
            with open(self.security_log, 'a') as f:
                f.write(json.dumps(event) + '\n')
                
            # Also log to standard logger
            logger.info(f"Security event: {event_type} - {kwargs}")
            
        except Exception as e:
            logger.error(f"Failed to log security event: {e}")
    
    async def log_authentication_attempt(self, user_id: str, success: bool, 
                                       method: str, details: Dict[str, Any] = None):
        """Log authentication attempt."""
        await self.log_event(
            "authentication_attempt",
            user_id=user_id,
            success=success,
            method=method,
            details=details or {}
        )
    
    async def log_threat_detected(self, threat_type: str, severity: str, 
                                details: Dict[str, Any] = None):
        """Log detected security threat."""
        await self.log_event(
            "threat_detected",
            threat_type=threat_type,
            severity=severity,
            details=details or {}
        )
        
        # For high severity threats, also write to system log
        if severity in ["high", "critical"]:
            logger.warning(f"HIGH SEVERITY THREAT: {threat_type} - {details}")
    
    async def log_access_granted(self, user_id: str, resource: str, 
                               method: str = "dual_factor"):
        """Log successful access grant."""
        await self.log_event(
            "access_granted",
            user_id=user_id,
            resource=resource,
            method=method
        )
    
    async def log_lockout(self, user_id: str, reason: str, duration: int):
        """Log user lockout event."""
        await self.log_event(
            "user_lockout",
            user_id=user_id,
            reason=reason,
            duration_seconds=duration
        )
    
    def get_recent_events(self, event_type: str = None, limit: int = 100) -> list:
        """
        Get recent security events.
        
        Args:
            event_type: Filter by event type
            limit: Maximum number of events to return
            
        Returns:
            List of recent events
        """
        events = []
        
        try:
            if self.security_log.exists():
                with open(self.security_log, 'r') as f:
                    for line in f:
                        try:
                            event = json.loads(line.strip())
                            if event_type is None or event['event_type'] == event_type:
                                events.append(event)
                        except json.JSONDecodeError:
                            continue
            
            # Return most recent events
            return sorted(events, 
                        key=lambda x: x['timestamp'], 
                        reverse=True)[:limit]
                        
        except Exception as e:
            logger.error(f"Failed to read security log: {e}")
            return []
    
    def analyze_threats(self, time_window_minutes: int = 60) -> Dict[str, Any]:
        """
        Analyze recent events for threat patterns.
        
        Args:
            time_window_minutes: Time window to analyze
            
        Returns:
            Threat analysis results
        """
        from datetime import timedelta
        
        cutoff = datetime.now() - timedelta(minutes=time_window_minutes)
        recent_events = []
        
        # Get events within time window
        for event in self.get_recent_events():
            try:
                event_time = datetime.fromisoformat(event['timestamp'])
                if event_time > cutoff:
                    recent_events.append(event)
            except Exception:
                continue
        
        # Analyze patterns
        analysis = {
            "total_events": len(recent_events),
            "failed_attempts": 0,
            "threats_detected": 0,
            "unique_users": set(),
            "suspicious_patterns": []
        }
        
        user_failures = {}
        
        for event in recent_events:
            event_type = event.get('event_type')
            data = event.get('data', {})
            
            if event_type == 'authentication_attempt':
                user_id = data.get('user_id')
                if user_id:
                    analysis['unique_users'].add(user_id)
                    
                if not data.get('success'):
                    analysis['failed_attempts'] += 1
                    user_failures[user_id] = user_failures.get(user_id, 0) + 1
                    
            elif event_type == 'threat_detected':
                analysis['threats_detected'] += 1
        
        # Check for suspicious patterns
        for user_id, failures in user_failures.items():
            if failures >= 3:
                analysis['suspicious_patterns'].append({
                    'pattern': 'multiple_failures',
                    'user_id': user_id,
                    'failure_count': failures
                })
        
        analysis['unique_users'] = len(analysis['unique_users'])
        
        return analysis