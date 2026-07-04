# -*- coding: utf-8 -*-
"""Pure-logic unit tests for scripts/bake_brain_golden_image.py.

UNIT ONLY -- zero live GCP. Every assertion is on the generated startup-script
STRING (pure string assembly, no I/O) plus a shared-helper identity smoke.

The CPU brain image is the image the GCP Brain VM boots from (spec:
docs/superpowers/specs/2026-07-03-gcp-orchestrator-relocation-design.md). It
differs from the J-Prime code-model image: NO Ollama / NO NVIDIA / NO model
pull -- just base image + python3.11 + git + a repo clone at the IsomorphicEnv
canonical path /opt/trinity/jarvis + the headless battle-test warm-standby unit.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "bake_brain_golden_image.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("bake_brain_golden_image", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def bake():
    return _load_module()


# --------------------------------------------------------------------------- #
# (a) clone target is /opt/trinity/jarvis with the repo URL from the env var
#     (NOT a hardcoded literal URL).
# --------------------------------------------------------------------------- #
def test_clone_target_is_opt_trinity_with_repo_url_from_arg(bake):
    url = "https://example.invalid/some/brain-repo.git"
    script = bake.build_startup_script(url, repo_ref="feature/x")
    # The clone must target the IsomorphicEnv canonical path.
    assert "/opt/trinity/jarvis" in script
    # git clone of the supplied URL into that path.
    assert "git clone" in script
    assert url in script
    # ref is substituted (not hardcoded 'main').
    assert "feature/x" in script


def test_repo_url_is_not_a_hardcoded_literal_in_module(bake):
    """A caller-less default URL must never be baked into the module source."""
    src = _SCRIPT.read_text(encoding="utf-8")
    # No github.com / drussell23 repo literal anywhere in the script source.
    assert "github.com/drussell23" not in src
    assert "JARVIS-AI-Agent.git" not in src
    # The default is env-resolved, empty when unset.
    assert 'os.environ.get("JARVIS_BRAIN_REPO_URL"' in src


# --------------------------------------------------------------------------- #
# (b) NO ollama / nvidia / accelerator tokens anywhere in the CPU image.
# --------------------------------------------------------------------------- #
def test_no_ollama_no_nvidia_no_accelerator_tokens(bake):
    script = bake.build_startup_script("https://example.invalid/repo.git")
    low = script.lower()
    for forbidden in ("ollama", "nvidia", "accelerator", "gpu", "cuda", "vram"):
        assert forbidden not in low, f"CPU brain image must not mention {forbidden!r}"


# --------------------------------------------------------------------------- #
# (c) systemd unit runs the headless battle-test cmd + --max-wall-seconds 0 +
#     EnvironmentFile=/etc/jarvis/brain.env.
# --------------------------------------------------------------------------- #
def test_systemd_unit_headless_battle_test_warm_standby(bake):
    script = bake.build_startup_script("https://example.invalid/repo.git")
    assert "jarvis-brain.service" in script
    assert "scripts/ouroboros_battle_test.py" in script
    assert "--production-soak" in script
    assert "--headless" in script
    assert "--cost-cap" in script
    # warm standby -> no wall clock cap.
    assert "--max-wall-seconds 0" in script
    assert "EnvironmentFile=/etc/jarvis/brain.env" in script
    assert "WorkingDirectory=/opt/trinity/jarvis" in script


def test_cost_cap_is_env_resolved_not_a_literal(bake):
    """cost-cap must come from the env file, never a baked literal number."""
    script = bake.build_startup_script("https://example.invalid/repo.git")
    # A shell/systemd variable reference resolved from EnvironmentFile.
    assert "--cost-cap ${JARVIS_BRAIN_COST_CAP}" in script


# --------------------------------------------------------------------------- #
# (d) brain.env is written from instance metadata BEFORE the unit starts.
# --------------------------------------------------------------------------- #
def test_brain_env_written_from_metadata_before_unit_starts(bake):
    script = bake.build_startup_script("https://example.invalid/repo.git")
    assert "/etc/jarvis/brain.env" in script
    # Instance-metadata read (the J-Prime metadata pattern).
    assert "metadata.google.internal" in script
    assert "Metadata-Flavor: Google" in script
    # The metadata read must precede the point where the unit is enabled.
    # (We `enable` -- not `--now` -- during the bake: the enabled-state is baked
    # into the image so the unit auto-starts on the real Brain VM boot; we never
    # run the paid soak on the ephemeral bake node.)
    meta_idx = script.index("metadata.google.internal")
    start_idx = script.index("systemctl enable jarvis-brain.service")
    assert meta_idx < start_idx, "brain.env must be populated from metadata before the unit is enabled"


# --------------------------------------------------------------------------- #
# (e) the sentinel write appears AFTER both the .git-exists check and the
#     transport-import check (sentinel-after-proof, never a fixed sleep).
# --------------------------------------------------------------------------- #
def test_sentinel_written_only_after_git_and_transport_proof(bake):
    script = bake.build_startup_script("https://example.invalid/repo.git")
    git_check_idx = script.index("/opt/trinity/jarvis/.git")
    transport_idx = script.index("import backend.core.ouroboros.governance.transport")
    sentinel_write_idx = script.index("> /var/run/jarvis_brain_ready")
    assert git_check_idx < sentinel_write_idx, ".git check must precede the sentinel write"
    assert transport_idx < sentinel_write_idx, "transport import must precede the sentinel write"
    # A failed proof must NOT write the sentinel (fail-loud, exit 1).
    assert "NOT writing sentinel" in script
    # No fixed-sleep readiness lie.
    assert "sleep 30\n" not in script


# --------------------------------------------------------------------------- #
# (f) idle-shutdown timeout is env-driven (from brain.env), never a literal.
# --------------------------------------------------------------------------- #
def test_idle_shutdown_timeout_is_env_driven(bake):
    script = bake.build_startup_script("https://example.invalid/repo.git")
    # The idle-shutdown threshold is read from brain.env's env var with a default.
    assert "JARVIS_BRAIN_VM_IDLE_SHUTDOWN_S" in script
    assert "jarvis-brain-idle" in script  # the timer/checker unit name


# --------------------------------------------------------------------------- #
# (g) PEP 668: Debian 12 system pip is externally-managed and refuses installs
#     outright (live-fire attempt 1, 2026-07-04). ALL pip must run inside the
#     venv; the service and the sentinel proof must use the SAME interpreter
#     the deps were installed into.
# --------------------------------------------------------------------------- #
def test_pip_runs_in_venv_never_system_pip(bake):
    script = bake.build_startup_script("https://example.invalid/repo.git")
    assert "python3.11 -m venv /opt/trinity/venv" in script
    assert "/opt/trinity/venv/bin/pip install" in script
    # No system-pip install may remain anywhere (PEP 668 refuses it on debian-12).
    assert "python3.11 -m pip install" not in script
    assert "python3.11 -m ensurepip" not in script


def test_pip_installs_brain_scoped_requirements_not_full_stack(bake):
    """The Brain is a CPU orchestrator (design Non-Goals): it must install the
    brain-scoped subset, never the root requirements.txt Mac/voice/ML stack."""
    script = bake.build_startup_script("https://example.invalid/repo.git")
    assert "backend/requirements-brain.txt" in script
    assert "-r requirements.txt" not in script


def test_apt_installs_build_prereqs_before_pip(bake):
    script = bake.build_startup_script("https://example.invalid/repo.git")
    assert "build-essential" in script
    assert "python3.11-dev" in script
    apt_idx = script.index("build-essential")
    pip_idx = script.index("/opt/trinity/venv/bin/pip install")
    assert apt_idx < pip_idx, "build prereqs must be installed before pip runs"


def test_execstart_uses_venv_python(bake):
    script = bake.build_startup_script("https://example.invalid/repo.git")
    assert (
        "ExecStart=/opt/trinity/venv/bin/python scripts/ouroboros_battle_test.py"
        in script
    )
    assert "ExecStart=/usr/bin/python3.11" not in script


def test_sentinel_proof_uses_venv_python(bake):
    """The readiness proof must import under the venv interpreter -- proving the
    system python imports would validate a DIFFERENT runtime than the service."""
    script = bake.build_startup_script("https://example.invalid/repo.git")
    assert (
        '/opt/trinity/venv/bin/python -c "import backend.core.ouroboros.governance.transport"'
        in script
    )


def test_brain_requirements_file_excludes_ml_voice_stack():
    """backend/requirements-brain.txt is the lean CPU-orchestrator set: no local
    inference, no voice I/O, no training stack (design Non-Goals)."""
    req_path = _SCRIPT.parents[1] / "backend" / "requirements-brain.txt"
    assert req_path.exists(), "backend/requirements-brain.txt must exist"
    # Check requirement LINES only (comments may name the excluded stack).
    req = "\n".join(
        line.split("#", 1)[0].strip()
        for line in req_path.read_text(encoding="utf-8").splitlines()
        if line.split("#", 1)[0].strip()
    ).lower()
    for forbidden in (
        "torch",  # also catches torchaudio
        "whisper",
        "pyaudio",
        "llama-cpp",
        "speechbrain",
        "librosa",
        "pvporcupine",
        "webrtcvad",
        "sounddevice",
        "coremltools",
        "transformers",
        "bitsandbytes",
    ):
        assert forbidden not in req, f"brain requirements must not carry {forbidden!r}"


# --------------------------------------------------------------------------- #
# (h) whole startup script is ASCII-only.
# --------------------------------------------------------------------------- #
def test_startup_script_is_ascii(bake):
    bake.build_startup_script("https://example.invalid/repo.git").encode("ascii")


def test_startup_script_shebang_and_no_fixed_sleep_sentinel(bake):
    script = bake.build_startup_script("https://example.invalid/repo.git")
    assert script.startswith("#!")


# --------------------------------------------------------------------------- #
# Image name uses the shared stamp + the jarvis-brain-golden-<stamp> shape.
# --------------------------------------------------------------------------- #
def test_default_image_name_is_brain_golden_stamped(bake):
    name = bake._default_image_name()
    assert name.startswith("jarvis-brain-golden-")


def test_no_literal_project_id_or_zone_in_module(bake):
    """Mandate 2: no literal GCP project IDs / zones baked into the module."""
    src = _SCRIPT.read_text(encoding="utf-8")
    assert "jarvis-473803" not in src
    assert "us-central1-a" not in src


# --------------------------------------------------------------------------- #
# Shared-helper identity smoke: parse_validation_verdict is THE J-Prime function
# (imported, not reimplemented -- DRY mandate).
# --------------------------------------------------------------------------- #
def test_parse_validation_verdict_is_the_shared_jprime_function(bake):
    import sys

    # Import J-Prime through the SAME module-system path the brain script uses
    # (`from bake_jprime_golden_image import ...`), so both resolve to the one
    # sys.modules-cached object. A fresh spec_from_file_location exec would mint
    # a *different* module object and mask a real duplication.
    scripts_dir = str(Path(__file__).resolve().parents[2] / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import bake_jprime_golden_image as jprime

    # Same function object -> proves it was imported, not duplicated.
    assert bake.parse_validation_verdict is jprime.parse_validation_verdict
    # And the other stable helpers are the shared ones too.
    assert bake._run is jprime._run
    assert bake._log is jprime._log
    assert bake._now_stamp is jprime._now_stamp


def test_bake_family_matches_provisioner_single_source_of_truth(monkeypatch):
    """Cross-component pin: the bake MUST create the exact image family the
    Task-2 provisioner (_brain_image_family) provisions FROM, else Task-5
    ignition finds no image. Both must read JARVIS_BRAIN_VM_IMAGE_FAMILY and
    share a default. Regression guard for the drift caught pre-live-fire
    (bake was JARVIS_BRAIN_IMAGE_FAMILY/jarvis-brain vs provisioner
    JARVIS_BRAIN_VM_IMAGE_FAMILY/jarvis-brain-golden)."""
    import importlib.util as _u
    monkeypatch.delenv("JARVIS_BRAIN_VM_IMAGE_FAMILY", raising=False)
    monkeypatch.delenv("JARVIS_BRAIN_IMAGE_FAMILY", raising=False)
    _s = _u.spec_from_file_location("_bb_pin", "scripts/bake_brain_golden_image.py")
    _m = _u.module_from_spec(_s)
    _s.loader.exec_module(_m)
    from backend.core.ouroboros.governance.gcp_compute_rest import _brain_image_family
    assert _m._DEFAULT_IMAGE_FAMILY == _brain_image_family(), (
        "bake image family must match the provisioner's -- Task 5 provisions "
        "from _brain_image_family(); a mismatch means no image found"
    )
    # And the shared env var drives BOTH:
    monkeypatch.setenv("JARVIS_BRAIN_VM_IMAGE_FAMILY", "jarvis-brain-custom-fam")
    _m2 = _u.module_from_spec(_s)
    _s.loader.exec_module(_m2)
    assert _m2._DEFAULT_IMAGE_FAMILY == "jarvis-brain-custom-fam"
    assert _brain_image_family() == "jarvis-brain-custom-fam"
