#!/usr/bin/env bash
# Live-system test harness — runs on the LOGIN node (has passwordless sudo + slurm tools)
PASS=0; FAIL=0; WARN=0
ok(){ echo "  PASS | $*"; PASS=$((PASS+1)); }
no(){ echo "  FAIL | $*"; FAIL=$((FAIL+1)); }
wn(){ echo "  WARN | $*"; WARN=$((WARN+1)); }
sec(){ echo; echo "== $* =="; }

sec "L1 — Services (login)"
for s in slurmctld slurmdbd mariadb munge iit-gpu-audit; do
  [ "$(systemctl is-active $s)" = active ] && ok "$s active" || no "$s NOT active"
done

sec "L2 — Users / Groups / UID-GID consistency"
for u in daham public slurm; do getent passwd $u >/dev/null && ok "user $u exists" || no "user $u missing"; done
[ "$(id -u daham)" = 1002 ] && ok "daham UID=1002" || no "daham UID wrong"
[ "$(id -u public)" = 1003 ] && ok "public UID=1003" || no "public UID wrong"
GID=$(getent group gpuusers | cut -d: -f3)
[ "$GID" = 1500 ] && ok "gpuusers GID=1500 (login)" || no "gpuusers GID=$GID (expected 1500)"
id daham | grep -q '1500(gpuusers)' && ok "daham in gpuusers" || no "daham NOT in gpuusers"
id public | grep -q '1500(gpuusers)' && ok "public in gpuusers" || no "public NOT in gpuusers"
id slurm | grep -q '1500(gpuusers)' && ok "slurm in gpuusers" || no "slurm NOT in gpuusers"

sec "L3 — NFS / shared storage"
mount | grep -q '/shared type nfs' && ok "/shared NFS-mounted" || no "/shared not NFS"
T=/shared/.harness_$$; if echo x > "$T" 2>/dev/null; then ok "/shared writable from login"; rm -f "$T"; else no "/shared NOT writable"; fi
JM=$(stat -c '%a %G' /shared/jobs 2>/dev/null)
[ "$JM" = "770 gpuusers" ] && ok "/shared/jobs is 0770 gpuusers" || wn "/shared/jobs = '$JM' (expected '770 gpuusers')"

sec "L4 — SLURM config"
grep -q 'AccountingStorageTRES=gres/gpu' /etc/slurm/slurm.conf && ok "AccountingStorageTRES=gres/gpu set" || no "TRES gres/gpu missing"
grep -q 'MaxTime=1-00:00:00' /etc/slurm/slurm.conf && ok "partition MaxTime capped (24h)" || wn "partition MaxTime not 24h"
sinfo -h -o '%a %D %T' | grep -q up && ok "partition gpu UP" || no "partition not UP"
ST=$(sinfo -h -N -o '%T' | head -1); { [ "$ST" = idle ] || [ "$ST" = mix ] || [ "$ST" = allocated ]; } && ok "node state healthy ($ST)" || no "node state=$ST"

sec "L5 — Accounting / QOS"
sudo sacctmgr -n show tres format=Type,Name 2>/dev/null | grep -q 'gres *gpu' && ok "gres/gpu TRES tracked" || no "gres/gpu TRES not tracked"
QN=$(sudo sacctmgr -n -P show qos normal format=MaxTRESPerUser 2>/dev/null)
echo "$QN" | grep -q 'gres/gpu=1' && ok "QOS normal enforces gres/gpu=1" || no "QOS normal cap missing ($QN)"
sudo sacctmgr -n -P show qos long format=Name 2>/dev/null | grep -q long && ok "QOS long exists" || wn "QOS long missing"
sudo -u daham sacct -X -n --format=JobID 2>/dev/null >/dev/null && ok "sacct runs as daham (sudoers ok)" || no "sacct-as-daham failed"

sec "L6 — Security: sudoers scope"
SUDO=/etc/sudoers.d/iit-gpu-gateway
sudo grep -q 'sacct' $SUDO && ok "sacct in gateway sudoers" || no "sacct missing from sudoers"
sudo grep -qE 'NOPASSWD: /usr/bin/sbatch' $SUDO && ok "sbatch allowed via sudoers" || no "sbatch missing"
# Ensure NO blanket ALL command grant
sudo grep -qE '\(daham\) NOPASSWD: ALL' $SUDO && no "DANGER: sudoers grants ALL as daham" || ok "sudoers is command-scoped (no blanket ALL)"
sudo visudo -c -f $SUDO >/dev/null 2>&1 && ok "sudoers syntax valid" || no "sudoers syntax INVALID"

sec "L7 — Forced TUI / sshd"
SSHCONF=$(sudo sshd -T 2>/dev/null | grep -i 'forcecommand' || true)
sudo grep -rq 'ForceCommand /usr/local/bin/iit-gpu-manager' /etc/ssh/ 2>/dev/null && ok "ForceCommand TUI configured for gpuusers" || wn "ForceCommand not found in /etc/ssh"

sec "L8 — Tool: deployed code + unit suite"
[ -f /opt/iit-gpu/iitgpu/slurm.py ] && ok "/opt/iit-gpu deployed" || no "/opt/iit-gpu missing"
grep -q 'def sacct_history' /opt/iit-gpu/iitgpu/slurm.py && ok "deployed slurm.py has sacct_history" || no "sacct_history not deployed"
grep -q 'def render_notebook_sbatch' /opt/iit-gpu/iitgpu/jobs.py && ok "deployed jobs.py has notebook render" || no "notebook render not deployed"
( cd /home/slurmadmin/IIT-Secure-SLURM-Job-Gateway && PYTHONPATH=. python3 -m pytest tests/ -q >/tmp/pyt.txt 2>&1 ) && ok "pytest suite: $(grep -oE '[0-9]+ passed' /tmp/pyt.txt)" || no "pytest FAILED ($(tail -1 /tmp/pyt.txt))"

sec "L9 — Tool: live selftest as public (sandbox env)"
sudo -u public env -i HOME=/home/public USER=public LOGNAME=public \
  PATH="/shared/miniforge3/bin:/usr/local/bin:/usr/bin:/bin" PYTHONPATH="/opt/iit-gpu" \
  CONDA_PREFIX_SHARED="/shared/miniforge3" NFS_ROOT="/shared" DEMO_MODE=1 \
  /usr/bin/python3 -m iitgpu --selftest 2>&1 | grep -q 'All checks passed' && ok "public selftest passes" || no "public selftest failed"

sec "L10 — Tool functions (live, real SLURM)"
PYTHONPATH=/opt/iit-gpu python3 - << 'PY' 2>&1
import sys
try:
    from iitgpu.config import load_config
    from iitgpu import slurm
    cfg = load_config()
    print(f"  PASS | config.sacct_enabled auto-detected = {cfg.sacct_enabled}")
    parts = slurm.get_partitions()
    print(f"  {'PASS' if parts else 'FAIL'} | get_partitions() -> {len(parts)} partition(s)")
    ns = slurm.get_node_stats()
    print(f"  {'PASS' if ns else 'FAIL'} | get_node_stats() -> state={getattr(ns,'state','?')} gpu_total={getattr(ns,'gpu_total','?')}")
    q = slurm.queue()
    print(f"  PASS | queue() -> {len(q)} job(s) (no exception)")
    h = slurm.sacct_history(limit=5)
    print(f"  PASS | sacct_history() -> {len(h)} row(s) (no exception)")
except Exception as e:
    print(f"  FAIL | tool function raised: {e!r}")
    sys.exit(1)
PY

sec "L11 — render_sbatch branches (conda/container/notebook)"
PYTHONPATH=/opt/iit-gpu python3 - << 'PY' 2>&1
from iitgpu.jobs import JobSpec, render_sbatch, render_notebook_sbatch
import tempfile, os
d = tempfile.mkdtemp()
# conda
s = JobSpec(job_name="t",partition="gpu",gpus=1,cpus=4,mem_gb=8,time_limit="01:00:00",run_command="python x.py",conda_env="/shared/envs/e")
sc = render_sbatch(s,d); print("  PASS | conda render has activate" if "conda activate" in sc and "apptainer" not in sc else "  FAIL | conda branch")
# container
s2 = JobSpec(job_name="t",partition="gpu",gpus=1,cpus=4,mem_gb=8,time_limit="01:00:00",run_command="python x.py",container_image="/shared/images/llm-finetune.sif")
sc2 = render_sbatch(s2,d); print("  PASS | container render uses apptainer --nv, no conda" if "apptainer exec --nv" in sc2 and "conda activate" not in sc2 else "  FAIL | container branch")
# notebook
s3 = JobSpec(job_name="nb",partition="gpu",gpus=1,cpus=8,mem_gb=32,time_limit="08:00:00",run_command="")
sc3 = render_notebook_sbatch(s3,d,port=8888); print("  PASS | notebook render has jupyter+tunnel+token" if all(x in sc3 for x in ["jupyter lab","127.0.0.1","secrets.token_hex","ssh -p 2225"]) else "  FAIL | notebook branch")
PY

sec "L12 — Security: path jail"
PYTHONPATH=/opt/iit-gpu NFS_ROOT=/shared python3 - << 'PY' 2>&1
from iitgpu.validate import in_jail
checks = [("/shared/daham/x.py",True),("/etc/shadow",False),("/shared/../etc/passwd",False),("/shared/jobs/a",True)]
allok=True
for p,exp in checks:
    got=in_jail(p)
    print(f"  {'PASS' if got==exp else 'FAIL'} | in_jail({p}) = {got} (expect {exp})")
    allok = allok and got==exp
PY

echo; echo "==================== SUMMARY ===================="
echo "  PASS=$PASS  FAIL=$FAIL  WARN=$WARN"
