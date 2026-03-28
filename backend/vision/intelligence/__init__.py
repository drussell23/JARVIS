"""
JARVIS Vision Intelligence System
Multi-language vision understanding without hardcoding
"""

from .visual_state_management_system import (
    VisualStateManagementSystem,
    ApplicationStateTracker,
    StateObservation,
    ApplicationState,
    VisualSignature,
    StateType,
    PatternBasedStateDetector
)

from .vision_intelligence_bridge import (
    VisionIntelligenceBridge,
    get_vision_intelligence_bridge,
    analyze_screenshot,
    SwiftBridge
)

__all__ = [
    'VisualStateManagementSystem',
    'ApplicationStateTracker',
    'StateObservation',
    'ApplicationState',
    'VisualSignature',
    'StateType',
    'PatternBasedStateDetector',
    'VisionIntelligenceBridge',
    'get_vision_intelligence_bridge',
    'analyze_screenshot',
    'SwiftBridge'
]

# Try to import Rust components if available
try:
    import vision_intelligence as rust_vi
    __all__.extend(['rust_vi'])
    RUST_AVAILABLE = True
except ImportError:
    rust_vi = None
    RUST_AVAILABLE = False

import logging as _logging
_logger = _logging.getLogger(__name__)

if RUST_AVAILABLE:
    _logger.debug("Vision Intelligence: Rust acceleration available")
else:
    _logger.debug("Vision Intelligence: Rust acceleration not available (Python fallback active)")