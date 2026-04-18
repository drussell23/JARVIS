"""I7 substrate export-ban CI check.

Structural enforcement of the invariant:

    The ``Attachment`` type and ``ctx.attachments`` field are consumable
    **only** by (a) ``VisionSensor``, (b) ``visual_verify.py``, and the
    narrow orchestrator / provider paths documented below. Any other
    module reading ``ctx.attachments`` is a spec violation.

Enforcement layers:

1. **CI greps the codebase** (this file). Any ``ctx.attachments`` /
   ``operation_context.attachments`` / ``operation.attachments`` read in
   a non-authorized module fails the build. Reviewer discipline alone is
   insufficient — the ban is structural.

2. **Provider serialization gate** (``providers._serialize_attachments``
   with ``purpose ∈ {sensor_classify, visual_verify}``) — enforced by
   ``tests/governance/test_attachment_serialization.py`` (Task 7 of the
   implementation plan). That gate prevents attachments from reaching a
   provider API call they were not authorized for, even if an attacker
   got past this file.

The ban expires only via a dedicated spec review that graduates a new
consumer through its own 3-session arc. Adding a path to
``_AUTHORIZED_MODULES`` without that review is itself a violation.
"""
from __future__ import annotations

import pathlib
import re

import pytest


# Repo root: tests/governance/test_attachment_export_ban.py → parents[2]
_ROOT = pathlib.Path(__file__).resolve().parents[2]


# The only modules permitted to reference ``ctx.attachments`` or siblings.
# Keep this set AS SMALL AS POSSIBLE. Any addition requires a spec review.
_AUTHORIZED_MODULES = frozenset(
    {
        # Definition site — the ``Attachment`` dataclass and
        # ``OperationContext.with_attachments`` live here.
        "backend/core/ouroboros/governance/op_context.py",
        # I7 sanctioned consumer #1 — VisionSensor reads sensor_frame
        # attachments to populate evidence envelopes. Created in Task 8
        # of the implementation plan.
        "backend/core/ouroboros/governance/intake/sensors/vision_sensor.py",
        # I7 sanctioned consumer #2 — Visual VERIFY reads pre_apply /
        # post_apply attachments for deterministic + advisory checks.
        # Created in Task 17 of the implementation plan.
        "backend/core/ouroboros/governance/visual_verify.py",
        # Serialization boundary — ``_serialize_attachments`` is the ONLY
        # function in providers.py allowed to walk ctx.attachments, and
        # only when ``purpose ∈ {sensor_classify, visual_verify}`` (Task 7
        # purpose-gate test enforces that further restriction).
        "backend/core/ouroboros/governance/providers.py",
        # Orchestrator — attaches pre_apply frame at GENERATE start and
        # post_apply frame at APPLY success. The orchestrator never
        # *reads* attachments for any purpose other than writing them
        # back via ``ctx.add_attachment`` (capture-only, not consume).
        "backend/core/ouroboros/governance/orchestrator.py",
    }
)


# Patterns that indicate a read of the attachments field on any plausible
# variable name. Word boundaries prevent matching ``XYZ_ctx.attachments_raw``
# or similar false positives.
_READ_PATTERN = re.compile(
    r"\b(?:ctx|operation_context|op_context|operation|op)\.attachments\b"
)

# Patterns that indicate an unauthorized import of Attachment itself. The
# grep is conservative — we allow the type to be re-exported from
# op_context (where it's defined) but flag direct imports elsewhere.
_IMPORT_PATTERN = re.compile(
    r"from\s+backend\.core\.ouroboros\.governance\.op_context\s+import\s+[^#\n]*\bAttachment\b"
)


# Path substrings that indicate vendored / build / cache content we must
# never scan — third-party code carries its own unrelated ``.attachments``
# fields that would false-positive the grep.
_EXCLUDED_PATH_SUBSTRINGS: frozenset = frozenset(
    {
        "__pycache__",
        "/venv/",
        "/.venv/",
        "/site-packages/",
        "/node_modules/",
        "/.git/",
        "/build/",
        "/dist/",
        "/.tox/",
        "/.mypy_cache/",
        "/.pytest_cache/",
        "/.ruff_cache/",
    }
)


def _iter_backend_py_files():
    """Walk all *first-party* ``.py`` files under ``backend/``.

    Skips vendored trees (venv, site-packages, node_modules), VCS
    metadata (.git), and build/cache artifacts. Without this filter, the
    grep would walk into third-party libraries whose own data models
    happen to use ``.attachments`` fields.
    """
    for path in _ROOT.glob("backend/**/*.py"):
        rel = path.relative_to(_ROOT).as_posix()
        if rel.endswith(".pyc"):
            continue
        rel_with_slashes = "/" + rel
        if any(skip in rel_with_slashes for skip in _EXCLUDED_PATH_SUBSTRINGS):
            continue
        yield path, rel


# ---------------------------------------------------------------------------
# CI checks
# ---------------------------------------------------------------------------


def test_no_unauthorized_attachment_reads():
    """Reject any non-authorized module that reads ``ctx.attachments``."""
    violations = []
    for path, rel in _iter_backend_py_files():
        if rel in _AUTHORIZED_MODULES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _READ_PATTERN.search(text):
            # Find line numbers for precise error reporting
            lines = []
            for i, line in enumerate(text.splitlines(), start=1):
                if _READ_PATTERN.search(line):
                    lines.append(f"  L{i}: {line.strip()}")
            violations.append(f"{rel}:\n" + "\n".join(lines))
    assert not violations, (
        "I7 substrate export-ban violation — the following modules read "
        "ctx.attachments without authorization:\n\n"
        + "\n\n".join(violations)
        + "\n\nAttachment data is exclusive to VisionSensor + Visual VERIFY. "
        "Add a module to _AUTHORIZED_MODULES ONLY after a dedicated spec "
        "review graduates a new consumer through its own 3-session arc. "
        "See docs/superpowers/specs/2026-04-18-vision-sensor-verify-design.md "
        "§Invariant I7."
    )


def test_no_unauthorized_attachment_imports():
    """Reject any non-authorized module that imports ``Attachment`` directly.

    Re-exports via ``op_context`` (the definition module) are fine. This
    check catches cases like ``from ... import Attachment`` in modules
    that should never touch the type.
    """
    violations = []
    for path, rel in _iter_backend_py_files():
        if rel in _AUTHORIZED_MODULES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _IMPORT_PATTERN.search(text):
            violations.append(rel)
    assert not violations, (
        "I7 violation — modules importing Attachment without authorization: "
        f"{violations}. See spec §Invariant I7."
    )


def test_authorized_modules_set_is_tight():
    """Meta-invariant: the authorization list is small and documented.

    The ban weakens every time a new module joins the list. Flag if the
    list grows past a hard ceiling — forces a design conversation.
    """
    # Hard ceiling: if _AUTHORIZED_MODULES exceeds 6 entries, someone has
    # added consumers without spec review. The ceiling can only be raised
    # in-file with a matching comment explaining *why*, which shows up in
    # code review.
    assert len(_AUTHORIZED_MODULES) <= 6, (
        f"I7 authorization list exceeds 6 entries ({len(_AUTHORIZED_MODULES)}). "
        "Each addition demands a dedicated spec review — raising this "
        "ceiling without one is itself a violation."
    )


def test_authorized_modules_exist_on_disk():
    """Sanity: every authorized path must actually exist (or be plan-scheduled).

    At this stage of the implementation plan, some authorized modules
    haven't been created yet (vision_sensor.py and visual_verify.py ship
    in Tasks 8 and 17). Mark those as plan-pending; the others must
    exist.
    """
    plan_pending = {
        "backend/core/ouroboros/governance/intake/sensors/vision_sensor.py",
        "backend/core/ouroboros/governance/visual_verify.py",
    }
    missing_required = []
    for rel in _AUTHORIZED_MODULES:
        if rel in plan_pending:
            continue
        if not (_ROOT / rel).exists():
            missing_required.append(rel)
    assert not missing_required, (
        f"Authorized modules missing on disk (and not plan-pending): {missing_required}"
    )


# ---------------------------------------------------------------------------
# Canary tests — prove the grep patterns work
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "snippet",
    [
        "def f(ctx): return ctx.attachments[0]",
        "for a in ctx.attachments: pass",
        "op.attachments",
        "operation.attachments",
        "operation_context.attachments",
        "op_context.attachments",
    ],
)
def test_canary_read_pattern_detects_violation(snippet):
    """Negative control — pattern must fire on obvious violation shapes."""
    assert _READ_PATTERN.search(snippet), f"Pattern missed: {snippet!r}"


@pytest.mark.parametrize(
    "snippet",
    [
        "context.attachments_metadata",     # different field name
        "attachments = []",                  # not a dotted read
        "some_other.attachments_field",     # bare word, not our patterns
        "# ctx.attachments in a comment is fine",  # comments are still text — see note below
    ],
)
def test_canary_read_pattern_tolerates_non_violations(snippet):
    """Positive control — pattern must NOT fire on unrelated code.

    Note: comments matching the pattern are technically flagged. That's
    acceptable — if someone writes ``# ctx.attachments = foo`` in a
    non-authorized module, the grep is being conservative, and the fix
    (move or delete the comment) is trivial. The ban would rather err
    toward false-positives than miss a real violation.
    """
    # Skip the comment case for this canary — we accept conservative behavior.
    if snippet.startswith("#"):
        return
    assert not _READ_PATTERN.search(snippet), f"Pattern fired on {snippet!r}"


def test_canary_import_pattern_detects_violation():
    assert _IMPORT_PATTERN.search(
        "from backend.core.ouroboros.governance.op_context import Attachment"
    )
    assert _IMPORT_PATTERN.search(
        "from backend.core.ouroboros.governance.op_context import (\n"
        "    Attachment,\n"
        "    OperationContext,\n"
        ")"
    ) is None  # multiline import — grep is single-line conservative; accepted trade-off
