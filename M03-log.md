# M03 — IIT Secure SLURM Job Gateway: Full SLURM Frontend Rebuild

**Date:** 2026-05-31
**Author:** Daham Dissanayake
**Scope:** The Phase 0–8 rebuild that turned the single-purpose job gateway into a
**complete SLURM frontend** with **real per-user identity**, open-source-ready
code, and full submit / monitor / accounting / files / notebooks / admin
coverage. Documents the Linux changes, the SLURM changes, and the tool itself.
**Builds on:** [M01-log.md](./M01-log.md), [M02-log.md](./M02-log.md).
**Branches:** `feature/phase0-opensource` … `feature/phase8-polish` (one per
phase, cumulative; merged to `main` by the maintainer). **305 tests passing.**

---

## 0. Executive summary

| Before (M02) | After (M03) |
|--------------|-------------|
| One shared `public` login; jobs ran as `daham` via `sudo -u daham` | **Per-user identity** — each person has their own Linux+SLURM account on both nodes; jobs run as themselves; `sacct`/fairshare/quotas/audit attribute correctly |
| Hardcoded site values (IPs, ports, GIDs) in code | **Site-agnostic** — everything in `config.py` + git-ignored `deploy/site.env`; `main` is releasable; MIT licensed |
| Deploy via `rsync --delete` to `/opt/iit-gpu` | **Single canonical clone** at `/opt/iit-gpu`; update = `git pull --ff-only` |
| Submit: script/notebook/container | + **job arrays, dependencies, interactive `srun --pty`** |
| Monitor: queue/cancel/log tail | + **hold/release/requeue, `seff`, live log follow, history filters** |
| No accounting UI | **Usage area** — GPU/CPU-hours per user, fairshare, sreport |
| Upload only | **Two-pane file manager** + env/container management |
| Notebook only | + **TensorBoard** + **running-services** view with teardown |
| No admin tooling | **Admin panel** (gated) — drain/resume, user provision/offboard, audit viewer, cluster usage |
| 161 → 217 tests | **305 tests** |

The live cluster was cut over to per-user identity during this work: **`public`
now runs as `public`, not `daham`** (verified job 107 → `User=public`).

---

## 1. The Linux layer

### 1.1 Users & groups (end state)

| Principal | UID/GID | Nodes | Role |
|-----------|---------|-------|------|
| `gpuusers` | **GID 1500** | both | gateway access group (forced-TUI + job-dir group) |
| `gpuadmins` | **GID 1501** | both | admin group (unlocks the admin panel + admin sudoers) |
| per-user accounts (`tuser`, …) | UID ≥ 2000, **matched on both nodes** | both | real identities; members of `gpuusers` |
| `daham` | 1002 | both | legacy shared account (now also a `gpuadmins` member) |
| `public` | 1003 | both | demoted to a testing/demo account; runs as itself |
| `gpusync` | service | login | audit daemon |

**Why matched UIDs/GIDs matter:** NFS uses `sec=sys` (numeric IDs) and SLURM
hands `slurmstepd` a numeric UID. An account that isn't identical on both nodes
breaks job execution and file ownership.

### 1.2 Onboarding mechanism (the whole trick is group membership)

`deploy/iit-gpu-adduser.sh` (+ interactive `addUser.sh` wrapper, + `iit-gpu-deluser.sh`):

1. Picks a UID **free on both nodes** (range from `site.env`).
2. `useradd` on the login VM **and** over SSH on the GPU host with that UID.
3. Adds the user to `gpuusers` (and `gpuadmins` with `--admin`).
4. Registers the SLURM association (`sacctmgr add user … account=… qos=…`).
5. Creates `/shared/<user>` (0700) **on the NFS server** (root_squash-safe).
6. Verifies the UID matches on both nodes and the group is set.

Because sshd has `Match Group gpuusers → ForceCommand iit-gpu-manager`, **adding a
user to `gpuusers` is all it takes** to give them the tool — the TUI is never
copied per user.

**Provisioning plumbing (one-time, FRONTEND-RUNBOOK §1.0a):** login-node root
holds an SSH key authorized for the GPU host, and the GPU host grants `root-daham`
*scoped passwordless sudo* (`useradd/userdel/groupadd/groupdel/usermod/mkdir/
chown/chmod`) so the scripts run non-interactively. Verified live: `addUser.sh`
provisioned `demo1` (UID 2001, both nodes), it ran a job as `demo1`, then
`iit-gpu-deluser` removed it from both nodes + `/shared`.

### 1.3 NFS hardening

`no_root_squash → root_squash`. Consequence discovered + handled: admin file ops
on `/shared` (chown of a new user's dir, deleting an offboarded dir) must run on
the **GPU host** (the NFS server, where root is real), not over NFS from the
login node. Both onboarding scripts do this server-side.

### 1.4 Filesystem ownership

Job dirs are `0770`, group `gpuusers`. In per-user mode the **owner is the real
user** (`make_job_folder` chowns the group to `gpuusers` via the user's own
membership). Users can't read each other's outputs; `slurmstepd` (running as the
user) writes cleanly.

---

## 2. The SLURM layer

### 2.1 Cluster hardening (Phase 0, applied live)

| Change | Detail |
|--------|--------|
| **CPUs 16 → 32** | Host is an i9-14900K (hybrid 8 P + 16 E cores = 32 logical). slurmd's hwloc miscounts as 16, so `SlurmdParameters=config_overrides` makes SLURM trust the configured geometry |
| **RealMemory = 62000** | Kept ≥ 61440 so the default 60 G `train` jobs still schedule; `ConstrainRAMSpace` prevents overrun |
| **cgroup ConstrainCores + ConstrainRAMSpace = yes** | Per-job CPU/RAM isolation; verified a GPU job is constrained yet still sees the RTX 5090 |
| **ConstrainDevices = no (kept)** | Enabling needs a per-job NVIDIA eBPF allowlist (M01); risks hiding the GPU |
| **SlurmdDebug=info, log off NFS** | `/var/log/slurm/slurmd.log` |
| **Fairshare** | `PriorityType=priority/multifactor` + fairshare/age/QOS weights |

### 2.2 Per-user identity cutover (Phases 1, live)

- `slurm.py` no longer hardcodes `sudo -u daham`. A gated `_gateway_prefix()` /
  `_effective_user()` runs SLURM **as the logged-in user** when
  `GATEWAY_SHARED_USER=0` (the default), or as the shared account when `=1`
  (legacy rollback).
- The live `/opt/iit-gpu/deploy/site.env` was set to `GATEWAY_SHARED_USER=0` and
  the per-user code deployed; **`public` now attributes to `public`** in sacct.
- Post-cutover sudoers (`deploy/sudoers-gateway-admin`) scopes elevated SLURM ops
  to `%gpuadmins`; normal users need no sudo at all.

### 2.3 Accounting & QOS (from M02, now surfaced in the tool)

`slurmdbd` + `gres/gpu` TRES + QOS `normal` (`MaxTRESPerUser=gres/gpu=1`, 8 h) /
`long`. The tool reads it via `sacct`/`sreport`/`sshare`.

---

## 3. The tool — phase by phase

| Phase | Module(s) | What it adds |
|-------|-----------|--------------|
| **0** | `config.py`, `redeploy-igm.sh`, `LICENSE`, `CONTRIBUTING.md`, `site.env.example` | site.env layering + knobs; git-pull deploy; MIT; CI guard `test_no_hardcoded_site_values` |
| **1** | `iit-gpu-adduser/deluser.sh`, `addUser.sh`, `config.is_admin` | per-user onboarding on both nodes; role detection |
| **2** | `jobs.py`, `validate.py`, `wizard.py` | job **arrays**, **dependencies**, **interactive `srun --pty`**, QOS validation |
| **3** | `slurm.py`, `monitor.py` | **hold/release/requeue**, `scontrol show job` + **`seff`**, **live log follow**, **history filters** |
| **4** | `accounting.py` | GPU/CPU-hours per user, **fairshare**, **sreport** — “Usage & accounting” area |
| **5** | `files.py`, `containers.py`, `envbuilder.py`, `envs.py` | jailed **file manager** (mkdir/rename/delete/copy/disk-usage), env+container **delete**, env manager |
| **6** | `notebooks.py`, `jobs.py` | **TensorBoard** launch; **“My running services”** view with tunnel hints + one-key teardown |
| **7** | `admin.py` | **admin panel** (gated): drain/resume, user provision/offboard, audit viewer, QOS, cluster usage |
| **8** | `notify.py`, `jobs.py`, `wizard.py` | **completion notifications** (SLURM `--mail-type` if MTA, else in-TUI poller); quota surfaced |

### 3.1 Security invariants preserved throughout

Path jail (`validate.in_jail`) wraps every new filesystem path (every mutating
`files.py` op is tested to reject `..` and `/etc`). Every privileged action is
audited. The forced-TUI and the command-scoped sudoers remain; the admin panel
is gated by group membership (`public` = not admin, verified).

---

## 4. Open-source & deployment model

- **One canonical clone** at `/opt/iit-gpu`, owned `slurmadmin:gpuusers` 0750.
  Every user's launcher sets `PYTHONPATH=/opt/iit-gpu` + `IIT_SITE_ENV=…/site.env`.
- **Update for everyone:** `cd /opt/iit-gpu && git pull --ff-only && pytest`.
- **Site-agnostic:** `grep -rnE '192\.168\.|10\.35\.|:2225|sudo -u daham' iitgpu/`
  is clean and CI-guarded. A new cluster only edits `deploy/site.env`.
- **Secrets** (munge key, DB pass, SSH keys, `site.env`) are git-ignored.

---

## 5. What changed on the live box (summary)

- slurm.conf: 32 CPUs (config_overrides), RealMemory 62000, fairshare, info logs.
- cgroup.conf: Cores+RAM constrained.
- NFS: root_squash.
- Groups: `gpuusers` (1500), **new `gpuadmins` (1501)** with `daham`.
- Users: `tuser` (UID 2000) provisioned as a real per-user account.
- Identity: `/opt/iit-gpu` on the per-user code; `GATEWAY_SHARED_USER=0`;
  **`public` runs as `public`.**
- Sudoers: provisioning (`/etc/sudoers.d/iit-gpu-provisioning`) + admin
  (`/etc/sudoers.d/iit-gpu-admin`) installed; scripts in `/usr/local/bin`.
- Launcher: reads `IIT_SITE_ENV`.

**Rollback:** set `GATEWAY_SHARED_USER=1` in `/opt/iit-gpu/deploy/site.env`.

---

## 6. Verification

- **305 unit tests** green (`PYTHONPATH=. pytest tests/ -q`).
- Live, end-to-end: per-user submit (`public→public`, `tuser→tuser`,
  `demo1→demo1`); **job array** (103_0/1/2) + **dependency** (104 waited then ran);
  **hold→release** (109); GPU jobs on the RTX 5090 (sm_120) under cgroup limits;
  `addUser.sh`/`deluser` full lifecycle; admin gate (`daham`=admin, `public`=not).

---

## 7. Remaining for the maintainer

1. **Merge** `feature/phase0…8` to `main` (PRs), then `git pull` on `/opt/iit-gpu`
   so `/opt` tracks `main` instead of the temporary `deployed` branch.
2. **Provision real users** with `addUser.sh`; retire reliance on `public`.
3. Optional: enable `ConstrainDevices` once a per-job NVIDIA eBPF allowlist is in
   place; add an MTA for email notifications; XFS project quotas on `/shared`.

---

*End of M03.*
