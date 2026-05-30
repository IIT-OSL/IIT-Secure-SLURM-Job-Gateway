# IIT-GPU-Manager

Secure terminal gateway for SLURM GPU job submission at IIT. Users connect via SSH and interact with a forced-command TUI — they can never drop to a shell. Every action is logged to SQLite and JSONL before it is executed.

**Target cluster:** IIT login-node → RTX 5090, 1 GPU, 16 CPUs, 63 GB RAM, SLURM 25.11.2, `/shared` NFS

---

## Table of Contents

1. [Overview](#overview)
2. [Demo Mode (no SLURM required)](#demo-mode-no-slurm-required)
3. [Running Tests](#running-tests)
4. [Production Installation](#production-installation)
5. [Adding and Removing Users](#adding-and-removing-users)
6. [Menu Reference](#menu-reference)
7. [Configuration Reference](#configuration-reference)
8. [Audit Logs](#audit-logs)
9. [Project Layout](#project-layout)
10. [Security Bypass-Test Checklist](#security-bypass-test-checklist)

---

## Overview

```
SSH login → ASCII splash → Main Menu
   ├── 1. Upload Files   (store datasets in /shared for jobs)
   ├── 2. Setup          (environment, data, model, health check)
   ├── 3. Run a job      (submit ML training / inference job)
   ├── 4. Monitor        (live dashboard, job queue, logs)
   ├── 5. Advanced       (SLURM command shell)
   └── 6. Quit
```

Users with no Linux knowledge can SSH in, set up a PyTorch/TensorFlow/JAX conda environment, upload their dataset, and submit a training job entirely through menus. Advanced users retain full audited SLURM command access through the shell screen.

**Security model (always on):**
- `ForceCommand` in sshd — users land in the TUI, never a shell
- All TCP/X11/agent forwarding disabled
- Every action (job submit, cancel, shell command, file browse) written to SQLite + JSONL before execution
- Path jail: all file access validated via `in_jail()` / `Path.resolve()` — symlinks followed before comparison
- Resource ceilings enforced server-side regardless of what users type
- Environment stripped via `env -i` in the launcher

---

## Demo Mode (no SLURM required)

Run the full TUI on any machine without a SLURM cluster.

### Prerequisites

- Python 3.11 or newer
- `pip`

### Setup

```bash
git clone <repo-url>
cd IIT-Secure-SLURM-Job-Gateway

pip install rich questionary
```

### Launch

```bash
python -m iitgpu --demo
```

The `--demo` flag simulates SLURM partitions, job submission, and the queue entirely in memory. No SLURM binaries or audit daemon are required.

To skip the ASCII splash screen:

```bash
python -m iitgpu --demo --no-splash
```

### Built-in selftest

Verifies the jail logic, validators, and audit fallback without any interactive prompts:

```bash
python -m iitgpu --selftest
```

Expected output:

```
=== IIT-GPU-Manager Selftest ===

  [PASS] in_jail accepts file under root
  [PASS] in_jail rejects /etc/shadow
  [PASS] in_jail rejects ../etc escape
  [PASS] clamp_int caps 9999 to MAX_GPUS
  [PASS] clean_time_limit clamps 999h to 72h
  [PASS] clean_time_limit rejects garbage
  [PASS] clean_run_command flattens newlines

All checks passed!
```

---

## Running Tests

```bash
pip install pytest rich questionary
pytest tests/ -v
```

To run a specific test file:

```bash
pytest tests/test_validate.py -v
pytest tests/test_jobs.py -v
pytest tests/test_e2e.py -v
pytest tests/test_setup.py -v
pytest tests/test_dashboard.py -v
pytest tests/test_shell.py -v
pytest tests/test_envbuilder.py -v
pytest tests/test_upload.py -v
```

---

## Production Installation

### Prerequisites (on the SLURM gateway server)

- Linux with systemd
- Python 3.11+
- Conda (Miniconda or Anaconda) available system-wide for environment building
- OpenSSH server with `sshd_config.d/` support (OpenSSH 8.2+)
- SLURM installed (`sbatch`, `squeue`, `scancel`, `sinfo` in `/usr/bin/`)
- `sudo` and `visudo` available
- Root access for the installer

### Step 1 — Clone the repository

```bash
git clone <repo-url> /tmp/iit-gpu-manager-src
cd /tmp/iit-gpu-manager-src
```

### Step 2 — Run the installer

```bash
sudo bash deploy/install.sh
```

The installer will:

1. Create system group `gpuusers` and system users `slurmsvc` (no-login), `gpusync` (no-login)
2. Copy the project to `/opt/iit-gpu/` (root-owned, mode 755 — users cannot modify the tool)
3. Install Python dependencies system-wide (`rich`, `questionary`)
4. Create `/var/lib/iit-gpu/` owned by `gpusync` (mode 750 — users cannot read audit logs)
5. Install a hardened launcher at `/usr/local/bin/iit-gpu-manager` that uses `env -i` to strip the inherited environment
6. Install and start the `iit-gpu-audit` systemd service (runs as `gpusync`)
7. Install `deploy/sshd-gateway.conf` to `/etc/ssh/sshd_config.d/` and validate it with `sshd -t`
8. Install `deploy/sudoers-gateway` to `/etc/sudoers.d/` and validate it with `visudo -cf`

### Step 3 — Configure NFS_ROOT

Edit the launcher at `/usr/local/bin/iit-gpu-manager` and set `NFS_ROOT` to the path users should be jailed to:

```bash
sudo nano /usr/local/bin/iit-gpu-manager
```

Add the line inside the `env -i` block:

```
NFS_ROOT="/shared" \
```

Ensure `/shared/envs/` exists and is writable by `gpuusers`:

```bash
sudo mkdir -p /shared/envs
sudo chown root:gpuusers /shared/envs
sudo chmod 2775 /shared/envs
```

### Step 4 — Verify the service

```bash
systemctl status iit-gpu-audit
journalctl -u iit-gpu-audit -f
```

### Step 5 — Test SSH access

Add yourself to `gpuusers` (see next section), then test:

```bash
ssh <your-username>@<gateway-host>
```

You should land directly in the IIT-GPU-Manager TUI with no shell access.

---

## Adding and Removing Users

### Add a user

```bash
# Grant access
sudo usermod -aG gpuusers <username>

# Verify
groups <username>
```

The user's next SSH login will be forced into the TUI.

### Remove a user

```bash
sudo gpasswd -d <username> gpuusers
```

### Check who has access

```bash
getent group gpuusers
```

---

## Menu Reference

### 1. Upload Files

Copies local files or directories into `/shared/<username>/data/` via a jailed file browser. Displays file count and total size before confirming. All paths are validated with `in_jail()`.

### 2. Setup

Five sequential steps for first-time users:

| Step | What it does |
|------|--------------|
| **Health check** | Runs `sinfo`, verifies `/shared` is writable, verifies `/shared/envs/` exists. Blocks if any check fails. |
| **Environment builder** | Framework picker → version list → `conda create` + `pip install` → registers env. Supported: PyTorch 2.4/2.5, TensorFlow 2.18, JAX 0.4, Bare Python 3.11. Optional `requirements.txt` via jailed browser. |
| **Data upload** | Same as menu item 1 — jailed copy to `/shared/<user>/data/`. |
| **Model download** | HuggingFace repo ID or arbitrary URL via existing `models.py` functions. |
| **Smoke test** | Submits a 2-line SLURM job, attaches to live output, prints `✔ CUDA: True` or `✘ CUDA: False`. |

### 3. Run a Job

4-step wizard — resources are set automatically:

| Task type | GPUs | CPUs | Mem | Time limit |
|-----------|------|------|-----|------------|
| Train from scratch | 1 | 16 | 60 G | no limit |
| Fine-tune a model | 1 | 16 | 60 G | no limit |
| Run inference | 1 | 8 | 32 G | 04:00:00 |
| Quick test | 1 | 4 | 16 G | 00:30:00 |

Steps: **Task type** → **Environment** (from registry) → **Script** (jailed `.py`/`.sh` browser) → **Extra args** → sbatch preview → Submit / Save template / Discard.

After submission, the user can watch live output immediately in the dashboard.

### 4. Monitor

Sub-menu with four options:

- **Live dashboard** — Rich `Live` layout, auto-refreshes every 3 s. Shows job list + rolling last-20-lines of the selected job's output file. Keys: `Q` quit, `S` switch job, `C` cancel, `R` force refresh.
- **View my queue** — Prints current `squeue` output for the logged-in user.
- **Cancel a job** — Prompts for job ID, calls `scancel`.
- **View job log** — Jailed file browser to tail any `.out` file in `/shared/<user>/`.
- **Cluster status** — Table of partitions, states, node counts, GPUs per node.

### 5. Advanced (SLURM Shell)

A restricted command loop — never uses `shell=True`. Allowed commands: `sbatch`, `squeue`, `scancel`, `sinfo`, `tail`. Every typed line is audit-logged. Paths passed to `sbatch` and `tail` must pass `in_jail()`.

```
slurm> squeue -u $USER
slurm> tail -f /shared/myuser/jobs/train_20260530/slurm-143.out
slurm> exit
```

---

## Configuration Reference

All settings are environment variables. Set them in the launcher (`/usr/local/bin/iit-gpu-manager`) for user-facing limits, or in the systemd unit for daemon settings.

### Application settings

| Variable | Default | Purpose |
|----------|---------|---------|
| `NFS_ROOT` | `/shared` | Root of the path jail — users cannot access anything above this |
| `JOBS_SUBDIR` | `jobs` | Subdirectory under `NFS_ROOT` where job folders are created |
| `DEMO_MODE` | `0` | Set to `1` to simulate SLURM (no cluster needed) |

### Resource ceilings

| Variable | Default | Purpose |
|----------|---------|---------|
| `MAX_GPUS` | `8` | Hard ceiling on GPUs per job |
| `MAX_CPUS` | `64` | Hard ceiling on CPUs per job |
| `MAX_MEM_GB` | `256` | Hard ceiling on memory (GB) per job |
| `MAX_HOURS` | `72` | Hard ceiling on job duration (hours) |

### Audit settings

| Variable | Default | Purpose |
|----------|---------|---------|
| `AUDIT_SOCKET` | `/run/iit-gpu/audit.sock` | Unix socket to the audit daemon |
| `AUDIT_SPOOL` | `/run/iit-gpu/spool` | Fallback spool directory when socket unavailable |
| `AUDIT_STATE` | `/var/lib/iit-gpu` | Directory for `audit.db` and `audit.jsonl` (daemon only) |

---

## Audit Logs

The `iit-gpu-audit` systemd service (runs as `gpusync`, mode 750) maintains two log files that users cannot read or write:

| File | Format | Purpose |
|------|--------|---------|
| `/var/lib/iit-gpu/audit.db` | SQLite (WAL mode) | Queryable structured log of every event |
| `/var/lib/iit-gpu/audit.jsonl` | Newline-delimited JSON | Append-only human-readable log |

Columns written per event: `ts | user | session | action | detail | job_id | remote`

Administrators can view logs with the bundled viewer:

```bash
sudo -u gpusync python deploy/iit-gpu-log
```

The audit gate is enforced at submission time: `auditclient.log_or_block()` must succeed before any job is submitted. If the audit daemon is unreachable and the spool directory is also unavailable, the submission is refused.

---

## Project Layout

```
iitgpu/
  __init__.py       Package version (1.0.0)
  config.py         Load settings from environment variables
  ui.py             Rich console helpers (header, ok, warn, err, kv, panel)
  splash.py         ASCII art splash screen
  validate.py       Path jail (in_jail, safe_listdir) and input validators
  auditclient.py    Emit audit events to daemon socket; spool fallback
  slurm.py          SLURM wrappers (get_partitions, submit_job, queue, cancel)
  jobs.py           JobSpec dataclass, sbatch script renderer, task defaults
  templates.py      Built-in job presets (PyTorch, HuggingFace, Inference, Quick Debug)
  envs.py           Conda/venv environment registry
  models.py         HuggingFace and URL model downloader
  upload.py         Jailed file/folder copy to /shared/<user>/data/
  setup.py          Setup wizard: health check → env → data → model → smoke test
  envbuilder.py     Conda env creation: framework picker → conda create + pip install
  wizard.py         4-step job builder (task type → env → script → args)
  dashboard.py      Live dashboard: Rich Live layout, auto-refresh every 3 s
  shell.py          SLURM command shell: restricted input loop, audit-logged
  monitor.py        Queue view, job cancel, jailed log tail, cluster status
  menu.py           Main 6-item menu loop
  __main__.py       Hardened entry point: signal traps, session logging, --selftest

deploy/
  audit_daemon.py         Runs as gpusync — SQLite WAL + JSONL logging
  iit-gpu-audit.service   systemd service unit
  iit-gpu-log             Admin audit log viewer
  sshd-gateway.conf       sshd drop-in: ForceCommand + all forwarding disabled
  sudoers-gateway         Allow %gpuusers to run sbatch/squeue/scancel/sinfo only
  install.sh              Root installer script

tests/
  test_config.py          Config unit tests
  test_validate.py        Jail and validator unit tests
  test_auditclient.py     Socket emit and spool fallback tests
  test_jobs.py            sbatch renderer and task defaults tests
  test_templates.py       Built-in preset tests
  test_e2e.py             End-to-end demo and audit event tests
  test_setup.py           Health check pass/fail, env builder, smoke test generation
  test_dashboard.py       Log tail logic, job switching, missing log file handling
  test_shell.py           Allowed commands pass, blocked commands rejected, in_jail enforcement
  test_envbuilder.py      Package map lookup, conda-not-found graceful failure
  test_upload.py          Jailed file copy, size calculation, path validation
```

---

## Security Bypass-Test Checklist

Every item in this table must fail for the system to be correctly deployed:

| Attack | Defence |
|--------|---------|
| `ssh user@host bash` | `ForceCommand` in sshd drop-in always runs `iit-gpu-manager` |
| `ssh user@host -L 8080:internal:80` | `AllowTcpForwarding no` |
| `ssh user@host -A` (agent forwarding) | `AllowAgentForwarding no` |
| X11 forwarding | `X11Forwarding no` |
| Ctrl-Z to background into a shell | `SIGTSTP` is ignored in `__main__.py` |
| Symlink inside job folder pointing to `/etc/shadow` | `in_jail()` calls `Path.resolve()` before comparing — symlinks are followed |
| `../../etc` path in file browser | `in_jail()` resolves `..` via `realpath` — escape rejected |
| Typing a raw shell command in Advanced shell | `shell.py` allowlist: only `sbatch`, `squeue`, `scancel`, `sinfo`, `tail` execute |
| `sbatch /etc/cron.d/malicious` in Advanced shell | Path must pass `in_jail()` before `sbatch` is called |
| Directly editing the audit DB or JSONL | Files owned by `gpusync` (mode 750) — unprivileged users cannot read or write |
| Submitting a job while audit daemon is down | `log_or_block()` tries socket only; returns `False` → submission refused |
| Requesting 100 GPUs | `clamp_int(100, 1, MAX_GPUS, 1)` silently caps to `MAX_GPUS` (default 8) |
| Setting `LD_PRELOAD` or other env vars via SSH | `PermitUserEnvironment no` + launcher uses `env -i` to strip inherited env |
| `PermitUserRC` (.bashrc execution) | `PermitUserRC no` |
| Crashing the tool to expose a shell prompt | `try/except Exception` in launcher; `finally` logs `session_end`; always exits non-zero |
| Port forwarding / tunnelling | `PermitTunnel no`, `GatewayPorts no`, `AllowStreamLocalForwarding no` |
| Conda env created outside `/shared/envs/` | `envbuilder.py` hardcodes target path; `in_jail()` validates any user-provided path |
