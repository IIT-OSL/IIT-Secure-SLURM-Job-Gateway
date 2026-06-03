# M05 — IIT Secure SLURM Job Gateway: Mail, Storage Restructure & Access Control

**Date:** 2026-06-03
**Author:** Daham Dissanayake
**Scope:** Post-M04 session — Resend SMTP mail pipeline, branded HTML job notifications,
`/shared/users/` storage restructure, user provisioning bug fix, full ACL grant
for slurmadmin and daham across all `/shared` directories, and per-user file
access jail (browse + upload scoped to own folder; admins retain full access).
**Tests:** 450 passing (up from 439 at session start)
**Repo:** `https://github.com/DahamDissanayake/IIT-Secure-SLURM-Job-Gateway`
**Deployed at:** `/opt/iit-gpu/` on login node (192.168.122.10)

---

## Table of Contents

1. [Session Overview](#1-session-overview)
2. [System Status at Session Start](#2-system-status-at-session-start)
3. [Resend SMTP Mail Pipeline](#3-resend-smtp-mail-pipeline)
4. [Custom HTML Job Notification Mailer](#4-custom-html-job-notification-mailer)
5. [User Provisioning Bug Fix](#5-user-provisioning-bug-fix)
6. [Storage Restructure — /shared/users/](#6-storage-restructure--sharedusers)
7. [ACL Access — slurmadmin and daham](#7-acl-access--slurmadmin-and-daham)
8. [Test Fixes](#8-test-fixes)
9. [Commits This Session](#9-commits-this-session)
10. [Per-User File Access Jail](#10-per-user-file-access-jail)
11. [Active State](#11-active-state)

---

## 1. Session Overview

Session resumed after SSH disconnect. Prior session had completed all 8 feature phases
on `feature/spec-upgrades` and merged to `main`. This session covers operational
hardening: wiring up email notifications, fixing a user provisioning crash, and
cleaning up the shared NFS layout.

**Changes shipped:**

| Area | What changed |
|------|-------------|
| Mail | msmtp installed, `/etc/msmtprc` configured for Resend SMTP |
| Mail | `MailProg` in `slurm.conf` updated to `/usr/local/bin/iit-gpu-mailer` |
| Mail | Custom HTML mailer (`deploy/iit-gpu-mailer`) — 5 event types, dark-header design |
| Bug fix | `_run()` in `admin.py` — `ValueError: stdin and input arguments may not both be used` |
| Storage | `user_dir()` helper added to `config.py`; all user paths now under `/shared/users/` |
| Storage | `iit-gpu-adduser` updated to provision into `$NFS_ROOT/users/$USERNAME` |
| Storage | 7 user workspace dirs physically moved from `/shared/` to `/shared/users/` |
| Access | Recursive ACLs set on all `/shared/*` dirs for slurmadmin (UID 1000) and daham (UID 1002) |
| Access | slurmadmin added to `gpuusers` group on login node |
| Access | Per-user file jail: browse restricted to `users/<u>/` + models/envs; upload to `users/<u>/` only |
| Tests | `test_e2e.py` PYTHONPATH fix — was failing when pytest run without env set |
| Tests | Two tests updated to match new `_run()` and `user_dir()` behaviour |

---

## 2. System Status at Session Start

Checked immediately after SSH reconnect.

| Component | State |
|-----------|-------|
| `iit-gpu-audit.service` | active (running) |
| `iit-gpu-stats.service` | active on GPU host (192.168.122.1) |
| `slurmctld` | active |
| `/opt/iit-gpu` | on `main`, clean |
| Tests | 438 passed / 1 failed (`test_e2e::test_selftest_passes` — PYTHONPATH) |
| msmtp | not installed |
| `MailProg` | `/bin/mail` (default) |
| `/shared/users/` | did not exist |

---

## 3. Resend SMTP Mail Pipeline

### Installation

```bash
apt-get install -y msmtp msmtp-mta
```

`msmtp-mta` provides `/usr/sbin/sendmail` as a symlink so any program calling
`sendmail` also routes through msmtp.

### `/etc/msmtprc`  (mode 0600, never committed to git)

```
defaults
tls on
tls_starttls off
auth on
logfile /var/log/msmtp.log

account resend
host smtp.resend.com
port 465
from admin@gpu.indrajith.net
user resend
password re_<REDACTED>

account default : resend
```

### SLURM wiring

Added to `/etc/slurm/slurm.conf`:

```
MailProg=/usr/local/bin/iit-gpu-mailer
```

Then `scontrol reconfigure`. Verified:

```
MailProg = /usr/local/bin/iit-gpu-mailer
```

### Smoke test

```bash
echo "Subject: test" | msmtp --debug dahamdissanayake05@gmail.com
# → 250 OK  (Resend API confirmed delivery)
```

---

## 4. Custom HTML Job Notification Mailer

### Location

`/usr/local/bin/iit-gpu-mailer` (also tracked at `deploy/iit-gpu-mailer`)

### How it works

SLURM calls `MailProg` as:
```
iit-gpu-mailer -s "SLURM Job_id=N Name=X Began" recipient@email
```

The mailer:
1. Parses the SLURM subject to extract job ID, name, and event type
2. Queries `sacct` for live job details (node, CPUs, GPUs, runtime, exit code, paths)
3. Builds a branded HTML email
4. POSTs directly to the Resend HTTP API via `curl`
5. Falls back to `msmtp` plain-text if the API is unreachable

### Mail types

| Event | SLURM trigger | Header colour |
|-------|--------------|---------------|
| `STARTED` | `Began` | Blue `#3B82F6` |
| `COMPLETED` | `Ended … COMPLETED` | Green `#22C55E` |
| `FAILED` | `Failed … FAILED` | Red `#EF4444` |
| `TIMEOUT` | `Time limit reached` | Amber `#F59E0B` |
| `REQUEUED` | `Requeued` | Purple `#8B5CF6` |
| `OOM` | `OUT_OF_MEMORY` | Red `#EF4444` |

### Design

- Outer background: `#F4F4F5` (light grey — immune to email-client dark-mode override)
- 4 px accent bar at top: event colour (rendered as explicit `<td bgcolor>`)
- Dark header cell `#111827` with white headline, muted subtitle, coloured status pill
  — `<td bgcolor>` is always preserved by Gmail, Outlook, Apple Mail
- White content area `#FFFFFF` with dark text (`#111827`) — always readable
- Monospace field table: Job ID, Name, User, Partition, Node, CPUs, GPUs,
  Submitted, Started, Ended, Elapsed, Time Limit, Exit Code, Work Dir, stdout, stderr
- Contextual tip block (FAILED / TIMEOUT / OOM) with accent left-border
- Footer: `IIT GPU Cluster · <date>  |  iit-gpu-manager  |  By: IIT Research Team`

### Subject format

```
[IIT GPU] Job "cifar10_train" completed  [#1042]
[IIT GPU] Job "llama_finetune" failed  [#1043]
```

### Email client compatibility note

Full-dark emails (`background:#111`) get their backgrounds stripped by Gmail in
light mode, making light text on white unreadable. Fix applied: dark header is a
`<td bgcolor>` (always preserved) + white content `<td>` with dark text (always
readable regardless of client behaviour).

---

## 5. User Provisioning Bug Fix

### Symptom

```
✘  Unexpected error: stdin and input arguments may not both be used.
```

Occurred in Admin → Provision user after entering all fields and password.

### Root cause

`_run()` in `iitgpu/admin.py` called `subprocess.run()` with both
`stdin=subprocess.PIPE` **and** `input=stdin_data`. Python raises `ValueError`
when both are provided because `input=` internally sets `stdin=PIPE` itself.

```python
# Before (broken)
r = subprocess.run(
    cmd,
    capture_output=True, text=True, timeout=timeout,
    stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
    input=stdin_data,   # ← conflict when stdin_data is not None
)
```

### Fix

```python
# After
kw: dict = {"capture_output": True, "text": True, "timeout": timeout}
if stdin_data is not None:
    kw["input"] = stdin_data        # subprocess sets stdin=PIPE internally
else:
    kw["stdin"] = subprocess.DEVNULL
r = subprocess.run(cmd, **kw)
```

---

## 6. Storage Restructure — /shared/users/

### Motivation

All user workspace dirs (`daham/`, `tuser/`, `dahamtest/`, etc.) were at the
root of `/shared/` alongside system dirs (`jobs/`, `envs/`, `images/`, etc.),
making the layout messy and hard to administer.

### Code changes

**`iitgpu/config.py`** — new helper:
```python
def user_dir(cfg: Config, username: str) -> str:
    return str(Path(cfg.nfs_root) / "users" / username)
```

All modules updated to call `user_dir(cfg, user)` instead of
`Path(cfg.nfs_root) / user`:

| File | Change |
|------|--------|
| `iitgpu/wizard.py` | `scripts/`, `data/`, browse start paths |
| `iitgpu/files.py` | file manager start path |
| `iitgpu/setup.py` | smoke-test job dir, upload dest |
| `deploy/iit-gpu-adduser` | `mkdir -p $NFS_ROOT/users/$USERNAME` |

### Physical migration

Dirs moved from `/shared/` → `/shared/users/` on the GPU host (NFS server):

| Directory | Original owner | Mode |
|-----------|---------------|------|
| `anuktest` | dahamtest (2001) | 0700 |
| `daham` | daham (1002) | 0775 |
| `dahamtest` | dahamtest (2001) | 0700 |
| `damafinetune` | public (1003) | 0777 |
| `public` | public (1003) | 0777 |
| `testuser1` | dahamtest (2001) | 0700 |
| `tuser` | tuser (2000) | 0700 |

All original ownership and permissions preserved. Migration used
`sudo -n chown` (NOPASSWD) to temporarily transfer ownership for the
`rename()` syscall (AppArmor blocks cross-owner renames on ext4), then
restored original owner afterwards.

### `/shared/` after restructure

```
/shared/
├── data/               # shared datasets
├── envs/               # conda environments
├── images/             # Apptainer .sif containers
├── jobs/               # SLURM job output dirs
├── miniforge3/         # shared conda installation
├── models/             # shared model weights
├── scripts/            # shared utility scripts
├── templates/          # job script templates
├── tmp/                # scratch space
├── training-scripts/   # example training scripts
└── users/              # ← all user workspaces
    ├── anuktest/
    ├── daham/
    ├── dahamtest/
    ├── damafinetune/
    ├── public/
    ├── testuser1/
    └── tuser/
```

### Symlinks updated

`/home/tuser/shared` and `/home/dahamtest/shared` updated to point to
`/shared/users/<username>`.

---

## 7. ACL Access — slurmadmin and daham

### Goal

Both `slurmadmin` (login-node admin, UID 1000) and `daham` (UID 1002) need
unrestricted read/write access to every directory under `/shared/`, including
user workspaces that are mode 0700 owned by other users.

### slurmadmin group membership

slurmadmin was not in `gpuusers` — added:

```bash
usermod -aG gpuusers slurmadmin   # login node
```

slurmadmin does not exist on the GPU host (NFS access is by UID 1000).

### ACLs set

Recursive `rwX` ACLs applied to every directory under `/shared/` for both UIDs.
Default ACLs also set so new files/dirs created inside inherit the same grants.

```bash
# Applied to each dir (top-level + recursive for users/*)
setfacl -m u:1000:rwx,u:1002:rwx <dir>
setfacl -d -m u:1000:rwx,u:1002:rwx <dir>
```

Dirs covered:

```
/shared/data        /shared/envs        /shared/images
/shared/jobs        /shared/miniforge3  /shared/models
/shared/scripts     /shared/templates   /shared/tmp
/shared/training-scripts               /shared/users
/shared/users/*     (recursive)
```

Root-owned dirs (`images`, `miniforge3`) required the `sudo -n chown` trick
(same as migration) to obtain owner rights before `setfacl`, then ownership
was restored.

### Verification

```bash
getfacl /shared/images
# user:1000:rwx
# user:1002:rwx
# default:user:1000:rwx
# default:user:1002:rwx
```

---

## 8. Test Fixes

### test_e2e.py — PYTHONPATH in selftest subprocess

`test_selftest_passes` spawned `python3 -m iitgpu --selftest` without
`PYTHONPATH` set, so the subprocess couldn't find the `iitgpu` package.

```python
# Fix
repo_root = str(Path(__file__).parent.parent)
pythonpath = os.pathsep.join(filter(None, [repo_root, os.environ.get("PYTHONPATH", "")]))
result = subprocess.run(
    [sys.executable, "-m", "iitgpu", "--selftest"],
    env={**os.environ, "DEMO_MODE": "1", "PYTHONPATH": pythonpath},
    ...
)
```

### test_admin.py — test_run_uses_pipe_when_stdin_data_given

Test asserted `kwargs["stdin"] == PIPE`. After the `_run()` fix, `stdin` is
never passed when `input=` is used. Updated assertion:

```python
assert kwargs["input"] == "hello\n"
assert "stdin" not in kwargs
```

### test_wizard.py — test_generated_loader_script_is_valid_python

Test looked for generated script at `tmp_path / user / "scripts"`. After the
`user_dir()` change the path is `tmp_path / "users" / user / "scripts"`.

```python
# Before
scripts_dir = tmp_path / user / "scripts"
# After
scripts_dir = tmp_path / "users" / user / "scripts"
```

---

## 9. Commits This Session

| Hash | Message |
|------|---------|
| `60ddba7` | `fix(test): pass PYTHONPATH into selftest subprocess` |
| `ee58565` | `feat(mail): branded HTML job notification mailer via Resend API` |
| `bff48df` | `feat(mail): redesign emails — dark theme, accent divider line only, no icons` |
| `a79988a` | `fix(mail): dark header + white content to survive email client overrides` |
| `9b3c1c2` | `feat(mail): add IIT Research Team credit to footer` |
| `e21e6e9` | `fix(users): provision error + /shared/users/ restructure` |
| `114b299` | `feat(jail): per-user file scope — browse own dir + models/envs, upload to own dir only` |

All commits pushed to `main` on `github.com/DahamDissanayake/IIT-Secure-SLURM-Job-Gateway`.
`/opt/iit-gpu/` redeployed after each push via `bash deploy/redeploy-igm.sh`.

---

## 10. Per-User File Access Jail

### Motivation

Before this fix, `in_jail()` in `validate.py` allowed any path inside `/shared`
for all users. A regular user logged in as `tuser` could browse to `/shared/users/daham/`,
read other users' datasets, and delete files anywhere in `/shared`. The upload TUI
listed every root subdirectory of `/shared` as a valid upload target.

### Fix

Three files changed + 11 new tests.

#### `iitgpu/validate.py` — new helpers

```python
user_browse_roots(nfs_root, username) -> list[str]
    # [shared/users/<u>, shared/models, shared/envs]

user_upload_root(nfs_root, username) -> str
    # shared/users/<u>

in_user_browse_jail(path, nfs_root, username) -> bool
    # True for own dir + models + envs

in_user_upload_jail(path, nfs_root, username) -> bool
    # True for own dir only
```

#### `iitgpu/files.py` — `file_manager()` is now role-aware

| User type | Navigation (`_in_nav`) | Mutations (`_in_mut`) |
|-----------|----------------------|----------------------|
| Regular | `in_user_browse_jail` — own dir + models + envs | `in_user_upload_jail` — own dir only |
| Admin | `in_jail` — full NFS jail | `in_jail` — full NFS jail |

In read-only areas (models/envs for regular users):
- `[ + new folder ]` row is suppressed
- Directory action menu shows only `Open` / `Cancel` (no Rename/Delete)
- File action menu shows only `Show size` / `Cancel`
- Status line prints `Read-only area — browsing only`

#### `iitgpu/upload.py` — `run_upload()` scopes base dir by role

| User type | Base dir | Folder list |
|-----------|----------|------------|
| Regular | `shared/users/<username>/` | Sub-folders of own dir only; option to upload directly to root of own dir |
| Admin | `shared/` (entire NFS root) | All subdirectories (unchanged behaviour) |

#### Commit

```
114b299  feat(jail): per-user file scope — browse own dir + models/envs, upload to own dir only
```

---


## 11. Active State

### Services

| Service | Node | State |
|---------|------|-------|
| `slurmctld` | login (192.168.122.10) | active |
| `iit-gpu-audit` | login (192.168.122.10) | active |
| `iit-gpu-stats` | GPU host (192.168.122.1) | active |

### Tests

```
450 passed  (PYTHONPATH=. python3 -m pytest tests/ -q)
```

### Mail delivery

```
From:    admin@gpu.indrajith.net  (Resend SMTP relay)
Mailer:  /usr/local/bin/iit-gpu-mailer
Log:     /var/log/msmtp.log  (fallback only — primary path uses Resend API directly)
```

### /shared layout

```
System dirs at /shared root.  User workspaces under /shared/users/.
slurmadmin (UID 1000) and daham (UID 1002): rwx ACL on every dir.
Default ACLs set — new files inherit grants automatically.
```

### Groups

| User | Groups |
|------|--------|
| `slurmadmin` | slurmadmin, auditadmin, gpuadmins, **gpuusers** (added M05) |
| `daham` | daham, gpuusers, gpuadmins |
| `tuser` | tuser, gpuusers |
| `dahamtest` | dahamtest, gpuusers |
