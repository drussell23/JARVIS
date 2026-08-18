"""No name may be used in `cli/` that nothing defines.

TWICE NOW, in the operator's front door, and both times invisibly:

* `thin_client.spawn_daemon` called `logger.debug(...)` while the module
  imported no `logging` and defined no `logger` (since 2026-08-08, #70440).
  Every ignition raised `NameError`, the function's blanket
  `except Exception: return None` swallowed it, and `ensure_daemon` reported
  the single sentence "⚠ ignition failed". Bare `ov` could not cold-boot the
  organism for ten days.

* `ov._client_extra_bindings` did the same, then referenced `logger` AGAIN
  inside the handler catching the first NameError, so the outer
  `except Exception: return None` discarded the ENTIRE extra key-binding set:
  confirm actions, the completion arbiter, paste collapse, rewind, transcript
  hatches and transcript mode. All built, all mounted, all thrown away one
  line later.

`ov.py` also used a bare `asyncio` twice in a scope whose alias is `_aio`, so
the RMS keeper task was never created and a cleanup `except` clause raised
while handling another exception.

Nothing caught any of it. `flake8` runs in `ci-cd-pipeline.yml` as
`flake8 backend/ ... || true` — the result is discarded, which is the same
"green because nobody looked" failure this repo keeps paying for. Removing
that `|| true` is not available: the repo carries ~812 undefined-name findings
overall, most in vendored `venv/` and in `core/quarantine`.

So this gate is SCOPED to where the count is genuinely zero and the blast
radius is the operator's cockpit. It is deliberately not repo-wide, and it
says so, because a gate that cannot pass is a gate somebody disables.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

pytest.importorskip(
    "pyflakes",
    reason="pyflakes is declared in ci/requirements-ov-surface.txt; a runner "
           "without it cannot enforce this gate",
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# ONE analyzer, shared with the differential gate. This file previously
# carried its own pyflakes wrapper, and a second opinion about what counts as
# an undefined name is how the two would eventually disagree about a build.
from ci.lint_gate import undefined_names as _undefined_names_shared  # noqa: E402

#: The package this gate covers. Chosen because it is at zero today and it is
#: what an operator runs.
GATED = Path("backend/core/ouroboros/cli")

#: Packages knowingly NOT covered. Named so nobody reads a passing run as a
#: repo-wide clean bill.
#:
#: `governance` and `battle_test` USED to be here (16 and 8 findings). Both
#: reached zero and were promoted into `ci.lint_gate.DEFAULT_CLEAN_PACKAGES`,
#: which is now the ratchet of record. What remains ungated is the rest of the
#: repository — ~812 findings dominated by vendored `venv/` and
#: `core/quarantine/`, held by the DIFFERENTIAL gate instead.
UNGATED_KNOWN = {
    "backend/core/quarantine": "excluded — quarantined by name",
    "backend/api": "differential gate only",
}


def _undefined_names(path: Path):
    """Real (non-inert) findings only — delegated to the shared analyzer."""
    return [
        (f.lineno, f"undefined name '{f.name}'")
        for f in _undefined_names_shared(path)
        if not f.inert
    ]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_cli_package_has_no_undefined_names():
    root = _repo_root()
    target = root / GATED
    assert target.is_dir(), f"gated package missing: {target}"

    offenders = []
    for path in sorted(target.glob("*.py")):
        for lineno, msg in _undefined_names(path):
            offenders.append(f"{path.relative_to(root)}:{lineno}: {msg}")

    assert not offenders, (
        "a name is used here that nothing defines. Every occurrence of this "
        "in `cli/` so far has been swallowed by a blanket `except Exception` "
        "and surfaced only as a cockpit that would not start:\n  "
        + "\n  ".join(offenders)
    )


def test_the_gate_can_actually_fail(tmp_path):
    """Guards the guard. A detector that cannot fire proves nothing."""
    probe = tmp_path / "probe.py"
    probe.write_text("def f():\n    return no_such_name\n", encoding="utf-8")
    assert _undefined_names(probe), (
        "the shared analyzer did not flag an obviously undefined name"
    )


def test_ungated_packages_are_named_not_forgotten():
    """The honest half: this gate is scoped, and the scope is written down.

    A passing run means `cli/` is clean, never that the repository is."""
    assert UNGATED_KNOWN, "if nothing is ungated, say so explicitly"
    for pkg in UNGATED_KNOWN:
        assert (_repo_root() / pkg).is_dir(), (
            f"{pkg} is named as ungated but does not exist — this note has "
            "gone stale and would mislead the next reader"
        )

    # And the converse: anything claimed CLEAN must actually be gated, or the
    # ratchet is a comment rather than a mechanism.
    import sys as _sys
    _sys.path.insert(0, str(_repo_root()))
    from ci.lint_gate import DEFAULT_CLEAN_PACKAGES
    assert str(GATED) in DEFAULT_CLEAN_PACKAGES, (
        "this file gates cli/, so cli/ must be in the shared ratchet too"
    )
    for pkg in DEFAULT_CLEAN_PACKAGES:
        assert pkg not in UNGATED_KNOWN, (
            f"{pkg} is claimed both gated and ungated"
        )
