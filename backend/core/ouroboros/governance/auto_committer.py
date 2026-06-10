"""
Auto-Committer — Autonomous Git Commit After Successful APPLY+VERIFY
=====================================================================

Closes the autonomy loop: after O+V applies a change and verifies it passes
tests, the AutoCommitter creates a structured git commit with the O+V
signature. Without this, applied changes sit on disk as uncommitted
modifications, breaking the self-development cycle.

Design Principle: Zero-Context Readability (Mythos §7.4)
-------------------------------------------------------
Every commit message MUST be written for a reviewer who has:

- Zero session context — they were not watching the daemon run.
- No knowledge of the sensor signal — they don't know what triggered this op.
- No prior ops in the loop — they can't infer intent from neighboring commits.

The body always includes:

1. **Signal** — what triggered the operation (test_failure, ai_miner, etc.)
2. **Urgency** — why this was prioritized over other work.
3. **Rationale** — a self-contained explanation of WHY this change was needed,
   written so a cold reader can understand it without grepping internal logs.

Commit Message Format
---------------------
.. code-block:: text

    <type>(<scope>): <description>

    Signal: <signal_source> | Urgency: <urgency>

    Why: <rationale — self-contained explanation of what triggered this
    operation, what was wrong, and why this change fixes it>

    Op-ID: <op_id>
    Risk: <risk_tier>
    Provider: <provider> ($<cost>)
    Files: <file_list>

    Ouroboros+Venom [O+V] — Autonomous Self-Development Engine
    Co-Authored-By: Ouroboros+Venom <ouroboros@jarvis.trinity>

Risk-Tier Behavior
------------------
- ``SAFE_AUTO`` (Green): Commit immediately after VERIFY passes.
- ``NOTIFY_APPLY`` (Yellow): Commit after diff preview delay.
- ``APPROVAL_REQUIRED`` (Orange): Commit after human approval.
- ``BLOCKED`` (Red): Never reaches APPLY — no commit.

Environment Variables
---------------------
- ``JARVIS_AUTO_COMMIT_ENABLED`` (default ``true``): Master switch.
- ``JARVIS_AUTO_PUSH_BRANCH`` (default ``""``): If set, push to this branch.
  Empty = no push. Never pushes to main/master.

Manifesto Alignment
-------------------
- Section 6 (Iron Gate): Git operations use create_subprocess_exec arrays,
  never shell strings. Push is gated to non-protected branches only.
- Section 7 (Absolute Observability): Commit hash emitted via heartbeat for
  SerpentFlow rendering.
- Mythos §7.4 (Zero-Context Readability): Signal + rationale always present
  in commit body so reviewers never need to cross-reference session logs.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.AutoCommitter")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_ENABLED = os.environ.get("JARVIS_AUTO_COMMIT_ENABLED", "true").lower() in (
    "true", "1", "yes",
)
_PUSH_BRANCH = os.environ.get("JARVIS_AUTO_PUSH_BRANCH", "").strip()
_PROTECTED_BRANCHES = frozenset({"main", "master", "production", "release"})

# O+V Signature — the identity of the autonomous developer
_OV_SIGNATURE = "Ouroboros+Venom [O+V] — Autonomous Self-Development Engine"
_OV_COAUTHOR = "Co-Authored-By: Ouroboros+Venom <ouroboros@jarvis.trinity>"


# ---------------------------------------------------------------------------
# Public accessors — single source of truth for downstream observability
# ---------------------------------------------------------------------------
#
# Read-only observability substrates (e.g. second_order_doll_metric) need to
# detect O+V-authored commits from ``git log`` output. Rather than have those
# consumers grep on parallel string literals (drift risk), they compose these
# accessors via lazy-import. The constants themselves stay private (under-
# scored) — the API surface is the function, not the value.


def ov_signature_substring() -> str:
    """Canonical substring identifying an O+V commit in ``git log`` output.

    Downstream metric / observability layers detect autonomous commits by
    checking whether this substring is present in the commit message body.
    Stable across schema bumps — adding lines to the commit body never
    removes this exact signature line.
    """
    return _OV_SIGNATURE


def _run_post_commit_self_audit(op_id: str, commit_hash: str) -> None:
    """Runs in a thread executor — the sync, git-heavy adversarial-autobiography
    self-audit. NEVER raises. The audit self-publishes SSE + a JSONL ledger; a
    CORPUS_ESCAPE (a shipped autonomous commit smuggled a cage-bypass pattern)
    is logged loud for operator review. We do NOT auto-remediate."""
    try:
        from backend.core.ouroboros.governance.adversarial_autobiography import (
            audit_autobiography,
        )
        report = audit_autobiography(force_refresh=True)
        finding = str(getattr(report.finding, "value", report.finding))
        if finding == "corpus_escape":
            logger.warning(
                "[AutoCommitter] SELF-AUDIT CAGE ESCAPE after commit %s "
                "(op=%s): %d adversarial-corpus pattern(s) in O+V history — "
                "operator review required.",
                (commit_hash or "")[:8], op_id,
                int(getattr(report, "escape_count", 0) or 0),
            )
    except Exception:  # noqa: BLE001 — self-audit must never affect anything
        pass


def _schedule_post_commit_self_audit(op_id: str, commit_hash: str) -> None:
    """Slice 101 Phase 7 — after an O+V commit lands, schedule a NON-BLOCKING
    adversarial-autobiography self-audit so a cage-bypass that shipped is caught
    promptly (not only on the 30-min sleep cadence). The audit is sync + git-
    heavy, so it runs in a thread executor (Zero-Block Invariant) — fire-and-
    forget; the commit path NEVER waits on it. Self-gates on the autobiography
    master so nothing is spawned when off. NEVER raises into the commit path.
    """
    try:
        from backend.core.ouroboros.governance.adversarial_autobiography import (
            master_enabled as _autobiography_enabled,
        )
        if not _autobiography_enabled():
            return
        loop = asyncio.get_running_loop()
        # Fire-and-forget: do NOT await. _run_post_commit_self_audit never raises.
        loop.run_in_executor(
            None, _run_post_commit_self_audit, op_id, commit_hash,
        )
    except Exception:  # noqa: BLE001 — scheduling must never touch the commit
        pass


def ov_coauthor_line() -> str:
    """Canonical ``Co-Authored-By:`` trailer line for O+V commits."""
    return _OV_COAUTHOR


def _archive_authority_event(kind: str, detail: dict) -> None:
    """Best-effort append to the OCA observability ring (Slice 3
    #2). Archive absence/disable is silent (telemetry, never
    authority). NEVER raises into the commit path."""
    try:
        from backend.core.ouroboros.governance import (
            commit_authority_archive as _arch,
        )
        _arch.record(kind=kind, detail=detail)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class CommitResult:
    """Outcome of an auto-commit attempt."""

    committed: bool
    commit_hash: str = ""
    commit_message: str = ""
    pushed: bool = False
    push_branch: str = ""
    error: str = ""
    skipped_reason: str = ""
    intent_token: str = ""  # §24.6.2 — content-addressed dedup token
    # AutoCommitterIgnoreGuard Slice 2 -- two defense-layer audit
    # surfaces. Empty by default (pre-graduation + clean-path).
    # ``skipped_ignored`` carries paths refused at the per-file
    # pre-stage gate (Layer 1: gitignore_guard.find_ignored_targets
    # before each git add). ``aborted_validator_breach`` carries
    # paths that slipped past Layer 1 and were caught by the
    # post-stage validator (Layer 2: git diff --cached cross-
    # checked via the same guard). Layer 2 ABORTS the commit
    # and resets the index when populated.
    skipped_ignored: Tuple[str, ...] = field(default_factory=tuple)
    aborted_validator_breach: Tuple[str, ...] = field(
        default_factory=tuple,
    )


# ---------------------------------------------------------------------------
# AutoCommitter
# ---------------------------------------------------------------------------

class AutoCommitter:
    """Creates structured git commits after successful APPLY+VERIFY.

    All git operations are async subprocess calls using
    ``asyncio.create_subprocess_exec`` (argument arrays, no shell injection).
    Push is optional and gated to non-protected branches.

    Parameters
    ----------
    repo_root:
        Git repository root path.
    """

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = repo_root

    # ------------------------------------------------------------------
    # P1 Slice 2 — Ledger Sovereignty effective commit root
    # ------------------------------------------------------------------

    def _effective_repo_root(self) -> Path:
        """Resolve the cwd for every git subprocess in this class.

        When ``JARVIS_AUTO_COMMIT_WORKSPACE`` is set (Slice 2
        harness wiring), commits land in the owned worktree at
        that path. Otherwise falls back to ``self._repo_root``
        (legacy path; byte-identical pre-Slice-2 behavior).

        The env-var indirection keeps the structural fix to a
        single seam — no constructor signature change, no orches-
        trator plumbing — while letting the harness re-target the
        commit cwd at boot. Slice 2 wires it; Slice 1 substrate
        guards it via :func:`assert_ledger_sovereignty`.
        """
        override = os.environ.get("JARVIS_AUTO_COMMIT_WORKSPACE")
        if override:
            return Path(override)
        return self._repo_root

    def _assert_commit_target_sovereign(self) -> None:
        """Guard every commit entry — raises :exc:`LedgerSovereigntyError`
        when the master flag is on and the effective commit root is
        not an owned work-area. NEVER raises when master is off
        (§33.1 byte-identical legacy path).
        """
        try:
            from backend.core.ouroboros.governance.ledger_sovereignty import (  # noqa: E501
                assert_ledger_sovereignty,
            )
        except Exception:  # noqa: BLE001 — defensive
            return
        assert_ledger_sovereignty(
            self._effective_repo_root(),
            expected_session_id=(
                os.environ.get("JARVIS_OUROBOROS_SESSION_ID")
                or None
            ),
        )

    async def commit(
        self,
        op_id: str,
        description: str,
        target_files: Tuple[str, ...],
        risk_tier: Optional[Any] = None,
        provider_name: str = "",
        generation_cost: float = 0.0,
        signal_source: str = "",
        signal_urgency: str = "",
        rationale: str = "",
    ) -> CommitResult:
        """Create a structured git commit for the applied operation.

        Parameters
        ----------
        signal_source:
            What triggered the operation (e.g. ``"test_failure"``,
            ``"ai_miner"``, ``"voice_human"``).  Written into the commit
            body so a cold reviewer knows the originating signal.
        signal_urgency:
            Priority classification (``"critical"``/``"high"``/``"normal"``/
            ``"low"``).  Explains why this change was prioritized.
        rationale:
            Self-contained explanation of WHY this change was needed.
            Must be readable by someone with zero session context.

        Returns a :class:`CommitResult`. Never raises.
        """
        if not _ENABLED:
            return CommitResult(
                committed=False,
                skipped_reason="auto_commit_disabled",
            )

        if not target_files:
            return CommitResult(
                committed=False,
                skipped_reason="no_target_files",
            )

        # OCA Slice 3 #4 — Unified commit-authority gate. The
        # AutoCommitter IS the autonomous path by definition, so
        # the channel is the LITERAL "autonomous" — NEVER env,
        # NEVER resolve_commit_channel (that seam exists only for
        # the IDE/operator surface; an autonomous committer that
        # sniffed env could be tricked into an operator channel).
        # verify_pre_commit(autonomous) internally composes
        # ledger_sovereignty + the governance hash-cap under ONE
        # verdict taxonomy (the "unify human + autonomous paths"
        # principle). The legacy per-concern blocks below remain
        # as redundant defense — they compose the same substrates
        # so they agree. Substrate-unavailable → fall through to
        # the legacy gates (rollback discipline). NEVER raises.
        try:
            from backend.core.ouroboros.governance import (
                operator_commit_authority as _oca,
            )
            _root, _branch = _oca.resolve_repo_root_and_branch(
                self._effective_repo_root()
            )
            _verdict = _oca.verify_pre_commit(
                _oca.CommitAuthorityContext(
                    channel="autonomous",
                    repo_root=str(
                        _root or self._effective_repo_root()
                    ),
                    branch=_branch,
                    staged_files=tuple(target_files),
                )
            )
            _archive_authority_event(
                "verify_verdict",
                {
                    "op_id": op_id, "channel": "autonomous",
                    "verdict": _verdict.verdict.value,
                },
            )
            if not _verdict.authorized() and (
                _verdict.verdict
                is not _oca.CommitAuthorityVerdict.DISABLED
            ):
                _reason = {
                    _oca.CommitAuthorityVerdict.DENIED_SOVEREIGNTY:
                        "ledger_sovereignty_refused",
                    _oca.CommitAuthorityVerdict.DENIED_GOVERNANCE_DRIFT:
                        "governance_manifest_drift",
                }.get(
                    _verdict.verdict,
                    f"oca_denied:{_verdict.verdict.value}",
                )
                logger.warning(
                    "[AutoCommitter] OCA autonomous gate refused "
                    "commit op=%s verdict=%s — %s",
                    op_id, _verdict.verdict.value,
                    _verdict.detail[:200],
                )
                return CommitResult(
                    committed=False,
                    skipped_reason=_reason,
                    error=_verdict.detail[:512],
                )
        except Exception as _oca_exc:  # noqa: BLE001 — fall through
            logger.debug(
                "[AutoCommitter] OCA autonomous gate degraded "
                "(legacy gates still apply): %s", _oca_exc,
            )

        # P1 Slice 2 — Ledger Sovereignty structural boundary.
        # Refuses (typed) to commit when master flag is on AND
        # the effective commit root is not a registered owned
        # work-area. The check happens BEFORE the manifest hash
        # cap so the most-load-bearing defense fires first.
        # Master-FALSE path is byte-identical (no-op assertion).
        try:
            self._assert_commit_target_sovereign()
        except Exception as _sov_exc:  # LedgerSovereigntyError
            # Convert raise to typed CommitResult — the public
            # `commit` contract is "Never raises" (line 199).
            # Telemetry surfaces the violation via skipped_reason
            # and the warning log; the operator's main checkout
            # is structurally protected from this commit.
            logger.warning(
                "[AutoCommitter] ledger sovereignty refused "
                "commit op=%s — %s",
                op_id, _sov_exc,
            )
            return CommitResult(
                committed=False,
                skipped_reason="ledger_sovereignty_refused",
                error=str(_sov_exc)[:512],
            )

        # §40 Wave 2 #6 — RRD §1 hash-cap on self-modification.
        # When the staged commit touches the canonical governance
        # directory AND the operator-signed manifest exists AND
        # the master flag is on AND the current governance/ state
        # has drifted from the signed baseline → refuse the commit.
        # The verifier composes the canonical Wave 2 #5 boundary
        # gate's prefix accessor + a pure-function hash check.
        # NEVER raises: any verification failure falls through
        # to the legacy commit path (substrate-unavailable
        # rollback discipline).
        try:
            from backend.core.ouroboros.governance.governance_manifest import (  # noqa: E501
                is_refusal_verdict,
                verify_governance_state,
            )
            _manifest_verdict = verify_governance_state(
                target_files=target_files,
            )
            if is_refusal_verdict(_manifest_verdict.verdict):
                logger.warning(
                    "[AutoCommitter] governance hash-cap "
                    "refused commit op=%s detail=%s",
                    op_id, _manifest_verdict.detail[:200],
                )
                return CommitResult(
                    committed=False,
                    skipped_reason="governance_manifest_drift",
                )
        except Exception as _hashcap_exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[AutoCommitter] governance hash-cap "
                "verifier unavailable: %s — proceeding "
                "without manifest check", _hashcap_exc,
            )

        try:
            # §24.6.2 — Commit-intent token: content-addressed
            # dedup check BEFORE staging. Prevents double-apply
            # from crash-recovery race.
            intent_token = self._compute_intent_token(
                op_id, target_files,
            )

            # PRD §3.6.2 vector #10 closure (Wave 3 hygiene 2026-05-05):
            # the intent_token check + git commit + store_intent_token
            # are a TOCTOU critical section. Two concurrent processes
            # could both pass `_intent_token_exists()` before either
            # commits, producing duplicate commits. Wrap the section
            # in an async cross-process flock keyed on the
            # intent_token so two processes computing the SAME token
            # (same op_id + same file content) serialize at the OS
            # level. Different tokens → different lock files → no
            # unnecessary serialization across unrelated ops. Per the
            # §33.4 Per-Cluster flock'd JSONL Persistence pattern
            # (composing the canonical primitive — no parallel
            # locking machinery).
            try:
                from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
                    async_flock_critical_section as _async_flock,
                )
            except Exception as _imp_exc:  # noqa: BLE001 — defensive
                # Substrate unavailable — fall through to the legacy
                # path. Better to commit (with the residual TOCTOU
                # window) than to fail closed and miss legitimate
                # commits.
                logger.debug(
                    "[AutoCommitter] async_flock primitive "
                    "unavailable: %s — proceeding without "
                    "cross-process lock", _imp_exc,
                )
                return await self._commit_critical_section(
                    intent_token=intent_token,
                    op_id=op_id,
                    description=description,
                    target_files=target_files,
                    risk_tier=risk_tier,
                    provider_name=provider_name,
                    generation_cost=generation_cost,
                    signal_source=signal_source,
                    signal_urgency=signal_urgency,
                    rationale=rationale,
                )

            lock_path = self._intent_lock_path(intent_token)
            async with _async_flock(lock_path) as acquired:
                if not acquired:
                    # Cross-process contention beyond timeout — a
                    # sibling process is mid-commit for the same
                    # intent_token. Returning a distinct
                    # ``commit_lock_contended`` skipped_reason so
                    # operators see the contention in audit logs.
                    # Sibling will write the canonical commit; this
                    # process correctly skips (idempotent — same as
                    # the duplicate_intent_token branch).
                    logger.info(
                        "[AutoCommitter] Cross-process lock "
                        "contended for intent_token %s op=%s — "
                        "skipping (sibling process is committing)",
                        intent_token[:12], op_id,
                    )
                    return CommitResult(
                        committed=False,
                        skipped_reason="commit_lock_contended",
                        intent_token=intent_token,
                    )
                return await self._commit_critical_section(
                    intent_token=intent_token,
                    op_id=op_id,
                    description=description,
                    target_files=target_files,
                    risk_tier=risk_tier,
                    provider_name=provider_name,
                    generation_cost=generation_cost,
                    signal_source=signal_source,
                    signal_urgency=signal_urgency,
                    rationale=rationale,
                )

        except Exception as exc:
            logger.warning(
                "[AutoCommitter] Commit failed for op=%s: %s",
                op_id, exc,
            )
            return CommitResult(committed=False, error=str(exc))

    async def _commit_critical_section(
        self,
        *,
        intent_token: str,
        op_id: str,
        description: str,
        target_files: Tuple[str, ...],
        risk_tier: Optional[Any],
        provider_name: str,
        generation_cost: float,
        signal_source: str,
        signal_urgency: str,
        rationale: str,
    ) -> "CommitResult":
        """The TOCTOU-critical section extracted from
        :meth:`commit` so it can be invoked under the
        :func:`async_flock_critical_section` guard.

        Pre-Wave-3-hygiene this code lived inline in commit().
        Refactored 2026-05-05 to support the cross-process lock
        wrap (PRD §3.6.2 vector #10). NEVER raises out — the
        outer commit() try/except catches anything that escapes."""
        try:
            if await self._intent_token_exists(intent_token):
                logger.info(
                    "[AutoCommitter] Duplicate intent token %s for "
                    "op=%s — skipping (crash-recovery dedup)",
                    intent_token[:12], op_id,
                )
                return CommitResult(
                    committed=False,
                    skipped_reason="duplicate_intent_token",
                    intent_token=intent_token,
                )

            # Stage the target files (Slice 2 Layer 1 inside).
            staged, skipped_ignored = await self._stage_files(
                target_files,
            )
            if not staged:
                return CommitResult(
                    committed=False,
                    skipped_reason="nothing_to_stage",
                    skipped_ignored=skipped_ignored,
                )

            # Slice 2 Layer 2: post-stage validator. Cross-check
            # ``git diff --cached --name-only`` against the
            # gitignore guard to catch anything that slipped past
            # Layer 1 (e.g., a path that was clean at pre-stage
            # but got pulled in by a directory glob, or a Layer 1
            # subprocess failure that returned empty fail-open).
            # Two-layer fail-closed contract: Layer 1 fails open;
            # Layer 2 fails closed (aborts commit + resets index).
            breach = await self._validate_no_ignored_staged()
            if breach:
                logger.warning(
                    "[AutoCommitter] Layer 2 validator caught "
                    "%d ignored path(s) past Layer 1: %s -- "
                    "aborting commit + resetting index",
                    len(breach), list(breach)[:5],
                )
                await self._reset_index()
                # Slice 3 graduation: best-effort SSE publish on
                # Layer 2 abort so observability sees the ABORT
                # event live. NEVER raises into the commit path.
                try:
                    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
                        publish_auto_committer_ignored_blocked as _pub_blocked,
                    )
                    _pub_blocked(
                        op_id=op_id,
                        layer="layer2_validator",
                        blocked_paths=tuple(breach),
                        skipped_count=len(breach),
                        aborted=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "[AutoCommitter] SSE publish (layer2) "
                        "degraded: %s", exc,
                    )
                return CommitResult(
                    committed=False,
                    error=(
                        f"gitignore_breach_blocked: "
                        f"{len(breach)} path(s) refused"
                    ),
                    skipped_ignored=skipped_ignored,
                    aborted_validator_breach=breach,
                )

            # Build structured commit message
            message = self._build_commit_message(
                op_id=op_id,
                description=description,
                target_files=target_files,
                risk_tier=risk_tier,
                provider_name=provider_name,
                generation_cost=generation_cost,
                signal_source=signal_source,
                signal_urgency=signal_urgency,
                rationale=rationale,
            )

            # Commit
            commit_hash = await self._git_commit(message)
            if not commit_hash:
                return CommitResult(
                    committed=False,
                    error="git commit returned no hash",
                )

            # §24.6.2 — Store intent token in git notes post-commit.
            await self._store_intent_token(intent_token, commit_hash)

            # Slice 101 Phase 7 — fire-and-forget self-audit (non-blocking,
            # master-gated, never touches the commit path).
            _schedule_post_commit_self_audit(op_id, commit_hash)

            result = CommitResult(
                committed=True,
                commit_hash=commit_hash,
                commit_message=message,
                intent_token=intent_token,
                # Slice 2: surface Layer 1's refusals even on
                # success so operators see WHICH paths were
                # filtered when partitioning mixed inputs.
                skipped_ignored=skipped_ignored,
            )

            # Optional push
            if _PUSH_BRANCH:
                push_ok = await self._git_push(_PUSH_BRANCH)
                result.pushed = push_ok
                result.push_branch = _PUSH_BRANCH

            logger.info(
                "[AutoCommitter] Committed %s for op=%s (%d files)%s",
                commit_hash[:8], op_id, len(target_files),
                f" -> {_PUSH_BRANCH}" if result.pushed else "",
            )
            return result

        except Exception as exc:
            logger.warning(
                "[AutoCommitter] Commit failed for op=%s: %s",
                op_id, exc,
            )
            return CommitResult(committed=False, error=str(exc))

    # ------------------------------------------------------------------
    # Commit message construction
    # ------------------------------------------------------------------

    def _build_commit_message(
        self,
        op_id: str,
        description: str,
        target_files: Tuple[str, ...],
        risk_tier: Optional[Any] = None,
        provider_name: str = "",
        generation_cost: float = 0.0,
        signal_source: str = "",
        signal_urgency: str = "",
        rationale: str = "",
    ) -> str:
        """Build a structured commit message with O+V signature.

        Zero-Context Rule (Mythos §7.4)
        ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
        Every commit body must be self-contained: a reviewer who has never
        seen the daemon's session logs, has no knowledge of the originating
        sensor signal, and has read no neighboring commits should still
        understand *what triggered* this change, *why* it was needed, and
        *what* it does.
        """
        commit_type = self._infer_commit_type(description)
        scope = self._infer_scope(target_files)

        # Truncate description for subject line (max 72 chars for subject)
        subject_desc = description.strip()
        if len(subject_desc) > 60:
            subject_desc = subject_desc[:57] + "..."

        subject = f"{commit_type}({scope}): {subject_desc}"

        # Body — ordered for cold-reader comprehension:
        #   1. Signal provenance (what triggered this)
        #   2. Rationale (why this change is needed)
        #   3. Operational metadata (op-id, risk, provider, files)
        #   4. Signature
        body_parts: List[str] = []

        # --- Signal provenance block (Mythos §7.4) ---
        _sig = signal_source or "unknown"
        _urg = signal_urgency or "normal"
        body_parts.append(f"Signal: {_sig} | Urgency: {_urg}")

        # --- Rationale block (Mythos §7.4) ---
        # The rationale must be readable by someone with zero session context.
        # If no explicit rationale is provided, fall back to the description
        # (which at least tells the reader WHAT was done, even if not WHY).
        _rationale = (rationale or description).strip()
        if _rationale:
            # Wrap rationale to ~72 chars for git log readability
            _wrapped = self._wrap_rationale(_rationale)
            body_parts.append("")
            body_parts.append(f"Why: {_wrapped}")

        body_parts.append("")

        # --- Operational metadata ---
        body_parts.append(f"Op-ID: {op_id}")

        risk_str = self._format_risk_tier(risk_tier)
        body_parts.append(f"Risk: {risk_str}")

        if provider_name:
            cost_str = f" (${generation_cost:.4f})" if generation_cost > 0 else ""
            body_parts.append(f"Provider: {provider_name}{cost_str}")

        # File list (compact)
        if len(target_files) <= 5:
            files_str = ", ".join(target_files)
        else:
            files_str = ", ".join(target_files[:4]) + f" +{len(target_files) - 4} more"
        body_parts.append(f"Files: {files_str}")

        # O+V Signature block
        body_parts.append("")
        body_parts.append(_OV_SIGNATURE)
        body_parts.append(_OV_COAUTHOR)

        return subject + "\n\n" + "\n".join(body_parts)

    @staticmethod
    def _infer_commit_type(description: str) -> str:
        """Infer conventional commit type from the operation description."""
        desc_lower = description.lower()
        if any(w in desc_lower for w in ("fix", "bug", "error", "crash", "broken", "repair")):
            return "fix"
        if any(w in desc_lower for w in ("test", "spec", "coverage")):
            return "test"
        if any(w in desc_lower for w in ("refactor", "clean", "simplif", "restructur")):
            return "refactor"
        if any(w in desc_lower for w in ("doc", "readme", "comment", "changelog")):
            return "docs"
        if any(w in desc_lower for w in ("perf", "optimiz", "speed", "latency")):
            return "perf"
        if any(w in desc_lower for w in ("style", "format", "lint", "whitespace")):
            return "style"
        return "feat"

    @staticmethod
    def _infer_scope(target_files: Tuple[str, ...]) -> str:
        """Infer scope from target file paths."""
        if not target_files:
            return "ouroboros"

        parts_list = [Path(f).parts for f in target_files]
        if len(parts_list) == 1:
            p = Path(target_files[0])
            return p.parent.name if p.parent.name else p.stem

        # Multiple files — find common prefix directory
        common: List[str] = []
        for level_parts in zip(*parts_list):
            if len(set(level_parts)) == 1:
                common.append(level_parts[0])
            else:
                break

        if common:
            return common[-1]
        return "ouroboros"

    @staticmethod
    def _wrap_rationale(text: str, width: int = 68) -> str:
        """Wrap rationale text to fit within git log column width.

        The first line follows "Why: " (5 chars), subsequent lines are
        indented 5 spaces to align under the first word after "Why: ".
        """
        import textwrap
        lines = textwrap.wrap(text, width=width)
        if not lines:
            return text
        # First line is inline with "Why: "; subsequent lines indent to align
        return "\n     ".join(lines)

    @staticmethod
    def _format_risk_tier(risk_tier: Optional[Any]) -> str:
        """Format risk tier for commit message."""
        if risk_tier is None:
            return "UNKNOWN"
        name = getattr(risk_tier, "name", str(risk_tier))
        tier_map = {
            "SAFE_AUTO": "SAFE_AUTO (Green)",
            "NOTIFY_APPLY": "NOTIFY_APPLY (Yellow)",
            "APPROVAL_REQUIRED": "APPROVAL_REQUIRED (Orange)",
            "BLOCKED": "BLOCKED (Red)",
        }
        return tier_map.get(name, name)

    # ------------------------------------------------------------------
    # Git operations (async subprocess_exec, no shell injection)
    # ------------------------------------------------------------------

    async def _stage_files(
        self, target_files: Tuple[str, ...],
    ) -> Tuple[bool, Tuple[str, ...]]:
        """Stage target files for commit. Returns
        ``(staged_any, skipped_ignored)``.

        AutoCommitterIgnoreGuard Slice 2 Layer 1: when the
        ``JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED`` master
        is on, batch-checks ``target_files`` via
        ``find_ignored_targets`` and refuses to add any path
        that matches a ``.gitignore`` rule -- even if currently
        tracked (the ``--no-index`` semantics in the guard).
        Returned tuple lets the caller surface the refusal in
        ``CommitResult.skipped_ignored`` for audit.
        """
        # Check if there are any changes to stage
        proc = await asyncio.create_subprocess_exec(
            "git", "status", "--porcelain",
            cwd=str(self._effective_repo_root()),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if not stdout.strip():
            logger.debug("[AutoCommitter] No changes detected")
            return (False, ())

        # Slice 2 Layer 1: pre-stage gitignore filter. Single batch
        # subprocess (cheap; the guard's batched implementation makes
        # this O(1) git invocations regardless of len(target_files)).
        # Master-flag-off path returns empty tuple -> no filtering ->
        # behavior identical to pre-Slice-2.
        ignored_set: set = set()
        try:
            from backend.core.ouroboros.governance.gitignore_guard import (
                find_ignored_targets,
            )
            ignored_paths = find_ignored_targets(
                self._repo_root, list(target_files),
            )
            if ignored_paths:
                ignored_set = set(ignored_paths)
                logger.warning(
                    "[AutoCommitter] gitignore guard refused %d "
                    "path(s) at pre-stage: %s",
                    len(ignored_paths),
                    list(ignored_paths)[:5],
                )
                # Slice 3 graduation: best-effort SSE publish so
                # observability sees Layer 1 skips live. NEVER
                # raises into the staging path.
                try:
                    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
                        publish_auto_committer_ignored_blocked as _pub_blocked,
                    )
                    _pub_blocked(
                        op_id="",  # _stage_files doesn't have op_id
                        layer="layer1_prestage",
                        blocked_paths=tuple(ignored_paths),
                        skipped_count=len(ignored_paths),
                        aborted=False,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "[AutoCommitter] SSE publish (layer1) "
                        "degraded: %s", exc,
                    )
        except Exception as exc:  # noqa: BLE001 -- defensive
            # Guard failure is fail-open by contract (Slice 1's
            # primitive returns empty on subprocess failure). The
            # post-stage validator (Layer 2) is the second-layer
            # safety net.
            logger.debug(
                "[AutoCommitter] gitignore guard pre-check "
                "degraded: %s", exc,
            )

        # Stage each target file individually (safer than git add -A).
        # Skip those flagged by the pre-stage guard.
        staged_any = False
        for f in target_files:
            if f in ignored_set:
                continue  # Layer 1 refused; recorded for audit
            abs_path = self._repo_root / f
            if not abs_path.exists():
                continue
            proc = await asyncio.create_subprocess_exec(
                "git", "add", "--", str(f),
                cwd=str(self._effective_repo_root()),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode == 0:
                staged_any = True
            else:
                logger.debug(
                    "[AutoCommitter] git add failed for %s: %s",
                    f, stderr.decode(errors="replace").strip(),
                )

        return (staged_any, tuple(ignored_set))

    async def _validate_no_ignored_staged(self) -> Tuple[str, ...]:
        """Slice 2 Layer 2: post-stage defense. Run
        ``git diff --cached --name-only`` to enumerate currently-
        staged paths, then cross-check via the gitignore guard.
        Returns the ignored subset that slipped past Layer 1
        (empty when clean).

        NEVER raises. Subprocess failure -> empty tuple (best
        we can do; the absence of evidence is treated as evidence
        of absence at this layer because Layer 1 already had
        a fail-open pass).
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "diff", "--cached", "--name-only",
                cwd=str(self._effective_repo_root()),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return ()
            staged_paths = [
                line.strip()
                for line in stdout.decode(
                    errors="replace",
                ).splitlines()
                if line.strip()
            ]
            if not staged_paths:
                return ()
            from backend.core.ouroboros.governance.gitignore_guard import (
                find_ignored_targets,
            )
            return find_ignored_targets(
                self._repo_root, staged_paths,
            )
        except Exception as exc:  # noqa: BLE001 -- defensive
            logger.debug(
                "[AutoCommitter] _validate_no_ignored_staged "
                "degraded: %s", exc,
            )
            return ()

    async def _reset_index(self) -> bool:
        """Slice 2 Layer 2 cleanup. Run ``git reset HEAD --`` to
        unstage everything when the post-stage validator caught a
        breach. Returns True on subprocess success. NEVER raises.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "reset", "HEAD", "--",
                cwd=str(self._effective_repo_root()),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            return proc.returncode == 0
        except Exception as exc:  # noqa: BLE001 -- defensive
            logger.debug(
                "[AutoCommitter] _reset_index degraded: %s", exc,
            )
            return False

    async def _git_commit(self, message: str) -> str:
        """Create a git commit. Returns short commit hash."""
        proc = await asyncio.create_subprocess_exec(
            "git", "commit", "-m", message,
            cwd=str(self._effective_repo_root()),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            if "nothing to commit" in err or "nothing added to commit" in err:
                logger.debug("[AutoCommitter] Nothing to commit")
                return ""
            raise RuntimeError(f"git commit failed: {err}")

        return await self._get_head_hash()

    async def _get_head_hash(self) -> str:
        """Get the current HEAD commit hash (short)."""
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--short", "HEAD",
            cwd=str(self._effective_repo_root()),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().strip()

    async def _git_push(self, branch: str) -> bool:
        """Push to a branch. Refuses protected branches (Iron Gate)."""
        if branch in _PROTECTED_BRANCHES:
            logger.warning(
                "[AutoCommitter] Refusing to push to protected branch %r",
                branch,
            )
            return False

        # Slice 199 — the network/credential subprocess runs under the
        # non-interactive hardened env so a missing/expired credential FAILS
        # CLOSED (returncode != 0) instead of HANGING on a hidden CLI prompt.
        try:
            from backend.core.ouroboros.governance.m10_autonomous_graduation import (  # noqa: E501
                hardened_git_env as _s199_hardened_env,
            )
            _push_env = _s199_hardened_env()
        except Exception:  # noqa: BLE001
            _push_env = None
        proc = await asyncio.create_subprocess_exec(
            "git", "push", "-u", "origin", branch,
            cwd=str(self._effective_repo_root()),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_push_env,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(
                "[AutoCommitter] Push to %s failed: %s",
                branch, stderr.decode(errors="replace").strip(),
            )
            return False
        return True

    # ------------------------------------------------------------------
    # §24.6.2 — Commit-intent token (crash-recovery dedup)
    # ------------------------------------------------------------------

    # Notes ref used for intent token storage. Separate namespace from
    # regular git notes so we don't pollute the default notes ref.
    _INTENT_NOTES_REF = "refs/notes/ouroboros-applied"

    # Cross-process lock directory (PRD §3.6.2 vector #10 closure,
    # Wave 3 hygiene 2026-05-05). Per-token lock files keep
    # different commit ops from blocking each other while serializing
    # the TOCTOU section for ops with the SAME intent_token.
    _LOCK_DIR_NAME = "auto_commit_locks"

    def _intent_lock_path(self, intent_token: str) -> Path:
        """Per-intent_token lock file under
        ``<repo_root>/.jarvis/auto_commit_locks/<token>.lock``.

        The token is sha256-hex (64 chars) so the filename is
        path-safe by construction. Directory is created on demand
        by the flock primitive. NEVER raises."""
        # Defensive: clamp to first 32 chars to keep filenames
        # short, and accept either full token or pre-trimmed
        # variants. Sha256 maintains uniqueness at 32 hex chars
        # (128-bit) — collision probability ~2^-128 across the
        # global commit graph, far below relevant tolerance.
        safe = (intent_token or "unknown")[:32]
        return (
            self._repo_root
            / ".jarvis"
            / self._LOCK_DIR_NAME
            / f"{safe}.lock"
        )

    @staticmethod
    def _compute_intent_token(
        op_id: str,
        target_files: Tuple[str, ...],
    ) -> str:
        """Compute content-addressed intent token.

        ``sha256(canonical_json({op_id, sorted_target_files}))``

        The token uniquely identifies a specific APPLY attempt for
        a specific op targeting specific files. Two APPLY attempts
        for the same op + files produce the same token — the second
        attempt is rejected as a duplicate.
        """
        try:
            from backend.core.ouroboros.governance.observability.determinism_substrate import (  # noqa: E501
                canonical_hash,
            )
            return canonical_hash({
                "op_id": op_id,
                "target_files": sorted(target_files),
            })
        except Exception:  # noqa: BLE001 — defensive
            # Fallback: basic hash without canonical serializer.
            h = hashlib.sha256()
            h.update(op_id.encode("utf-8", errors="replace"))
            for f in sorted(target_files):
                h.update(f.encode("utf-8", errors="replace"))
            return h.hexdigest()

    async def _intent_token_exists(self, token: str) -> bool:
        """Check if an intent token already exists in git notes.

        Returns True if the token was already applied (duplicate).
        Returns False on any error (fail-open — prefer to commit
        and handle the error downstream rather than silently dropping
        a legitimate commit).
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "notes", "--ref", self._INTENT_NOTES_REF,
                "list",
                cwd=str(self._effective_repo_root()),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return False  # notes ref doesn't exist yet — no dupes
            # Each line is "<note_hash> <annotated_object_hash>".
            # We store the intent token as the annotated object name
            # (a pseudo-ref). Check if any line ends with the token.
            content = stdout.decode(errors="replace")
            return token in content
        except Exception:  # noqa: BLE001 — fail-open
            return False

    async def _store_intent_token(
        self, token: str, commit_hash: str,
    ) -> None:
        """Store intent token in git notes for future dedup checks.

        Best-effort — failure to store is logged but does NOT fail
        the commit (the commit already succeeded at this point).
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "notes", "--ref", self._INTENT_NOTES_REF,
                "add", "-m", f"intent:{token}",
                commit_hash,
                cwd=str(self._effective_repo_root()),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if proc.returncode != 0:
                logger.debug(
                    "[AutoCommitter] Failed to store intent token for "
                    "%s (non-critical)",
                    commit_hash[:8],
                )
        except Exception:  # noqa: BLE001 — best-effort
            pass
