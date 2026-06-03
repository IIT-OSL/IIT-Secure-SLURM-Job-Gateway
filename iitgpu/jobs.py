# iitgpu/jobs.py
from __future__ import annotations
import getpass
import grp
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

_LK = timezone(timedelta(hours=5, minutes=30))
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
    "notebook":  TaskDefaults(gpus=1, cpus=8,  mem_gb=32, time_limit="08:00:00"),
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
    array: str = ""            # SLURM --array spec, e.g. "0-9" or "1-100%4"
    dependency: str = ""       # SLURM --dependency, e.g. "afterok:12345"
    data_path: str = ""        # path exported as DATA_PATH in the sbatch script
    mail_user: str = ""        # email for SLURM mail directives (if MTA present)


def make_job_folder(jobs_dir: str, spec: JobSpec) -> str:
    timestamp = datetime.now(_LK).strftime("%Y%m%d_%H%M%S")
    folder = Path(jobs_dir) / spec.user / f"{spec.job_name}_{timestamp}"
    folder.mkdir(parents=True, exist_ok=True)
    # 0o770: owner + gpuusers group can read/write; other users cannot
    # Set group to gpuusers so daham (the sudo-sbatch user) can open the script.
    # Non-root can chown group to any group they belong to; public is in gpuusers.
    folder.chmod(0o770)
    try:
        from iitgpu.config import load_config
        gid = grp.getgrnam(load_config().gpuusers_group).gr_gid
        os.chown(str(folder), -1, gid)
    except (KeyError, PermissionError, OSError):
        pass   # best-effort; sbatch will fail with a clear error if still blocked
    return str(folder)


def save_upload(src: str, folder: str) -> str:
    dest = Path(folder) / Path(src).name
    shutil.copy2(src, dest)
    return str(dest)


def render_sbatch(spec: JobSpec, folder: str) -> str:
    lines = [
        "#!/bin/bash",
        # Use the full job-folder name (e.g. finetune_20260601_045303) as the
        # SLURM job name so queue/sacct/log listings are unambiguous per run,
        # instead of every finetune showing up as just "finetune".
        f"#SBATCH --job-name={Path(folder).name}",
        f"#SBATCH --partition={spec.partition}",
        f"#SBATCH --gres=gpu:{spec.gpus}",
        f"#SBATCH --cpus-per-task={spec.cpus}",
        f"#SBATCH --mem={spec.mem_gb}G",
    ]
    if spec.time_limit:
        lines.append(f"#SBATCH --time={spec.time_limit}")
    if spec.array:
        lines.append(f"#SBATCH --array={spec.array}")
    if spec.dependency:
        lines.append(f"#SBATCH --dependency={spec.dependency}")
    if spec.mail_user:
        from iitgpu.config import load_config
        mail_types = load_config().notify_mail_types
        lines.append(f"#SBATCH --mail-user={spec.mail_user}")
        lines.append(f"#SBATCH --mail-type={mail_types}")
    lines += [
        (f"#SBATCH --output={folder}/slurm-%A_%a.out"
         if spec.array else f"#SBATCH --output={folder}/slurm-%j.out"),
        (f"#SBATCH --error={folder}/slurm-%A_%a.err"
         if spec.array else f"#SBATCH --error={folder}/slurm-%j.err"),
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

    if spec.data_path:
        lines.append(f"export DATA_PATH={spec.data_path}")
        lines.append("")

    lines.append(f"cd {folder}")
    lines.append(spec.run_command)
    return "\n".join(lines) + "\n"


def write_sbatch(spec: JobSpec, folder: str) -> str:
    path = Path(folder) / "job.sbatch"
    path.write_text(render_sbatch(spec, folder))
    path.chmod(0o644)
    return str(path)



def build_interactive_cmd(spec: "JobSpec", partition: str = "gpu") -> list[str]:
    """Build an `srun --pty` interactive GPU session command.

    Runs a real shell ON the compute node inside a SLURM allocation. It is bound
    to the allocation (exits when the job ends / time limit hits) and is NOT a
    host login shell — the user only reaches the node through SLURM.
    """
    cmd = [
        "srun",
        f"--partition={spec.partition or partition}",
        f"--gres=gpu:{spec.gpus}",
        f"--cpus-per-task={spec.cpus}",
        f"--mem={spec.mem_gb}G",
    ]
    if spec.time_limit:
        cmd.append(f"--time={spec.time_limit}")
    cmd += ["--pty", "bash", "-l"]
    return cmd

def render_notebook_sbatch(
    spec: "JobSpec",
    folder: str,
    port: int = 8888,
    gateway_host: str = "localhost",
    gateway_port: int = 22,
) -> str:
    """Generate an sbatch script that launches JupyterLab on the GPU node.

    The script:
    - Binds JupyterLab to 127.0.0.1 only (not exposed to network)
    - Writes port + token to the job's stdout
    - Prints the exact SSH tunnel command for the user's laptop
    - Shuts JupyterLab down when the job's time limit is reached
    Token is per-job and appears only in the job's stdout (never logged).
    """
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

    # Environment activation (container or conda/venv)
    if spec.container_image:
        # Notebook inside container — wrap the entire jupyter launch
        launcher = (
            f"apptainer exec --nv --bind /shared {spec.container_image} "
            f"jupyter lab --no-browser --ip=127.0.0.1 --port={port} "
            f"--notebook-dir=/shared --ServerApp.token=\"$JUPYTER_TOKEN\""
        )
    else:
        if spec.conda_env:
            lines += [
                '_conda_sh="${CONDA_PREFIX_SHARED:-/shared/miniforge3}/etc/profile.d/conda.sh"',
                '[ -f "$_conda_sh" ] && source "$_conda_sh"',
                f"conda activate {spec.conda_env}",
                "",
            ]
        elif spec.venv_path:
            lines.append(f"source {spec.venv_path}/bin/activate")
            lines.append("")

        launcher = (
            f"jupyter lab --no-browser --ip=127.0.0.1 --port={port} "
            f"--notebook-dir=/shared --ServerApp.token=\"$JUPYTER_TOKEN\""
        )

    lines += [
        "# Generate a random per-job token (not logged beyond this script's stdout)",
        'JUPYTER_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(24))")',
        "",
        "echo '================================================='",
        "echo 'JupyterLab is starting on the GPU node.'",
        "echo 'Run this SSH tunnel command from YOUR LAPTOP:'",
        f"echo '  ssh -p {gateway_port} -L {port}:localhost:{port} public@{gateway_host}'",
        f"echo 'Then open: http://localhost:{port}'",
        "echo '================================================='",
        "",
        launcher,
    ]
    return "\n".join(lines) + "\n"


def write_notebook_sbatch(
    spec: "JobSpec",
    folder: str,
    port: int = 8888,
) -> str:
    """Write the notebook sbatch script to folder/job.sbatch."""
    path = Path(folder) / "job.sbatch"
    path.write_text(render_notebook_sbatch(spec, folder, port=port))
    path.chmod(0o644)
    return str(path)


def render_tensorboard_sbatch(spec, folder, logdir, port=6006,
                              gateway_host="localhost", gateway_port=22):
    """sbatch that launches TensorBoard bound to 127.0.0.1 and prints the
    SSH tunnel command. Reuses conda/container activation like notebooks."""
    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name=tensorboard",
        f"#SBATCH --partition={spec.partition}",
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
    if spec.container_image:
        launcher = (
            f"apptainer exec --bind /shared {spec.container_image} "
            f"tensorboard --logdir {logdir} --port {port} --host 127.0.0.1"
        )
    else:
        if spec.conda_env:
            lines += [
                '_conda_sh="${CONDA_PREFIX_SHARED:-/shared/miniforge3}/etc/profile.d/conda.sh"',
                '[ -f "$_conda_sh" ] && source "$_conda_sh"',
                f"conda activate {spec.conda_env}",
                "",
            ]
        launcher = f"tensorboard --logdir {logdir} --port {port} --host 127.0.0.1"
    lines += [
        "echo \'=================================================\'",
        "echo \'TensorBoard starting. SSH tunnel from your laptop:\'",
        f"echo \'  ssh -p {gateway_port} -L {port}:localhost:{port} $(whoami)@{gateway_host}\'",
        f"echo \'Then open: http://localhost:{port}\'",
        "echo \'=================================================\'",
        "",
        launcher,
    ]
    return "\n".join(lines) + "\n"
