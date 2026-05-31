# tests/test_submit_completeness.py
"""Phase 2: job arrays, dependencies, interactive sessions, QOS validation."""
import tempfile
from pathlib import Path
import pytest
from iitgpu.jobs import JobSpec, make_job_folder, render_sbatch, build_interactive_cmd


def _spec(**kw):
    base = dict(job_name="j", partition="gpu", gpus=1, cpus=4, mem_gb=8,
                time_limit="01:00:00", run_command="python x.py")
    base.update(kw)
    return JobSpec(**base)


# ── Job arrays ─────────────────────────────────────────────────────────────────

def test_render_sbatch_emits_array_directive(tmp_path):
    spec = _spec(array="0-9")
    folder = make_job_folder(str(tmp_path), spec)
    script = render_sbatch(spec, folder)
    assert "#SBATCH --array=0-9" in script


def test_array_uses_per_task_output_filenames(tmp_path):
    spec = _spec(array="1-100%4")
    folder = make_job_folder(str(tmp_path), spec)
    script = render_sbatch(spec, folder)
    assert "slurm-%A_%a.out" in script
    assert "slurm-%A_%a.err" in script


def test_no_array_uses_jobid_output(tmp_path):
    spec = _spec()
    folder = make_job_folder(str(tmp_path), spec)
    script = render_sbatch(spec, folder)
    assert "slurm-%j.out" in script
    assert "%A_%a" not in script


# ── Dependencies ───────────────────────────────────────────────────────────────

def test_render_sbatch_emits_dependency(tmp_path):
    spec = _spec(dependency="afterok:12345")
    folder = make_job_folder(str(tmp_path), spec)
    script = render_sbatch(spec, folder)
    assert "#SBATCH --dependency=afterok:12345" in script


def test_no_dependency_omits_directive(tmp_path):
    spec = _spec()
    folder = make_job_folder(str(tmp_path), spec)
    assert "--dependency" not in render_sbatch(spec, folder)


# ── Interactive ────────────────────────────────────────────────────────────────

def test_build_interactive_cmd_is_srun_pty():
    cmd = build_interactive_cmd(_spec(gpus=1, cpus=8, mem_gb=16, time_limit="02:00:00"))
    assert cmd[0] == "srun"
    assert "--pty" in cmd
    assert "--gres=gpu:1" in cmd
    assert "--cpus-per-task=8" in cmd
    assert "--mem=16G" in cmd
    assert "--time=02:00:00" in cmd
    assert cmd[-2:] == ["bash", "-l"]


def test_interactive_cmd_omits_time_when_unset():
    cmd = build_interactive_cmd(_spec(time_limit=""))
    assert not any(c.startswith("--time=") for c in cmd)


# ── Validators ─────────────────────────────────────────────────────────────────

def test_clean_array_spec_valid():
    from iitgpu.validate import clean_array_spec
    assert clean_array_spec("0-9") == "0-9"
    assert clean_array_spec("1-100%4") == "1-100%4"
    assert clean_array_spec("1,3,5") == "1,3,5"


def test_clean_array_spec_rejects_garbage():
    from iitgpu.validate import clean_array_spec
    assert clean_array_spec("rm -rf /") is None
    assert clean_array_spec("") is None
    assert clean_array_spec("abc") is None


def test_clean_dependency_valid():
    from iitgpu.validate import clean_dependency
    assert clean_dependency("afterok:123") == "afterok:123"
    assert clean_dependency("afterany:1:2:3") == "afterany:1:2:3"
    assert clean_dependency("singleton") == "singleton"


def test_clean_dependency_rejects_garbage():
    from iitgpu.validate import clean_dependency
    assert clean_dependency("afterok:123; rm -rf /") is None
    assert clean_dependency("badtype:1") is None
    assert clean_dependency("") is None


def test_validate_against_qos_rejects_too_many_gpus():
    from iitgpu.validate import validate_against_qos
    ok, msg = validate_against_qos(gpus=2, time_limit="01:00:00", max_gpus_per_user=1)
    assert ok is False
    assert "GPU" in msg


def test_validate_against_qos_rejects_over_walltime():
    from iitgpu.validate import validate_against_qos
    ok, msg = validate_against_qos(gpus=1, time_limit="48:00:00", max_gpus_per_user=1, max_hours=8)
    assert ok is False
    assert "wall-time" in msg.lower()


def test_validate_against_qos_accepts_in_policy():
    from iitgpu.validate import validate_against_qos
    ok, _ = validate_against_qos(gpus=1, time_limit="04:00:00", max_gpus_per_user=1, max_hours=8)
    assert ok is True
