# bootstrap-install.md — One-time install of the canonical clone

> Run once on the **login node** as an admin. Establishes `/opt/iit-gpu` as the
> single live checkout that every `gpuusers` member runs via the launcher.

```bash
# 1. Canonical live clone — readable by all gpuusers, pull-to-update by admin only
sudo git clone https://github.com/DahamDissanayake/IIT-Secure-SLURM-Job-Gateway.git /opt/iit-gpu
sudo chown -R slurmadmin:gpuusers /opt/iit-gpu
sudo chmod -R 0750 /opt/iit-gpu

# 2. Site configuration (git-ignored)
sudo -u slurmadmin cp /opt/iit-gpu/deploy/site.env.example /opt/iit-gpu/deploy/site.env
sudo -u slurmadmin nano /opt/iit-gpu/deploy/site.env     # edit for your cluster

# 3. Launcher — the single integration point (PYTHONPATH points at the clone)
sudo tee /usr/local/bin/iit-gpu-manager >/dev/null <<'LAUNCHER'
#!/bin/bash
exec env -i \
    HOME="$HOME" USER="$USER" LOGNAME="$LOGNAME" \
    PATH="/shared/miniforge3/bin:/usr/local/bin:/usr/bin:/bin" \
    SSH_CLIENT="${SSH_CLIENT:-}" TERM="${TERM:-xterm}" \
    PYTHONPATH="/opt/iit-gpu" \
    IIT_SITE_ENV="/opt/iit-gpu/deploy/site.env" \
    /usr/bin/python3 -m iitgpu
LAUNCHER
sudo chmod 0755 /usr/local/bin/iit-gpu-manager

# 4. Forced TUI for the gateway group (sshd)
#    /etc/ssh/sshd_config.d/iit-gpu-gateway.conf:
#      Match Group gpuusers
#          ForceCommand /usr/local/bin/iit-gpu-manager
#          (+ the no-forwarding hardening from M02 §9)
#    Adding a user to gpuusers is then all it takes to grant the tool.

# 5. Update for everyone, any time:
cd /opt/iit-gpu && git pull --ff-only && python3 -m pytest tests/ -q
```
