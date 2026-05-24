# IIT-GPU-Manager

Secure terminal gateway for SLURM GPU job submission. Users connect via SSH
and interact with a forced-command TUI — they can never drop to a shell.

## Quick Start (Demo Mode)

```bash
pip install rich questionary
python -m iitgpu --demo
```

Run the selftest (no SLURM or daemon required):
```bash
python -m iitgpu --selftest
```

## Layout

```
iitgpu/
  __init__.py     Version (1.0.0)
  config.py       Env-var settings: NFS_ROOT, JOBS_SUBDIR, DEMO_MODE
  ui.py           Rich console helpers
  splash.py       ASCII art splash screen
  validate.py     Security validators + path jail (in_jail, safe_listdir, …)
  auditclient.py  Audit event emitter (socket → spool fallback)
  slurm.py        SLURM command wrappers with demo mode
  jobs.py         JobSpec dataclass + sbatch renderer
  wizard.py       Interactive job builder (questionary)
  monitor.py      Queue view, cancel, jailed log tail
  menu.py         Main menu loop
  __main__.py     Hardened launcher, signal traps, --selftest

deploy/
  audit_daemon.py         Runs as gpusync; SQLite WAL + JSONL + Google Sheets
  iit-gpu-audit.service   Systemd unit
  sshd-gateway.conf       sshd drop-in (ForceCommand + lockdowns)
  sudoers-gateway         SLURM-only sudoers for %gpuusers
  install.sh              Full system installer

tests/
  test_config.py          Config unit tests
  test_validate.py        Jail + validator tests
  test_auditclient.py     Socket + spool tests
  test_jobs.py            sbatch render tests
  test_e2e.py             End-to-end demo run
```

## Production Install

```bash
sudo bash deploy/install.sh
sudo usermod -aG gpuusers <username>
```

Requires: Python 3.11+, systemd, OpenSSH with `sshd_config.d/` support.

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `NFS_ROOT` | `/shared` | Base directory users may access |
| `JOBS_SUBDIR` | `jobs` | Subdirectory under NFS_ROOT for job folders |
| `DEMO_MODE` | `0` | Set to `1` to simulate SLURM |
| `MAX_GPUS` | `8` | Hard ceiling on GPU requests |
| `MAX_CPUS` | `64` | Hard ceiling on CPU requests |
| `MAX_MEM_GB` | `256` | Hard ceiling on memory requests |
| `MAX_HOURS` | `72` | Hard ceiling on job duration |
| `AUDIT_SOCKET` | `/run/iit-gpu/audit.sock` | Daemon socket path |
| `AUDIT_SPOOL` | `/run/iit-gpu/spool` | Fallback spool directory |
| `SHEET_ID` | — | Google Sheets spreadsheet ID (daemon) |

## Bypass-Test Checklist

These attacks must all fail:

| Attack | Defence |
|--------|---------|
| `ssh user@host bash` | `ForceCommand` always runs `iit-gpu-manager` |
| `ssh user@host -L 8080:…` | `AllowTcpForwarding no` |
| Ctrl-Z to background | `SIGTSTP` is ignored |
| Symlink to `/etc/shadow` in job folder | `in_jail()` resolves via `realpath()` |
| `../../etc` in file browser | `in_jail()` rejects `..` traversal |
| Edit the audit log | DB/JSONL owned by `gpusync`; users only send to socket |
| Submit without audit log | `log_or_block()` must return `True`; refused otherwise |
| Request 100 GPUs | `clamp_int(100, 1, MAX_GPUS, 1)` → 8 |
| `PermitUserEnvironment` env injection | Set to `no` in sshd drop-in |
| X11/agent/TCP forwarding tunnel | All forwarding disabled in sshd drop-in |
| Crash to shell prompt | `try/except` in launcher; `finally` logs `session_end`; exits non-zero |
