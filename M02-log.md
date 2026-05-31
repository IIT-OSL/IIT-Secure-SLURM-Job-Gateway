# M02 — IIT Secure SLURM Job Gateway: Full System & Architecture Log

**Date:** 2026-05-31
**Author:** Daham Dissanayake
**Scope:** Complete post-upgrade audit of the cluster — SLURM, accounting, NFS,
filesystem, Linux users/groups, and the TUI tool architecture & data flow.
**Supersedes/extends:** [M01-log.md](./M01-log.md)
**Repo state:** branch `main`, Phases 1–7 merged and deployed to `/opt/iit-gpu`.

---

## Table of Contents

1. [Cluster Topology](#1-cluster-topology)
2. [SLURM Configuration (both nodes)](#2-slurm-configuration-both-nodes)
3. [Accounting Stack — slurmdbd + MariaDB + sacct](#3-accounting-stack--slurmdbd--mariadb--sacct)
4. [QOS & Partition Policy](#4-qos--partition-policy)
5. [NFS & Shared Storage](#5-nfs--shared-storage)
6. [Linux Users & Groups (both nodes)](#6-linux-users--groups-both-nodes)
7. [The TUI Tool — Architecture & Data Flow](#7-the-tui-tool--architecture--data-flow)
8. [Services Inventory](#8-services-inventory)
9. [Security Model](#9-security-model)
10. [Prebuilt Environments & Containers](#10-prebuilt-environments--containers)
11. [Validation Results](#11-validation-results)
12. [Quick Operational Reference](#12-quick-operational-reference)

---

## 1. Cluster Topology

```
                          192.168.122.0/24
   ┌────────────────────────────┐      ┌────────────────────────────────┐
   │   LOGIN NODE (KVM guest)   │      │   GPU HOST (bare metal /        │
   │   login-node               │      │   KVM hypervisor)               │
   │   192.168.122.10           │◄────►│   iit-MS-7E06   192.168.122.1   │
   │                            │ 6817 │                                 │
   │   slurmctld    (active)    │ 6818 │   slurmd        (active)        │
   │   slurmdbd     (active)    │ 6819 │   munged        (active)        │
   │   mariadb      (active)    │munge │   iit-gpu-stats (active)        │
   │   munged       (active)    │      │                                 │
   │   iit-gpu-audit(active)    │      │   RTX 5090 — 32 GB, sm_120      │
   │                            │      │   32 CPU threads, 61 GB RAM     │
   │   /shared (NFS4 client)    │◄─NFS─│   /mnt/nvme_storage (1.8 TB)    │
   │   Users SSH in as `public` │      │   /shared → symlink to above    │
   └────────────────────────────┘      └────────────────────────────────┘
```

| Property | Login node | GPU host |
|----------|-----------|----------|
| Hostname | `login-node` | `iit-MS-7E06` |
| IP | 192.168.122.10 | 192.168.122.1 |
| Kernel | 7.0.0-15-generic | 7.0.0-15-generic (Ubuntu, Apr 2026) |
| SLURM | slurm-wlm **25.11.2** | slurm-wlm **25.11.2** |
| Root disk | `/dev/vda1` 38 GB (12% used) | `/dev/sda2` 915 GB (4% used) |
| Data disk | — (NFS only) | `/dev/nvme0n1p1` 1.8 TB ext4 (1% used) |

**GPU (live):** NVIDIA GeForce RTX 5090 · 32607 MiB · driver **595.71.05** ·
compute capability **12.0 (sm_120, Blackwell)** · idle ~37 °C.

---

## 2. SLURM Configuration (both nodes)

`/etc/slurm/slurm.conf` (identical on both nodes):

```ini
ClusterName=iit
SlurmctldHost=login-node(192.168.122.10)
AuthType=auth/munge
ProctrackType=proctrack/cgroup
TaskPlugin=task/cgroup
ReturnToService=2
SchedulerType=sched/backfill
SelectType=select/linear
SlurmUser=slurm
StateSaveLocation=/var/spool/slurmctld
SlurmdSpoolDir=/var/spool/slurmd
GresTypes=gpu
NodeName=iit-MS-7E06 NodeAddr=192.168.122.1 CPUs=16 RealMemory=63030 Gres=gpu:1 State=UNKNOWN
PartitionName=gpu Nodes=iit-MS-7E06 Default=YES MaxTime=1-00:00:00 State=UP
SlurmdDebug=debug3
SlurmdLogFile=/shared/slurmd_debug.log
AccountingStorageType=accounting_storage/slurmdbd
AccountingStorageTRES=gres/gpu
AccountingStorageHost=login-node
AccountingStoragePort=6819
JobAcctGatherType=jobacct_gather/linux
JobAcctGatherFrequency=30
```

**Key directives:**

| Directive | Value | Purpose |
|-----------|-------|---------|
| `ProctrackType` / `TaskPlugin` | `cgroup` | Required for cgroup v2 process tracking & task isolation |
| `SelectType` | `select/linear` | Whole-node scheduling (single-GPU node) |
| `GresTypes` / `Gres=gpu:1` | `gpu` | One GPU exposed via GRES |
| `PartitionName=gpu … MaxTime` | `1-00:00:00` | 24 h wall cap on the default partition |
| `AccountingStorageType` | `slurmdbd` | Job history persisted to the accounting DB |
| `AccountingStorageTRES` | `gres/gpu` | Tracks GPU as a TRES → enables per-user GPU limits |
| `JobAcctGatherType` | `jobacct_gather/linux` | Per-job CPU/mem accounting via `/proc` |

`/etc/slurm/gres.conf` (both): `Name=gpu File=/dev/nvidia0`
`/etc/slurm/cgroup.conf` (both):
```ini
CgroupPlugin=autodetect
ConstrainCores=no
ConstrainRAMSpace=no
ConstrainSwapSpace=no
ConstrainDevices=no
```

---

## 3. Accounting Stack — slurmdbd + MariaDB + sacct

The tool queries the SLURM accounting database for job history (file scanning is
retained only as a fallback).

| Component | State | Detail |
|-----------|-------|--------|
| `mariadb` | active | DB `slurm_acct_db`, user `slurm`@localhost |
| `slurmdbd` | active | `/etc/slurm/slurmdbd.conf` (0600, slurm:slurm) |
| DbdPort | 6819 | matches `AccountingStoragePort` |
| Purge policy | 12 months | jobs/events/steps/usage/txn purged after 1 yr |

`slurmdbd.conf` (password redacted):
```ini
AuthType=auth/munge
DbdHost=login-node
DbdPort=6819
StorageType=accounting_storage/mysql
StorageHost=localhost
StorageUser=slurm
StoragePass=***REDACTED***
StorageLoc=slurm_acct_db
SlurmUser=slurm
Purge*After=12months
```

**Registered in the DB (`sacctmgr`):**
- Cluster: `iit` (ControlHost 192.168.122.10)
- Account: `default` (Org=iit) + `root`
- Users → account/QOS: `daham → default/normal`, `public → default/normal`

**TRES tracked:** `gres/gpu`, `gres/gpumem`, `gres/gpuutil` (plus the defaults
cpu/mem/node/billing/energy/fs).

**Tool integration (`iitgpu/config.py`, `iitgpu/slurm.py`):**
- `Config.sacct_enabled` auto-detects via `shutil.which("sacct")` → **True**
  (sacct at `/usr/bin/sacct`). Override with `SACCT_ENABLED=1|0|auto`.
- `sacct_history()` →
  `sacct --noheader --parsable2 --format=JobID,JobName,User,State,Elapsed,Start,End,AllocTRES`.
- `job_history()` uses sacct when enabled, falls back to file scan otherwise.
- The gateway sudoers permits `sudo -u daham sacct`, so the sandboxed `public`
  user gets real DB-backed history in the dashboard.

---

## 4. QOS & Partition Policy

Two QOS defined (`sacctmgr show qos`):

| QOS | MaxWall | MaxTRESPerUser | Use |
|-----|---------|----------------|-----|
| `normal` (default) | 08:00:00 | **`gres/gpu=1`** | All regular users — 1 GPU, 8 h cap |
| `long` | 7-00:00:00 | (none) | Admin / extended experiments |

Partition `gpu`: `MaxTime=1-00:00:00`, `Default=YES`, `State=UP`,
`SelectType=select/linear` (whole-node scheduling).

Because `gres/gpu` is tracked as a TRES, the `MaxTRESPerUser=gres/gpu=1` limit on
`normal` is **actively enforced** — a user cannot hold more than one GPU
allocation at a time. Move `daham` to the `long` QOS for runs beyond 8 h:
`sudo sacctmgr modify user daham set DefaultQOS=long`.

---

## 5. NFS & Shared Storage

**Export (GPU host `/etc/exports`):**
```
/mnt/nvme_storage/shared 192.168.122.0/24(rw,sync,no_subtree_check,no_root_squash)
```
**Mount (login node):**
```
192.168.122.1:/mnt/nvme_storage/shared on /shared type nfs4 (rw,vers=4.2,hard,proto=tcp,sec=sys)
fstab: 192.168.122.1:/mnt/nvme_storage/shared /shared nfs defaults 0 0
```
- GPU host: `/shared` → symlink → `/mnt/nvme_storage/shared` (ext4, 1.8 TB).
- `sec=sys` → permissions enforced by **numeric UID/GID**; UIDs/GIDs are kept
  identical across nodes (see §6) so ownership resolves consistently.

**`/shared` layout (live):**
```
.apptainer_cache/  .apptainer_tmp/   ← Apptainer build scratch (on NVMe, not /tmp)
.gpu_stats.json    ← live metrics, rewritten every 2 s by iit-gpu-stats
.pip-cache/  .pip-tmp/  ← pip routed here to avoid login-VM quota pressure
daham/  public/  dahamtestrun1/      ← per-user working dirs
data/   models/  scripts/  templates/  ← shared assets (group gpuusers)
envs/   → conda envs (pytorch-2.7-test1)
images/ → Apptainer .sif (built on demand)
jobs/   → per-job folders (group gpuusers, mode 0770)
miniforge3/ → shared conda (CONDA_PREFIX_SHARED)
munge.key   (root, 0600)
```
Disk: **1.8 TB total, ~13 GB used (1%).**

---

## 6. Linux Users & Groups (both nodes)

**Rule:** every job-submitting user exists on **both** nodes with the **same
UID**, and shared groups use the **same GID**, because SLURM passes numeric UID
and NFS `sec=sys` enforces numeric UID/GID.

| User | UID | Login node | GPU host | Role |
|------|-----|-----------|----------|------|
| `slurmadmin` | 1000 | ✅ | ✗ | SLURM/login admin |
| `iit` | 1000 | ✗ | ✅ | GPU host console operator |
| `root-daham` | 1001 | ✗ | ✅ | GPU host local admin (sudo) |
| `daham` | 1002 | ✅ | ✅ | Cluster job user (jobs run as this UID) |
| `public` | 1003 | ✅ | ✅ | Sandboxed gateway user (forced TUI) |
| `slurm` | 64030 | ✅ | ✅ | SLURM service account |
| `gpusync` | (svc) | ✅ | — | Audit daemon service account (login) |

**Shared group — identical GID on both nodes:**

| Group | GID (both) | Members |
|-------|-----------|---------|
| `gpuusers` | **1500** | login: daham, public, slurm · gpu: daham, slurm |

`gpuusers` is the access group for the gateway: it scopes the forced-TUI sshd
match, the sudoers privilege drop, and group ownership of `/shared/jobs`,
`/shared/data`, `/shared/envs`, `/shared/models`, `/shared/scripts`,
`/shared/templates`. With the GID identical across nodes, a job running as
`daham` on the GPU host has correct group access to the `0770` job directories
created from the login node — outputs write cleanly, and users cannot read each
other's job folders.

### 6.1 User & group tree — LOGIN NODE (192.168.122.10)

```
login-node
│
├─ Human / login users
│   ├─ slurmadmin ........ UID 1000   primary: slurmadmin(1000)
│   │     └─ groups: auditadmin(983)                       [login admin · full sudo]
│   ├─ daham ............. UID 1002   primary: daham(1002)
│   │     └─ groups: slurm(64030), gpuusers(1500)          [SLURM job identity]
│   └─ public ............ UID 1003   primary: public(1003)
│         └─ groups: slurm(64030), gpuusers(1500)          [forced-TUI gateway user]
│
└─ Service accounts
    ├─ slurm ............. UID 64030  primary: slurm(64030)
    │     └─ groups: gpuusers(1500)                         [slurmctld / slurmdbd]
    ├─ munge ............. UID 111    primary: munge(112)   [MUNGE auth daemon]
    └─ gpusync ........... UID 997    primary: gpusync(984)
          └─ groups: auditadmin(983)                        [iit-gpu-audit daemon]
```

### 6.2 User & group tree — GPU HOST (192.168.122.1)

```
iit-MS-7E06
│
├─ Human / admin users
│   ├─ iit ............... UID 1000   primary: iit(1000)
│   │     └─ groups: adm,sudo(27),libvirt(972),users,…     [console operator · sudo]
│   ├─ root-daham ........ UID 1001   primary: root-daham(1001)
│   │     └─ groups: sudo(27), libvirt(972), users(100)    [local admin · sudo]
│   ├─ daham ............. UID 1002   primary: daham(1002)
│   │     └─ groups: gpuusers(1500)                         [job execution user]
│   └─ public ............ UID 1003   primary: public(1003)
│         └─ groups: slurm(64030)                           [UID resolution only]
│
└─ Service accounts
    ├─ slurm ............. UID 64030  primary: slurm(64030)
    │     └─ groups: gpuusers(1500)                         [slurmd / slurmstepd]
    └─ munge ............. UID 117    primary: munge(118)   [MUNGE auth daemon]
```

> **Cross-node UID/GID consistency:** `daham(1002)`, `public(1003)`,
> `slurm(64030)`, and `gpuusers(1500)` carry **identical numbers on both nodes** —
> the requirement that makes NFS (`sec=sys`) and SLURM (numeric UID hand-off)
> resolve ownership correctly. `slurmadmin(1000)` exists only on the login node
> and `iit(1000)`/`root-daham(1001)` only on the GPU host (their UIDs never cross
> NFS, so no collision).

### 6.3 Privilege / access hierarchy (across both nodes)

```
ACCESS TIERS  (highest privilege → lowest)
│
├─ Tier 0 — Root / sudo
│   ├─ [GPU HOST] iit (1000), root-daham (1001) ........ full sudo on iit-MS-7E06
│   └─ [LOGIN]    slurmadmin (1000) ................... full sudo on login-node
│
├─ Tier 1 — SLURM service plane  (both nodes, numeric-matched)
│   ├─ slurm (64030) .... runs slurmctld / slurmdbd / slurmd; member of gpuusers
│   ├─ munge  ........... MUNGE credential signing (RPC auth between daemons)
│   └─ gpusync (997, login) ... iit-gpu-audit daemon → SQLite WAL + JSONL
│
├─ Tier 2 — gpuusers (GID 1500, both nodes)  ◀── the gateway access group
│   ├─ daham (1002) ..... SLURM job identity — sbatch/squeue/scancel/sinfo/sacct
│   │                     all execute AS daham via sudoers; jobs run under this UID
│   ├─ public (1003) .... sandboxed login; sshd ForceCommand → TUI; sudo→daham only
│   └─ slurm (64030) ..... member so slurmstepd can write the 0770 job dirs
│
└─ Tier 3 — Unprivileged
    └─ (no gateway access outside the above)
```

The flow of privilege at job time: **public** logs in → locked to the TUI →
the tool runs `sudo -u daham …` (Tier 2) → SLURM daemons (Tier 1, MUNGE-authed)
→ `slurmstepd` drops to **daham**'s UID on the GPU host and writes into the
`gpuusers`-owned `0770` job directory.

---

## 7. The TUI Tool — Architecture & Data Flow

### 7.1 Entry & launcher

Users SSH as `public@login-node`; `sshd` forces the gateway (§9). Launcher
`/usr/local/bin/iit-gpu-manager`:

```bash
exec env -i \
    HOME="$HOME" USER="$USER" LOGNAME="$LOGNAME" \
    PATH="/shared/miniforge3/bin:/usr/local/bin:/usr/bin:/bin" \
    SSH_CLIENT="${SSH_CLIENT:-}" TERM="${TERM:-xterm}" \
    PYTHONPATH="/opt/iit-gpu" \
    CONDA_PREFIX_SHARED="/shared/miniforge3" NFS_ROOT="/shared" \
    /usr/bin/python3 -m iitgpu
```
`env -i` strips the user environment. Runtime knobs: `NFS_ROOT`,
`CONDA_PREFIX_SHARED`, `SACCT_ENABLED=auto`, `DEMO_MODE=0`.

### 7.2 Module map (`/opt/iit-gpu/iitgpu/`, 21 modules)

| Module | Responsibility |
|--------|---------------|
| `__main__.py` | flags (`--demo/--selftest/--no-splash`), signal handlers, splash → menu |
| `config.py` | `Config` dataclass; `sacct_enabled` auto-detect; path helpers |
| `menu.py` | main menu (Upload / Setup / Run / Monitor / Advanced / Quit) |
| `wizard.py` | job wizard: task type → env (conda \| container \| none) → script/notebook → submit |
| `jobs.py` | `JobSpec`, `render_sbatch`, `render_notebook_sbatch`, `make_job_folder` (0770) |
| `slurm.py` | `submit_job`, `queue`, `cancel`, `get_node_stats`, `sacct_history`/`job_history`, `recent_jobs` |
| `containers.py` | `list_images` (jailed), `validate_image`, `render_apptainer_wrap` |
| `envbuilder.py` | conda env builder; cu128/torch≥2.7; `_smoke_check_pytorch` (sm_120 + torch.compile) |
| `envs.py` | env registry (`/shared/models/.envs.json`), conda discovery |
| `setup.py` | health check, env setup, install prebuilt env, data/model, smoke test |
| `dashboard.py` | Rich live dashboard (queue + node stats + log tail) |
| `monitor.py` | queue table, cancel, jailed log tail, cluster status |
| `models.py` / `templates.py` | model download / job-template save-load |
| `upload.py` | jailed dataset upload |
| `shell.py` | restricted SLURM command shell (audited) |
| `validate.py` | path jail (`in_jail`), input sanitizers, clamps |
| `auditclient.py` | datagram → audit daemon, spool fallback |
| `ui.py` / `splash.py` | Rich helpers / ASCII splash |

### 7.3 Job submission data flow

```
public (TUI) ──▶ wizard builds JobSpec ──▶ render_sbatch / render_notebook_sbatch
        │                                         │
        │  validate.in_jail() on every path       │  writes /shared/jobs/<user>/<job>_<ts>/job.sbatch (0770)
        ▼                                         ▼
auditclient.log_or_block("job_submit")     slurm.submit_job()
        │                                         │
        ▼                                  sudo -u daham sbatch <script>   (sudoers-gateway)
   audit daemon (gpusync)                         │
   SQLite WAL + JSONL                       slurmctld ─RPC(munge)▶ slurmd ─▶ slurmstepd (drops to daham)
                                                  │
                                            output ▶ /shared/jobs/.../slurm-%j.out|err
```

**Three execution environments the wizard supports:**
1. **Conda/venv** — sources `conda.sh`, `conda activate <path>`.
2. **Container (.sif)** — `apptainer exec --nv --bind /shared <img> bash -lc "<cmd>"`,
   conda skipped; image must pass `validate_image` (jail + `.sif`).
3. **Notebook** — `render_notebook_sbatch`: per-job `JUPYTER_TOKEN`
   (`secrets.token_hex`), JupyterLab bound to `127.0.0.1`, prints
   `ssh -p 2225 -L <port>:localhost:<port> public@10.35.4.100`; works with both
   conda and container envs; auto-teardown on job end.

### 7.4 Live stats path

`iit-gpu-stats` (GPU host) → `nvidia-smi` + `/proc` every 2 s → atomic write to
`/shared/.gpu_stats.json` → `slurm.get_node_stats()` reads it (≤10 s fresh) →
dashboard. Fallback: direct `nvidia-smi`/`/proc` if the file is stale.
Sample: `gpu_util 0%, mem 76/32607 MB, 37 °C, 15 W, cpu 1%`.

---

## 8. Services Inventory

| Service | Node | State | Unit / source |
|---------|------|-------|---------------|
| `slurmctld` | login | active, enabled | distro |
| `slurmdbd` | login | active, enabled | distro + `/etc/slurm/slurmdbd.conf` |
| `mariadb` | login | active, enabled | distro |
| `munge` | both | active | distro |
| `iit-gpu-audit` | login | active, enabled | `deploy/iit-gpu-audit.service` (User=gpusync) |
| `slurmd` | gpu | active | distro |
| `iit-gpu-stats` | gpu | active, enabled | `deploy/iit-gpu-stats.service` (User=root-daham, Restart=always, RestartSec=2) |

The GPU stats writer runs under systemd at `/usr/local/bin/iit-gpu-stats-writer`
(`Restart=always`), surviving crashes and reboots; no cron involvement.

**Audit daemon state (login):** `/var/lib/iit-gpu/audit.db` (SQLite WAL) +
`audit.jsonl`; socket `/run/iit-gpu/audit.sock` + spool dir, owned by `gpusync`.

---

## 9. Security Model

1. **Forced TUI** — `deploy/sshd-gateway.conf`:
   ```
   Match Group gpuusers
       ForceCommand /usr/local/bin/iit-gpu-manager
       PermitTTY yes
       AllowTcpForwarding no   AllowAgentForwarding no
       AllowStreamLocalForwarding no   X11Forwarding no
       PermitTunnel no   GatewayPorts no   PermitUserRC no
   ```
2. **Privilege drop via sudoers** — `/etc/sudoers.d/iit-gpu-gateway`:
   ```
   Defaults:gpuusers !lecture, timestamp_timeout=0
   %gpuusers ALL=(daham) NOPASSWD: /usr/bin/sbatch, /usr/bin/squeue,
                                    /usr/bin/scancel, /usr/bin/sinfo, /usr/bin/sacct
   ```
   `public` runs SLURM only as `daham`, only these five read/submit commands —
   `sacct` included so DB-backed history works inside the sandbox.
3. **Path jail** — `validate.in_jail()` confines every file path to `NFS_ROOT`
   (and `$HOME`), resolving symlinks first (tested vs `..`, `/etc/shadow`,
   symlink-escape).
4. **Audit everything** — privileged actions emit events (`job_submit`,
   `container_selected`, `notebook_submit`, `env_build_*`, cancels) to the audit
   daemon; `log_or_block` refuses to submit if it can neither send nor spool.
5. **Filesystem isolation** — job dirs are `0770`, group `gpuusers` (GID 1500 on
   both nodes), so users cannot read each other's outputs while the job user
   `daham` retains group write access on the compute node.

---

## 10. Prebuilt Environments & Containers

**Conda specs** — `envs/specs/*.yml`, all pinned **CUDA 12.8 / PyTorch ≥ 2.7 /
python=3.11**: `llm-finetune`, `llm-serve`, `vision`, `diffusion`,
`data-science`. Install via TUI **Setup → Install a prebuilt environment**
(auto-registers) or
`conda env create -p /shared/envs/<name> -f envs/specs/<name>.yml`.
Currently installed: `pytorch-2.7-test1`.

**Apptainer defs** — `deploy/images/*.def`, base **`ubuntu:22.04`** with
`pip install --no-cache-dir torch … --index-url …/cu128` (the cu128 wheels bundle
the CUDA runtime, so no heavy CUDA base image is needed). `PIP_NO_CACHE_DIR=1`,
apt cleanup, and `rm -rf /opt/conda/pkgs/* /tmp/* /root/.cache` keep the build
sandbox ~4–5 GB.

**Build hygiene** — point Apptainer scratch at NVMe, not the 31 GB `/tmp` tmpfs:
```bash
sudo APPTAINER_TMPDIR=/shared/.apptainer_tmp APPTAINER_CACHEDIR=/shared/.apptainer_cache \
     apptainer build /shared/images/<name>.sif /tmp/<name>.def
```
Policy: build on demand, one at a time, delete when switching (each `.sif`
≈ 9–10 GB). Apptainer **1.5.0** on the GPU host; `build-essential` (gcc 15.2.0)
present → `torch.compile`/Triton available.

---

## 11. Validation Results

Confirmed live at time of writing:

| Check | Result |
|-------|--------|
| SLURM services (ctld/dbd/d/munge/mariadb) | all **active** |
| `iit-gpu-audit`, `iit-gpu-stats` | **active, enabled** |
| Partition `gpu` | **UP**, MaxTime 24 h |
| `gpuusers` GID — login vs GPU host | **1500 == 1500** (matched) |
| `/shared/jobs` ownership / mode | `gpuusers:0770` (group access for `daham` ✓) |
| `gres/gpu` TRES tracked | **yes** (`gres/gpu`, `gpumem`, `gpuutil`) |
| QOS `normal` per-user GPU cap | **`gres/gpu=1` enforced** |
| `sudo -u daham sacct` (tool path) | **OK** (history works for `public`) |
| GPU | RTX 5090, sm_120, 37 °C, idle |
| `/shared` capacity | 1.8 TB, 1% used |
| Test suite | **208 passing** |
| Tool deployment | `/opt/iit-gpu` on `main` |

---

## 12. Quick Operational Reference

| Task | User@node | Command |
|------|-----------|---------|
| Login admin | `slurmadmin@login-node` | `sudo systemctl … slurmctld/slurmdbd/mariadb` |
| GPU host admin | `root-daham@iit-MS-7E06` | `sudo systemctl … slurmd/iit-gpu-stats` |
| Job history | any | `sacct -X --format=JobID,JobName,State,Elapsed` |
| Node state | any | `scontrol show node iit-MS-7E06` |
| QOS | `slurmadmin` | `sudo sacctmgr show qos` |
| Deploy tool | `slurmadmin` | `bash …/deploy/redeploy-igm.sh` (pull → 208 tests → /opt/iit-gpu) |
| Build image | `root-daham` | `sudo APPTAINER_TMPDIR=/shared/.apptainer_tmp apptainer build …` |
| Add cluster user | both (sudo) | matching UID on both nodes + add to `gpuusers` |

**Health snapshot:** `slurmctld / slurmd / slurmdbd / mariadb / munge /
iit-gpu-audit / iit-gpu-stats` all **active**; partition `gpu` **UP**; GPU
**idle 37 °C**; `gpuusers` GID **1500** matched; QOS GPU cap **enforced**;
`/shared` **1% used**; test suite **208 passing**; tool deployed on `main`.

---

*End of M02.*
