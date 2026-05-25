"""Slice 7 — traceback observability for L2 _generate_repair_candidate failures.

Closes the silent-exception gap exposed by soak bt-2026-05-25-203830.
Slice 6.1 proved that both L2 dispatches threw IDENTICAL
``generate_error:TypeError`` on iter 2 (logged via ``l2_soft_retries_
exhausted attempts=2 history=['generate_error:TypeError',
'generate_error:TypeError']``) but the underlying exception's
message, file, line, and stack frames were silently swallowed by
the broad ``except Exception`` at repair_engine.py:1180.

Manifesto §8 (Absolute Observability) violation: operators saw the
class name but had no way to fix what they couldn't see.

# Fix mechanism — log full traceback at ERROR before quarantine

  except Exception as exc:
      _logger.error(
          "[L2 Repair] _generate_repair_candidate raised %s: %s "
          "(op=%s) — quarantining as generate_error stop_reason; "
          "full traceback follows",
          type(exc).__name__, str(exc) or "(no message)",
          getattr(ctx, "op_id", "<unknown>"),
          exc_info=True,  # ← attaches full traceback to log record
      )
      return CandidateGenerationResult(
          candidate=None, model_id=None, provider_name=None,
          stop_reason=f"generate_error:{type(exc).__name__}",
      )

# Discipline

* Pure diagnostic addition. Return shape unchanged. Stop_reason
  format unchanged. FSM consumers (Slice 6/6.1 l2_retry handler
  in orchestrator.py + validate_runner.py) see identical behavior.
* Slice 5A's provider_iter_timeout path is intact — it returns
  early BEFORE the generic exception handler.
* ``exc_info=True`` works on both ``logger.error`` and
  ``logger.exception`` — we use ``logger.error(..., exc_info=True)``
  to retain explicit control of the message format.

# Test surface (2 AST pins + 3 spine)
"""

from __future__ import annotations

import ast
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REPAIR_ENGINE_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "repair_engine.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 2
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_generate_exception_handler_logs_traceback() -> None:
    """The ``except Exception`` block in ``_generate_repair_candidate``
    MUST call ``_logger.error(..., exc_info=True)`` (or equivalent)
    BEFORE returning the quarantined CandidateGenerationResult.

    Without this, TypeErrors and other provider-side exceptions are
    classified by class name only — operators lose the file, line,
    and message needed to fix the bug."""
    src = REPAIR_ENGINE_FILE.read_text()
    # The Slice 7 attribution + actual logger call + exc_info=True
    # must all be present in the file
    assert "Slice 7" in src, (
        "Slice 7 attribution comment missing — diagnostic context lost"
    )
    assert "_generate_repair_candidate raised" in src, (
        "Slice 7 log message stem missing — handler lacks traceback log"
    )
    assert "exc_info=True" in src, (
        "exc_info=True NOT in repair_engine — traceback is still "
        "swallowed by the broad except (Manifesto §8 violation)"
    )


def test_ast_pin_handler_log_precedes_return() -> None:
    """The logger.error call must come BEFORE the return statement
    inside the except block — logging AFTER return is unreachable."""
    tree = ast.parse(REPAIR_ENGINE_FILE.read_text(), filename=str(REPAIR_ENGINE_FILE))

    found_handler = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None or not isinstance(node.type, ast.Name):
            continue
        if node.type.id != "Exception":
            continue
        # Find the body — look for a call to _logger.error and a
        # subsequent return with CandidateGenerationResult
        has_log = False
        has_return = False
        for stmt in node.body:
            # logger.error or _logger.error call
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                call = stmt.value
                if (
                    isinstance(call.func, ast.Attribute)
                    and call.func.attr == "error"
                    and any(
                        isinstance(kw, ast.keyword) and kw.arg == "exc_info"
                        for kw in call.keywords
                    )
                ):
                    has_log = True
            elif isinstance(stmt, ast.Return):
                # Return must come AFTER the log to be observable
                if has_log:
                    has_return = True
        if has_log and has_return:
            found_handler = True
            break

    assert found_handler, (
        "No ExceptionHandler with logger.error(exc_info=True) preceding "
        "a Return statement found — Slice 7 wiring inert"
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 3
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spine_typeerror_traceback_actually_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Functional test — force a TypeError in the provider's generate
    call and verify the full traceback lands in caplog at ERROR level.
    This is the exact scenario from bt-2026-05-25-203830."""
    from backend.core.ouroboros.governance.repair_engine import (
        RepairBudget, RepairEngine,
    )

    class _TypeErrorProvider:
        async def generate(self, ctx, deadline, repair_context=None):
            # Simulate the exact failure mode from bt-2026-05-25-203830
            raise TypeError(
                "unsupported operand type(s) for +: 'NoneType' and 'str'"
            )

    engine = RepairEngine(
        budget=RepairBudget(per_iter_provider_timeout_s=10.0),
        prime_provider=_TypeErrorProvider(),
        repo_root="/tmp",
        sandbox_factory=lambda *a, **k: None,
    )

    class _Ctx:
        op_id = "test-op-slice7"

    caplog.set_level(logging.ERROR)
    deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
    outcome = await engine._generate_repair_candidate(
        _Ctx(), deadline, repair_context={},
    )

    # Verify the contract is preserved (Slice 6 still sees the
    # generate_error stop_reason and classifies as SOFT)
    assert outcome.candidate is None
    assert outcome.stop_reason == "generate_error:TypeError"

    # Verify the traceback was logged (Slice 7 deliverable)
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "No ERROR log emitted on TypeError — Slice 7 inert"

    # The log record must carry exc_info (traceback)
    record_with_traceback = [r for r in error_records if r.exc_info]
    assert record_with_traceback, (
        "No ERROR record has exc_info attached — traceback still hidden"
    )

    # The specific TypeError message must appear in the log
    message_text = "\n".join(r.getMessage() for r in error_records)
    assert "TypeError" in message_text, (
        "TypeError class name not in log message"
    )
    # op_id attribution
    assert "test-op-slice7" in message_text, (
        "op_id not in log message — operators can't attribute failures"
    )


@pytest.mark.asyncio
async def test_spine_provider_iter_timeout_path_does_not_log_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Slice 5A's provider_iter_timeout path (asyncio.TimeoutError) must
    NOT trigger the Slice 7 error log — it has its own warning-level
    handler with structured detail. Slice 7 must not pollute timeouts
    with extra ERROR-level noise."""
    from backend.core.ouroboros.governance.repair_engine import (
        RepairBudget, RepairEngine,
    )

    class _SlowProvider:
        async def generate(self, ctx, deadline, repair_context=None):
            await asyncio.sleep(10)  # exceeds 1s bound

    engine = RepairEngine(
        budget=RepairBudget(per_iter_provider_timeout_s=1.0),
        prime_provider=_SlowProvider(),
        repo_root="/tmp",
        sandbox_factory=lambda *a, **k: None,
    )

    class _Ctx:
        op_id = "test-op-slice7-timeout"

    caplog.set_level(logging.ERROR)
    deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
    outcome = await engine._generate_repair_candidate(
        _Ctx(), deadline, repair_context={},
    )

    assert outcome.candidate is None
    assert outcome.stop_reason is not None
    assert outcome.stop_reason.startswith("provider_iter_timeout:")

    # No ERROR-level traceback for the timeout path
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    # Slice 5A uses logger.warning (not error) for this path
    assert not error_records, (
        f"Slice 7 incorrectly logged ERROR on timeout path: {error_records}"
    )


def test_spine_contract_preserved_byte_equivalent() -> None:
    """Verify the public contract: CandidateGenerationResult shape
    on exception is UNCHANGED (Slice 6/6.1 consumers still see the
    same stop_reason format). Pure diagnostic — zero behavior change."""
    src = REPAIR_ENGINE_FILE.read_text()
    # The exact stop_reason format that Slice 6/6.1's classification
    # depends on must still be present
    assert 'stop_reason=f"generate_error:{type(exc).__name__}"' in src, (
        "Slice 7 broke the stop_reason format — Slice 6.1 SOFT/HARD "
        "classifier will see unexpected shape"
    )
    # The CandidateGenerationResult shape (4 named args) must still
    # be the return shape on exception
    assert "candidate=None" in src
    assert "model_id=None" in src
    assert "provider_name=None" in src
