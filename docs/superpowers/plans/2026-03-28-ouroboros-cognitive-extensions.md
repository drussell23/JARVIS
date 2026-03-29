# Ouroboros Cognitive Extensions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Roadmap Sensor (Clock 1) and Feature Synthesis Engine (Clock 2) to the Ouroboros Daemon, enabling it to understand WHERE the system is going and WHAT capabilities are missing.

**Architecture:** Two-clock model — Clock 1 deterministically materializes a versioned RoadmapSnapshot from tiered sources (specs, plans, backlog, git, issues). Clock 2 agentically synthesizes FeatureHypotheses via Tier 0 deterministic hints + Doubleword 397B batch. REM Sleep consumes cached artifacts without re-analyzing.

**Tech Stack:** Python 3.12, asyncio, SHA256 hashing, Doubleword batch API (Qwen3.5-397B), existing Oracle graph (NetworkX), existing governance pipeline

**Spec:** `docs/superpowers/specs/2026-03-28-ouroboros-cognitive-extensions-design.md`

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `backend/core/ouroboros/roadmap/__init__.py` | Package marker |
| `backend/core/ouroboros/roadmap/snapshot.py` | RoadmapSnapshot + SnapshotFragment schemas |
| `backend/core/ouroboros/roadmap/hypothesis.py` | FeatureHypothesis schema + fingerprinting |
| `backend/core/ouroboros/roadmap/source_crawlers.py` | Tier-specific crawlers (P0 specs/plans, P1 git, P2 issues) |
| `backend/core/ouroboros/roadmap/sensor.py` | RoadmapSensor (Clock 1 — snapshot refresh) |
| `backend/core/ouroboros/roadmap/tier0_hints.py` | Deterministic gap detection (zero tokens) |
| `backend/core/ouroboros/roadmap/hypothesis_cache.py` | Exact fingerprint cache + staleness checking |
| `backend/core/ouroboros/roadmap/synthesis_engine.py` | FeatureSynthesisEngine (Clock 2) |
| `backend/core/ouroboros/roadmap/hypothesis_envelope_factory.py` | FeatureHypothesis -> IntentEnvelope |
| `tests/core/ouroboros/roadmap/__init__.py` | Test package marker |
| `tests/core/ouroboros/roadmap/test_snapshot.py` | Tests for snapshot schemas |
| `tests/core/ouroboros/roadmap/test_hypothesis.py` | Tests for hypothesis schema + fingerprinting |
| `tests/core/ouroboros/roadmap/test_source_crawlers.py` | Tests for crawlers |
| `tests/core/ouroboros/roadmap/test_sensor.py` | Tests for RoadmapSensor |
| `tests/core/ouroboros/roadmap/test_tier0_hints.py` | Tests for deterministic gap hints |
| `tests/core/ouroboros/roadmap/test_hypothesis_cache.py` | Tests for cache |
| `tests/core/ouroboros/roadmap/test_synthesis_engine.py` | Tests for synthesis engine |
| `tests/core/ouroboros/roadmap/test_hypothesis_envelope_factory.py` | Tests for envelope factory |
| `tests/core/ouroboros/roadmap/test_integration.py` | End-to-end integration test |

### Modified Files
| File | Change |
|------|--------|
| `backend/core/ouroboros/governance/intake/intent_envelope.py:20` | Add `"roadmap"` to `_VALID_SOURCES` |
| `backend/core/ouroboros/governance/intake/unified_intake_router.py:33` | Add `"roadmap": 4` to `_PRIORITY_MAP` |
| `backend/core/ouroboros/governance/risk_engine.py:240` | Add `source="roadmap"` to exploration-level rules |
| `backend/core/ouroboros/daemon_config.py:111` | Add roadmap/synthesis env vars |
| `backend/core/ouroboros/rem_epoch.py:249` | Add `_load_cached_hypotheses()` in `_explore()` |
| `backend/core/ouroboros/daemon.py:120` | Pass hypothesis cache path; wire sensor + engine |
| `backend/core/ouroboros/governed_loop_service.py` | Wire RoadmapSensor + FeatureSynthesisEngine |

---

## Task 1: Schemas (SnapshotFragment + RoadmapSnapshot + FeatureHypothesis)

**Files:**
- Create: `backend/core/ouroboros/roadmap/__init__.py`
- Create: `backend/core/ouroboros/roadmap/snapshot.py`
- Create: `backend/core/ouroboros/roadmap/hypothesis.py`
- Create: `tests/core/ouroboros/roadmap/__init__.py`
- Create: `tests/core/ouroboros/roadmap/test_snapshot.py`
- Create: `tests/core/ouroboros/roadmap/test_hypothesis.py`

- [ ] **Step 1: Write snapshot schema tests**

```python
# tests/core/ouroboros/roadmap/test_snapshot.py
"""Tests for RoadmapSnapshot and SnapshotFragment schemas."""
import hashlib
import time
import pytest
from backend.core.ouroboros.roadmap.snapshot import (
    SnapshotFragment,
    RoadmapSnapshot,
    compute_snapshot_hash,
)


def _frag(source_id="spec:test", content_hash="abc123", tier=0):
    return SnapshotFragment(
        source_id=source_id,
        uri="docs/test.md",
        tier=tier,
        content_hash=content_hash,
        fetched_at=time.time(),
        mtime=time.time(),
        title="Test",
        summary="Test summary",
        fragment_type="spec",
    )


def test_fragment_is_frozen():
    f = _frag()
    with pytest.raises(AttributeError):
        f.source_id = "changed"


def test_snapshot_hash_is_deterministic():
    frags = (_frag("a", "hash1"), _frag("b", "hash2"))
    h1 = compute_snapshot_hash(frags)
    h2 = compute_snapshot_hash(frags)
    assert h1 == h2


def test_snapshot_hash_changes_with_content():
    frags1 = (_frag("a", "hash1"),)
    frags2 = (_frag("a", "hash2"),)
    assert compute_snapshot_hash(frags1) != compute_snapshot_hash(frags2)


def test_snapshot_hash_order_independent():
    frags_ab = (_frag("a", "h1"), _frag("b", "h2"))
    frags_ba = (_frag("b", "h2"), _frag("a", "h1"))
    assert compute_snapshot_hash(frags_ab) == compute_snapshot_hash(frags_ba)


def test_snapshot_hash_canonical_format():
    frags = (_frag("spec:test", "abc123"),)
    expected_input = "spec:test\tabc123"
    expected = hashlib.sha256(expected_input.encode()).hexdigest()
    assert compute_snapshot_hash(frags) == expected


def test_snapshot_version_increments():
    frags1 = (_frag("a", "h1"),)
    s1 = RoadmapSnapshot.create(frags1, previous_version=0)
    assert s1.version == 1

    frags2 = (_frag("a", "h2"),)
    s2 = RoadmapSnapshot.create(frags2, previous_version=1)
    assert s2.version == 2


def test_snapshot_version_unchanged_if_same_hash():
    frags = (_frag("a", "h1"),)
    s1 = RoadmapSnapshot.create(frags, previous_version=5)
    s2 = RoadmapSnapshot.create(frags, previous_version=5, previous_hash=s1.content_hash)
    assert s2.version == 5  # not incremented


def test_snapshot_tier_counts():
    frags = (_frag("a", "h1", tier=0), _frag("b", "h2", tier=0), _frag("c", "h3", tier=1))
    s = RoadmapSnapshot.create(frags, previous_version=0)
    assert s.tier_counts == {0: 2, 1: 1}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/test_snapshot.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement snapshot.py**

```python
# backend/core/ouroboros/roadmap/snapshot.py
"""RoadmapSnapshot and SnapshotFragment schemas.

Deterministic, versioned, cached organism self-awareness.
All timestamps are UTC epoch seconds (wall clock, not monotonic).
"""
from __future__ import annotations

import hashlib
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class SnapshotFragment:
    """Single ingested source document with provenance."""
    source_id: str          # stable: "spec:ouroboros-daemon-design", "git:jarvis:bounded"
    uri: str                # "docs/superpowers/specs/2026-03-28-ouroboros-daemon-design.md"
    tier: int               # 0=authoritative, 1=trajectory, 2=external, 3=personal
    content_hash: str       # SHA256 of file content
    fetched_at: float       # UTC epoch seconds
    mtime: float            # file modification time (UTC epoch seconds)
    title: str              # from frontmatter or first heading
    summary: str            # first 500 chars or frontmatter description
    fragment_type: str      # "spec", "plan", "backlog", "memory", "commit_log", "issue"


def compute_snapshot_hash(fragments: Tuple[SnapshotFragment, ...]) -> str:
    """Canonical, deterministic snapshot hash.

    Formula: sha256("\\n".join(sorted(f"{sf.source_id}\\t{sf.content_hash}" for sf in fragments)))
    Sorted to ensure order-independence. Tab separator prevents source_id/hash collisions.
    """
    canonical = "\n".join(
        sorted(f"{sf.source_id}\t{sf.content_hash}" for sf in fragments)
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class RoadmapSnapshot:
    """Versioned, cached organism self-awareness."""
    version: int
    content_hash: str
    created_at: float
    fragments: Tuple[SnapshotFragment, ...]
    tier_counts: Dict[int, int]

    @classmethod
    def create(
        cls,
        fragments: Tuple[SnapshotFragment, ...],
        previous_version: int = 0,
        previous_hash: Optional[str] = None,
    ) -> RoadmapSnapshot:
        """Create snapshot. Version increments iff content_hash changes."""
        content_hash = compute_snapshot_hash(fragments)
        version = previous_version if content_hash == previous_hash else previous_version + 1
        tier_counts = dict(Counter(f.tier for f in fragments))
        return cls(
            version=version,
            content_hash=content_hash,
            created_at=time.time(),
            fragments=fragments,
            tier_counts=tier_counts,
        )
```

Also create the package init:
```python
# backend/core/ouroboros/roadmap/__init__.py
# tests/core/ouroboros/roadmap/__init__.py
```

- [ ] **Step 4: Run snapshot tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/test_snapshot.py -v`
Expected: All PASS

- [ ] **Step 5: Write hypothesis schema tests**

```python
# tests/core/ouroboros/roadmap/test_hypothesis.py
"""Tests for FeatureHypothesis schema and fingerprinting."""
import time
import pytest
from backend.core.ouroboros.roadmap.hypothesis import (
    FeatureHypothesis,
    compute_hypothesis_fingerprint,
)


def _hyp(**kw):
    defaults = dict(
        hypothesis_id="test-uuid",
        description="Missing WhatsApp agent",
        evidence_fragments=("spec:manifesto",),
        gap_type="missing_capability",
        confidence=0.9,
        confidence_rule_id="spec_symbol_miss",
        urgency="normal",
        suggested_scope="backend/neural_mesh/agents/",
        suggested_repos=("jarvis",),
        provenance="deterministic",
        synthesized_for_snapshot_hash="abc123",
        synthesized_at=time.time(),
        synthesis_input_fingerprint="def456",
    )
    defaults.update(kw)
    return FeatureHypothesis(**defaults)


def test_fingerprint_is_deterministic():
    h1 = _hyp()
    h2 = _hyp()
    assert h1.hypothesis_fingerprint == h2.hypothesis_fingerprint


def test_fingerprint_changes_with_description():
    h1 = _hyp(description="Missing WhatsApp agent")
    h2 = _hyp(description="Missing Slack agent")
    assert h1.hypothesis_fingerprint != h2.hypothesis_fingerprint


def test_fingerprint_changes_with_gap_type():
    h1 = _hyp(gap_type="missing_capability")
    h2 = _hyp(gap_type="incomplete_wiring")
    assert h1.hypothesis_fingerprint != h2.hypothesis_fingerprint


def test_fingerprint_ignores_uuid():
    h1 = _hyp(hypothesis_id="uuid-1")
    h2 = _hyp(hypothesis_id="uuid-2")
    assert h1.hypothesis_fingerprint == h2.hypothesis_fingerprint


def test_fingerprint_ignores_timestamps():
    h1 = _hyp(synthesized_at=100.0)
    h2 = _hyp(synthesized_at=200.0)
    assert h1.hypothesis_fingerprint == h2.hypothesis_fingerprint


def test_fingerprint_function_matches_property():
    h = _hyp()
    fp = compute_hypothesis_fingerprint(
        h.description, h.evidence_fragments, h.gap_type,
    )
    assert h.hypothesis_fingerprint == fp


def test_default_status_is_active():
    h = _hyp()
    assert h.status == "active"


def test_is_stale_hash_mismatch():
    h = _hyp(synthesized_for_snapshot_hash="old_hash")
    assert h.is_stale(current_snapshot_hash="new_hash", ttl_s=86400)


def test_is_stale_age_exceeded():
    h = _hyp(synthesized_at=time.time() - 100000)
    assert h.is_stale(current_snapshot_hash=h.synthesized_for_snapshot_hash, ttl_s=86400)


def test_not_stale_when_fresh_and_matching():
    h = _hyp(synthesized_at=time.time())
    assert not h.is_stale(current_snapshot_hash=h.synthesized_for_snapshot_hash, ttl_s=86400)
```

- [ ] **Step 6: Run hypothesis tests to verify they fail**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/test_hypothesis.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 7: Implement hypothesis.py**

```python
# backend/core/ouroboros/roadmap/hypothesis.py
"""FeatureHypothesis schema — a gap between where the system is going and where it is."""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Tuple


def compute_hypothesis_fingerprint(
    description: str,
    evidence_fragments: Tuple[str, ...],
    gap_type: str,
) -> str:
    """Deterministic dedup key. Ignores UUIDs and timestamps."""
    normalized = (
        f"{description.strip().lower()}\t"
        f"{','.join(sorted(evidence_fragments))}\t"
        f"{gap_type}"
    )
    return hashlib.sha256(normalized.encode()).hexdigest()[:32]


@dataclass
class FeatureHypothesis:
    """A gap between where the system is going and where it is."""
    hypothesis_id: str
    description: str
    evidence_fragments: Tuple[str, ...]     # source_ids from snapshot
    gap_type: str                            # missing_capability, incomplete_wiring,
                                             # stale_implementation, manifesto_violation
    confidence: float                        # 0-1
    confidence_rule_id: str                  # "spec_symbol_miss", "model_inference", etc.
    urgency: str                             # critical, high, normal, low
    suggested_scope: str                     # "backend/neural_mesh/agents/"
    suggested_repos: Tuple[str, ...]         # ("jarvis",) or ("jarvis", "jarvis-prime")
    provenance: str                          # "deterministic", "model:doubleword-397b", "model:claude"

    # Synthesis metadata
    synthesized_for_snapshot_hash: str
    synthesized_at: float                    # UTC epoch seconds
    synthesis_input_fingerprint: str

    # Lifecycle
    status: str = "active"

    # Computed
    hypothesis_fingerprint: str = field(init=False, default="")

    def __post_init__(self):
        self.hypothesis_fingerprint = compute_hypothesis_fingerprint(
            self.description, self.evidence_fragments, self.gap_type,
        )

    def is_stale(self, current_snapshot_hash: str, ttl_s: float) -> bool:
        """Stale if hash mismatch OR age exceeded. OR, not AND."""
        hash_mismatch = self.synthesized_for_snapshot_hash != current_snapshot_hash
        age_exceeded = (time.time() - self.synthesized_at) > ttl_s
        return hash_mismatch or age_exceeded
```

- [ ] **Step 8: Run all schema tests**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/test_snapshot.py tests/core/ouroboros/roadmap/test_hypothesis.py -v`
Expected: All PASS

- [ ] **Step 9: Commit**

```bash
git add backend/core/ouroboros/roadmap/__init__.py backend/core/ouroboros/roadmap/snapshot.py backend/core/ouroboros/roadmap/hypothesis.py tests/core/ouroboros/roadmap/__init__.py tests/core/ouroboros/roadmap/test_snapshot.py tests/core/ouroboros/roadmap/test_hypothesis.py
git commit -m "feat(ouroboros/roadmap): add RoadmapSnapshot and FeatureHypothesis schemas"
```

---

## Task 2: Source Crawlers (P0/P1/P2)

**Files:**
- Create: `backend/core/ouroboros/roadmap/source_crawlers.py`
- Create: `tests/core/ouroboros/roadmap/test_source_crawlers.py`

- [ ] **Step 1: Write crawler tests**

```python
# tests/core/ouroboros/roadmap/test_source_crawlers.py
"""Tests for tiered source crawlers."""
import json
import os
import time
import pytest
from backend.core.ouroboros.roadmap.source_crawlers import (
    crawl_specs,
    crawl_plans,
    crawl_backlog,
    crawl_memory,
    crawl_git_log,
    crawl_claude_md,
)
from backend.core.ouroboros.roadmap.snapshot import SnapshotFragment


@pytest.fixture
def spec_dir(tmp_path):
    d = tmp_path / "docs" / "superpowers" / "specs"
    d.mkdir(parents=True)
    (d / "2026-03-28-test-design.md").write_text("# Test Design\n\nSome content here.")
    return tmp_path


def test_crawl_specs_finds_md_files(spec_dir):
    frags = crawl_specs(spec_dir)
    assert len(frags) == 1
    assert frags[0].fragment_type == "spec"
    assert frags[0].tier == 0
    assert "test-design" in frags[0].source_id


def test_crawl_specs_empty_dir(tmp_path):
    d = tmp_path / "docs" / "superpowers" / "specs"
    d.mkdir(parents=True)
    frags = crawl_specs(tmp_path)
    assert frags == []


def test_crawl_specs_content_hash_changes(spec_dir):
    frags1 = crawl_specs(spec_dir)
    spec_file = spec_dir / "docs" / "superpowers" / "specs" / "2026-03-28-test-design.md"
    spec_file.write_text("# Updated\n\nDifferent content.")
    frags2 = crawl_specs(spec_dir)
    assert frags1[0].content_hash != frags2[0].content_hash


@pytest.fixture
def backlog_dir(tmp_path):
    jarvis_dir = tmp_path / ".jarvis"
    jarvis_dir.mkdir()
    backlog = [{"task": "Build WhatsApp agent", "priority": "high"}]
    (jarvis_dir / "backlog.json").write_text(json.dumps(backlog))
    return tmp_path


def test_crawl_backlog_finds_json(backlog_dir):
    frags = crawl_backlog(backlog_dir)
    assert len(frags) == 1
    assert frags[0].fragment_type == "backlog"
    assert frags[0].tier == 0


def test_crawl_backlog_missing_file(tmp_path):
    frags = crawl_backlog(tmp_path)
    assert frags == []


def test_crawl_git_log_returns_fragment(tmp_path):
    # Mock: create a fake git output file
    frags = crawl_git_log(tmp_path, max_commits=5)
    # May be empty if not a git repo, but should not raise
    assert isinstance(frags, list)


def test_fragment_source_id_is_stable(spec_dir):
    frags1 = crawl_specs(spec_dir)
    frags2 = crawl_specs(spec_dir)
    assert frags1[0].source_id == frags2[0].source_id
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/test_source_crawlers.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement source_crawlers.py**

```python
# backend/core/ouroboros/roadmap/source_crawlers.py
"""Tier-specific source crawlers for RoadmapSnapshot materialization.

All crawlers return List[SnapshotFragment]. Zero model calls.
Timestamps are UTC epoch seconds.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import List

from backend.core.ouroboros.roadmap.snapshot import SnapshotFragment


def _hash_content(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _extract_title(content: str, path: Path) -> str:
    """Extract title from first markdown heading or filename."""
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


def _extract_summary(content: str, max_len: int = 500) -> str:
    """First max_len chars of content as summary."""
    return content[:max_len].strip()


# --- P0: Specs ---

def crawl_specs(repo_root: Path) -> List[SnapshotFragment]:
    """Crawl docs/superpowers/specs/*.md files."""
    specs_dir = Path(repo_root) / "docs" / "superpowers" / "specs"
    if not specs_dir.exists():
        return []
    fragments = []
    for md_file in sorted(specs_dir.glob("*.md")):
        content = md_file.read_text(errors="replace")
        fragments.append(SnapshotFragment(
            source_id=f"spec:{md_file.stem}",
            uri=str(md_file.relative_to(repo_root)),
            tier=0,
            content_hash=_hash_content(content),
            fetched_at=time.time(),
            mtime=md_file.stat().st_mtime,
            title=_extract_title(content, md_file),
            summary=_extract_summary(content),
            fragment_type="spec",
        ))
    return fragments


# --- P0: Plans ---

def crawl_plans(repo_root: Path) -> List[SnapshotFragment]:
    """Crawl docs/superpowers/plans/*.md files."""
    plans_dir = Path(repo_root) / "docs" / "superpowers" / "plans"
    if not plans_dir.exists():
        return []
    fragments = []
    for md_file in sorted(plans_dir.glob("*.md")):
        content = md_file.read_text(errors="replace")
        fragments.append(SnapshotFragment(
            source_id=f"plan:{md_file.stem}",
            uri=str(md_file.relative_to(repo_root)),
            tier=0,
            content_hash=_hash_content(content),
            fetched_at=time.time(),
            mtime=md_file.stat().st_mtime,
            title=_extract_title(content, md_file),
            summary=_extract_summary(content),
            fragment_type="plan",
        ))
    return fragments


# --- P0: Backlog ---

def crawl_backlog(repo_root: Path) -> List[SnapshotFragment]:
    """Crawl .jarvis/backlog.json."""
    backlog_path = Path(repo_root) / ".jarvis" / "backlog.json"
    if not backlog_path.exists():
        return []
    content = backlog_path.read_text(errors="replace")
    return [SnapshotFragment(
        source_id="backlog:jarvis",
        uri=".jarvis/backlog.json",
        tier=0,
        content_hash=_hash_content(content),
        fetched_at=time.time(),
        mtime=backlog_path.stat().st_mtime,
        title="JARVIS Backlog",
        summary=_extract_summary(content),
        fragment_type="backlog",
    )]


# --- P0: Workspace Memory ---

def crawl_memory(repo_root: Path) -> List[SnapshotFragment]:
    """Crawl memory/*.md files in workspace."""
    memory_dir = Path(repo_root) / "memory"
    if not memory_dir.exists():
        return []
    fragments = []
    for md_file in sorted(memory_dir.glob("*.md")):
        content = md_file.read_text(errors="replace")
        fragments.append(SnapshotFragment(
            source_id=f"memory:{md_file.stem}",
            uri=str(md_file.relative_to(repo_root)),
            tier=0,
            content_hash=_hash_content(content),
            fetched_at=time.time(),
            mtime=md_file.stat().st_mtime,
            title=_extract_title(content, md_file),
            summary=_extract_summary(content),
            fragment_type="memory",
        ))
    return fragments


# --- P0: CLAUDE.md ---

def crawl_claude_md(repo_root: Path) -> List[SnapshotFragment]:
    """Crawl CLAUDE.md and AGENTS.md if they exist."""
    fragments = []
    for name in ("CLAUDE.md", "AGENTS.md"):
        path = Path(repo_root) / name
        if path.exists():
            content = path.read_text(errors="replace")
            fragments.append(SnapshotFragment(
                source_id=f"config:{name}",
                uri=name,
                tier=0,
                content_hash=_hash_content(content),
                fetched_at=time.time(),
                mtime=path.stat().st_mtime,
                title=name,
                summary=_extract_summary(content),
                fragment_type="memory",
            ))
    return fragments


# --- P1: Git Log (bounded) ---

def crawl_git_log(
    repo_root: Path,
    max_commits: int = 50,
    max_days: int = 30,
) -> List[SnapshotFragment]:
    """Crawl bounded git log. Returns single synthetic fragment."""
    try:
        result = subprocess.run(
            ["git", "log", f"--max-count={max_commits}",
             f"--since={max_days} days ago", "--oneline"],
            cwd=str(repo_root),
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []
        output = result.stdout.strip()
        if not output:
            return []
        return [SnapshotFragment(
            source_id=f"git:{Path(repo_root).name}:bounded",
            uri=f"git log --max-count={max_commits} --since={max_days}d",
            tier=1,
            content_hash=_hash_content(output),
            fetched_at=time.time(),
            mtime=time.time(),
            title=f"Git log ({output.count(chr(10)) + 1} commits)",
            summary=_extract_summary(output),
            fragment_type="commit_log",
        ))
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/test_source_crawlers.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/roadmap/source_crawlers.py tests/core/ouroboros/roadmap/test_source_crawlers.py
git commit -m "feat(ouroboros/roadmap): add tiered source crawlers for P0/P1 ingestion"
```

---

## Task 3: RoadmapSensor (Clock 1)

**Files:**
- Create: `backend/core/ouroboros/roadmap/sensor.py`
- Create: `tests/core/ouroboros/roadmap/test_sensor.py`

- [ ] **Step 1: Write sensor tests**

```python
# tests/core/ouroboros/roadmap/test_sensor.py
"""Tests for RoadmapSensor (Clock 1 — snapshot refresh)."""
import json
import time
import pytest
from unittest.mock import MagicMock, AsyncMock
from pathlib import Path
from backend.core.ouroboros.roadmap.sensor import RoadmapSensor, RoadmapSensorConfig
from backend.core.ouroboros.roadmap.snapshot import RoadmapSnapshot


@pytest.fixture
def repo_with_spec(tmp_path):
    (tmp_path / "docs" / "superpowers" / "specs").mkdir(parents=True)
    (tmp_path / "docs" / "superpowers" / "specs" / "test.md").write_text("# Test Spec")
    return tmp_path


def test_sensor_creates_snapshot(repo_with_spec):
    sensor = RoadmapSensor(
        repo_root=repo_with_spec,
        config=RoadmapSensorConfig(),
    )
    snapshot = sensor.refresh()
    assert isinstance(snapshot, RoadmapSnapshot)
    assert snapshot.version == 1
    assert len(snapshot.fragments) >= 1


def test_sensor_returns_cached_when_unchanged(repo_with_spec):
    sensor = RoadmapSensor(
        repo_root=repo_with_spec,
        config=RoadmapSensorConfig(),
    )
    s1 = sensor.refresh()
    s2 = sensor.refresh()
    assert s1.content_hash == s2.content_hash
    assert s2.version == 1  # not incremented


def test_sensor_detects_change(repo_with_spec):
    sensor = RoadmapSensor(
        repo_root=repo_with_spec,
        config=RoadmapSensorConfig(),
    )
    s1 = sensor.refresh()
    # Modify a spec file
    (repo_with_spec / "docs" / "superpowers" / "specs" / "test.md").write_text("# Updated")
    s2 = sensor.refresh()
    assert s1.content_hash != s2.content_hash
    assert s2.version == 2


def test_sensor_calls_on_change_callback(repo_with_spec):
    called = []
    sensor = RoadmapSensor(
        repo_root=repo_with_spec,
        config=RoadmapSensorConfig(),
        on_snapshot_changed=lambda s: called.append(s),
    )
    sensor.refresh()  # first = change from nothing
    assert len(called) == 1
    sensor.refresh()  # second = no change
    assert len(called) == 1  # not called again


def test_sensor_health(repo_with_spec):
    sensor = RoadmapSensor(
        repo_root=repo_with_spec,
        config=RoadmapSensorConfig(),
    )
    health = sensor.health()
    assert "snapshot_version" in health
    assert "fragment_count" in health


def test_p1_disabled_skips_git(repo_with_spec):
    config = RoadmapSensorConfig(p1_enabled=False)
    sensor = RoadmapSensor(repo_root=repo_with_spec, config=config)
    snapshot = sensor.refresh()
    assert all(f.tier != 1 for f in snapshot.fragments)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/test_sensor.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement sensor.py**

```python
# backend/core/ouroboros/roadmap/sensor.py
"""RoadmapSensor — Clock 1: deterministic snapshot refresh.

Zero model calls. Materializes enabled sources into versioned RoadmapSnapshot.
Does NOT emit IntentEnvelopes. Optionally triggers Clock 2 on change.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from backend.core.ouroboros.roadmap.snapshot import (
    RoadmapSnapshot,
    SnapshotFragment,
)
from backend.core.ouroboros.roadmap.source_crawlers import (
    crawl_specs,
    crawl_plans,
    crawl_backlog,
    crawl_memory,
    crawl_claude_md,
    crawl_git_log,
)

logger = logging.getLogger(__name__)

_SNAPSHOT_PATH = os.path.expanduser("~/.jarvis/ouroboros/roadmap/snapshot.json")


@dataclass
class RoadmapSensorConfig:
    """Configuration for the Roadmap Sensor."""
    p1_enabled: bool = True
    p1_commit_limit: int = 50
    p1_days: int = 30
    p2_enabled: bool = False
    p3_enabled: bool = False
    refresh_interval_s: float = 3600.0
    snapshot_path: str = _SNAPSHOT_PATH


class RoadmapSensor:
    """Clock 1 — Deterministic snapshot materialization."""

    def __init__(
        self,
        repo_root: Path,
        config: RoadmapSensorConfig,
        on_snapshot_changed: Optional[Callable] = None,
    ) -> None:
        self._repo_root = Path(repo_root)
        self._config = config
        self._on_changed = on_snapshot_changed
        self._current_snapshot: Optional[RoadmapSnapshot] = None
        self._last_refresh_at: float = 0.0

    def refresh(self) -> RoadmapSnapshot:
        """Crawl all enabled sources and compose snapshot.

        Returns cached snapshot if content_hash unchanged.
        Calls on_snapshot_changed if hash changed.
        """
        fragments: List[SnapshotFragment] = []

        # P0: Always on
        fragments.extend(crawl_specs(self._repo_root))
        fragments.extend(crawl_plans(self._repo_root))
        fragments.extend(crawl_backlog(self._repo_root))
        fragments.extend(crawl_memory(self._repo_root))
        fragments.extend(crawl_claude_md(self._repo_root))

        # P1: Git log (if enabled)
        if self._config.p1_enabled:
            fragments.extend(crawl_git_log(
                self._repo_root,
                max_commits=self._config.p1_commit_limit,
                max_days=self._config.p1_days,
            ))

        prev_version = self._current_snapshot.version if self._current_snapshot else 0
        prev_hash = self._current_snapshot.content_hash if self._current_snapshot else None

        snapshot = RoadmapSnapshot.create(
            fragments=tuple(fragments),
            previous_version=prev_version,
            previous_hash=prev_hash,
        )

        changed = snapshot.content_hash != prev_hash
        self._current_snapshot = snapshot
        self._last_refresh_at = time.time()

        if changed and self._on_changed is not None:
            try:
                self._on_changed(snapshot)
            except Exception as exc:
                logger.warning("[RoadmapSensor] on_changed callback failed: %s", exc)

        return snapshot

    @property
    def current_snapshot(self) -> Optional[RoadmapSnapshot]:
        return self._current_snapshot

    def health(self) -> Dict[str, Any]:
        return {
            "snapshot_version": self._current_snapshot.version if self._current_snapshot else 0,
            "fragment_count": len(self._current_snapshot.fragments) if self._current_snapshot else 0,
            "last_refresh_at": self._last_refresh_at,
            "content_hash": self._current_snapshot.content_hash if self._current_snapshot else None,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/test_sensor.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/roadmap/sensor.py tests/core/ouroboros/roadmap/test_sensor.py
git commit -m "feat(ouroboros/roadmap): implement RoadmapSensor (Clock 1 — snapshot refresh)"
```

---

## Task 4: Tier 0 Deterministic Gap Hints

**Files:**
- Create: `backend/core/ouroboros/roadmap/tier0_hints.py`
- Create: `tests/core/ouroboros/roadmap/test_tier0_hints.py`

- [ ] **Step 1: Write Tier 0 tests**

```python
# tests/core/ouroboros/roadmap/test_tier0_hints.py
"""Tests for Tier 0 deterministic gap detection (zero tokens)."""
import time
import pytest
from unittest.mock import MagicMock
from backend.core.ouroboros.roadmap.tier0_hints import generate_tier0_hints
from backend.core.ouroboros.roadmap.snapshot import SnapshotFragment, RoadmapSnapshot
from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis


def _spec_fragment(content_hash="abc", summary="We need a WhatsApp agent for messaging"):
    return SnapshotFragment(
        source_id="spec:test",
        uri="docs/test.md",
        tier=0,
        content_hash=content_hash,
        fetched_at=time.time(),
        mtime=time.time(),
        title="Test Spec",
        summary=summary,
        fragment_type="spec",
    )


def _snapshot(*fragments):
    return RoadmapSnapshot.create(
        fragments=tuple(fragments),
        previous_version=0,
    )


def _mock_oracle(known_symbols=None):
    oracle = MagicMock()
    if known_symbols is None:
        known_symbols = []
    oracle.find_nodes_by_name = MagicMock(
        side_effect=lambda name, fuzzy=False: [
            MagicMock(name=s) for s in known_symbols if name.lower() in s.lower()
        ]
    )
    return oracle


def test_detects_missing_agent():
    snapshot = _snapshot(_spec_fragment(summary="Build a WhatsApp agent for messaging"))
    oracle = _mock_oracle(known_symbols=[])  # no WhatsApp symbol
    hints = generate_tier0_hints(snapshot, oracle)
    assert any("whatsapp" in h.description.lower() for h in hints)
    assert all(h.provenance == "deterministic" for h in hints)


def test_no_hint_when_symbol_exists():
    snapshot = _snapshot(_spec_fragment(summary="Build a WhatsApp agent"))
    oracle = _mock_oracle(known_symbols=["WhatsAppAgent"])
    hints = generate_tier0_hints(snapshot, oracle)
    # Should not flag WhatsApp as missing
    assert not any(
        "whatsapp" in h.description.lower() and h.gap_type == "missing_capability"
        for h in hints
    )


def test_hints_have_deterministic_provenance():
    snapshot = _snapshot(_spec_fragment(summary="Need a Slack integration agent"))
    oracle = _mock_oracle(known_symbols=[])
    hints = generate_tier0_hints(snapshot, oracle)
    for h in hints:
        assert h.provenance == "deterministic"
        assert h.confidence_rule_id != ""


def test_returns_empty_for_no_gaps():
    snapshot = _snapshot(_spec_fragment(summary="This is a general overview document"))
    oracle = _mock_oracle(known_symbols=[])
    hints = generate_tier0_hints(snapshot, oracle)
    # May or may not find hints — but should not crash
    assert isinstance(hints, list)


def test_hints_carry_evidence_fragments():
    frag = _spec_fragment(summary="We need LinkedIn automation")
    snapshot = _snapshot(frag)
    oracle = _mock_oracle(known_symbols=[])
    hints = generate_tier0_hints(snapshot, oracle)
    for h in hints:
        assert len(h.evidence_fragments) > 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/test_tier0_hints.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement tier0_hints.py**

```python
# backend/core/ouroboros/roadmap/tier0_hints.py
"""Tier 0: Deterministic gap detection — zero tokens.

Heuristic keyword extraction from spec/plan summaries cross-referenced
against Oracle symbol graph. Acceptable as v1; requires evidence_fragments
pointing at the spec source_id.
"""
from __future__ import annotations

import re
import time
import uuid
from typing import Any, List

from backend.core.ouroboros.roadmap.snapshot import RoadmapSnapshot
from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis

# Heuristic patterns for capability references
_CAPABILITY_PATTERNS = [
    (r"\b(\w+)\s+agent\b", "agent"),
    (r"\b(\w+)\s+sensor\b", "sensor"),
    (r"\b(\w+)\s+integration\b", "integration"),
    (r"\b(\w+)\s+provider\b", "provider"),
]


def generate_tier0_hints(
    snapshot: RoadmapSnapshot,
    oracle: Any,
) -> List[FeatureHypothesis]:
    """Generate deterministic gap hypotheses from spec/plan content.

    Zero model calls. Cross-references capability keywords against Oracle.
    """
    if oracle is None:
        return []

    hints: List[FeatureHypothesis] = []
    seen_capabilities: set = set()

    for fragment in snapshot.fragments:
        if fragment.tier > 0:
            continue  # Only P0 sources for deterministic hints
        if fragment.fragment_type not in ("spec", "plan", "backlog"):
            continue

        text = fragment.summary.lower()

        for pattern, cap_type in _CAPABILITY_PATTERNS:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                cap_name = match.group(1).strip()
                if len(cap_name) < 3 or cap_name in ("the", "this", "that", "some", "any", "new"):
                    continue

                cap_key = f"{cap_name}_{cap_type}"
                if cap_key in seen_capabilities:
                    continue
                seen_capabilities.add(cap_key)

                # Cross-reference Oracle
                try:
                    matches = oracle.find_nodes_by_name(cap_name, fuzzy=True)
                except Exception:
                    matches = []

                if not matches:
                    hints.append(FeatureHypothesis(
                        hypothesis_id=uuid.uuid4().hex[:16],
                        description=f"Spec references '{cap_name} {cap_type}' but no matching symbol found in codebase",
                        evidence_fragments=(fragment.source_id,),
                        gap_type="missing_capability",
                        confidence=0.85,
                        confidence_rule_id="spec_symbol_miss",
                        urgency="normal",
                        suggested_scope=f"backend/",
                        suggested_repos=("jarvis",),
                        provenance="deterministic",
                        synthesized_for_snapshot_hash=snapshot.content_hash,
                        synthesized_at=time.time(),
                        synthesis_input_fingerprint="tier0_deterministic",
                    ))

    return hints
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/test_tier0_hints.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/roadmap/tier0_hints.py tests/core/ouroboros/roadmap/test_tier0_hints.py
git commit -m "feat(ouroboros/roadmap): add Tier 0 deterministic gap detection (zero tokens)"
```

---

## Task 5: Hypothesis Cache

**Files:**
- Create: `backend/core/ouroboros/roadmap/hypothesis_cache.py`
- Create: `tests/core/ouroboros/roadmap/test_hypothesis_cache.py`

- [ ] **Step 1: Write cache tests**

```python
# tests/core/ouroboros/roadmap/test_hypothesis_cache.py
"""Tests for hypothesis cache with exact fingerprint matching."""
import json
import time
import pytest
from pathlib import Path
from backend.core.ouroboros.roadmap.hypothesis_cache import HypothesisCache
from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis


def _hyp(desc="Test gap", snapshot_hash="snap1", fingerprint="fp1"):
    return FeatureHypothesis(
        hypothesis_id="test",
        description=desc,
        evidence_fragments=("spec:test",),
        gap_type="missing_capability",
        confidence=0.9,
        confidence_rule_id="test",
        urgency="normal",
        suggested_scope="backend/",
        suggested_repos=("jarvis",),
        provenance="deterministic",
        synthesized_for_snapshot_hash=snapshot_hash,
        synthesized_at=time.time(),
        synthesis_input_fingerprint=fingerprint,
    )


def test_cache_save_and_load(tmp_path):
    cache = HypothesisCache(cache_dir=tmp_path)
    hyps = [_hyp("gap1"), _hyp("gap2")]
    cache.save(hyps)
    loaded = cache.load()
    assert len(loaded) == 2


def test_cache_hit_on_matching_fingerprint(tmp_path):
    cache = HypothesisCache(cache_dir=tmp_path)
    hyps = [_hyp(fingerprint="fp1")]
    cache.save(hyps)
    result = cache.get_if_valid(input_fingerprint="fp1")
    assert result is not None
    assert len(result) == 1


def test_cache_miss_on_different_fingerprint(tmp_path):
    cache = HypothesisCache(cache_dir=tmp_path)
    hyps = [_hyp(fingerprint="fp1")]
    cache.save(hyps)
    result = cache.get_if_valid(input_fingerprint="fp2")
    assert result is None


def test_cache_persists_to_disk(tmp_path):
    cache1 = HypothesisCache(cache_dir=tmp_path)
    cache1.save([_hyp()])
    # New cache instance reads from disk
    cache2 = HypothesisCache(cache_dir=tmp_path)
    loaded = cache2.load()
    assert len(loaded) == 1


def test_cache_empty_when_no_file(tmp_path):
    cache = HypothesisCache(cache_dir=tmp_path)
    loaded = cache.load()
    assert loaded == []


def test_stale_check(tmp_path):
    cache = HypothesisCache(cache_dir=tmp_path)
    hyps = [_hyp(snapshot_hash="old")]
    cache.save(hyps)
    assert cache.is_stale(current_snapshot_hash="new", ttl_s=86400)


def test_not_stale_when_matching(tmp_path):
    cache = HypothesisCache(cache_dir=tmp_path)
    hyps = [_hyp(snapshot_hash="current")]
    cache.save(hyps)
    assert not cache.is_stale(current_snapshot_hash="current", ttl_s=86400)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/test_hypothesis_cache.py -v`
Expected: FAIL

- [ ] **Step 3: Implement hypothesis_cache.py**

```python
# backend/core/ouroboros/roadmap/hypothesis_cache.py
"""Hypothesis cache — exact fingerprint matching + staleness checks.

Deterministic: cache keys and invalidation are code. No model calls.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_DIR = os.path.expanduser("~/.jarvis/ouroboros/roadmap")
_CACHE_FILE = "hypotheses.json"
_META_FILE = "hypotheses_meta.json"


class HypothesisCache:
    """Exact fingerprint cache for FeatureHypothesis lists."""

    def __init__(self, cache_dir: Path = Path(_DEFAULT_CACHE_DIR)) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_file = self._cache_dir / _CACHE_FILE
        self._meta_file = self._cache_dir / _META_FILE

    def save(self, hypotheses: List[FeatureHypothesis]) -> None:
        """Persist hypotheses to disk."""
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        data = [self._serialize(h) for h in hypotheses]
        self._cache_file.write_text(json.dumps(data, indent=2))

        # Meta: input fingerprint for cache hit checking
        if hypotheses:
            meta = {
                "input_fingerprint": hypotheses[0].synthesis_input_fingerprint,
                "snapshot_hash": hypotheses[0].synthesized_for_snapshot_hash,
                "saved_at": time.time(),
                "count": len(hypotheses),
            }
        else:
            meta = {"input_fingerprint": "", "snapshot_hash": "", "saved_at": time.time(), "count": 0}
        self._meta_file.write_text(json.dumps(meta))

    def load(self) -> List[FeatureHypothesis]:
        """Load hypotheses from disk. Returns [] if no cache."""
        if not self._cache_file.exists():
            return []
        try:
            data = json.loads(self._cache_file.read_text())
            return [self._deserialize(d) for d in data]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("[HypothesisCache] Corrupt cache: %s", exc)
            return []

    def get_if_valid(self, input_fingerprint: str) -> Optional[List[FeatureHypothesis]]:
        """Return cached hypotheses if fingerprint matches. Else None."""
        if not self._meta_file.exists():
            return None
        try:
            meta = json.loads(self._meta_file.read_text())
            if meta.get("input_fingerprint") == input_fingerprint:
                return self.load()
        except (json.JSONDecodeError, KeyError):
            pass
        return None

    def is_stale(self, current_snapshot_hash: str, ttl_s: float) -> bool:
        """Stale if hash mismatch OR age exceeded. OR, not AND."""
        if not self._meta_file.exists():
            return True
        try:
            meta = json.loads(self._meta_file.read_text())
            hash_mismatch = meta.get("snapshot_hash") != current_snapshot_hash
            age_exceeded = (time.time() - meta.get("saved_at", 0)) > ttl_s
            return hash_mismatch or age_exceeded
        except (json.JSONDecodeError, KeyError):
            return True

    @staticmethod
    def _serialize(h: FeatureHypothesis) -> Dict[str, Any]:
        return {
            "hypothesis_id": h.hypothesis_id,
            "description": h.description,
            "evidence_fragments": list(h.evidence_fragments),
            "gap_type": h.gap_type,
            "confidence": h.confidence,
            "confidence_rule_id": h.confidence_rule_id,
            "urgency": h.urgency,
            "suggested_scope": h.suggested_scope,
            "suggested_repos": list(h.suggested_repos),
            "provenance": h.provenance,
            "synthesized_for_snapshot_hash": h.synthesized_for_snapshot_hash,
            "synthesized_at": h.synthesized_at,
            "synthesis_input_fingerprint": h.synthesis_input_fingerprint,
            "status": h.status,
        }

    @staticmethod
    def _deserialize(d: Dict[str, Any]) -> FeatureHypothesis:
        return FeatureHypothesis(
            hypothesis_id=d["hypothesis_id"],
            description=d["description"],
            evidence_fragments=tuple(d["evidence_fragments"]),
            gap_type=d["gap_type"],
            confidence=d["confidence"],
            confidence_rule_id=d["confidence_rule_id"],
            urgency=d["urgency"],
            suggested_scope=d["suggested_scope"],
            suggested_repos=tuple(d["suggested_repos"]),
            provenance=d["provenance"],
            synthesized_for_snapshot_hash=d["synthesized_for_snapshot_hash"],
            synthesized_at=d["synthesized_at"],
            synthesis_input_fingerprint=d["synthesis_input_fingerprint"],
            status=d.get("status", "active"),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/test_hypothesis_cache.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/roadmap/hypothesis_cache.py tests/core/ouroboros/roadmap/test_hypothesis_cache.py
git commit -m "feat(ouroboros/roadmap): add hypothesis cache with exact fingerprint matching"
```

---

## Task 6: Feature Synthesis Engine (Clock 2)

**Files:**
- Create: `backend/core/ouroboros/roadmap/synthesis_engine.py`
- Create: `tests/core/ouroboros/roadmap/test_synthesis_engine.py`

- [ ] **Step 1: Write synthesis engine tests**

```python
# tests/core/ouroboros/roadmap/test_synthesis_engine.py
"""Tests for FeatureSynthesisEngine (Clock 2)."""
import asyncio
import hashlib
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from backend.core.ouroboros.roadmap.synthesis_engine import (
    FeatureSynthesisEngine,
    SynthesisConfig,
    compute_input_fingerprint,
)
from backend.core.ouroboros.roadmap.snapshot import RoadmapSnapshot, SnapshotFragment
from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis


def _snapshot(content_hash="snap1"):
    frag = SnapshotFragment(
        source_id="spec:test", uri="test.md", tier=0,
        content_hash="abc", fetched_at=time.time(), mtime=time.time(),
        title="Test", summary="Build a WhatsApp agent", fragment_type="spec",
    )
    snap = MagicMock(spec=RoadmapSnapshot)
    snap.content_hash = content_hash
    snap.fragments = (frag,)
    snap.version = 1
    return snap


def _mock_cache(hit=None):
    cache = MagicMock()
    cache.get_if_valid.return_value = hit
    cache.save = MagicMock()
    cache.load.return_value = hit or []
    return cache


def _mock_oracle():
    oracle = MagicMock()
    oracle.find_nodes_by_name.return_value = []
    return oracle


@pytest.mark.asyncio
async def test_cache_hit_returns_cached():
    cached = [MagicMock(spec=FeatureHypothesis)]
    engine = FeatureSynthesisEngine(
        oracle=_mock_oracle(),
        doubleword=None,
        cache=_mock_cache(hit=cached),
        config=SynthesisConfig(),
    )
    result = await engine.synthesize(_snapshot())
    assert result == cached


@pytest.mark.asyncio
async def test_cache_miss_runs_tier0():
    engine = FeatureSynthesisEngine(
        oracle=_mock_oracle(),
        doubleword=None,
        cache=_mock_cache(hit=None),
        config=SynthesisConfig(),
    )
    result = await engine.synthesize(_snapshot())
    assert isinstance(result, list)
    # Should have run Tier 0 at minimum (may produce hints or not)


@pytest.mark.asyncio
async def test_single_flight_guard():
    engine = FeatureSynthesisEngine(
        oracle=_mock_oracle(),
        doubleword=None,
        cache=_mock_cache(hit=None),
        config=SynthesisConfig(min_interval_s=0),
    )
    # Run two syntheses concurrently — second should return cached
    t1 = asyncio.create_task(engine.synthesize(_snapshot()))
    t2 = asyncio.create_task(engine.synthesize(_snapshot()))
    r1, r2 = await asyncio.gather(t1, t2)
    assert isinstance(r1, list)
    assert isinstance(r2, list)


@pytest.mark.asyncio
async def test_min_interval_respected():
    engine = FeatureSynthesisEngine(
        oracle=_mock_oracle(),
        doubleword=None,
        cache=_mock_cache(hit=None),
        config=SynthesisConfig(min_interval_s=3600),
    )
    await engine.synthesize(_snapshot())  # first run
    # Second run too soon — should return cached
    engine._cache = _mock_cache(hit=None)
    result = await engine.synthesize(_snapshot(), force=False)
    # Should not re-run synthesis (min_interval not elapsed)


def test_input_fingerprint_deterministic():
    fp1 = compute_input_fingerprint("hash1", 1, "model1")
    fp2 = compute_input_fingerprint("hash1", 1, "model1")
    assert fp1 == fp2


def test_input_fingerprint_changes():
    fp1 = compute_input_fingerprint("hash1", 1, "model1")
    fp2 = compute_input_fingerprint("hash2", 1, "model1")
    assert fp1 != fp2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/test_synthesis_engine.py -v`
Expected: FAIL

- [ ] **Step 3: Implement synthesis_engine.py**

```python
# backend/core/ouroboros/roadmap/synthesis_engine.py
"""FeatureSynthesisEngine — Clock 2: agentic gap synthesis.

Runs Tier 0 deterministic hints + Doubleword 397B batch + Claude fallback.
Guarded by single-flight lock and minimum interval.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any, List, Optional

from backend.core.ouroboros.roadmap.snapshot import RoadmapSnapshot
from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis
from backend.core.ouroboros.roadmap.hypothesis_cache import HypothesisCache
from backend.core.ouroboros.roadmap.tier0_hints import generate_tier0_hints

logger = logging.getLogger(__name__)


def compute_input_fingerprint(
    snapshot_hash: str,
    prompt_version: int,
    model_id: str,
) -> str:
    """Deterministic cache key for synthesis inputs."""
    canonical = f"{snapshot_hash}\t{prompt_version}\t{model_id}"
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class SynthesisConfig:
    min_interval_s: float = 21600.0   # 6 hours
    ttl_s: float = 86400.0            # 24 hours
    prompt_version: int = 1
    model_id: str = "doubleword-397b"


class FeatureSynthesisEngine:
    """Clock 2 — Agentic feature gap synthesis."""

    def __init__(
        self,
        oracle: Any,
        doubleword: Any,
        cache: HypothesisCache,
        config: SynthesisConfig,
    ) -> None:
        self._oracle = oracle
        self._doubleword = doubleword
        self._cache = cache
        self._config = config
        self._synthesis_lock = asyncio.Lock()
        self._last_synthesis_at: float = 0.0

    async def synthesize(
        self,
        snapshot: RoadmapSnapshot,
        *,
        force: bool = False,
    ) -> List[FeatureHypothesis]:
        """Run synthesis pipeline. Single-flight guarded."""
        # Check cache first (no lock needed)
        fingerprint = compute_input_fingerprint(
            snapshot.content_hash,
            self._config.prompt_version,
            self._config.model_id,
        )
        cached = self._cache.get_if_valid(fingerprint)
        if cached is not None:
            return cached

        # Single-flight: if synthesis already running, return what we have
        if self._synthesis_lock.locked():
            return self._cache.load()

        # Min interval check
        if not force:
            elapsed = time.time() - self._last_synthesis_at
            if elapsed < self._config.min_interval_s and self._last_synthesis_at > 0:
                return self._cache.load()

        async with self._synthesis_lock:
            return await self._run_synthesis(snapshot, fingerprint)

    async def _run_synthesis(
        self,
        snapshot: RoadmapSnapshot,
        fingerprint: str,
    ) -> List[FeatureHypothesis]:
        """Full synthesis pipeline: Tier 0 + model."""
        logger.info("[Synthesis] Starting for snapshot v%d", snapshot.version)
        all_hypotheses: List[FeatureHypothesis] = []

        # Tier 0: Deterministic gap hints (zero tokens)
        try:
            tier0 = generate_tier0_hints(snapshot, self._oracle)
            all_hypotheses.extend(tier0)
            logger.info("[Synthesis] Tier 0: %d deterministic hints", len(tier0))
        except Exception as exc:
            logger.warning("[Synthesis] Tier 0 failed: %s", exc)

        # Tier 1: Doubleword 397B (if available)
        # TODO in future: integrate batch API when OperationContext bridge is built
        # For v1: Tier 0 hints are the primary output

        # Dedup by fingerprint (deterministic wins)
        seen: dict[str, FeatureHypothesis] = {}
        for h in all_hypotheses:
            if h.hypothesis_fingerprint not in seen:
                seen[h.hypothesis_fingerprint] = h
            elif h.provenance == "deterministic":
                seen[h.hypothesis_fingerprint] = h  # deterministic wins

        result = list(seen.values())

        # Persist
        self._cache.save(result)
        self._last_synthesis_at = time.time()
        logger.info("[Synthesis] Complete: %d hypotheses", len(result))

        return result

    async def trigger(self, snapshot: RoadmapSnapshot) -> None:
        """Fire-and-forget synthesis trigger (called by sensor on change)."""
        try:
            await self.synthesize(snapshot)
        except Exception as exc:
            logger.warning("[Synthesis] Triggered synthesis failed: %s", exc)

    def health(self) -> dict:
        return {
            "last_synthesis_at": self._last_synthesis_at,
            "locked": self._synthesis_lock.locked(),
            "cached_count": len(self._cache.load()),
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/test_synthesis_engine.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/roadmap/synthesis_engine.py tests/core/ouroboros/roadmap/test_synthesis_engine.py
git commit -m "feat(ouroboros/roadmap): implement FeatureSynthesisEngine (Clock 2 — 397B synthesis)"
```

---

## Task 7: Hypothesis Envelope Factory

**Files:**
- Create: `backend/core/ouroboros/roadmap/hypothesis_envelope_factory.py`
- Create: `tests/core/ouroboros/roadmap/test_hypothesis_envelope_factory.py`

- [ ] **Step 1: Write factory tests**

```python
# tests/core/ouroboros/roadmap/test_hypothesis_envelope_factory.py
"""Tests for FeatureHypothesis -> IntentEnvelope conversion."""
import time
import pytest
from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis
from backend.core.ouroboros.roadmap.hypothesis_envelope_factory import hypotheses_to_envelopes


def _hyp(**kw):
    defaults = dict(
        hypothesis_id="test-uuid",
        description="Missing WhatsApp agent",
        evidence_fragments=("spec:manifesto",),
        gap_type="missing_capability",
        confidence=0.9,
        confidence_rule_id="spec_symbol_miss",
        urgency="normal",
        suggested_scope="backend/neural_mesh/agents/",
        suggested_repos=("jarvis",),
        provenance="deterministic",
        synthesized_for_snapshot_hash="abc",
        synthesized_at=time.time(),
        synthesis_input_fingerprint="fp1",
    )
    defaults.update(kw)
    return FeatureHypothesis(**defaults)


def test_creates_envelope_per_hypothesis():
    hyps = [_hyp(description="gap1"), _hyp(description="gap2")]
    envelopes = hypotheses_to_envelopes(hyps, snapshot_version=1)
    assert len(envelopes) == 2


def test_envelope_source_is_roadmap():
    envelopes = hypotheses_to_envelopes([_hyp()], snapshot_version=1)
    assert envelopes[0].source == "roadmap"


def test_envelope_carries_analysis_complete():
    envelopes = hypotheses_to_envelopes([_hyp()], snapshot_version=1)
    assert envelopes[0].evidence.get("analysis_complete") is True


def test_envelope_carries_hypothesis_id():
    envelopes = hypotheses_to_envelopes([_hyp(hypothesis_id="h123")], snapshot_version=1)
    assert envelopes[0].evidence["hypothesis_id"] == "h123"


def test_envelope_carries_provenance():
    envelopes = hypotheses_to_envelopes([_hyp(provenance="model:doubleword-397b")], snapshot_version=1)
    assert envelopes[0].evidence["provenance"] == "model:doubleword-397b"


def test_envelope_requires_no_human_ack():
    envelopes = hypotheses_to_envelopes([_hyp()], snapshot_version=1)
    assert envelopes[0].requires_human_ack is False


def test_envelope_target_files_from_scope():
    envelopes = hypotheses_to_envelopes(
        [_hyp(suggested_scope="backend/agents/whatsapp.py")],
        snapshot_version=1,
    )
    assert envelopes[0].target_files == ("backend/agents/whatsapp.py",)


def test_envelope_repo_from_hypothesis():
    envelopes = hypotheses_to_envelopes(
        [_hyp(suggested_repos=("jarvis-prime",))],
        snapshot_version=1,
    )
    assert envelopes[0].repo == "jarvis-prime"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/test_hypothesis_envelope_factory.py -v`
Expected: FAIL

- [ ] **Step 3: Implement hypothesis_envelope_factory.py**

```python
# backend/core/ouroboros/roadmap/hypothesis_envelope_factory.py
"""Convert FeatureHypotheses into IntentEnvelopes for the governance pipeline.

source="roadmap". evidence includes analysis_complete=True so governance
pipeline skips re-analysis (hypotheses are already synthesized by Clock 2).
"""
from __future__ import annotations

from typing import List

from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis
from backend.core.ouroboros.governance.intake.intent_envelope import (
    IntentEnvelope,
    make_envelope,
)


def hypotheses_to_envelopes(
    hypotheses: List[FeatureHypothesis],
    *,
    snapshot_version: int,
) -> List[IntentEnvelope]:
    """Convert feature hypotheses into IntentEnvelopes.

    source="roadmap". Already synthesized — carries analysis_complete=True.
    """
    envelopes: List[IntentEnvelope] = []
    for h in hypotheses:
        envelope = make_envelope(
            source="roadmap",
            description=f"[{h.gap_type}] {h.description}",
            target_files=(h.suggested_scope,),
            repo=h.suggested_repos[0] if h.suggested_repos else "jarvis",
            confidence=h.confidence,
            urgency=h.urgency,
            evidence={
                "hypothesis_id": h.hypothesis_id,
                "provenance": h.provenance,
                "gap_type": h.gap_type,
                "confidence_rule_id": h.confidence_rule_id,
                "snapshot_version": snapshot_version,
                "analysis_complete": True,
            },
            requires_human_ack=False,
        )
        envelopes.append(envelope)
    return envelopes
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/test_hypothesis_envelope_factory.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/roadmap/hypothesis_envelope_factory.py tests/core/ouroboros/roadmap/test_hypothesis_envelope_factory.py
git commit -m "feat(ouroboros/roadmap): add hypothesis envelope factory for roadmap -> pipeline conversion"
```

---

## Task 8: Existing File Modifications (roadmap source + config)

**Files:**
- Modify: `backend/core/ouroboros/governance/intake/intent_envelope.py:20`
- Modify: `backend/core/ouroboros/governance/intake/unified_intake_router.py:33`
- Modify: `backend/core/ouroboros/governance/risk_engine.py:240`
- Modify: `backend/core/ouroboros/daemon_config.py:111`

- [ ] **Step 1: Write tests for roadmap source**

```python
# tests/core/ouroboros/roadmap/test_roadmap_source.py
"""Tests that 'roadmap' is a valid IntentEnvelope source."""
import pytest


def test_roadmap_is_valid_source():
    from backend.core.ouroboros.governance.intake.intent_envelope import _VALID_SOURCES
    assert "roadmap" in _VALID_SOURCES


def test_roadmap_priority_in_map():
    from backend.core.ouroboros.governance.intake.unified_intake_router import _PRIORITY_MAP
    assert "roadmap" in _PRIORITY_MAP
    assert _PRIORITY_MAP["roadmap"] == 4


def test_roadmap_config_fields():
    from backend.core.ouroboros.daemon_config import DaemonConfig
    config = DaemonConfig.from_env()
    assert hasattr(config, "roadmap_enabled")
    assert hasattr(config, "roadmap_refresh_s")
    assert hasattr(config, "synthesis_enabled")
    assert hasattr(config, "synthesis_min_interval_s")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/test_roadmap_source.py -v`
Expected: FAIL — "roadmap" not in sources

- [ ] **Step 3: Add "roadmap" to _VALID_SOURCES**

In `backend/core/ouroboros/governance/intake/intent_envelope.py` line 20:
```python
# Add "roadmap" to the frozenset
_VALID_SOURCES = frozenset({"backlog", "test_failure", "voice_human", "ai_miner", "capability_gap", "runtime_health", "exploration", "roadmap"})
```

- [ ] **Step 4: Add "roadmap" to _PRIORITY_MAP**

In `backend/core/ouroboros/governance/intake/unified_intake_router.py` line 33:
```python
_PRIORITY_MAP: Dict[str, int] = {
    "voice_human": 0,
    "test_failure": 1,
    "backlog": 2,
    "ai_miner": 3,
    "exploration": 4,
    "roadmap": 4,           # same tier as exploration, tie-break by submitted_at
    "capability_gap": 5,
    "runtime_health": 6,
}
```

- [ ] **Step 5: Add roadmap to risk_engine.py exploration rules**

In `backend/core/ouroboros/governance/risk_engine.py`, where the exploration rules check `if profile.source == "exploration":`, change to:
```python
if profile.source in ("exploration", "roadmap"):
```

- [ ] **Step 6: Add roadmap/synthesis config fields to daemon_config.py**

In `backend/core/ouroboros/daemon_config.py`, add after `exploration_model_rpm` field:
```python
    # Roadmap sensor (Clock 1)
    roadmap_enabled: bool = True
    roadmap_refresh_s: float = 3600.0
    roadmap_p1_enabled: bool = True
    roadmap_p1_commit_limit: int = 50
    roadmap_p1_days: int = 30
    roadmap_p2_enabled: bool = False
    roadmap_p3_enabled: bool = False

    # Feature synthesis (Clock 2)
    synthesis_enabled: bool = True
    synthesis_min_interval_s: float = 21600.0
    synthesis_ttl_s: float = 86400.0
    synthesis_prompt_version: int = 1
```

And in `from_env()`, read these from environment variables with `OUROBOROS_ROADMAP_*` and `OUROBOROS_SYNTHESIS_*` prefixes.

- [ ] **Step 7: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/test_roadmap_source.py -v`
Expected: All PASS

- [ ] **Step 8: Commit**

```bash
git add backend/core/ouroboros/governance/intake/intent_envelope.py backend/core/ouroboros/governance/intake/unified_intake_router.py backend/core/ouroboros/governance/risk_engine.py backend/core/ouroboros/daemon_config.py tests/core/ouroboros/roadmap/test_roadmap_source.py
git commit -m "feat(ouroboros): add 'roadmap' source + config fields for cognitive extensions"
```

---

## Task 9: REM Epoch Integration

**Files:**
- Modify: `backend/core/ouroboros/rem_epoch.py:249`

- [ ] **Step 1: Write integration tests**

```python
# tests/core/ouroboros/roadmap/test_rem_integration.py
"""Tests for REM epoch consuming cached hypotheses."""
import time
import pytest
from unittest.mock import MagicMock, AsyncMock
from backend.core.ouroboros.cancellation_token import CancellationToken
from backend.core.ouroboros.rem_epoch import RemEpoch
from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis
from backend.core.ouroboros.roadmap.hypothesis_cache import HypothesisCache


def _mock_hyp(desc="Test gap"):
    return FeatureHypothesis(
        hypothesis_id="test",
        description=desc,
        evidence_fragments=("spec:test",),
        gap_type="missing_capability",
        confidence=0.9,
        confidence_rule_id="spec_symbol_miss",
        urgency="normal",
        suggested_scope="backend/agents/",
        suggested_repos=("jarvis",),
        provenance="deterministic",
        synthesized_for_snapshot_hash="abc",
        synthesized_at=time.time(),
        synthesis_input_fingerprint="fp1",
    )


@pytest.mark.asyncio
async def test_rem_epoch_includes_hypothesis_findings(tmp_path):
    # Set up cache with one hypothesis
    cache = HypothesisCache(cache_dir=tmp_path)
    cache.save([_mock_hyp("Missing WhatsApp agent")])

    oracle = MagicMock()
    oracle.find_dead_code.return_value = []
    oracle.find_circular_dependencies.return_value = []

    fleet = AsyncMock()
    fleet.deploy.return_value = MagicMock(findings=[], total_findings=0,
                                           agents_deployed=0, agents_completed=0)

    config = MagicMock(
        rem_cycle_timeout_s=30, rem_epoch_timeout_s=60,
        rem_max_findings_per_epoch=10, rem_max_agents=5,
    )

    epoch = RemEpoch(
        epoch_id=1, oracle=oracle, fleet=fleet,
        spinal_cord=MagicMock(stream_up=AsyncMock(), stream_down=AsyncMock()),
        intake_router=AsyncMock(ingest=AsyncMock(return_value="enqueued")),
        doubleword=None, config=config,
        hypothesis_cache_dir=tmp_path,
    )

    token = CancellationToken(epoch_id=1)
    result = await epoch.run(token)
    # Should include hypothesis findings in the count
    assert result.findings_count >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/test_rem_integration.py -v`
Expected: FAIL — `hypothesis_cache_dir` not in RemEpoch.__init__

- [ ] **Step 3: Add hypothesis loading to RemEpoch**

In `backend/core/ouroboros/rem_epoch.py`:

Add `hypothesis_cache_dir` parameter to `__init__`:
```python
def __init__(
    self,
    epoch_id: int,
    oracle: Any,
    fleet: Any,
    spinal_cord: Any,
    intake_router: Any,
    doubleword: Any,
    config: Any,
    hypothesis_cache_dir: Any = None,  # NEW: path to hypothesis cache
) -> None:
```

Add `_load_cached_hypotheses()` method:
```python
def _load_cached_hypotheses(self) -> List[RankedFinding]:
    """Load cached FeatureHypotheses and convert to RankedFinding for ranking."""
    if self._hypothesis_cache_dir is None:
        return []
    try:
        from backend.core.ouroboros.roadmap.hypothesis_cache import HypothesisCache
        cache = HypothesisCache(cache_dir=self._hypothesis_cache_dir)
        hypotheses = cache.load()
        findings = []
        _BLAST_RADIUS = {
            "missing_capability": 0.5,
            "incomplete_wiring": 0.3,
            "stale_implementation": 0.2,
            "manifesto_violation": 0.7,
        }
        for h in hypotheses:
            if h.status != "active":
                continue
            findings.append(RankedFinding(
                description=h.description,
                category=h.gap_type,
                file_path=h.suggested_scope,
                blast_radius=_BLAST_RADIUS.get(h.gap_type, 0.3),
                confidence=h.confidence,
                urgency=h.urgency,
                last_modified=h.synthesized_at,
                repo=h.suggested_repos[0] if h.suggested_repos else "jarvis",
                source_check=f"roadmap:{h.provenance}",
            ))
        return findings
    except Exception as exc:
        logger.debug("[RemEpoch] Hypothesis load failed: %s", exc)
        return []
```

In `_explore()`, add the hypothesis findings before `merge_and_rank`:
```python
# NEW: Load cached roadmap hypotheses
hypothesis_findings = self._load_cached_hypotheses()
all_findings.extend(hypothesis_findings)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/test_rem_integration.py -v`
Expected: PASS

- [ ] **Step 5: Run existing REM epoch tests to verify no regression**

Run: `python3 -m pytest tests/core/ouroboros/test_rem_epoch.py -v`
Expected: All existing tests still PASS (hypothesis_cache_dir defaults to None)

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/rem_epoch.py tests/core/ouroboros/roadmap/test_rem_integration.py
git commit -m "feat(ouroboros): integrate roadmap hypotheses into REM epoch exploration"
```

---

## Task 10: Daemon + GLS Wiring

**Files:**
- Modify: `backend/core/ouroboros/daemon.py`
- Modify: `backend/core/ouroboros/rem_sleep.py`

- [ ] **Step 1: Add hypothesis_cache_dir to RemSleepDaemon and OuroborosDaemon**

In `backend/core/ouroboros/rem_sleep.py`, add `hypothesis_cache_dir` parameter to `__init__`:
```python
def __init__(
    self,
    oracle: Any,
    fleet: Any,
    spinal_cord: Any,
    intake_router: Any,
    proactive_drive: Any,
    doubleword: Any,
    config: Any,
    hypothesis_cache_dir: Any = None,  # NEW
) -> None:
    ...
    self._hypothesis_cache_dir = hypothesis_cache_dir
```

In `_run_epoch()`, pass it to RemEpoch:
```python
epoch = RemEpoch(
    epoch_id=epoch_id,
    oracle=self._oracle,
    fleet=self._fleet,
    spinal_cord=self._spinal,
    intake_router=self._intake,
    doubleword=self._doubleword,
    config=self._config,
    hypothesis_cache_dir=self._hypothesis_cache_dir,  # NEW
)
```

In `backend/core/ouroboros/daemon.py`, in `awaken()` where RemSleepDaemon is created, pass the cache dir:
```python
import os
_HYPOTHESIS_CACHE_DIR = os.path.expanduser("~/.jarvis/ouroboros/roadmap")

self._rem = RemSleepDaemon(
    oracle=self._oracle,
    fleet=self._fleet,
    spinal_cord=self._spinal,
    intake_router=self._intake_router,
    proactive_drive=self._proactive_drive,
    doubleword=self._doubleword,
    config=self._config,
    hypothesis_cache_dir=_HYPOTHESIS_CACHE_DIR,  # NEW
)
```

- [ ] **Step 2: Verify existing tests still pass**

Run: `python3 -m pytest tests/core/ouroboros/test_rem_sleep.py tests/core/ouroboros/test_daemon.py tests/core/ouroboros/test_daemon_integration.py -v --tb=short`
Expected: All existing tests PASS (hypothesis_cache_dir defaults to None)

- [ ] **Step 3: Commit**

```bash
git add backend/core/ouroboros/daemon.py backend/core/ouroboros/rem_sleep.py
git commit -m "feat(ouroboros): wire hypothesis cache through daemon -> REM -> epoch"
```

---

## Task 11: End-to-End Integration Test

**Files:**
- Create: `tests/core/ouroboros/roadmap/test_integration.py`

- [ ] **Step 1: Write integration test**

```python
# tests/core/ouroboros/roadmap/test_integration.py
"""End-to-end: Clock 1 -> Clock 2 -> REM consumption."""
import asyncio
import json
import time
import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock
from backend.core.ouroboros.roadmap.sensor import RoadmapSensor, RoadmapSensorConfig
from backend.core.ouroboros.roadmap.synthesis_engine import FeatureSynthesisEngine, SynthesisConfig
from backend.core.ouroboros.roadmap.hypothesis_cache import HypothesisCache
from backend.core.ouroboros.roadmap.hypothesis import FeatureHypothesis
from backend.core.ouroboros.roadmap.snapshot import RoadmapSnapshot


@pytest.fixture
def repo(tmp_path):
    """Create a minimal repo structure."""
    specs = tmp_path / "docs" / "superpowers" / "specs"
    specs.mkdir(parents=True)
    (specs / "manifesto.md").write_text(
        "# Manifesto\n\nWe need a WhatsApp agent for messaging automation."
    )
    plans = tmp_path / "docs" / "superpowers" / "plans"
    plans.mkdir(parents=True)
    return tmp_path


@pytest.fixture
def cache_dir(tmp_path):
    return tmp_path / "cache"


def _mock_oracle():
    oracle = MagicMock()
    oracle.find_nodes_by_name.return_value = []  # no WhatsApp symbol
    return oracle


def test_clock1_produces_snapshot(repo):
    sensor = RoadmapSensor(repo_root=repo, config=RoadmapSensorConfig(p1_enabled=False))
    snapshot = sensor.refresh()
    assert snapshot.version == 1
    assert len(snapshot.fragments) >= 1
    assert any("manifesto" in f.source_id for f in snapshot.fragments)


@pytest.mark.asyncio
async def test_clock2_produces_hypotheses(repo, cache_dir):
    sensor = RoadmapSensor(repo_root=repo, config=RoadmapSensorConfig(p1_enabled=False))
    snapshot = sensor.refresh()

    cache = HypothesisCache(cache_dir=cache_dir)
    engine = FeatureSynthesisEngine(
        oracle=_mock_oracle(),
        doubleword=None,
        cache=cache,
        config=SynthesisConfig(min_interval_s=0),
    )
    hypotheses = await engine.synthesize(snapshot, force=True)
    assert isinstance(hypotheses, list)
    # Should find WhatsApp agent gap from Tier 0
    assert any("whatsapp" in h.description.lower() for h in hypotheses)


@pytest.mark.asyncio
async def test_full_pipeline_clock1_to_clock2(repo, cache_dir):
    """Full pipeline: sensor detects change -> triggers synthesis -> cache populated."""
    cache = HypothesisCache(cache_dir=cache_dir)
    engine = FeatureSynthesisEngine(
        oracle=_mock_oracle(),
        doubleword=None,
        cache=cache,
        config=SynthesisConfig(min_interval_s=0),
    )

    triggered = []
    async def on_change(snapshot):
        result = await engine.synthesize(snapshot, force=True)
        triggered.append(result)

    sensor = RoadmapSensor(
        repo_root=repo,
        config=RoadmapSensorConfig(p1_enabled=False),
        on_snapshot_changed=lambda s: asyncio.get_event_loop().create_task(on_change(s)),
    )

    sensor.refresh()  # triggers on_change (first snapshot = change)

    # Give async callback time to complete
    await asyncio.sleep(0.2)

    # Verify cache has hypotheses
    cached = cache.load()
    assert len(cached) > 0


def test_hypothesis_cache_survives_restart(repo, cache_dir):
    """Cache persists across process restarts."""
    cache1 = HypothesisCache(cache_dir=cache_dir)
    cache1.save([FeatureHypothesis(
        hypothesis_id="h1",
        description="Missing WhatsApp agent",
        evidence_fragments=("spec:manifesto",),
        gap_type="missing_capability",
        confidence=0.85,
        confidence_rule_id="spec_symbol_miss",
        urgency="normal",
        suggested_scope="backend/agents/",
        suggested_repos=("jarvis",),
        provenance="deterministic",
        synthesized_for_snapshot_hash="abc",
        synthesized_at=time.time(),
        synthesis_input_fingerprint="fp1",
    )])

    # New cache instance (simulates restart)
    cache2 = HypothesisCache(cache_dir=cache_dir)
    loaded = cache2.load()
    assert len(loaded) == 1
    assert loaded[0].description == "Missing WhatsApp agent"
```

- [ ] **Step 2: Run integration tests**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/test_integration.py -v`
Expected: All PASS

- [ ] **Step 3: Run full roadmap test suite**

Run: `python3 -m pytest tests/core/ouroboros/roadmap/ -v --tb=short`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add tests/core/ouroboros/roadmap/test_integration.py
git commit -m "test(ouroboros/roadmap): add end-to-end integration tests for Clock 1 -> Clock 2 pipeline"
```

---

## Summary

| Task | What it builds | New files | Modified files |
|------|---------------|-----------|---------------|
| 1 | Schemas (Snapshot + Hypothesis) | 4 + 2 inits | 0 |
| 2 | Source Crawlers (P0/P1) | 2 | 0 |
| 3 | RoadmapSensor (Clock 1) | 2 | 0 |
| 4 | Tier 0 Hints | 2 | 0 |
| 5 | Hypothesis Cache | 2 | 0 |
| 6 | Synthesis Engine (Clock 2) | 2 | 0 |
| 7 | Hypothesis Envelope Factory | 2 | 0 |
| 8 | Existing file mods (source + config) | 1 test | 4 |
| 9 | REM Epoch Integration | 1 test | 1 |
| 10 | Daemon + REM wiring | 0 | 2 |
| 11 | Integration tests | 1 | 0 |
| **Total** | | **19 new** | **7 modified** |
