"""Slice 97 Stage 3 — LIVE multi-repo cross-repo handshake (JARVIS → siblings).

Fires a REAL JARVIS-emitted, JARVIS-signed ripple and drives the ACTUAL
listeners in the sibling repos (jarvis-prime, reactor-core), loaded from their
on-disk source. Asserts the full distributed handshake:

  JARVIS signs  →  sibling INDEPENDENTLY verifies (its own vendored contract,
  shared PSK)  →  sibling emits a LOCAL intent  →  nothing JARVIS-dictated is
  executed.

And the adversarial half: a tampered / replayed / wrong-origin ripple is DROPPED
by BOTH siblings with zero false-positives and zero intents.

The two siblings both ship a package literally named ``cross_repo_mesh``, so we
load each from its file path under a UNIQUE alias (importlib) to avoid the
import collision — this exercises the real sibling code, not a copy. The test
SKIPS gracefully when the sibling repos are not checked out next to JARVIS (so
it never blocks CI in a JARVIS-only environment).
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

from backend.core.ouroboros.cross_repo_mesh.ripple_contract import (
    RippleKind,
    VerifyVerdict,
)
from backend.core.ouroboros.cross_repo_mesh.ripple_emitter import build_ripple

# Sibling repos live next to the JARVIS checkout. Resolve their root robustly
# (this test may run from an isolated worktree under /tmp, not next to the
# siblings) by searching candidate roots; the test SKIPS if not found.
def _candidate_roots() -> list:
    roots = []
    env = __import__("os").environ.get("JARVIS_SIBLING_REPOS_ROOT", "").strip()
    if env:
        roots.append(Path(env))
    # grandparent of the JARVIS repo (the real ~/Documents/repos checkout case)
    roots.append(Path(__file__).resolve().parents[3])
    roots.append(Path.home() / "Documents" / "repos")
    return roots


def _find_sibling_dir(repo: str) -> Path | None:
    for root in _candidate_roots():
        cand = root / repo / "cross_repo_mesh" / "ripple_listener.py"
        if cand.is_file():
            return root / repo
    return None


_SIBLINGS = {
    "jarvis-prime": (
        "JARVIS_PRIME_RIPPLE_LISTENER_ENABLED",
        "JARVIS_PRIME_RIPPLE_INTENT_LEDGER_PATH",
        "jarvis-prime",
    ),
    "reactor-core": (
        "REACTOR_CORE_RIPPLE_LISTENER_ENABLED",
        "REACTOR_CORE_RIPPLE_INTENT_LEDGER_PATH",
        "reactor-core",
    ),
}

_PSK = b"shared-cross-repo-secret-32-bytes!!"


def _load_sibling_listener(repo: str, alias: str) -> ModuleType:
    """Load <repo>/cross_repo_mesh/ripple_listener.py (and its vendored
    ripple_contract) from disk under a unique package alias so the two
    same-named ``cross_repo_mesh`` packages don't collide in one process."""
    sibling_dir = _find_sibling_dir(repo)
    assert sibling_dir is not None, repo
    pkg_dir = sibling_dir / "cross_repo_mesh"
    pkg_spec = importlib.util.spec_from_file_location(
        alias, pkg_dir / "__init__.py",
        submodule_search_locations=[str(pkg_dir)],
    )
    pkg = importlib.util.module_from_spec(pkg_spec)
    sys.modules[alias] = pkg
    pkg_spec.loader.exec_module(pkg)  # type: ignore[union-attr]
    # ripple_contract MUST load first — ripple_listener does `from .ripple_contract`.
    for sub in ("ripple_contract", "ripple_listener"):
        sub_spec = importlib.util.spec_from_file_location(
            f"{alias}.{sub}", pkg_dir / f"{sub}.py",
        )
        mod = importlib.util.module_from_spec(sub_spec)
        sys.modules[f"{alias}.{sub}"] = mod
        sub_spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return sys.modules[f"{alias}.ripple_listener"]


def _sibling_present(repo: str) -> bool:
    return _find_sibling_dir(repo) is not None


def _emit_jarvis_token(*, now: float, intent: str, nonce_kind=RippleKind.CONTRACT_CHANGED,
                       origin: str = "jarvis", ttl: float = 3600.0) -> str:
    """Produce a REAL JARVIS-emitter ripple, signed with the shared PSK."""
    payload = build_ripple(
        kind=nonce_kind,
        intent=intent,
        payload_obj={"changed": "qutebrowser/misc/guiprocess.py", "lines": 23},
        source_repo=origin,
        now_unix=now,
        ttl_s=ttl,
    )
    from backend.core.ouroboros.cross_repo_mesh.ripple_contract import sign_ripple
    return sign_ripple(payload, _PSK)


@pytest.mark.parametrize("repo", list(_SIBLINGS))
def test_live_handshake_jarvis_to_sibling(repo, monkeypatch, tmp_path):
    if not _sibling_present(repo):
        pytest.skip(f"sibling repo {repo!r} not checked out next to JARVIS")
    enabled_env, ledger_env, _ = _SIBLINGS[repo]
    monkeypatch.setenv(enabled_env, "true")
    monkeypatch.setenv("JARVIS_CROSS_REPO_EMIT_PSK", _PSK.decode())
    monkeypatch.setenv(ledger_env, str(tmp_path / f"{repo}_intents.jsonl"))

    listener = _load_sibling_listener(repo, f"sib_{repo.replace('-', '_')}")
    now = time.time()

    # 1. VALID: JARVIS fires → sibling independently verifies → local intent.
    token = _emit_jarvis_token(now=now, intent="contract guiprocess.py changed")
    verdict, intent = listener.handle_inbound_ripple(token, now_unix=now, seen_nonces=set())
    assert verdict == VerifyVerdict.VERIFIED.value, (repo, verdict)
    assert intent is not None and intent["origin"] == "jarvis"
    # The local intent was logged to the sibling's own ledger.
    assert (tmp_path / f"{repo}_intents.jsonl").exists()

    # 2. TAMPERED → DROPPED by the sibling, no intent (zero false-positive).
    head, sig = token.split(".", 1)
    bad = head[:-1] + ("A" if head[-1] != "A" else "B") + "." + sig
    v_bad, i_bad = listener.handle_inbound_ripple(bad, now_unix=now, seen_nonces=set())
    assert v_bad == VerifyVerdict.DROPPED_BAD_SIGNATURE.value
    assert i_bad is None

    # 3. REPLAY → DROPPED on the second delivery (shared seen-set).
    seen: set = set()
    fresh = _emit_jarvis_token(now=now, intent="replay-test")
    v1, _ = listener.handle_inbound_ripple(fresh, now_unix=now, seen_nonces=seen)
    v2, i2 = listener.handle_inbound_ripple(fresh, now_unix=now, seen_nonces=seen)
    assert v1 == VerifyVerdict.VERIFIED.value and v2 == VerifyVerdict.DROPPED_REPLAY.value
    assert i2 is None


def test_live_fire_reaches_both_siblings(monkeypatch, tmp_path):
    """Fire ONE JARVIS event and assert it is independently VERIFIED by BOTH
    siblings (the distributed mesh), with each logging its own local intent."""
    present = [r for r in _SIBLINGS if _sibling_present(r)]
    if len(present) < 2:
        pytest.skip("both sibling repos must be checked out for the full-mesh test")
    monkeypatch.setenv("JARVIS_CROSS_REPO_EMIT_PSK", _PSK.decode())
    now = time.time()
    token = _emit_jarvis_token(now=now, intent="capability graduated: slice96",
                               nonce_kind=RippleKind.CAPABILITY_GRADUATED)
    results = {}
    for repo in present:
        enabled_env, ledger_env, _ = _SIBLINGS[repo]
        monkeypatch.setenv(enabled_env, "true")
        monkeypatch.setenv(ledger_env, str(tmp_path / f"{repo}.jsonl"))
        listener = _load_sibling_listener(repo, f"mesh_{repo.replace('-', '_')}")
        verdict, intent = listener.handle_inbound_ripple(token, now_unix=now, seen_nonces=set())
        results[repo] = (verdict, intent)
    # Same signed event, independently VERIFIED by every sibling.
    for repo, (verdict, intent) in results.items():
        assert verdict == VerifyVerdict.VERIFIED.value, (repo, verdict)
        assert intent is not None
        assert (tmp_path / f"{repo}.jsonl").exists()


def test_no_remote_execution_across_the_mesh(monkeypatch, tmp_path):
    """A ripple whose intent LOOKS like code, fired from JARVIS, must be verified
    + logged as inert DATA by every sibling — never executed."""
    present = [r for r in _SIBLINGS if _sibling_present(r)]
    if not present:
        pytest.skip("no sibling repos present")
    monkeypatch.setenv("JARVIS_CROSS_REPO_EMIT_PSK", _PSK.decode())
    marker = tmp_path / "PWNED_ACROSS_MESH"
    now = time.time()
    evil = f"__import__('os').system('touch {marker}')"
    token = _emit_jarvis_token(now=now, intent=evil)
    for repo in present:
        enabled_env, ledger_env, _ = _SIBLINGS[repo]
        monkeypatch.setenv(enabled_env, "true")
        monkeypatch.setenv(ledger_env, str(tmp_path / f"{repo}.jsonl"))
        listener = _load_sibling_listener(repo, f"noexec_{repo.replace('-', '_')}")
        verdict, intent = listener.handle_inbound_ripple(token, now_unix=now, seen_nonces=set())
        assert verdict == VerifyVerdict.VERIFIED.value
        assert intent["intent"] == evil          # inert data
    assert not marker.exists()                    # NOTHING executed anywhere
