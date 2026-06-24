"""Tests for the Autonomous Pre-Flight Cache Manager (trinity_prebake_manager).

NO real Docker / build. The docker command boundary is an injectable fake that
records argv and returns scripted results. Proves:
  * dep_hash is deterministic + changes when a dep file's CONTENT changes;
  * sandbox_image_tag format (+ env prefix);
  * is_image_cached True/False via fake `docker image inspect` rc;
  * prebake_if_needed: disabled (skipped, inert) / fast-path (all cached, NO
    build) / bake-path (only the missing built, WAN docker build issued) /
    bake-failure (fail-CLOSED, caller must not proceed);
  * the 3 Dockerfile.sandbox templates exist + carry FROM/pip-install/HEALTHCHECK/CMD.
"""
from __future__ import annotations

import os

import pytest

from backend.core.ouroboros.governance.saga import trinity_prebake_manager as pbm
from backend.core.ouroboros.governance.saga.trinity_prebake_manager import (
    CmdResult,
    PrebakeResult,
    dep_hash,
    is_image_cached,
    prebake_if_needed,
    prebake_enabled,
    sandbox_image_tag,
)


# --------------------------------------------------------------------------- #
# Fake docker command boundary
# --------------------------------------------------------------------------- #
class FakeRunner:
    """Records argv; returns scripted results.

    ``inspect_present`` -> the set of repo keys whose `image inspect` returns rc0
    (cached). ``build_fail`` -> the set of repo keys whose `docker build` fails.
    """

    def __init__(self, *, inspect_present=(), build_fail=()):
        self.calls = []
        self._present = set(inspect_present)
        self._build_fail = set(build_fail)

    async def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[:3] == ["docker", "image", "inspect"]:
            tag = argv[3]
            # tag = <prefix>-<repo>:<hash>; extract repo between last '-' and ':'.
            repo = tag.rsplit(":", 1)[0].rsplit("-", 1)[-1]
            return CmdResult(0 if repo in self._present else 1)
        if argv[:2] == ["docker", "build"]:
            # -t <tag> is at a known offset; find repo from the tag.
            tag = argv[argv.index("-t") + 1]
            repo = tag.rsplit(":", 1)[0].rsplit("-", 1)[-1]
            return CmdResult(1 if repo in self._build_fail else 0)
        return CmdResult(0)

    @property
    def build_calls(self):
        return [c for c in self.calls if c[:2] == ["docker", "build"]]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in (
        "JARVIS_TRINITY_PREBAKE_ENABLED",
        "JARVIS_TRINITY_IMAGE_PREFIX",
        "JARVIS_TRINITY_BAKE_TIMEOUT_S",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


def _mk_repo(tmp_path, name, *, req=None, pyproj=None):
    root = tmp_path / name
    root.mkdir()
    if req is not None:
        (root / "requirements.txt").write_text(req, encoding="utf-8")
    if pyproj is not None:
        (root / "pyproject.toml").write_text(pyproj, encoding="utf-8")
    return str(root)


# --------------------------------------------------------------------------- #
# dep_hash
# --------------------------------------------------------------------------- #
def test_dep_hash_deterministic(tmp_path):
    root = _mk_repo(tmp_path, "r", req="flask==1.0\n", pyproj="[tool]\n")
    assert dep_hash(root) == dep_hash(root)
    assert len(dep_hash(root)) == 16
    # ASCII hex.
    int(dep_hash(root), 16)


def test_dep_hash_changes_when_requirements_content_changes(tmp_path):
    a = _mk_repo(tmp_path, "a", req="flask==1.0\n")
    b = _mk_repo(tmp_path, "b", req="flask==2.0\n")
    assert dep_hash(a) != dep_hash(b)


def test_dep_hash_changes_when_pyproject_content_changes(tmp_path):
    a = _mk_repo(tmp_path, "a", pyproj="[a]\nx=1\n")
    b = _mk_repo(tmp_path, "b", pyproj="[a]\nx=2\n")
    assert dep_hash(a) != dep_hash(b)


def test_dep_hash_missing_files_is_stable_and_distinct(tmp_path):
    empty = _mk_repo(tmp_path, "empty")  # no dep files
    withreq = _mk_repo(tmp_path, "withreq", req="x\n")
    # Two empty repos hash identically (deterministic empty sections).
    empty2 = _mk_repo(tmp_path, "empty2")
    assert dep_hash(empty) == dep_hash(empty2)
    # Adding a dep file changes the hash (cache miss -> re-bake).
    assert dep_hash(empty) != dep_hash(withreq)


def test_dep_hash_split_matters(tmp_path):
    # Same combined bytes but in different files must NOT collide (labelled).
    a = _mk_repo(tmp_path, "a", req="abc", pyproj="")
    b = _mk_repo(tmp_path, "b", req="", pyproj="abc")
    assert dep_hash(a) != dep_hash(b)


# --------------------------------------------------------------------------- #
# sandbox_image_tag
# --------------------------------------------------------------------------- #
def test_sandbox_image_tag_format():
    assert sandbox_image_tag("prime", "deadbeefcafe0001") == (
        "jarvis-trinity-sandbox-prime:deadbeefcafe0001"
    )


def test_sandbox_image_tag_env_prefix(monkeypatch):
    monkeypatch.setenv("JARVIS_TRINITY_IMAGE_PREFIX", "custom-pfx")
    assert sandbox_image_tag("reactor", "abcd").startswith("custom-pfx-reactor:")


# --------------------------------------------------------------------------- #
# is_image_cached
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_is_image_cached_true_false():
    runner = FakeRunner(inspect_present={"prime"})
    assert await is_image_cached("prime", "abcd", runner=runner) is True
    assert await is_image_cached("reactor", "abcd", runner=runner) is False


@pytest.mark.asyncio
async def test_is_image_cached_failsoft_to_false():
    async def raising(argv):
        raise RuntimeError("docker absent")

    # Any error -> treat as needs-bake (fail-CLOSED toward re-baking).
    assert await is_image_cached("jarvis", "abcd", runner=raising) is False


# --------------------------------------------------------------------------- #
# prebake_enabled
# --------------------------------------------------------------------------- #
def test_prebake_disabled_by_default():
    assert prebake_enabled() is False


def test_prebake_enabled_truthy(monkeypatch):
    monkeypatch.setenv("JARVIS_TRINITY_PREBAKE_ENABLED", "true")
    assert prebake_enabled() is True


# --------------------------------------------------------------------------- #
# prebake_if_needed -- disabled / inert
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_prebake_disabled_is_inert_no_commands(tmp_path):
    j = _mk_repo(tmp_path, "j", req="x\n")
    p = _mk_repo(tmp_path, "p", req="x\n")
    r = _mk_repo(tmp_path, "r", pyproj="[a]\n")
    runner = FakeRunner()
    res = await prebake_if_needed(
        jarvis_root=j, prime_root=p, reactor_root=r, runner=runner
    )
    assert res.skipped is True
    assert res.reason == "prebake_disabled"
    assert res.ok is True  # caller proceeds with the base-image path
    assert runner.calls == []  # NO docker commands at all


# --------------------------------------------------------------------------- #
# prebake_if_needed -- fast path (all cached -> NO build)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_prebake_fast_path_all_cached_no_build(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_TRINITY_PREBAKE_ENABLED", "1")
    j = _mk_repo(tmp_path, "j", req="x\n")
    p = _mk_repo(tmp_path, "p", req="y\n")
    r = _mk_repo(tmp_path, "r", pyproj="[a]\n")
    runner = FakeRunner(inspect_present={"jarvis", "prime", "reactor"})
    res = await prebake_if_needed(
        jarvis_root=j, prime_root=p, reactor_root=r, runner=runner
    )
    assert res.skipped is False
    assert res.reason == "all_cached"
    assert res.ok is True
    assert set(res.cached) == {"jarvis", "prime", "reactor"}
    assert res.baked == ()
    assert set(res.images) == {"jarvis", "prime", "reactor"}
    # NO build commands -- only inspects.
    assert runner.build_calls == []


# --------------------------------------------------------------------------- #
# prebake_if_needed -- bake path (only missing built, WAN docker build)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_prebake_bakes_only_missing_wan_build(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_TRINITY_PREBAKE_ENABLED", "yes")
    j = _mk_repo(tmp_path, "j", req="x\n")
    p = _mk_repo(tmp_path, "p", req="y\n")
    r = _mk_repo(tmp_path, "r", pyproj="[a]\n")
    # prime already cached; jarvis + reactor missing -> build only those 2.
    runner = FakeRunner(inspect_present={"prime"})
    res = await prebake_if_needed(
        jarvis_root=j,
        prime_root=p,
        reactor_root=r,
        runner=runner,
        dockerfile_dir="deploy/sandbox",
    )
    assert res.reason == "baked"
    assert res.ok is True
    assert set(res.cached) == {"prime"}
    assert set(res.baked) == {"jarvis", "reactor"}
    builds = runner.build_calls
    assert len(builds) == 2
    built_repos = set()
    for cmd in builds:
        # docker build -f deploy/sandbox/Dockerfile.<repo>.sandbox -t <tag> <root>
        assert cmd[:3] == ["docker", "build", "-f"]
        dfile = cmd[3]
        assert dfile.startswith("deploy/sandbox/Dockerfile.")
        assert dfile.endswith(".sandbox")
        repo = dfile.split("Dockerfile.")[1].rsplit(".sandbox", 1)[0]
        built_repos.add(repo)
        # build context = the repo root (last arg), NOT the dockerfile.
        ctx = cmd[-1]
        assert ctx in (j, p, r)
    assert built_repos == {"jarvis", "reactor"}


# --------------------------------------------------------------------------- #
# prebake_if_needed -- bake FAILURE -> fail-CLOSED
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_prebake_bake_failure_is_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_TRINITY_PREBAKE_ENABLED", "true")
    j = _mk_repo(tmp_path, "j", req="x\n")
    p = _mk_repo(tmp_path, "p", req="y\n")
    r = _mk_repo(tmp_path, "r", pyproj="[a]\n")
    # reactor build fails.
    runner = FakeRunner(inspect_present=(), build_fail={"reactor"})
    res = await prebake_if_needed(
        jarvis_root=j, prime_root=p, reactor_root=r, runner=runner
    )
    assert res.skipped is False
    assert res.reason.startswith("bake_failed:reactor")
    # The caller MUST NOT proceed.
    assert res.ok is False


@pytest.mark.asyncio
async def test_prebake_bake_exception_is_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_TRINITY_PREBAKE_ENABLED", "true")
    j = _mk_repo(tmp_path, "j", req="x\n")
    p = _mk_repo(tmp_path, "p", req="y\n")
    r = _mk_repo(tmp_path, "r", pyproj="[a]\n")

    class Boom:
        def __init__(self):
            self.calls = 0

        async def __call__(self, argv):
            if argv[:2] == ["docker", "build"]:
                raise RuntimeError("daemon down")
            return CmdResult(1)  # nothing cached -> everything needs bake

    res = await prebake_if_needed(
        jarvis_root=j, prime_root=p, reactor_root=r, runner=Boom()
    )
    assert res.ok is False
    assert "bake_failed" in res.reason


# --------------------------------------------------------------------------- #
# The 3 Dockerfile.sandbox templates
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "repo,port,cmd_token",
    [
        ("jarvis", "8091", "8091"),
        ("prime", "8000", "run_server.py"),
        ("reactor", "8090", "run_reactor.py"),
    ],
)
def test_dockerfile_templates_exist_and_contain_required_directives(repo, port, cmd_token):
    here = os.path.dirname(__file__)
    repo_root = os.path.abspath(os.path.join(here, "..", ".."))
    path = os.path.join(repo_root, "deploy", "sandbox", "Dockerfile.%s.sandbox" % repo)
    assert os.path.isfile(path), path
    text = open(path, "r", encoding="utf-8").read()
    assert "FROM python:3.11-slim" in text
    assert "pip install" in text
    assert "HEALTHCHECK" in text
    assert "/health" in text
    assert port in text
    assert "CMD" in text
    assert cmd_token in text
    # ASCII only.
    text.encode("ascii")
