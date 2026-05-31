#!/usr/bin/env bash
# redeploy-host.sh — Run from the GPU host (iit-MS-7E06) as root-daham.
#
# This machine has no git. All git/deploy work runs on the login node
# (192.168.122.10) via SSH. This script:
#   1. Triggers the login-node deploy (git pull → tests → /opt/iit-gpu → service restart)
#   2. Ensures the iit-gpu-stats systemd service is active on this host
set -euo pipefail

LOGIN="slurmadmin@192.168.122.10"
STATS_JSON="/shared/.gpu_stats.json"
WRITER_DEST="/usr/local/bin/iit-gpu-stats-writer"
SERVICE_SRC="/tmp/iit-gpu-stats.service"   # synced from login node via SSH before this script

ok()   { echo "  ✔  $*"; }
warn() { echo "  ⚠  $*"; }
fail() { echo "  ✘  $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }

# ── 1. Deploy on login node ───────────────────────────────────────────────────
step "Running deploy on login node (192.168.122.10)..."
ssh "$LOGIN" "bash /home/slurmadmin/IIT-Secure-SLURM-Job-Gateway/deploy/redeploy-igm.sh" \
    || fail "Login-node deploy failed"

# ── 2. Sync service files from login node ────────────────────────────────────
step "Syncing stats writer from login node..."
scp "$LOGIN:/home/slurmadmin/IIT-Secure-SLURM-Job-Gateway/deploy/iit-gpu-stats-writer" \
    "$WRITER_DEST"
chmod +x "$WRITER_DEST"

scp "$LOGIN:/home/slurmadmin/IIT-Secure-SLURM-Job-Gateway/deploy/iit-gpu-stats.service" \
    /etc/systemd/system/iit-gpu-stats.service

ok "Files synced"

# ── 3. Install / reload systemd service ──────────────────────────────────────
step "Installing iit-gpu-stats systemd service..."

# [GPU-HOST] these commands require root — emit as labeled block if not root
if [ "$(id -u)" -ne 0 ]; then
    echo
    echo "  ┌─────────────────────────────────────────────────────────────────┐"
    echo "  │  [GPU-HOST] run manually as root to install/restart service:    │"
    echo "  │    sudo systemctl daemon-reload                                  │"
    echo "  │    sudo systemctl enable --now iit-gpu-stats                    │"
    echo "  │    sudo systemctl status iit-gpu-stats                          │"
    echo "  └─────────────────────────────────────────────────────────────────┘"
    echo
    warn "Run the above as root to activate the systemd service."
else
    systemctl daemon-reload
    systemctl enable iit-gpu-stats
    systemctl restart iit-gpu-stats
    sleep 3
    if systemctl is-active --quiet iit-gpu-stats; then
        ok "iit-gpu-stats is active ($(systemctl show iit-gpu-stats --property=MainPID --value))"
    else
        warn "Service not active — check: journalctl -u iit-gpu-stats -n 30"
    fi
fi

# ── 4. Verify stats file is fresh ────────────────────────────────────────────
step "Checking stats file freshness..."
sleep 5
if [ -f "$STATS_JSON" ]; then
    AGE=$(( $(date +%s) - $(stat --format=%Y "$STATS_JSON") ))
    if [ "$AGE" -lt 15 ]; then
        ok "Stats file fresh (age ${AGE}s)"
    else
        warn "Stats file stale (age ${AGE}s) — service may not be running"
    fi
else
    warn "Stats file not found yet — service may still be starting"
fi

echo
ok "Done."
