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
    # The SLURM job name is the full job-folder name (job_name + timestamp),
    # e.g. "mytest_20260601_045303", so each run is uniquely identifiable.
    folder = make_job_folder(str(tmp_path), _spec(job_name="mytest"))
    script = render_sbatch(_spec(job_name="mytest"), folder)
    assert f"#SBATCH --job-name={Path(folder).name}" in script
    assert Path(folder).name.startswith("mytest_")


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


def test_render_sbatch_chdir_set_to_folder(tmp_path):
    folder = make_job_folder(str(tmp_path), _spec())
    assert f"#SBATCH --chdir={folder}" in render_sbatch(_spec(), folder)


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


from iitgpu.jobs import TaskDefaults, resource_defaults, TASK_DEFAULTS


def test_resource_defaults_train():
    d = resource_defaults("train")
    assert d.gpus == 1
    assert d.cpus == 16
    assert d.mem_gb == 60
    assert d.time_limit == ""


def test_resource_defaults_inference():
    d = resource_defaults("inference")
    assert d.cpus == 8
    assert d.mem_gb == 32
    assert d.time_limit == "04:00:00"


def test_resource_defaults_test():
    d = resource_defaults("test")
    assert d.cpus == 4
    assert d.mem_gb == 16
    assert d.time_limit == "00:30:00"


def test_resource_defaults_unknown_falls_back_to_custom():
    d = resource_defaults("nonexistent_task")
    assert d == resource_defaults("custom")


def test_render_sbatch_empty_time_limit_omits_time_directive(tmp_path):
    spec = _spec(time_limit="")
    folder = make_job_folder(str(tmp_path), spec)
    script = render_sbatch(spec, folder)
    assert "#SBATCH --time=" not in script


def test_render_sbatch_nonempty_time_limit_includes_directive(tmp_path):
    spec = _spec(time_limit="02:00:00")
    folder = make_job_folder(str(tmp_path), spec)
    script = render_sbatch(spec, folder)
    assert "#SBATCH --time=02:00:00" in script


def test_render_sbatch_path_conda_env_uses_source_activate(tmp_path):
    spec = _spec(conda_env="/shared/envs/pytorch-2.5", modules=[])
    folder = make_job_folder(str(tmp_path), spec)
    script = render_sbatch(spec, folder)
    # Path-based conda envs now source conda.sh then use conda activate (not source .../bin/activate)
    assert "conda.sh" in script
    assert "conda activate /shared/envs/pytorch-2.5" in script


def test_render_sbatch_named_conda_env_uses_conda_activate(tmp_path):
    spec = _spec(conda_env="my-env", modules=[])
    folder = make_job_folder(str(tmp_path), spec)
    script = render_sbatch(spec, folder)
    assert "conda activate my-env" in script


def test_jobspec_has_task_type_default():
    spec = _spec()
    assert spec.task_type == "custom"


def test_jobspec_task_type_can_be_set():
    spec = _spec(task_type="train")
    assert spec.task_type == "train"
