# iitgpu/jobs.py
from __future__ import annotations
import getpass
import grp
import os
import shlex
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta


def _cluster_tz():
    try:
        from iitgpu.config import cluster_tz
        return cluster_tz()
    except Exception:
        return timezone(timedelta(hours=5, minutes=30))
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
    "notebook-script": TaskDefaults(gpus=1, cpus=8, mem_gb=32, time_limit="08:00:00"),
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
    timestamp = datetime.now(_cluster_tz()).strftime("%Y%m%d_%H%M%S")
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


# import-name → PyPI-package aliases for auto-install (the common mismatches).
_PIP_IMPORT_ALIASES: dict[str, str] = {
    "cv2": "opencv-python", "sklearn": "scikit-learn", "PIL": "Pillow",
    "yaml": "PyYAML", "bs4": "beautifulsoup4", "skimage": "scikit-image",
    "Crypto": "pycryptodome", "dateutil": "python-dateutil", "OpenSSL": "pyOpenSSL",
    "google": "protobuf", "dotenv": "python-dotenv", "jwt": "PyJWT",
    "attr": "attrs", "yaml_": "PyYAML",
}


def _pip_self_heal_jupyter() -> str:
    """Bash that installs jupyter/nbconvert into ~/.local when missing."""
    return (
        "if ! command -v jupyter >/dev/null 2>&1; then\n"
        '    echo "jupyter not found in this environment - installing it (one-time)..."\n'
        "    python3 -m pip install --user --quiet --no-warn-script-location "
        "jupyterlab nbconvert ipykernel \\\n"
        '        || { echo "ERROR: jupyter/nbconvert missing and could not be installed." >&2; \\\n'
        '             echo "       Use an environment that includes them (e.g. the data-science prebuilt env)." >&2; exit 1; }\n'
        '    export PATH="$HOME/.local/bin:$PATH"\n'
        "fi\n"
    )


def pip_install_block(requirements: str = "", packages: str = "") -> str:
    """Bash that pip-installs declared deps into the user site (~/.local) BEFORE a
    run. --user keeps shared prebuilt envs unpolluted while staying importable
    from any conda env (user site is on sys.path unless PYTHONNOUSERSITE)."""
    if requirements:
        req = shlex.quote(requirements)
        return (
            f'echo "Installing dependencies from {Path(requirements).name} ..."\n'
            f"python3 -m pip install --user --no-warn-script-location -r {req} \\\n"
            '    || { echo "Dependency install FAILED - see pip output above." >&2; exit 1; }\n'
            'export PATH="$HOME/.local/bin:$PATH"\n'
        )
    if packages:
        toks = " ".join(shlex.quote(t) for t in packages.split())
        return (
            f'echo "Installing dependencies: {packages} ..."\n'
            f"python3 -m pip install --user --no-warn-script-location {toks} \\\n"
            '    || { echo "Dependency install FAILED - see pip output above." >&2; exit 1; }\n'
            'export PATH="$HOME/.local/bin:$PATH"\n'
        )
    return ""


def _auto_install_alias_fn() -> str:
    """Bash function mapping an import name to its PyPI package name."""
    cases = "".join(
        f"        {shlex.quote(imp)}) echo {shlex.quote(pkg)};;\n"
        for imp, pkg in _PIP_IMPORT_ALIASES.items()
    )
    return (
        "_iit_pkg_for() {\n"
        "    case \"$1\" in\n"
        + cases
        + '        *) echo "$1";;\n'
        "    esac\n"
        "}\n"
    )


def notebook_run_command(notebook_path: str, *, in_container: bool = False,
                         requirements: str = "", packages: str = "",
                         auto_install: bool = True) -> str:
    """Bash that installs a notebook's deps, runs it top-to-bottom, saves results.

    Returned as a JobSpec.run_command, so render_sbatch handles the SLURM header,
    env activation, DATA_PATH export and `cd <job folder>` around it.

    Dependencies — three layers:
    1. *requirements* (requirements.txt path) or *packages* (space list): installed
       up front into ~/.local (authoritative / fastest when known).
    2. *auto_install* (default True): the execution is wrapped in a retry loop —
       on `ModuleNotFoundError: No module named 'X'` it maps X → PyPI package
       (alias table) and pip-installs it, then re-runs. This catches TRANSITIVE /
       hidden deps a static import scan misses (e.g. tensorboard, pulled in by
       `from torch.utils.tensorboard import ...`). Bounded to 8 attempts.
    All installs use --user so shared prebuilt envs stay unpolluted.

    Execution: every cell runs via `jupyter nbconvert --execute`; an executed copy
    (executed.ipynb) and an HTML render land in the job folder (the script's cwd).
    Outside a container a missing jupyter/nbconvert is self-installed into ~/.local;
    inside a container jupyter must already ship in the image.
    """
    nb = shlex.quote(notebook_path)
    heal = "" if in_container else _pip_self_heal_jupyter()
    deps = pip_install_block(requirements, packages)

    # Pin the execution kernel to THIS env's python. Without this, nbconvert
    # resolves the notebook's "python3" kernelspec to a stray ~/.local/system
    # python whose sys.path lacks the env's packages → spurious "No module named
    # pandas" even though pandas is installed in the active env (and the
    # auto-install loop then spins forever: pip says "already satisfied", the
    # kernel still can't import it). Registering the active python as a per-job
    # kernel and pinning nbconvert to it guarantees env + ~/.local are both on
    # the kernel's path.
    kernel_setup = "" if in_container else (
        '_IIT_KERNEL="iit-nb-${SLURM_JOB_ID:-$$}"\n'
        "python3 -m pip install --user --quiet --no-warn-script-location ipykernel >/dev/null 2>&1 || true\n"
        'python3 -m ipykernel install --user --name "$_IIT_KERNEL" --display-name "IIT notebook job" >/dev/null 2>&1 \\\n'
        '    || { echo "ERROR: could not register a Jupyter kernel for this environment." >&2; exit 1; }\n'
        'echo "Notebook execution kernel: $_IIT_KERNEL"\n'
    )
    kpin = "" if in_container else ' --ExecutePreprocessor.kernel_name="$_IIT_KERNEL"'
    nbconvert = (
        "jupyter nbconvert --to notebook --execute --ExecutePreprocessor.timeout=-1"
        + kpin
        + f" --output executed.ipynb --output-dir . {nb}"
    )

    if auto_install and not in_container:
        run = (
            _auto_install_alias_fn()
            + f'echo "Executing notebook: {Path(notebook_path).name}"\n'
            + "_iit_attempt=0; _iit_max=8\n"
            + "while : ; do\n"
            + '    _iit_log="$(mktemp)"\n'
            + f"    {nbconvert} 2>&1 | tee \"$_iit_log\"\n"
            + "    _iit_rc=${PIPESTATUS[0]}\n"
            + '    [ "$_iit_rc" -eq 0 ] && { rm -f "$_iit_log"; break; }\n'
            # Match the clean "No module named 'X'" — nbconvert ANSI-colours the
            # "ModuleNotFoundError" token, so don't anchor on it.
            + "    _iit_miss=\"$(grep -oE \"No module named '[^']+'\" \"$_iit_log\" "
              "| tail -1 | sed -E \"s/.*'([^']+)'.*/\\1/\")\"\n"
            + '    rm -f "$_iit_log"\n'
            + '    if [ -z "$_iit_miss" ]; then\n'
            + '        echo "Notebook FAILED and it is not a missing-module error — see the traceback above." >&2; exit 1\n'
            + "    fi\n"
            + '    _iit_attempt=$((_iit_attempt+1))\n'
            + '    if [ "$_iit_attempt" -gt "$_iit_max" ]; then\n'
            + '        echo "Gave up after $_iit_max auto-installs (still missing: $_iit_miss)." >&2; exit 1\n'
            + "    fi\n"
            # Map the TOP-LEVEL import (e.g. google.protobuf -> google -> protobuf)
            # to its PyPI package name; pip packages are keyed on the top module.
            + '    _iit_top="${_iit_miss%%.*}"\n'
            + '    _iit_pkg="$(_iit_pkg_for "$_iit_top")"\n'
            + '    echo ">> Auto-installing missing dependency \'$_iit_miss\' (pip: $_iit_pkg) [attempt $_iit_attempt/$_iit_max]"\n'
            + '    python3 -m pip install --user --no-warn-script-location "$_iit_pkg" \\\n'
            + '        || { echo "Failed to pip install $_iit_pkg" >&2; exit 1; }\n'
            + '    export PATH="$HOME/.local/bin:$PATH"\n'
            + "done\n"
        )
    else:
        run = (
            f'echo "Executing notebook: {Path(notebook_path).name}"\n'
            + nbconvert + " \\\n"
            + '    || { echo "Notebook execution FAILED - see the traceback above." >&2; exit 1; }\n'
        )

    return (
        heal + deps + kernel_setup + run
        + "jupyter nbconvert --to html --output-dir . executed.ipynb || true\n"
        + 'echo "Notebook finished. Results: executed.ipynb (+ executed.html) in this job folder."'
    )



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


# Bash that resolves the compute node's gateway-reachable address at runtime.
#
# Interactive services (JupyterLab / TensorBoard) run on a GPU *compute* node,
# but users only ever SSH into the *login/gateway* node — a different host. If
# the service binds to 127.0.0.1 it lives on the compute node's loopback, which
# the gateway cannot reach, so the user's `-L <port>:localhost:<port>` tunnel
# (terminating on the login node) connects to nothing. The job therefore looks
# "started" yet is unreachable, and users give up and cancel it.
#
# Fix: bind to the node's SLURM NodeAddr — routable from the gateway over the
# cluster network but NOT the public-facing interface — and forward the tunnel
# to that same address. The per-job random token still gates access. We resolve
# NodeAddr via scontrol (authoritative), falling back to the first local IP and
# finally loopback so the script is always well-defined.
_NODE_ADDR_SNIPPET = [
    'IIT_NODE_ADDR=$(scontrol show node "${SLURMD_NODENAME:-$(hostname -s)}" 2>/dev/null'
    ' | sed -n "s/.*NodeAddr=\\([^ ]*\\).*/\\1/p")',
    '[ -z "$IIT_NODE_ADDR" ] && IIT_NODE_ADDR=$(hostname -I | awk "{print \\$1}")',
    '[ -z "$IIT_NODE_ADDR" ] && IIT_NODE_ADDR=127.0.0.1',
    "",
]


def render_notebook_sbatch(
    spec: "JobSpec",
    folder: str,
    port: int = 8888,
    gateway_host: str = "localhost",
    gateway_port: int = 22,
    requirements: str = "",
    packages: str = "",
) -> str:
    """Generate an sbatch script that launches JupyterLab on the GPU node.

    The script:
    - Binds JupyterLab to 127.0.0.1 only (not exposed to network)
    - Optionally pip-installs *requirements*/*packages* into ~/.local first, so the
      interactive session starts with the user's deps ready (no first-cell import
      failures)
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
    if spec.mail_user:
        from iitgpu.config import load_config
        mail_types = load_config().notify_mail_types
        lines.append(f"#SBATCH --mail-user={spec.mail_user}")
        lines.append(f"#SBATCH --mail-type={mail_types}")
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
            f"jupyter lab --no-browser --ip=$IIT_NODE_ADDR --port={port} "
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

        # JupyterLab must be on PATH for the launch below. Not every environment
        # ships it (a plain PyTorch env, say), which previously left the job
        # dying with "jupyter: command not found". Install it into the user's
        # site (~/.local) on the fly when missing so the notebook still comes up;
        # fail loudly with guidance if even that cannot be done.
        lines += [
            "if ! command -v jupyter >/dev/null 2>&1; then",
            '    echo "JupyterLab not found in this environment - installing it (one-time)..."',
            '    python3 -m pip install --user --quiet --no-warn-script-location jupyterlab \\',
            '        || { echo "ERROR: JupyterLab is missing and could not be installed automatically." >&2; \\',
            '             echo "       Use an environment that includes JupyterLab (e.g. the data-science prebuilt env)." >&2; exit 1; }',
            '    export PATH="$HOME/.local/bin:$PATH"',
            "fi",
            "",
        ]

        # Optional: pre-install the user's deps so the interactive session is
        # ready (e.g. tensorboard/tqdm) instead of failing on the first cell.
        _deps = pip_install_block(requirements, packages)
        if _deps:
            lines.append(_deps.rstrip("\n"))
            lines.append("")

        launcher = (
            f"jupyter lab --no-browser --ip=$IIT_NODE_ADDR --port={port} "
            f"--notebook-dir=/shared --ServerApp.token=\"$JUPYTER_TOKEN\""
        )

    lines += _NODE_ADDR_SNIPPET
    lines += [
        "# Generate a random per-job token (not logged beyond this script's stdout)",
        'JUPYTER_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(24))")',
        "",
        "echo '================================================='",
        "echo 'JupyterLab is starting on the GPU node.'",
        "echo 'Run this SSH tunnel command from YOUR LAPTOP:'",
        f'echo "  ssh -p {gateway_port} -L {port}:$IIT_NODE_ADDR:{port} $USER@{gateway_host}"',
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
    if spec.mail_user:
        from iitgpu.config import load_config
        mail_types = load_config().notify_mail_types
        lines.append(f"#SBATCH --mail-user={spec.mail_user}")
        lines.append(f"#SBATCH --mail-type={mail_types}")
    lines += [
        f"#SBATCH --output={folder}/slurm-%j.out",
        f"#SBATCH --error={folder}/slurm-%j.err",
        f"#SBATCH --chdir={folder}",
        "",
    ]
    if spec.container_image:
        launcher = (
            f"apptainer exec --bind /shared {spec.container_image} "
            f"tensorboard --logdir {logdir} --port {port} --host $IIT_NODE_ADDR"
        )
    else:
        if spec.conda_env:
            lines += [
                '_conda_sh="${CONDA_PREFIX_SHARED:-/shared/miniforge3}/etc/profile.d/conda.sh"',
                '[ -f "$_conda_sh" ] && source "$_conda_sh"',
                f"conda activate {spec.conda_env}",
                "",
            ]
        launcher = f"tensorboard --logdir {logdir} --port {port} --host $IIT_NODE_ADDR"
    lines += _NODE_ADDR_SNIPPET
    lines += [
        "echo \'=================================================\'",
        "echo \'TensorBoard starting. SSH tunnel from your laptop:\'",
        f'echo "  ssh -p {gateway_port} -L {port}:$IIT_NODE_ADDR:{port} $USER@{gateway_host}"',
        f"echo \'Then open: http://localhost:{port}\'",
        "echo \'=================================================\'",
        "",
        launcher,
    ]
    return "\n".join(lines) + "\n"
