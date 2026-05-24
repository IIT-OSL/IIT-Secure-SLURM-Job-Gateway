# tests/test_jobs.py
import getpass
from pathlib import Path
import pytest
from iitgpu.jobs import JobSpec, make_job_folder, render_sbatch, write_sbatch


def _spec(**kwargs) -> JobSpec:
    defaults = dict(
        job_name="train_model",
        partition="gpu-short",
        gpus=2,
        cpus=8,
        mem_gb=32,
        time_limit="04:00:00",
        run_command="python train.py --epochs 10",
        modules=["CUDA/11.8", "Python/3.11"],
        uploads=[],
    )
    defaults.update(kwargs)
    return JobSpec(**defaults)


def test_make_job_folder_user_dir(tmp_path):
    folder = make_job_folder(str(tmp_path), _spec())
    assert Path(folder).parent.name == getpass.getuser()


def test_make_job_folder_name_prefix(tmp_path):
    folder = make_job_folder(str(tmp_path), _spec(job_name="mytest"))
    assert Path(folder).name.startswith("mytest_")


def test_make_job_folder_creates_directory(tmp_path):
    folder = make_job_folder(str(tmp_path), _spec())
    assert Path(folder).is_dir()


def test_render_sbatch_shebang(tmp_path):
    folder = make_job_folder(str(tmp_path), _spec())
    assert render_sbatch(_spec(), folder).startswith("#!/bin/bash")


def test_render_sbatch_job_name(tmp_path):
    folder = make_job_folder(str(tmp_path), _spec(job_name="mytest"))
    assert "#SBATCH --job-name=mytest" in render_sbatch(_spec(job_name="mytest"), folder)


def test_render_sbatch_gres_gpu(tmp_path):
    folder = make_job_folder(str(tmp_path), _spec(gpus=3))
    assert "#SBATCH --gres=gpu:3" in render_sbatch(_spec(gpus=3), folder)


def test_render_sbatch_cpus(tmp_path):
    folder = make_job_folder(str(tmp_path), _spec(cpus=16))
    assert "#SBATCH --cpus-per-task=16" in render_sbatch(_spec(cpus=16), folder)


def test_render_sbatch_mem(tmp_path):
    folder = make_job_folder(str(tmp_path), _spec(mem_gb=64))
    assert "#SBATCH --mem=64G" in render_sbatch(_spec(mem_gb=64), folder)


def test_render_sbatch_time(tmp_path):
    folder = make_job_folder(str(tmp_path), _spec(time_limit="08:00:00"))
    assert "#SBATCH --time=08:00:00" in render_sbatch(_spec(time_limit="08:00:00"), folder)


def test_render_sbatch_partition(tmp_path):
    folder = make_job_folder(str(tmp_path), _spec(partition="gpu-long"))
    assert "#SBATCH --partition=gpu-long" in render_sbatch(_spec(partition="gpu-long"), folder)


def test_render_sbatch_run_command(tmp_path):
    folder = make_job_folder(str(tmp_path), _spec(run_command="python train.py"))
    assert "python train.py" in render_sbatch(_spec(run_command="python train.py"), folder)


def test_render_sbatch_module_loads(tmp_path):
    folder = make_job_folder(str(tmp_path), _spec())
    script = render_sbatch(_spec(), folder)
    assert "module load CUDA/11.8" in script
    assert "module load Python/3.11" in script


def test_write_sbatch_creates_file(tmp_path):
    spec = _spec()
    folder = make_job_folder(str(tmp_path), spec)
    path = write_sbatch(spec, folder)
    assert Path(path).exists()
    assert Path(path).read_text().startswith("#!/bin/bash")
