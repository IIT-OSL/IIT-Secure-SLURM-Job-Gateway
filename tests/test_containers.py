# tests/test_containers.py
"""Tests for iitgpu/containers.py and container branch of render_sbatch."""
import os
from pathlib import Path
from unittest.mock import patch
import pytest


# ── containers module ─────────────────────────────────────────────────────────

def test_list_images_empty_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    from iitgpu.containers import list_images
    result = list_images(str(tmp_path))
    assert result == []


def test_list_images_returns_sif_files(tmp_path, monkeypatch):
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    (images_dir / "llm-finetune.sif").write_text("")
    (images_dir / "vision.sif").write_text("")
    (images_dir / "README.txt").write_text("")  # should be excluded

    from iitgpu.containers import list_images
    result = list_images(str(tmp_path))
    assert len(result) == 2
    assert all(r.endswith(".sif") for r in result)
    assert "README.txt" not in " ".join(result)


def test_list_images_sorted(tmp_path):
    images_dir = tmp_path / "images"
    images_dir.mkdir()
    for name in ["z-model.sif", "a-model.sif", "m-model.sif"]:
        (images_dir / name).write_text("")
    from iitgpu.containers import list_images
    result = list_images(str(tmp_path))
    assert result == sorted(result)


def test_validate_image_rejects_outside_jail(tmp_path):
    from iitgpu.containers import validate_image
    with patch("iitgpu.containers.in_jail", return_value=False):
        assert validate_image("/etc/passwd.sif") is False


def test_validate_image_rejects_non_sif():
    from iitgpu.containers import validate_image
    with patch("iitgpu.containers.in_jail", return_value=True):
        assert validate_image("/shared/images/model.tar.gz") is False


def test_validate_image_accepts_valid_sif():
    from iitgpu.containers import validate_image
    with patch("iitgpu.containers.in_jail", return_value=True):
        assert validate_image("/shared/images/llm-finetune.sif") is True


def test_render_apptainer_wrap_contains_nv_flag():
    from iitgpu.containers import render_apptainer_wrap
    result = render_apptainer_wrap("/shared/images/vision.sif", "python train.py")
    assert "--nv" in result
    assert "--bind /shared" in result
    assert "/shared/images/vision.sif" in result


# ── render_sbatch container branch ────────────────────────────────────────────

def test_render_sbatch_container_uses_apptainer(tmp_path):
    from iitgpu.jobs import JobSpec, make_job_folder, render_sbatch

    spec = JobSpec(
        job_name="test_cont",
        partition="gpu",
        gpus=1,
        cpus=8,
        mem_gb=32,
        time_limit="04:00:00",
        run_command="python train.py",
        container_image="/shared/images/vision.sif",
    )
    folder = make_job_folder(str(tmp_path), spec)
    script = render_sbatch(spec, folder)
    assert "apptainer exec --nv --bind /shared" in script
    assert "/shared/images/vision.sif" in script
    assert "python train.py" in script


def test_render_sbatch_container_omits_conda_activation(tmp_path):
    from iitgpu.jobs import JobSpec, make_job_folder, render_sbatch

    spec = JobSpec(
        job_name="test_cont",
        partition="gpu",
        gpus=1,
        cpus=4,
        mem_gb=16,
        time_limit="01:00:00",
        run_command="python infer.py",
        container_image="/shared/images/llm-serve.sif",
        conda_env="/shared/envs/pytorch-2.7",  # should be ignored
    )
    folder = make_job_folder(str(tmp_path), spec)
    script = render_sbatch(spec, folder)
    assert "apptainer exec --nv" in script
    assert "conda activate" not in script


def test_render_sbatch_no_container_still_works(tmp_path):
    """Ensure the non-container path is unaffected."""
    from iitgpu.jobs import JobSpec, make_job_folder, render_sbatch

    spec = JobSpec(
        job_name="plain_job",
        partition="gpu",
        gpus=1,
        cpus=4,
        mem_gb=16,
        time_limit="01:00:00",
        run_command="python train.py",
        conda_env="/shared/envs/pytorch-2.7",
    )
    folder = make_job_folder(str(tmp_path), spec)
    script = render_sbatch(spec, folder)
    assert "apptainer" not in script
    assert "conda activate" in script


def test_jobspec_container_image_default_is_empty():
    from iitgpu.jobs import JobSpec
    spec = JobSpec(
        job_name="j", partition="gpu", gpus=1, cpus=1, mem_gb=4,
        time_limit="", run_command="echo hi",
    )
    assert spec.container_image == ""
