"""Tests for LargeFileChunker — semantic file chunking for L1 execution pipeline."""
import hashlib

import pytest

from backend.core.ouroboros.governance.autonomy.file_chunker import (
    ChunkedFile,
    ChunkConfig,
    ChunkType,
    FileChunk,
    LargeFileChunker,
)


# ---------------------------------------------------------------------------
# ChunkConfig
# ---------------------------------------------------------------------------

class TestChunkConfigDefaults:
    def test_default_thresholds(self):
        cfg = ChunkConfig()
        assert cfg.threshold_lines == 500
        assert cfg.max_chunk_lines == 200
        assert cfg.min_chunk_lines == 10


# ---------------------------------------------------------------------------
# FileChunk
# ---------------------------------------------------------------------------

class TestFileChunkLineCount:
    def test_line_count_property(self):
        chunk = FileChunk(
            chunk_id="f::foo",
            chunk_type=ChunkType.FUNCTION,
            content="x\n" * 11,
            start_line=10,
            end_line=20,
            name="foo",
            order=0,
        )
        assert chunk.line_count == 11  # inclusive: 20 - 10 + 1


# ---------------------------------------------------------------------------
# ChunkedFile
# ---------------------------------------------------------------------------

class TestChunkedFileGetChunk:
    def _make_chunked(self) -> ChunkedFile:
        c1 = FileChunk(
            chunk_id="a::imports",
            chunk_type=ChunkType.IMPORTS,
            content="import os",
            start_line=1,
            end_line=1,
            name="imports",
            order=0,
        )
        c2 = FileChunk(
            chunk_id="a::Foo",
            chunk_type=ChunkType.CLASS,
            content="class Foo:\n    pass",
            start_line=3,
            end_line=4,
            name="Foo",
            order=1,
        )
        return ChunkedFile(
            file_path="a.py",
            chunks=[c1, c2],
            total_lines=4,
            original_hash="abc123",
        )

    def test_get_chunk_found(self):
        cf = self._make_chunked()
        found = cf.get_chunk("a::Foo")
        assert found is not None
        assert found.name == "Foo"

    def test_get_chunk_not_found(self):
        cf = self._make_chunked()
        assert cf.get_chunk("nonexistent") is None


class TestChunkedFileReassemble:
    def test_reassemble_preserves_order(self):
        """Chunks with out-of-order creation should reassemble in order field."""
        c_mid = FileChunk(
            chunk_id="x::mid", chunk_type=ChunkType.FUNCTION,
            content="def mid(): pass", start_line=5, end_line=5,
            name="mid", order=1,
        )
        c_first = FileChunk(
            chunk_id="x::first", chunk_type=ChunkType.IMPORTS,
            content="import os", start_line=1, end_line=1,
            name="imports", order=0,
        )
        c_last = FileChunk(
            chunk_id="x::last", chunk_type=ChunkType.FUNCTION,
            content="def last(): pass", start_line=10, end_line=10,
            name="last", order=2,
        )
        cf = ChunkedFile(
            file_path="x.py",
            # deliberately out of order
            chunks=[c_mid, c_last, c_first],
            total_lines=10,
            original_hash="h",
        )
        assembled = cf.reassemble()
        lines = assembled.split("\n")
        assert lines[0] == "import os"
        assert "def mid" in assembled
        assert "def last" in assembled
        # order: first (0) < mid (1) < last (2)
        assert assembled.index("import os") < assembled.index("def mid")
        assert assembled.index("def mid") < assembled.index("def last")


# ---------------------------------------------------------------------------
# LargeFileChunker — threshold
# ---------------------------------------------------------------------------

class TestShouldChunk:
    def test_should_chunk_small_file(self):
        chunker = LargeFileChunker()
        small = "x = 1\n" * 10
        assert chunker.should_chunk(small) is False

    def test_should_chunk_large_file(self):
        chunker = LargeFileChunker()
        large = "x = 1\n" * 600
        assert chunker.should_chunk(large) is True

    def test_should_chunk_respects_config(self):
        cfg = ChunkConfig(threshold_lines=20)
        chunker = LargeFileChunker(config=cfg)
        medium = "x = 1\n" * 25
        assert chunker.should_chunk(medium) is True


# ---------------------------------------------------------------------------
# LargeFileChunker — AST-based chunking
# ---------------------------------------------------------------------------

_SAMPLE_PYTHON = '''\
"""Module docstring."""

import os
import sys
from pathlib import Path

MAX_SIZE = 100
DEFAULT_NAME = "test"


class Alpha:
    """A class."""

    def __init__(self):
        self.x = 1

    def method_a(self):
        return self.x


class Beta:
    """Another class."""

    def run(self):
        pass


def standalone_one():
    """A function."""
    return 1


def standalone_two():
    """Another function."""
    return 2


def standalone_three():
    return 3
'''


class TestChunkContentExtractsImports:
    def test_chunk_content_extracts_imports(self):
        chunker = LargeFileChunker(config=ChunkConfig(threshold_lines=5))
        result = chunker.chunk_content(_SAMPLE_PYTHON, "sample.py")
        import_chunks = [c for c in result.chunks if c.chunk_type == ChunkType.IMPORTS]
        assert len(import_chunks) >= 1
        first = import_chunks[0]
        assert "import os" in first.content
        assert "import sys" in first.content


class TestChunkContentExtractsClasses:
    def test_chunk_content_extracts_classes(self):
        chunker = LargeFileChunker(config=ChunkConfig(threshold_lines=5))
        result = chunker.chunk_content(_SAMPLE_PYTHON, "sample.py")
        class_chunks = [c for c in result.chunks if c.chunk_type == ChunkType.CLASS]
        names = {c.name for c in class_chunks}
        assert "Alpha" in names
        assert "Beta" in names


class TestChunkContentExtractsFunctions:
    def test_chunk_content_extracts_functions(self):
        chunker = LargeFileChunker(config=ChunkConfig(threshold_lines=5))
        result = chunker.chunk_content(_SAMPLE_PYTHON, "sample.py")
        func_chunks = [c for c in result.chunks if c.chunk_type == ChunkType.FUNCTION]
        names = {c.name for c in func_chunks}
        assert "standalone_one" in names
        assert "standalone_two" in names
        assert "standalone_three" in names


class TestChunkContentReassemblesCorrectly:
    def test_chunk_content_reassembles_correctly(self):
        """Chunk then reassemble — the reassembled content must compile and
        contain all original top-level names."""
        chunker = LargeFileChunker(config=ChunkConfig(threshold_lines=5))
        result = chunker.chunk_content(_SAMPLE_PYTHON, "sample.py")
        reassembled = result.reassemble()
        # Must be valid Python
        compile(reassembled, "reassembled.py", "exec")
        # Must contain all original names
        for name in ("Alpha", "Beta", "standalone_one", "standalone_two",
                      "standalone_three", "MAX_SIZE", "import os"):
            assert name in reassembled, f"{name!r} missing from reassembled output"


# ---------------------------------------------------------------------------
# LargeFileChunker — fallback (line-based)
# ---------------------------------------------------------------------------

class TestChunkByLinesFallback:
    def test_chunk_by_lines_fallback(self):
        """Invalid Python should fall back to line-based chunking."""
        bad_python = "def broken(\n" * 50 + "x = 1\n" * 50
        chunker = LargeFileChunker(config=ChunkConfig(threshold_lines=5, max_chunk_lines=20))
        result = chunker.chunk_content(bad_python, "broken.py")
        # Should still produce chunks (line-based fallback)
        assert len(result.chunks) >= 1
        # Reassembly should reproduce original (modulo trailing newline)
        reassembled = result.reassemble()
        assert reassembled.strip() == bad_python.strip()


class TestChunkByLinesDirectly:
    def test_chunk_by_lines_produces_bounded_chunks(self):
        content = "line\n" * 100
        chunker = LargeFileChunker(config=ChunkConfig(max_chunk_lines=25))
        result = chunker.chunk_by_lines(content, "big.txt")
        for chunk in result.chunks:
            assert chunk.line_count <= 25


# ---------------------------------------------------------------------------
# Hash integrity
# ---------------------------------------------------------------------------

class TestChunkContentPreservesHash:
    def test_chunk_content_preserves_hash(self):
        chunker = LargeFileChunker(config=ChunkConfig(threshold_lines=5))
        result = chunker.chunk_content(_SAMPLE_PYTHON, "sample.py")
        expected = hashlib.sha256(_SAMPLE_PYTHON.encode("utf-8")).hexdigest()
        assert result.original_hash == expected


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEmptyFile:
    def test_empty_file(self):
        chunker = LargeFileChunker()
        result = chunker.chunk_content("", "empty.py")
        assert result.total_lines == 0
        # Either zero chunks or a single empty/blank chunk
        assert len(result.chunks) <= 1


class TestLargeClassHandling:
    def test_large_class_gets_split(self):
        """A class exceeding max_chunk_lines should be split into PARTIAL_CLASS chunks."""
        methods = []
        for i in range(30):
            methods.append(
                f"    def method_{i}(self):\n"
                + "".join(f"        x = {j}\n" for j in range(12))
                + "        return x\n"
            )
        big_class = "class Huge:\n    '''A huge class.'''\n\n" + "\n".join(methods)
        chunker = LargeFileChunker(config=ChunkConfig(
            threshold_lines=5,
            max_chunk_lines=50,
        ))
        result = chunker.chunk_content(big_class, "huge.py")
        partial_chunks = [c for c in result.chunks if c.chunk_type == ChunkType.PARTIAL_CLASS]
        # Must have produced partial chunks (header + methods)
        assert len(partial_chunks) >= 2, (
            f"Expected PARTIAL_CLASS chunks, got types: "
            f"{[c.chunk_type.value for c in result.chunks]}"
        )


class TestConstantsChunk:
    def test_module_level_assignments_in_constants(self):
        """Module-level assignments between imports and first class/func
        should appear in a CONSTANTS chunk."""
        src = (
            "import os\n"
            "\n"
            "FOO = 1\n"
            "BAR = 'hello'\n"
            "\n"
            "def func():\n"
            "    return FOO\n"
        )
        chunker = LargeFileChunker(config=ChunkConfig(threshold_lines=2))
        result = chunker.chunk_content(src, "consts.py")
        const_chunks = [c for c in result.chunks if c.chunk_type == ChunkType.CONSTANTS]
        assert len(const_chunks) >= 1
        combined = "\n".join(c.content for c in const_chunks)
        assert "FOO = 1" in combined


class TestDependencyTracking:
    def test_function_referencing_class_has_dependency(self):
        """A function that references a class name should list it as a dependency."""
        src = (
            "class MyClass:\n"
            "    pass\n"
            "\n"
            "def factory():\n"
            "    return MyClass()\n"
        )
        chunker = LargeFileChunker(config=ChunkConfig(threshold_lines=2))
        result = chunker.chunk_content(src, "deps.py")
        factory_chunks = [c for c in result.chunks if c.name == "factory"]
        assert len(factory_chunks) == 1
        deps = factory_chunks[0].dependencies
        # Should reference the MyClass chunk
        assert any("MyClass" in d for d in deps)
