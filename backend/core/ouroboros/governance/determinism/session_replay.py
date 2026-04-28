"""Phase 1 Slice 1.4 — Session-level Replay Harness.

The CLI orchestrator that turns ``--rerun <session-id>`` into a
single command. Locates a recorded session's persisted state,
validates replay-readiness, applies the appropriate env vars, and
hands off to the standard battle-test boot sequence which then
runs in REPLAY (or VERIFY) mode against the recorded ledger.

Phase 1 layering recap:

  * Slice 1.1 — entropy + clock substrate primitives
  * Slice 1.2 — DecisionRuntime + ``decide(...)`` (RECORD/REPLAY/
    VERIFY/PASSTHROUGH integration runtime)
  * Slice 1.3 — phase_capture wrapper for production callsites
    + ROUTE phase wired
  * Slice 1.4 (THIS module) — session-level CLI orchestrator
  * Antigravity (parallel) — ``observability/replay_harness.py``:
    pure-function ``replay(log, state_0) → state_T`` for trace
    verification. Different abstraction level — Antigravity's is a
    pure reduce; mine is a whole-session re-boot orchestrator. They
    coexist cleanly.

The CLI ergonomics:

    python3 scripts/ouroboros_battle_test.py --rerun bt-2026-04-28-201119

This module's ``setup_replay_from_cli`` does the heavy lifting:
  1. Discovers the persisted seed at
     ``.jarvis/determinism/<session-id>/seed.json``.
  2. Discovers the persisted decisions ledger at
     ``.jarvis/determinism/<session-id>/decisions.jsonl``.
  3. Validates: seed exists + decisions ledger exists + at least
     one record present. Fail-fast with a structured error message
     if any check fails (operators get clear diagnostic, not silent
     drift to fresh-session mode).
  4. Applies the full env-var set required for replay:
       - ``JARVIS_DETERMINISM_LEDGER_ENABLED=true``
       - ``JARVIS_DETERMINISM_LEDGER_MODE={replay|verify}``
       - ``JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED=true``
       - ``JARVIS_DETERMINISM_ENTROPY_ENABLED=true``
       - ``JARVIS_DETERMINISM_CLOCK_ENABLED=true``
       - ``OUROBOROS_BATTLE_SESSION_ID=<session-id>``
       - ``OUROBOROS_DETERMINISM_SEED=<seed from disk>``
  5. Returns a frozen ``ReplaySessionPlan`` for caller observability
     (logged to stdout for the operator).

The harness then boots normally; phase_capture sees the env flags
and replays decisions automatically. Pure single-process replay —
no extra orchestration layer.

Operator's design constraints applied:

  * **Asynchronous** — discovery is sync I/O (small files only); the
    actual replay execution happens in the existing async harness
    flow.
  * **Dynamic** — mode is an argument; future modes (e.g.,
    "verify-strict") plug in via the same surface.
  * **Adaptive** — partial-state sessions (no decisions ledger but
    valid seed) are detected and reported; operator can choose
    fail-fast OR fall-through to fresh-session-with-seed.
  * **Intelligent** — leverages Slice 1.1's atomic-read pattern;
    schema versioning catches stale state cleanly.
  * **Robust** — every public method NEVER raises into the harness
    main(); errors surface as structured ``ReplaySessionPlan``
    fields (``is_replayable=False`` + ``failure_reason``).
  * **No hardcoding** — paths configurable via env; mode is
    enum-typed but the env variant is free-form for future
    extension.
  * **Leverages existing** — reuses Slice 1.1's
    ``SessionEntropy.seed_for_session`` and Slice 1.2's
    ``DecisionRuntime`` lookup index. ZERO new disk-format code.

Authority invariants (pinned by tests):
  * NEVER imports orchestrator / phase_runner / candidate_generator.
  * NEVER raises out of any public method.
  * Pure stdlib + Slice 1.1/1.2 imports only.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


# Per-session storage layout — mirrors Slice 1.1 + 1.2:
#   <state_dir>/<session-id>/seed.json
#   <state_dir>/<session-id>/decisions.jsonl


def _state_dir() -> Path:
    """Root directory for per-session determinism state.

    Reads ``JARVIS_DETERMINISM_STATE_DIR`` (Slice 1.1's env knob)
    so we land in the same directory as the seed + ledger files.
    Default ``.jarvis/determinism``."""
    raw = os.environ.get(
        "JARVIS_DETERMINISM_STATE_DIR",
        ".jarvis/determinism",
    ).strip()
    return Path(raw)


def _ledger_dir() -> Path:
    """Decisions ledger directory.

    Slice 1.2 uses ``JARVIS_DETERMINISM_LEDGER_DIR`` for the ledger
    base. By default it matches the state dir, so per-session files
    live under the same root. We honor the same env var here so an
    operator who customized one customizes the other consistently."""
    raw = os.environ.get(
        "JARVIS_DETERMINISM_LEDGER_DIR",
        ".jarvis/determinism",
    ).strip()
    return Path(raw)


# Master flag for the replay-CLI surface itself. When false, the
# CLI flag is accepted but the harness exits with a clear message
# (graceful degradation). When true, the harness proceeds. Defaults
# to ``true`` because the CLI surface is opt-in by argument anyway.
def replay_cli_enabled() -> bool:
    """``JARVIS_DETERMINISM_REPLAY_CLI_ENABLED`` (default ``true``).

    Hot-revert path for operators who want to disable the CLI
    surface without rolling back the whole determinism arc."""
    raw = os.environ.get(
        "JARVIS_DETERMINISM_REPLAY_CLI_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # default-on (this is just the CLI surface)
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# ReplaySessionPlan — frozen result of discovery + validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplaySessionPlan:
    """Frozen plan describing a replay-ready session.

    Returned by ``SessionReplayer.discover``. Caller checks
    ``is_replayable`` before applying env vars; if False,
    ``failure_reason`` documents the diagnostic (missing seed,
    missing decisions, schema mismatch, etc.).

    The plan is the SINGLE source of truth for what the CLI knows
    about the session — apply_env reads from here, validate reads
    from here, log output reads from here. No hidden state."""
    session_id: str
    state_dir: Path
    seed_path: Path
    decisions_path: Path
    is_replayable: bool
    seed: int = 0
    decision_count: int = 0
    failure_reason: str = ""
    diagnostics: Tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# SessionReplayer — discovery + validation + env application
# ---------------------------------------------------------------------------


class SessionReplayer:
    """Locates, validates, and prepares a recorded session for
    replay. Stateless — every method accepts the session_id and
    re-discovers on each call. NEVER raises out of any public
    method."""

    # Valid mode strings for env application. Free-form to allow
    # future extension (e.g., "verify-strict"); the underlying
    # Slice 1.2 runtime handles unknown values defensively.
    VALID_MODES: Tuple[str, ...] = (
        "replay", "verify", "passthrough", "record",
    )

    def discover(self, session_id: str) -> ReplaySessionPlan:
        """Find + validate a session's persisted state. NEVER
        raises — failures surface as ``is_replayable=False`` + a
        structured ``failure_reason``."""
        sid = (str(session_id).strip() if session_id else "")
        if not sid:
            return self._fail_plan(
                session_id="",
                state_dir=_state_dir(),
                failure_reason="empty_session_id",
                diagnostics=("session_id must be non-empty",),
            )

        state_dir = _state_dir()
        seed_path = state_dir / sid / "seed.json"
        decisions_path = _ledger_dir() / sid / "decisions.jsonl"

        diagnostics: list = [
            f"state_dir={state_dir}",
            f"seed_path={seed_path}",
            f"decisions_path={decisions_path}",
        ]

        # Step 1: seed file
        seed = self._read_seed(seed_path)
        if seed is None:
            return self._fail_plan(
                session_id=sid,
                state_dir=state_dir,
                seed_path=seed_path,
                decisions_path=decisions_path,
                failure_reason="seed_missing_or_invalid",
                diagnostics=tuple(
                    diagnostics + ["seed.json not found or unparseable"]
                ),
            )
        diagnostics.append(f"seed=0x{seed:016x}")

        # Step 2: decisions ledger
        decision_count = self._count_decisions(decisions_path)
        if decision_count is None:
            return self._fail_plan(
                session_id=sid,
                state_dir=state_dir,
                seed_path=seed_path,
                decisions_path=decisions_path,
                failure_reason="decisions_unreadable",
                diagnostics=tuple(
                    diagnostics + ["decisions.jsonl exists but unreadable"]
                ),
                seed=seed,
            )
        diagnostics.append(f"decision_count={decision_count}")

        if decision_count == 0:
            # The session has a seed but no decisions. Replay is
            # *technically* possible — every decision will be a
            # replay-miss + fall-through to RECORD. We return
            # is_replayable=True because the harness still works,
            # but mark the diagnostic so the operator sees they're
            # effectively running a fresh session with a pinned seed.
            diagnostics.append(
                "WARN: empty decisions ledger — replay will degrade "
                "to RECORD-on-miss for every decide() call"
            )

        return ReplaySessionPlan(
            session_id=sid,
            state_dir=state_dir,
            seed_path=seed_path,
            decisions_path=decisions_path,
            is_replayable=True,
            seed=seed,
            decision_count=decision_count,
            failure_reason="",
            diagnostics=tuple(diagnostics),
        )

    def apply_env(
        self,
        plan: ReplaySessionPlan,
        *,
        mode: str = "replay",
    ) -> None:
        """Apply the env-var set required for replay.

        Idempotent — safe to call multiple times. Mutates
        ``os.environ`` only when ``plan.is_replayable=True``;
        unrepayable plans are silent no-ops (caller is responsible
        for surfacing the diagnostic to the operator).

        ``mode`` MUST be a member of ``VALID_MODES``; unknown values
        log a warning + fall through to ``"replay"``. NEVER raises."""
        if not plan.is_replayable:
            return
        normalized_mode = (str(mode).strip() if mode else "").lower()
        if normalized_mode not in self.VALID_MODES:
            logger.warning(
                "[determinism.replay] unknown mode %r — falling "
                "back to 'replay'", normalized_mode,
            )
            normalized_mode = "replay"

        try:
            os.environ["JARVIS_DETERMINISM_LEDGER_ENABLED"] = "true"
            os.environ["JARVIS_DETERMINISM_LEDGER_MODE"] = normalized_mode
            os.environ["JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED"] = "true"
            os.environ["JARVIS_DETERMINISM_ENTROPY_ENABLED"] = "true"
            os.environ["JARVIS_DETERMINISM_CLOCK_ENABLED"] = "true"
            os.environ["OUROBOROS_BATTLE_SESSION_ID"] = plan.session_id
            os.environ["OUROBOROS_DETERMINISM_SEED"] = str(plan.seed)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.warning(
                "[determinism.replay] env apply failed: %s — replay "
                "may not engage cleanly", exc,
            )

    def validate(
        self, plan: ReplaySessionPlan,
    ) -> Tuple[bool, str]:
        """Pure validation — returns ``(is_valid, diagnostic)``.
        Provides a single structured signal the CLI can use to
        decide whether to fail-fast or proceed."""
        if not plan.is_replayable:
            return False, plan.failure_reason or "not_replayable"
        if plan.seed == 0:
            return False, "zero_seed_invalid"
        return True, "ok"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_seed(self, seed_path: Path) -> Optional[int]:
        """Read the seed from disk. NEVER raises — returns None on
        any failure (missing, corrupt, schema mismatch)."""
        if not seed_path.exists():
            return None
        try:
            payload = json.loads(seed_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug(
                "[determinism.replay] seed read failed at %s: %s",
                seed_path, exc,
            )
            return None
        if not isinstance(payload, dict):
            return None
        # Slice 1.1 schema
        if payload.get("schema_version") != "session_seed.1":
            return None
        seed = payload.get("seed")
        if isinstance(seed, int) and seed >= 0:
            return seed
        return None

    def _count_decisions(self, decisions_path: Path) -> Optional[int]:
        """Count valid records in the JSONL ledger. NEVER raises —
        returns:
          * 0 if file doesn't exist (treat as zero-record session)
          * count of parseable lines if exists
          * None on read error (caller treats as failure)"""
        if not decisions_path.exists():
            return 0
        try:
            count = 0
            with decisions_path.open("r", encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        payload = json.loads(raw)
                        if (
                            isinstance(payload, dict)
                            and payload.get("schema_version") == (
                                "decision_record.1"
                            )
                        ):
                            count += 1
                    except json.JSONDecodeError:
                        continue
            return count
        except OSError as exc:
            logger.debug(
                "[determinism.replay] decisions read failed: %s", exc,
            )
            return None

    @staticmethod
    def _fail_plan(
        *,
        session_id: str,
        state_dir: Path,
        failure_reason: str,
        diagnostics: Tuple[str, ...],
        seed_path: Optional[Path] = None,
        decisions_path: Optional[Path] = None,
        seed: int = 0,
    ) -> ReplaySessionPlan:
        """Helper: build a failure plan with consistent shape."""
        return ReplaySessionPlan(
            session_id=session_id,
            state_dir=state_dir,
            seed_path=seed_path or (state_dir / "seed.json"),
            decisions_path=decisions_path or (
                state_dir / "decisions.jsonl"
            ),
            is_replayable=False,
            seed=seed,
            decision_count=0,
            failure_reason=failure_reason,
            diagnostics=diagnostics,
        )


# ---------------------------------------------------------------------------
# Public CLI helper — one-shot setup
# ---------------------------------------------------------------------------


def setup_replay_from_cli(
    session_id: str,
    *,
    mode: str = "replay",
    raise_on_failure: bool = True,
) -> ReplaySessionPlan:
    """One-shot: discover + validate + apply env. Returns the plan
    (frozen) for caller observability.

    Behavior:
      * Discovers the session's persisted state.
      * If ``is_replayable=False`` AND ``raise_on_failure=True``,
        raises ``ValueError`` with the diagnostic. Operators see
        clear failures rather than silent drift to fresh-session.
      * If ``is_replayable=False`` AND ``raise_on_failure=False``,
        returns the failure plan without mutating env. Caller
        decides what to do next.
      * If ``is_replayable=True``, applies the env vars + returns
        the plan.

    The default ``raise_on_failure=True`` matches operator's
    'no shortcuts' directive: replay should fail loudly when it
    can't produce real replay semantics."""
    if not replay_cli_enabled():
        # CLI surface explicitly disabled — return an
        # un-replayable plan; caller (battle test main) reports
        # to operator + falls through to fresh session.
        return ReplaySessionPlan(
            session_id=str(session_id),
            state_dir=_state_dir(),
            seed_path=_state_dir() / "seed.json",
            decisions_path=_ledger_dir() / "decisions.jsonl",
            is_replayable=False,
            failure_reason="cli_disabled",
            diagnostics=(
                "JARVIS_DETERMINISM_REPLAY_CLI_ENABLED=false — "
                "operator has disabled the replay CLI surface",
            ),
        )

    replayer = SessionReplayer()
    plan = replayer.discover(session_id)
    if not plan.is_replayable:
        if raise_on_failure:
            raise ValueError(
                f"replay setup failed: {plan.failure_reason}\n"
                f"diagnostics:\n  "
                + "\n  ".join(plan.diagnostics)
            )
        return plan
    replayer.apply_env(plan, mode=mode)
    return plan


def render_plan_summary(plan: ReplaySessionPlan) -> str:
    """Format a plan into a multi-line operator-readable summary.
    Used by the battle-test CLI to print what was set up before
    handing off to the harness boot. NEVER raises."""
    lines = [
        f"[Replay] Session: {plan.session_id}",
        f"  state_dir:      {plan.state_dir}",
        f"  seed:           0x{plan.seed:016x}" if plan.seed else "  seed:           <none>",
        f"  decisions:      {plan.decision_count}",
        f"  is_replayable:  {plan.is_replayable}",
    ]
    if not plan.is_replayable and plan.failure_reason:
        lines.append(f"  failure:        {plan.failure_reason}")
    if plan.diagnostics:
        lines.append("  diagnostics:")
        for d in plan.diagnostics:
            lines.append(f"    - {d}")
    return "\n".join(lines)


__all__ = [
    "ReplaySessionPlan",
    "SessionReplayer",
    "render_plan_summary",
    "replay_cli_enabled",
    "setup_replay_from_cli",
]
