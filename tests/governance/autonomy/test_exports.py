"""tests/governance/autonomy/test_exports.py"""


def test_autonomy_public_api():
    from backend.core.ouroboros.governance.autonomy import (
        AutonomyTier,
        TIER_ORDER,
        CognitiveLoad,
        WorkContext,
        CAISnapshot,
        UAESnapshot,
        SAISnapshot,
        GraduationMetrics,
        SignalAutonomyConfig,
        AutonomyGate,
        TrustGraduator,
        AutonomyState,
    )
    assert AutonomyTier is not None
    assert AutonomyGate is not None
    assert TrustGraduator is not None
    assert AutonomyState is not None
