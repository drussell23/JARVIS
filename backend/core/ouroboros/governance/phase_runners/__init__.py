"""Concrete PhaseRunner subclasses — one per extracted orchestrator phase.

Wave 2 item (5) slice sequence (see `memory/project_wave2_scope_draft.md`):

  Slice 1: COMPLETERunner (pilot)
  Slice 2: CLASSIFYRunner
  Slice 3: ROUTERunner, ContextExpansionRunner, PLANRunner
  Slice 4: VALIDATERunner, GATERunner, APPROVERunner, APPLYRunner,
           VERIFYRunner
  Slice 5: GENERATERunner (likely sub-extracted)
  Slice 6: dispatcher cutover — orchestrator becomes a thin registry loop

Each runner extracts its phase verbatim from the inline block in
``orchestrator.py::_run_pipeline()``, guarded by
``JARVIS_PHASE_RUNNER_<PHASE>_EXTRACTED`` (default ``false``) until
parity-test proven and graduation-session-soak confirmed.
"""
from __future__ import annotations

from backend.core.ouroboros.governance.phase_runners.classify_runner import (
    CLASSIFYRunner,
)
from backend.core.ouroboros.governance.phase_runners.complete_runner import (
    COMPLETERunner,
)
from backend.core.ouroboros.governance.phase_runners.context_expansion_runner import (
    ContextExpansionRunner,
)
from backend.core.ouroboros.governance.phase_runners.plan_runner import (
    PLANRunner,
)
from backend.core.ouroboros.governance.phase_runners.route_runner import (
    ROUTERunner,
)

__all__ = [
    "CLASSIFYRunner",
    "COMPLETERunner",
    "ContextExpansionRunner",
    "PLANRunner",
    "ROUTERunner",
]
