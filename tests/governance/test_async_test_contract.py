"""Async test failures: the right label, and the contract that prevents them.

bt-2026-09-22-201845: 23 validation failures were headlined "RuntimeWarning".
None died of a warning. They died of ``asyncio.run() cannot be called from a
running event loop``, ``no running event loop``, ``TypeError`` -- and the
digest took its class from the first bare ``...Warning:`` line, which in a
pytest report is the warnings summary (``coroutine ... was never awaited``, the
SYMPTOM of the asyncio.run failure). That headline feeds the re-planner and
the GRPO corpus.

Underneath, the model wrote the wrong TEST SHAPE for the subject: a plain test
for a sync function that needs a running loop, an async test around code that
starts its own. The signature said ``def`` in every case; only the body and
the repo's ``asyncio_mode`` say which shape can run it.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.ast_signature_anchor import (
    build_signature_anchor,
    extract_public_api,
    pytest_asyncio_mode_line,
)
from backend.core.ouroboros.governance.test_failure_digest import digest_from_text

RUNNER_MOD = '''\
import asyncio


async def _work():
    return 1


def kick():
    return asyncio.run(_work())
'''

ASYNC_TEST_OF_A_LOOP_STARTER = '''\
from backend.runner_mod import kick


async def test_kick():
    assert kick() == 1
'''


def _real_pytest_output(tmp_path: Path, color: str = "no") -> str:
    """The real run, streamed the way TestRunner streams it.

    The ``RuntimeWarning`` is not in pytest's report at all: Python writes
    ``sys:1: RuntimeWarning: coroutine ... was never awaited`` to STDERR at
    interpreter shutdown, and TestRunner's streaming path merges the two pipes
    by arrival, so it lands between the FAILURES section and the short
    summary. That placement is what made it the digest's first class seen."""
    (tmp_path / "pytest.ini").write_text("[pytest]\nasyncio_mode = auto\npythonpath = .\n")
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/runner_mod.py").write_text(RUNNER_MOD)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_kick.py").write_text(ASYNC_TEST_OF_A_LOOP_STARTER)
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-o", "addopts=", "-p", "no:cacheprovider",
         "-v", "--tb=short", f"--color={color}", "tests/test_kick.py"],
        cwd=tmp_path, capture_output=True, text=True, timeout=120,
    )
    rule = next(
        (ln for ln in done.stdout.splitlines(keepends=True) if "short test summary info" in ln),
        "",
    )
    if not rule:
        return done.stdout + done.stderr
    return done.stdout.replace(rule, done.stderr + rule, 1)


# ---------------------------------------------------------------------------
# The digest names the failure, not the warning it left behind
# ---------------------------------------------------------------------------

# "no" is what TestRunner produces (-o addopts= drops the ini's --color=yes)
# and is the soak's exact shape; "yes" is what the repair sandbox produces.
@pytest.mark.parametrize("color", ["no", "yes"])
def test_the_real_failure_is_the_class_not_the_warnings_summary(tmp_path, color):
    out = _real_pytest_output(tmp_path, color)
    assert "never awaited" in out, "the fixture must reproduce the soak's warning"
    digest = digest_from_text(out)
    assert digest.error_class == "RuntimeError", digest.headline
    assert digest.headline.startswith("RuntimeError · test_kick")
    assert "cannot be called from a running event loop" in digest.headline


def test_a_warning_that_failed_the_test_is_still_its_class():
    """-W error turns a warning into the failure; pytest then reports it on the
    FAILED line, which outranks everything else."""
    out = (
        "=================================== FAILURES ===================================\n"
        "E   RuntimeWarning: coroutine 'x' was never awaited\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/t.py::test_a - RuntimeWarning: coroutine 'x' was never awaited\n"
    )
    assert digest_from_text(out).error_class == "RuntimeWarning"


def test_a_bare_warning_line_alone_is_not_a_failure_class():
    out = "tests/t.py:3: RuntimeWarning: coroutine 'x' was never awaited\n  x()\n"
    out = out.replace("tests/t.py:3: ", "")
    assert digest_from_text(out).error_class == ""


def test_the_hint_still_wins():
    assert digest_from_text("E   TypeError: x", error_class_hint="SyntaxError").error_class == "SyntaxError"


# ---------------------------------------------------------------------------
# The anchor states which test shape can call each sync function
# ---------------------------------------------------------------------------

SUBJECT = '''\
import asyncio
import subprocess


async def _loader():
    return 1


def schedule_preload():
    """Kick off background loading."""
    asyncio.create_task(_loader())


def run_blocking():
    return asyncio.run(_loader())


def shell():
    return subprocess.run(["true"])


async def fetch():
    return await _loader()


def outer():
    def inner():
        return asyncio.run(_loader())
    return inner
'''


def test_a_sync_function_that_needs_a_loop_says_so():
    api = extract_public_api(SUBJECT, "backend.subject")
    block = api.split("def schedule_preload", 1)[1].split("def run_blocking", 1)[0]
    assert "needs a RUNNING one (asyncio.create_task" in block
    assert "`async def` test" in block


def test_a_sync_function_that_starts_a_loop_says_so():
    block = extract_public_api(SUBJECT, "m").split("def run_blocking", 1)[1].split("def shell", 1)[0]
    assert "STARTS its own (asyncio run" in block
    assert "plain `def` test" in block


@pytest.mark.parametrize("name", ["shell", "fetch", "outer"])
def test_no_contract_where_there_is_none(name):
    """subprocess.run is not asyncio.run; async def is self-describing; a
    nested def's call belongs to the nested def."""
    api = extract_public_api(SUBJECT, "m")
    after = api.split(f"def {name}", 1)[1]
    block = after.split("\ndef ", 1)[0].split("\nasync def ", 1)[0]
    assert "# event loop:" not in block


def test_a_module_that_runs_a_loop_at_import_says_so():
    src = "import asyncio\n\nasync def verify():\n    return 1\n\nok = asyncio.run(verify())\n\ndef helper():\n    return ok\n"
    api = extract_public_api(src, "backend.fresh")
    assert "importing this module runs asyncio run (line 6)" in api


@pytest.mark.parametrize("guard", [
    'if __name__ == "__main__":', "if '__main__' == __name__:",
])
def test_a_main_guarded_loop_does_not_run_at_import(guard):
    """Found on the real smart_startup_manager: the script idiom was reported
    as running a loop at import, which it never does."""
    src = f"import asyncio\n\nasync def main():\n    return 1\n\ndef helper():\n    return 1\n\n{guard}\n    asyncio.run(main())\n"
    assert "importing this module" not in extract_public_api(src, "m")


# ---------------------------------------------------------------------------
# The runner's asyncio_mode comes from the repo's own config
# ---------------------------------------------------------------------------

def test_mode_is_read_from_pytest_ini(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\nasyncio_mode = auto\n")
    assert "asyncio_mode=auto (pytest.ini)" in pytest_asyncio_mode_line(tmp_path)


def test_mode_is_read_from_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\nasyncio_mode = "strict"\n')
    assert "@pytest.mark.asyncio" in pytest_asyncio_mode_line(tmp_path)


def test_pytest_ini_wins_over_pyproject_like_pytest(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\npythonpath = .\n")
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\nasyncio_mode = "auto"\n')
    assert pytest_asyncio_mode_line(tmp_path) == "", "pytest stops at the first config file"


def test_the_runner_line_reaches_a_test_writing_prompt_only(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\nasyncio_mode = auto\n")
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/subject.py").write_text(SUBJECT)
    for_test = build_signature_anchor(
        ("tests/test_subject.py",), "Write tests for backend/subject.py", tmp_path,
    )
    assert "asyncio_mode=auto" in for_test
    assert "needs a RUNNING one" in for_test
    for_source = build_signature_anchor(("backend/subject.py",), "refactor", tmp_path)
    assert "asyncio_mode" not in for_source
