# IIT GPU Manager — ML Training TUI Redesign

**Date:** 2026-05-29
**Approach:** Redesign the UX layer (menu, wizard, monitoring) while keeping the security engine intact.
**Target cluster:** IIT login-node → RTX 5090, 1 GPU, 16 CPUs, 63 GB RAM, SLURM 25.11.2, `/shared` NFS

---

## Goal

Make GPU job submission usable by people with no Linux knowledge. A student should be able to SSH in, set up their PyTorch environment, and submit a training job without ever typing a shell command. Advanced users who know SLURM retain full manual control through an audited shell.

---

## What Changes, What Stays

### Rewritten
| File | Why |
|---|---|
| `iitgpu/menu.py` | New 5-item top-level menu: Setup / Run / Monitor / Advanced / Quit |
| `iitgpu/wizard.py` | Stripped to 4-step auto-configure flow; removes the 15-step expert wizard |

### New files
| File | Purpose |
|---|---|
| `iitgpu/setup.py` | Setup wizard: health check → env builder → data upload → smoke test |
| `iitgpu/envbuilder.py` | Conda env creation: framework picker → package resolution → conda create + pip |
| `iitgpu/dashboard.py` | Live dashboard: Rich Live layout, auto-refresh every 3 s, job list + rolling log |
| `iitgpu/shell.py` | SLURM command shell: restricted input loop, audit-logged, no shell= True ever |

### Minor updates
| File | Change |
|---|---|
| `iitgpu/jobs.py` | Add `task_type: str` to `JobSpec`; add `resource_defaults(task_type)` function |
| `iitgpu/templates.py` | Update built-in presets to match real cluster (1 GPU, 16 CPUs, 60 GB) |

### Untouched
`slurm.py`, `validate.py`, `auditclient.py`, `models.py`, `envs.py`, `config.py`, `ui.py`, `splash.py`, `__main__.py`, `deploy/` (entire directory), existing tests.

---

## Top-Level Menu Flow

```
SSH login → splash → Main Menu
   ├── 1. Setup          → setup.py
   ├── 2. Run a job      → wizard.py
   ├── 3. Monitor        → dashboard.py
   ├── 4. Advanced       → shell.py
   └── 5. Quit
```

---

## Setup Wizard (`setup.py`)

Five sequential steps. Step 1 (health check) always runs and blocks proceeding on failure. Steps 2–5 are individually skippable.

### Step 1 — Health check
- Run `sinfo` via `slurm.get_partitions()` — must return at least one partition
- Verify `/shared` is writable: create and delete a temp file
- Verify `/shared/envs/` exists (or can be created)
- On failure: print exact fix command and refuse to proceed

### Step 2 — Environment builder (delegates to `envbuilder.py`)
- Framework picker: `PyTorch` / `TensorFlow` / `JAX` / `Bare Python 3.11` / `Skip`
- For PyTorch/TF/JAX: show version list pre-filtered for CUDA 13.2 compatibility
- Optional: attach a `requirements.txt` via jailed file browser
- Creates env at `/shared/envs/{name}` via `conda create -p ... python=3.11` then `pip install`
- Registers the resulting env in the existing `envs.py` registry
- Shows progress output inline (subprocess stdout piped to console)

### Step 3 — Data upload
- Jailed file browser starting at user home
- Copies selected files/folders to `/shared/{user}/data/`
- Displays file count and total size before confirming

### Step 4 — Model download
- Delegates directly to existing `models.py` interactive functions
- HuggingFace repo ID or arbitrary URL

### Step 5 — Smoke test
- Ask user: "Run a quick test to verify the environment works?"
- Generates and submits a 2-line job:
  ```bash
  #!/bin/bash
  #SBATCH --gres=gpu:1 --time=00:05:00 --output=/shared/{user}/smoke_test.out
  source activate /shared/envs/{chosen_env}
  python -c "import torch; print('CUDA:', torch.cuda.is_available())"
  ```
- Attaches immediately to live output (same dashboard as Monitor)
- Shows `✔ CUDA: True` or `✘ CUDA: False` clearly when job finishes

---

## Run Wizard (`wizard.py`)

4 steps. Resources are set automatically — the user never sees GPU/CPU/memory fields.

### Step 1 — Task type
| Choice | GPU | CPUs | Mem | Time limit |
|---|---|---|---|---|
| Train from scratch | 1 | 16 | 60 G | no limit |
| Fine-tune a model | 1 | 16 | 60 G | no limit |
| Run inference | 1 | 8 | 32 G | 04:00:00 |
| Quick test | 1 | 4 | 16 G | 00:30:00 |

### Step 2 — Environment
- List from `envs.py` registry (conda + venv)
- If registry is empty: warn and offer to jump to Setup → Environment

### Step 3 — Script
- Jailed file browser starting at `/shared/{user}/`
- Accepts `.py` and `.sh` files only

### Step 4 — Extra arguments
- Free-text input, blank = no extra args
- Sanitised by existing `clean_run_command()`

### After step 4
- Show generated sbatch script preview (Panel)
- Choices: `Submit` / `Save as template + submit` / `Save template only` / `Discard`
- On submit: `auditclient.log_or_block("job_submit")` — refuse if it returns False
- On success: ask "Watch live output now?" → yes drops directly into dashboard

### Generated script format
```bash
#!/bin/bash
#SBATCH --job-name={task_type}_{timestamp}
#SBATCH --partition=gpu
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task={cpus}
#SBATCH --mem={mem_gb}G
#SBATCH --time={time_limit}
#SBATCH --output=/shared/{user}/jobs/{name}_{ts}/slurm-%j.out
#SBATCH --error=/shared/{user}/jobs/{name}_{ts}/slurm-%j.err

source /shared/envs/{env_name}/bin/activate

cd /shared/{user}/jobs/{name}_{ts}
python {script_path} {args}
```

---

## Live Dashboard (`dashboard.py`)

Uses `rich.live.Live` with a `rich.layout.Layout` — not a print loop.

### Layout
```
┌─ My Jobs ───────────────────────────────────────┐
│  143  train_resnet   RUNNING    00:04:21  gpu    │  ← highlighted row
│  142  debug_run      COMPLETED  00:01:03  gpu    │
└─────────────────────────────────────────────────┘
┌─ Output: job 143 ───────────────────────────────┐
│  Epoch 4/10  loss=1.103  acc=0.61               │
│  Epoch 5/10  loss=0.891  acc=0.72               │
└─────────────────────────────────────────────────┘
  Q=quit  S=switch job  C=cancel selected  R=refresh now
```

### Behaviour
- Polls `slurm.queue(user=current_user)` every 3 s for the job list
- Reads last 20 lines of the selected job's `.out` file every 3 s
- If log file does not exist yet (job still queued): shows "Waiting for job to start..."
- `S` cycles through jobs in the list (changes the highlighted row and the log panel)
- `C` calls `slurm.cancel(job_id)` on the highlighted job after confirmation
- `R` forces an immediate refresh
- `Q` or Ctrl+C exits back to main menu
- Keyboard input handled via `select.select([sys.stdin], [], [], 0)` with `tty`/`termios` raw mode during the Live context; restored to normal on exit

---

## SLURM Command Shell (`shell.py`)

A custom input loop — never uses `shell=True` or `subprocess` without an explicit allowlist.

### Allowed commands
`sbatch`, `squeue`, `scancel`, `sinfo`, `tail`

### Rules
- Parse first token as command, rest as args
- `sbatch <path>`: path must pass `in_jail()` before executing
- `tail <path>` / `tail -f <path>`: path must pass `in_jail()` before executing
- Any other command: print "Command not allowed: {cmd}" — no execution
- Every typed line logged: `auditclient.log("shell_cmd", detail=raw_input)`
- `exit` or empty Ctrl+C → return to main menu, log `shell_exit`

### Prompt
```
slurm> _
```

---

## `envbuilder.py` — Framework → Conda Env

```python
FRAMEWORK_PACKAGES = {
    "pytorch-2.5": ["torch==2.5.* torchvision torchaudio --index-url https://download.pytorch.org/whl/cu131"],
    "pytorch-2.4": ["torch==2.4.* torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121"],
    "tensorflow-2.18": ["tensorflow[and-cuda]==2.18.*"],
    "jax-0.4": ["jax[cuda12]"],
    "bare": [],
}
```

- `build_env(name, framework_key, extra_requirements_path)` → runs conda + pip, streams output to console, returns `(success: bool, env_path: str)`
- If `conda` not found in PATH: print install instructions, return `(False, "")`
- If `extra_requirements_path` given: runs `pip install -r {path}` after framework install
- Registers the env via `envs.register_venv(cfg, name, env_path)` on success

---

## `jobs.py` updates

Add to `JobSpec`:
```python
task_type: str = "custom"   # "train" | "finetune" | "inference" | "test" | "custom"
```

Add:
```python
TASK_DEFAULTS = {
    "train":     JobDefaults(gpus=1, cpus=16, mem_gb=60, time_limit=""),
    "finetune":  JobDefaults(gpus=1, cpus=16, mem_gb=60, time_limit=""),
    "inference": JobDefaults(gpus=1, cpus=8,  mem_gb=32, time_limit="04:00:00"),
    "test":      JobDefaults(gpus=1, cpus=4,  mem_gb=16, time_limit="00:30:00"),
}
```

---

## Built-in Template Updates (`templates.py`)

Replace current presets (which reference non-existent multi-GPU partitions) with:

| Name | Task | GPUs | CPUs | Mem | Time |
|---|---|---|---|---|---|
| PyTorch Training | train | 1 | 16 | 60 G | no limit |
| HuggingFace Fine-tune | finetune | 1 | 16 | 60 G | no limit |
| Inference / Serving | inference | 1 | 8 | 32 G | 04:00:00 |
| Quick Debug | test | 1 | 4 | 16 G | 00:30:00 |

---

## New Tests

| File | Covers |
|---|---|
| `tests/test_setup.py` | Health check pass/fail, env builder path validation, smoke test script generation |
| `tests/test_dashboard.py` | Log tail logic (last N lines), job switching, missing log file handling |
| `tests/test_shell.py` | Allowed commands pass, blocked commands rejected, in_jail enforcement on sbatch/tail paths |
| `tests/test_envbuilder.py` | Package map lookup, conda-not-found graceful failure, requirements.txt path validation |

---

## What This Does NOT Change

- SSH `ForceCommand` security model
- Path jail (`in_jail`, `safe_listdir`)
- Audit daemon, SQLite/JSONL logging, Google Sheets integration
- `slurmsvc` sudo delegation
- `auditclient.log_or_block` pre-submission gate
- SIGTSTP suppression
- Any existing passing tests
