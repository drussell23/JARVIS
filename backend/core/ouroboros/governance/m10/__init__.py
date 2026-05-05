"""M10 ArchitectureProposer (PRD §32.4 / supersedes §30.5.2).

Architecture-extension proposal pipeline. The system autonomously
proposes new sensor classes / phase candidates / observers / flag
families when it detects recurring signal patterns no existing
sensor catches. Every proposal routes through ``APPROVAL_REQUIRED``
+ Quorum K=3 + 5-layer validation; operator authorizes via
GitHub PR (NOT REPL).

Architecture: lifts design contracts from the archived
``graduation_orchestrator.py`` (15-phase FSM + AdaptiveThreshold +
H1-H6 hard-won lessons + 5-layer validation) without inheriting
the dead code itself. Composes with already-graduated cage
components: ``WorktreeManager`` (L3 isolation), ``AutoCommitter``
(structured commits), ``OrangePRReviewer`` (async PR review),
``SemanticGuardian`` (10 patterns), ``urgency_router`` +
``candidate_generator`` (cost-gated routing), ``GenerativeQuorum``
(K=3 mandatory), ``Iron Gate`` (exploration-first floor).

Master flag ``JARVIS_M10_ARCH_PROPOSER_ENABLED`` defaults FALSE
and stays default-false until 30+ proposal-acceptance audit
(operator-pinned per §30.5.2). Slice 5 graduation flips ONLY the
opt-in surface, not the production default."""
from __future__ import annotations

__all__: list = []
