"""The signature anchor states what a test may mock.patch through a module.

bt-2026-09-23-172111: the anchor showed ``backend/apply_robust_learning.py``'s
exact API 34 times, and every candidate still died on ``AttributeError: <module
'backend.apply_robust_learning'> does not have the attribute
'apply_robust_learning_patch'`` -- a name imported INSIDE a function, which
binds nothing on the module.
"""
from __future__ import annotations

import importlib
import sys
import textwrap
from unittest import mock

import pytest

from backend.core.ouroboros.governance import ast_signature_anchor as A

# The shape of the real subject, reduced.
SUBJECT = textwrap.dedent('''
    """Apply the patch."""
    import os
    import logging as log
    logger = log.getLogger(__name__)

    def apply_patches():
        """Apply."""
        from pkgx.source import apply_patch
        import pkgx.other as other
        return apply_patch() and other.ok()

    class Runner:
        def go(self):
            from .source import helper
            return helper()
''')


def _block(label="pkgx/subject.py"):
    return A.extract_public_api(SUBJECT, label)


def test_function_local_imports_are_named_with_their_real_patch_target():
    block = _block()
    assert '# mock.patch targets:' in block
    assert 'apply_patches(): apply_patch -> patch("pkgx.source.apply_patch")' in block
    assert 'apply_patches(): other -> patch("pkgx.other")' in block
    assert 'Runner.go(): helper -> patch("pkgx.source.helper")' in block   # relative, resolved
    assert 'NOT attributes of pkgx.subject' in block


def test_module_level_imports_are_listed_as_patchable_here():
    line = next(ln for ln in _block().splitlines() if "module-level imports" in ln)
    assert '"pkgx.subject.<name>"' in line and "os" in line and "log" in line


def test_the_emitted_target_works_and_the_naive_one_is_the_soaks_error(tmp_path, monkeypatch):
    pkg = tmp_path / "pkgx"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "source.py").write_text("def apply_patch():\n    return False\ndef helper():\n    return 0\n")
    (pkg / "other.py").write_text("def ok():\n    return True\n")
    (pkg / "subject.py").write_text(SUBJECT)
    monkeypatch.syspath_prepend(str(tmp_path))
    for m in [m for m in sys.modules if m == "pkgx" or m.startswith("pkgx.")]:
        monkeypatch.delitem(sys.modules, m)
    subject = importlib.import_module("pkgx.subject")

    target = next(ln.split('patch("')[1].rstrip('")') for ln in _block().splitlines()
                  if "apply_patch -> patch(" in ln)
    with mock.patch(target, return_value=True):
        assert subject.apply_patches() is True

    with pytest.raises(AttributeError, match="does not have the attribute 'apply_patch'"):
        with mock.patch("pkgx.subject.apply_patch", return_value=True):
            pass


def test_a_module_with_no_imports_adds_nothing():
    assert "mock.patch" not in A.extract_public_api("def f(x):\n    return x\n", "m.py")


def test_the_switch_turns_it_off(monkeypatch):
    monkeypatch.setenv("JARVIS_AST_SIGNATURE_ANCHOR_PATCH_TARGETS_ENABLED", "false")
    assert "mock.patch" not in _block()


def test_a_deleted_first_party_source_gets_the_sys_modules_stub_not_a_patch_string(tmp_path):
    """The real subject's local imports point at modules a cleanup deleted:
    patch() must import the source, so only a sys.modules stub can work."""
    backend = tmp_path / "backend"
    (backend / "vision").mkdir(parents=True)
    (backend / "vision" / "__init__.py").write_text("")
    (backend / "vision" / "present.py").write_text("def here():\n    return 1\n")
    subject = backend / "apply_x.py"
    subject.write_text(
        "def run():\n"
        "    from vision.gone import apply_patch\n"
        "    from vision.present import here\n"
        "    import json\n"
        "    return apply_patch(), here(), json\n"
    )
    block = A.extract_public_api(subject.read_text(), "backend/apply_x.py",
                                 source_path=subject, repo_root=tmp_path)
    assert ('run(): apply_patch -> source module "vision.gone" DOES NOT EXIST' in block
            and 'mock.patch.dict(sys.modules, {"vision.gone": fake})' in block)
    assert 'run(): here -> patch("vision.present.here")' in block
    assert 'run(): json -> patch("json")' in block   # stdlib is never "absent"
