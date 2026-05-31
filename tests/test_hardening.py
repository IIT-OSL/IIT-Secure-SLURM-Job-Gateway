# tests/test_hardening.py
"""Tests for Phase 7 hardening — job dir permissions, UPGRADE-RUNBOOK.md."""
import stat
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).parent.parent


# ── Job directory permissions ─────────────────────────────────────────────────

def test_make_job_folder_uses_0o770(tmp_path):
    from iitgpu.jobs import JobSpec, make_job_folder

    spec = JobSpec(
        job_name="sec_test",
        partition="gpu",
        gpus=1,
        cpus=4,
        mem_gb=8,
        time_limit="00:30:00",
        run_command="echo hi",
    )
    folder = make_job_folder(str(tmp_path), spec)
    mode = stat.S_IMODE(Path(folder).stat().st_mode)
    # Must be 0o770 (rwxrwx---): no world permissions
    assert mode == 0o770, (
        f"Job folder has mode {oct(mode)} — expected 0o770 (rwxrwx---). "
        "Other users should not be able to read each other's job output."
    )


def test_make_job_folder_no_world_readable(tmp_path):
    from iitgpu.jobs import JobSpec, make_job_folder

    spec = JobSpec(
        job_name="sec_test2",
        partition="gpu",
        gpus=1,
        cpus=4,
        mem_gb=8,
        time_limit="",
        run_command="echo hi",
    )
    folder = make_job_folder(str(tmp_path), spec)
    mode = stat.S_IMODE(Path(folder).stat().st_mode)
    # World bits (r=4, w=2, x=1) must all be 0
    world_bits = mode & 0o007
    assert world_bits == 0, (
        f"Job folder has world bits {oct(world_bits)} set — users can read each other's jobs."
    )


# ── UPGRADE-RUNBOOK.md ────────────────────────────────────────────────────────

def test_upgrade_runbook_exists():
    runbook = REPO_ROOT / "deploy" / "UPGRADE-RUNBOOK.md"
    assert runbook.exists(), "deploy/UPGRADE-RUNBOOK.md is missing"


def test_upgrade_runbook_has_all_phases():
    runbook = REPO_ROOT / "deploy" / "UPGRADE-RUNBOOK.md"
    if not runbook.exists():
        pytest.skip("runbook missing")
    content = runbook.read_text()
    for phase in ["Phase 1", "Phase 2", "Phase 3", "Phase 4", "Phase 5", "Phase 6", "Phase 7"]:
        assert phase in content, f"UPGRADE-RUNBOOK.md is missing section for {phase}"


def test_upgrade_runbook_has_gpu_host_markers():
    runbook = REPO_ROOT / "deploy" / "UPGRADE-RUNBOOK.md"
    if not runbook.exists():
        pytest.skip("runbook missing")
    content = runbook.read_text()
    assert "[GPU-HOST]" in content, "UPGRADE-RUNBOOK.md has no [GPU-HOST] markers"


def test_upgrade_runbook_has_final_checklist():
    runbook = REPO_ROOT / "deploy" / "UPGRADE-RUNBOOK.md"
    if not runbook.exists():
        pytest.skip("runbook missing")
    content = runbook.read_text()
    assert "Final Checklist" in content


# ── CHANGES.md ────────────────────────────────────────────────────────────────

def test_changes_md_exists():
    assert (REPO_ROOT / "CHANGES.md").exists(), "CHANGES.md is missing"
