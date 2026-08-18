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
from pathlib import Path

import pytest

pyflakes_checker = pytest.importorskip(
    "pyflakes.checker",
    reason="pyflakes is declared in ci/requirements-ov-surface.txt; a runner "
           "without it cannot enforce this gate",
)
from pyflakes import messages as pyflakes_messages  # noqa: E402

#: The package this gate covers. Chosen because it is at zero today and it is
#: what an operator runs.
GATED = Path("backend/core/ouroboros/cli")

#: Packages knowingly NOT covered, with their counts at the time this gate
#: landed. Named so nobody reads a passing run as a repo-wide clean bill.
UNGATED_KNOWN = {
    "backend/core/ouroboros/governance": 16,
    "backend/core/ouroboros/battle_test": 8,
}


def _undefined_names(path: Path):
    src = path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src, filename=str(path))
    check = pyflakes_checker.Checker(tree, filename=str(path))
    return [
        (m.lineno, m.message % m.message_args)
        for m in check.messages
        if isinstance(m, pyflakes_messages.UndefinedName)
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


def test_the_gate_can_actually_fail():
    """Guards the guard. A detector that cannot fire proves nothing."""
    tree = ast.parse("def f():\n    return no_such_name\n", filename="<probe>")
    check = pyflakes_checker.Checker(tree, filename="<probe>")
    found = [m for m in check.messages
             if isinstance(m, pyflakes_messages.UndefinedName)]
    assert found, "pyflakes did not flag an obviously undefined name"


def test_ungated_packages_are_named_not_forgotten():
    """The honest half: this gate is scoped, and the scope is written down.

    A passing run means `cli/` is clean, never that the repository is."""
    assert UNGATED_KNOWN, "if nothing is ungated, say so explicitly"
    for pkg in UNGATED_KNOWN:
        assert (_repo_root() / pkg).is_dir(), (
            f"{pkg} is named as ungated but does not exist — this note has "
            "gone stale and would mislead the next reader"
        )
