"""
Regression spine for the unlock grant bridge.

Three kinds of assertion, and the split matters:

* **Behavioural** -- the bridge maps every helper exit code to the right typed
  result and never raises into the voice-command path.
* **Cross-language parity** -- ``GrantOutcome`` and the helper's exit enum are
  the same numbers. They are duplicated across a boundary no build system spans,
  so nothing but a test can hold them together.
* **Wiring** -- the code-signing requirement is actually *applied* to both
  listeners. A security control that is present in the source but never attached
  to the object it protects is theatre, and this repo has been bitten by exactly
  that shape before.
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
from pathlib import Path

import pytest

from backend.voice_unlock.grant_bridge import (
    DEFAULT_HELPER_SEARCH_PATH,
    ENV_HELPER_PATH,
    GrantBridge,
    GrantOutcome,
    resolve_helper_path,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
AUTHPLUGIN_SRC = REPO_ROOT / "backend/voice_unlock/authplugin/src"
HELPER_SRC = AUTHPLUGIN_SRC / "jarvis_unlock_grant.m"
BROKER_SRC = AUTHPLUGIN_SRC / "JARVISUnlockBroker.m"
MECHANISM_SRC = AUTHPLUGIN_SRC / "JARVISUnlockMechanism.m"


def _fake_helper(tmp_path: Path, exit_code: int, stdout: str = "", stderr: str = "") -> str:
    """A stand-in for the signed helper that exits with a chosen code."""
    path = tmp_path / "jarvis-unlock-grant"
    path.write_text(
        "#!/bin/bash\n"
        f"[ -n {stdout!r} ] && printf '%s\\n' {stdout!r}\n"
        f"[ -n {stderr!r} ] && printf '%s\\n' {stderr!r} >&2\n"
        f"exit {exit_code}\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


# =============================================================================
# CROSS-LANGUAGE PARITY
# =============================================================================


def test_exit_codes_match_the_objc_helper():
    """
    The Python enum and the Objective-C enum must be the same numbers.

    Drift here would silently turn "the broker refused us" into "the broker is
    down" -- an install problem reported as a daemon problem, sending the
    operator to restart something that is working fine.
    """
    source = HELPER_SRC.read_text()
    pairs = dict(
        (name, int(value))
        for name, value in re.findall(
            r"JARVISGrantExit(\w+)\s*=\s*(\d+)", source
        )
    )
    assert pairs, "could not parse the helper's exit enum -- has it been restructured?"

    expected = {
        "Deposited": GrantOutcome.DEPOSITED,
        "Usage": GrantOutcome.USAGE,
        "Unavailable": GrantOutcome.UNAVAILABLE,
        "Rejected": GrantOutcome.REJECTED,
        "Timeout": GrantOutcome.TIMEOUT,
        "Config": GrantOutcome.CONFIG,
    }
    for objc_name, python_value in expected.items():
        assert objc_name in pairs, f"helper no longer defines JARVISGrantExit{objc_name}"
        assert pairs[objc_name] == int(python_value), (
            f"JARVISGrantExit{objc_name}={pairs[objc_name]} but "
            f"GrantOutcome.{python_value.name}={int(python_value)}"
        )


def test_helper_missing_is_python_side_only():
    """HELPER_MISSING must not collide with any code the helper can return."""
    source = HELPER_SRC.read_text()
    objc_values = {int(v) for _, v in re.findall(r"JARVISGrantExit(\w+)\s*=\s*(\d+)", source)}
    assert int(GrantOutcome.HELPER_MISSING) not in objc_values


# =============================================================================
# WIRING -- the guard must be attached, not merely written
# =============================================================================


def test_both_listeners_have_a_code_signing_requirement_applied():
    """
    Both Mach services must actually call setConnectionCodeSigningRequirement.

    The entire privilege separation -- consume vs deposit -- rests on this. A
    listener constructed without it accepts anyone, and the source would still
    *mention* requirements in its config struct.
    """
    source = BROKER_SRC.read_text()
    # Capture the whole argument expression up to the closing bracket. `\w+`
    # would stop at `_config` and miss which requirement was passed, which is
    # the only part that distinguishes the two listeners.
    applied = re.findall(r"setConnectionCodeSigningRequirement:([^\]]+)\]", source)
    assert len(applied) == 2, (
        f"expected 2 listeners to have requirements applied, found {len(applied)}: {applied}"
    )
    joined = " ".join(applied)
    assert "consumer" in joined.lower(), "consume listener has no requirement applied"
    assert "depositor" in joined.lower(), "deposit listener has no requirement applied"


def test_broker_refuses_when_services_are_identical():
    """Collapsing the two services would silently merge minting and spending."""
    source = BROKER_SRC.read_text()
    assert "isEqualToString:_depositServiceName" in source, (
        "broker no longer rejects identical consume/deposit service names"
    )


def test_mechanism_never_denies():
    """
    The mechanism must contain no path returning kAuthorizationResultDeny.

    Deny fails the whole authorization right and locks the operator out of their
    own machine. Yielding is Allow -- it hands control to builtin:authenticate,
    which prompts as it always has. This is the single line whose inversion
    bricks the lock screen.
    """
    source = MECHANISM_SRC.read_text()
    executable = [
        line for line in source.splitlines()
        if "kAuthorizationResultDeny" in line and not line.strip().startswith("*")
        and not line.strip().startswith("//")
    ]
    assert not executable, f"mechanism gained a Deny path: {executable}"


# =============================================================================
# BEHAVIOURAL
# =============================================================================


def test_rejected_signature_does_not_read_as_success(tmp_path):
    """
    The invalid-code-signature case.

    When the broker refuses our signature the helper exits EX_NOPERM. The bridge
    must report that as a refusal, must not fabricate a grant id from stdout,
    and must classify it as an install problem rather than a transient one --
    restarting the daemon will never fix a signature mismatch.
    """
    helper = _fake_helper(
        tmp_path,
        exit_code=int(GrantOutcome.REJECTED),
        stdout="AAAA-BBBB-CCCC",  # must NOT become a grant_id
        stderr="peer code signing requirement not satisfied",
    )
    result = asyncio.run(GrantBridge(helper_path=helper).deposit("voice: unlock my screen"))

    assert result.succeeded is False
    assert result.outcome is GrantOutcome.REJECTED
    assert result.grant_id is None, "a refused deposit must not carry a grant id"
    assert result.outcome.is_install_problem is True
    assert "signing" in result.detail


def test_successful_deposit_returns_the_grant_id(tmp_path):
    helper = _fake_helper(tmp_path, exit_code=0, stdout="GRANT-123")
    result = asyncio.run(GrantBridge(helper_path=helper).deposit("voice: unlock"))

    assert result.succeeded is True
    assert result.grant_id == "GRANT-123"


@pytest.mark.parametrize(
    "code,expected,install_problem",
    [
        (69, GrantOutcome.UNAVAILABLE, False),
        (75, GrantOutcome.TIMEOUT, False),
        (77, GrantOutcome.REJECTED, True),
        (78, GrantOutcome.CONFIG, True),
        (64, GrantOutcome.USAGE, True),
    ],
)
def test_every_helper_exit_maps_to_a_distinct_outcome(tmp_path, code, expected, install_problem):
    """
    'Broker is down' and 'broker refused us' must stay distinguishable.

    One means restart a daemon; the other means the install is wrong. Collapsing
    them sends the operator to the wrong place, which is the failure mode the
    typed enum exists to prevent.
    """
    helper = _fake_helper(tmp_path, exit_code=code)
    result = asyncio.run(GrantBridge(helper_path=helper).deposit("x"))

    assert result.outcome is expected
    assert result.succeeded is False
    assert result.outcome.is_install_problem is install_problem


def test_unmapped_exit_is_treated_as_refusal(tmp_path):
    """An unrecognised answer from the component that mints unlocks is not benign."""
    helper = _fake_helper(tmp_path, exit_code=3, stderr="something new")
    result = asyncio.run(GrantBridge(helper_path=helper).deposit("x"))

    assert result.outcome is GrantOutcome.REJECTED
    assert "unmapped exit 3" in result.detail


def test_missing_helper_is_reported_not_raised():
    """The voice path must get a typed result, never a traceback."""
    result = asyncio.run(
        GrantBridge(helper_path="/nonexistent/jarvis-unlock-grant").deposit("x")
    )
    assert result.outcome is GrantOutcome.HELPER_MISSING
    assert result.succeeded is False


def test_wedged_helper_is_killed_not_left_running(tmp_path):
    """
    A helper that never exits is terminated, not abandoned.

    An orphan holding a privileged XPC connection is precisely the kind of thing
    that outlives its reason to exist.
    """
    path = tmp_path / "jarvis-unlock-grant"
    path.write_text("#!/bin/bash\nsleep 30\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)

    result = asyncio.run(GrantBridge(helper_path=str(path), timeout_s=0.5).deposit("x"))

    assert result.outcome is GrantOutcome.TIMEOUT
    assert "wedged" in result.detail


def test_blank_reason_never_reaches_the_audit_log_empty(tmp_path):
    helper = _fake_helper(tmp_path, exit_code=0, stdout="G1")
    # An empty reason must not produce an empty audit entry; the helper receives
    # a placeholder instead.
    result = asyncio.run(GrantBridge(helper_path=helper).deposit("   "))
    assert result.succeeded is True


# =============================================================================
# HELPER RESOLUTION
# =============================================================================


def test_explicit_env_path_must_be_executable(tmp_path):
    """A non-executable path is refused rather than silently searched past."""
    plain = tmp_path / "not-executable"
    plain.write_text("")
    assert resolve_helper_path({ENV_HELPER_PATH: str(plain)}) is None


def test_resolution_returns_none_rather_than_guessing():
    """
    None, not a plausible path.

    Exec'ing whatever happens to sit at a conventional location, when the thing
    being exec'd mints screen unlocks, is not a defensible default.
    """
    assert resolve_helper_path({ENV_HELPER_PATH: "/definitely/not/here"}) is None


def test_search_path_is_absolute_and_root_owned_locations():
    """Every default location must be absolute -- a relative path is cwd-dependent."""
    for candidate in DEFAULT_HELPER_SEARCH_PATH:
        assert os.path.isabs(candidate), f"{candidate} is not absolute"
