"""tests/governance/intent/test_exports.py"""


def test_intent_public_api():
    from backend.core.ouroboros.governance.intent import (
        IntentSignal,
        DedupTracker,
        RateLimiter,
        RateLimiterConfig,
        TestWatcher,
        ErrorInterceptor,
        IntentEngine,
        IntentEngineConfig,
    )
    assert IntentSignal is not None
    assert IntentEngine is not None
