"""tests/governance/comms/test_exports.py"""


def test_comms_public_api():
    from backend.core.ouroboros.governance.comms import (
        format_narration,
        SCRIPTS,
        VoiceNarrator,
        OpsLogger,
        TUISelfProgramPanel,
        SelfProgramPanelState,
        PipelineStatus,
        CompletionSummary,
    )
    assert VoiceNarrator is not None
    assert OpsLogger is not None
    assert TUISelfProgramPanel is not None
