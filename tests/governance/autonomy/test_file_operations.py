"""tests/governance/autonomy/test_file_operations.py

TDD tests for FileOperationRequest, MultiFileRequest, and FileOperationValidator
(Task M2: structured multi-file ops for L1).

Covers:
- FileOperationRequest: validation for each FileOpType
- MultiFileRequest: aggregation, filtering properties, serialization, immutability
- FileOperationValidator: protected paths, duplicates, conflicts, custom patterns
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# FileOperationRequest
# ---------------------------------------------------------------------------


class TestFileOperationRequest:
    def test_create_requires_content(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            FileOperationRequest,
            FileOpType,
        )

        req = FileOperationRequest(op_type=FileOpType.CREATE, file_path="src/new.py")
        errors = req.validate()
        assert any("content" in e.lower() for e in errors)

    def test_modify_requires_content(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            FileOperationRequest,
            FileOpType,
        )

        req = FileOperationRequest(op_type=FileOpType.MODIFY, file_path="src/main.py")
        errors = req.validate()
        assert any("content" in e.lower() for e in errors)

    def test_rename_requires_new_path(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            FileOperationRequest,
            FileOpType,
        )

        req = FileOperationRequest(op_type=FileOpType.RENAME, file_path="old.py")
        errors = req.validate()
        assert any("new_path" in e.lower() for e in errors)

    def test_delete_valid_without_content(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            FileOperationRequest,
            FileOpType,
        )

        req = FileOperationRequest(
            op_type=FileOpType.DELETE,
            file_path="src/obsolete.py",
        )
        errors = req.validate()
        assert errors == []

    def test_empty_path_invalid(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            FileOperationRequest,
            FileOpType,
        )

        req = FileOperationRequest(op_type=FileOpType.DELETE, file_path="")
        errors = req.validate()
        assert any("file_path" in e.lower() for e in errors)

    def test_valid_create(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            FileOperationRequest,
            FileOpType,
        )

        req = FileOperationRequest(
            op_type=FileOpType.CREATE,
            file_path="src/new_module.py",
            content="# new module\n",
            reason="Adding utility module",
        )
        errors = req.validate()
        assert errors == []

    def test_create_empty_content_invalid(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            FileOperationRequest,
            FileOpType,
        )

        req = FileOperationRequest(
            op_type=FileOpType.CREATE, file_path="src/empty.py", content=""
        )
        errors = req.validate()
        assert any("content" in e.lower() and "empty" in e.lower() for e in errors)

    def test_valid_rename(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            FileOperationRequest,
            FileOpType,
        )

        req = FileOperationRequest(
            op_type=FileOpType.RENAME,
            file_path="old_name.py",
            new_path="new_name.py",
        )
        errors = req.validate()
        assert errors == []

    def test_frozen(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            FileOperationRequest,
            FileOpType,
        )

        req = FileOperationRequest(
            op_type=FileOpType.CREATE,
            file_path="src/x.py",
            content="x = 1\n",
        )
        with pytest.raises(AttributeError):
            req.file_path = "other.py"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# MultiFileRequest
# ---------------------------------------------------------------------------


class TestMultiFileRequest:
    def _make_ops(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            FileOperationRequest,
            FileOpType,
        )

        return (
            FileOperationRequest(
                op_type=FileOpType.CREATE,
                file_path="src/a.py",
                content="a = 1\n",
            ),
            FileOperationRequest(
                op_type=FileOpType.CREATE,
                file_path="src/b.py",
                content="b = 2\n",
            ),
            FileOperationRequest(
                op_type=FileOpType.MODIFY,
                file_path="src/c.py",
                content="c = 3\n",
            ),
        )

    def test_file_count(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            MultiFileRequest,
        )

        ops = self._make_ops()
        mfr = MultiFileRequest(operations=ops)
        assert mfr.file_count == 3

    def test_creates_property(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            FileOpType,
            MultiFileRequest,
        )

        ops = self._make_ops()
        mfr = MultiFileRequest(operations=ops)
        creates = mfr.creates
        assert len(creates) == 2
        assert all(op.op_type == FileOpType.CREATE for op in creates)

    def test_modifies_property(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            FileOpType,
            MultiFileRequest,
        )

        ops = self._make_ops()
        mfr = MultiFileRequest(operations=ops)
        modifies = mfr.modifies
        assert len(modifies) == 1
        assert modifies[0].op_type == FileOpType.MODIFY

    def test_deletes_property(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            FileOperationRequest,
            FileOpType,
            MultiFileRequest,
        )

        ops = (
            FileOperationRequest(
                op_type=FileOpType.DELETE, file_path="src/old.py"
            ),
            FileOperationRequest(
                op_type=FileOpType.CREATE,
                file_path="src/new.py",
                content="new\n",
            ),
        )
        mfr = MultiFileRequest(operations=ops)
        deletes = mfr.deletes
        assert len(deletes) == 1
        assert deletes[0].file_path == "src/old.py"

    def test_affected_paths(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            MultiFileRequest,
        )

        ops = self._make_ops()
        mfr = MultiFileRequest(operations=ops)
        paths = mfr.affected_paths
        assert isinstance(paths, frozenset)
        assert paths == frozenset({"src/a.py", "src/b.py", "src/c.py"})

    def test_validate_aggregates_errors(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            FileOperationRequest,
            FileOpType,
            MultiFileRequest,
        )

        ops = (
            FileOperationRequest(
                op_type=FileOpType.CREATE, file_path="src/a.py"
            ),  # missing content
            FileOperationRequest(
                op_type=FileOpType.RENAME, file_path="src/b.py"
            ),  # missing new_path
        )
        mfr = MultiFileRequest(operations=ops)
        errors = mfr.validate()
        assert len(errors) >= 2

    def test_to_dict_excludes_content(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            MultiFileRequest,
        )

        ops = self._make_ops()
        mfr = MultiFileRequest(operations=ops, op_id="op-1", brain_id="claude")
        d = mfr.to_dict()
        assert d["request_id"] == mfr.request_id
        assert d["op_id"] == "op-1"
        assert d["brain_id"] == "claude"
        assert d["file_count"] == 3
        # Verify content is excluded from serialised operations
        for op_dict in d["operations"]:
            assert "content" not in op_dict

    def test_immutable(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            MultiFileRequest,
        )

        ops = self._make_ops()
        mfr = MultiFileRequest(operations=ops)
        with pytest.raises((TypeError, AttributeError)):
            mfr.operations = ()  # type: ignore[misc]

    def test_request_id_auto_generated(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            MultiFileRequest,
        )

        mfr = MultiFileRequest()
        assert mfr.request_id  # non-empty
        assert len(mfr.request_id) == 12  # uuid hex[:12]

    def test_empty_request_valid(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            MultiFileRequest,
        )

        mfr = MultiFileRequest()
        errors = mfr.validate()
        assert errors == []

    def test_affected_paths_includes_rename_new_path(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            FileOperationRequest,
            FileOpType,
            MultiFileRequest,
        )

        ops = (
            FileOperationRequest(
                op_type=FileOpType.RENAME,
                file_path="old.py",
                new_path="new.py",
            ),
        )
        mfr = MultiFileRequest(operations=ops)
        paths = mfr.affected_paths
        assert "old.py" in paths
        assert "new.py" in paths


# ---------------------------------------------------------------------------
# FileOperationValidator
# ---------------------------------------------------------------------------


class TestFileOperationValidator:
    def test_protected_path_env(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            FileOperationRequest,
            FileOperationValidator,
            FileOpType,
            MultiFileRequest,
        )

        ops = (
            FileOperationRequest(
                op_type=FileOpType.MODIFY,
                file_path=".env",
                content="SECRET=x\n",
            ),
        )
        validator = FileOperationValidator()
        errors = validator.validate(MultiFileRequest(operations=ops))
        assert any(".env" in e for e in errors)

    def test_protected_path_git(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            FileOperationRequest,
            FileOperationValidator,
            FileOpType,
            MultiFileRequest,
        )

        ops = (
            FileOperationRequest(
                op_type=FileOpType.MODIFY,
                file_path=".git/config",
                content="bad\n",
            ),
        )
        validator = FileOperationValidator()
        errors = validator.validate(MultiFileRequest(operations=ops))
        assert any(".git/" in e for e in errors)

    def test_protected_path_credentials(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            FileOperationRequest,
            FileOperationValidator,
            FileOpType,
            MultiFileRequest,
        )

        ops = (
            FileOperationRequest(
                op_type=FileOpType.CREATE,
                file_path="config/credentials.json",
                content="{}\n",
            ),
        )
        validator = FileOperationValidator()
        errors = validator.validate(MultiFileRequest(operations=ops))
        assert any("credentials" in e for e in errors)

    def test_normal_path_allowed(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            FileOperationRequest,
            FileOperationValidator,
            FileOpType,
            MultiFileRequest,
        )

        ops = (
            FileOperationRequest(
                op_type=FileOpType.CREATE,
                file_path="src/main.py",
                content="print('hello')\n",
            ),
        )
        validator = FileOperationValidator()
        errors = validator.validate(MultiFileRequest(operations=ops))
        assert errors == []

    def test_duplicate_paths_detected(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            FileOperationRequest,
            FileOperationValidator,
            FileOpType,
            MultiFileRequest,
        )

        ops = (
            FileOperationRequest(
                op_type=FileOpType.CREATE,
                file_path="src/dup.py",
                content="a\n",
            ),
            FileOperationRequest(
                op_type=FileOpType.MODIFY,
                file_path="src/dup.py",
                content="b\n",
            ),
        )
        validator = FileOperationValidator()
        errors = validator.validate(MultiFileRequest(operations=ops))
        assert any("duplicate" in e.lower() for e in errors)

    def test_conflicting_ops_detected(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            FileOperationRequest,
            FileOperationValidator,
            FileOpType,
            MultiFileRequest,
        )

        ops = (
            FileOperationRequest(
                op_type=FileOpType.CREATE,
                file_path="src/conflict.py",
                content="x\n",
            ),
            FileOperationRequest(
                op_type=FileOpType.DELETE,
                file_path="src/conflict.py",
            ),
        )
        validator = FileOperationValidator()
        errors = validator.validate(MultiFileRequest(operations=ops))
        assert any("conflict" in e.lower() for e in errors)

    def test_custom_protected_patterns(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            FileOperationRequest,
            FileOperationValidator,
            FileOpType,
            MultiFileRequest,
        )

        ops = (
            FileOperationRequest(
                op_type=FileOpType.CREATE,
                file_path="deploy/production.yaml",
                content="kind: Deployment\n",
            ),
        )
        validator = FileOperationValidator(additional_protected=["production"])
        errors = validator.validate(MultiFileRequest(operations=ops))
        assert any("production" in e for e in errors)

    def test_valid_multi_file_request(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            FileOperationRequest,
            FileOperationValidator,
            FileOpType,
            MultiFileRequest,
        )

        ops = (
            FileOperationRequest(
                op_type=FileOpType.CREATE,
                file_path="src/new_module.py",
                content="# new module\n",
                reason="Add utility",
            ),
            FileOperationRequest(
                op_type=FileOpType.MODIFY,
                file_path="src/existing.py",
                content="# modified\n",
                reason="Fix bug",
            ),
            FileOperationRequest(
                op_type=FileOpType.DELETE,
                file_path="src/deprecated.py",
                reason="No longer needed",
            ),
        )
        validator = FileOperationValidator()
        errors = validator.validate(MultiFileRequest(operations=ops))
        assert errors == []

    def test_is_protected_method(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            FileOperationValidator,
        )

        validator = FileOperationValidator()
        assert validator.is_protected(".env") is True
        assert validator.is_protected(".git/hooks/pre-commit") is True
        assert validator.is_protected("src/main.py") is False
        assert validator.is_protected("my_secret_config.yaml") is True
        assert validator.is_protected("node_modules/pkg/index.js") is True
        assert validator.is_protected("__pycache__/mod.cpython-311.pyc") is True

    def test_rename_new_path_also_protected(self):
        from backend.core.ouroboros.governance.autonomy.file_operations import (
            FileOperationRequest,
            FileOperationValidator,
            FileOpType,
            MultiFileRequest,
        )

        ops = (
            FileOperationRequest(
                op_type=FileOpType.RENAME,
                file_path="safe_old.py",
                new_path=".env.backup",
            ),
        )
        validator = FileOperationValidator()
        errors = validator.validate(MultiFileRequest(operations=ops))
        assert any(".env" in e for e in errors)
