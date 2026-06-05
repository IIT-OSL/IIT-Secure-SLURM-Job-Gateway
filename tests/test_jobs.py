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


# ── notebook-as-batch-job (.ipynb execution) ───────────────────────────────────

def test_resource_defaults_notebook_script():
    from iitgpu.jobs import resource_defaults
    d = resource_defaults("notebook-script")
    assert d.gpus == 1 and d.time_limit  # has a concrete (non-empty) time limit


def test_notebook_run_command_executes_via_nbconvert():
    from iitgpu.jobs import notebook_run_command
    cmd = notebook_run_command("/shared/users/alice/analysis.ipynb")
    assert "jupyter nbconvert --to notebook --execute" in cmd
    assert "--output executed.ipynb" in cmd
    assert "--output-dir ." in cmd
    assert "/shared/users/alice/analysis.ipynb" in cmd
    # also renders an HTML copy for easy viewing
    assert "--to html" in cmd


def test_notebook_run_command_self_heals_jupyter_outside_container():
    from iitgpu.jobs import notebook_run_command
    cmd = notebook_run_command("/shared/users/alice/nb.ipynb")
    assert "command -v jupyter" in cmd
    assert "pip install --user" in cmd
    assert "nbconvert" in cmd
    assert 'export PATH="$HOME/.local/bin:$PATH"' in cmd


def test_notebook_run_command_no_pip_install_in_container():
    from iitgpu.jobs import notebook_run_command
    cmd = notebook_run_command("/shared/users/alice/nb.ipynb", in_container=True)
    assert "pip install --user" not in cmd
    # but still actually executes the notebook
    assert "jupyter nbconvert --to notebook --execute" in cmd


def test_notebook_run_command_quotes_path_with_spaces():
    from iitgpu.jobs import notebook_run_command
    cmd = notebook_run_command("/shared/users/alice/my notebook.ipynb")
    assert "'/shared/users/alice/my notebook.ipynb'" in cmd


def test_notebook_run_command_installs_requirements_before_running():
    from iitgpu.jobs import notebook_run_command
    cmd = notebook_run_command("/u/nb.ipynb", requirements="/u/proj/requirements.txt")
    assert "pip install --user" in cmd
    assert "-r /u/proj/requirements.txt" in cmd
    # deps must be installed BEFORE the notebook executes
    assert cmd.index("-r /u/proj/requirements.txt") < cmd.index("nbconvert --to notebook --execute")


def test_notebook_run_command_installs_packages():
    from iitgpu.jobs import notebook_run_command
    cmd = notebook_run_command("/u/nb.ipynb", packages="tqdm wfdb==4.1 h5py")
    assert "pip install --user" in cmd
    assert "tqdm" in cmd and "wfdb==4.1" in cmd and "h5py" in cmd


def test_notebook_run_command_quotes_unsafe_package_token():
    """Even if a junk token slips through, it is shell-quoted (one pip arg) so it
    cannot inject into the job script."""
    from iitgpu.jobs import notebook_run_command
    cmd = notebook_run_command("/u/nb.ipynb", packages="foo;rm")
    assert "'foo;rm'" in cmd


def test_notebook_run_command_no_upfront_dep_install_when_unspecified():
    from iitgpu.jobs import notebook_run_command
    cmd = notebook_run_command("/u/nb.ipynb", auto_install=False)
    assert "Installing dependencies" not in cmd  # no up-front pip block


def test_notebook_run_command_auto_install_loop_is_default():
    """Default: the run is wrapped in a retry loop that auto-installs missing
    modules (incl. transitive ones like tensorboard) via an alias table."""
    from iitgpu.jobs import notebook_run_command
    cmd = notebook_run_command("/u/nb.ipynb")
    assert "_iit_pkg_for" in cmd                       # alias function present
    assert "No module named '" in cmd                  # detects the missing module (ANSI-safe)
    assert "opencv-python" in cmd and "scikit-learn" in cmd  # alias entries
    assert "protobuf" in cmd                           # google -> protobuf alias (the SummaryWriter dep)
    assert "${_iit_miss%%.*}" in cmd                   # maps the TOP-LEVEL import (google.protobuf -> google)
    assert "pip install --user" in cmd                # installs the missing dep


def test_notebook_run_command_auto_install_can_be_disabled():
    from iitgpu.jobs import notebook_run_command
    cmd = notebook_run_command("/u/nb.ipynb", auto_install=False)
    assert "_iit_pkg_for" not in cmd
    assert 'Notebook execution FAILED' in cmd          # plain single run
    assert "jupyter nbconvert --to notebook --execute" in cmd


def test_notebook_run_command_pins_env_kernel():
    """Execution must be pinned to a kernel built from the active env's python
    (registered per-job) so env-only packages like pandas are importable —
    otherwise nbconvert may pick a stray python3 kernel that can't see them."""
    from iitgpu.jobs import notebook_run_command
    cmd = notebook_run_command("/u/nb.ipynb")
    assert "ipykernel install --user" in cmd
    assert "iit-nb-${SLURM_JOB_ID:-$$}" in cmd          # per-job kernel name
    assert '--ExecutePreprocessor.kernel_name="$_IIT_KERNEL"' in cmd


def test_notebook_run_command_container_does_not_pin_kernel():
    from iitgpu.jobs import notebook_run_command
    cmd = notebook_run_command("/u/nb.ipynb", in_container=True)
    assert "ipykernel install" not in cmd
    assert "kernel_name" not in cmd


def test_notebook_run_command_streams_cells_via_papermill():
    """Primary engine is papermill with --log-output so each cell's stdout
    streams to the job log live (the job no longer looks hung during training)."""
    from iitgpu.jobs import notebook_run_command
    cmd = notebook_run_command("/u/nb.ipynb")
    assert "papermill --log-output" in cmd
    assert '-k "$_IIT_KERNEL"' in cmd                  # reuses the per-job kernel
    # nbconvert is kept as a runtime fallback when papermill is unavailable
    assert "command -v papermill" in cmd
    assert "jupyter nbconvert --to notebook --execute" in cmd


def test_notebook_run_command_self_heals_papermill_outside_container():
    from iitgpu.jobs import notebook_run_command
    cmd = notebook_run_command("/u/nb.ipynb")
    assert "command -v papermill" in cmd
    assert "pip install --user --quiet --no-warn-script-location papermill" in cmd


def test_notebook_run_command_no_papermill_install_in_container():
    """Container images must already ship papermill/jupyter — no ~/.local install."""
    from iitgpu.jobs import notebook_run_command
    cmd = notebook_run_command("/u/nb.ipynb", in_container=True)
    assert "install --user --quiet --no-warn-script-location papermill" not in cmd
    # container still has a papermill→nbconvert runtime switch, just no self-heal
    assert "command -v papermill" in cmd


def test_notebook_run_command_has_progress_heartbeat():
    """A background heartbeat keeps the job log advancing during a quiet cell so
    a healthy run is not mistaken for a hung one."""
    from iitgpu.jobs import notebook_run_command
    cmd = notebook_run_command("/u/nb.ipynb")
    assert "_iit_heartbeat" in cmd
    assert "still executing" in cmd
    assert "_IIT_HB_PID" in cmd                         # started in background and later killed


def test_notebook_run_command_heartbeat_present_in_container():
    from iitgpu.jobs import notebook_run_command
    cmd = notebook_run_command("/u/nb.ipynb", in_container=True)
    assert "_iit_heartbeat" in cmd


def test_render_notebook_sbatch_installs_requirements(tmp_path):
    from iitgpu.jobs import render_notebook_sbatch
    folder = make_job_folder(str(tmp_path), _spec())
    script = render_notebook_sbatch(_spec(), folder, requirements="/u/r.txt")
    assert "pip install --user" in script and "-r /u/r.txt" in script


def test_render_notebook_sbatch_installs_packages(tmp_path):
    from iitgpu.jobs import render_notebook_sbatch
    folder = make_job_folder(str(tmp_path), _spec())
    script = render_notebook_sbatch(_spec(), folder, packages="tensorboard tqdm")
    assert "pip install --user" in script
    assert "tensorboard" in script and "tqdm" in script


def test_render_sbatch_runs_notebook_command_with_env(tmp_path):
    """A notebook-script job reuses render_sbatch: conda activation + the
    nbconvert execution must both land in the generated script."""
    from iitgpu.jobs import notebook_run_command
    folder = make_job_folder(str(tmp_path), _spec())
    run_cmd = notebook_run_command("/shared/users/alice/nb.ipynb")
    script = render_sbatch(
        _spec(run_command=run_cmd, conda_env="/shared/envs/data-science",
              task_type="notebook-script"),
        folder,
    )
    assert "conda activate /shared/envs/data-science" in script
    assert "jupyter nbconvert --to notebook --execute" in script
