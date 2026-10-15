"""Hot reload reads only files that changed.

Every check read and MD5-hashed every watched file in the repo on cpu_count
threads; those threads held the GIL together and the event loop wedged
0.6 s each round (2026-10-14 20:56:53 stall dump: ten threads in hash_file).
"""
from __future__ import annotations

import hashlib
import os
import types

import pytest


@pytest.fixture
def watcher(tmp_path, monkeypatch):
    us = pytest.importorskip("unified_supervisor")
    w = us.HotReloadWatcher.__new__(us.HotReloadWatcher)
    w.repo_root = tmp_path
    w.exclude_dirs = {"__pycache__"}
    w.exclude_patterns = ["*.pyc"]
    w._watch_decisions, w._file_signatures, w._file_hashes = {}, {}, {}
    w._type_registry = types.SimpleNamespace(
        get_file_info=lambda p: types.SimpleNamespace(requires_restart=p.endswith(".py")),
        categorize_changes=lambda changed: {"backend": list(changed)})
    reads = []
    real_md5 = hashlib.md5
    monkeypatch.setattr(hashlib, "md5", lambda data=b"": reads.append(data) or real_md5(data))
    return w, reads


def test_unchanged_files_are_never_read_again(watcher, tmp_path):
    w, reads = watcher
    for i in range(20):
        (tmp_path / f"m{i}.py").write_text(f"x = {i}\n")
    (tmp_path / "notes.txt").write_text("not watched")
    first = w._calculate_file_hashes_parallel()
    assert len(first) == 20 and len(reads) == 20
    reads.clear()
    assert w._calculate_file_hashes_parallel() == first and reads == []


def test_an_edit_a_new_file_and_a_deletion_are_still_seen(watcher, tmp_path):
    w, reads = watcher
    (tmp_path / "a.py").write_text("a = 1\n")
    (tmp_path / "b.py").write_text("b = 1\n")
    w._file_hashes = w._calculate_file_hashes_parallel()
    reads.clear()
    (tmp_path / "a.py").write_text("a = 22\n")                 # size changes too
    (tmp_path / "c.py").write_text("c = 1\n")
    (tmp_path / "b.py").unlink()
    changed, files, _ = w._detect_changes()
    assert changed and set(files) == {"a.py", "c.py", "[DELETED] b.py"}
    assert len(reads) == 2                                     # only a.py and c.py were read


def test_a_rewrite_with_the_same_content_is_not_a_change(watcher, tmp_path):
    w, _ = watcher
    target = tmp_path / "a.py"
    target.write_text("a = 1\n")
    w._file_hashes = w._calculate_file_hashes_parallel()
    st = target.stat()
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    changed, files, _ = w._detect_changes()
    assert not changed and files == []
