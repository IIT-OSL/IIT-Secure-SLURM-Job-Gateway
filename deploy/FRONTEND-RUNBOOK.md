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

### 1.1 [LOGIN] Reduce the gateway sudoers to admin-only
Once users run SLURM as themselves (`GATEWAY_SHARED_USER=0`), the broad
`%gpuusers ALL=(daham) NOPASSWD: ...` rule is no longer needed for normal jobs.
Replace `/etc/sudoers.d/iit-gpu-gateway` with an admin-only scope (node
drain/resume + provisioning). Validate with `visudo -c` before saving.

### 1.2 Cutover
Set `GATEWAY_SHARED_USER=0` in `/opt/iit-gpu/deploy/site.env` only after every
active user has a real account (Phase 1 provisioning). `public` keeps working as
long as the flag is `1`.
