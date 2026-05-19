"""GitIndexGuard Slice 2 — boot wiring + SSE regression spine.

Slice 1 shipped the pure-stdlib substrate (default-OFF). Slice 2
wires it:
  * ``ide_observability_stream`` gains ``git_index_anomaly`` event
    type + ``publish_git_index_anomaly`` (mirrors the
    ``posture_changed`` publisher; composes ``get_default_broker``)
  * ``BattleTestHarness._boot_git_index_guard`` — FIRST boot phase,
    composes ``detect_and_rebuild`` with the SSE callback as the
    ``on_anomaly`` seam (the guard imports NO governance module;
    the harness owns the wiring). NEVER raises into boot.

Coverage:
  * event type value + membership in _VALID_EVENT_TYPES
  * publish_git_index_anomaly: stream-disabled / non-mapping /
    happy publish / never-raises
  * boot hook: missing index → rebuilt + SSE seam fired; bogus
    repo path → never raises; master-off → DISABLED no-op (no SSE)
  * AST pin: boot hook is the first _BootPhase, composes
    detect_and_rebuild + publish_git_index_anomaly; event type in
    _VALID_EVENT_TYPES
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from types import SimpleNamespace

from backend.core.ouroboros.governance import (
    ide_observability_stream as stream,
)
from backend.core.ouroboros.battle_test.harness import BattleTestHarness


# --------------------------------------------------------------------------
# SSE vocabulary
# --------------------------------------------------------------------------


def test_event_type_value_and_membership():
    assert stream.EVENT_TYPE_GIT_INDEX_ANOMALY == "git_index_anomaly"
    assert (
        stream.EVENT_TYPE_GIT_INDEX_ANOMALY
        in stream._VALID_EVENT_TYPES
    )


def test_publish_stream_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(stream, "stream_enabled", lambda: False)
    assert stream.publish_git_index_anomaly({"outcome": "x"}) is None


def test_publish_non_mapping_returns_none(monkeypatch):
    monkeypatch.setattr(stream, "stream_enabled", lambda: True)
    assert stream.publish_git_index_anomaly("not-a-mapping") is None  # type: ignore[arg-type]


def test_publish_happy_path(monkeypatch):
    monkeypatch.setattr(stream, "stream_enabled", lambda: True)
    seen = {}

    class _Broker:
        def publish(self, etype, opid, payload):
            seen["etype"] = etype
            seen["opid"] = opid
            seen["payload"] = payload
            return "evt-1"

    monkeypatch.setattr(stream, "get_default_broker", lambda: _Broker())
    out = stream.publish_git_index_anomaly(
        {"outcome": "missing_rebuilt", "detail": "d"}
    )
    assert out == "evt-1"
    assert seen["etype"] == "git_index_anomaly"
    assert seen["opid"] == "git_index"
    assert seen["payload"]["outcome"] == "missing_rebuilt"


def test_publish_never_raises(monkeypatch):
    monkeypatch.setattr(stream, "stream_enabled", lambda: True)

    def _boom():
        raise RuntimeError("broker exploded")

    monkeypatch.setattr(stream, "get_default_broker", _boom)
    # Must swallow and return None, not propagate.
    assert stream.publish_git_index_anomaly({"outcome": "x"}) is None


# --------------------------------------------------------------------------
# Boot hook
# --------------------------------------------------------------------------


def _git_repo(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)

    def run(*a):
        subprocess.run(
            ["git", *a], cwd=str(p), check=True,
            capture_output=True, text=True,
        )
    run("init", "-q")
    run("config", "user.email", "t@t.t")
    run("config", "user.name", "t")
    (p / "f.txt").write_text("payload\n")
    run("add", "f.txt")
    run("commit", "-q", "-m", "seed")
    return p


async def test_boot_hook_rebuilds_and_fires_sse(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("JARVIS_GIT_INDEX_GUARD_ENABLED", "true")
    repo = _git_repo(tmp_path / "repo")
    (repo / ".git" / "index").unlink()
    assert not (repo / ".git" / "index").is_file()

    fired = []
    monkeypatch.setattr(
        stream, "publish_git_index_anomaly",
        lambda d: fired.append(d),
    )

    stub = SimpleNamespace(_config=SimpleNamespace(repo_path=str(repo)))
    # Call the unbound coroutine with our minimal stub.
    await BattleTestHarness._boot_git_index_guard(stub)  # type: ignore[arg-type]

    assert (repo / ".git" / "index").is_file()  # rebuilt
    assert (repo / "f.txt").read_text() == "payload\n"  # WT intact
    assert len(fired) == 1
    assert fired[0]["outcome"] == "missing_rebuilt"


async def test_boot_hook_master_off_is_noop_no_sse(
    tmp_path, monkeypatch,
):
    monkeypatch.delenv("JARVIS_GIT_INDEX_GUARD_ENABLED", raising=False)
    repo = _git_repo(tmp_path / "repo")
    (repo / ".git" / "index").unlink()
    fired = []
    monkeypatch.setattr(
        stream, "publish_git_index_anomaly",
        lambda d: fired.append(d),
    )
    stub = SimpleNamespace(_config=SimpleNamespace(repo_path=str(repo)))
    await BattleTestHarness._boot_git_index_guard(stub)  # type: ignore[arg-type]
    # DISABLED → no rebuild, no SSE (byte-identical boot).
    assert not (repo / ".git" / "index").is_file()
    assert fired == []


async def test_boot_hook_never_raises_on_bogus_repo(monkeypatch):
    monkeypatch.setenv("JARVIS_GIT_INDEX_GUARD_ENABLED", "true")
    stub = SimpleNamespace(
        _config=SimpleNamespace(repo_path="/nonexistent/zzz/repo")
    )
    # Must not raise into boot.
    await BattleTestHarness._boot_git_index_guard(stub)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# AST pins — wiring cannot silently regress
# --------------------------------------------------------------------------


def test_ast_pin_boot_hook_first_phase_and_composition():
    import backend.core.ouroboros.battle_test.harness as hm

    src = Path(hm.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    # (a) _boot_git_index_guard method exists and composes
    #     detect_and_rebuild.
    fn = next(
        (
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
            and n.name == "_boot_git_index_guard"
        ),
        None,
    )
    assert fn is not None, "_boot_git_index_guard method missing"
    body_src = ast.unparse(fn)
    assert "detect_and_rebuild" in body_src, (
        "boot hook must compose git_index_guard.detect_and_rebuild"
    )
    assert "publish_git_index_anomaly" in body_src, (
        "boot hook must wire the SSE on_anomaly seam"
    )

    # (b) the hook is invoked as the FIRST _BootPhase (before
    #     boot_oracle) — a missing index corrupts everything else.
    run_fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "run"
    )
    phase_order = [
        node.args[0].value
        for node in ast.walk(run_fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_BootPhase"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ]
    assert phase_order, "no _BootPhase calls found in run()"
    assert phase_order[0] == "boot_git_index_guard", (
        f"git index guard must be the FIRST boot phase, got "
        f"{phase_order[:3]}"
    )


def test_ast_pin_event_type_registered():
    s_src = Path(stream.__file__).read_text(encoding="utf-8")
    tree = ast.parse(s_src)
    # The constant assignment exists with the stable value.
    found_const = any(
        isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Name)
            and t.id == "EVENT_TYPE_GIT_INDEX_ANOMALY"
            for t in n.targets
        )
        and isinstance(n.value, ast.Constant)
        and n.value.value == "git_index_anomaly"
        for n in ast.walk(tree)
    )
    assert found_const, "EVENT_TYPE_GIT_INDEX_ANOMALY const missing"
    # And it is a member of the frozenset (runtime check is the
    # authoritative one; this pins the source too).
    assert stream.EVENT_TYPE_GIT_INDEX_ANOMALY in stream._VALID_EVENT_TYPES
