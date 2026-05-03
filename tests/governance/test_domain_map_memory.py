"""DomainMapMemory Slice 3 -- regression spine.

Pins the cross-session per-centroid_hash8 entry store +
DomainMapEntry frozen dataclass + idempotent merge logic +
atomic write + cross-process flock discipline.

Coverage:
  * Master flag asymmetric env semantics (default false until
    Slice 5)
  * Env-knob clamping (files_cap / role_max_chars / lock_timeout)
  * DomainMapEntry default + frozen guard + to_dict round-trip
  * DomainMapEntry.from_dict defensive parse (5 corruption modes)
  * _is_valid_hash8 boundary cases (empty / non-alnum / too-long
    / valid)
  * lookup_by_centroid_hash8: hit / miss / corrupt JSON / missing
    dir / master-off / invalid hash / read-error
  * list_all: empty / multiple sorted by last_updated_at desc /
    skips corrupt files / missing dir / master-off
  * record_exploration: master-off short-circuit / first call
    creates / second call merges
  * Merge semantics:
    - theme_label: caller-wins-if-non-empty / preserved otherwise
    - discovered_files: dedup-preserving-order union (new
      prepended)
    - discovered_files: cap honored
    - architectural_role: caller-wins-if-non-empty /
      preserved-on-empty (don't clobber known role)
    - confidence: monotonic max
    - cluster_id: caller-wins-if-non-negative
    - exploration_count: increments per call
    - populated_by_op_id: caller-wins-if-non-empty
  * Atomic write: tempfile cleanup on failure / partial-write
    safety / serialization failure returns False
  * Cross-process flock contract: concurrent record_exploration
    on same hash from threads converges (no lost updates)
  * clear() test helper removes entries + lock files
  * Singleton lifecycle (get_default_store / reset_default_store)
  * Schema version pin
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

import pytest

from backend.core.ouroboros.governance.domain_map_memory import (
    DOMAIN_MAP_SCHEMA_VERSION,
    DomainMapEntry,
    DomainMapStore,
    _is_valid_hash8,
    domain_map_enabled,
    domain_map_files_cap,
    domain_map_lock_timeout_s,
    domain_map_role_max_chars,
    get_default_store,
    reset_default_store,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in (
        "JARVIS_DOMAIN_MAP_ENABLED",
        "JARVIS_DOMAIN_MAP_FILES_CAP",
        "JARVIS_DOMAIN_MAP_ROLE_MAX_CHARS",
        "JARVIS_DOMAIN_MAP_LOCK_TIMEOUT_S",
    ):
        monkeypatch.delenv(var, raising=False)
    yield
    reset_default_store()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DOMAIN_MAP_ENABLED", "true")
    return DomainMapStore(project_root=tmp_path)


# ---------------------------------------------------------------------------
# Schema version + master flag
# ---------------------------------------------------------------------------


class TestSchemaVersion:
    def test_constant(self):
        assert DOMAIN_MAP_SCHEMA_VERSION == "domain_map.v1"


class TestMasterFlag:
    def test_default_true_post_graduation(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_DOMAIN_MAP_ENABLED", raising=False,
        )
        assert domain_map_enabled() is True

    def test_empty_is_default(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DOMAIN_MAP_ENABLED", "")
        assert domain_map_enabled() is True

    def test_whitespace_is_default(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DOMAIN_MAP_ENABLED", "   ")
        assert domain_map_enabled() is True

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "On"])
    def test_truthy(self, monkeypatch, raw):
        monkeypatch.setenv("JARVIS_DOMAIN_MAP_ENABLED", raw)
        assert domain_map_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", "garbage"])
    def test_falsy(self, monkeypatch, raw):
        monkeypatch.setenv("JARVIS_DOMAIN_MAP_ENABLED", raw)
        assert domain_map_enabled() is False


class TestEnvKnobs:
    def test_files_cap_default(self):
        assert domain_map_files_cap() == 64

    def test_files_cap_floor(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DOMAIN_MAP_FILES_CAP", "0")
        assert domain_map_files_cap() == 1

    def test_files_cap_ceiling(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DOMAIN_MAP_FILES_CAP", "9999")
        assert domain_map_files_cap() == 256

    def test_files_cap_garbage(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DOMAIN_MAP_FILES_CAP", "abc")
        assert domain_map_files_cap() == 64

    def test_role_max_chars_default(self):
        assert domain_map_role_max_chars() == 500

    def test_role_max_chars_floor(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DOMAIN_MAP_ROLE_MAX_CHARS", "0")
        assert domain_map_role_max_chars() == 32

    def test_role_max_chars_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_DOMAIN_MAP_ROLE_MAX_CHARS", "999999",
        )
        assert domain_map_role_max_chars() == 4000

    def test_lock_timeout_default(self):
        assert domain_map_lock_timeout_s() == 5.0

    def test_lock_timeout_floor(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DOMAIN_MAP_LOCK_TIMEOUT_S", "0.0")
        assert domain_map_lock_timeout_s() == 0.1

    def test_lock_timeout_ceiling(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DOMAIN_MAP_LOCK_TIMEOUT_S", "100")
        assert domain_map_lock_timeout_s() == 30.0


# ---------------------------------------------------------------------------
# Hash validation
# ---------------------------------------------------------------------------


class TestHashValidation:
    @pytest.mark.parametrize("hash8", [
        "abc12345", "DEADBEEF", "abcd0000ef", "x", "0", "abc",
    ])
    def test_valid(self, hash8):
        assert _is_valid_hash8(hash8) is True

    @pytest.mark.parametrize("hash8", [
        "", "   ", "abc-123", "abc_123", "abc.123", "abc 123",
        "a/b", "a\\b", "../etc/passwd",
    ])
    def test_invalid_non_alnum(self, hash8):
        assert _is_valid_hash8(hash8) is False

    def test_invalid_too_long(self):
        assert _is_valid_hash8("a" * 65) is False

    def test_valid_at_max_length(self):
        assert _is_valid_hash8("a" * 64) is True

    def test_non_string_invalid(self):
        assert _is_valid_hash8(None) is False  # type: ignore[arg-type]
        assert _is_valid_hash8(42) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# DomainMapEntry frozen dataclass
# ---------------------------------------------------------------------------


class TestEntryDefaults:
    def test_minimal_construction(self):
        entry = DomainMapEntry(centroid_hash8="abc12345")
        assert entry.centroid_hash8 == "abc12345"
        assert entry.cluster_id == -1
        assert entry.theme_label == ""
        assert entry.discovered_files == ()
        assert entry.architectural_role == ""
        assert entry.confidence == 0.0
        assert entry.last_updated_at == 0.0
        assert entry.populated_by_op_id == ""
        assert entry.exploration_count == 0
        assert entry.schema_version == DOMAIN_MAP_SCHEMA_VERSION

    def test_frozen(self):
        entry = DomainMapEntry(centroid_hash8="abc12345")
        with pytest.raises(FrozenInstanceError):
            entry.confidence = 0.5  # type: ignore[misc]


class TestEntryToDictRoundTrip:
    def test_full(self):
        entry = DomainMapEntry(
            centroid_hash8="abc12345",
            cluster_id=3,
            theme_label="voice biometric",
            discovered_files=("voice/auth.py", "voice/util.py"),
            architectural_role="primitive for voice ID",
            confidence=0.85,
            last_updated_at=1700000000.0,
            populated_by_op_id="op-42",
            exploration_count=2,
        )
        d = entry.to_dict()
        recovered = DomainMapEntry.from_dict(d)
        assert recovered == entry

    def test_to_dict_serializable(self):
        entry = DomainMapEntry(centroid_hash8="abc12345")
        # Must round-trip through json without raising.
        json.dumps(entry.to_dict())


class TestEntryFromDictDefensive:
    def test_missing_hash_returns_none(self):
        assert DomainMapEntry.from_dict({}) is None
        assert DomainMapEntry.from_dict({"centroid_hash8": ""}) is None

    def test_non_mapping_returns_none(self):
        assert DomainMapEntry.from_dict([1, 2, 3]) is None  # type: ignore[arg-type]
        assert DomainMapEntry.from_dict("oops") is None  # type: ignore[arg-type]
        assert DomainMapEntry.from_dict(None) is None  # type: ignore[arg-type]

    def test_garbage_files_dropped(self):
        entry = DomainMapEntry.from_dict({
            "centroid_hash8": "abc12345",
            "discovered_files": ["ok.py", None, 42, "", "another.py"],
        })
        assert entry is not None
        assert entry.discovered_files == ("ok.py", "another.py")

    def test_non_list_files_treated_as_empty(self):
        entry = DomainMapEntry.from_dict({
            "centroid_hash8": "abc12345",
            "discovered_files": "oops",
        })
        assert entry is not None
        assert entry.discovered_files == ()

    def test_invalid_numeric_returns_none(self):
        # ValueError on int conversion
        assert DomainMapEntry.from_dict({
            "centroid_hash8": "abc12345",
            "exploration_count": "not-an-int",
        }) is None


# ---------------------------------------------------------------------------
# lookup_by_centroid_hash8
# ---------------------------------------------------------------------------


class TestLookup:
    def test_master_off_returns_none(self, tmp_path, monkeypatch):
        # Post-graduation default is true; explicit "false" is the
        # operator escape hatch.
        monkeypatch.setenv("JARVIS_DOMAIN_MAP_ENABLED", "false")
        s = DomainMapStore(project_root=tmp_path)
        # Even if there's a file on disk, master-off short-circuits.
        s._dir.mkdir(parents=True, exist_ok=True)
        (s._dir / "abc12345.json").write_text(
            json.dumps({"centroid_hash8": "abc12345"})
        )
        assert s.lookup_by_centroid_hash8("abc12345") is None

    def test_invalid_hash_returns_none(self, store):
        assert store.lookup_by_centroid_hash8("") is None
        assert store.lookup_by_centroid_hash8("../etc") is None
        assert store.lookup_by_centroid_hash8("a" * 65) is None

    def test_missing_file_returns_none(self, store):
        assert store.lookup_by_centroid_hash8("nonexistent") is None

    def test_corrupt_json_returns_none(self, store):
        store._dir.mkdir(parents=True, exist_ok=True)
        (store._dir / "abc12345.json").write_text("not json {")
        assert store.lookup_by_centroid_hash8("abc12345") is None

    def test_corrupt_schema_returns_none(self, store):
        store._dir.mkdir(parents=True, exist_ok=True)
        # Valid JSON but missing centroid_hash8.
        (store._dir / "abc12345.json").write_text(
            json.dumps({"foo": "bar"})
        )
        assert store.lookup_by_centroid_hash8("abc12345") is None

    def test_round_trip(self, store):
        entry = store.record_exploration(
            "abc12345",
            theme_label="voice biometric",
            discovered_files=("voice/auth.py",),
        )
        assert entry is not None
        recovered = store.lookup_by_centroid_hash8("abc12345")
        assert recovered is not None
        assert recovered.theme_label == "voice biometric"
        assert recovered.discovered_files == ("voice/auth.py",)


# ---------------------------------------------------------------------------
# list_all
# ---------------------------------------------------------------------------


class TestListAll:
    def test_master_off_returns_empty(self, tmp_path, monkeypatch):
        # Post-graduation default is true; explicit "false" is the
        # operator escape hatch.
        monkeypatch.setenv("JARVIS_DOMAIN_MAP_ENABLED", "false")
        s = DomainMapStore(project_root=tmp_path)
        s._dir.mkdir(parents=True, exist_ok=True)
        (s._dir / "abc12345.json").write_text(
            json.dumps({"centroid_hash8": "abc12345"})
        )
        assert s.list_all() == []

    def test_missing_dir_returns_empty(self, store):
        # store._dir was never created
        assert store.list_all() == []

    def test_empty_dir_returns_empty(self, store):
        store._dir.mkdir(parents=True, exist_ok=True)
        assert store.list_all() == []

    def test_sorted_by_last_updated_desc(self, store):
        store.record_exploration("aaaa1111")
        time.sleep(0.01)
        store.record_exploration("bbbb2222")
        time.sleep(0.01)
        store.record_exploration("cccc3333")
        out = store.list_all()
        assert [e.centroid_hash8 for e in out] == [
            "cccc3333", "bbbb2222", "aaaa1111",
        ]

    def test_skips_corrupt_files(self, store):
        store.record_exploration("aaaa1111")
        # Plant a corrupt file.
        (store._dir / "bbbb2222.json").write_text("not json {")
        out = store.list_all()
        assert len(out) == 1
        assert out[0].centroid_hash8 == "aaaa1111"


# ---------------------------------------------------------------------------
# record_exploration -- creation + merge semantics
# ---------------------------------------------------------------------------


class TestRecordCreate:
    def test_master_off_returns_none(self, tmp_path, monkeypatch):
        # Post-graduation default is true; explicit "false" is the
        # operator escape hatch.
        monkeypatch.setenv("JARVIS_DOMAIN_MAP_ENABLED", "false")
        s = DomainMapStore(project_root=tmp_path)
        out = s.record_exploration(
            "abc12345", theme_label="x",
        )
        assert out is None
        # No file written.
        assert not (s._dir / "abc12345.json").exists()

    def test_invalid_hash_returns_none(self, store):
        assert store.record_exploration("") is None
        assert store.record_exploration("../etc") is None

    def test_first_call_creates_entry(self, store):
        entry = store.record_exploration(
            "abc12345",
            theme_label="voice biometric",
            discovered_files=("voice/auth.py", "voice/util.py"),
            architectural_role="primitive",
            confidence=0.7,
            cluster_id=3,
            op_id="op-1",
        )
        assert entry is not None
        assert entry.centroid_hash8 == "abc12345"
        assert entry.theme_label == "voice biometric"
        assert entry.discovered_files == (
            "voice/auth.py", "voice/util.py",
        )
        assert entry.architectural_role == "primitive"
        assert entry.confidence == 0.7
        assert entry.cluster_id == 3
        assert entry.populated_by_op_id == "op-1"
        assert entry.exploration_count == 1
        assert entry.last_updated_at > 0

    def test_creates_directory_lazy(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_DOMAIN_MAP_ENABLED", "true")
        s = DomainMapStore(project_root=tmp_path)
        # _dir doesn't exist yet
        assert not s._dir.exists()
        s.record_exploration("abc12345")
        # _dir created on first write
        assert s._dir.is_dir()


class TestRecordMerge:
    def test_exploration_count_increments(self, store):
        store.record_exploration("abc12345")
        store.record_exploration("abc12345")
        e = store.record_exploration("abc12345")
        assert e is not None
        assert e.exploration_count == 3

    def test_files_dedup_preserving_order(self, store):
        store.record_exploration(
            "abc12345",
            discovered_files=("a.py", "b.py"),
        )
        e = store.record_exploration(
            "abc12345",
            discovered_files=("c.py", "a.py", "d.py"),
        )
        assert e is not None
        # New files prepended (deduped); old files preserved.
        assert e.discovered_files == ("c.py", "a.py", "d.py", "b.py")

    def test_files_cap_honored(self, store, monkeypatch):
        monkeypatch.setenv("JARVIS_DOMAIN_MAP_FILES_CAP", "3")
        e = store.record_exploration(
            "abc12345",
            discovered_files=tuple(f"f{i}.py" for i in range(10)),
        )
        assert e is not None
        assert len(e.discovered_files) == 3

    def test_theme_label_caller_wins_if_nonempty(self, store):
        store.record_exploration(
            "abc12345", theme_label="initial",
        )
        e = store.record_exploration(
            "abc12345", theme_label="updated",
        )
        assert e.theme_label == "updated"

    def test_theme_label_preserved_on_empty_caller(self, store):
        store.record_exploration(
            "abc12345", theme_label="initial",
        )
        e = store.record_exploration("abc12345")  # empty theme
        assert e.theme_label == "initial"

    def test_role_caller_wins_if_nonempty(self, store):
        store.record_exploration(
            "abc12345", architectural_role="initial role",
        )
        e = store.record_exploration(
            "abc12345", architectural_role="updated role",
        )
        assert e.architectural_role == "updated role"

    def test_role_preserved_on_empty_caller(self, store):
        store.record_exploration(
            "abc12345", architectural_role="known role",
        )
        # Slice 4 may invoke without role inference -- existing
        # role should not be clobbered by the empty new value.
        e = store.record_exploration("abc12345")
        assert e.architectural_role == "known role"

    def test_role_capped(self, store, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_DOMAIN_MAP_ROLE_MAX_CHARS", "32",
        )
        e = store.record_exploration(
            "abc12345", architectural_role="x" * 1000,
        )
        assert e is not None
        assert len(e.architectural_role) == 32

    def test_confidence_monotonic_max(self, store):
        store.record_exploration("abc12345", confidence=0.6)
        e1 = store.record_exploration("abc12345", confidence=0.3)
        # 0.3 < 0.6 -> existing 0.6 preserved
        assert e1.confidence == 0.6
        e2 = store.record_exploration("abc12345", confidence=0.9)
        # 0.9 > 0.6 -> updated
        assert e2.confidence == 0.9

    def test_confidence_clamped_0_to_1(self, store):
        e1 = store.record_exploration("abc12345", confidence=-5.0)
        assert e1.confidence == 0.0
        e2 = store.record_exploration("bbbb2222", confidence=99.0)
        assert e2.confidence == 1.0

    def test_cluster_id_caller_wins_if_nonneg(self, store):
        store.record_exploration("abc12345", cluster_id=3)
        e = store.record_exploration("abc12345", cluster_id=7)
        assert e.cluster_id == 7

    def test_cluster_id_preserved_when_caller_negative(self, store):
        store.record_exploration("abc12345", cluster_id=3)
        e = store.record_exploration("abc12345", cluster_id=-1)
        assert e.cluster_id == 3

    def test_op_id_caller_wins_if_nonempty(self, store):
        store.record_exploration("abc12345", op_id="op-1")
        e = store.record_exploration("abc12345", op_id="op-2")
        assert e.populated_by_op_id == "op-2"

    def test_op_id_preserved_on_empty_caller(self, store):
        store.record_exploration("abc12345", op_id="op-1")
        e = store.record_exploration("abc12345")
        assert e.populated_by_op_id == "op-1"

    def test_existing_corrupt_file_treated_as_no_existing(self, store):
        store._dir.mkdir(parents=True, exist_ok=True)
        (store._dir / "abc12345.json").write_text("not json {")
        # Should overwrite with fresh entry (existing corrupt -> None).
        e = store.record_exploration(
            "abc12345", theme_label="fresh",
        )
        assert e is not None
        assert e.theme_label == "fresh"
        assert e.exploration_count == 1


# ---------------------------------------------------------------------------
# Atomic write / persistence
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_round_trip_via_disk(self, store):
        entry = store.record_exploration(
            "abc12345",
            theme_label="x",
            discovered_files=("a.py",),
        )
        path = store._dir / "abc12345.json"
        assert path.exists()
        # Re-read raw and verify it deserializes correctly.
        data = json.loads(path.read_text())
        assert data["centroid_hash8"] == "abc12345"
        assert data["theme_label"] == "x"
        assert data["discovered_files"] == ["a.py"]

    def test_no_orphan_tempfile_on_success(self, store):
        store.record_exploration("abc12345")
        # No .tmp files left over.
        tmps = list(store._dir.glob("*.tmp"))
        assert tmps == []

    def test_atomic_write_failure_returns_none(self, store):
        # Simulate atomic-write failure by patching os.replace.
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            out = store.record_exploration("abc12345")
        assert out is None


# ---------------------------------------------------------------------------
# clear() test helper
# ---------------------------------------------------------------------------


class TestClear:
    def test_removes_entry_files(self, store):
        store.record_exploration("aaaa1111")
        store.record_exploration("bbbb2222")
        store.record_exploration("cccc3333")
        n = store.clear()
        assert n == 3
        assert store.list_all() == []

    def test_idempotent_on_empty_dir(self, store):
        assert store.clear() == 0

    def test_missing_dir_returns_zero(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_DOMAIN_MAP_ENABLED", "true")
        s = DomainMapStore(project_root=tmp_path)
        # _dir not created
        assert s.clear() == 0


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_first_call_no_root_returns_none(self):
        reset_default_store()
        assert get_default_store() is None

    def test_first_call_with_root_initialises(self, tmp_path):
        reset_default_store()
        s = get_default_store(tmp_path)
        assert s is not None

    def test_subsequent_calls_return_same(self, tmp_path):
        reset_default_store()
        s1 = get_default_store(tmp_path)
        s2 = get_default_store()
        s3 = get_default_store(tmp_path)
        assert s1 is s2 is s3


# ---------------------------------------------------------------------------
# Cross-process flock contract -- threaded stress
# ---------------------------------------------------------------------------


class TestConcurrencyConvergence:
    def test_concurrent_record_exploration_converges(self, store):
        """20 threads each call record_exploration on the same
        centroid_hash8 with distinct files. Final state must
        contain ALL contributions (no lost updates) and
        exploration_count must equal thread count."""
        N = 20

        def _writer(idx: int):
            store.record_exploration(
                "abc12345",
                discovered_files=(f"contrib_{idx}.py",),
            )

        threads = [
            threading.Thread(target=_writer, args=(i,))
            for i in range(N)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final = store.lookup_by_centroid_hash8("abc12345")
        assert final is not None
        assert final.exploration_count == N
        contributed = set(final.discovered_files)
        for i in range(N):
            assert f"contrib_{i}.py" in contributed, (
                f"lost update: contrib_{i}.py missing"
            )
