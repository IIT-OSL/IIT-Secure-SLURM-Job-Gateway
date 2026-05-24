# IIT-GPU-Manager — Complete How-To Guide

Secure terminal gateway for SLURM GPU job submission. Users connect via SSH and are dropped directly into a forced-command TUI — they can never reach a shell. Every action is logged to SQLite, JSONL, and optionally Google Sheets before it is executed.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Prerequisites](#2-prerequisites)
3. [Demo Mode (no SLURM required)](#3-demo-mode-no-slurm-required)
4. [Running the Test Suite](#4-running-the-test-suite)
5. [Production Installation](#5-production-installation)
6. [Post-Install Configuration](#6-post-install-configuration)
7. [User Management](#7-user-management)
8. [Audit Daemon](#8-audit-daemon)
9. [Google Sheets Integration](#9-google-sheets-integration)
10. [Full Configuration Reference](#10-full-configuration-reference)
11. [How the Code Works](#11-how-the-code-works)
12. [Security Model](#12-security-model)
13. [Security Bypass-Test Checklist](#13-security-bypass-test-checklist)
14. [Troubleshooting](#14-troubleshooting)

---

## 1. Architecture Overview

```
User SSH ──► sshd (ForceCommand) ──► /usr/local/bin/iit-gpu-manager
                                           │
                                     iitgpu package (TUI)
                                           │
                        ┌──────────────────┼──────────────────┐
                        │                  │                  │
                   validate.py         slurm.py        auditclient.py
                   (path jail,         (sbatch/squeue   (Unix datagram
                    input sanitise)     via sudo)         socket)
                                                          │
                                                   audit_daemon.py
                                                   (gpusync user)
                                                   SQLite + JSONL
                                                   + Google Sheets
```

**Key security properties:**

- SSH ForceCommand prevents shell access entirely; all forwarding disabled.
- Job commands run as `slurmsvc` via a narrow `sudoers` rule — never as root.
- All file access is validated through a path jail (`in_jail()`) that resolves symlinks before comparing.
- The audit daemon runs as an unprivileged system user (`gpusync`); its state directory is not readable by end users.
- Every job submission is audit-logged *before* `sbatch` is called; if logging fails the submission is refused.
- Signal handlers suppress `SIGTSTP` (Ctrl-Z) so users cannot background into a shell.

---

## 2. Prerequisites

### Demo / development machine

| Requirement | Version |
|---|---|
| Python | 3.11 or newer |
| pip | any recent |

### Production SLURM gateway server

| Requirement | Notes |
|---|---|
| Linux with systemd | Ubuntu 22.04 LTS recommended |
| Python 3.11+ | `python3 --version` to check |
| OpenSSH server | 8.2+ for `sshd_config.d/` drop-in support |
| SLURM binaries | `sbatch`, `squeue`, `scancel`, `sinfo` all in `/usr/bin/` |
| `sudo` / `visudo` | installer validates sudoers before activating |
| Root access | installer must run as root |
| NFS or shared storage | a path that all compute nodes can read job scripts from |

---

## 3. Demo Mode (no SLURM required)

Demo mode runs the complete TUI, simulating SLURM partitions, job submission, and a job queue entirely in memory. No SLURM binaries or audit daemon needed.

### 3.1 Clone and install dependencies

```bash
git clone <repo-url>
cd IIT-Secure-SLURM-Job-Gateway

pip install rich questionary
```

### 3.2 Start the TUI

```bash
python -m iitgpu --demo
```

You will see the ASCII splash screen followed by the main menu with five options:

```
1. Create & submit GPU job
2. Monitor jobs
3. Cluster status
4. Settings (read-only)
5. Quit
```

To skip the splash screen:

```bash
python -m iitgpu --demo --no-splash
```

### 3.3 What demo mode simulates

| Real system | Demo substitute |
|---|---|
| `sinfo` querying the cluster | Three hardcoded partitions: `gpu-short` (4 nodes, 4 GPUs/node), `gpu-long` (8 nodes, 8 GPUs/node), `gpu-debug` (1 node, 2 GPUs/node) |
| `sbatch` submitting a script | In-memory counter returning auto-incremented job IDs starting at 1001 |
| `squeue` listing jobs | In-memory list updated by submit/cancel |
| `scancel` cancelling a job | Removes from the in-memory list |
| Audit daemon socket | Falls back to spool directory (or `/dev/null` if spool unavailable) |

### 3.4 Built-in selftest

Verifies the jail logic, validators, and audit spool fallback without any interactive prompts:

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
  [PASS] audit falls back to spool when socket missing
  [PASS] spool file created

All checks passed!
```

A non-zero exit code means at least one check failed.

---

## 4. Running the Test Suite

### 4.1 Install test dependencies

```bash
pip install pytest rich questionary
```

### 4.2 Run all tests

```bash
pytest tests/ -v
```

Expected result: **39 passed, 5 skipped**. The 5 skips are Unix-socket and symlink tests that only run on Linux.

### 4.3 Run individual test files

```bash
pytest tests/test_config.py    -v   # Config env-var loading
pytest tests/test_validate.py  -v   # Path jail and input validators
pytest tests/test_jobs.py      -v   # sbatch script rendering
pytest tests/test_auditclient.py -v # Socket emit and spool fallback
pytest tests/test_e2e.py       -v   # End-to-end demo + audit events
```

### 4.4 What each test file covers

| File | Tests |
|---|---|
| `test_config.py` | Default values, env-var overrides, `jobs_dir()` path joining |
| `test_validate.py` | `in_jail()` accepts/rejects, `safe_listdir()`, `clamp_int()`, `clean_time_limit()`, `clean_job_name()`, `clean_run_command()` |
| `test_jobs.py` | `make_job_folder()` structure, `render_sbatch()` all fields, `write_sbatch()` file creation |
| `test_auditclient.py` | Socket delivery of 3 events, spool fallback when socket missing, `log_or_block()` returns False when both socket and spool fail |
| `test_e2e.py` | `--selftest` subprocess, full demo submit+queue cycle, 3-event spool accumulation |

---

## 5. Production Installation

### 5.1 Clone to a staging directory

```bash
git clone <repo-url> /tmp/iit-gpu-manager-src
cd /tmp/iit-gpu-manager-src
```

### 5.2 Run the installer

```bash
sudo bash deploy/install.sh
```

The installer performs these steps in order:

| Step | What happens |
|---|---|
| 1 | Creates system group `gpuusers` (if not present) |
| 2 | Creates no-login system user `slurmsvc` (SLURM command proxy) |
| 3 | Creates no-login system user `gpusync` (audit daemon owner) |
| 4 | Copies entire project to `/opt/iit-gpu/` (root-owned, mode 755) |
| 5 | `pip3 install rich questionary google-api-python-client google-auth` |
| 6 | Creates `/var/lib/iit-gpu/` owned by `gpusync`, mode 750 |
| 7 | Writes hardened launcher at `/usr/local/bin/iit-gpu-manager` using `env -i` |
| 8 | Installs and starts the `iit-gpu-audit` systemd service |
| 9 | Installs `deploy/sshd-gateway.conf` → `/etc/ssh/sshd_config.d/99-iit-gpu-gateway.conf`, validates with `sshd -t`, then reloads sshd |
| 10 | Installs `deploy/sudoers-gateway` → `/etc/sudoers.d/iit-gpu-gateway`, validates with `visudo -cf` |

If sshd or sudoers validation fails, the installer removes the offending file and exits with an error — it never leaves a broken config in place.

### 5.3 What the installer creates

```
/opt/iit-gpu/                           ← project files (root-owned, read-only for users)
/usr/local/bin/iit-gpu-manager          ← hardened launcher (env-stripped)
/var/lib/iit-gpu/                       ← audit DB + JSONL (gpusync-owned, mode 750)
/run/iit-gpu/audit.sock                 ← Unix datagram socket (created at daemon start)
/run/iit-gpu/spool/                     ← spool directory for offline events
/etc/systemd/system/iit-gpu-audit.service
/etc/ssh/sshd_config.d/99-iit-gpu-gateway.conf
/etc/sudoers.d/iit-gpu-gateway
```

### 5.4 The hardened launcher

The installer writes `/usr/local/bin/iit-gpu-manager` with this content:

```bash
#!/bin/bash
exec env -i \
    HOME="$HOME" \
    USER="$USER" \
    LOGNAME="$LOGNAME" \
    PATH="/usr/local/bin:/usr/bin:/bin" \
    SSH_CLIENT="${SSH_CLIENT:-}" \
    TERM="${TERM:-xterm}" \
    PYTHONPATH="/opt/iit-gpu" \
    python3 -m iitgpu --no-splash
```

`env -i` strips the entire inherited environment before launching Python. Only the listed variables are passed in. This prevents `LD_PRELOAD`, `PYTHONPATH` injection, and other environment-based attacks.

---

## 6. Post-Install Configuration

### 6.1 Set NFS_ROOT (required)

Edit the launcher and add `NFS_ROOT` inside the `env -i` block:

```bash
sudo nano /usr/local/bin/iit-gpu-manager
```

Add this line (replace the path with your actual shared filesystem mount):

```bash
exec env -i \
    HOME="$HOME" \
    USER="$USER" \
    LOGNAME="$LOGNAME" \
    PATH="/usr/local/bin:/usr/bin:/bin" \
    SSH_CLIENT="${SSH_CLIENT:-}" \
    TERM="${TERM:-xterm}" \
    PYTHONPATH="/opt/iit-gpu" \
    NFS_ROOT="/shared/gpu-jobs" \
    python3 -m iitgpu --no-splash
```

`NFS_ROOT` is the path jail root. Users cannot browse, read, or write anything outside this directory. Job scripts are created under `NFS_ROOT/jobs/<username>/<jobname_timestamp>/`.

### 6.2 Verify the audit daemon

```bash
systemctl status iit-gpu-audit
journalctl -u iit-gpu-audit -f
```

You should see:

```
iit-gpu-audit.service — IIT GPU Audit Daemon
   Active: active (running)
...
INFO Listening on /run/iit-gpu/audit.sock
```

### 6.3 Verify sshd config is loaded

```bash
sshd -T | grep -i forcecommand
```

Expected output:

```
forcecommand /usr/local/bin/iit-gpu-manager
```

### 6.4 Verify sudoers rule

```bash
sudo -l -U <a-gpuusers-member>
```

Expected output includes:

```
(slurmsvc) NOPASSWD: /usr/bin/sbatch, /usr/bin/squeue, /usr/bin/scancel, /usr/bin/sinfo
```

### 6.5 Test SSH access

```bash
# Add yourself first (see section 7)
sudo usermod -aG gpuusers $USER

# Then open a new SSH session
ssh $USER@localhost
```

You should land in the TUI immediately. No shell prompt, no `.bashrc` execution.

---

## 7. User Management

### 7.1 Add a user

```bash
sudo usermod -aG gpuusers <username>
```

The change takes effect on the user's **next SSH login**. Existing sessions are not affected.

### 7.2 Remove a user

```bash
sudo gpasswd -d <username> gpuusers
```

The user's next SSH login will proceed normally (standard shell) or be denied, depending on your `sshd_config` defaults.

### 7.3 List all users with access

```bash
getent group gpuusers
```

### 7.4 Pre-create a user account

If the user account does not yet exist on the gateway:

```bash
sudo useradd -m -s /bin/bash <username>
sudo passwd <username>
sudo usermod -aG gpuusers <username>
```

### 7.5 SSH public key authentication (recommended)

On the gateway, add the user's public key to their `~/.ssh/authorized_keys`. Password authentication should be disabled in your main `sshd_config`:

```
PasswordAuthentication no
PubkeyAuthentication yes
```

---

## 8. Audit Daemon

The audit daemon (`deploy/audit_daemon.py`) runs as the unprivileged `gpusync` user. It owns all persistent audit state and is the only process that can write to `/var/lib/iit-gpu/`.

### 8.1 How events reach the daemon

1. The TUI calls `auditclient.log(action, detail, job_id)`.
2. `auditclient` builds a JSON payload and sends it via a **Unix datagram socket** (`/run/iit-gpu/audit.sock`).
3. If the socket send succeeds, done.
4. If the socket is unavailable (daemon not running, or socket not yet created), the payload is written to a spool file under `/run/iit-gpu/spool/`.
5. When the daemon restarts, it drains the spool directory and processes any queued events.

### 8.2 Event schema

Each event is a JSON object with these fields:

| Field | Description |
|---|---|
| `ts` | ISO-8601 UTC timestamp |
| `user` | Username of the SSH session owner |
| `session` | UUIDv4 unique to this login session |
| `action` | Event type (see table below) |
| `detail` | Human-readable detail string |
| `job_id` | SLURM job ID (empty string if not applicable) |
| `remote` | Client IP from `SSH_CLIENT` env var, or `"local"` |

### 8.3 Audit event types

| Action | When emitted |
|---|---|
| `session_start` | When the TUI starts |
| `session_end` | When the TUI exits (including crash) |
| `signal_exit` | When SIGINT or SIGQUIT is received |
| `tool_crash` | When an unhandled exception occurs |
| `job_submit` | *Before* calling `sbatch` (blocks submission if this fails) |
| `job_submitted_ok` | After `sbatch` succeeds |
| `job_submit_failed` | After `sbatch` returns non-zero |
| `job_template_saved` | When the user saves a script without submitting |
| `job_cancel` | When a cancel is requested |
| `selftest` | During `--selftest` socket fallback check |

### 8.4 Persistent storage

| File | Format | Purpose |
|---|---|---|
| `/var/lib/iit-gpu/audit.db` | SQLite (WAL mode) | Queryable structured log |
| `/var/lib/iit-gpu/audit.jsonl` | JSONL (one event per line) | Append-only flat log for parsing/export |

Query the SQLite database:

```bash
sudo -u gpusync sqlite3 /var/lib/iit-gpu/audit.db \
  "SELECT ts, user, action, detail, job_id FROM events ORDER BY id DESC LIMIT 20;"
```

Tail the JSONL log:

```bash
sudo -u gpusync tail -f /var/lib/iit-gpu/audit.jsonl | python3 -m json.tool
```

### 8.5 Daemon service management

```bash
# Status
systemctl status iit-gpu-audit

# Live logs
journalctl -u iit-gpu-audit -f

# Restart (e.g. after config changes)
sudo systemctl restart iit-gpu-audit

# Stop
sudo systemctl stop iit-gpu-audit

# Disable (won't start on boot)
sudo systemctl disable iit-gpu-audit
```

### 8.6 The "audit-or-block" safety rule

`auditclient.log_or_block()` is called before every `sbatch` invocation. It:

1. Tries to send to the socket.
2. Falls back to the spool directory.
3. Returns `False` only if **both** the socket send and the spool write fail.

If it returns `False`, the wizard refuses to call `sbatch` and prints an error. This ensures no job can be submitted without at least a spool record of the attempt.

---

## 9. Google Sheets Integration

The audit daemon can mirror every event to a Google Sheet in real time. Sheets failures are non-fatal and never interrupt local SQLite/JSONL logging.

### 9.1 Create a Google Cloud service account

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → IAM & Admin → Service Accounts.
2. Click **Create Service Account**. Name it e.g. `iit-gpu-audit`.
3. No roles needed at the project level.
4. Click the service account → **Keys** → **Add Key** → **Create new key** → JSON.
5. Download the JSON key file to your local machine.

### 9.2 Create and share the spreadsheet

1. Create a new Google Sheet.
2. Click **Share** → paste the service account email (it ends in `@<project>.iam.gserviceaccount.com`) → **Editor**.
3. Copy the spreadsheet ID from the URL:
   ```
   https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit
   ```

### 9.3 Install the key on the server

```bash
# Copy the key file to the server
scp service-account.json root@<gateway>:/tmp/

# Move it to the state directory, owned by gpusync, readable only by gpusync
sudo mv /tmp/service-account.json /var/lib/iit-gpu/
sudo chown gpusync:gpusync /var/lib/iit-gpu/service-account.json
sudo chmod 0400 /var/lib/iit-gpu/service-account.json
```

### 9.4 Configure the systemd unit

Edit the service file:

```bash
sudo systemctl edit iit-gpu-audit
```

This opens a drop-in override editor. Add:

```ini
[Service]
Environment=SHEET_ID=<your-spreadsheet-id>
Environment=SHEET_RANGE=Sheet1!A:H
Environment=GOOGLE_APPLICATION_CREDENTIALS=/var/lib/iit-gpu/service-account.json
```

Save and reload:

```bash
sudo systemctl daemon-reload
sudo systemctl restart iit-gpu-audit
```

Verify:

```bash
journalctl -u iit-gpu-audit -f
```

You should see events logged without "Google Sheets append failed" warnings.

### 9.5 Sheet column layout

Columns are appended in this order:

| A | B | C | D | E | F | G |
|---|---|---|---|---|---|---|
| ts | user | session | action | detail | job_id | remote |

---

## 10. Full Configuration Reference

All settings are environment variables. For user-facing limits set them in the launcher (`/usr/local/bin/iit-gpu-manager`). For daemon settings set them in the systemd unit.

### 10.1 Application settings

| Variable | Default | Purpose |
|---|---|---|
| `NFS_ROOT` | `/shared` | Path jail root — users cannot read/write/browse above this path |
| `JOBS_SUBDIR` | `jobs` | Subdirectory under `NFS_ROOT` where job folders are created |
| `DEMO_MODE` | `0` | Set to `1` to simulate SLURM entirely in memory |

### 10.2 Resource ceilings

| Variable | Default | Purpose |
|---|---|---|
| `MAX_GPUS` | `8` | Hard ceiling on GPUs per job |
| `MAX_CPUS` | `64` | Hard ceiling on CPUs per job |
| `MAX_MEM_GB` | `256` | Hard ceiling on memory (GB) per job |
| `MAX_HOURS` | `72` | Hard ceiling on job duration (hours) |

Inputs above these ceilings are silently clamped down, never rejected with an error. Inputs below 1 are clamped up to 1.

### 10.3 Audit settings

| Variable | Default | Purpose |
|---|---|---|
| `AUDIT_SOCKET` | `/run/iit-gpu/audit.sock` | Unix socket path for the audit daemon |
| `AUDIT_SPOOL` | `/run/iit-gpu/spool` | Spool directory when socket is unavailable |
| `AUDIT_STATE` | `/var/lib/iit-gpu` | Directory for `audit.db` and `audit.jsonl` (daemon only) |
| `SHEET_ID` | _(none)_ | Google Sheets ID — leave empty to disable Sheets sync |
| `SHEET_RANGE` | `Sheet1!A:H` | Sheet range for appending rows |
| `GOOGLE_APPLICATION_CREDENTIALS` | _(none)_ | Path to service-account JSON key (daemon only) |

### 10.4 Example: tighten resource limits

In `/usr/local/bin/iit-gpu-manager`, inside the `env -i` block:

```bash
exec env -i \
    HOME="$HOME" \
    USER="$USER" \
    LOGNAME="$LOGNAME" \
    PATH="/usr/local/bin:/usr/bin:/bin" \
    SSH_CLIENT="${SSH_CLIENT:-}" \
    TERM="${TERM:-xterm}" \
    PYTHONPATH="/opt/iit-gpu" \
    NFS_ROOT="/shared/gpu-jobs" \
    MAX_GPUS=4 \
    MAX_HOURS=24 \
    MAX_MEM_GB=128 \
    python3 -m iitgpu --no-splash
```

---

## 11. How the Code Works

### 11.1 Package layout

```
iitgpu/
  __init__.py     Version string (1.0.0)
  __main__.py     Entry point: CLI args, signal handlers, session logging
  config.py       Config dataclass loaded from environment variables
  validate.py     Path jail (in_jail, safe_listdir) and all input validators
  auditclient.py  Build and send audit events; spool fallback
  slurm.py        Thin wrappers around sbatch/squeue/scancel/sinfo
  jobs.py         JobSpec dataclass; sbatch script rendering and writing
  wizard.py       Interactive job-builder TUI (questionary prompts)
  monitor.py      Queue view, cancel, log browser, cluster status
  menu.py         Top-level 5-item menu loop
  ui.py           Rich console helpers: header, ok, warn, err, kv, panel
  splash.py       ASCII art splash screen

deploy/
  audit_daemon.py           Audit daemon process
  iit-gpu-audit.service     systemd unit
  sshd-gateway.conf         sshd drop-in config
  sudoers-gateway           sudoers rule
  install.sh                Root installer
```

### 11.2 Entry point (`__main__.py`)

1. Parses `--demo`, `--no-splash`, `--selftest`.
2. If `--selftest`: runs the built-in checks and exits.
3. If `--demo`: sets `DEMO_MODE=1` in the environment.
4. Installs signal handlers: SIGINT/SIGQUIT log and exit cleanly; SIGTSTP is **ignored** (prevents Ctrl-Z backgrounding).
5. Logs `session_start` to the audit daemon.
6. Shows splash screen (unless `--no-splash`).
7. Calls `run_menu()`.
8. On any unhandled exception: logs `tool_crash`, prints an error, exits non-zero.
9. In the `finally` block: always logs `session_end`.

### 11.3 Path jail (`validate.py`)

`in_jail(path)` resolves the full real path (following all symlinks) and checks that it starts with the resolved `NFS_ROOT`. This means:

- `../etc` escapes are caught because `Path.resolve()` expands them before comparison.
- Symlinks pointing outside `NFS_ROOT` are caught because `resolve()` follows them first.
- `/etc/shadow` is rejected because it does not start with `NFS_ROOT`.

`safe_listdir(path)` calls `in_jail()` first and returns `[]` on any failure.

`allowed_roots()` also includes the user's home directory unless it would subsume the NFS root (avoids edge-case escapes on machines where home is a parent of NFS_ROOT).

### 11.4 Input validators (`validate.py`)

| Function | What it does |
|---|---|
| `clamp_int(value, lo, hi, default)` | Parses to int; clamps to [lo, hi]; returns default on bad input |
| `clean_time_limit(value)` | Accepts `HH:MM:SS`; clamps hours to `MAX_HOURS`; returns `None` on bad format |
| `clean_job_name(value)` | Strips everything outside `[A-Za-z0-9._-]`; truncates to 64 chars |
| `clean_modules(value)` | Tokenises on `[A-Za-z0-9_.+\-/]+`; returns up to 20 tokens |
| `clean_run_command(value)` | Replaces all control characters (including `\n`) with space; truncates to 1000 chars |

### 11.5 Job submission flow (`wizard.py`)

1. Prompt for job name → sanitised with `clean_job_name()`.
2. Select partition from live `sinfo` output (or demo list).
3. Prompt for GPUs, CPUs, memory — each clamped with `clamp_int()`.
4. Prompt for time limit — validated/clamped with `clean_time_limit()`.
5. Optional file attachment browser — every path checked with `in_jail()`.
6. Prompt for run command → sanitised with `clean_run_command()`.
7. Prompt for modules → sanitised with `clean_modules()`.
8. Render sbatch script and display preview.
9. User chooses Submit / Save template only / Discard.
10. If Submit: call `auditclient.log_or_block("job_submit")` — refuse if it returns `False`.
11. Call `submit_job(sbatch_path)` which runs `sudo -u slurmsvc sbatch <path>`.
12. Log `job_submitted_ok` or `job_submit_failed`.

### 11.6 sbatch script format (`jobs.py`)

`render_sbatch()` generates:

```bash
#!/bin/bash
#SBATCH --job-name=<name>
#SBATCH --partition=<partition>
#SBATCH --gres=gpu:<gpus>
#SBATCH --cpus-per-task=<cpus>
#SBATCH --mem=<mem_gb>G
#SBATCH --time=<HH:MM:SS>
#SBATCH --output=<folder>/slurm-%j.out
#SBATCH --error=<folder>/slurm-%j.err

module load <mod1>
module load <mod2>
...

cd <folder>
<run_command>
```

Job folders are created at `NFS_ROOT/jobs/<username>/<jobname>_<YYYYMMDD_HHMMSS>/`.

### 11.7 Audit client (`auditclient.py`)

- Each TUI process generates a unique `_SESSION_ID` (UUIDv4) at import time.
- `_USER` is set via `getpass.getuser()`.
- `_REMOTE` is the first token of `SSH_CLIENT` (the client IP), or `"local"`.
- Events are sent as raw JSON bytes over a Unix datagram socket (connectionless, non-blocking).
- Spool files are named `<uuid>.json` so concurrent TUI sessions never collide.

### 11.8 Audit daemon (`deploy/audit_daemon.py`)

- Binds a Unix datagram socket at `AUDIT_SOCKET` and sets it world-writable (0o777) so unprivileged TUI processes can send to it.
- Polls the socket with `select()` in a 5-second loop.
- On each received datagram: inserts into SQLite (WAL mode), appends to JSONL, and appends to Google Sheets.
- Every 30 seconds: drains the spool directory to catch events buffered while the daemon was offline.
- On SIGTERM or SIGINT: sets `_running = False`, closes the socket, closes the DB connection, removes the socket file.

---

## 12. Security Model

### 12.1 SSH layer (`deploy/sshd-gateway.conf`)

The sshd drop-in applies to every member of `gpuusers`:

```
Match Group gpuusers
    ForceCommand /usr/local/bin/iit-gpu-manager
    PermitTTY yes
    AllowTcpForwarding no
    AllowAgentForwarding no
    AllowStreamLocalForwarding no
    X11Forwarding no
    PermitTunnel no
    GatewayPorts no
    PermitUserRC no
    PermitUserEnvironment no
```

`ForceCommand` means that even if the user passes a command (`ssh user@host bash`), sshd ignores it and runs `iit-gpu-manager` instead.

### 12.2 SLURM command delegation (`deploy/sudoers-gateway`)

```
Defaults:gpuusers !lecture, timestamp_timeout=0
%gpuusers ALL=(slurmsvc) NOPASSWD: /usr/bin/sbatch, /usr/bin/squeue, /usr/bin/scancel, /usr/bin/sinfo
```

- Users can only run these four exact binaries, as `slurmsvc`.
- No wildcards. No shell access. No other commands.
- `timestamp_timeout=0` means sudo credential caching is disabled — each call is independently authorised.

### 12.3 Filesystem isolation

- All user-facing file operations go through `in_jail()`.
- Job scripts and outputs are created inside `NFS_ROOT`.
- Audit state (`/var/lib/iit-gpu/`) is owned by `gpusync` (mode 750) — users cannot read or tamper with logs.

### 12.4 Environment stripping

The launcher uses `env -i` so users cannot inject `LD_PRELOAD`, `PYTHONPATH`, or any other variable via the SSH environment. `PermitUserEnvironment no` in the sshd drop-in prevents `~/.ssh/environment` from being read.

### 12.5 Crash containment

All code in `menu.py` and the TUI is wrapped in a top-level `try/except Exception` in `__main__.py`. On any unhandled exception: log the crash, print an error, and exit non-zero. The user never sees a Python traceback that could leak path or code information. The `finally` block guarantees `session_end` is logged even if the process crashes.

---

## 13. Security Bypass-Test Checklist

After deployment, verify every item in this table **fails** (the attack is blocked):

| Attack | Expected defence | How to test |
|---|---|---|
| `ssh user@host bash` | ForceCommand always runs `iit-gpu-manager` | Run the command; you should get the TUI, not a shell |
| `ssh user@host -L 8080:internal:80` | `AllowTcpForwarding no` | Connection should be refused with "Port forwarding is disabled" |
| `ssh user@host -A` | `AllowAgentForwarding no` | Agent forwarding silently disabled |
| X11 forwarding | `X11Forwarding no` | Forwarding silently disabled |
| Ctrl-Z to background | `SIGTSTP` ignored in `__main__.py` | Press Ctrl-Z in TUI — nothing happens |
| Symlink `link → /etc/shadow` inside job folder | `in_jail()` calls `Path.resolve()` first | Create symlink, attempt to select it in file browser; "Access denied" |
| `../../etc` in file browser navigation | `in_jail()` resolves `..` via `realpath` | Try navigating up past `NFS_ROOT`; "Cannot navigate outside allowed paths" |
| Read/write audit DB or JSONL directly | `gpusync`-owned, mode 750 | `cat /var/lib/iit-gpu/audit.db` as a normal user returns "Permission denied" |
| Submit job while audit daemon is down | `log_or_block()` spools; returns False only if spool also fails | Stop daemon, attempt submit; job is spooled or refused, not silently submitted |
| Request 100 GPUs | `clamp_int(100, 1, MAX_GPUS, 1)` caps to `MAX_GPUS` | Enter 100 in wizard; submitted script shows `#SBATCH --gres=gpu:8` (or your MAX_GPUS) |
| `LD_PRELOAD` via SSH environment | `PermitUserEnvironment no` + `env -i` launcher | Set `LD_PRELOAD` in `~/.ssh/environment`; it does not reach the Python process |
| `.bashrc` / `PermitUserRC` | `PermitUserRC no` | `.bashrc` is never sourced on login |
| Port forwarding / tunnelling | `PermitTunnel no`, `GatewayPorts no`, `AllowStreamLocalForwarding no` | Tunnel attempts refused |

---

## 14. Troubleshooting

### 14.1 TUI does not launch on SSH login

Check that the user is in `gpuusers`:
```bash
groups <username>
```

Check that the sshd drop-in is loaded:
```bash
sshd -T -C user=<username> | grep forcecommand
```

Check that `iit-gpu-manager` is executable:
```bash
ls -la /usr/local/bin/iit-gpu-manager
```

Check sshd logs:
```bash
journalctl -u ssh -f   # or sshd on some distros
```

### 14.2 "Audit logging failed. Refusing to submit"

The audit daemon is not reachable and the spool write also failed. Check:

```bash
systemctl status iit-gpu-audit
ls -la /run/iit-gpu/
ls -la /run/iit-gpu/spool/
```

If the socket does not exist, restart the daemon:
```bash
sudo systemctl restart iit-gpu-audit
```

If the spool directory is a file instead of a directory (a misconfiguration), remove it:
```bash
sudo rm /run/iit-gpu/spool
sudo systemctl restart iit-gpu-audit
```

### 14.3 "sbatch: command not found" or job submission fails

Verify SLURM binaries exist in `/usr/bin/`:
```bash
ls /usr/bin/sbatch /usr/bin/squeue /usr/bin/scancel /usr/bin/sinfo
```

Verify the sudoers rule is active:
```bash
sudo -l -U <username>
```

Verify `slurmsvc` user exists:
```bash
id slurmsvc
```

Test sudo manually as the user:
```bash
sudo -u slurmsvc sbatch --version
```

### 14.4 Python import errors on startup

Check that the project was installed correctly:
```bash
ls /opt/iit-gpu/iitgpu/__init__.py
```

Check that PYTHONPATH is set in the launcher:
```bash
grep PYTHONPATH /usr/local/bin/iit-gpu-manager
```

Check that dependencies are installed system-wide:
```bash
python3 -c "import rich; import questionary; print('OK')"
```

### 14.5 Google Sheets events not appearing

Check daemon logs for warnings:
```bash
journalctl -u iit-gpu-audit | grep Sheets
```

Verify the key file is readable by gpusync:
```bash
sudo -u gpusync cat /var/lib/iit-gpu/service-account.json | head -3
```

Verify `SHEET_ID` and `GOOGLE_APPLICATION_CREDENTIALS` are set in the unit:
```bash
systemctl show iit-gpu-audit | grep Environment
```

Check the service account has Editor access to the spreadsheet by logging into Google Drive and verifying sharing settings.

### 14.6 Tests fail with import errors

Make sure you are running pytest from the project root where `iitgpu/` is a subdirectory:

```bash
cd /path/to/IIT-Secure-SLURM-Job-Gateway
pytest tests/ -v
```

If you get `ModuleNotFoundError: No module named 'iitgpu'`, add the project root to `PYTHONPATH`:

```bash
PYTHONPATH=. pytest tests/ -v
```

### 14.7 sshd reload fails after install

The installer validates the config with `sshd -t` before reloading. If reload still fails:

```bash
sudo sshd -t   # show the specific error
```

Common causes:
- OpenSSH older than 8.2 does not support `sshd_config.d/` drop-ins. Check: `ssh -V`.
- A pre-existing conflicting `Match Group` block in `/etc/ssh/sshd_config`.

Fix: either upgrade OpenSSH or merge the directives into the main `sshd_config` manually.

### 14.8 sudoers validation fails

```bash
sudo visudo -cf /etc/sudoers.d/iit-gpu-gateway
```

If it fails, the most common cause is that `/usr/bin/sbatch` does not exist at that exact path on this system. Find the real path:

```bash
which sbatch
```

Edit `deploy/sudoers-gateway` to match the real path before re-running the installer.

---

*Version 1.0.0 — IIT HPC*
