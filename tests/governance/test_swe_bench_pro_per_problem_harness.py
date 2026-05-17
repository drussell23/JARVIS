"""Regression spine for v3.7 Phase 2 Phase B.1 — per-problem harness substrate.

Pins the load-bearing structural invariants for
:mod:`backend.core.ouroboros.governance.swe_bench_pro.per_problem_harness`:

* ``HarnessOutcome`` 5-value taxonomy bytes-pinned (AST class-body
  walk asserts the value-set strings exactly)
* ``DiffCaptureOutcome`` 3-value taxonomy bytes-pinned
* ``PreparedProblem`` frozen dataclass + symmetric to_dict /
  from_dict round-trip (§33.5)
* Master flag short-circuit — ``MASTER_FLAG_OFF`` outcome when
  ``JARVIS_SWE_BENCH_PRO_ENABLED`` unset
* Path sanitization is filesystem-safe + deterministic
* Repo-cache path derivation strips https/git@/.git correctly
* Real-git integration (via ``tmp_path``):
  - prepare_problem against a local-bare repo (file:// URL) → READY
  - test_patch application + diff capture round-trip
  - cleanup_prepared removes worktree + branch
* Authority asymmetry pin — module MUST NOT import policy
  substrates (orchestrator / iron_gate / etc)
* Canonical safe-subprocess composition pin (AST-based) —
  ``asyncio.to_thread`` + ``subprocess.run`` (the loop-decoupled
  pattern that survives event-loop starvation); FORBIDS the v2
  ``asyncio.create_subprocess_exec`` + ``proc.communicate()``
  pattern (which exhibited a 2500× slowdown under sensor
  contention during 2026-05-12 stage-1 wiring soak); FORBIDS
  ``shell=True`` and direct os-level command execution
* Event-loop starvation contention regression — runs ``_run_git``
  alongside 8 CPU-spinning coroutines and asserts it completes
  in << what the v2 pattern took (the broken pattern timed out
  at 60s on a sub-second workload)
* FlagRegistry self-registration registers exactly 4 specs
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import os
import subprocess
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.swe_bench_pro import per_problem_harness
from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import (
    ProblemSpec,
)
from backend.core.ouroboros.governance.swe_bench_pro.per_problem_harness import (
    DiffCaptureOutcome,
    HarnessOutcome,
    PER_PROBLEM_HARNESS_SCHEMA_VERSION,
    PreparedProblem,
    capture_produced_patch,
    cleanup_prepared,
    prepare_problem,
    repo_cache_path,
    worktree_base_path,
)


_HARNESS_SRC = Path(
    inspect.getfile(per_problem_harness),
).read_text(encoding="utf-8")
_HARNESS_AST = ast.parse(_HARNESS_SRC)


# ===========================================================================
# Closed taxonomy pins
# ===========================================================================


def test_harness_outcome_taxonomy_is_closed_five_values():
    values = {m.value for m in HarnessOutcome}
    assert values == {
        "ready", "master_flag_off", "clone_failed",
        "worktree_create_failed", "test_patch_failed",
    }, f"HarnessOutcome taxonomy drift; got {sorted(values)}"


def test_harness_outcome_class_body_ast_bytes_pinned():
    cls_node = next(
        (n for n in ast.walk(_HARNESS_AST)
         if isinstance(n, ast.ClassDef) and n.name == "HarnessOutcome"),
        None,
    )
    assert cls_node is not None
    names = [
        a.targets[0].id for a in cls_node.body
        if isinstance(a, ast.Assign)
        and len(a.targets) == 1
        and isinstance(a.targets[0], ast.Name)
    ]
    assert names == [
        "READY", "MASTER_FLAG_OFF", "CLONE_FAILED",
        "WORKTREE_CREATE_FAILED", "TEST_PATCH_FAILED",
    ]


def test_diff_capture_outcome_taxonomy_is_closed_three_values():
    values = {m.value for m in DiffCaptureOutcome}
    assert values == {"captured", "no_changes", "capture_failed"}


def test_diff_capture_outcome_class_body_ast_bytes_pinned():
    cls_node = next(
        (n for n in ast.walk(_HARNESS_AST)
         if isinstance(n, ast.ClassDef) and n.name == "DiffCaptureOutcome"),
        None,
    )
    assert cls_node is not None
    names = [
        a.targets[0].id for a in cls_node.body
        if isinstance(a, ast.Assign)
        and len(a.targets) == 1
        and isinstance(a.targets[0], ast.Name)
    ]
    assert names == ["CAPTURED", "NO_CHANGES", "CAPTURE_FAILED"]


# ===========================================================================
# Schema version
# ===========================================================================


def test_schema_version_constant_pinned():
    assert PER_PROBLEM_HARNESS_SCHEMA_VERSION == "swe_bench_pro_prepared.v1"


# ===========================================================================
# PreparedProblem — frozen + symmetric round-trip (§33.5)
# ===========================================================================


def _sample_prepared(tmp_path: Path) -> PreparedProblem:
    return PreparedProblem(
        problem_instance_id="astropy__astropy-12907",
        worktree_path=tmp_path / "wt",
        base_commit="d16bfe05a744909de4b27f5875fe0d4ed41ce607",
        repo_url="https://github.com/astropy/astropy.git",
        branch_name="swebp/astropy__astropy-12907",
        target_paths=("astropy/modeling/separable.py",),
        elapsed_s=2.5,
    )


def test_prepared_problem_is_frozen(tmp_path):
    p = _sample_prepared(tmp_path)
    with pytest.raises(Exception):
        p.problem_instance_id = "different"  # type: ignore[misc]


def test_prepared_problem_round_trip(tmp_path):
    p = _sample_prepared(tmp_path)
    rebuilt = PreparedProblem.from_dict(p.to_dict())
    assert rebuilt == p


def test_prepared_problem_to_dict_serialization(tmp_path):
    p = _sample_prepared(tmp_path)
    data = p.to_dict()
    for key in (
        "schema_version", "problem_instance_id", "worktree_path",
        "base_commit", "repo_url", "branch_name", "target_paths",
        "elapsed_s",
    ):
        assert key in data
    assert isinstance(data["worktree_path"], str)
    assert isinstance(data["target_paths"], list)


# ===========================================================================
# Path sanitization
# ===========================================================================


def test_sanitize_replaces_unsafe_chars():
    s = per_problem_harness._sanitize_for_filename
    assert s("astropy/astropy") == "astropy_astropy"
    assert s("foo:bar") == "foo_bar"
    assert s("foo bar") == "foo_bar"
    assert s("path\\with\\slashes") == "path_with_slashes"


def test_sanitize_handles_empty():
    assert per_problem_harness._sanitize_for_filename("") == "_unnamed"


def test_sanitize_is_pure():
    s = per_problem_harness._sanitize_for_filename
    assert s("test_id") == s("test_id")
    assert s("astropy/astropy-12907") == "astropy_astropy-12907"


# ===========================================================================
# Repo cache path derivation
# ===========================================================================


def test_cached_repo_path_strips_https():
    p = per_problem_harness._cached_repo_path_for(
        "https://github.com/astropy/astropy.git"
    )
    assert "github.com_astropy_astropy" in str(p)


def test_cached_repo_path_strips_git_suffix():
    p1 = per_problem_harness._cached_repo_path_for(
        "https://github.com/x/y.git",
    )
    p2 = per_problem_harness._cached_repo_path_for(
        "https://github.com/x/y",
    )
    assert p1 == p2


def test_cached_repo_path_handles_ssh_form():
    p = per_problem_harness._cached_repo_path_for(
        "git@github.com:astropy/astropy.git"
    )
    assert "github.com_astropy_astropy" in str(p)


# ===========================================================================
# Branch name composition
# ===========================================================================


def test_branch_name_uses_swebp_prefix():
    branch = per_problem_harness._branch_name_for("astropy__astropy-12907")
    assert branch == "swebp/astropy__astropy-12907"
    assert branch.startswith("swebp/")


def test_branch_name_sanitizes_id():
    branch = per_problem_harness._branch_name_for("foo/bar:baz")
    assert "/" not in branch[len("swebp/"):]
    assert ":" not in branch


# ===========================================================================
# Test-patch target extraction
# ===========================================================================


def test_extract_target_paths_from_diff():
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1 +1 @@\n"
        "-old\n+new\n"
        "diff --git a/bar.py b/bar.py\n"
        "--- a/bar.py\n"
        "+++ b/bar.py\n"
        "@@ -1 +1 @@\n"
        "-old\n+new\n"
    )
    paths = per_problem_harness._extract_target_paths_from_patch(diff)
    assert paths == ("foo.py", "bar.py")


def test_extract_target_paths_dedups():
    diff = "+++ b/foo.py\n+++ b/foo.py\n+++ b/bar.py\n"
    assert per_problem_harness._extract_target_paths_from_patch(diff) == (
        "foo.py", "bar.py",
    )


def test_extract_target_paths_skips_dev_null():
    diff = "+++ b/foo.py\n+++ b//dev/null\n"
    paths = per_problem_harness._extract_target_paths_from_patch(diff)
    assert "/dev/null" not in paths


def test_extract_target_paths_empty():
    assert per_problem_harness._extract_target_paths_from_patch("") == ()


# ===========================================================================
# Master flag short-circuit
# ===========================================================================


def test_prepare_problem_short_circuits_when_master_flag_off(monkeypatch):
    """Production-byte-identical contract: when master flag is off,
    prepare_problem MUST return (None, MASTER_FLAG_OFF) without
    performing any I/O."""
    monkeypatch.delenv("JARVIS_SWE_BENCH_PRO_ENABLED", raising=False)
    spec = ProblemSpec(
        instance_id="x", repo="x/x", base_commit="abc",
        problem_statement="", test_patch="", gold_patch="",
    )
    result, outcome = asyncio.run(prepare_problem(spec))
    assert result is None
    assert outcome == HarnessOutcome.MASTER_FLAG_OFF


# ===========================================================================
# Real-git integration via tmp_path (no network — file:// URL)
# ===========================================================================


def _bash_git(*args, cwd=None):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True, text=True, check=True,
    ).stdout


def _make_tiny_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "upstream"
    repo.mkdir(parents=True)
    _bash_git("init", "-q", "-b", "main", cwd=repo)
    _bash_git("config", "user.email", "test@example.com", cwd=repo)
    _bash_git("config", "user.name", "Test", cwd=repo)
    (repo / "buggy.py").write_text(
        "def add(a, b):\n    return a - b  # BUG\n",
        encoding="utf-8",
    )
    _bash_git("add", "-A", cwd=repo)
    _bash_git("commit", "-q", "-m", "initial", cwd=repo)
    sha = _bash_git("rev-parse", "HEAD", cwd=repo).strip()
    return repo, sha


def _isolated_env(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_REPO_CACHE_PATH",
        str(tmp_path / "repo_cache"),
    )
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_WORKTREE_BASE_PATH",
        str(tmp_path / "worktrees"),
    )


def test_prepare_problem_end_to_end_with_local_repo(monkeypatch, tmp_path):
    """Full happy path: clone local file:// repo, create worktree
    at base_commit, apply test_patch, return READY."""
    _isolated_env(monkeypatch, tmp_path)
    upstream, sha = _make_tiny_repo(tmp_path)
    test_patch = (
        "diff --git a/test_buggy.py b/test_buggy.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/test_buggy.py\n"
        "@@ -0,0 +1,3 @@\n"
        "+from buggy import add\n"
        "+def test_add():\n"
        "+    assert add(2, 3) == 5\n"
    )
    spec = ProblemSpec(
        instance_id="local__tiny-1",
        repo="local/tiny",
        repo_url=f"file://{upstream}",
        base_commit=sha,
        problem_statement="add() returns subtraction instead",
        test_patch=test_patch,
        gold_patch="",
    )
    prepared, outcome = asyncio.run(prepare_problem(spec))
    assert outcome == HarnessOutcome.READY, (
        f"Expected READY, got {outcome.value}"
    )
    assert prepared is not None
    assert prepared.problem_instance_id == "local__tiny-1"
    assert prepared.base_commit == sha
    assert prepared.branch_name == "swebp/local__tiny-1"
    assert prepared.worktree_path.is_dir()
    assert (prepared.worktree_path / "buggy.py").is_file()
    assert (prepared.worktree_path / "test_buggy.py").is_file()
    assert prepared.target_paths == ("test_buggy.py",)
    asyncio.run(cleanup_prepared(prepared))


def test_capture_produced_patch_round_trip(monkeypatch, tmp_path):
    """After prepare, modify a file in the worktree, then capture
    the diff."""
    _isolated_env(monkeypatch, tmp_path)
    upstream, sha = _make_tiny_repo(tmp_path)
    spec = ProblemSpec(
        instance_id="diff__test-1",
        repo="local/tiny",
        repo_url=f"file://{upstream}",
        base_commit=sha,
        problem_statement="",
        test_patch="",
        gold_patch="",
    )
    prepared, outcome = asyncio.run(prepare_problem(spec))
    assert outcome == HarnessOutcome.READY
    assert prepared is not None

    (prepared.worktree_path / "buggy.py").write_text(
        "def add(a, b):\n    return a + b  # FIXED\n",
        encoding="utf-8",
    )
    _bash_git("add", "-A", cwd=prepared.worktree_path)
    _bash_git(
        "-c", "user.email=test@example.com",
        "-c", "user.name=Test",
        "commit", "-q", "-m", "fix",
        cwd=prepared.worktree_path,
    )

    diff, dco = asyncio.run(capture_produced_patch(prepared))
    assert dco == DiffCaptureOutcome.CAPTURED
    assert diff is not None
    assert "+    return a + b" in diff
    assert "-    return a - b" in diff

    asyncio.run(cleanup_prepared(prepared))


def test_capture_produced_patch_no_changes(monkeypatch, tmp_path):
    """When worktree HEAD is base_commit (no model-produced
    changes), capture returns NO_CHANGES."""
    _isolated_env(monkeypatch, tmp_path)
    upstream, sha = _make_tiny_repo(tmp_path)
    spec = ProblemSpec(
        instance_id="no_changes__test-1",
        repo="local/tiny",
        repo_url=f"file://{upstream}",
        base_commit=sha,
        problem_statement="",
        test_patch="",
        gold_patch="",
    )
    prepared, outcome = asyncio.run(prepare_problem(spec))
    assert outcome == HarnessOutcome.READY
    assert prepared is not None
    diff, dco = asyncio.run(capture_produced_patch(prepared))
    assert dco == DiffCaptureOutcome.NO_CHANGES
    assert diff is None
    asyncio.run(cleanup_prepared(prepared))


def test_prepare_problem_caches_repo_for_reuse(monkeypatch, tmp_path):
    """Two problems against the same upstream URL share the
    cached clone."""
    _isolated_env(monkeypatch, tmp_path)
    upstream, sha = _make_tiny_repo(tmp_path)
    cache_root = Path(os.environ["JARVIS_SWE_BENCH_PRO_REPO_CACHE_PATH"])

    spec1 = ProblemSpec(
        instance_id="cache__test-1",
        repo="local/tiny",
        repo_url=f"file://{upstream}",
        base_commit=sha,
        problem_statement="", test_patch="", gold_patch="",
    )
    p1, o1 = asyncio.run(prepare_problem(spec1))
    assert o1 == HarnessOutcome.READY
    assert p1 is not None

    cached_after_first = list(cache_root.iterdir())
    assert len(cached_after_first) == 1

    spec2 = ProblemSpec(
        instance_id="cache__test-2",
        repo="local/tiny",
        repo_url=f"file://{upstream}",
        base_commit=sha,
        problem_statement="", test_patch="", gold_patch="",
    )
    p2, o2 = asyncio.run(prepare_problem(spec2))
    assert o2 == HarnessOutcome.READY
    assert p2 is not None

    cached_after_second = list(cache_root.iterdir())
    assert len(cached_after_second) == 1
    assert cached_after_first == cached_after_second

    wt_root = Path(os.environ["JARVIS_SWE_BENCH_PRO_WORKTREE_BASE_PATH"])
    assert len(list(wt_root.iterdir())) == 2

    asyncio.run(cleanup_prepared(p1))
    asyncio.run(cleanup_prepared(p2))


def test_prepare_problem_clone_failed_on_invalid_url(monkeypatch, tmp_path):
    _isolated_env(monkeypatch, tmp_path)
    spec = ProblemSpec(
        instance_id="bad__url-1",
        repo="local/missing",
        repo_url="file:///path/that/does/not/exist/at/all/x.git",
        base_commit="abc",
        problem_statement="", test_patch="", gold_patch="",
    )
    result, outcome = asyncio.run(prepare_problem(spec))
    assert result is None
    assert outcome == HarnessOutcome.CLONE_FAILED


def test_prepare_problem_worktree_create_failed_on_invalid_commit(
    monkeypatch, tmp_path,
):
    # Phase C: an invalid base_commit fails at `git worktree add -b
    # <branch> <path> <commit>` — that IS a worktree-creation failure
    # (there is no `git checkout` step). Renamed from the legacy
    # CHECKOUT_FAILED misnomer.
    _isolated_env(monkeypatch, tmp_path)
    upstream, _sha = _make_tiny_repo(tmp_path)
    spec = ProblemSpec(
        instance_id="bad__commit-1",
        repo="local/tiny",
        repo_url=f"file://{upstream}",
        base_commit="0000000000000000000000000000000000000000",
        problem_statement="", test_patch="", gold_patch="",
    )
    result, outcome = asyncio.run(prepare_problem(spec))
    assert result is None
    assert outcome == HarnessOutcome.WORKTREE_CREATE_FAILED


def test_prepare_problem_test_patch_failed_on_malformed_diff(
    monkeypatch, tmp_path,
):
    _isolated_env(monkeypatch, tmp_path)
    upstream, sha = _make_tiny_repo(tmp_path)
    spec = ProblemSpec(
        instance_id="bad__patch-1",
        repo="local/tiny",
        repo_url=f"file://{upstream}",
        base_commit=sha,
        problem_statement="",
        test_patch="this is not a unified diff at all",
        gold_patch="",
    )
    result, outcome = asyncio.run(prepare_problem(spec))
    assert result is None
    assert outcome == HarnessOutcome.TEST_PATCH_FAILED


# ===========================================================================
# Authority asymmetry (§1 Boundary) — AST-pinned forbidden imports
# ===========================================================================


_FORBIDDEN_IMPORT_PREFIXES = (
    ".governance.orchestrator",
    ".governance.iron_gate",
    ".governance.change_engine",
    ".governance.candidate_generator",
    ".governance.policy_engine",
    ".governance.risk_tier",
    ".governance.repair_engine",
)


def test_forbidden_imports_not_present():
    found = []
    for node in ast.walk(_HARNESS_AST):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            normalized = module
            if normalized.startswith("backend.core.ouroboros"):
                normalized = normalized[len("backend.core.ouroboros"):]
            for prefix in _FORBIDDEN_IMPORT_PREFIXES:
                if normalized.endswith(prefix) or prefix in normalized:
                    found.append((module, prefix))
    assert found == [], (
        f"Phase B.1 has forbidden authority-inverting imports: {found}"
    )


# ===========================================================================
# Canonical safe-subprocess composition (AST-based pins)
# ===========================================================================


def _walk_calls():
    """Yield every Call node in the harness AST."""
    for node in ast.walk(_HARNESS_AST):
        if isinstance(node, ast.Call):
            yield node


def _attribute_chain(node):
    """Return dotted name of an Attribute chain, e.g.
    ``asyncio.create_subprocess_exec`` for nested Attribute nodes.
    Returns None if the chain isn't pure Name+Attribute."""
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return None
    parts.append(cur.id)
    return ".".join(reversed(parts))


def test_no_os_level_command_execution():
    """AST pin: substrate MUST NOT call direct os-level command
    execution surfaces (``os.system`` / ``os.popen``) — those
    invoke a shell interpreter and bypass the program+args list
    invariant."""
    forbidden = {"os.system", "os.popen"}
    found = []
    for call in _walk_calls():
        chain = _attribute_chain(call.func)
        if chain in forbidden:
            found.append(chain)
    assert found == [], (
        f"Phase B.1 substrate has forbidden os-level command "
        f"execution calls: {found}"
    )


def test_no_shell_true_kwarg():
    """AST pin: NO subprocess call uses ``shell=True``."""
    found = []
    for call in _walk_calls():
        for kw in call.keywords:
            if kw.arg == "shell":
                # shell=True or shell=<truthy literal>
                if isinstance(kw.value, ast.Constant) and kw.value.value:
                    found.append(ast.unparse(call))
    assert found == [], (
        f"Phase B.1 substrate uses shell=True in subprocess: {found}"
    )


def test_uses_loop_decoupled_subprocess_pattern():
    """AST pin: substrate MUST compose ``asyncio.to_thread`` +
    ``subprocess.run`` (the loop-decoupled pattern that survives
    event-loop starvation under sensor contention).  At least
    one ``asyncio.to_thread`` call AND at least one
    ``subprocess.run`` call must appear.

    Replaces the v2 pin which required
    ``asyncio.create_subprocess_exec``; that v2 pattern was
    falsified in stage-1 wiring soak 2026-05-12 — under harness
    sensor contention, ``proc.communicate()`` await scheduling
    was starved beyond the 60s timeout despite the subprocess
    completing in milliseconds (2500× slowdown vs. standalone).
    """
    has_to_thread = False
    has_subprocess_run = False
    for call in _walk_calls():
        chain = _attribute_chain(call.func)
        if chain == "asyncio.to_thread":
            has_to_thread = True
        elif chain == "subprocess.run":
            has_subprocess_run = True
    assert has_to_thread, (
        "Phase B.1 substrate MUST dispatch git invocations via "
        "asyncio.to_thread — composes the loop-decoupled pattern "
        "that survives event-loop starvation"
    )
    assert has_subprocess_run, (
        "Phase B.1 substrate MUST use subprocess.run inside the "
        "to_thread closure — OS-enforced timeout replaces "
        "scheduler-dependent asyncio.wait_for"
    )


def test_forbids_v2_create_subprocess_exec_pattern():
    """AST pin: substrate MUST NOT use
    ``asyncio.create_subprocess_exec`` — that pattern's
    ``proc.communicate()`` await is scheduler-dependent and was
    starved under harness sensor contention during 2026-05-12
    wiring soak (60s timeout firing on 0.35s real workload)."""
    found = []
    for call in _walk_calls():
        chain = _attribute_chain(call.func)
        if chain == "asyncio.create_subprocess_exec":
            found.append(ast.unparse(call)[:120])
    assert found == [], (
        "Phase B.1 substrate MUST NOT use asyncio.create_subprocess_exec — "
        f"the v2 anti-pattern starved under sensor contention: {found}"
    )


# ===========================================================================
# FlagRegistry self-registration
# ===========================================================================


class _FakeRegistry:
    def __init__(self):
        self.registered = []

    def register(self, spec):
        self.registered.append(spec)


def test_register_flags_registers_four_specs():
    reg = _FakeRegistry()
    count = per_problem_harness.register_flags(reg)
    assert count == 4
    names = sorted(s.name for s in reg.registered)
    assert names == sorted([
        "JARVIS_SWE_BENCH_PRO_REPO_CACHE_PATH",
        "JARVIS_SWE_BENCH_PRO_WORKTREE_BASE_PATH",
        "JARVIS_SWE_BENCH_PRO_GIT_CLONE_TIMEOUT_S",
        "JARVIS_SWE_BENCH_PRO_GIT_OP_TIMEOUT_S",
    ])


def test_register_flags_fail_open_on_registry_failure():
    class _BrokenRegistry:
        def __init__(self):
            self.calls = 0
        def register(self, spec):
            self.calls += 1
            raise RuntimeError("synthetic failure")
    reg = _BrokenRegistry()
    count = per_problem_harness.register_flags(reg)
    assert count == 0
    assert reg.calls == 4


# ===========================================================================
# Env knob defaults + clamping
# ===========================================================================


def test_repo_cache_path_default(monkeypatch):
    monkeypatch.delenv("JARVIS_SWE_BENCH_PRO_REPO_CACHE_PATH", raising=False)
    assert repo_cache_path() == Path(".jarvis/swe_bench_pro/repo_cache")


def test_repo_cache_path_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_REPO_CACHE_PATH", str(tmp_path / "custom"),
    )
    assert repo_cache_path() == tmp_path / "custom"


def test_worktree_base_path_default(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_SWE_BENCH_PRO_WORKTREE_BASE_PATH", raising=False,
    )
    assert worktree_base_path() == Path(".jarvis/swe_bench_pro/worktrees")


def test_git_clone_timeout_default_and_clamping(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_SWE_BENCH_PRO_GIT_CLONE_TIMEOUT_S", raising=False,
    )
    assert per_problem_harness.git_clone_timeout_s() == 600
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_GIT_CLONE_TIMEOUT_S", "120")
    assert per_problem_harness.git_clone_timeout_s() == 120
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_GIT_CLONE_TIMEOUT_S", "garbage")
    assert per_problem_harness.git_clone_timeout_s() == 600
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_GIT_CLONE_TIMEOUT_S", "-50")
    assert per_problem_harness.git_clone_timeout_s() == 1


# ===========================================================================
# AST pin: clone invocation disables template-hook copying
# ===========================================================================
#
# Operator binding 2026-05-12: SWE-Bench-Pro clones don't need or want
# pre-commit / commit-msg / etc. template hooks — they're benchmark
# eval substrates, not contributor checkouts. The ``--template=`` flag
# (empty string) disables the copy. As a side effect this also unblocks
# restricted environments where git's global templates dir is non-
# writable. This pin prevents drift back to template-copying clones.


def test_ast_pin_clone_invocation_disables_template_hooks():
    """The ``--template=`` flag MUST appear in the
    ``_ensure_repo_cached`` clone args list. Drift here re-introduces
    the stage-1 wiring soak failure mode
    (``fatal: cannot copy '/opt/.../templates/hooks/...'``) AND
    silently pollutes benchmark clones with contributor hooks."""
    source = Path(per_problem_harness.__file__).read_text()
    tree = ast.parse(source)
    target_fn = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_ensure_repo_cached"
        ):
            target_fn = node
            break
    assert target_fn is not None, (
        "_ensure_repo_cached not found — pin needs updating"
    )
    fn_text = ast.unparse(target_fn)
    # ast.unparse renders string literals in single quotes (Python's
    # default repr style); match either form for robustness.
    assert "'clone'" in fn_text or '"clone"' in fn_text, (
        "_ensure_repo_cached no longer issues a `clone` subcommand"
    )
    assert "'--template='" in fn_text or '"--template="' in fn_text, (
        "_ensure_repo_cached clone args do NOT include `--template=` "
        "— template-hook copying re-enabled. This will break in "
        "restricted environments (e.g. sandboxes that disallow writes "
        "to /opt/.../templates/hooks/) AND pollute benchmark clones "
        "with contributor hooks. Restore the flag per the operator "
        "binding 2026-05-12."
    )


# ===========================================================================
# Event-loop starvation contention regression — pins the to_thread fix
# ===========================================================================


@pytest.mark.asyncio
async def test_run_git_survives_event_loop_contention(tmp_path):
    """Regression: ``_run_git`` MUST complete under heavy
    event-loop contention.

    The v2 wrapper (``asyncio.create_subprocess_exec`` +
    ``asyncio.wait_for(proc.communicate(), …)``) exhibited a
    2500× slowdown vs. standalone timing under 8 concurrent
    CPU-spinning coroutines — the 60s git timeout fired on a
    sub-second workload because the pipe-reader coroutines
    couldn't be scheduled.  The to_thread + subprocess.run fix
    decouples the subprocess wait from the event loop entirely.

    Test pattern: initialize an empty git repo in tmp_path,
    spawn 8 CPU-spinning coroutines, then invoke ``_run_git
    ['status']`` and assert it completes in << what the v2
    pattern took (60s timeout).  Pass-bar is 10s — under
    asyncio.to_thread the test typically completes in <1s; the
    10s ceiling preserves headroom for slow CI machines while
    still catching any regression to a loop-coupled wait.
    """
    import time as _time

    # Initialize a git repo so `git status` has something to operate on
    repo = tmp_path / "repo"
    repo.mkdir()
    init_rc, _stdout, init_stderr = await per_problem_harness._run_git(
        ["init"],
        cwd=repo,
        timeout_s=30,
    )
    assert init_rc == 0, f"git init failed: {init_stderr}"

    # CPU-spinning coroutines that aggressively yield + recompute —
    # mimics ProactiveExplorationSensor + OpportunityMinerSensor
    # cycles in the harness boot context.
    contention_active = {"stop": False}

    async def _spinner():
        while not contention_active["stop"]:
            x = 0
            for _ in range(50_000):
                x += 1
            await asyncio.sleep(0)

    spinners = [asyncio.create_task(_spinner()) for _ in range(8)]
    try:
        t0 = _time.monotonic()
        rc, _stdout, stderr = await per_problem_harness._run_git(
            ["status", "--porcelain"],
            cwd=repo,
            timeout_s=30,
        )
        elapsed = _time.monotonic() - t0
    finally:
        contention_active["stop"] = True
        for s in spinners:
            s.cancel()
        for s in spinners:
            try:
                await s
            except asyncio.CancelledError:
                pass

    assert rc == 0, (
        f"git status returned rc={rc} under contention "
        f"(stderr={stderr!r}) — this is the v2-regression failure mode "
        f"(timeout firing on completed subprocess)"
    )
    # Generous 10s ceiling.  Standalone is ~50ms; the v2 pattern
    # would have hit the 30s timeout and returned rc=-1.
    assert elapsed < 10.0, (
        f"_run_git took {elapsed:.2f}s under event-loop contention — "
        f"the v2 pattern's scheduler-coupled wait is back. Verify "
        f"_run_git dispatches via asyncio.to_thread + subprocess.run."
    )
