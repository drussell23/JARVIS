"""Tests for the memory governance AST checker."""
import ast
import pytest
import textwrap
from pathlib import Path


# Import the checker
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
from check_memory_governance import GovernanceChecker, Violation, check_file


class TestBannedConstructors:
    def _check(self, code: str, file_path: str = "backend/some_file.py") -> list:
        tree = ast.parse(textwrap.dedent(code))
        checker = GovernanceChecker(file_path)
        checker.visit(tree)
        return checker.violations

    def test_detects_direct_sentence_transformer(self):
        code = '''
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer("all-MiniLM-L6-v2")
        '''
        violations = self._check(code)
        assert len(violations) == 1
        assert "SentenceTransformer" in violations[0].detail

    def test_allows_sentence_transformer_in_embedding_service(self):
        code = '''
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer("all-MiniLM-L6-v2")
        '''
        violations = self._check(code, "backend/core/embedding_service.py")
        assert len(violations) == 0

    def test_allows_sentence_transformer_in_budgeted_loaders(self):
        code = '''
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer("all-MiniLM-L6-v2")
        '''
        violations = self._check(code, "backend/core/budgeted_loaders.py")
        assert len(violations) == 0

    def test_detects_psutil_virtual_memory(self):
        code = '''
        import psutil
        mem = psutil.virtual_memory()
        '''
        violations = self._check(code)
        assert len(violations) == 1
        assert "psutil.virtual_memory" in violations[0].detail

    def test_allows_psutil_in_memory_quantizer(self):
        code = '''
        import psutil
        mem = psutil.virtual_memory()
        '''
        violations = self._check(code, "backend/core/memory_quantizer.py")
        assert len(violations) == 0

    def test_detects_psutil_swap_memory(self):
        code = '''
        import psutil
        swap = psutil.swap_memory()
        '''
        violations = self._check(code)
        assert len(violations) == 1
        assert "psutil.swap_memory" in violations[0].detail

    def test_no_violations_in_clean_code(self):
        code = '''
        from backend.core.embedding_service import get_embedding_service
        service = get_embedding_service()
        embeddings = service.encode("hello")
        '''
        violations = self._check(code)
        assert len(violations) == 0

    def test_violation_includes_line_number(self):
        code = '''
        from sentence_transformers import SentenceTransformer
        x = 1
        y = 2
        model = SentenceTransformer("test")
        '''
        violations = self._check(code)
        assert len(violations) == 1
        assert violations[0].line == 5  # Line 5 in the dedented code

    def test_comment_reference_not_flagged(self):
        """String references to SentenceTransformer should not be flagged."""
        code = '''
        # This uses SentenceTransformer via the embedding service
        x = "SentenceTransformer is great"
        '''
        violations = self._check(code)
        assert len(violations) == 0

    def test_multiple_violations_in_one_file(self):
        code = '''
        import psutil
        from sentence_transformers import SentenceTransformer
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        model = SentenceTransformer("test")
        '''
        violations = self._check(code)
        assert len(violations) == 3
