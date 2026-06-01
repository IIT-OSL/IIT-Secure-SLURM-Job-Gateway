# M04 — IIT Secure SLURM Job Gateway: Complete Architecture & Audit Reference

**Date:** 2026-06-01
**Author:** Daham Dissanayake
**Scope:** Definitive end-state reference — users, groups, permissions, SSH gateway,
SLURM stack, NFS, the full TUI pipeline, audit system, deployment, and all issues
discovered across M01–M03. Supersedes M01–M03 as the single source of architectural
truth. **334 tests passing.**
**Repo:** `https://github.com/DahamDissanayake/IIT-Secure-SLURM-Job-Gateway`
**Deployed at:** `/opt/iit-gpu/` on login node (192.168.122.10)

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Full Cluster Topology](#2-full-cluster-topology)
3. [Linux Users & Groups](#3-linux-users--groups)
4. [SSH Gateway Mechanism](#4-ssh-gateway-mechanism)
5. [Sudo & Permission Architecture](#5-sudo--permission-architecture)
6. [SLURM Configuration](#6-slurm-configuration)
7. [Accounting Stack — slurmdbd + MariaDB + sacct](#7-accounting-stack--slurmdbd--mariadb--sacct)
8. [NFS & Shared Storage](#8-nfs--shared-storage)
9. [TUI Architecture — Module Map](#9-tui-architecture--module-map)
10. [Complete User Journey — End-to-End Pipeline](#10-complete-user-journey--end-to-end-pipeline)
11. [Job Submission Flow](#11-job-submission-flow)
12. [Audit System — Complete Reference](#12-audit-system--complete-reference)
13. [GPU Stats Pipeline](#13-gpu-stats-pipeline)
14. [Services Inventory](#14-services-inventory)
15. [Security Model](#15-security-model)
16. [Deployment Pipeline](#16-deployment-pipeline)
17. [User Onboarding & Offboarding](#17-user-onboarding--offboarding)
18. [Prebuilt Environments & Containers](#18-prebuilt-environments--containers)
19. [Test Suite](#19-test-suite)
20. [All Issues & Fixes (M01–M03 Consolidated)](#20-all-issues--fixes-m01m03-consolidated)
21. [Active State & Pending Items](#21-active-state--pending-items)
22. [Quick Reference](#22-quick-reference)

---

## 1. Executive Summary

The IIT Secure SLURM Job Gateway is a **Python TUI** (terminal user interface) that
acts as a controlled frontend to a single-node SLURM GPU cluster. Users SSH in,
land directly in the tool (they cannot reach a shell), and use it to submit ML
training jobs, monitor their runs, manage files, and view accounting.

**What makes it "secure":**
- SSH `ForceCommand` — every user in `gpuusers` gets the TUI, not a shell
- Path jail — all file operations are constrained to `/shared`
- Per-user identity — jobs run as the authenticated user, not a shared account
- Audit trail — every significant action logged to SQLite + JSONL via a dedicated daemon
- Sudoers scoping — normal users need zero sudo; admins get only what they need

**Evolution across M01–M03:**
| Log | Focus |
|-----|-------|
| M01 | Initial deploy: stats daemon, GPU dashboard, WideResNet training script, redeploy scripts |
| M02 | Full audit: SLURM config, NFS, accounting, QOS, service inventory, security model |
| M03 | Frontend rebuild: per-user identity, job arrays/deps, interactive sessions, file manager, accounting UI, admin panel, notifications; 9 post-deploy bug fixes |
| **M04** | **This file** — definitive consolidated reference, all layers, all visuals |

---

## 2. Full Cluster Topology

```
  ┌─────────────────────────────────────────────────────────────────────────┐
  │                        USER'S LAPTOP / WORKSTATION                       │
  │                                                                           │
  │   ssh alice@10.35.4.100 -p 2225                                          │
  │          │                                                                │
  └──────────┼────────────────────────────────────────────────────────────── ┘
             │ TCP port 2225 (external)
             ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │            LOGIN NODE  —  KVM guest  —  192.168.122.10                  │
  │                                                                           │
  │   sshd  ─── Match Group gpuusers ──► ForceCommand iit-gpu-manager       │
  │                                              │                            │
  │   Python TUI  /opt/iit-gpu/iitgpu/           │                            │
  │   ├── menu.py       (main menu)              │                            │
  │   ├── wizard.py     (job submission)         │                            │
  │   ├── dashboard.py  (live monitoring)        │                            │
  │   ├── monitor.py    (queue / logs)           │                            │
  │   ├── files.py      (file manager)           │                            │
  │   ├── accounting.py (GPU/CPU-hour reports)   │                            │
  │   ├── admin.py      (admin panel)            │                            │
  │   └── ...                                    │                            │
  │                                              │                            │
  │   slurmctld  ◄─────────────────────────────►│                            │
  │   slurmdbd   ─── MariaDB (localhost)         │                            │
  │   iit-gpu-audit.service  (gpusync user)      │                            │
  │     └── /var/lib/iit-gpu/audit.db            │                            │
  │     └── /var/lib/iit-gpu/audit.jsonl         │                            │
  │                                              │                            │
  │   /shared  (NFS mount ← GPU host)           ◄┘                            │
  │     ├── jobs/      (per-user job output dirs)                              │
  │     ├── envs/      (shared conda environments)                             │
  │     ├── images/    (Apptainer .sif containers)                             │
  │     ├── data/      (datasets)                                              │
  │     ├── models/    (downloaded HF models)                                  │
  │     └── .gpu_stats.json  (live hardware metrics)                           │
  │                                                                           │
  └──────────────────────────────────────────────────────────────────────────┘
             │  SLURM TCP 6817/6818 + NFS
             ▼
  ┌──────────────────────────────────────────────────────────────────────────┐
  │            GPU HOST  —  bare metal KVM hypervisor  —  192.168.122.1     │
  │            hostname: iit-MS-7E06                                          │
  │                                                                           │
  │   slurmd   (worker daemon, receives jobs from slurmctld)                  │
  │   nvidia-smi  →  RTX 5090 (32 GB GDDR7, Blackwell sm_120)                │
  │                                                                           │
  │   iit-gpu-stats.service   ─► /shared/.gpu_stats.json  (every 2 s)       │
  │                                                                           │
  │   /mnt/nvme_storage/shared  ◄── NFS server root  (1.7 TB NVMe)           │
  │     symlinked as /shared                                                   │
  │                                                                           │
  │   Per-user /shared/<user>  dirs (0700) — owned by matching UID            │
  │                                                                           │
  └──────────────────────────────────────────────────────────────────────────┘

  Network: 192.168.122.0/24 (KVM virbr0 bridge)
  External access: 10.35.4.100:2225 → login-node:22 (port-forward or NAT)
```

---

## 3. Linux Users & Groups

### 3.1 Users — both nodes (UIDs must match on login node AND GPU host)

| User | UID | Primary GID | Shell | Role |
|------|-----|-------------|-------|------|
| `slurmadmin` | 1000 | 1000 | `/bin/bash` | Cluster admin; owns `/opt/iit-gpu`; has `gpuadmins` membership; runs redeploy scripts |
| `daham` | 1002 | 1002 | `/bin/bash` | Legacy shared account; `gpuadmins` member; `gpuusers` member; SLURM association in `default` account |
| `gpusync` | 997 | 984 | `/usr/sbin/nologin` | Service account — runs `iit-gpu-audit.service` only; member of `gpuusers` (required so it can read `/opt/iit-gpu` at 0750) |
| `public` | 1003 | 1003 | `/bin/bash` | Demo/shared login account; `gpuusers` member; SLURM association; jobs run **as `public`** (per-user mode active) |
| `tuser` | 2000 | 2000 | `/bin/bash` | First real per-user test account; provisioned with `iit-gpu-adduser`; verified full lifecycle |
| _future users_ | UID ≥ 2001 | _same as UID_ | `/bin/bash` | Provisioned by admin via `addUser.sh` / `iit-gpu-adduser` |

### 3.2 Groups

| Group | GID | Members | Purpose |
|-------|-----|---------|---------|
| `gpuusers` | 1500 | `daham`, `public`, `slurm`, `tuser`, `gpusync` | **Gateway access group** — SSH `Match Group gpuusers` triggers `ForceCommand iit-gpu-manager`. Being added to this group is the single action that grants TUI access. Job output dirs are group-owned by this GID (0770). |
| `gpuadmins` | 1501 | `daham`, `slurmadmin` | Admin group — unlocks the Admin panel in the TUI. Scoped sudoers grant `scontrol update`, `iit-gpu-adduser`, `iit-gpu-deluser` as root. |
| `slurm` | 64030 | `daham`, `public` | SLURM daemon group (system) |

### 3.3 Why UIDs must match on both nodes

NFS uses `sec=sys` (numeric UID/GID in wire headers). SLURM's `slurmstepd` starts
the job process with the submitter's numeric UID on the GPU host. If the UID doesn't
exist on the GPU host — or maps to a different user — file ownership breaks and the
job step may fail or write output to the wrong location. The onboarding script
(`iit-gpu-adduser.sh`) picks the **highest free UID on both nodes** and creates
accounts with that exact UID on each, guaranteeing identity symmetry.

### 3.4 Visual — group membership map

```
  gpuusers (GID 1500) ─── SSH ForceCommand → TUI for all members
  ├── daham     (1002)  also in: slurm, gpuadmins
  ├── public    (1003)  also in: slurm
  ├── slurm     (sys)
  ├── tuser     (2000)
  ├── gpusync   (997)   nologin, needed so audit daemon can read /opt/iit-gpu
  └── <future>  (≥2001)

  gpuadmins (GID 1501) ─── Admin panel access + elevated sudoers
  ├── daham     (1002)
  └── slurmadmin (1000)
```

---

## 4. SSH Gateway Mechanism

### 4.1 How it works

When any member of `gpuusers` SSH's into the login node, sshd intercepts their
session with `ForceCommand` before any shell is spawned:

```
/etc/ssh/sshd_config.d/iit-gpu-gateway.conf
────────────────────────────────────────────
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
```

This means:
- The user **cannot** reach a shell — the TUI is the only thing they can run
- Port forwarding is fully blocked (no tunnelling out)
- X11 forwarding is disabled
- The user's `.bashrc`, `.profile`, and `.ssh/rc` are all skipped (`PermitUserRC no`)

### 4.2 The launcher script

`/usr/local/bin/iit-gpu-manager` (executed by `ForceCommand`):

```bash
exec env -i \
    HOME="$HOME" USER="$USER" LOGNAME="$LOGNAME" \
    PATH="/shared/miniforge3/bin:/usr/local/bin:/usr/bin:/bin" \
    SSH_CLIENT="${SSH_CLIENT:-}" TERM="${TERM:-xterm}" \
    PYTHONPATH="/opt/iit-gpu" \
    CONDA_PREFIX_SHARED="/shared/miniforge3" \
    NFS_ROOT="/shared" \
    IIT_SITE_ENV="/opt/iit-gpu/deploy/site.env" \
    /usr/bin/python3 -m iitgpu
```

Key design decisions:
- `env -i` — clean environment. User's shell env vars (including potentially
  malicious `PYTHONPATH`, `LD_PRELOAD`, etc.) are stripped before the tool runs.
- `PYTHONPATH=/opt/iit-gpu` — the single canonical code location is injected.
- `IIT_SITE_ENV` — points to the deployed `site.env` with cluster-specific config.
- The tool runs as the authenticated user (e.g. `public`, `tuser`, `daham`).

### 4.3 Flow diagram

```
  User: ssh alice@10.35.4.100 -p 2225
        │
        ▼
  sshd authenticates (password / pubkey)
        │
        ▼
  sshd checks: is alice in group gpuusers?
        │  YES
        ▼
  ForceCommand: exec /usr/local/bin/iit-gpu-manager
  (user's shell is NEVER spawned)
        │
        ▼
  env -i → python3 -m iitgpu
        │
        ▼
  __main__.py:
    1. Install signal handlers (SIGINT → audit + exit, SIGTSTP → ignore)
    2. Show splash screen
    3. Load site.env via config.py
    4. Log session_start to audit daemon
    5. Call menu.run_menu()
        │
        ▼
  Main TUI loop (user interacts until Quit)
        │
        ▼
  On exit: log session_end → audit daemon → clean exit
```

---

## 5. Sudo & Permission Architecture

### 5.1 The two-mode model

The cluster operates in **per-user identity mode** (`GATEWAY_SHARED_USER=0` in
`/opt/iit-gpu/deploy/site.env`). In this mode, jobs run as the authenticated user —
no sudo is needed for SLURM operations. The legacy shared-user mode
(`GATEWAY_SHARED_USER=1`, still documented in the repo's `deploy/site.env` template
as a rollback option) ran all jobs as `daham` via `sudo -u daham sbatch`.

### 5.2 Current sudoers configuration

```
/etc/sudoers.d/iit-gpu-gateway  (admin-only, post-cutover)
───────────────────────────────────────────────────────────
Defaults:%gpuadmins !lecture, timestamp_timeout=0

# Elevated SLURM ops (drain/resume/reconfigure) and user provisioning:
%gpuadmins ALL=(root) NOPASSWD:
    /usr/bin/scontrol update *,
    /usr/bin/scontrol reconfigure,
    /usr/local/bin/iit-gpu-adduser,
    /usr/local/bin/iit-gpu-deluser
```

```
/etc/sudoers.d/iit-gpu-provisioning  (GPU HOST only)
────────────────────────────────────────────────────
# Allows the login node's root SSH key to provision accounts on the GPU host
# without an interactive password. Scoped to account management only.
root-daham ALL=(root) NOPASSWD:
    /usr/sbin/useradd, /usr/sbin/userdel,
    /usr/sbin/groupadd, /usr/sbin/groupdel, /usr/sbin/usermod,
    /bin/mkdir, /bin/chown, /bin/chmod
```

### 5.3 Permission levels — complete visual

```
  ┌────────────────────────────────────────────────────────────────────┐
  │                    PERMISSION LEVELS                                │
  │                                                                     │
  │  Level 0 — Normal user (gpuusers member, not gpuadmins)            │
  │  ─────────────────────────────────────────────────────             │
  │  • Runs the TUI (ForceCommand)                                      │
  │  • Submits jobs as themselves (sbatch, squeue, scancel own jobs)    │
  │  • Reads /shared/<own_user>/ (0700 private)                        │
  │  • Reads shared envs/images/models (shared registry is 0666)       │
  │  • CANNOT: reach a shell, forward ports, view other users' jobs     │
  │                                                                     │
  │  Level 1 — Admin user (gpuadmins member)                           │
  │  ───────────────────────────────────────                           │
  │  Everything in Level 0, plus:                                       │
  │  • Admin panel in TUI: drain/resume nodes, provision/offboard users │
  │  • View all users' jobs and audit logs                              │
  │  • sudo scontrol update / scontrol reconfigure (node state)         │
  │  • sudo iit-gpu-adduser / iit-gpu-deluser (user lifecycle)          │
  │                                                                     │
  │  Level 2 — slurmadmin (cluster owner, direct shell access)         │
  │  ────────────────────────────────────────────────────────          │
  │  Full system access; owns /opt/iit-gpu; runs redeploy scripts;      │
  │  manages systemd services; edits slurm.conf.                        │
  │                                                                     │
  └────────────────────────────────────────────────────────────────────┘
```

### 5.4 What normal users CAN and CANNOT do

| Action | Normal User | Admin |
|--------|-------------|-------|
| Submit GPU job | ✅ (as themselves) | ✅ |
| Cancel own job | ✅ | ✅ |
| Cancel other's job | ❌ | ✅ |
| View own queue | ✅ | ✅ |
| View all jobs (sacct -a) | ❌ | ✅ |
| Hold / release own job | ✅ | ✅ |
| Run job arrays / deps | ✅ | ✅ |
| Interactive srun session | ✅ | ✅ |
| Upload files to /shared/<own> | ✅ | ✅ |
| Browse /shared/<other> | ❌ (jailed) | ❌ (jailed) |
| Drain/resume a node | ❌ | ✅ |
| Provision new user | ❌ | ✅ |
| Offboard a user | ❌ | ✅ |
| View audit log | ❌ | ✅ |
| Reach a bash shell | ❌ | ❌ (use slurmadmin account) |

---

## 6. SLURM Configuration

### 6.1 slurm.conf (applied values — must be byte-identical on both nodes)

```ini
ClusterName=iit
SlurmctldHost=login-node

# Scheduler
SchedulerType=sched/backfill
SelectType=select/cons_tres
SelectTypeParameters=CR_Core_Memory

# Accounting
AccountingStorageType=accounting_storage/slurmdbd
AccountingStorageHost=localhost
AccountingStorageTRES=gres/gpu
JobAcctGatherType=jobacct_gather/cgroup
JobAcctGatherFrequency=30

# Priority (fairshare)
PriorityType=priority/multifactor
PriorityWeightFairshare=100000
PriorityWeightAge=1000
PriorityWeightQOS=10000

# Hardware (i9-14900K: 32 logical CPUs — hwloc miscounts as 16 on hybrid arch)
SlurmdParameters=config_overrides
NodeName=iit-MS-7E06 NodeAddr=192.168.122.1 CPUs=32 RealMemory=62000 Gres=gpu:1 State=UNKNOWN

# Partition
PartitionName=gpu Nodes=iit-MS-7E06 Default=yes MaxTime=1-00:00:00 State=UP

# Logging
SlurmdDebug=info
SlurmdLogFile=/var/log/slurm/slurmd.log
```

### 6.2 cgroup.conf

```ini
CgroupPlugin=autodetect
ConstrainCores=yes          # per-job CPU isolation
ConstrainRAMSpace=yes       # per-job RAM isolation (prevents memory overrun)
ConstrainSwapSpace=no
ConstrainDevices=no         # KEPT no — enabling needs per-job NVIDIA eBPF allowlist
                             # (risk: GPU disappears inside job). See M01 §8 quirks.
```

### 6.3 QOS policy

| QOS | MaxTRESPerUser | MaxWall | Purpose |
|-----|----------------|---------|---------|
| `normal` | `gres/gpu=1` (1 GPU) | 8 hours | Default for all users |
| `long` | _(none)_ | 7 days | Extended runs (admin-assigned) |

### 6.4 SLURM accounts

```
Account: root (parent)
  └── Account: default
        ├── User: daham   → QOS: normal
        ├── User: public  → QOS: normal
        └── User: tuser   → QOS: normal
```

### 6.5 Known SLURM quirks on this build

| Quirk | Impact | Workaround |
|-------|--------|------------|
| `AllocTRES` omits GPU in scontrol output | `gpu_alloc` always reads 0 from scontrol | Count via `squeue --states=RUNNING --format=%b` (lines containing "gpu") |
| `GresUsed` absent from scontrol node output | Cannot read GPU utilization from SLURM | Read from `/shared/.gpu_stats.json` (nvidia-smi daemon) |
| `sacct` disabled in compute node context | `recent_jobs()` can't use sacct history | Scan `slurm-*.out` files in job dirs; compute elapsed from file stat times |
| `slurm.conf` mismatch warning on restart | Cosmetic — does not affect jobs | Ensure conf is byte-identical on both nodes after any change |
| Interactive `srun` leaves dirty cgroup | RaisedSignal:53 on next job after srun | `sudo systemctl restart slurmd` on GPU host |
| No C compiler (gcc/cc/clang) | `torch.compile` unavailable | Use eager mode; CUDA wheels must come from PyPI |

---

## 7. Accounting Stack — slurmdbd + MariaDB + sacct

### 7.1 Stack layers

```
  sacct / sreport / sshare (CLI)
          │
          ▼
  slurmdbd (accounting daemon — login node)
          │
          ▼
  MariaDB 11.8.6 (slurm_acct_db database — login node, localhost)
```

### 7.2 Data written

Every SLURM job writes: JobID, User, Account, QOS, State, AllocCPUS, AllocTRES
(includes `gres/gpu=N`), Elapsed, Start, End, ExitCode, NodeList.

The TUI's **Usage & Accounting** area (`accounting.py`) surfaces:
- GPU-hours and CPU-hours per user (last N days)
- Fairshare scores (`sshare`)
- Cluster utilization report (`sreport`)

### 7.3 sacct_history() — used by the TUI

`slurm.sacct_history(limit=200)` calls:
```
sacct --noheader --parsable2 -X -u <user>
      --format=JobID,JobName,User,State,Elapsed,Start,End,AllocCPUS,AllocTRES
      -S now-30days
```
Returns `QueueEntry` objects used by the dashboard's "recent jobs" section.

### 7.4 SACCT_ENABLED flag

`config.py` reads `SACCT_ENABLED` from `site.env`:
- `auto` (default) — probe sacct; if it returns data, enable it; else fall back to file scanning
- `true` — force enable
- `false` — force disable (file scanning only)

---

## 8. NFS & Shared Storage

### 8.1 Export configuration (GPU host — the NFS server)

```
/etc/exports:
/mnt/nvme_storage/shared  192.168.122.0/24(rw,sync,no_subtree_check,root_squash)
```

```
GPU host:  /mnt/nvme_storage/shared  (real path, 1.7 TB NVMe)
              ↑ symlinked as /shared on both nodes
Login node: /shared  (NFS mount)
GPU host:   /shared  (symlink to /mnt/nvme_storage/shared — local access)
```

### 8.2 root_squash implications

`root_squash` maps root from client (login node) to `nobody:nogroup`. This means:
- Admin `chown`/`chmod` of files on `/shared` **must run on the GPU host** (the NFS
  server, where root is real), not from the login node over NFS.
- The onboarding script creates `/shared/<user>` on the GPU host over SSH.
- The offboarding script deletes `/shared/<user>` on the GPU host over SSH.
- ACLs report "Operation not supported" on this NFS export.
- setgid directory inheritance does not propagate over this NFS mount.

**App-level workaround for shared state files:** `config.make_shared_writable(path)`
sets shared registry files (env registry, model registry, templates) to 0666/0777
after creation so any group member can update them. This is safe because `/shared` is
only accessible to `gpuusers` (ForceCommand + group auth).

### 8.3 Directory layout

```
/shared/
├── .gpu_stats.json        ← live hardware metrics (nvidia-smi, 2s cadence)
├── jobs/
│   ├── public/
│   │   └── finetune_20260601_045303/
│   │       ├── job.sbatch        ← generated SLURM script
│   │       ├── slurm-118.out     ← job stdout
│   │       └── slurm-118.err     ← job stderr
│   └── tuser/
│       └── ...
├── envs/
│   ├── .registry.json     ← shared env registry (0666)
│   ├── pytorch-cuda/      ← conda env tree
│   └── llm-finetune/      ← conda env tree (torch+transformers+trl+peft+bitsandbytes)
├── images/
│   └── *.sif              ← Apptainer container images
├── data/
│   ├── cifar10/           ← CIFAR-10 dataset cache
│   └── <user>/            ← per-user uploaded datasets
├── models/
│   └── .registry.json     ← shared model registry (0666)
├── templates/
│   └── .registry.json     ← saved job templates (0666)
├── training-scripts/
│   └── finetune_qlora.py  ← QLoRA finetune script (not in repo — runtime only)
├── miniforge3/            ← shared conda base install
│   └── bin/conda
├── tmp/                   ← build temp dir (used by pip/conda to avoid /tmp overflow)
├── public/                ← public user's workspace (0700)
├── tuser/                 ← tuser's workspace (0700)
└── <user>/                ← per-user workspace (0700, owned by matching UID)
```

---

## 9. TUI Architecture — Module Map

### 9.1 Package structure

```
/opt/iit-gpu/
└── iitgpu/
    ├── __init__.py        version = "1.0.0"
    ├── __main__.py        entry point: signal handlers, splash, site.env load, menu
    ├── config.py          Config dataclass; reads site.env + env vars; is_admin()
    ├── menu.py            main menu + monitor menu dispatch
    ├── wizard.py          4-step job submission wizard (type/env/script/args)
    ├── jobs.py            JobSpec + sbatch renderer + make_job_folder()
    ├── slurm.py           SLURM interface: NodeStats, queue, sacct, submit, cancel
    ├── dashboard.py       live Rich TUI dashboard (job queue + log tail + hardware)
    ├── monitor.py         static queue/log views, hold/release/requeue, seff
    ├── accounting.py      GPU/CPU-hours per user, fairshare, sreport
    ├── admin.py           admin panel: node drain/resume, user provision/offboard, audit
    ├── files.py           jailed file manager: browse/mkdir/rename/delete/copy/du
    ├── upload.py          dataset upload (local copy + HTTP download)
    ├── envbuilder.py      conda env creator (framework selection → conda create)
    ├── envs.py            env manager: list/delete/inspect conda envs + containers
    ├── containers.py      Apptainer container listing and management
    ├── notebooks.py       running interactive services (Jupyter/TensorBoard) + teardown
    ├── notify.py          completion notifications: email (MTA) or in-TUI poller
    ├── setup.py           arrow-key setup menu: health check, env install, model dl
    ├── shell.py           restricted SLURM shell (allowlisted commands only)
    ├── auditclient.py     audit sender: Unix socket → daemon, spool fallback
    ├── validate.py        path jail, input sanitizers, clamp_int, clean_*
    ├── splash.py          ASCII art splash screen
    ├── templates.py       job template save/load/list
    ├── models.py          HF model download (huggingface_hub, Xet disabled)
    └── ui.py              Rich console helpers: header, info, warn, err, kv, ok
```

### 9.2 Module dependency graph

```
  __main__
     │
     ├── config  ←──────────────────────────── (all modules read config)
     ├── auditclient  ←──────────────────────── (all significant actions log here)
     ├── validate  ←─────────────────────────── (all file paths pass through)
     │
     ├── menu
     │    ├── wizard ── jobs ── slurm
     │    │               └── validate
     │    ├── setup ── envbuilder ── config
     │    │         └── models
     │    ├── dashboard ── slurm
     │    │            └── (log tail)
     │    ├── monitor ── slurm
     │    ├── accounting ── (sacct/sreport subprocess)
     │    ├── admin ── auditclient
     │    │         └── (iit-gpu-adduser subprocess)
     │    ├── files ── validate
     │    ├── upload ── validate
     │    ├── notebooks ── slurm
     │    └── shell ── validate ── auditclient
```

### 9.3 Module reference — key behaviors

#### `config.py`
Reads `IIT_SITE_ENV` path → merges into env → frozen `Config` dataclass.

| Field | Env var | Default | Description |
|-------|---------|---------|-------------|
| `nfs_root` | `NFS_ROOT` | `/shared` | Root of NFS share |
| `jobs_subdir` | `JOBS_SUBDIR` | `jobs` | Sub-dir under nfs_root |
| `conda_prefix` | `CONDA_PREFIX_SHARED` | `/shared/miniforge3` | Miniforge install |
| `demo_mode` | `DEMO_MODE` | `False` | Mocks all SLURM calls |
| `sacct_enabled` | `SACCT_ENABLED` | `auto` | sacct usage policy |
| `gateway_shared_user` | `GATEWAY_SHARED_USER` | `0` | 0=per-user, 1=legacy |
| `gateway_host` | `GATEWAY_HOST` | — | External hostname for tunnel hints |
| `gateway_port` | `GATEWAY_PORT` | `2225` | External SSH port |
| `slurm_partition` | `SLURM_PARTITION` | `gpu` | Default partition |
| `slurm_qos` | `SLURM_QOS` | `normal` | Default QOS |
| `gpuusers_group` | `GPUUSERS_GROUP` | `gpuusers` | Gateway group name |
| `admin_group` | `ADMIN_GROUP` | `gpuadmins` | Admin group name |

`is_admin(cfg)` — checks `os.getgroups()` for the admin GID.

#### `validate.py`
All file operations pass through `in_jail(path)`:
1. `Path(path).resolve()` — follow all symlinks
2. Check resolved path starts with `NFS_ROOT`
3. Reject: `..` traversal, absolute paths outside jail, symlink escapes

Other validators:
- `clean_run_command(s)` — strip newlines/control chars, truncate at 1000
- `clean_job_name(s)` — strip non-`[a-zA-Z0-9_-]`, truncate at 64
- `clean_time_limit(s)` — validate `H:M:S` format, clamp to `MAX_HOURS` (72h default)
- `clamp_int(v, lo, hi, default)` — bounds-check integers (e.g. GPU count ≤ `MAX_GPUS`)
- `safe_listdir(path)` — jailed `os.listdir`

#### `slurm.py` — SLURM interface layer

`_gateway_prefix()` / `_effective_user()`:
- `GATEWAY_SHARED_USER=0` (current): returns `[]` — SLURM commands run as the logged-in user directly
- `GATEWAY_SHARED_USER=1` (legacy): returns `["sudo", "-u", "daham"]` — commands run as the shared account

`get_node_stats()` — two-source merge:
1. `scontrol show node <node> --oneliner` (no sudo needed — read-only)
2. `/shared/.gpu_stats.json` (written by stats daemon every 2s)
3. Fallback: `_read_hw_stats_direct()` — runs `nvidia-smi` + reads `/proc` directly

`_count_running_gpu_jobs()` — `squeue --states=RUNNING --format=%b | grep gpu | wc -l`
(because `AllocTRES` omits GPU on this SLURM build)

`queue()` — `squeue --noheader --format=...` as the effective user
`cancel(job_id)` — `scancel <id>` as the effective user
`hold(job_id)` / `release(job_id)` / `requeue(job_id)` — `scontrol suspend/resume/requeue`
`submit(script_path)` — `sbatch <path>` as the effective user

#### `jobs.py` — Job spec and sbatch renderer

`make_job_folder(user, job_name, cfg)`:
- Creates `/shared/jobs/<user>/<job_name>_<timestamp>/`
- Permissions: **0o770**, group `gpuusers` — so `slurmstepd` (running as the user)
  can write output files, and the user can read them

`render_sbatch(spec, job_folder)` generates:
```bash
#!/bin/bash
#SBATCH --job-name=<job_name>_<timestamp>    ← full name, unique per run (fixed in M03)
#SBATCH --partition=<partition>
#SBATCH --gres=gpu:<gpus>
#SBATCH --cpus-per-task=<cpus>
#SBATCH --mem=<mem>
#SBATCH --time=<time_limit>
#SBATCH --output=<job_folder>/slurm-%j.out
#SBATCH --error=<job_folder>/slurm-%j.err
#SBATCH --chdir=<job_folder>

source /shared/miniforge3/etc/profile.d/conda.sh
conda activate <env_path>

export MODEL_PATH=<model_path>
export HF_HOME=/shared/models

<run_command>
```

Resource defaults per task type:

| Task | GPUs | CPUs | RAM | Time |
|------|------|------|-----|------|
| train | 1 | 16 | 60 GB | unlimited |
| finetune | 1 | 16 | 60 GB | unlimited |
| inference | 1 | 8 | 32 GB | 4h |
| test | 1 | 4 | 16 GB | 30 min |

#### `dashboard.py` — Live Rich TUI dashboard

```
┌─ Cluster: iit ──────────────────────────────────────────────────────┐
│  iit-MS-7E06  ALLOCATED  │  GPU 87% 28.4/32GB 78°C 595W  CPU 12% │
│               load: 8.31 / 7.90  │  RAM 24.1/62.0 GB               │
└─────────────────────────────────────────────────────────────────────┘
┌─ Job Queue ──────────────────────────────────────────────────────────┐
│  ID   User     Name                 State       Elapsed    Part      │
│  118  public   finetune_20260601..  ⠏ RUNNING   2:22:01   gpu       │
│  ─── recent ──────────────────────────────────────────────────────  │
│  117  public   finetune_20260601..  FAILED      14:32     gpu       │
│  116  tuser    train_20260531...    COMPLETED    4:52      gpu       │
└─────────────────────────────────────────────────────────────────────┘
┌─ Output: /shared/jobs/public/finetune_.../slurm-118.out ─────────────┐
│  Loading model weights... (shard 291/291)                            │
│  Epoch 1/3 | step 100/512 (19%) | loss 1.24 | lr 1.2e-4            │
│  ...                                                                 │
└─────────────────────────────────────────────────────────────────────┘
  Q=quit  S=switch job  C=cancel selected  R=refresh
```

Refresh rates: data every 2.0s (squeue + node stats + log tail), display at 4 FPS
(braille spinner ticks to prove liveness without inventing progress data).

---

## 10. Complete User Journey — End-to-End Pipeline

```
  USER LAPTOP
      │
      │  ssh alice@10.35.4.100 -p 2225
      ▼
  ════════════════════════════════════════════
  LOGIN NODE  (192.168.122.10)
  ════════════════════════════════════════════
      │
      │  [sshd] Match Group gpuusers → ForceCommand
      ▼
  /usr/local/bin/iit-gpu-manager
      │  (env -i; sets PYTHONPATH, NFS_ROOT, IIT_SITE_ENV)
      ▼
  python3 -m iitgpu
      │
      ├── config.py    reads /opt/iit-gpu/deploy/site.env
      ├── auditclient  → session_start logged to /run/iit-gpu/audit.sock
      └── menu.run_menu()
              │
              │  User chooses: "3. Run a job"
              ▼
          wizard.run_wizard()
              │
              ├─ 1. Template? (load saved template or start fresh)
              ├─ 2. Task type (train / finetune / inference / test)
              ├─ 3. Environment (select from /shared/envs/* or skip)
              ├─ 4. Script (jailed browser of /shared/alice/)
              ├─ 5. Config (model selection if train_cifar10.py)
              ├─ 6. Extra args (sanitized by clean_run_command)
              └─ 7. Preview → Submit
                      │
                      ▼
              jobs.make_job_folder()
                  └─ /shared/jobs/alice/train_20260601_120000/  (0770 gpuusers)
                      │
                      ▼
              jobs.render_sbatch()
                  └─ /shared/jobs/alice/train_20260601_120000/job.sbatch
                      │
                      ▼
              auditclient → "job_submit" logged
                      │
                      ▼
              slurm.submit(script_path)
                  └─ sbatch /shared/jobs/alice/train_20260601_120000/job.sbatch
                  └─ returns job_id = 119
                      │
              ════════════════════════════════
              SLURM SCHEDULER (slurmctld)
              ════════════════════════════════
                      │  dispatches to GPU host
                      ▼
  ════════════════════════════════════════════
  GPU HOST  (192.168.122.1 / iit-MS-7E06)
  ════════════════════════════════════════════
              slurmstepd (running as alice, UID 2001)
                  │  cgroup: constrained CPUs + RAM
                  │  GPU: RTX 5090 visible (ConstrainDevices=no)
                  ▼
              /bin/bash job.sbatch
                  │  conda activate /shared/envs/llm-finetune
                  │  python /shared/alice/finetune.py ...
                  ├─ stdout → /shared/jobs/alice/train_.../slurm-119.out
                  └─ stderr → /shared/jobs/alice/train_.../slurm-119.err

  (login node reads job output via NFS — same /shared mount)
              │
              ▼
  ════════════════════════════════════════════
  USER WATCHES on login node  (via dashboard)
  ════════════════════════════════════════════
      dashboard.run_dashboard()
          │
          ├─ slurm.queue() every 2s → job 119 RUNNING
          ├─ slurm.get_node_stats() → GPU 100% 28GB/32GB 78°C
          ├─ log tail: reads slurm-119.out (NFS) → shows last 20 lines
          └─ [Q] quit or [C] cancel or [S] switch job
```

---

## 11. Job Submission Flow

### 11.1 Standard batch job

```
  wizard.run_wizard()
      │
      ├─ [validate] clean_run_command(args)
      ├─ [validate] clean_job_name(name)
      ├─ [validate] in_jail(script_path)
      │
      ├─ [audit] log("job_submit_attempt")
      │
      ├─ jobs.make_job_folder()   → mkdir 0770 + chgrp gpuusers
      ├─ jobs.render_sbatch()     → write job.sbatch
      │
      ├─ [audit] log("job_submit", job_id=<id>)   ← BLOCKS if audit socket AND spool both fail
      │
      └─ slurm.submit() → sbatch → returns job_id
```

### 11.2 Job array

```
  wizard detects array notation ("1-10" or "0-9%2") in args
      │
      └─ render_sbatch adds: #SBATCH --array=1-10
         Submit: sbatch → job_id = 120_[1-10]
```

### 11.3 Job dependency

```
  wizard asks: "Depends on job ID?" → user enters 119
      │
      └─ render_sbatch adds: #SBATCH --dependency=afterok:119
         Job 120 waits in PENDING until 119 COMPLETES
```

### 11.4 Interactive session

```
  wizard selects task_type="interactive"
      │
      └─ slurm.interactive() → srun --pty --gres=gpu:1 bash
         User gets an interactive shell ON THE GPU HOST (only this path)
```

---

## 12. Audit System — Complete Reference

### 12.1 Architecture

```
  TUI (any user, any module)
      │
      │  auditclient.log(action, detail, job_id)
      │
      ▼
  Unix datagram socket: /run/iit-gpu/audit.sock  (0777 — world-writable)
      │
      │  If socket is unavailable:
      │  → spool to /run/iit-gpu/spool/<uuid>.json (disk fallback)
      │  → re-sent on next successful socket connection
      │
      │  If BOTH socket AND spool fail: log_or_block() returns False
      │  → job submission is BLOCKED (safety policy)
      │
      ▼
  iit-gpu-audit.service  (systemd, User=gpusync, runs deploy/audit_daemon.py)
      │
      ├─ SQLite (WAL mode): /var/lib/iit-gpu/audit.db
      │     table: events(id, ts, user, session, action, detail, job_id, remote)
      └─ JSONL:              /var/lib/iit-gpu/audit.jsonl
```

### 12.2 What is logged

| Action | Trigger |
|--------|---------|
| `session_start` | Every TUI launch |
| `session_end` | Every clean TUI exit |
| `signal_exit` | SIGINT/SIGQUIT received |
| `job_submit` | Every sbatch submission (with job_id) |
| `job_cancel` | Every scancel |
| `job_hold` | scontrol suspend |
| `job_release` | scontrol resume |
| `job_requeue` | scontrol requeue |
| `env_create` | conda env creation |
| `env_delete` | conda env deletion |
| `model_download` | HF model download |
| `file_delete` | File or directory deletion |
| `file_upload` | File upload |
| `shell_cmd` | Every command in the restricted shell |
| `admin_node_drain` | Admin drains a node |
| `admin_node_resume` | Admin resumes a node |
| `admin_provision_user` | Admin provisions a new user |
| `admin_offboard_user` | Admin offboards a user |

### 12.3 Audit event schema

Each event is a JSON object:
```json
{
  "ts":      "2026-06-01T07:30:40.611029+00:00",   // ISO-8601 UTC timestamp
  "user":    "public",                               // Linux username
  "session": "f34003c8-3a69-4c6d-ab67-f504ce9e74c8", // UUID per TUI invocation
  "action":  "job_submit",                          // action key (see table above)
  "detail":  "finetune_20260601_045303",            // action-specific detail
  "job_id":  "118",                                 // SLURM job ID (empty if N/A)
  "remote":  "192.168.122.10"                       // SSH_CLIENT IP
}
```

### 12.4 How to access audit logs

#### As slurmadmin (shell):

```bash
# View JSONL tail (most recent events):
sudo tail -f /var/lib/iit-gpu/audit.jsonl

# Query SQLite for all job submissions by a user:
sudo sqlite3 /var/lib/iit-gpu/audit.db \
  "SELECT ts, user, action, detail, job_id FROM events
   WHERE user='public' AND action='job_submit'
   ORDER BY ts DESC LIMIT 20;"

# All events in the last hour:
sudo sqlite3 /var/lib/iit-gpu/audit.db \
  "SELECT ts, user, action, detail FROM events
   WHERE ts > datetime('now', '-1 hour')
   ORDER BY ts;"

# Count sessions per user:
sudo sqlite3 /var/lib/iit-gpu/audit.db \
  "SELECT user, count(*) as sessions FROM events
   WHERE action='session_start' GROUP BY user ORDER BY sessions DESC;"

# All admin actions:
sudo sqlite3 /var/lib/iit-gpu/audit.db \
  "SELECT ts, user, action, detail FROM events
   WHERE action LIKE 'admin_%' ORDER BY ts;"
```

#### As a gpuadmins member (via TUI Admin panel):

```
Main Menu → 7. Admin → Audit Log Viewer
```
The admin panel shows the most recent N events with timestamp, user, action, detail.
Filterable by user and action type.

#### Service status:
```bash
sudo systemctl status iit-gpu-audit
sudo journalctl -u iit-gpu-audit --since "1 hour ago"
```

### 12.5 Spool fallback behavior

When `iit-gpu-audit.service` is down:
1. `auditclient.log()` writes `<uuid>.json` to `/run/iit-gpu/spool/`
2. On service restart, `_drain_spool()` re-reads all spool files and inserts them
3. This means audit history is preserved even through service restarts
4. **Exception:** if the spool directory itself is unavailable (e.g. `/run/iit-gpu`
   doesn't exist), `log_or_block()` returns `False` and job submission is refused

---

## 13. GPU Stats Pipeline

### 13.1 Architecture

```
  GPU HOST (iit-MS-7E06, 192.168.122.1)
      │
      iit-gpu-stats.service
          └── deploy/iit-gpu-stats-writer (Python script)
                  │  every 2 seconds:
                  │  nvidia-smi → gpu_util, gpu_mem, temp, power
                  │  /proc/loadavg → cpu_load1, cpu_load5
                  │  /proc/meminfo → mem_used, mem_total
                  ▼
              /shared/.gpu_stats.json  (NFS — readable by login node)

  LOGIN NODE
      │
      slurm.get_node_stats()
          ├─ scontrol show node (no sudo) → CPU/RAM/state from SLURM
          └─ read /shared/.gpu_stats.json → GPU/CPU/RAM real-time
              │  freshness check: mtime ≤ 10 seconds
              │  if stale: fall back to _read_hw_stats_direct()
              │      └── ssh to GPU host → nvidia-smi + /proc (direct probe)
              ▼
          NodeStats dataclass (merged)
```

### 13.2 Stats JSON format

```json
{
  "gpu_util":         87,        // %
  "gpu_mem_util":     88,        // %
  "gpu_mem_used_mb":  28672,     // MB
  "gpu_mem_total_mb": 32607,     // MB (32 GB GDDR7)
  "gpu_temp":         78,        // °C
  "gpu_power_w":      595.0,     // W
  "cpu_load1":        8.31,      // 1-min load avg
  "cpu_load5":        7.90,      // 5-min load avg
  "cpu_util":         12,        // %
  "mem_total_mb":     63030,     // MB
  "mem_used_mb":      24576,     // MB
  "ts":               1780204240.7  // Unix timestamp
}
```

### 13.3 Dashboard panel color coding

| Metric | Green | Yellow | Red |
|--------|-------|--------|-----|
| GPU util | < 70% | 70–90% | > 90% |
| GPU temp | < 70°C | 70–85°C | > 85°C |
| VRAM | < 70% | 70–90% | > 90% |
| CPU util | < 70% | 70–90% | > 90% |
| RAM | < 70% | 70–90% | > 90% |

---

## 14. Services Inventory

### 14.1 Login node (192.168.122.10)

| Service | User | State | Purpose |
|---------|------|-------|---------|
| `slurmctld.service` | slurm | active | SLURM controller daemon |
| `slurmdbd.service` | slurm | active | SLURM accounting daemon |
| `mariadb.service` | mysql | active | slurm_acct_db backend |
| `iit-gpu-audit.service` | gpusync | active | Audit event receiver + SQLite writer |
| `nfs-server.service` | root | n/a | NFS server runs on GPU host, not here |
| sshd | root | active | SSH daemon (entry point for all users) |

### 14.2 GPU host (192.168.122.1 / iit-MS-7E06)

| Service | User | State | Purpose |
|---------|------|-------|---------|
| `slurmd.service` | slurm | active | SLURM worker daemon |
| `iit-gpu-stats.service` | root | active | Writes /shared/.gpu_stats.json every 2s |
| `nfs-server.service` | root | active | Exports /mnt/nvme_storage/shared |

### 14.3 Service dependencies

```
  sshd → ForceCommand → iit-gpu-manager → audit.sock → iit-gpu-audit (gpusync)
                                       ↓
                                  slurmctld → slurmdbd → mariadb
                                       ↓
                                   slurmd (GPU host)
                                       ↓
                              iit-gpu-stats (GPU host) → /shared/.gpu_stats.json
```

### 14.4 Starting / stopping services

```bash
# Login node
sudo systemctl restart iit-gpu-audit   # restart audit daemon (spool is preserved)
sudo systemctl restart slurmctld       # restart controller (jobs continue on slurmd)
sudo systemctl restart slurmdbd        # restart accounting daemon

# GPU host
sudo systemctl restart slurmd          # restart worker (kills running jobs!)
sudo systemctl restart iit-gpu-stats   # restart stats writer (stats gap until restart)
```

---

## 15. Security Model

### 15.1 Defense-in-depth layers

```
  Layer 1 — Network
  ─────────────────
  • External access: port 2225 only (SSH)
  • No HTTP, no RPC, no direct SLURM port exposure to external network
  • Internal cluster on 192.168.122.0/24 (KVM bridge, isolated)

  Layer 2 — SSH enforcement
  ─────────────────────────
  • Match Group gpuusers → ForceCommand → TUI only (no shell escape)
  • AllowTcpForwarding no (no tunnelling out)
  • PermitUserRC no (no .ssh/rc execution)

  Layer 3 — Process isolation
  ───────────────────────────
  • env -i on launcher (strips user environment)
  • SLURM cgroups: ConstrainCores + ConstrainRAMSpace
  • Jobs run as the authenticated user (per-user identity mode)

  Layer 4 — Filesystem jail
  ─────────────────────────
  • validate.in_jail() on every file path
  • All user-writable paths under /shared only
  • /shared/<user>/ is 0700 (user can only see their own files)
  • No write access to /opt/iit-gpu/ (0750 slurmadmin:gpuusers)

  Layer 5 — SLURM authorization
  ──────────────────────────────
  • Normal users can only sbatch/squeue/scancel their own jobs
  • QOS limits: max 1 GPU per user, 8h max wall time
  • Fairshare prevents any user from monopolizing the cluster

  Layer 6 — Audit + blocking
  ──────────────────────────
  • All significant actions logged before execution
  • log_or_block() refuses job submission if audit is completely unavailable
  • Audit records include user, session UUID, IP, timestamp

  Layer 7 — Admin gating
  ──────────────────────
  • Admin panel visible only to gpuadmins members (is_admin() check)
  • Elevated operations (drain, provision) audited and sudoers-scoped
```

### 15.2 Threat model — what's protected

| Threat | Mitigation |
|--------|------------|
| User escapes TUI to shell | ForceCommand — TUI is the only process allowed |
| Path traversal attack | validate.in_jail() on every file path + symlink resolve |
| Job submission with malicious script | validate.clean_run_command() + sbatch path jail |
| Unauthorized GPU access | QOS MaxTRESPerUser=gres/gpu=1 |
| User reads another user's files | Private 0700 dirs; TUI file manager is jailed to own dir |
| Root privilege escalation | Sudoers scoped to specific commands + admin group gating |
| Audit log tampering | Audit daemon runs as unprivileged gpusync; no write access to DB from user sessions |
| OOM kill from large model download | HF Xet backend disabled; `max_workers=4`; resumes on re-run |
| /tmp overflow during conda build | TMPDIR redirected to /shared/tmp (1.7 TB) |

### 15.3 Known open risks

| Risk | Severity | Notes |
|------|----------|-------|
| Login node has no swap (3.8 GB RAM) | Medium | Large memory spikes (>3 GB) can OOM-kill TUI sessions. Recommended: add 8 GB swapfile. |
| ConstrainDevices=no | Low | All GPU jobs can see all GPUs (no per-job device allowlist). Acceptable on a single-GPU cluster. |
| Shared-state files are 0666 | Low | Any gpuusers member can overwrite env/model/template registries. Acceptable given ForceCommand isolation. |
| No network egress filtering | Low | Jobs running on GPU host can make outbound connections (needed for HF downloads). |

---

## 16. Deployment Pipeline

### 16.1 Repository → deployed system

```
  GitHub repo                         Deployed at
  ──────────────                      ──────────────────────────────────────
  main branch                    →    /opt/iit-gpu/   (canonical clone)
  deploy/site.env.example        →    /opt/iit-gpu/deploy/site.env  (git-ignored, live config)
  deploy/iit-gpu-audit.service   →    /etc/systemd/system/iit-gpu-audit.service
  deploy/sshd-gateway.conf       →    /etc/ssh/sshd_config.d/iit-gpu-gateway.conf
  deploy/sudoers-gateway-admin   →    /etc/sudoers.d/iit-gpu-gateway
  deploy/iit-gpu-adduser.sh      →    /usr/local/bin/iit-gpu-adduser
  deploy/iit-gpu-deluser.sh      →    /usr/local/bin/iit-gpu-deluser
  deploy/install.sh              →    one-time bootstrap (runs once on fresh cluster)
  deploy/iit-gpu-stats-writer    →    /opt/iit-gpu/deploy/ (symlinked to by service)
```

### 16.2 Update procedure (standard — code changes)

```bash
# On the GPU host — runs host-side script which SSHs to the login node:
bash /tmp/redeploy.sh

# What redeploy-host.sh does:
# 1. SSH to login node → runs /home/slurmadmin/redeploy-igm.sh
# 2. Checks iit-gpu-stats.service is running; restarts if stale

# What redeploy-igm.sh does on the login node:
# 1. Git: if local changes → commit + push; if clean → git pull --ff-only
# 2. Tests: python3 -m pytest tests/ -q  (ABORTS if any test fails)
# 3. sudo systemctl stop iit-gpu-audit
# 4. [/opt/iit-gpu is a git clone — git pull already updated it]
# 5. Clear __pycache__: find /opt/iit-gpu -name '__pycache__' -exec rm -rf {} +
# 6. Rebuild /usr/local/bin/iit-gpu-manager launcher
# 7. sudo systemctl start iit-gpu-audit
# 8. Smoke check: python3 -c "import iitgpu.config" as public user
```

### 16.3 CI guard — no hardcoded site values

`tests/test_no_hardcoded_site_values.py` fails CI if any of these appear in
`iitgpu/`:
- `192.168.` or `10.35.` (hardcoded IPs)
- `:2225` (hardcoded port)
- `sudo -u daham` (hardcoded shared user)

This ensures `main` stays releasable for other clusters.

### 16.4 Deploy model design

- **Single canonical clone** at `/opt/iit-gpu/` — no rsync, no copy. Update = `git pull`.
- **`core.fileMode false`** set on `/opt/iit-gpu` — exec-bit-only diffs (common on
  NFS mounts) don't abort `git pull --ff-only`.
- **All users share the same code** — `PYTHONPATH=/opt/iit-gpu` in every user's launcher.
  An update affects every user immediately on their next TUI launch.

---

## 17. User Onboarding & Offboarding

### 17.1 Onboarding — adding a new user

```
sudo addUser.sh                          # interactive wrapper
# OR:
sudo iit-gpu-adduser <username>          # direct
sudo iit-gpu-adduser <username> --admin  # grant admin panel access
sudo iit-gpu-adduser <username> --dry-run  # preview only
```

What the script does:
1. Finds the highest free UID on both login node AND GPU host (SSH)
2. `useradd` on login node with that UID
3. SSH to GPU host → `useradd` with the same UID
4. `usermod -aG gpuusers <username>` on both nodes
5. `sacctmgr add user <username> account=default qos=normal`
6. SSH to GPU host → `mkdir /shared/<username>; chown <uid>:<uid>; chmod 0700`
7. `ln -sfn /shared/<username> /home/<username>/shared`
8. Verifies: UID matches on both nodes + user is in gpuusers

After this: `ssh alice@<gateway>` lands alice directly in the TUI.
Set a password: `sudo passwd alice` or install `~alice/.ssh/authorized_keys`.

### 17.2 Offboarding — removing a user

```
sudo iit-gpu-deluser <username>
sudo iit-gpu-deluser <username> --purge-data   # also removes /shared/<username>
```

What the script does:
1. `userdel` on login node (removes from gpuusers automatically)
2. SSH to GPU host → `userdel` with same UID
3. `sacctmgr delete user <username>`
4. If `--purge-data`: SSH to GPU host → `rm -rf /shared/<username>`

After this: any active SSH session is terminated at next command (ForceCommand fails).

### 17.3 Provisioning plumbing (one-time setup)

The onboarding script SSHs from the login node's root to the GPU host. This requires:
1. Login node root has an SSH key: `/root/.ssh/id_ed25519`
2. GPU host `~root-daham/.ssh/authorized_keys` contains that key
3. GPU host `/etc/sudoers.d/iit-gpu-provisioning` grants `root-daham` passwordless sudo
   for `useradd/userdel/groupadd/groupdel/usermod/mkdir/chown/chmod`

---

## 18. Prebuilt Environments & Containers

### 18.1 Available prebuilt conda environments (envs/specs/*.yml)

| Env name | Key packages | CUDA build | Approx build time |
|----------|-------------|------------|-------------------|
| `pytorch-cuda` | torch 2.7.*, torchvision, torchaudio | cu128 | 15–20 min |
| `llm-finetune` | torch, transformers 5.9, trl 1.5.1, peft, bitsandbytes | cu128 | 25–40 min |
| `jax-cuda` | jax[cuda12] | cuda12 | 15–25 min |
| `tensorflow-cuda` | tensorflow (latest) | CUDA via pip | 10–15 min |
| `data-science` | numpy, scipy, pandas, scikit-learn, matplotlib | CPU only | 5–10 min |

Install via: `TUI → Setup → Install a prebuilt environment` or:
```bash
TMPDIR=/shared/tmp conda env create -f /opt/iit-gpu/envs/specs/<name>.yml
```
**Must use `TMPDIR=/shared/tmp`** — `/tmp` is a 2 GB tmpfs that overflows unpacking
CUDA wheels (cudnn 727 MB + cublas 610 MB).

### 18.2 Apptainer containers

Definition files: `envs/images/*.def`  
Built images: `/shared/images/*.sif`

Build:
```bash
sudo apptainer build /shared/images/llm-finetune.sif \
  /opt/iit-gpu/envs/images/llm-finetune.def
```
(20–40 min per image; VRAM not required for build)

Use at submit time: `TUI → Run a job → Environment type → Container image (.sif)`

### 18.3 Env discovery

The TUI discovers environments from two sources (merged, de-duped):
1. **Shared registry** `/shared/envs/.registry.json` — populated when an env is
   installed via the TUI or the shared `conda env create`
2. **Per-user `environments.txt`** — conda's standard discovery file in the
   user's home (catches envs installed outside the TUI)

---

## 19. Test Suite

**334 tests — all passing** (as of 2026-06-01)

```
tests/
├── test_accounting.py        GPU/CPU-hour aggregation, fairshare, sreport
├── test_adduser_wrapper.py   interactive addUser.sh prompt logic
├── test_admin.py             admin functions: drain/resume, provision/offboard, gate
├── test_auditclient.py       socket send, spool fallback, block-on-both-fail
├── test_config.py            defaults, env overrides, demo mode, path helpers, is_admin
├── test_containers.py        container listing, delete
├── test_dashboard.py         log tail, job finder, time parser, NodeStats None on fail
├── test_dependencies.py      AST-scans iitgpu/ for every 3rd-party import, asserts in requirements.txt
├── test_e2e.py               selftest, demo submit+queue, audit spooling
├── test_envbuilder.py        framework packages, conda env create, missing conda
├── test_envs.py              env list/delete, shared registry persistence
├── test_files.py             mkdir/rename/delete/copy, jail enforcement
├── test_hardening.py         sshd config, sudoers scope
├── test_jobs.py              folder naming, sbatch render (all directives), task defaults
├── test_models.py            HF download, Xet disabled, registry write
├── test_monitor_completeness.py  hold/release/requeue, seff, live follow, history
├── test_no_hardcoded_site_values.py  CI guard: no IPs, ports, or 'sudo -u daham' in iitgpu/
├── test_notebook.py          notebook job submission
├── test_notebooks.py         running services list, teardown
├── test_notify.py            MTA detection, poll_until_done
├── test_onboarding.py        UID allocation, both-node creation, SLURM association
├── test_permissions.py       job dir 0770, shared files 0666
├── test_prebuilt_envs.py     parses every spec's pip block; asserts one requirement per line
├── test_setup.py             health check, smoke test, arrow-key menu
├── test_shell.py             allowed commands, path jail, flag blocklist
├── test_slurm.py             NodeStats parsing, queue, sacct, GPU job count
├── test_submit_completeness.py  arrays, deps, interactive srun
├── test_templates.py         template save/load, resource limit validation
├── test_upload.py            folder name, URL safety, browse jail
├── test_validate.py          in_jail, safe_listdir, clamp_int, sanitizers
└── test_wizard.py            4-step wizard flow, model selection, template handling
```

Run: `python3 -m pytest tests/ -q` from the repo root (or `/opt/iit-gpu/`).

---

## 20. All Issues & Fixes (M01–M03 Consolidated)

### M01 fixes (initial deploy)

| # | Problem | Root Cause | Fix | Commit |
|---|---------|-----------|-----|--------|
| 1 | Cluster panel showed zeros | `scontrol` run with `sudo -u daham` — not in sudoers for scontrol | Run `scontrol` without sudo (it's read-only) | `165b4e2` |
| 2 | `recent_jobs()` returned `user="?"`, `time_used="-"` | Hardcoded values; only scanned files | Extract user from path; compute elapsed from `stat --format=%W %Y` | `165b4e2` |
| 3 | Hardware stats showed SLURM allocation (0 when idle) | No real utilization data source | Added stats writer daemon on compute node → `/shared/.gpu_stats.json` | `165b4e2` |
| 4 | Job table showed fake bouncing progress bar | No real progress data; ETA always "no limit" | Removed Progress/ETA columns; added braille spinner | `13c74db` |
| 5 | "View hardware stats" was unreachable | Added to `monitor.py:monitor_menu()` which is never called | Added to `menu.py:_monitor_menu()` (the actual monitor menu) | `4e4ef7c` |
| 6 | `get_node_stats()` still used sudo for scontrol | Not fixed in first pass | Run `scontrol show node` directly | `87ea135` |
| 7 | Hardware stats showed `GPU 0/1` during active jobs | `AllocTRES` omits GPU on this SLURM build | Count via `squeue --states=RUNNING --format=%b \| grep gpu` | `2aa7c09` |
| 8 | `cudnn.benchmark=True` consumed 5–15 min on first epoch | RTX 5090 (Blackwell sm_120) has no cached cuDNN kernel timings; CIFAR-10 has fixed shapes | Removed `benchmark=True` | (train script) |
| 9 | Epoch output never appeared in log files until script exit | Python block-buffers stdout (4 KB) when writing to file | `sys.stdout.reconfigure(line_buffering=True)` | (train script) |
| 10 | `redeploy.sh` tried to `cd` to repo path that doesn't exist on compute node | Script ran on compute node; repo is on login node | Rewrote to SSH to login node for all git/deploy ops | `8003513` |
| 11 | `cp -r` left stale files from renamed/deleted modules | `cp` doesn't remove destination-only files | Replaced with `rsync --delete` + wipe `__pycache__` | `8003513` |

### M03 fixes (post-deploy, 2026-05-31)

| # | Problem | Root Cause | Fix | Commit |
|---|---------|-----------|-----|--------|
| 1 | `conda: error: unrecognized arguments: --force` | conda ≥24 removed `--force` from `conda env create` | Use `--yes` instead | `453185d` |
| 2 | `ERROR: Invalid requirement: 'torch==2.7.* torchvision torchaudio'` | All packages on one pip requirements line | One package per line; `--extra-index-url` on its own line | `7b50526` |
| 3 | `huggingface_hub not installed` | `install.sh` hardcoded specific packages, skipped huggingface_hub | `install.sh` runs `pip3 install -r requirements.txt` (single source of truth) | `2790b23` |
| 4 | `Cannot uninstall click 8.1.8` | `huggingface_hub` 1.x pulls `typer` → `click 8.4.1` over Debian-managed 8.1.8 | Pin `huggingface_hub>=0.20,<1.0` | `ac5a7ce` |
| 5 | Conda build dies mid-CUDA-wheel (no spec error) | `/tmp` is 2 GB tmpfs; cudnn+cublas wheels overflow it | Set `TMPDIR`/`PIP_CACHE_DIR` to `/shared/tmp` during prebuilt build | `f3211fa` |
| 6 | Prebuilt conda env not visible to other users | `_load_venv_registry` filtered to `kind=="venv"`, discarded conda entries | Load all registry entries; list_all_envs already de-dupes | `dd09149` |
| 7 | Large HF download OOM-killed the TUI session | Login node 3.8 GB RAM, no swap; huggingface_hub Xet backend buffered 3.4 GB RSS → OOM kill | Set `HF_HUB_DISABLE_XET=1`; cap `max_workers=4`; downloads resume | `1108a47` |
| 8 | Model registry permission denied for other users | Shared-state files owned by first creator; root_squash blocks chown over NFS; ACLs unsupported | `config.make_shared_writable()` sets shared files to 0666/0777 on creation | `71d5045` |
| 9 | Audit daemon crash-looped; job submit refused | `gpusync` not in `gpuusers`; couldn't read `/opt/iit-gpu` (0750); socket never created | `usermod -aG gpuusers gpusync`; restart daemon | `1093479` |

### M03 incident — job 117, TRL 1.x API break (2026-06-01)

| # | Problem | Root Cause | Fix |
|---|---------|-----------|-----|
| 10 | `TypeError: SFTTrainer.__init__() got unexpected kwarg 'dataset_text_field'` | TRL ≥1.0 moved SFT knobs to `SFTConfig`; env had trl 1.5.1 | Rebuild as `SFTConfig` (subclass of `TrainingArguments`); move `dataset_text_field` + `max_length` into it; pass `processing_class=tokenizer` to `SFTTrainer` |

Old API (trl < 1.0):
```python
trainer = SFTTrainer(
    model=model,
    tokenizer=tokenizer,
    train_dataset=dataset,
    dataset_text_field="text",
    max_seq_length=512,
    args=training_args,
)
```

New API (trl ≥ 1.0):
```python
sft_config = SFTConfig(
    **training_args.to_dict(),
    dataset_text_field="text",
    max_length=512,
)
trainer = SFTTrainer(
    model=model,
    processing_class=tokenizer,
    train_dataset=dataset,
    args=sft_config,
)
```

Also fixed: `#SBATCH --job-name` now uses the full job folder name
(`finetune_20260601_045303`) not just `finetune`, making each run uniquely
identifiable in squeue/sacct/log listings. (Commit `b89a77b`)

---

## 21. Active State & Pending Items

### 21.1 Current live state (2026-06-01)

| Component | State |
|-----------|-------|
| SLURM 25.11.2 | slurmctld + slurmd: active |
| slurmdbd + MariaDB | active |
| iit-gpu-audit | active (running since 2026-05-31 17:17) |
| iit-gpu-stats | active on GPU host |
| RTX 5090 | ALLOCATED (job 118 running — finetune, user=public) |
| NFS /shared | mounted on both nodes |
| Per-user identity | ACTIVE (`GATEWAY_SHARED_USER=0` in `/opt/iit-gpu/deploy/site.env`) |
| Test suite | 334 passing |
| menu.py | 1 unstaged change in repo (uncommitted; identical to deployed `/opt/iit-gpu`) |

### 21.2 Repo vs deployed discrepancy

The repo (`/home/slurmadmin/IIT-Secure-SLURM-Job-Gateway/`) has:
- `deploy/site.env` — still shows `GATEWAY_SHARED_USER=1` with a legacy comment
- `iitgpu/menu.py` — modified but not staged

The deployed path (`/opt/iit-gpu/deploy/site.env`) correctly has `GATEWAY_SHARED_USER=0`.
**Action:** commit the unstaged menu.py change and update the repo's site.env to
reflect the live deployed state (or add a `.gitignore` entry for `deploy/site.env`
since it's already git-ignored per the .gitignore).

### 21.3 Pending optional steps

| Step | Priority | Command / Notes |
|------|----------|-----------------|
| Add swap to login node | Medium | `sudo fallocate -l 8G /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile` + fstab entry. Prevents OOM on memory spikes. |
| Build Apptainer .sif images | Low | `sudo apptainer build /shared/images/<name>.sif /opt/iit-gpu/envs/images/<name>.def` (20–40 min each) |
| Set GATEWAY_SHARED_USER=0 in repo site.env | Low | Align repo with deployed state for documentation clarity |
| Commit unstaged menu.py | Low | `git add iitgpu/menu.py && git commit` |
| Provision real user accounts | When needed | `sudo addUser.sh` for each new user |
| Enable ConstrainDevices | Optional | Needs per-job NVIDIA eBPF allowlist; risk of hiding GPU |
| XFS project quotas on /shared | Optional | Prevents a single user from filling the 1.7 TB NVMe |

---

## 22. Quick Reference

### 22.1 Key paths

| Path | What it is |
|------|-----------|
| `/opt/iit-gpu/` | Deployed Python package (login node) — `git pull` to update |
| `/opt/iit-gpu/deploy/site.env` | Live cluster config (git-ignored, not committed) |
| `/usr/local/bin/iit-gpu-manager` | Launcher (ForceCommand target) |
| `/home/slurmadmin/IIT-Secure-SLURM-Job-Gateway/` | Git repo |
| `/home/slurmadmin/redeploy-igm.sh` | Login-node deploy script |
| `/tmp/redeploy.sh` | Host-side deploy script (GPU host) |
| `/shared/jobs/<user>/<name>_<ts>/` | Per-job output directory |
| `/shared/jobs/<user>/<name>_<ts>/slurm-<id>.out` | SLURM stdout |
| `/shared/jobs/<user>/<name>_<ts>/job.sbatch` | Generated sbatch script |
| `/shared/envs/.registry.json` | Shared env registry (0666) |
| `/shared/.gpu_stats.json` | Live GPU/CPU/RAM metrics (2s cadence) |
| `/run/iit-gpu/audit.sock` | Audit daemon Unix socket |
| `/run/iit-gpu/spool/` | Audit event spool (offline buffer) |
| `/var/lib/iit-gpu/audit.db` | Audit SQLite database |
| `/var/lib/iit-gpu/audit.jsonl` | Audit JSONL log |
| `/etc/sudoers.d/iit-gpu-gateway` | Sudoers for gateway/admin operations |
| `/etc/ssh/sshd_config.d/iit-gpu-gateway.conf` | ForceCommand + AllowTcpForwarding no |

### 22.2 Hardware facts

| Component | Value |
|-----------|-------|
| GPU | NVIDIA RTX 5090 (Blackwell sm_120) |
| VRAM | 32 GB GDDR7 |
| BF16 peak | 1,792 TFLOPS |
| CPU | Intel i9-14900K, 32 logical cores |
| RAM | 63 GB |
| Login node RAM | 3.8 GB (no swap — add swapfile) |
| NFS storage | 1.7 TB NVMe (`/mnt/nvme_storage/shared`) |
| SLURM version | 25.11.2 |
| Conda | 26.3.2 (miniforge3) |
| Python | 3.14 (system) |

### 22.3 Common operational commands

```bash
# As slurmadmin — check cluster state
sinfo; squeue; sacctmgr show assoc

# Add a user
sudo addUser.sh

# Deploy code update
bash /tmp/redeploy.sh   # from GPU host

# View audit log (recent)
sudo tail -f /var/lib/iit-gpu/audit.jsonl

# Query audit SQLite
sudo sqlite3 /var/lib/iit-gpu/audit.db "SELECT ts,user,action,detail FROM events ORDER BY ts DESC LIMIT 20;"

# Check service health
sudo systemctl status iit-gpu-audit slurmd slurmctld slurmdbd

# Run tests
cd /home/slurmadmin/IIT-Secure-SLURM-Job-Gateway && python3 -m pytest tests/ -q

# If jobs fail with RaisedSignal:53 after interactive srun
ssh root-daham@192.168.122.1 'sudo systemctl restart slurmd'

# Build a prebuilt conda env manually
TMPDIR=/shared/tmp conda env create -f /opt/iit-gpu/envs/specs/llm-finetune.yml

# Rollback to shared-user mode (emergency)
# Edit /opt/iit-gpu/deploy/site.env → set GATEWAY_SHARED_USER=1
# sudo cp /opt/iit-gpu/deploy/sudoers-gateway /etc/sudoers.d/iit-gpu-gateway
```

---

*End of M04. This document consolidates M01–M03 and represents the complete
architectural state of the IIT Secure SLURM Job Gateway as of 2026-06-01.*
