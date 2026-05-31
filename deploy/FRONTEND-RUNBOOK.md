# FRONTEND-RUNBOOK.md — Manual steps for the SLURM frontend rebuild

> Consolidates every `[LOGIN]` and `[GPU-HOST]` manual/root step for the
> frontend phases. Login-node steps run as `slurmadmin` (passwordless sudo);
> GPU-host steps run as `root-daham` (sudo). `slurm.conf`/`cgroup.conf` must stay
> **byte-identical on both nodes** — edit on login, copy to GPU host, restart.

---

## Phase 0 — Cluster hardening + de-hardcode

### 0.1 [LOGIN] slurm.conf — declare full CPUs, sane memory, fairshare, quieter logs

The GPU host is an Intel i9-14900K: 32 logical CPUs but a *hybrid* layout
(8 P-cores x2 threads + 16 E-cores x1 thread). slurmd's hwloc miscounts this as
16, so configuring CPUs=32 alone marks the node INVALID_REG ("Low
socket*core*thread count"). Fix: add SlurmdParameters=config_overrides so SLURM
trusts the configured geometry.

RealMemory: the default train task requests --mem=60G (61440 MB), so RealMemory
must stay >= 61440. Set 62000 (~1 GB headroom; ConstrainRAMSpace=yes prevents
per-job overrun, so tight headroom is safe).

Edit /etc/slurm/slurm.conf (APPLIED values):
```ini
SlurmdParameters=config_overrides
NodeName=iit-MS-7E06 NodeAddr=192.168.122.1 CPUs=32 RealMemory=62000 Gres=gpu:1 State=UNKNOWN
SlurmdDebug=info
SlurmdLogFile=/var/log/slurm/slurmd.log
PriorityType=priority/multifactor
PriorityWeightFairshare=100000
PriorityWeightAge=1000
PriorityWeightQOS=10000
```

### 0.2 [LOGIN] cgroup.conf — enforce CPU/RAM (and optionally devices)
```ini
CgroupPlugin=autodetect
ConstrainCores=yes
ConstrainRAMSpace=yes
ConstrainSwapSpace=no
ConstrainDevices=no       # KEPT no — see 0.4 (GPU works without it; enabling risks hiding it)
```

### 0.3 [LOGIN→GPU-HOST] sync + restart
```bash
# [LOGIN]
sudo scp /etc/slurm/slurm.conf  root-daham@192.168.122.1:/tmp/slurm.conf.new
sudo scp /etc/slurm/cgroup.conf root-daham@192.168.122.1:/tmp/cgroup.conf.new
sudo systemctl restart slurmctld

# [GPU-HOST]
sudo mkdir -p /var/log/slurm && sudo chown slurm:slurm /var/log/slurm
sudo cp /tmp/slurm.conf.new  /etc/slurm/slurm.conf
sudo cp /tmp/cgroup.conf.new /etc/slurm/cgroup.conf
sudo chown slurm:slurm /etc/slurm/slurm.conf /etc/slurm/cgroup.conf
sudo systemctl restart slurmd

# [LOGIN] confirm
scontrol show node iit-MS-7E06 | grep -oE 'CPUTot=[0-9]+|State=[A-Z]+'
sudo scontrol update nodename=iit-MS-7E06 state=resume   # if drained
```

### 0.4 [GPU-HOST] Verify ConstrainDevices doesn't hide the GPU
cgroup v2 device control needs the NVIDIA devices visible to jobs. After 0.3,
submit a GPU job and confirm `nvidia-smi` still sees the 5090 inside the job.
If the GPU disappears, revert `ConstrainDevices=yes → no` on both nodes and
restart — the per-job eBPF device allowlist needs extra kernel config (see M01).

### 0.5 [GPU-HOST] NFS root_squash — APPLIED & verified
`/etc/exports`:
```
/mnt/nvme_storage/shared 192.168.122.0/24(rw,sync,no_subtree_check,root_squash)
```
```bash
sudo exportfs -ra
```
Then verify `/shared` is still readable/writable by jobs and the stats service.
Keep `no_root_squash` only if a documented workflow needs root writes over NFS.

---

## Phase 1 — Per-user identity (see also deploy/iit-gpu-adduser.sh)

### 1.0a [LOGIN+GPU-HOST] Provisioning plumbing (one-time, required for addUser.sh)

The onboarding scripts run on the login node and SSH to the GPU host to create
the matching account. So login-node root needs key access to the GPU host, and
the GPU-side provisioning commands must run without an interactive password:

```bash
# [LOGIN] give root an SSH key
sudo ssh-keygen -t ed25519 -N "" -f /root/.ssh/id_ed25519 -C iit-gpu-provisioning
sudo cat /root/.ssh/id_ed25519.pub        # copy this

# [GPU-HOST] authorize it for the GPU_HOST_SSH user (e.g. root-daham)
#   append the pubkey to ~root-daham/.ssh/authorized_keys (0600)
# [GPU-HOST] scoped passwordless sudo so adduser/deluser work non-interactively:
sudo tee /etc/sudoers.d/iit-gpu-provisioning >/dev/null <<'SUDO'
root-daham ALL=(root) NOPASSWD: /usr/sbin/useradd, /usr/sbin/userdel, \
    /usr/sbin/groupadd, /usr/sbin/groupdel, /usr/sbin/usermod, \
    /bin/mkdir, /bin/chown, /bin/chmod
SUDO
sudo chmod 0440 /etc/sudoers.d/iit-gpu-provisioning && sudo visudo -c -f /etc/sudoers.d/iit-gpu-provisioning
```
Verify: `sudo ssh root-daham@<gpu-host> 'sudo -n groupadd -g 59999 _t && sudo -n groupdel _t && echo ok'`.

### 1.0 Provision users
```bash
# [LOGIN] (GPU host must allow the SSH target passwordless sudo, or run the
# GPU-host useradd lines manually — see the script's step 3).
sudo IIT_SITE_ENV=/opt/iit-gpu/deploy/site.env iit-gpu-adduser alice
sudo passwd alice           # or install ~alice/.ssh/authorized_keys
# alice now lands in the TUI on next SSH login (gpuusers triggers ForceCommand).
```
The onboarding allocates a UID free on **both** nodes, creates the account on
each, adds it to `gpuusers`, registers the SLURM association, and makes
`/shared/alice` (0700) **on the NFS server** (root_squash-safe). `tuser` was
provisioned this way and verified: `sacct -X --format=JobID,User` shows
`User=tuser`.

### 1.1 CUTOVER ORDER (do not break `public`)
The production `/opt/iit-gpu` must be running the per-user code BEFORE the flag
flip / sudoers reduction, or `public`'s `sudo -u daham` path breaks. Sequence:
```
1. Merge feature/phase1-identity → main (maintainer).
2. cd /opt/iit-gpu && git pull --ff-only && python3 -m pytest tests/ -q
3. Set GATEWAY_SHARED_USER=0 in /opt/iit-gpu/deploy/site.env
   (public has a SLURM association, so it keeps working — now as itself).
4. Swap the sudoers rule to the admin-only scope:
     sudo cp /opt/iit-gpu/deploy/sudoers-gateway-admin /etc/sudoers.d/iit-gpu-gateway
     sudo visudo -c -f /etc/sudoers.d/iit-gpu-gateway
   (edit %gpuadmins to match your ADMIN_GROUP).
5. Verify: public and a provisioned user each submit a job; sacct shows their
   own usernames. Rollback = set GATEWAY_SHARED_USER=1 and restore the old
   sudoers file (kept as deploy/sudoers-gateway).
