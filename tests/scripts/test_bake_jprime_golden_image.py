# -*- coding: utf-8 -*-
"""Pure-logic tests for scripts/bake_jprime_golden_image.py.

No real gcloud. ALL subprocess goes through the script's single `_run` boundary,
which these tests monkeypatch to assert dry-run never executes and execute does.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "bake_jprime_golden_image.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("bake_jprime_golden_image", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def bake():
    return _load_module()


def _noop_autopsy_dir(bake, monkeypatch, tmp_path):
    """Redirect autopsy output to tmp_path so tests don't litter the working dir."""
    monkeypatch.setattr(bake, "_AUTOPSY_DIR", str(tmp_path / "autopsy_reports"))


# --------------------------------------------------------------------------- #
# Startup-script generator.
# --------------------------------------------------------------------------- #
def test_startup_script_installs_ollama_pulls_model_and_sentinels(bake):
    script = bake.build_startup_script("qwen2.5-coder:7b")
    assert "ollama.com/install.sh" in script  # installs Ollama
    assert "ollama pull qwen2.5-coder:7b" in script  # pulls the model
    assert "ollama serve" in script  # serves
    # sentinel written ONLY after the pull (inside the success branch)
    assert "/var/run/jprime_bake_ready" in script
    assert script.startswith("#!")
    # ASCII only.
    script.encode("ascii")


def test_startup_script_sentinel_uses_custom_model(bake):
    script = bake.build_startup_script("codellama:13b")
    assert "ollama pull codellama:13b" in script


# --------------------------------------------------------------------------- #
# Validation-verdict parser (the load-bearing lock).
# --------------------------------------------------------------------------- #
def test_validation_pass_on_200_with_def(bake):
    body = '{"response": "def is_even(n):\\n    return n % 2 == 0"}'
    passed, reason = bake.parse_validation_verdict(0, body)
    assert passed is True
    assert "def " in reason


def test_validation_pass_on_openai_chat_shape(bake):
    body = (
        '{"choices":[{"message":{"content":"Here:\\ndef is_even(n): return n%2==0"}}]}'
    )
    passed, reason = bake.parse_validation_verdict(0, body)
    assert passed is True


def test_validation_fail_on_empty_body(bake):
    passed, reason = bake.parse_validation_verdict(0, "")
    assert passed is False
    assert "empty" in reason.lower()


def test_validation_fail_on_non_200(bake):
    # curl --fail sets rc != 0 on a non-200; the parser must ABORT.
    passed, reason = bake.parse_validation_verdict(22, '{"response": "def x(): pass"}')
    assert passed is False
    assert "rc=" in reason


def test_validation_fail_on_no_def(bake):
    body = '{"response": "I cannot help with that."}'
    passed, reason = bake.parse_validation_verdict(0, body)
    assert passed is False
    assert "def " in reason  # reason explains 'def ' is absent


def test_validation_handles_non_json_raw_text(bake):
    passed, reason = bake.parse_validation_verdict(0, "def is_even(n): return True")
    assert passed is True


# --------------------------------------------------------------------------- #
# Awaken one-liner.
# --------------------------------------------------------------------------- #
def test_awaken_one_liner_references_image_family(bake):
    args = bake.build_parser().parse_args(
        ["--image-family", "jarvis-prime-coder", "--project", "p", "--zone", "z"]
    )
    line = bake.build_awaken_one_liner(args)
    assert "--image-family=jarvis-prime-coder" in line
    assert "gcloud compute instances create" in line


# --------------------------------------------------------------------------- #
# argparse defaults honor env vars.
# --------------------------------------------------------------------------- #
def test_argparse_defaults_honor_env(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT", "env-project-xyz")
    monkeypatch.setenv("JPRIME_BAKE_MODEL", "env-model:1b")
    monkeypatch.setenv("JPRIME_IMAGE_FAMILY", "env-family")
    mod = _load_module()  # re-load so module-level env defaults re-read
    args = mod.build_parser().parse_args([])
    assert args.project == "env-project-xyz"
    assert args.model == "env-model:1b"
    assert args.image_family == "env-family"


def test_argparse_default_is_dry_run(bake):
    args = bake.build_parser().parse_args([])
    assert args.dry_run is True
    args2 = bake.build_parser().parse_args(["--execute"])
    assert args2.dry_run is False


# --------------------------------------------------------------------------- #
# Dry-run never executes; execute does (monkeypatched _run boundary).
# --------------------------------------------------------------------------- #
def test_dry_run_does_not_invoke_run(bake, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(bake, "_run", lambda *a, **k: calls.append(a) or (0, ""))
    rc = bake.main(["--dry-run", "--project", "p", "--zone", "z"])
    assert rc == 0
    assert calls == []  # NOT called in dry-run
    out = capsys.readouterr().out
    assert "PLAN" in out
    assert "AWAKEN ONE-LINER" in out
    assert "nothing executed" in out.lower()


def test_execute_invokes_run_and_validates(bake, monkeypatch, tmp_path):
    calls = []
    _noop_autopsy_dir(bake, monkeypatch, tmp_path)

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        joined = " ".join(cmd)
        # image describe -> not found (so the idempotency guard passes)
        if "images" in cmd and "describe" in cmd:
            return 1, "NOT_FOUND"
        # readiness poll SSH -> ready
        if "ssh" in cmd and "JPRIME_READY" in joined:
            return 0, "JPRIME_READY\n"
        # validation SSH curl -> a valid generation
        if "ssh" in cmd and "api/generate" in joined:
            return 0, '{"response": "def is_even(n): return n % 2 == 0"}'
        return 0, ""

    monkeypatch.setattr(bake, "_run", fake_run)
    rc = bake.main(["--execute", "--project", "p", "--zone", "z",
                    "--node-name", "bake-node"])
    assert rc == 0
    # _run WAS called in execute mode.
    assert calls, "expected _run to be invoked in --execute"
    # provision + image create + cleanup delete all happened.
    flat = [" ".join(c) for c in calls]
    assert any("instances create bake-node" in f for f in flat)
    assert any("images create" in f for f in flat)
    assert any("instances delete bake-node" in f for f in flat)


def test_execute_aborts_and_does_not_snapshot_on_validation_fail(bake, monkeypatch, tmp_path):
    calls = []
    _noop_autopsy_dir(bake, monkeypatch, tmp_path)

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        joined = " ".join(cmd)
        if "images" in cmd and "describe" in cmd:
            return 1, "NOT_FOUND"
        if "ssh" in cmd and "JPRIME_READY" in joined:
            return 0, "JPRIME_READY\n"
        if "ssh" in cmd and "api/generate" in joined:
            return 0, '{"response": "I refuse."}'  # no 'def ' -> FAIL
        return 0, ""

    monkeypatch.setattr(bake, "_run", fake_run)
    rc = bake.main(["--execute", "--project", "p", "--zone", "z",
                    "--node-name", "bake-node"])
    assert rc == 6  # validation-fail exit code
    flat = [" ".join(c) for c in calls]
    # NEVER snapshotted a broken image.
    assert not any("images create" in f for f in flat)
    # BUT the node was still cleaned up (no orphaned billing).
    assert any("instances delete bake-node" in f for f in flat)


def test_execute_aborts_when_image_exists_without_force(bake, monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "images" in cmd and "describe" in cmd:
            return 0, "existing-image"  # image EXISTS
        return 0, ""

    monkeypatch.setattr(bake, "_run", fake_run)
    rc = bake.main(["--execute", "--project", "p", "--zone", "z",
                    "--node-name", "bake-node"])
    assert rc == 3  # image-exists guard
    flat = [" ".join(c) for c in calls]
    # never provisioned a node.
    assert not any("instances create" in f for f in flat)


# --------------------------------------------------------------------------- #
# NEW: Autopsy fires before teardown on all abort paths (capability 3).
# --------------------------------------------------------------------------- #

def test_autopsy_fires_before_teardown_on_readiness_timeout(bake, monkeypatch, tmp_path):
    """Autopsy SSH commands must precede the instances-delete call on timeout."""
    call_log: list = []
    _noop_autopsy_dir(bake, monkeypatch, tmp_path)

    def fake_run(cmd, **kwargs):
        call_log.append(list(cmd))
        joined = " ".join(cmd)
        if "images" in cmd and "describe" in cmd:
            return 1, "NOT_FOUND"
        # Sentinel check: always NOT_READY so timeout is hit.
        if "ssh" in cmd and "JPRIME_READY" in joined:
            return 0, "JPRIME_NOT_READY\n"
        # Bake log tail (progress + early-fail check): no error markers.
        if "ssh" in cmd and "tail" in joined and "jprime_bake.log" in joined:
            return 0, "[jprime-bake] pulling model qwen2.5-coder:7b\n"
        return 0, ""

    monkeypatch.setattr(bake, "_run", fake_run)
    # Use a very short timeout so the test is not slow.
    rc = bake.main(["--execute", "--project", "p", "--zone", "z",
                    "--node-name", "bake-node", "--bake-timeout-s", "1"])
    assert rc == 5  # readiness-timeout exit code

    flat = [" ".join(c) for c in call_log]
    # Autopsy SSH commands (cat jprime_bake.log, ollama list, etc.) must appear.
    autopsy_calls = [f for f in flat if "ssh" in f and (
        "jprime_bake.log" in f or "ollama list" in f or "systemctl" in f
        or "journalctl" in f or "df -h" in f or "api/tags" in f
        or "ollama_serve.log" in f
    )]
    delete_calls = [f for f in flat if "instances delete" in f]
    assert autopsy_calls, "expected autopsy SSH calls to be issued before teardown"
    assert delete_calls, "expected teardown (instances delete) to still run"

    # Assert ordering: ALL autopsy calls precede the first delete call.
    # Find the index of the first delete call.
    delete_idx = next(i for i, f in enumerate(flat) if "instances delete" in f)
    # Every autopsy SSH call must come before the delete call.
    for ac in autopsy_calls:
        ac_idx = next(i for i, f in enumerate(flat) if f == ac)
        assert ac_idx < delete_idx, (
            f"autopsy call '{ac}' appeared AFTER instances delete (idx {ac_idx} >= {delete_idx})"
        )


def test_autopsy_fires_before_teardown_on_validation_fail(bake, monkeypatch, tmp_path):
    """Autopsy must also precede teardown when validation fails (abort rc=6)."""
    call_log: list = []
    _noop_autopsy_dir(bake, monkeypatch, tmp_path)

    def fake_run(cmd, **kwargs):
        call_log.append(list(cmd))
        joined = " ".join(cmd)
        if "images" in cmd and "describe" in cmd:
            return 1, "NOT_FOUND"
        if "ssh" in cmd and "JPRIME_READY" in joined:
            return 0, "JPRIME_READY\n"
        if "ssh" in cmd and "api/generate" in joined:
            return 0, '{"response": "I refuse."}'  # validation FAIL
        return 0, ""

    monkeypatch.setattr(bake, "_run", fake_run)
    rc = bake.main(["--execute", "--project", "p", "--zone", "z",
                    "--node-name", "bake-node"])
    assert rc == 6  # validation-fail exit code

    flat = [" ".join(c) for c in call_log]
    autopsy_calls = [f for f in flat if "ssh" in f and (
        "jprime_bake.log" in f or "ollama list" in f or "systemctl" in f
        or "df -h" in f or "api/tags" in f
    )]
    delete_calls = [f for f in flat if "instances delete" in f]
    assert autopsy_calls, "autopsy SSH calls must fire on validation-fail abort"
    assert delete_calls, "teardown must still run after autopsy"

    delete_idx = next(i for i, f in enumerate(flat) if "instances delete" in f)
    for ac in autopsy_calls:
        ac_idx = next(i for i, f in enumerate(flat) if f == ac)
        assert ac_idx < delete_idx, (
            f"autopsy call appeared AFTER teardown (idx {ac_idx} >= {delete_idx})"
        )


def test_early_failure_detection_aborts_without_full_timeout(bake, monkeypatch, tmp_path):
    """When the bake log contains an ERROR marker, abort immediately (rc=5)."""
    call_log: list = []
    _noop_autopsy_dir(bake, monkeypatch, tmp_path)

    sentinel_attempts = [0]

    def fake_run(cmd, **kwargs):
        call_log.append(list(cmd))
        joined = " ".join(cmd)
        if "images" in cmd and "describe" in cmd:
            return 1, "NOT_FOUND"
        # Sentinel check: NOT_READY.
        if "ssh" in cmd and "JPRIME_READY" in joined:
            sentinel_attempts[0] += 1
            return 0, "JPRIME_NOT_READY\n"
        # Bake log tail: returns an ERROR marker immediately.
        if "ssh" in cmd and "tail" in joined and "jprime_bake.log" in joined:
            return 0, "[jprime-bake] ERROR: ollama pull failed\n"
        return 0, ""

    monkeypatch.setattr(bake, "_run", fake_run)
    # Long timeout -- the test must NOT take 300s; the early-fail must cut it.
    rc = bake.main(["--execute", "--project", "p", "--zone", "z",
                    "--node-name", "bake-node", "--bake-timeout-s", "300"])
    assert rc == 5  # early-fail + readiness abort share exit code 5

    # Should have short-circuited after at most 1-2 sentinel checks.
    assert sentinel_attempts[0] <= 2, (
        f"expected early abort after <=2 sentinel checks, got {sentinel_attempts[0]}"
    )


def test_live_progress_poll_issues_log_tail_ssh(bake, monkeypatch, tmp_path):
    """Each readiness poll that gets NOT_READY must also issue a tail SSH."""
    tail_calls: list = []
    _noop_autopsy_dir(bake, monkeypatch, tmp_path)

    ready_calls = [0]

    def fake_run(cmd, **kwargs):
        joined = " ".join(cmd)
        if "images" in cmd and "describe" in cmd:
            return 1, "NOT_FOUND"
        if "ssh" in cmd and "JPRIME_READY" in joined:
            ready_calls[0] += 1
            if ready_calls[0] >= 2:
                return 0, "JPRIME_READY\n"   # ready on 2nd attempt
            return 0, "JPRIME_NOT_READY\n"   # 1st attempt: not ready
        if "ssh" in cmd and "tail" in joined and "jprime_bake.log" in joined:
            tail_calls.append(cmd)
            return 0, "[jprime-bake] pulling model\n"
        if "ssh" in cmd and "api/generate" in joined:
            return 0, '{"response": "def is_even(n): return n % 2 == 0"}'
        return 0, ""

    monkeypatch.setattr(bake, "_run", fake_run)
    rc = bake.main(["--execute", "--project", "p", "--zone", "z",
                    "--node-name", "bake-node"])
    assert rc == 0
    # At least one tail call was issued (for the NOT_READY attempt).
    assert tail_calls, "expected at least one tail-log SSH call during the NOT_READY poll"


def test_successful_bake_does_not_write_autopsy(bake, monkeypatch, tmp_path):
    """A successful bake must NOT produce any autopsy file."""
    autopsy_dir = tmp_path / "autopsy_reports"
    _noop_autopsy_dir(bake, monkeypatch, tmp_path)

    def fake_run(cmd, **kwargs):
        joined = " ".join(cmd)
        if "images" in cmd and "describe" in cmd:
            return 1, "NOT_FOUND"
        if "ssh" in cmd and "JPRIME_READY" in joined:
            return 0, "JPRIME_READY\n"
        if "ssh" in cmd and "api/generate" in joined:
            return 0, '{"response": "def is_even(n): return n % 2 == 0"}'
        return 0, ""

    monkeypatch.setattr(bake, "_run", fake_run)
    rc = bake.main(["--execute", "--project", "p", "--zone", "z",
                    "--node-name", "bake-node"])
    assert rc == 0
    # autopsy dir must either not exist or be empty.
    if autopsy_dir.exists():
        reports = list(autopsy_dir.glob("*.log"))
        assert reports == [], f"unexpected autopsy files on success: {reports}"
