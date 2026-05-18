"""
Tiered Source Crawlers
======================

Materialise filesystem and git sources into :class:`SnapshotFragment` objects.

Two tiers are defined:

- **P0** (tier 0) — always-on, authoritative sources: specs, plans, backlog,
  memory files, and configuration documents (CLAUDE.md, AGENTS.md).
- **P1** (tier 1) — trajectory sources: bounded git log.

Zero model calls are made here; all work is pure I/O.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path
from typing import List

from backend.core.ouroboros.roadmap.snapshot import SnapshotFragment


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _hash_content(content: str) -> str:
    """Return the SHA-256 hex digest of *content* (UTF-8 encoded)."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _extract_title(content: str, path: Path) -> str:
    """Return the first ``# Heading`` line's text, or the file stem."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return path.stem


def _extract_summary(content: str, max_len: int = 500) -> str:
    """Return the first *max_len* characters of *content* (stripped)."""
    return content.strip()[:max_len]


def _fragment_from_file(
    path: Path,
    source_id: str,
    fragment_type: str,
    tier: int,
    uri: str | None = None,
) -> SnapshotFragment:
    """Build a :class:`SnapshotFragment` from an on-disk file."""
    content = path.read_text(encoding="utf-8", errors="replace")
    stat = path.stat()
    return SnapshotFragment(
        source_id=source_id,
        uri=uri if uri is not None else str(path),
        tier=tier,
        content_hash=_hash_content(content),
        fetched_at=time.time(),
        mtime=stat.st_mtime,
        title=_extract_title(content, path),
        summary=_extract_summary(content),
        fragment_type=fragment_type,
    )


# ---------------------------------------------------------------------------
# P0 Crawlers
# ---------------------------------------------------------------------------

def crawl_specs(repo_root: Path) -> List[SnapshotFragment]:
    """Crawl ``docs/superpowers/specs/*.md`` into tier-0 spec fragments.

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root.

    Returns
    -------
    List[SnapshotFragment]
        One fragment per ``.md`` file found; empty list if the directory
        does not exist or contains no Markdown files.
    """
    specs_dir = repo_root / "docs" / "superpowers" / "specs"
    if not specs_dir.is_dir():
        return []

    fragments: List[SnapshotFragment] = []
    for md_file in sorted(specs_dir.glob("*.md")):
        source_id = f"spec:{md_file.stem}"
        uri = str(md_file.relative_to(repo_root))
        fragments.append(
            _fragment_from_file(md_file, source_id, "spec", tier=0, uri=uri)
        )
    return fragments


def crawl_plans(repo_root: Path) -> List[SnapshotFragment]:
    """Crawl ``docs/superpowers/plans/*.md`` into tier-0 plan fragments.

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root.

    Returns
    -------
    List[SnapshotFragment]
        One fragment per ``.md`` file found; empty list if the directory
        does not exist or contains no Markdown files.
    """
    plans_dir = repo_root / "docs" / "superpowers" / "plans"
    if not plans_dir.is_dir():
        return []

    fragments: List[SnapshotFragment] = []
    for md_file in sorted(plans_dir.glob("*.md")):
        source_id = f"plan:{md_file.stem}"
        uri = str(md_file.relative_to(repo_root))
        fragments.append(
            _fragment_from_file(md_file, source_id, "plan", tier=0, uri=uri)
        )
    return fragments


def crawl_backlog(repo_root: Path) -> List[SnapshotFragment]:
    """Read ``.jarvis/backlog.json`` into a single tier-0 backlog fragment.

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root.

    Returns
    -------
    List[SnapshotFragment]
        A single-element list when the file exists; empty list otherwise.
    """
    backlog_path = repo_root / ".jarvis" / "backlog.json"
    if not backlog_path.is_file():
        return []

    content = backlog_path.read_text(encoding="utf-8", errors="replace")
    stat = backlog_path.stat()
    uri = str(backlog_path.relative_to(repo_root))
    return [
        SnapshotFragment(
            source_id="backlog:jarvis",
            uri=uri,
            tier=0,
            content_hash=_hash_content(content),
            fetched_at=time.time(),
            mtime=stat.st_mtime,
            title="JARVIS Backlog",
            summary=_extract_summary(content),
            fragment_type="backlog",
        )
    ]


def crawl_memory(repo_root: Path) -> List[SnapshotFragment]:
    """Crawl ``memory/*.md`` into tier-0 memory fragments.

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root.

    Returns
    -------
    List[SnapshotFragment]
        One fragment per ``.md`` file found; empty list if the directory
        does not exist or contains no Markdown files.
    """
    memory_dir = repo_root / "memory"
    if not memory_dir.is_dir():
        return []

    fragments: List[SnapshotFragment] = []
    for md_file in sorted(memory_dir.glob("*.md")):
        source_id = f"memory:{md_file.stem}"
        uri = str(md_file.relative_to(repo_root))
        fragments.append(
            _fragment_from_file(md_file, source_id, "memory", tier=0, uri=uri)
        )
    return fragments


def _parse_cargo_package(cargo_text: str) -> tuple[str, str]:
    """Return ``(package_name, package_description)`` from a Cargo.toml.

    Prefers stdlib ``tomllib`` (3.11+, no new dependency); falls back
    to a bounded regex over the ``[package]`` table if TOML parsing is
    unavailable or the manifest is malformed. Returns ``("", "")`` if
    no package name can be derived (workspace-only manifests).
    """
    try:
        import tomllib  # stdlib 3.11+ — no new dependency
        data = tomllib.loads(cargo_text)
        pkg = data.get("package", {}) if isinstance(data, dict) else {}
        if isinstance(pkg, dict):
            return (
                str(pkg.get("name", "") or ""),
                str(pkg.get("description", "") or ""),
            )
    except Exception:  # noqa: BLE001 — fall through to regex
        pass
    import re

    name = ""
    desc = ""
    # Scope the scan to the [package] table only (a Cargo.toml may
    # also carry [dependencies] etc. with their own name= keys).
    in_pkg = False
    for line in cargo_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_pkg = stripped == "[package]"
            continue
        if not in_pkg:
            continue
        m = re.match(r'name\s*=\s*"([^"]+)"', stripped)
        if m and not name:
            name = m.group(1)
        m = re.match(r'description\s*=\s*"([^"]+)"', stripped)
        if m and not desc:
            desc = m.group(1)
    return name, desc


def crawl_rust_subsystems(repo_root: Path) -> List[SnapshotFragment]:
    """Crawl Rust crate manifests into tier-0 ``rust_crate`` fragments.

    Makes O+V *aware* the Rust subsystems exist so it proactively
    reaches for them via the language-agnostic Venom tools
    (``read_file`` / ``glob_files`` / ``search_code`` / ``bash``).
    The Oracle structural graph remains Python-only — this crawler
    does NOT change that; it only surfaces a crate map.

    Discovery is fully dynamic (no hardcoded crate list):

      * Walk ``repo_root / <search_root>`` for ``**/Cargo.toml``
        (env ``JARVIS_STRATEGIC_RUST_SEARCH_ROOT``, default
        ``"backend"``).
      * Skip build artifacts / vendored / worktree copies
        (``target/``, ``.git/``, ``worktrees/``, ``node_modules``)
        so we surface real first-party crates, not compiled output.
      * Parse ``[package].name`` / ``.description`` (stdlib
        ``tomllib``, regex fallback). Workspace-only manifests
        (no ``[package]``) are skipped.
      * De-duplicate by crate name (stable, sorted-path order).
      * Cap at ``JARVIS_STRATEGIC_RUST_MAX_CRATES`` (default 12).

    Returns one fragment per crate; empty list if the search root
    does not exist or no crates are found.
    """
    search_root_name = os.environ.get(
        "JARVIS_STRATEGIC_RUST_SEARCH_ROOT", "backend",
    ).strip() or "backend"
    try:
        max_crates = int(
            os.environ.get("JARVIS_STRATEGIC_RUST_MAX_CRATES", "12")
        )
    except (TypeError, ValueError):
        max_crates = 12
    max_crates = max(1, max_crates)

    search_root = repo_root / search_root_name
    if not search_root.is_dir():
        return []

    _SKIP = ("/target/", "/.git/", "/worktrees/", "/node_modules/")
    fragments: List[SnapshotFragment] = []
    seen_names: set[str] = set()
    for cargo in sorted(search_root.rglob("Cargo.toml")):
        cargo_str = str(cargo)
        if any(s in cargo_str for s in _SKIP):
            continue
        try:
            cargo_text = cargo.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        name, description = _parse_cargo_package(cargo_text)
        if not name or name in seen_names:
            continue  # workspace-only manifest, or a duplicate name
        seen_names.add(name)

        crate_dir = cargo.parent
        # Summary precedence: sibling README first line -> Cargo
        # description -> crate path (always something non-empty).
        summary = ""
        readme = crate_dir / "README.md"
        if readme.is_file():
            try:
                for line in readme.read_text(
                    encoding="utf-8", errors="replace",
                ).splitlines():
                    if line.strip():
                        summary = line.strip().lstrip("# ").strip()
                        break
            except OSError:
                pass
        if not summary:
            summary = description.strip()
        try:
            rel = str(crate_dir.relative_to(repo_root))
        except ValueError:
            rel = str(crate_dir)
        if not summary:
            summary = rel

        try:
            mtime = cargo.stat().st_mtime
        except OSError:
            mtime = 0.0
        fragments.append(SnapshotFragment(
            source_id=f"rust:{name}",
            uri=rel,
            tier=0,
            content_hash=_hash_content(cargo_text),
            fetched_at=time.time(),
            mtime=mtime,
            title=name,
            summary=summary[:500],
            fragment_type="rust_crate",
        ))
        if len(fragments) >= max_crates:
            break
    return fragments


def crawl_claude_md(repo_root: Path) -> List[SnapshotFragment]:
    """Read ``CLAUDE.md`` and ``AGENTS.md`` from *repo_root* if they exist.

    Each file becomes a tier-0 memory fragment.

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root.

    Returns
    -------
    List[SnapshotFragment]
        Zero, one, or two fragments depending on which files exist.
    """
    candidates = ["CLAUDE.md", "AGENTS.md"]
    fragments: List[SnapshotFragment] = []
    for name in candidates:
        candidate = repo_root / name
        if not candidate.is_file():
            continue
        source_id = f"config:{name}"
        uri = name  # relative to repo root — always top-level
        fragments.append(
            _fragment_from_file(candidate, source_id, "memory", tier=0, uri=uri)
        )
    return fragments


# ---------------------------------------------------------------------------
# P1 Crawlers
# ---------------------------------------------------------------------------

def crawl_git_log(
    repo_root: Path,
    max_commits: int = 50,
    max_days: int = 30,
) -> List[SnapshotFragment]:
    """Run ``git log`` over the repository and return a single tier-1 fragment.

    The fragment's ``summary`` contains the raw one-line log output (up to
    500 characters).  No exception is raised on any error — the caller
    receives an empty list instead.

    Parameters
    ----------
    repo_root:
        Absolute path to the repository root (or any directory inside it).
    max_commits:
        Maximum number of commits to include (``--max-count``).
    max_days:
        Maximum age of commits in days (``--since``).

    Returns
    -------
    List[SnapshotFragment]
        A single-element list on success; empty list if the directory is not
        a git repository or if the subprocess times out / errors.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "log",
                f"--max-count={max_commits}",
                f"--since={max_days} days ago",
                "--oneline",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(repo_root),
        )
        if result.returncode != 0:
            return []
        log_output = result.stdout.strip()
    except Exception:  # noqa: BLE001 — covers timeout, FileNotFoundError, etc.
        return []

    if not log_output:
        # Git succeeded but no commits in range — still return a fragment so
        # callers know the repo exists and is accessible.
        log_output = "(no commits in range)"

    repo_name = repo_root.resolve().name
    source_id = f"git:{repo_name}:bounded"
    now = time.time()
    return [
        SnapshotFragment(
            source_id=source_id,
            uri=f"git log --max-count={max_commits} --since={max_days}d",
            tier=1,
            content_hash=_hash_content(log_output),
            fetched_at=now,
            mtime=now,
            title=f"Git log ({repo_name}, last {max_days}d)",
            summary=_extract_summary(log_output),
            fragment_type="commit_log",
        )
    ]
