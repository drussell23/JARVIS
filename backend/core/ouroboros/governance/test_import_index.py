"""Content-addressed index of which test files import which modules.

One builder for the ``dotted_module -> [test files that import it]`` map,
shared by ``test_runner`` (VALIDATE / L2 test scoping) and
``target_stratification`` (coverage prior). They used to carry two copies of
the same loop, each feeding its own module-level ``Dict[Path, map]`` that was
never evicted.

Why it is content-addressed
---------------------------
The map was cached per TREE PATH. A validation sandbox is a fresh
``/tmp/...`` copy per candidate, so every sandbox whose test tree differed
from the base (every L2 repair sandbox, and every candidate that writes a
test -- i.e. all test-synthesis work) added a new ~14k-key, ~6 MB map that
lived for the rest of the process. Soak bt-2026-09-23-005910's growth
tracer named exactly those allocation sites (``_register_import``) as the
top heap growth; the daemon grew ~450 MB/h.

What a test file imports is a pure function of its bytes. So the unit that
is shared across trees is the per-FILE import list, keyed by a digest of the
file's content: an identical ``test_foo.py`` in the base tree and in fifty
sandboxes is parsed once. Parsing is ~80% of a warm build on this tree
(measured: walk 0.06 s, read 0.54 s, hash 0.02 s, parse 2.2 s for 3,541
files), so a sandbox build drops to walk + read + hash + assembly.

The assembled map still holds absolute paths under the tree it was built
for -- that is the shape every consumer reads -- so a map cannot be shared
between two roots. Those maps live in a small bounded LRU keyed by root;
evicting one costs a re-assembly from the per-file tier, not a re-parse.

Both tiers are bounded (env-tunable) and thread-safe: builds run in
executor threads and in the offload process pool.
"""
from __future__ import annotations

import ast
import hashlib
import os
import sys
import threading
from collections import OrderedDict
from pathlib import Path
from typing import (
    Dict,
    FrozenSet,
    Generic,
    Hashable,
    Iterable,
    Iterator,
    List,
    Optional,
    Tuple,
    TypeVar,
)

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")

_MISSING = object()


def _env_int(name: str, default: int, floor: int) -> int:
    try:
        return max(floor, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def file_cache_max() -> int:
    """Distinct test-file CONTENTS remembered (``JARVIS_TEST_IMPORT_FILE_CACHE_MAX``).

    Default 16384, about 4x this tree's 3,541 test files, so the base tree
    plus every in-flight candidate's edited files fit with room to spare.
    """
    return _env_int("JARVIS_TEST_IMPORT_FILE_CACHE_MAX", 16384, 1)


def tree_cache_max() -> int:
    """Assembled per-root maps kept (``JARVIS_TEST_IMPORT_MAP_CACHE_MAX``).

    Default 4: the base tree plus a few concurrent sandboxes. Each is ~6 MB,
    and a miss re-assembles from the file tier instead of re-parsing.
    """
    return _env_int("JARVIS_TEST_IMPORT_MAP_CACHE_MAX", 4, 1)


class BoundedLRU(Generic[K, V]):
    """Thread-safe LRU with a mapping-style surface.

    ``maxsize`` may be a callable so an env knob is read at insert time,
    which keeps a module-level instance tunable without a restart and lets
    tests shrink it.
    """

    def __init__(self, maxsize) -> None:
        self._maxsize = maxsize
        self._data: "OrderedDict[K, V]" = OrderedDict()
        self._lock = threading.Lock()

    def _limit(self) -> int:
        m = self._maxsize() if callable(self._maxsize) else self._maxsize
        return max(1, int(m))

    def get(self, key: K, default=None):
        with self._lock:
            val = self._data.get(key, _MISSING)
            if val is _MISSING:
                return default
            self._data.move_to_end(key)
            return val

    def __setitem__(self, key: K, val: V) -> None:
        with self._lock:
            self._data[key] = val
            self._data.move_to_end(key)
            limit = self._limit()
            while len(self._data) > limit:
                self._data.popitem(last=False)

    def __getitem__(self, key: K) -> V:
        val = self.get(key, _MISSING)
        if val is _MISSING:
            raise KeyError(key)
        return val

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._data

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def __iter__(self) -> Iterator[K]:
        with self._lock:
            return iter(list(self._data))

    def pop(self, key: K, default=None):
        with self._lock:
            return self._data.pop(key, default)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


#: digest of a test file's bytes -> the dotted names it imports.
_file_imports: "BoundedLRU[bytes, Tuple[str, ...]]" = BoundedLRU(file_cache_max)


def content_digest(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=16).digest()


def imported_names(source: str, filename: str = "<test>") -> Tuple[str, ...]:
    """Every name a module imports, as the map keys them.

    ``import a.b`` -> ``a.b``; ``from a import b`` -> ``a`` and ``a.b``.
    Order-preserving and de-duplicated. Raises SyntaxError like ``ast.parse``.
    """
    tree = ast.parse(source, filename=filename)
    out: Dict[str, None] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.setdefault(sys.intern(alias.name), None)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module:
                out.setdefault(sys.intern(module), None)
            for alias in node.names:
                full = f"{module}.{alias.name}" if module else alias.name
                out.setdefault(sys.intern(full), None)
    return tuple(out)


def _imports_of(test_file: Path) -> Optional[Tuple[str, ...]]:
    """Import names of one file, parsed at most once per distinct content.

    None when the file is unreadable or not valid Python -- the same files
    the legacy builders skipped. A syntax error is cached as an empty tuple
    (it is a property of the bytes too) so a broken file is not re-parsed
    on every build.
    """
    try:
        data = test_file.read_bytes()
    except OSError:
        return None
    digest = content_digest(data)
    hit = _file_imports.get(digest)
    if hit is not None:
        return hit
    try:
        names = imported_names(
            data.decode("utf-8", errors="replace"), filename=str(test_file),
        )
    except (SyntaxError, ValueError):
        names = ()
    _file_imports[digest] = names
    return names


def iter_test_files(repo_root: Path, dir_names: FrozenSet[str]) -> Iterator[Path]:
    """Every ``test_*.py`` under the configured test roots, in stable order."""
    for tdn in sorted(dir_names):
        top = repo_root / tdn
        if not top.is_dir():
            continue
        for test_file in sorted(top.rglob("test_*.py")):
            if test_file.is_file():
                yield test_file


def build_import_map(
    repo_root: Path,
    dir_names: FrozenSet[str],
    *,
    files: Optional[Iterable[Path]] = None,
) -> Dict[str, List[Path]]:
    """``dotted_module -> [test files under repo_root that import it]``.

    Same output as the two builders it replaces. ``files`` lets a caller that
    already walked the tree reuse its listing.
    """
    import_map: Dict[str, List[Path]] = {}
    for test_file in (files if files is not None else iter_test_files(repo_root, dir_names)):
        names = _imports_of(test_file)
        if not names:
            continue
        for name in names:
            import_map.setdefault(name, []).append(test_file)
    return import_map


def reset_for_tests() -> None:
    _file_imports.clear()
