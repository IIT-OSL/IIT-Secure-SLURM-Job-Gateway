# IIT-GPU-Manager

Secure terminal gateway for SLURM GPU job submission. Users connect via SSH and interact with a forced-command TUI — they can never drop to a shell. Every action is logged to SQLite, JSONL, and optionally Google Sheets before it is executed.

---

## Table of Contents

1. [Demo Mode (no SLURM required)](#demo-mode-no-slurm-required)
2. [Running Tests](#running-tests)
3. [Production Installation](#production-installation)
4. [Adding and Removing Users](#adding-and-removing-users)
5. [Google Sheets Integration (optional)](#google-sheets-integration-optional)
6. [Configuration Reference](#configuration-reference)
7. [Project Layout](#project-layout)
8. [Security Bypass-Test Checklist](#security-bypass-test-checklist)

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

Expected: **39 passed, 5 skipped** (the 5 skips are Unix-socket and symlink tests that require Linux).

To run a specific test file:

```bash
pytest tests/test_validate.py -v
pytest tests/test_jobs.py -v
pytest tests/test_e2e.py -v
```

---

## Production Installation

### Prerequisites (on the SLURM gateway server)

- Linux with systemd
- Python 3.11+
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
3. Install Python dependencies system-wide (`rich`, `questionary`, `google-api-python-client`, `google-auth`)
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
NFS_ROOT="/shared/gpu-jobs" \
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

## Google Sheets Integration (optional)

The audit daemon can mirror every event to a Google Sheet in real time. Sheets failures are non-fatal and never interrupt local logging.

### Step 1 — Create a service account

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → IAM & Admin → Service Accounts
2. Create a new service account (e.g., `iit-gpu-audit`)
3. Download the JSON key file

### Step 2 — Share the spreadsheet

1. Create a new Google Sheet
2. Share it with the service account email (Editor access)
3. Note the spreadsheet ID from the URL: `https://docs.google.com/spreadsheets/d/<SHEET_ID>/`

### Step 3 — Configure the daemon

Edit `/etc/systemd/system/iit-gpu-audit.service`:

```ini
Environment=SHEET_ID=<your-spreadsheet-id>
Environment=SHEET_RANGE=Sheet1!A:H
Environment=GOOGLE_APPLICATION_CREDENTIALS=/var/lib/iit-gpu/service-account.json
```

Copy the key file (readable only by `gpusync`):

```bash
sudo cp service-account.json /var/lib/iit-gpu/
sudo chown gpusync:gpusync /var/lib/iit-gpu/service-account.json
sudo chmod 0400 /var/lib/iit-gpu/service-account.json
```

Reload and restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart iit-gpu-audit
```

Columns written to the sheet: `ts | user | session | action | detail | job_id | remote`

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
| `SHEET_ID` | _(none)_ | Google Sheets ID — leave empty to disable Sheets sync |
| `SHEET_RANGE` | `Sheet1!A:H` | Sheet range for appending rows |
| `GOOGLE_APPLICATION_CREDENTIALS` | _(none)_ | Path to service-account JSON key (daemon only) |

---

## Project Layout

```
iitgpu/
  __init__.py     Package version (1.0.0)
  config.py       Load settings from environment variables
  ui.py           Rich console helpers (header, ok, warn, err, kv, panel)
  splash.py       ASCII art splash screen
  validate.py     Path jail (in_jail, safe_listdir) and input validators
  auditclient.py  Emit audit events to daemon socket; spool fallback
  slurm.py        SLURM wrappers (get_partitions, submit_job, queue, cancel)
  jobs.py         JobSpec dataclass, sbatch script renderer
  wizard.py       Interactive job builder with jailed file browser
  monitor.py      Queue view, job cancel, jailed log tail, cluster status
  menu.py         Main 5-item menu loop
  __main__.py     Hardened entry point: signal traps, session logging, --selftest

deploy/
  audit_daemon.py         Runs as gpusync — SQLite WAL + JSONL + Google Sheets
  iit-gpu-audit.service   systemd service unit
  sshd-gateway.conf       sshd drop-in: ForceCommand + all forwarding disabled
  sudoers-gateway         Allow %gpuusers to run sbatch/squeue/scancel/sinfo only
  install.sh              Root installer script

tests/
  test_config.py          Config unit tests
  test_validate.py        Jail and validator unit tests
  test_auditclient.py     Socket emit and spool fallback tests
  test_jobs.py            sbatch renderer tests
  test_e2e.py             End-to-end demo and audit event tests
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
| Directly editing the audit DB or JSONL | Files owned by `gpusync` (mode 750) — unprivileged users cannot read or write |
| Submitting a job while audit daemon is down | `log_or_block()` tries socket only; returns `False` → submission refused |
| Requesting 100 GPUs | `clamp_int(100, 1, MAX_GPUS, 1)` silently caps to `MAX_GPUS` (default 8) |
| Setting `LD_PRELOAD` or other env vars via SSH | `PermitUserEnvironment no` + launcher uses `env -i` to strip inherited env |
| `PermitUserRC` (.bashrc execution) | `PermitUserRC no` |
| Crashing the tool to expose a shell prompt | `try/except Exception` in launcher; `finally` logs `session_end`; always exits non-zero |
| Port forwarding / tunnelling | `PermitTunnel no`, `GatewayPorts no`, `AllowStreamLocalForwarding no` |
