"""
Tests for backend.loading_server.trinity_heartbeat

Covers HeartbeatData (from_dict factory) and TrinityHeartbeatReader
(cached reads, parallel reads, wait polling, write, health mapping).

asyncio_mode = auto in pytest.ini -- no @pytest.mark.asyncio needed.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from backend.loading_server.trinity_heartbeat import (
    HeartbeatData,
    TrinityHeartbeatReader,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_jarvis_home(tmp_path: Path) -> Path:
    """Create a temporary ~/.jarvis/ tree with trinity/components/ dir."""
    jarvis_home = tmp_path / ".jarvis"
    components_dir = jarvis_home / "trinity" / "components"
    components_dir.mkdir(parents=True)
    return jarvis_home


@pytest.fixture()
def heartbeat_writer(tmp_jarvis_home: Path):
    """Return a helper that writes a heartbeat JSON file for a component."""

    def _write(component: str, data: Dict[str, Any]) -> Path:
        path = tmp_jarvis_home / "trinity" / "components" / f"{component}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path

    return _write


@pytest.fixture()
def reader(tmp_jarvis_home: Path) -> TrinityHeartbeatReader:
    """A TrinityHeartbeatReader pointed at the temporary jarvis home."""
    return TrinityHeartbeatReader(jarvis_home=tmp_jarvis_home)


# ===================================================================
# TestHeartbeatData
# ===================================================================


class TestHeartbeatData:
    """Tests for the HeartbeatData dataclass and its from_dict factory."""

    def test_fresh_heartbeat_is_healthy(self):
        """A heartbeat whose timestamp is < 30 s old is healthy and not stale."""
        data = {"timestamp": time.time() - 5, "status": "running"}
        hb = HeartbeatData.from_dict("jarvis_body", data)

        assert hb.is_healthy is True
        assert hb.is_stale is False
        assert hb.age_seconds < 30
        assert hb.component == "jarvis_body"

    def test_stale_heartbeat(self):
        """A heartbeat whose timestamp is >= 30 s old is stale and not healthy."""
        data = {"timestamp": time.time() - 60, "status": "running"}
        hb = HeartbeatData.from_dict("reactor_core", data)

        assert hb.is_stale is True
        assert hb.is_healthy is False
        assert hb.age_seconds >= 30

    def test_missing_timestamp_very_stale(self):
        """If the dict has no 'timestamp' key, default is 0 -> extremely stale."""
        data = {"status": "unknown"}
        hb = HeartbeatData.from_dict("jarvis_prime", data)

        assert hb.timestamp == 0
        assert hb.is_stale is True
        assert hb.is_healthy is False
        # Age should be roughly equal to current epoch time (huge number)
        assert hb.age_seconds > 1_000_000

    def test_original_dict_preserved(self):
        """The .data attribute returns the original dict passed to from_dict."""
        data = {"timestamp": time.time(), "status": "ok", "extra_key": 42}
        hb = HeartbeatData.from_dict("coding_council", data)

        assert hb.data is data
        assert hb.data["extra_key"] == 42


# ===================================================================
# TestTrinityHeartbeatReader
# ===================================================================


class TestTrinityHeartbeatReader:
    """Tests for TrinityHeartbeatReader: file I/O, caching, health, wait."""

    # ---------------------------------------------------------------
    # Basic read operations
    # ---------------------------------------------------------------

    async def test_nonexistent_file_returns_none(self, reader: TrinityHeartbeatReader):
        """Reading a component with no heartbeat file returns None."""
        result = await reader.read_component_heartbeat("nonexistent_component")
        assert result is None

    async def test_valid_json_returns_heartbeat_data(
        self,
        reader: TrinityHeartbeatReader,
        heartbeat_writer,
    ):
        """A valid JSON heartbeat file is returned as a HeartbeatData."""
        ts = time.time()
        heartbeat_writer("jarvis_body", {"timestamp": ts, "status": "running", "pid": 1234})

        hb = await reader.read_component_heartbeat("jarvis_body")

        assert hb is not None
        assert isinstance(hb, HeartbeatData)
        assert hb.component == "jarvis_body"
        assert hb.status == "running"
        assert hb.data["pid"] == 1234
        assert hb.timestamp == ts

    async def test_invalid_json_returns_none(
        self,
        tmp_jarvis_home: Path,
        reader: TrinityHeartbeatReader,
    ):
        """Corrupt / non-JSON content returns None without raising."""
        path = tmp_jarvis_home / "trinity" / "components" / "broken.json"
        path.write_text("this is {{{ not valid json !!!")

        result = await reader.read_component_heartbeat("broken")
        assert result is None

    # ---------------------------------------------------------------
    # Cache behaviour
    # ---------------------------------------------------------------

    async def test_cache_returns_same_within_ttl(
        self,
        tmp_jarvis_home: Path,
        heartbeat_writer,
    ):
        """Two reads within the cache TTL return the exact same object (id check)."""
        short_ttl_reader = TrinityHeartbeatReader(
            jarvis_home=tmp_jarvis_home, cache_ttl=5.0
        )
        heartbeat_writer("jarvis_body", {"timestamp": time.time(), "status": "ok"})

        first = await short_ttl_reader.read_component_heartbeat("jarvis_body")
        second = await short_ttl_reader.read_component_heartbeat("jarvis_body")

        assert first is not None
        assert id(first) == id(second)

    async def test_cache_refreshes_after_ttl(
        self,
        tmp_jarvis_home: Path,
        heartbeat_writer,
    ):
        """After the TTL expires, the reader fetches fresh data from disk."""
        very_short_ttl = 0.05  # 50 ms
        short_reader = TrinityHeartbeatReader(
            jarvis_home=tmp_jarvis_home, cache_ttl=very_short_ttl
        )

        heartbeat_writer("jarvis_body", {"timestamp": time.time(), "status": "v1"})
        first = await short_reader.read_component_heartbeat("jarvis_body")
        assert first is not None
        assert first.status == "v1"

        # Wait for cache to expire, then write new data
        await asyncio.sleep(very_short_ttl + 0.02)
        heartbeat_writer("jarvis_body", {"timestamp": time.time(), "status": "v2"})

        second = await short_reader.read_component_heartbeat("jarvis_body")
        assert second is not None
        assert second.status == "v2"
        # Must be a different object
        assert id(first) != id(second)

    async def test_force_refresh_bypasses_cache(
        self,
        tmp_jarvis_home: Path,
        heartbeat_writer,
    ):
        """force_refresh=True always reads from disk, even within TTL."""
        long_ttl_reader = TrinityHeartbeatReader(
            jarvis_home=tmp_jarvis_home, cache_ttl=60.0
        )

        heartbeat_writer("jarvis_body", {"timestamp": time.time(), "status": "old"})
        first = await long_ttl_reader.read_component_heartbeat("jarvis_body")
        assert first is not None and first.status == "old"

        heartbeat_writer("jarvis_body", {"timestamp": time.time(), "status": "new"})
        refreshed = await long_ttl_reader.read_component_heartbeat(
            "jarvis_body", force_refresh=True
        )
        assert refreshed is not None
        assert refreshed.status == "new"

    # ---------------------------------------------------------------
    # get_all_heartbeats (parallel read)
    # ---------------------------------------------------------------

    async def test_get_all_heartbeats_parallel(
        self,
        tmp_jarvis_home: Path,
        heartbeat_writer,
    ):
        """get_all_heartbeats reads multiple components in one call."""
        # Create reader inside async context so asyncio.Lock binds to the
        # correct event loop (matters on Python 3.9 with older pytest-asyncio).
        async_reader = TrinityHeartbeatReader(jarvis_home=tmp_jarvis_home)

        now = time.time()
        heartbeat_writer("alpha", {"timestamp": now, "status": "ok"})
        heartbeat_writer("beta", {"timestamp": now, "status": "ok"})
        heartbeat_writer("gamma", {"timestamp": now, "status": "ok"})

        components = ["alpha", "beta", "gamma", "missing"]
        result = await async_reader.get_all_heartbeats(components=components)

        assert set(result.keys()) == {"alpha", "beta", "gamma", "missing"}
        assert result["alpha"] is not None
        assert result["beta"] is not None
        assert result["gamma"] is not None
        assert result["missing"] is None

    # ---------------------------------------------------------------
    # Health mapping via get_component_status
    # ---------------------------------------------------------------

    async def test_reader_health_mapping_healthy(
        self,
        reader: TrinityHeartbeatReader,
        heartbeat_writer,
    ):
        """A fresh heartbeat (age < 10 s) maps to 'healthy' status."""
        heartbeat_writer("jarvis_body", {"timestamp": time.time(), "status": "ok"})

        status = await reader.get_component_status("jarvis_body")
        assert status == "healthy"

    async def test_reader_health_mapping_stale(
        self,
        reader: TrinityHeartbeatReader,
        heartbeat_writer,
    ):
        """A heartbeat older than 30 s maps to 'stale' status."""
        heartbeat_writer(
            "jarvis_body", {"timestamp": time.time() - 60, "status": "ok"}
        )

        status = await reader.get_component_status("jarvis_body")
        assert status == "stale"

    async def test_reader_health_mapping_unknown(
        self,
        reader: TrinityHeartbeatReader,
    ):
        """A component with no heartbeat file maps to 'unknown' status."""
        status = await reader.get_component_status("does_not_exist")
        assert status == "unknown"

    # ---------------------------------------------------------------
    # wait_for_heartbeat
    # ---------------------------------------------------------------

    async def test_wait_for_heartbeat_success(
        self,
        reader: TrinityHeartbeatReader,
        heartbeat_writer,
    ):
        """wait_for_heartbeat returns HeartbeatData once the file appears."""

        async def _delayed_write():
            await asyncio.sleep(0.15)
            heartbeat_writer(
                "jarvis_prime", {"timestamp": time.time(), "status": "ready"}
            )

        task = asyncio.create_task(_delayed_write())
        try:
            result = await reader.wait_for_heartbeat(
                "jarvis_prime", timeout=2.0, check_interval=0.05
            )
            assert result is not None
            assert isinstance(result, HeartbeatData)
            assert result.component == "jarvis_prime"
            assert result.is_healthy is True
        finally:
            await task

    async def test_wait_for_heartbeat_timeout(
        self,
        reader: TrinityHeartbeatReader,
    ):
        """wait_for_heartbeat returns None when the file never appears."""
        result = await reader.wait_for_heartbeat(
            "never_arrives", timeout=0.3, check_interval=0.05
        )
        assert result is None

    # ---------------------------------------------------------------
    # write_heartbeat
    # ---------------------------------------------------------------

    async def test_write_heartbeat_creates_valid_json(
        self,
        reader: TrinityHeartbeatReader,
    ):
        """write_heartbeat writes a file that can be read back successfully."""
        success = await reader.write_heartbeat(
            "test_comp", status="active", extra_data={"version": "1.0"}
        )
        assert success is True

        hb = await reader.read_component_heartbeat("test_comp", force_refresh=True)
        assert hb is not None
        assert hb.component == "test_comp"
        assert hb.status == "active"
        assert hb.data["version"] == "1.0"
        assert hb.is_healthy is True  # just written, should be fresh

    async def test_write_heartbeat_creates_directory(
        self,
        tmp_path: Path,
    ):
        """write_heartbeat creates the parent directory if it does not exist."""
        fresh_home = tmp_path / "brand_new_jarvis"
        # Deliberately do NOT create trinity/components/ subdirectory
        wr = TrinityHeartbeatReader(jarvis_home=fresh_home)

        success = await wr.write_heartbeat("some_comp", status="booting")
        assert success is True

        expected_path = fresh_home / "trinity" / "components" / "some_comp.json"
        assert expected_path.exists()
        content = json.loads(expected_path.read_text())
        assert content["component"] == "some_comp"
        assert content["status"] == "booting"
        assert "timestamp" in content
