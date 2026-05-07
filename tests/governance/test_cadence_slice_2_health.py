"""Cadence Slice 2 — preflight probe + cadence_health.jsonl
regression spine.

Pins per operator binding 2026-05-06:

  * Probe is pure-function — caller injects paths, NEVER raises
  * EPERM/EACCES → failure_class=os_policy + errno populated
  * ENOENT → failure_class=missing_path
  * Happy path → kind=preflight_ok, subject=""
  * First-failure-wins: probe pinpoints the offending subject
  * record_health_row composes flock_append_line per §33.4 (no
    parallel locking — AST-pinned)
  * Closed taxonomies (KIND_/FAILURE_CLASS_/SUBJECT_) bytes-pinned
  * §33.5 versioned-artifact round-trip
  * Read API for Slice 3: most_recent_preflight_ok_epoch +
    most_recent_preflight_failure
  * Wrapper script invokes preflight before harness; non-zero
    exit aborts (no harness invocation)
  * Cron entry includes preflight invocation before harness
  * cadence_preflight.py defends against substrate import
    failure (sys.path manipulation; runs from any cwd)
  * NEVER raises across all paths

Verifies (32 tests).
"""
from __future__ import annotations

import ast
import errno
import json
import subprocess
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# classify_errno
# ---------------------------------------------------------------------------


def test_classify_eperm():
    from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
        FAILURE_CLASS_OS_POLICY, classify_errno,
    )
    assert classify_errno(errno.EPERM) == FAILURE_CLASS_OS_POLICY


def test_classify_eacces():
    from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
        FAILURE_CLASS_OS_POLICY, classify_errno,
    )
    assert classify_errno(errno.EACCES) == FAILURE_CLASS_OS_POLICY


def test_classify_enoent():
    from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
        FAILURE_CLASS_MISSING_PATH, classify_errno,
    )
    assert classify_errno(errno.ENOENT) == FAILURE_CLASS_MISSING_PATH


def test_classify_none_returns_ok():
    from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
        FAILURE_CLASS_OK, classify_errno,
    )
    assert classify_errno(None) == FAILURE_CLASS_OK


def test_classify_unknown_errno_returns_unexpected():
    from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
        FAILURE_CLASS_UNEXPECTED, classify_errno,
    )
    assert (
        classify_errno(errno.EIO) == FAILURE_CLASS_UNEXPECTED
    )


def test_classify_garbage_returns_unexpected():
    from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
        FAILURE_CLASS_UNEXPECTED, classify_errno,
    )
    assert (
        classify_errno("not_int")  # type: ignore
        == FAILURE_CLASS_UNEXPECTED
    )


def test_errno_name_eperm():
    from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
        errno_name,
    )
    assert errno_name(errno.EPERM) == "EPERM"


def test_errno_name_none():
    from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
        errno_name,
    )
    assert errno_name(None) is None


# ---------------------------------------------------------------------------
# run_preflight — happy + failure paths
# ---------------------------------------------------------------------------


def test_preflight_happy_path(tmp_path):
    from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
        KIND_PREFLIGHT_OK, run_preflight,
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    jarvis = repo / ".jarvis"
    jarvis.mkdir()
    logs = jarvis / "logs"
    logs.mkdir()
    row = run_preflight(
        repo_root=repo,
        jarvis_dir=jarvis,
        log_dir=logs,
        cadence_kind="cron",
    )
    assert row.kind == KIND_PREFLIGHT_OK
    assert row.subject == ""
    assert row.failure_class == "ok"
    assert row.cadence_kind == "cron"


def test_preflight_repo_root_missing(tmp_path):
    from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
        FAILURE_CLASS_MISSING_PATH, KIND_PREFLIGHT_FAILURE,
        run_preflight,
    )
    fake_repo = tmp_path / "nope"
    jarvis = tmp_path / "jarvis"
    jarvis.mkdir()
    row = run_preflight(
        repo_root=fake_repo,
        jarvis_dir=jarvis,
        log_dir=jarvis / "logs",
    )
    assert row.kind == KIND_PREFLIGHT_FAILURE
    assert row.subject == "repo_root"
    assert row.failure_class == FAILURE_CLASS_MISSING_PATH
    assert row.errno_name == "ENOENT"


def test_preflight_jarvis_creates_if_missing(tmp_path):
    """A missing .jarvis/ is recoverable — the probe creates
    it. Should pass cleanly."""
    from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
        KIND_PREFLIGHT_OK, run_preflight,
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    jarvis = repo / ".jarvis"  # NOT created
    logs = jarvis / "logs"
    row = run_preflight(
        repo_root=repo, jarvis_dir=jarvis, log_dir=logs,
    )
    assert row.kind == KIND_PREFLIGHT_OK
    assert jarvis.is_dir()  # created


def test_preflight_log_dir_unwritable_eacces(tmp_path):
    """Read-only log_dir → EACCES → failure_class=os_policy."""
    import os
    from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
        FAILURE_CLASS_OS_POLICY, KIND_PREFLIGHT_FAILURE,
        run_preflight,
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    jarvis = repo / ".jarvis"
    jarvis.mkdir()
    logs = jarvis / "logs"
    logs.mkdir()
    os.chmod(logs, 0o500)  # r-x; no write
    try:
        row = run_preflight(
            repo_root=repo, jarvis_dir=jarvis, log_dir=logs,
        )
        # Behavior depends on root-vs-non-root; allow either
        # outcome (root bypasses), but if it fails it MUST be
        # classified os_policy.
        if row.kind == KIND_PREFLIGHT_FAILURE:
            assert row.subject == "log_dir_write"
            assert row.failure_class == FAILURE_CLASS_OS_POLICY
            assert row.errno_name in ("EACCES", "EPERM")
    finally:
        os.chmod(logs, 0o700)


def test_preflight_never_raises_on_garbage_paths():
    """Defensive — pile of bad inputs MUST NOT raise."""
    from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
        run_preflight,
    )
    fake = Path("/dev/null/nope/cannot")  # invalid path shape
    try:
        row = run_preflight(
            repo_root=fake, jarvis_dir=fake, log_dir=fake,
        )
        assert row is not None
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"raised: {exc}")


# ---------------------------------------------------------------------------
# Append (§33.4 flock) + read API
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_health(monkeypatch, tmp_path):
    target = tmp_path / "cadence_health.jsonl"
    monkeypatch.setenv(
        "JARVIS_CADENCE_HEALTH_PATH", str(target),
    )
    yield target


def test_record_and_read(tmp_health, tmp_path):
    from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
        read_recent, record_health_row, run_preflight,
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    jarvis = repo / ".jarvis"
    jarvis.mkdir()
    logs = jarvis / "logs"
    logs.mkdir()
    row = run_preflight(
        repo_root=repo, jarvis_dir=jarvis, log_dir=logs,
    )
    ok, _ = record_health_row(row)
    assert ok is True
    rows = read_recent()
    assert len(rows) == 1
    assert rows[0].kind == "preflight_ok"


def test_most_recent_preflight_ok_epoch(tmp_health, tmp_path):
    from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
        most_recent_preflight_ok_epoch, record_health_row,
        run_preflight,
    )
    assert most_recent_preflight_ok_epoch() is None
    repo = tmp_path / "repo"
    repo.mkdir()
    jarvis = repo / ".jarvis"
    jarvis.mkdir()
    logs = jarvis / "logs"
    logs.mkdir()
    row = run_preflight(
        repo_root=repo, jarvis_dir=jarvis, log_dir=logs,
    )
    record_health_row(row)
    epoch = most_recent_preflight_ok_epoch()
    assert epoch is not None
    assert epoch == row.ts_epoch


def test_most_recent_preflight_failure(tmp_health, tmp_path):
    from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
        most_recent_preflight_failure, record_health_row,
        run_preflight,
    )
    assert most_recent_preflight_failure() is None
    fake_repo = tmp_path / "missing"
    jarvis = tmp_path / "j"
    jarvis.mkdir()
    row = run_preflight(
        repo_root=fake_repo, jarvis_dir=jarvis,
        log_dir=jarvis / "l",
    )
    record_health_row(row)
    f = most_recent_preflight_failure()
    assert f is not None
    assert f.subject == "repo_root"


def test_read_recent_defensive_on_garbage(tmp_health):
    """Corrupt JSON lines → silently skip; never raise."""
    tmp_health.write_text(
        'not_json\n{"valid": false}\n',
        encoding="utf-8",
    )
    from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
        read_recent,
    )
    rows = read_recent()
    assert isinstance(rows, list)


def test_read_recent_returns_empty_when_missing(tmp_health):
    from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
        read_recent,
    )
    assert read_recent() == []


def test_read_recent_limits():
    from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
        CadenceHealthRow, KIND_PREFLIGHT_OK, read_recent,
        record_health_row, CADENCE_HEALTH_SCHEMA_VERSION,
    )
    import os, tempfile
    fd, p = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    os.unlink(p)
    target = Path(p)
    os.environ["JARVIS_CADENCE_HEALTH_PATH"] = str(target)
    try:
        for i in range(5):
            row = CadenceHealthRow(
                schema_version=CADENCE_HEALTH_SCHEMA_VERSION,
                ts_iso="2026-05-06T00:00:00Z",
                ts_epoch=float(i),
                kind=KIND_PREFLIGHT_OK,
                failure_class="ok",
                errno=None, errno_name=None,
                subject="", detail="row-{}".format(i),
                cadence_kind="adhoc",
            )
            record_health_row(row)
        all_rows = read_recent()
        assert len(all_rows) == 5
        last_two = read_recent(limit=2)
        assert len(last_two) == 2
        assert last_two[-1].detail == "row-4"
    finally:
        if target.exists():
            target.unlink()
        os.environ.pop("JARVIS_CADENCE_HEALTH_PATH", None)


# ---------------------------------------------------------------------------
# §33.5 versioned-artifact round-trip
# ---------------------------------------------------------------------------


def test_artifact_round_trip():
    from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
        CADENCE_HEALTH_SCHEMA_VERSION, CadenceHealthRow,
        KIND_PREFLIGHT_FAILURE,
    )
    row = CadenceHealthRow(
        schema_version=CADENCE_HEALTH_SCHEMA_VERSION,
        ts_iso="2026-05-06T00:00:00Z",
        ts_epoch=1234567890.0,
        kind=KIND_PREFLIGHT_FAILURE,
        failure_class="os_policy",
        errno=errno.EPERM,
        errno_name="EPERM",
        subject="repo_root",
        detail="x" * 500,
        cadence_kind="cron",
    )
    rt = CadenceHealthRow.from_dict(row.to_dict())
    assert rt is not None
    assert rt.kind == "preflight_failure"
    assert rt.errno == errno.EPERM
    # Detail truncated to 256
    assert len(rt.detail) == 256


def test_artifact_from_dict_unknown_kind_returns_none():
    from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
        CadenceHealthRow,
    )
    assert CadenceHealthRow.from_dict({"kind": "weird"}) is None


def test_artifact_from_dict_handles_garbage():
    from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
        CadenceHealthRow,
    )
    assert CadenceHealthRow.from_dict("not a dict") is None  # type: ignore
    assert CadenceHealthRow.from_dict({}) is None


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def test_register_shipped_invariants_returns_4():
    from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
        register_shipped_invariants,
    )
    invs = register_shipped_invariants()
    assert {i.invariant_name for i in invs} == {
        "cadence_health_authority_asymmetry",
        "cadence_health_composes_canonical_flock",
        "cadence_health_versioned_artifact_compliance",
        "cadence_health_kind_taxonomy_closed",
    }


def test_all_pins_validate_clean():
    from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
        register_shipped_invariants,
    )
    target = (
        _repo_root()
        / "backend/core/ouroboros/governance/graduation/"
        "cadence_health.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for inv in register_shipped_invariants():
        violations = inv.validate(tree, source)
        assert violations == (), (
            f"pin {inv.invariant_name} fired: {violations}"
        )


def test_canonical_flock_pin_fires_on_raw_fcntl():
    from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = "import fcntl\n"
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "canonical_flock" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_taxonomy_pin_fires_on_unauthorized_kind():
    from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
KIND_PREFLIGHT_OK: str = "preflight_ok"
KIND_PREFLIGHT_FAILURE: str = "preflight_failure"
KIND_PREFLIGHT_UNAUTHORIZED: str = "preflight_unauthorized"
FAILURE_CLASS_OK: str = "ok"
FAILURE_CLASS_OS_POLICY: str = "os_policy"
FAILURE_CLASS_MISSING_PATH: str = "missing_path"
FAILURE_CLASS_UNEXPECTED: str = "unexpected"
SUBJECT_REPO_ROOT: str = "repo_root"
SUBJECT_JARVIS_DIR: str = "jarvis_dir"
SUBJECT_LOG_DIR_WRITE: str = "log_dir_write"
SUBJECT_MANIFEST_READ: str = "manifest_read"
SUBJECT_NONE: str = ""
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if "taxonomy_closed" in i.invariant_name
    )
    violations = pin.validate(tree, bad)
    assert violations


# ---------------------------------------------------------------------------
# scripts/cadence_preflight.py — end-to-end
# ---------------------------------------------------------------------------


def test_preflight_script_runs_from_any_cwd(tmp_path, monkeypatch):
    """Probe must work when invoked from /tmp (or any non-repo
    cwd) — sys.path manipulation must resolve backend.* imports
    regardless of cwd."""
    health = tmp_path / "h.jsonl"
    monkeypatch.setenv("JARVIS_CADENCE_HEALTH_PATH", str(health))
    script = (
        _repo_root() / "scripts" / "cadence_preflight.py"
    )
    result = subprocess.run(
        ["python3", str(script), "--cadence-kind", "cron"],
        capture_output=True, text=True, cwd=str(tmp_path),
    )
    assert result.returncode == 0
    assert health.exists()
    payload = json.loads(health.read_text().strip())
    assert payload["kind"] == "preflight_ok"
    assert payload["cadence_kind"] == "cron"


def test_preflight_script_records_failure_on_bogus_repo(
    tmp_path, monkeypatch,
):
    """When --repo-root points to a missing dir, script writes
    a preflight_failure row + exits 1."""
    health = tmp_path / "h.jsonl"
    monkeypatch.setenv("JARVIS_CADENCE_HEALTH_PATH", str(health))
    script = (
        _repo_root() / "scripts" / "cadence_preflight.py"
    )
    bogus = tmp_path / "nope"
    result = subprocess.run(
        [
            "python3", str(script),
            "--cadence-kind", "cron",
            "--repo-root", str(bogus),
        ],
        capture_output=True, text=True, cwd=str(tmp_path),
    )
    assert result.returncode == 1
    assert health.exists()
    payload = json.loads(health.read_text().strip())
    assert payload["kind"] == "preflight_failure"
    assert payload["subject"] == "repo_root"


# ---------------------------------------------------------------------------
# Wrapper + cron entry integration
# ---------------------------------------------------------------------------


def test_wrapper_invokes_preflight_before_harness():
    """run_live_fire_graduation_soak.sh wrapper MUST invoke
    cadence_preflight.py BEFORE the harness — non-zero exit
    blocks the harness."""
    target = (
        _repo_root() / "scripts" / "run_live_fire_graduation_soak.sh"
    )
    text = target.read_text(encoding="utf-8")
    assert "cadence_preflight.py" in text, (
        "wrapper MUST invoke cadence_preflight.py"
    )
    # Preflight invocation must precede harness exec
    preflight_idx = text.find("cadence_preflight.py")
    harness_exec_idx = text.find('exec /usr/bin/env python3 "$HARNESS"')
    assert preflight_idx >= 0 and harness_exec_idx >= 0
    assert preflight_idx < harness_exec_idx, (
        "wrapper MUST run preflight BEFORE the harness"
    )


def test_installer_cron_entry_invokes_preflight():
    """install_live_fire_soak_cron.sh build_cron_block MUST
    chain cadence_preflight.py before the harness invocation."""
    target = (
        _repo_root() / "scripts" / "install_live_fire_soak_cron.sh"
    )
    text = target.read_text(encoding="utf-8")
    assert "cadence_preflight.py" in text
    # The cron line must invoke preflight via &&-chain so
    # preflight failure aborts the harness invocation.
    # Find the cron schedule line (starts with $CRON_SCHEDULE).
    cron_line_idx = text.find("$CRON_SCHEDULE cd $REPO_ROOT")
    assert cron_line_idx >= 0
    cron_line_section = text[cron_line_idx:cron_line_idx + 1500]
    preflight_idx = cron_line_section.find("cadence_preflight.py")
    harness_idx = cron_line_section.find("$HARNESS_SCRIPT run")
    assert preflight_idx >= 0
    assert harness_idx >= 0
    assert preflight_idx < harness_idx


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_stable():
    from backend.core.ouroboros.governance.graduation import (
        cadence_health,
    )
    expected = {
        "CADENCE_HEALTH_SCHEMA_VERSION",
        "CadenceHealthRow",
        "FAILURE_CLASS_MISSING_PATH",
        "FAILURE_CLASS_OK",
        "FAILURE_CLASS_OS_POLICY",
        "FAILURE_CLASS_UNEXPECTED",
        "KIND_PREFLIGHT_FAILURE",
        "KIND_PREFLIGHT_OK",
        "SUBJECT_JARVIS_DIR",
        "SUBJECT_LOG_DIR_WRITE",
        "SUBJECT_MANIFEST_READ",
        "SUBJECT_NONE",
        "SUBJECT_REPO_ROOT",
        "classify_errno",
        "errno_name",
        "health_path",
        "most_recent_preflight_failure",
        "most_recent_preflight_ok_epoch",
        "read_recent",
        "record_health_row",
        "register_shipped_invariants",
        "run_preflight",
    }
    assert set(cadence_health.__all__) == expected
