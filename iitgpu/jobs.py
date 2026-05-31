# iitgpu/jobs.py
from __future__ import annotations
import getpass
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class TaskDefaults:
    gpus: int
    cpus: int
    mem_gb: int
    time_limit: str  # "" means no time limit (SLURM INFINITE)


TASK_DEFAULTS: dict[str, TaskDefaults] = {
    "train":     TaskDefaults(gpus=1, cpus=16, mem_gb=60, time_limit=""),
    "finetune":  TaskDefaults(gpus=1, cpus=16, mem_gb=60, time_limit=""),
    "inference": TaskDefaults(gpus=1, cpus=8,  mem_gb=32, time_limit="04:00:00"),
    "test":      TaskDefaults(gpus=1, cpus=4,  mem_gb=16, time_limit="00:30:00"),
    "custom":    TaskDefaults(gpus=1, cpus=16, mem_gb=60, time_limit=""),
}


def resource_defaults(task_type: str) -> TaskDefaults:
    return TASK_DEFAULTS.get(task_type, TASK_DEFAULTS["custom"])


@dataclass
class JobSpec:
    job_name: str
    partition: str
    gpus: int
    cpus: int
    mem_gb: int
    time_limit: str
    run_command: str
    modules: list[str] = field(default_factory=list)
    uploads: list[str] = field(default_factory=list)
    user: str = field(default_factory=getpass.getuser)
    model_path: str = ""
    conda_env: str = ""
    venv_path: str = ""
    task_type: str = "custom"
    container_image: str = ""  # path to .sif — when set, skips conda/venv


def make_job_folder(jobs_dir: str, spec: JobSpec) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = Path(jobs_dir) / spec.user / f"{spec.job_name}_{timestamp}"
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o777)
    return str(folder)


def save_upload(src: str, folder: str) -> str:
    dest = Path(folder) / Path(src).name
    shutil.copy2(src, dest)
    return str(dest)


def render_sbatch(spec: JobSpec, folder: str) -> str:
    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name={spec.job_name}",
        f"#SBATCH --partition={spec.partition}",
        f"#SBATCH --gres=gpu:{spec.gpus}",
        f"#SBATCH --cpus-per-task={spec.cpus}",
        f"#SBATCH --mem={spec.mem_gb}G",
    ]
    if spec.time_limit:
        lines.append(f"#SBATCH --time={spec.time_limit}")
    lines += [
        f"#SBATCH --output={folder}/slurm-%j.out",
        f"#SBATCH --error={folder}/slurm-%j.err",
        f"#SBATCH --chdir={folder}",
        "",
    ]

    for mod in spec.modules:
        lines.append(f"module load {mod}")
    if spec.modules:
        lines.append("")

    if spec.container_image:
        # Container mode: wrap run_command in apptainer exec; skip conda/venv
        lines.append(f"cd {folder}")
        lines.append(
            f"apptainer exec --nv --bind /shared {spec.container_image} "
            f"bash -lc {spec.run_command!r}"
        )
        return "\n".join(lines) + "\n"

    if spec.conda_env:
        # Source conda.sh before activating — required in non-interactive bash
        # (sbatch scripts). Without this, `conda activate` silently fails and the
        # job runs in the base Python environment instead of the requested env.
        # CONDA_PREFIX_SHARED is set by the launcher; fallback covers direct sbatch.
        lines += [
            '_conda_sh="${CONDA_PREFIX_SHARED:-/shared/miniforge3}/etc/profile.d/conda.sh"',
            '[ -f "$_conda_sh" ] && source "$_conda_sh"',
            f"conda activate {spec.conda_env}",
            "",
        ]
    elif spec.venv_path:
        lines.append(f"source {spec.venv_path}/bin/activate")
        lines.append("")

    if spec.model_path:
        lines.append(f"export MODEL_PATH={spec.model_path}")
        lines.append(f"export HF_HOME={spec.model_path}")
        lines.append("")

    lines.append(f"cd {folder}")
    lines.append(spec.run_command)
    return "\n".join(lines) + "\n"


def write_sbatch(spec: JobSpec, folder: str) -> str:
    path = Path(folder) / "job.sbatch"
    path.write_text(render_sbatch(spec, folder))
    path.chmod(0o644)
    return str(path)
