"""Deprecated — moved to backend.core.adaptive_timeout_manager."""
import warnings as _w
_w.warn(
    "Import from backend.core.adaptive_timeout_manager instead",
    DeprecationWarning, stacklevel=2,
)
from backend.core.adaptive_timeout_manager import (  # noqa: F401,E402
    AdaptiveTimeoutManager, OperationType, TimeoutStrategy,
    LoadLevel, TimeoutConfig, TimeoutBudget, OperationStats,
    OperationSample, ComplexityEstimator, DEFAULT_CONFIGS,
    get_timeout_manager, get_timeout_manager_sync,
    adaptive_get, adaptive_get_sync,
    DecisionReason, FrozenOperationStats,
    StartupMetricsHistoryAdapter,
)
