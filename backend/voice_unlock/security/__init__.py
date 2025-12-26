"""
Voice Unlock Security Module
============================

Advanced security features for voice biometric authentication.

Components:
- Visual Context Integration - Screen analysis during voice unlock
- Anti-Spoofing Detection - Replay attack detection
- Environmental Security - Location and device verification
- Multi-Factor Fusion - Visual + Voice + Behavioral security

Author: JARVIS AI System
Version: 6.2.0 - Visual Security Enhancement
"""

from .visual_context_integration import (
    VisualSecurityAnalyzer,
    VisualSecurityEvidence,
    ScreenSecurityStatus,
    get_visual_security_analyzer,
)

__all__ = [
    "VisualSecurityAnalyzer",
    "VisualSecurityEvidence",
    "ScreenSecurityStatus",
    "get_visual_security_analyzer",
]
