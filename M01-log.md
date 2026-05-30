# M01 — IIT Secure SLURM Job Gateway: Full System Log

**Cluster:** iit-MS-7E06 (RTX 5090, 32 GB VRAM, 16 CPU, 63 GB RAM)
**Login node:** 192.168.122.10 (slurmadmin VM)
**Install path:** `/opt/iit-gpu/` (login node)
**Repo:** `https://github.com/DahamDissanayake/IIT-Secure-SLURM-Job-Gateway`
**NFS share:** `/shared` (mounted on both compute node and login node)
**Last commit:** `c1d6bdb`

---

## 1. System Architecture

```
┌────────────────────────────────────────┐
│  User (SSH as "public")                │
│  runs: iit-gpu-manager                 │
└───────────────┬────────────────────────┘
                │ SSH to 192.168.122.10
┌───────────────▼────────────────────────┐
│  Login Node (192.168.122.10)           │
│  Python TUI: /opt/iit-gpu/iitgpu/     │
│  SLURM controller: slurmctld          │
│  NFS share: /shared (mounted)          │
└───────────────┬────────────────────────┘
                │ SLURM job dispatch (srun/sbatch)
┌───────────────▼────────────────────────┐
│  Compute Node: iit-MS-7E06            │
│  (192.168.122.1 — the KVM host)       │
│  GPU: RTX 5090, 32 GB VRAM            │
│  SLURM worker: slurmd                  │
│  NFS share: /shared (mounted)          │
│  Stats writer → /shared/.gpu_stats.json│
└────────────────────────────────────────┘
```

**Key constraint:** all SLURM commands (`sbatch`, `squeue`, `scancel`, `sinfo`) must run as `daham` via `sudo -u daham`. The `public` user has no direct SLURM access — only through the tool's sudoers rules.

---

## 2. Entry Point — How the Tool Launches

**Launcher:** `/usr/local/bin/iit-gpu-manager`

```bash
exec env -i \
    HOME="$HOME" USER="$USER" LOGNAME="$LOGNAME" \
    PATH="/shared/miniforge3/bin:/usr/local/bin:/usr/bin:/bin" \
    SSH_CLIENT="${SSH_CLIENT:-}" TERM="${TERM:-xterm}" \
    PYTHONPATH="/opt/iit-gpu" \
    CONDA_PREFIX_SHARED="/shared/miniforge3" \
    NFS_ROOT="/shared" \
    /usr/bin/python3 -m iitgpu
```

- Runs `/usr/bin/python3 -m iitgpu` (calls `iitgpu/__main__.py`)
- Clean environment (`env -i`) prevents user environment variables bleeding in
- `PYTHONPATH=/opt/iit-gpu` is how Python finds the package
- `NFS_ROOT=/shared` and `CONDA_PREFIX_SHARED=/shared/miniforge3` are the two runtime config knobs

**`__main__.py`** parses `--demo`, `--no-splash`, `--selftest` flags, installs signal handlers (SIGINT → clean audit exit, SIGTSTP → ignored), shows splash, then calls `menu.run_menu()`.

---

## 3. Module Reference

### `config.py`
Reads environment variables into a frozen `Config` dataclass.

| Env var | Default | Purpose |
|---|---|---|
| `NFS_ROOT` | `/shared` | Root of the NFS share |
| `JOBS_SUBDIR` | `jobs` | Sub-directory under NFS_ROOT for job output |
| `CONDA_PREFIX_SHARED` | `/shared/miniforge3` | Path to shared Miniforge install |
| `DEMO_MODE` | `0` | If `1`, all SLURM calls are mocked |

Helper functions: `jobs_dir(cfg)`, `models_dir(cfg)`, `templates_dir(cfg)`, `conda_sh(cfg)`.

---

### `menu.py` — Main Menu + Monitor Menu

**Main menu choices:**
1. Upload files — calls `upload.run_upload()`
2. Setup — calls `setup.run_setup()`
3. Run a job — calls `wizard.run_wizard()`
4. Monitor — calls local `_monitor_menu()`
5. Advanced — calls `shell.run_shell()`
6. Quit

**`_monitor_menu()` choices:**
- Live dashboard (auto-refresh) → `dashboard.run_dashboard()`
- View my queue → `monitor.show_queue()`
- Cancel a job → `monitor.cancel_job()`
- View job log → `monitor.browse_and_tail_log()`
- Cluster status → local `_show_cluster_status()` (lists SLURM partitions)
- **View hardware stats** → `dashboard.run_hardware_stats()` ← *added in this session*

---

### `wizard.py` — 4-Step Job Submission Wizard

Flow for `Run a Job`:

1. **Template?** — optional load of saved template (questionary confirm)
2. **Task type** — Train / Fine-tune / Inference / Quick test (sets resource defaults)
3. **Environment** — pick from registered conda/venv environments (or skip)
4. **Script** — jailed file browser starting at `/shared/{user}/`
5. **Training config** *(only if `train_cifar10.py` is selected)*:
   - Model: `SmallResNet (fast, ~2 min)` or `WideResNet-28-10 (accurate, ~14 min)`
   - Epochs: default 50, editable
   - Injects `--model wideres` and/or `--epochs N` automatically
6. **Extra arguments** — free-text, sanitized by `validate.clean_run_command()`
7. **Preview** — shows generated sbatch script via `panel()`
8. **Action** — Submit / Save as template + submit / Save template only / Discard
9. **Watch?** — optional jump to live dashboard after submit

---

### `jobs.py` — Job Spec and sbatch Renderer

**`JobSpec` dataclass** — captures all job parameters: name, partition, GPUs, CPUs, memory, time limit, run command, conda env, venv path, model path, task type.

**`resource_defaults(task_type)`** — returns sensible SLURM resource requests per task:

| Task | GPUs | CPUs | RAM | Time limit |
|---|---|---|---|---|
| train | 1 | 16 | 60 GB | unlimited |
| finetune | 1 | 16 | 60 GB | unlimited |
| inference | 1 | 8 | 32 GB | 4 h |
| test | 1 | 4 | 16 GB | 30 min |

**`make_job_folder()`** — creates `/shared/jobs/{user}/{job_name}_{timestamp}/` with permissions 0o777 so SLURM's `daham` user can write output files.

**`render_sbatch()`** — generates the `job.sbatch` script:
- `#SBATCH` directives (name, partition, gres, cpus, mem, time, output/error paths, chdir)
- conda activation: sources `conda.sh` then `conda activate {env_path}`
- Sets `MODEL_PATH` and `HF_HOME` if a model path was specified
- Appends the run command at the end

---

### `slurm.py` — SLURM Interface Layer

All SLURM calls go through `sudo -u daham` because jobs are owned by the `daham` user. The `public` gateway user's sudoers only allows: `sbatch`, `squeue`, `scancel`, `sinfo` as daham.

**Key functions:**

#### `get_node_stats()` → `NodeStats`
Two-source merge:
1. `scontrol show node iit-MS-7E06 --oneliner` — run **without** sudo (public can call scontrol read-only). Parses: State, CPUTot, CPUAlloc, CPULoad, RealMemory, AllocTRES, Gres.
2. `_read_gpu_stats_file()` — reads `/shared/.gpu_stats.json`. Falls back to `_read_hw_stats_direct()` which calls `nvidia-smi` + `/proc` directly.

`NodeStats` fields:

| Field | Source | Description |
|---|---|---|
| `state` | scontrol | IDLE / ALLOCATED / DOWN |
| `cpu_total`, `cpu_alloc` | scontrol | SLURM allocation |
| `mem_total_mb`, `mem_alloc_mb` | scontrol | SLURM allocation |
| `gpu_total`, `gpu_alloc` | squeue count | Running GPU jobs (AllocTRES was broken) |
| `cpu_load`, `cpu_load5` | scontrol + stats | 1/5-min load averages |
| `gpu_util`, `gpu_temp`, `gpu_power_w` | nvidia-smi | Real-time GPU metrics |
| `gpu_mem_used_mb`, `gpu_mem_total_mb` | nvidia-smi | Real VRAM usage |
| `cpu_util`, `mem_used_mb` | /proc | Real CPU/RAM usage |
| `live_stats` | bool | True if stats file/direct is fresh |

#### `_read_gpu_stats_file()` — stats data pipeline
1. Check `/shared/.gpu_stats.json` — if file exists and is ≤10 seconds old, parse and return it
2. If stale/missing → call `_read_hw_stats_direct()` which runs `nvidia-smi` and reads `/proc/loadavg`, `/proc/meminfo` directly
3. Always returns real data regardless of whether the daemon is running

#### `_count_running_gpu_jobs()` — GPU allocation fix
`AllocTRES` in scontrol output omits GPU on this SLURM build. Instead: `squeue --states=RUNNING --format=%b` and count lines containing "gpu". Used to populate `NodeStats.gpu_alloc`.

#### `queue()` → `list[QueueEntry]`
`sudo -u daham squeue --noheader --format="%i %j %u %T %P %M %l %D" -u daham`
Returns job_id, name, user, state, partition, time_used, time_limit, nodes.

#### `recent_jobs()` → `list[QueueEntry]`
Scans `slurm-*.out` files in the jobs directory (sorted by mtime, newest first).
- **User**: extracted from path `/shared/jobs/{user}/{job_name}/slurm-*.out`
- **Elapsed**: computed via `stat --format=%W %Y` (birthtime vs mtime) — real wall-clock run time
- **Time limit**: parsed from `job.sbatch` `#SBATCH --time=` directive
- **State**: COMPLETED if no `.err` content, FAILED if `.err` has content

Note: `sacct` is disabled on this cluster (`Slurm accounting storage is disabled`). File scanning is the only option.

---

### `dashboard.py` — Live TUI Dashboard

Uses Rich `Live` with `screen=True` (alternate screen buffer — no flicker).

**Architecture:**
- Data refresh every `_DATA_REFRESH_SECS = 2.0` seconds (squeue + node stats + log tail)
- Display refresh at `_DISPLAY_FPS = 4` (4 redraws/sec for smooth spinner animation)
- Data is cached in mutable lists (closure pattern) so the render loop is pure Rich (no I/O)

**Layout (top to bottom):**
```
┌─ Cluster: iit ──────────────────────────────────────────┐  height=3
│  iit-MS-7E06  ALLOCATED  │  GPU 100% 16.3/32GB 72°C ...│
└─────────────────────────────────────────────────────────┘
┌─ Job Queue ─────────────────────────────────────────────┐  height=min(jobs+6, 16)
│  ID   User   Name          State       Elapsed   Part   │
│  83   daham  train         ⠇ RUNNING   2:02      gpu    │
│  ─── recent ──────────────────────────────────────────  │
│  82   public train_...     COMPLETED   14:11     gpu    │
└─────────────────────────────────────────────────────────┘
┌─ Output: /shared/jobs/.../slurm-83.out ─────────────────┐  fills rest
│  (last 20 lines of selected job's stdout)               │
└─────────────────────────────────────────────────────────┘
  Q=quit   S=switch job   C=cancel selected   R=refresh
```

**Job table — what's shown and why:**
- **Spinner** (`⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`): ticks at display FPS to prove the display is live. Replaces the old fake progress bar that showed `──── running` with no real data behind it.
- **Elapsed**: from `squeue %M` for live jobs (SLURM wall-clock). For completed jobs: computed from log file birthtime/mtime.
- **No progress bar, no ETA**: removed. The cluster has no `--time` limits set on jobs (partition is `infinite`), so ETA was always "no limit" and the bar had no real data.
- **Completed jobs separator**: dim `─── recent ──────` rule between live and history.

**Cluster panel — live stats:**
When `live_stats=True` (stats file is fresh): shows `GPU util% | VRAM used/total | temp | power | CPU util% | load avg | RAM used/total`.
Fallback (stats file stale): shows SLURM allocation data with `alloc` suffix.

**Keyboard controls:**
| Key | Action |
|---|---|
| Q | Quit dashboard |
| S | Cycle to next job |
| C | Cancel selected job (with confirm prompt) |
| R | Force immediate data refresh |

#### `run_hardware_stats()` — Full-Screen Hardware View
Separate full-screen Live panel. Reads `get_node_stats()` every 2 seconds.

Shows per section:
- **GPU**: utilization bar + %, temperature, power draw; VRAM bar + used/total GB
- **CPU**: utilization bar + %, load avg 1-min and 5-min
- **RAM**: used bar + used/total GB + %
- **SLURM allocation**: state, GPU alloc, CPU alloc, RAM alloc

Color coding: green = normal, yellow = >70%, red = >90%.

---

### `monitor.py` — Static Queue/Log Views

Functions called from `menu._monitor_menu()`:

- **`show_queue()`** — prints a Rich table of live queue via `slurm.queue()`
- **`cancel_job()`** — questionary select → confirm → `slurm.cancel(job_id)`
- **`browse_and_tail_log()`** — jailed browser of `/shared/jobs/{user}/` folders then log file selector → `tail_log()`
- **`cluster_status()`** — lists SLURM partitions via `slurm.get_partitions()`

---

### `envbuilder.py` — Conda Environment Creator

Called from `setup.run_setup()` → "Create / update environment".
Finds conda binary in `CONDA_PREFIX_SHARED`, presents framework selection (PyTorch, TensorFlow, JAX, bare Python), then runs `conda create` or `conda install` with the appropriate packages.

Framework packages include PyTorch with CUDA index (`--index-url https://download.pytorch.org/whl/cu131`) and validated against the cluster's GPU.

---

### `validate.py` — Security / Path Jail

All file paths in the tool pass through `in_jail(path)`:
- Resolves symlinks with `Path.resolve()`
- Checks the resolved path starts with `NFS_ROOT` (`/shared`)
- Rejects `..` traversal, absolute paths outside jail, symlink escapes

Other validators:
- `clean_run_command()` — strips newlines, control characters, truncates at 1000 chars
- `clean_job_name()` — strips non-alphanumeric-dash-underscore, truncates at 64
- `clean_time_limit()` — validates `H:M:S` format, clamps to `MAX_HOURS` (default 72)
- `clamp_int()` — bounds-checks integer values (e.g., GPU count ≤ MAX_GPUS)

---

### `shell.py` — Restricted SLURM Shell

"Advanced" menu option. Gives the user a prompt where they can type SLURM commands directly, but only from an allowlist:
- `squeue`, `sinfo`, `sacct` (read-only)
- `sbatch` (only for paths inside the jail)
- `scancel` (own jobs only)
- `tail` (only for paths inside jail)

Every command is audit-logged before execution.

---

### `auditclient.py` — Audit Trail

Every significant action (job submit, cancel, env create, etc.) is logged via Unix socket to the `iit-gpu-audit` daemon. If the daemon is unavailable, the event is spooled to disk in `AUDIT_SPOOL` directory and re-sent when the daemon recovers.

`log_or_block()` — used for job submission. Returns `False` (blocking the submit) only if BOTH the socket AND the spool fail. Normal socket unavailability just spools.

The `iit-gpu-audit.service` systemd service runs the audit daemon on the login node.

---

### `upload.py` — Dataset / File Uploader

Menu option 1. Allows uploading files from local paths to `/shared/data/{user}/`. Path-jailed. Supports local copy and URL download (HTTP/HTTPS only, rejects FTP, shell injection via URL).

---

## 4. GPU Stats Writer Daemon

**File:** `/tmp/iit-gpu-stats-writer` (on compute node / `iit-MS-7E06`)
**Output:** `/shared/.gpu_stats.json` (NFS, readable by login node)
**Cadence:** every 2 seconds

Collects:
```json
{
  "gpu_util": 100,
  "gpu_mem_util": 47,
  "gpu_mem_used_mb": 16682,
  "gpu_mem_total_mb": 32607,
  "gpu_temp": 72,
  "gpu_power_w": 569.0,
  "cpu_load1": 1.17,
  "cpu_load5": 0.93,
  "cpu_util": 3,
  "mem_total_mb": 63030,
  "mem_used_mb": 11000,
  "ts": 1780168045.7
}
```

Sources: `nvidia-smi` (GPU fields), `/proc/loadavg` (CPU), `/proc/meminfo` (RAM).

**Startup:** added to `root-daham` crontab with `@reboot` so it auto-starts after reboot:
```
@reboot /usr/bin/python3 /tmp/iit-gpu-stats-writer >> /tmp/iit-gpu-stats.log 2>&1
```

The `redeploy.sh` host script also checks and restarts it if stale.

---

## 5. Training Script — `train_cifar10.py`

**Location:** `/shared/public/data/train_cifar10.py`
**Dataset:** CIFAR-10 (auto-downloaded to `/shared/data/cifar10/`)

### Two modes (selectable in wizard)

#### SmallResNet — fast mode (default)
```
python train_cifar10.py
```
- 4.8M parameters, 4 BasicBlocks (64 → 128 → 256 → 512 channels)
- batch=512, max_lr=0.3
- ~1.2–1.7s/epoch → **50 epochs in ~1.5 min** training / ~4–5 min total with SLURM overhead
- VRAM: ~0.6 GB peak
- Expected accuracy: ~93–95%

#### WideResNet-28-10 — accurate mode
```
python train_cifar10.py --model wideres
```
- 36.5M parameters, depth=28, widen_factor=10, dropout=0.3
- batch=1024, max_lr=0.4
- ~16s/epoch → **50 epochs in ~14 min** total
- VRAM: ~26 GB peak (PyTorch tensors), ~31 GB nvidia-smi (includes allocator pool)
- Expected accuracy: ~95–96%

### Training techniques (both modes)

**BF16 AMP (Automatic Mixed Precision)**
```python
with torch.autocast(device_type='cuda', dtype=torch.bfloat16):
    outputs = model(inputs)
    loss = criterion(outputs, labels)
```
RTX 5090 (Blackwell sm_120) has native BF16 Tensor Cores. BF16 uses 2 bytes vs FP32's 4 bytes, reducing memory bandwidth pressure. `GradScaler` handles gradient scaling.

**OneCycleLR**
Ramps LR from `max_lr/25` up to `max_lr` over the first 30% of training steps, then anneals back down with cosine. Purpose-built for fixed epoch budgets — gets higher accuracy in 50 epochs than CosineAnnealingLR which starts at full LR from epoch 1.

**CutMix augmentation** (50% probability per batch)
Swaps a random rectangular patch between two images in the batch. The loss is a weighted sum: `lam × loss(outputs, labels_a) + (1−lam) × loss(outputs, labels_b)`. Forces the model to learn from partial object views → better generalization, typically +1–2% accuracy on CIFAR-10.

**Output flushing**
```python
sys.stdout.reconfigure(line_buffering=True)
```
Python buffers stdout in 4 KB blocks when writing to a file (SLURM job output). Without this, epoch prints sit in the buffer and never appear in the `.out` file until the buffer fills or the script exits. Line-buffering makes every `print()` immediately write to the log file.

**Within-epoch progress** (every 25% of batches)
```
  epoch 5/50  step 12/49 (24%)  loss 1.63  acc 49.8%  lr 0.0949  8s
```
Proves training is alive between epoch completions.

**Removed: `cudnn.benchmark = True`**
On RTX 5090 (new Blackwell architecture, `sm_120`), PyTorch has to benchmark every cuDNN kernel variant for every layer on the first forward+backward pass. With WideResNet's ~50 unique convolution shapes, this took 5–15 minutes on the first epoch with no output. Removed because CIFAR-10 has fixed 32×32 inputs — benchmark gives zero benefit after the one-time cost.

**Why the GPU shows 100% util but throughput is flat vs batch size**
CIFAR-10's 32×32 images produce tiny feature maps (8×8 at the deepest layer). Each convolution is a small matrix operation that finishes before the next memory access arrives — the GPU is **memory-latency bound**, not compute bound. nvidia-smi shows 100% because a kernel is scheduled 100% of the time, but the actual TFLOPS achieved is ~3–8% of the RTX 5090's 1,792 TFLOPS BF16 peak. Throughput (~3,500 samples/sec for WideResNet) is flat regardless of batch size because it's dictated by memory bandwidth, not VRAM capacity. The only real fix would be `torch.compile` (needs `gcc`/Triton kernel fusion) or a larger-image dataset (ImageNet 224×224 achieves 60–80% GPU efficiency).

### CLI arguments

| Argument | Default | Description |
|---|---|---|
| `--model` | `small` | `small` or `wideres` |
| `--epochs` | 50 | Number of training epochs |
| `--lr` | 0.0 (auto) | max LR for OneCycleLR; auto = 0.3 for small, 0.4 for wideres |
| `--batch_size` | 0 (auto) | Batch size; auto = 512 for small, 1024 for wideres |
| `--data_dir` | `/shared/data/cifar10` | Where CIFAR-10 is stored |
| `--no_amp` | off | Disable BF16 AMP |
| `--no_cutmix` | off | Disable CutMix augmentation |

---

## 6. Deploy System

### Two scripts

#### `deploy/redeploy-host.sh` — run from `iit-MS-7E06` as `root-daham`

**Location:** `/tmp/redeploy.sh` (on compute node)

Steps:
1. SSH to login node → runs `redeploy-igm.sh` (full git + test + deploy cycle)
2. Checks if GPU stats writer is running and fresh; restarts if stale

Use: `bash /tmp/redeploy.sh`

#### `deploy/redeploy-igm.sh` — runs on login node as `slurmadmin`

**Location:** `/home/slurmadmin/redeploy-igm.sh`

Steps:
1. **Git sync** — if local changes: commit all + push; if no changes: pull from GitHub
2. **Tests** — `python3 -m pytest tests/ -q` — aborts deploy if any test fails
3. **Stop service** — `sudo systemctl stop iit-gpu-audit`
4. **Sync code** — `rsync --delete` from repo to `/opt/iit-gpu/` (rsync ensures deleted files are also removed)
5. **Clear bytecode** — removes all `__pycache__` directories so Python recompiles fresh
6. **Rebuild launcher** — rewrites `/usr/local/bin/iit-gpu-manager`
7. **Start service** — `sudo systemctl start iit-gpu-audit`
8. **Smoke import** — verifies `iitgpu.config` and conda are accessible as `public` user

`cp -r` was replaced with `rsync --delete` because `cp` leaves stale files from renamed/deleted modules.

---

## 7. All Changes Made in This Session

### Fix 1 — Real cluster stats (`165b4e2`)
**Problem:** Cluster panel showed all zeros. Node stats came from `scontrol show node` run with `sudo -u daham` which the `public` user cannot do (not in sudoers for `scontrol`).
**Fix:** Run `scontrol` without sudo — it's a read-only query any SLURM user can make.

**Problem:** `recent_jobs()` returned `user="?"`, `time_used="-"` — hardcoded because it only scanned files.
**Fix:** Extract user from file path (`/shared/jobs/{user}/...`), compute elapsed from `stat --format=%W %Y` (birthtime vs mtime).

**Problem:** `get_node_stats()` showed SLURM allocation data (0 when idle) instead of real utilization.
**Fix:** Added stats writer daemon on compute node writing `nvidia-smi` data to `/shared/.gpu_stats.json`. `get_node_stats()` reads and merges both sources.

### Fix 2 — Fake progress/ETA removed, hardware stats added (`13c74db`)
**Problem:** Job table showed a bouncing scanner bar for RUNNING jobs (fake — based on elapsed seconds with no real progress data). ETA column always showed "no limit" because partition time is `infinite`.
**Fix:** Removed `Progress` and `ETA` columns entirely. Added braille spinner (`⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏`) in the State column — proves display is live without inventing numbers.

**Added:** `run_hardware_stats()` in `dashboard.py` — full-screen live hardware panel.

### Fix 3 — Redeploy scripts (`8003513`)
**Problem:** `/tmp/redeploy.sh` on the host tried to `cd /home/slurmadmin/IIT-Secure-SLURM-Job-Gateway` which doesn't exist on the compute node (no git installed there).
**Fix:** Rewrote host-side script to SSH to login node for all git/deploy operations + manage stats writer locally.

**Problem:** Login-node script used `cp -r` which doesn't remove deleted files.
**Fix:** Replaced with `rsync --delete`, also wipes full `__pycache__` directories.

### Fix 4 — Hardware stats in correct menu location (`4e4ef7c`)
**Problem:** "View hardware stats" was added to `monitor.py`'s `monitor_menu()` function which is never called. The actual monitor menu is `_monitor_menu()` in `menu.py`.
**Fix:** Added the option to the correct function in `menu.py`.

### Fix 5 — `scontrol` without sudo for `public` user (`87ea135`)
**Problem:** `get_node_stats()` still used `sudo -u daham scontrol` even after the deploy fix. The `public` user's sudoers only allows sbatch/squeue/scancel/sinfo — not scontrol. Result: hardware stats panel showed "SLURM node unavailable".
**Fix:** Run `scontrol show node` directly without sudo. It's a read-only query.

### Fix 6 — GPU alloc display + stats fallback (`2aa7c09`)
**Problem:** Hardware stats showed `GPU 0/1` even during active training jobs.
**Cause:** `AllocTRES` field in `scontrol show node` output omits GPU on this SLURM build. Parsing it always gave `gpu_alloc=0`.
**Fix:** Count GPU jobs via `squeue --states=RUNNING --format=%b` and count lines containing "gpu".

**Added:** `_read_hw_stats_direct()` — direct `nvidia-smi` + `/proc` fallback when stats file is stale/missing. Hardware stats panel now always shows real data.

**Added:** Stats writer to `@reboot` crontab for persistence across reboots.

### Fix 7 — Training script overhaul

**Removed `cudnn.benchmark = True`** — was silently consuming the entire first epoch (5–15 minutes) benchmarking cuDNN kernels on RTX 5090 (new architecture, no cached kernel timings). CIFAR-10 has fixed 32×32 inputs so benchmark gives zero benefit after the one-time cost.

**Added `sys.stdout.reconfigure(line_buffering=True)`** — Python's block buffering in SLURM jobs caused epoch output to never appear in log files until the 4 KB buffer filled.

**Added within-epoch progress prints** — prints every 25% of batches so the log file updates every few seconds, proving training is alive.

**Replaced SmallResNet+FP32+batch=128 with WideResNet-28-10+BF16+batch=2048** — then reverted to SmallResNet as default after benchmarking proved:
- Throughput (samples/sec) is flat regardless of batch size (memory-latency bound workload)
- WideResNet is 9× slower per epoch (36.5M vs 4.8M params) with marginal accuracy gain
- SmallResNet with modern training tricks (BF16 + OneCycleLR + CutMix) reaches same accuracy faster

**Added OneCycleLR** — better convergence in fixed epoch budgets vs CosineAnnealingLR.

**Added CutMix** — mixes random patches between images, improves generalization +1–2%.

**Added `--model` flag** — `small` (default, fast) or `wideres` (accurate).

### Fix 8 — Model selection in wizard (`c1d6bdb`)
**Added:** Structured model/epoch selection in the wizard when `train_cifar10.py` is detected. Shows two choices with speed/VRAM/accuracy info before the free-text arguments box.

---

## 8. Cluster Hardware Facts

| Component | Value |
|---|---|
| GPU | NVIDIA GeForce RTX 5090 (Blackwell sm_120) |
| VRAM | 32 GB GDDR7 |
| BF16 peak | 1,792 TFLOPS |
| GPU SLURM label | `gpu:1` |
| CPU | 16 cores |
| RAM | 63 GB |
| SLURM partition | `gpu` (single partition, time limit: infinite) |
| Node name | `iit-MS-7E06` |
| Node addr | `192.168.122.1` |
| Login node | `192.168.122.10` (slurmadmin VM) |
| SLURM version | 25.11.2 |
| sacct | **disabled** (`Slurm accounting storage is disabled`) |
| C compiler | **none** (gcc/cc/g++/clang/nvcc all missing → `torch.compile` unavailable) |
| NFS | `/shared` → `/mnt/nvme_storage/shared` (symlink) |

**Known SLURM quirks:**
- `AllocTRES` omits GPU allocation — use `squeue` count instead
- `scontrol` readable without sudo but squeue/sbatch/scancel require `sudo -u daham`
- `GresUsed` field absent from scontrol output on this build
- `slurm.conf` mismatch warning between controller and node (cosmetic, doesn't affect jobs)
- Interactive `srun` commands leave cgroup state dirty — if jobs fail with `RaisedSignal:53` after using srun, restart slurmd: `sudo systemctl restart slurmd`

---

## 9. Test Suite

113 tests across:
- `test_auditclient.py` — socket send, spool fallback, block-on-both-fail
- `test_config.py` — defaults, env overrides, demo mode flag, path helpers
- `test_dashboard.py` — log tail, job log finder, time parser, node stats None on failure
- `test_e2e.py` — selftest passes, demo submit+queue, audit spooling
- `test_envbuilder.py` — framework package lists, conda env creation, missing conda
- `test_jobs.py` — folder naming, sbatch rendering (all directives), task defaults
- `test_setup.py` — health check, smoke test script
- `test_shell.py` — allowed commands, path jail enforcement, flag blocklist
- `test_templates.py` — preset GPU/CPU/mem limits against cluster spec
- `test_upload.py` — folder name validation, URL download safety, browse jail
- `test_validate.py` — path jail, safe_listdir, clamp_int, time/name/command sanitizers

Run: `python3 -m pytest tests/ -q` from the repo root.

---

## 10. Quick Reference — Key Paths

| Path | What it is |
|---|---|
| `/opt/iit-gpu/iitgpu/` | Installed Python package (login node) |
| `/usr/local/bin/iit-gpu-manager` | Launcher script (login node) |
| `/home/slurmadmin/IIT-Secure-SLURM-Job-Gateway/` | Git repo (login node) |
| `/home/slurmadmin/redeploy-igm.sh` | Login-node deploy script |
| `/tmp/redeploy.sh` | Host-side deploy script (compute node) |
| `/shared/jobs/{user}/{job_name}_{ts}/` | Per-job output directory |
| `/shared/jobs/{user}/{job_name}_{ts}/slurm-{id}.out` | SLURM stdout |
| `/shared/jobs/{user}/{job_name}_{ts}/slurm-{id}.err` | SLURM stderr |
| `/shared/jobs/{user}/{job_name}_{ts}/job.sbatch` | Generated sbatch script |
| `/shared/public/data/train_cifar10.py` | Training script |
| `/shared/data/cifar10/` | CIFAR-10 dataset cache |
| `/shared/envs/{env_name}/` | Conda environments |
| `/shared/.gpu_stats.json` | Live GPU/CPU/RAM stats (2s cadence) |
| `/tmp/iit-gpu-stats-writer` | Stats daemon source (compute node) |
| `/tmp/iit-gpu-stats.log` | Stats daemon log |
