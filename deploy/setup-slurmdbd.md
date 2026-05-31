# setup-slurmdbd.md — Enable SLURM Accounting via slurmdbd

> **[LOGIN] and [GPU-HOST] steps** — run manually. Restart slurmctld/slurmd when done.
> This enables `sacct` for job history in the dashboard instead of file scanning.

---

## 1. Install MariaDB on the Login VM

```bash
# [LOGIN] run as slurmadmin (with sudo)
sudo apt-get install -y mariadb-server

# Secure and start
sudo systemctl enable --now mariadb
sudo mysql_secure_installation   # set root password, remove anon users
```

## 2. Create the SLURM Accounting Database

```bash
# [LOGIN]
sudo mysql -u root << 'SQL'
CREATE DATABASE IF NOT EXISTS slurm_acct_db;
CREATE USER IF NOT EXISTS 'slurm'@'localhost' IDENTIFIED BY 'slurmdbpass';
GRANT ALL ON slurm_acct_db.* TO 'slurm'@'localhost';
FLUSH PRIVILEGES;
SQL
```

> Change `slurmdbpass` to a strong password and update it in `slurmdbd.conf`.

## 3. Install slurmdbd

```bash
# [LOGIN]
sudo apt-get install -y slurmdbd

# Copy the template (update DbPass to match step 2)
sudo cp /home/slurmadmin/IIT-Secure-SLURM-Job-Gateway/deploy/slurmdbd.conf /etc/slurm/slurmdbd.conf
sudo chown slurm:slurm /etc/slurm/slurmdbd.conf
sudo chmod 0600 /etc/slurm/slurmdbd.conf

sudo systemctl enable --now slurmdbd
sudo systemctl status slurmdbd
```

## 4. Update slurm.conf on Both Nodes

Add these lines to `/etc/slurm/slurm.conf` on **both** the login node and GPU host:

```ini
AccountingStorageType=accounting_storage/slurmdbd
AccountingStorageHost=login-node
AccountingStoragePort=6819
JobAcctGatherType=jobacct_gather/linux
JobAcctGatherFrequency=30
```

**Replace** `PartitionName=gpu ... MaxTime=INFINITE` with:

```ini
PartitionName=gpu Nodes=iit-MS-7E06 Default=YES MaxTime=1-00:00:00 State=UP
```

Sync to GPU host:

```bash
# [LOGIN]
sudo scp /etc/slurm/slurm.conf root@192.168.122.1:/etc/slurm/slurm.conf
```

## 5. Add a QOS + Per-User GPU Limit

```bash
# [LOGIN] — run after slurmdbd is confirmed running
sudo sacctmgr -i add cluster iit
sudo sacctmgr -i add account default description="Default account" Organization=IIT
sudo sacctmgr -i add user daham account=default
sudo sacctmgr -i add user public account=default

# Default QOS: max 8 h wall, 1 GPU per user
sudo sacctmgr -i add qos normal \
    MaxWallDurationPerJob=08:00:00 \
    MaxTRESPerUser="gres/gpu=1"

# Long QOS for admin (no GPU cap, 7-day wall)
sudo sacctmgr -i add qos long \
    MaxWallDurationPerJob=7-00:00:00

# Apply default QOS to partition
sudo sacctmgr -i modify qos normal set Flags=DenyOnLimit
```

> Assign `daham` to the `long` QOS when running extended experiments:
> `sudo sacctmgr modify user daham set DefaultQOS=long`

## 6. Restart SLURM Services

```bash
# [LOGIN]
sudo systemctl restart slurmctld
sudo scontrol update nodename=iit-MS-7E06 state=resume

# [GPU-HOST]
sudo systemctl restart slurmd
```

## 7. Verify

```bash
# [LOGIN] — submit a quick test job, then:
sacct --user=daham --format=JobID,JobName,State,Elapsed -X | head -10
# Expected: rows showing COMPLETED jobs

# Enable sacct in the TUI (auto-detected when sacct is on PATH):
# export SACCT_ENABLED=1   # or leave as "auto" — it probes sacct automatically
```

## Rollback

If slurmdbd causes issues, set `SACCT_ENABLED=0` in the launcher:

```bash
# [LOGIN] — edit /usr/local/bin/iit-gpu-manager
# Change: SACCT_ENABLED=auto  →  SACCT_ENABLED=0
# This forces file-scan fallback without changing any other config.
```
