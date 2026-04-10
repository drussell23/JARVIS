"""Safety tests for Venom's hardened edit_file / write_file handlers.

Covers the multi-layer safety chain added in Task #193:

  1. Path safety (safe_resolve + protected path list)
  2. Must-have-read invariant
  3. Iron Gate ASCII strict integration
  4. Iron Gate dependency file integrity integration
  5. Python AST validation
  6. Post-write hash verification + rollback
  7. Policy-layer defence-in-depth

Every test creates a fresh ToolExecutor so the instance state
(``_files_read``, ``_edit_history``) is isolated.
"""

from __future__ import annotations

from pathlib import Path

from backend.core.ouroboros.governance.tool_executor import (
    GoverningToolPolicy,
    PolicyContext,
    PolicyDecision,
    ToolCall,
    ToolExecutor,
    _is_protected_path,
    _run_venom_iron_gates,
    _validate_python_syntax,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path) -> Path:
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text(
        "aiohttp==3.13.2\n"
        "anthropic==0.75.0\n"
        "httpx==0.28.1\n"
        "rapidfuzz>=3.0.0\n"
        "requests==2.32.5\n",
        encoding="utf-8",
    )
    return tmp_path


def _read(executor: ToolExecutor, path: str) -> str:
    """Helper: call read_file and return raw tool output."""
    return executor._read_file({"path": path})


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


class TestProtectedPathHelper:
    def test_blocks_git_internals(self) -> None:
        assert _is_protected_path(".git/config") is not None
        assert _is_protected_path("repo/.git/HEAD") is not None
        assert _is_protected_path(".git/hooks/pre-commit") is not None

    def test_blocks_env_files(self) -> None:
        assert _is_protected_path(".env") is not None
        assert _is_protected_path(".env.local") is not None
        assert _is_protected_path("config/.env.prod") is not None

    def test_blocks_credentials_and_secrets(self) -> None:
        assert _is_protected_path("credentials.json") is not None
        assert _is_protected_path("aws_credentials") is not None
        assert _is_protected_path("secrets.yaml") is not None
        assert _is_protected_path("app/secret_key.pem") is not None

    def test_blocks_ssh_and_cloud_creds(self) -> None:
        assert _is_protected_path(".ssh/authorized_keys") is not None
        assert _is_protected_path("home/user/id_rsa") is not None
        assert _is_protected_path(".aws/credentials") is not None

    def test_blocks_package_manager_dirs(self) -> None:
        assert _is_protected_path("node_modules/foo/index.js") is not None
        assert _is_protected_path(".venv/lib/python3.9/site.py") is not None

    def test_blocks_jarvis_internal_state(self) -> None:
        assert _is_protected_path(".jarvis/ops/log.jsonl") is not None
        assert _is_protected_path(".ouroboros/sessions/abc.log") is not None

    def test_allows_normal_code(self) -> None:
        assert _is_protected_path("backend/main.py") is None
        assert _is_protected_path("tests/test_foo.py") is None
        assert _is_protected_path("requirements.txt") is None
        assert _is_protected_path("README.md") is None

    def test_empty_path_rejected(self) -> None:
        assert _is_protected_path("") is not None


class TestPythonSyntaxHelper:
    def test_valid_python_passes(self) -> None:
        assert _validate_python_syntax("foo.py", "x = 1\ndef f():\n    pass\n") is None

    def test_invalid_python_rejected(self) -> None:
        err = _validate_python_syntax("foo.py", "def broken(\n")
        assert err is not None
        assert "SyntaxError" in err

    def test_non_python_always_passes(self) -> None:
        # Even syntactically-broken Python should pass for .txt/.md files
        assert _validate_python_syntax("foo.txt", "def broken(\n") is None
        assert _validate_python_syntax("foo.md", "# heading\n") is None


class TestIronGateHelper:
    def test_ascii_content_passes(self) -> None:
        assert _run_venom_iron_gates("foo.py", "x = 1\n") is None

    def test_unicode_letter_rejected(self) -> None:
        # Arabic letter 'ف' (U+0641) — the exact rapidفuzz incident shape.
        reason = _run_venom_iron_gates(
            "requirements.txt", "rapid\u0641uzz>=3.0.0\n"
        )
        assert reason is not None
        assert "Iron Gate" in reason


# ---------------------------------------------------------------------------
# edit_file: safety chain
# ---------------------------------------------------------------------------


class TestEditFileMustHaveRead:
    def test_rejects_edit_without_prior_read(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        executor = ToolExecutor(repo_root=repo)

        result = executor._edit_file({
            "path": "backend/main.py",
            "old_text": "print('hello')",
            "new_text": "print('goodbye')",
        })
        assert "must-have-read violation" in result
        # File must be untouched
        assert (repo / "backend" / "main.py").read_text() == "print('hello')\n"

    def test_accepts_edit_after_read(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        executor = ToolExecutor(repo_root=repo)

        _read(executor, "backend/main.py")
        result = executor._edit_file({
            "path": "backend/main.py",
            "old_text": "print('hello')",
            "new_text": "print('goodbye')",
        })
        assert result.startswith("OK: edited")
        assert (repo / "backend" / "main.py").read_text() == "print('goodbye')\n"
        assert len(executor._edit_history) == 1
        entry = executor._edit_history[0]
        assert entry["tool"] == "edit_file"
        assert entry["path"] == "backend/main.py"


class TestEditFileProtectedPaths:
    def test_rejects_git_config(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        (repo / ".git").mkdir()
        (repo / ".git" / "config").write_text("[core]\n", encoding="utf-8")
        executor = ToolExecutor(repo_root=repo)
        # Pre-seed the read so must-have-read doesn't mask the test
        executor._files_read.add(".git/config")

        result = executor._edit_file({
            "path": ".git/config",
            "old_text": "[core]",
            "new_text": "[hacked]",
        })
        assert "protected path rejected" in result
        assert (repo / ".git" / "config").read_text() == "[core]\n"

    def test_rejects_env_file(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        (repo / ".env").write_text("SECRET=shh\n", encoding="utf-8")
        executor = ToolExecutor(repo_root=repo)
        executor._files_read.add(".env")

        result = executor._edit_file({
            "path": ".env",
            "old_text": "SECRET=shh",
            "new_text": "SECRET=leak",
        })
        assert "protected path rejected" in result
        assert (repo / ".env").read_text() == "SECRET=shh\n"

    def test_rejects_credentials(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        (repo / "credentials.json").write_text('{"key":"x"}\n', encoding="utf-8")
        executor = ToolExecutor(repo_root=repo)
        executor._files_read.add("credentials.json")

        result = executor._edit_file({
            "path": "credentials.json",
            "old_text": '"x"',
            "new_text": '"stolen"',
        })
        assert "protected path rejected" in result


class TestEditFileIronGates:
    def test_rejects_unicode_corruption(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        executor = ToolExecutor(repo_root=repo)
        _read(executor, "requirements.txt")

        # Arabic 'ف' replacing the 'f' in rapidfuzz — the real bt-2026-04-10 corruption
        result = executor._edit_file({
            "path": "requirements.txt",
            "old_text": "rapidfuzz>=3.0.0",
            "new_text": "rapid\u0641uzz>=3.0.0",
        })
        assert "Iron Gate" in result
        # Original content preserved
        assert "rapidfuzz>=3.0.0" in (repo / "requirements.txt").read_text()

    def test_rejects_dependency_rename_hallucination(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        executor = ToolExecutor(repo_root=repo)
        _read(executor, "requirements.txt")

        # Exact bt-2026-04-10-184157 regression: anthropic -> anthropichttp
        result = executor._edit_file({
            "path": "requirements.txt",
            "old_text": "anthropic==0.75.0",
            "new_text": "anthropichttp==0.75.0",
        })
        assert "Iron Gate dependency integrity" in result
        assert "anthropic -> anthropichttp" in result
        # Original content preserved
        assert "anthropic==0.75.0" in (repo / "requirements.txt").read_text()

    def test_accepts_legitimate_version_bump(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        executor = ToolExecutor(repo_root=repo)
        _read(executor, "requirements.txt")

        result = executor._edit_file({
            "path": "requirements.txt",
            "old_text": "anthropic==0.75.0",
            "new_text": "anthropic==0.80.0",
        })
        assert result.startswith("OK: edited")
        assert "anthropic==0.80.0" in (repo / "requirements.txt").read_text()


class TestEditFileAstValidation:
    def test_rejects_invalid_python(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        executor = ToolExecutor(repo_root=repo)
        _read(executor, "backend/main.py")

        result = executor._edit_file({
            "path": "backend/main.py",
            "old_text": "print('hello')",
            "new_text": "def broken(",  # missing colon, missing close
        })
        assert "AST validation failed" in result
        # File untouched
        assert (repo / "backend" / "main.py").read_text() == "print('hello')\n"

    def test_accepts_valid_python(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        executor = ToolExecutor(repo_root=repo)
        _read(executor, "backend/main.py")

        result = executor._edit_file({
            "path": "backend/main.py",
            "old_text": "print('hello')",
            "new_text": "def greet():\n    return 'hi'\n\ngreet()",
        })
        assert result.startswith("OK: edited")

    def test_skips_ast_for_non_python(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        (repo / "notes.txt").write_text("line1\n", encoding="utf-8")
        executor = ToolExecutor(repo_root=repo)
        _read(executor, "notes.txt")

        result = executor._edit_file({
            "path": "notes.txt",
            "old_text": "line1",
            "new_text": "def broken(",  # would fail AST, fine for .txt
        })
        assert result.startswith("OK: edited")


class TestEditFileUniqueness:
    def test_rejects_non_unique_old_text(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        (repo / "dup.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
        executor = ToolExecutor(repo_root=repo)
        _read(executor, "dup.py")

        result = executor._edit_file({
            "path": "dup.py",
            "old_text": "x = 1",
            "new_text": "x = 2",
        })
        assert "must be unique" in result
        # File untouched — both lines still there
        assert (repo / "dup.py").read_text() == "x = 1\nx = 1\n"

    def test_rejects_missing_old_text(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        executor = ToolExecutor(repo_root=repo)
        _read(executor, "backend/main.py")

        result = executor._edit_file({
            "path": "backend/main.py",
            "old_text": "nonexistent",
            "new_text": "anything",
        })
        assert "not found" in result


class TestEditFileAuditTrail:
    def test_records_edit_history(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        executor = ToolExecutor(repo_root=repo)
        _read(executor, "backend/main.py")

        executor._edit_file({
            "path": "backend/main.py",
            "old_text": "print('hello')",
            "new_text": "print('world')",
        })
        assert len(executor._edit_history) == 1
        entry = executor._edit_history[0]
        assert entry["tool"] == "edit_file"
        assert entry["path"] == "backend/main.py"
        assert entry["action"] == "edited"
        assert entry["sha256_before"] != entry["sha256_after"]
        assert entry["bytes_before"] > 0
        assert entry["bytes_after"] > 0
        assert "ts" in entry


# ---------------------------------------------------------------------------
# write_file: safety chain
# ---------------------------------------------------------------------------


class TestWriteFileNewFile:
    def test_creates_new_file_without_read(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        executor = ToolExecutor(repo_root=repo)

        result = executor._write_file({
            "path": "backend/new_mod.py",
            "content": "def greet():\n    return 'hi'\n",
        })
        assert result.startswith("OK: created")
        assert (repo / "backend" / "new_mod.py").exists()
        assert "backend/new_mod.py" in executor._files_read

    def test_ast_rejects_invalid_new_python(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        executor = ToolExecutor(repo_root=repo)

        result = executor._write_file({
            "path": "backend/bad.py",
            "content": "def broken(\n",
        })
        assert "AST validation failed" in result
        # File should NOT exist
        assert not (repo / "backend" / "bad.py").exists()


class TestWriteFileOverwrite:
    def test_rejects_overwrite_without_read(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        executor = ToolExecutor(repo_root=repo)

        result = executor._write_file({
            "path": "backend/main.py",
            "content": "print('replaced')\n",
        })
        assert "must-have-read violation" in result
        assert (repo / "backend" / "main.py").read_text() == "print('hello')\n"

    def test_accepts_overwrite_after_read(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        executor = ToolExecutor(repo_root=repo)
        _read(executor, "backend/main.py")

        result = executor._write_file({
            "path": "backend/main.py",
            "content": "print('replaced')\n",
        })
        assert result.startswith("OK: overwritten")
        assert (repo / "backend" / "main.py").read_text() == "print('replaced')\n"
        assert executor._edit_history[0]["action"] == "overwritten"


class TestWriteFileProtectedPaths:
    def test_rejects_write_to_env(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        executor = ToolExecutor(repo_root=repo)

        result = executor._write_file({
            "path": ".env",
            "content": "API_KEY=hacked\n",
        })
        assert "protected path rejected" in result
        assert not (repo / ".env").exists()

    def test_rejects_write_into_git_dir(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        executor = ToolExecutor(repo_root=repo)

        result = executor._write_file({
            "path": ".git/hooks/pre-commit",
            "content": "#!/bin/sh\nrm -rf /\n",
        })
        assert "protected path rejected" in result


class TestWriteFileDependencyIntegrity:
    def test_rejects_rename_hallucination_on_overwrite(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        executor = ToolExecutor(repo_root=repo)
        _read(executor, "requirements.txt")

        bad = (
            "aiohttp==3.13.2\n"
            "anthropichttp==0.75.0\n"   # <- corrupt rename
            "httpx==0.28.1\n"
            "rapidfuzz>=3.0.0\n"
            "requests==2.32.5\n"
        )
        result = executor._write_file({
            "path": "requirements.txt",
            "content": bad,
        })
        assert "Iron Gate dependency integrity" in result
        # Baseline preserved
        assert "anthropic==0.75.0" in (repo / "requirements.txt").read_text()
        assert "anthropichttp" not in (repo / "requirements.txt").read_text()


# ---------------------------------------------------------------------------
# Policy-layer defence-in-depth
# ---------------------------------------------------------------------------


class TestPolicyProtectedPathDefense:
    def test_policy_rejects_edit_to_protected_path(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        (repo / ".git").mkdir()
        (repo / ".git" / "config").write_text("[core]\n", encoding="utf-8")
        policy = GoverningToolPolicy(repo_roots={"jarvis": repo})
        ctx = PolicyContext(
            repo="jarvis",
            repo_root=repo,
            op_id="op-test",
            call_id="op-test:r0:edit_file",
            round_index=0,
        )
        call = ToolCall(
            name="edit_file",
            arguments={
                "path": ".git/config",
                "old_text": "[core]",
                "new_text": "[hacked]",
            },
        )

        result = policy.evaluate(call, ctx)
        assert result.decision == PolicyDecision.DENY
        assert result.reason_code == "tool.denied.protected_path"

    def test_policy_allows_normal_edit(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        policy = GoverningToolPolicy(repo_roots={"jarvis": repo})
        ctx = PolicyContext(
            repo="jarvis",
            repo_root=repo,
            op_id="op-test",
            call_id="op-test:r0:edit_file",
            round_index=0,
        )
        call = ToolCall(
            name="edit_file",
            arguments={
                "path": "backend/main.py",
                "old_text": "print('hello')",
                "new_text": "print('world')",
            },
        )

        result = policy.evaluate(call, ctx)
        assert result.decision == PolicyDecision.ALLOW


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_edit_missing_file(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        executor = ToolExecutor(repo_root=repo)
        executor._files_read.add("nonexistent.py")  # even with "read", file doesn't exist

        result = executor._edit_file({
            "path": "nonexistent.py",
            "old_text": "x",
            "new_text": "y",
        })
        assert "file not found" in result

    def test_edit_directory_rejected(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        executor = ToolExecutor(repo_root=repo)
        executor._files_read.add("backend")

        result = executor._edit_file({
            "path": "backend",
            "old_text": "x",
            "new_text": "y",
        })
        assert "is a directory" in result

    def test_edit_noop_detected(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        executor = ToolExecutor(repo_root=repo)
        _read(executor, "backend/main.py")

        result = executor._edit_file({
            "path": "backend/main.py",
            "old_text": "print('hello')",
            "new_text": "print('hello')",
        })
        assert "no-op" in result

    def test_write_file_into_new_subdirectory(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        executor = ToolExecutor(repo_root=repo)

        result = executor._write_file({
            "path": "backend/subpkg/__init__.py",
            "content": "",
        })
        assert result.startswith("OK: created")
        assert (repo / "backend" / "subpkg" / "__init__.py").exists()

    def test_edit_history_chains_across_tools(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        executor = ToolExecutor(repo_root=repo)

        # Create a new file
        executor._write_file({
            "path": "backend/fresh.py",
            "content": "x = 1\n",
        })
        # Edit the just-created file WITHOUT an explicit read — success
        # because _record_edit adds the path to _files_read.
        result = executor._edit_file({
            "path": "backend/fresh.py",
            "old_text": "x = 1",
            "new_text": "x = 2",
        })
        assert result.startswith("OK: edited")
        assert len(executor._edit_history) == 2

    def test_get_edit_history_returns_defensive_copy(
        self, tmp_path: Path,
    ) -> None:
        repo = _make_repo(tmp_path)
        executor = ToolExecutor(repo_root=repo)
        _read(executor, "backend/main.py")
        executor._edit_file({
            "path": "backend/main.py",
            "old_text": "print('hello')",
            "new_text": "print('hi')",
        })
        snapshot = executor.get_edit_history()
        assert len(snapshot) == 1
        snapshot[0]["path"] = "MUTATED"
        # Internal state unchanged by external mutation
        assert executor._edit_history[0]["path"] == "backend/main.py"

    def test_env_extensible_protected_paths(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        repo = _make_repo(tmp_path)
        (repo / "config").mkdir()
        (repo / "config" / "customer_data.json").write_text(
            "{}\n", encoding="utf-8",
        )
        executor = ToolExecutor(repo_root=repo)
        _read(executor, "config/customer_data.json")

        # Without the env var, the path is writable
        result_ok = executor._edit_file({
            "path": "config/customer_data.json",
            "old_text": "{}",
            "new_text": '{"a": 1}',
        })
        assert result_ok.startswith("OK: edited")

        # With the env var, the same path is now protected
        monkeypatch.setenv("JARVIS_VENOM_PROTECTED_PATHS", "customer_data")
        result_blocked = executor._edit_file({
            "path": "config/customer_data.json",
            "old_text": '{"a": 1}',
            "new_text": '{"hacked": true}',
        })
        assert "protected path rejected" in result_blocked
        assert "(env)" in result_blocked
