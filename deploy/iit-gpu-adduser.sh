#!/usr/bin/env bash
# iit-gpu-adduser.sh — provision a real per-user account across both cluster nodes.
#
# Usage:  sudo iit-gpu-adduser <username> [--dry-run] [--admin]
#
# Creates the user on the login node AND (over SSH) the GPU host with a UID free
# on BOTH nodes, adds them to the gateway group (forced-TUI), registers their
# SLURM association, and makes their /shared area. Group membership is the whole
# mechanism — the TUI itself is never copied per user.
#
# Site config comes from deploy/site.env (or environment). No hardcoded values.
set -euo pipefail

# ── Load site config ───────────────────────────────────────────────────────────
SITE_ENV="${IIT_SITE_ENV:-/opt/iit-gpu/deploy/site.env}"
[ -f "$SITE_ENV" ] && set -a && . "$SITE_ENV" && set +a

GPUUSERS_GROUP="${GPUUSERS_GROUP:-gpuusers}"
ADMIN_GROUP="${ADMIN_GROUP:-gpuadmins}"
NFS_ROOT="${NFS_ROOT:-/shared}"
SLURM_ACCOUNT="${SLURM_ACCOUNT:-default}"
SLURM_QOS="${SLURM_QOS:-normal}"
GPU_HOST_SSH="${GPU_HOST_SSH:-}"          # e.g. root-daham@192.168.122.1 (required)
UID_MIN="${UID_MIN:-2000}"
UID_MAX="${UID_MAX:-60000}"

ok()   { echo "  ✔  $*"; }
warn() { echo "  ⚠  $*"; }
fail() { echo "  ✘  $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }

# ── Args ───────────────────────────────────────────────────────────────────────
USERNAME=""; DRY=0; ADMIN=0
for a in "$@"; do
    case "$a" in
        --dry-run) DRY=1 ;;
        --admin)   ADMIN=1 ;;
        -*)        fail "unknown flag: $a" ;;
        *)         USERNAME="$a" ;;
    esac
done
[ -n "$USERNAME" ] || fail "usage: iit-gpu-adduser <username> [--dry-run] [--admin]"
[[ "$USERNAME" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || fail "invalid username: $USERNAME"
[ -n "$GPU_HOST_SSH" ] || fail "GPU_HOST_SSH not set (in $SITE_ENV or environment)"

run() { if [ "$DRY" = 1 ]; then echo "  [dry-run] $*"; else eval "$@"; fi; }

[ "$(id -u)" = 0 ] || [ "$DRY" = 1 ] || fail "must run as root (sudo)"

# ── 1. Pick a UID free on BOTH nodes ───────────────────────────────────────────
step "Finding a UID free on both nodes (>= $UID_MIN) ..."
local_max=$(getent passwd | awk -F: -v lo="$UID_MIN" -v hi="$UID_MAX" '$3>=lo && $3<=hi {print $3}' | sort -n | tail -1)
remote_max=$(ssh "$GPU_HOST_SSH" "getent passwd | awk -F: -v lo=$UID_MIN -v hi=$UID_MAX '\$3>=lo && \$3<=hi {print \$3}' | sort -n | tail -1")
start=$(( ${local_max:-$((UID_MIN-1))} > ${remote_max:-$((UID_MIN-1))} ? ${local_max:-$((UID_MIN-1))} : ${remote_max:-$((UID_MIN-1))} ))
NEW_UID=$(( start < UID_MIN ? UID_MIN : start + 1 ))
# Ensure truly free on both
while getent passwd "$NEW_UID" >/dev/null 2>&1 || ssh "$GPU_HOST_SSH" "getent passwd $NEW_UID >/dev/null 2>&1"; do
    NEW_UID=$((NEW_UID + 1))
done
ok "Chosen UID/GID: $NEW_UID"

# ── 2. Create on login node ────────────────────────────────────────────────────
step "Creating $USERNAME on login node ..."
run "groupadd -g $NEW_UID $USERNAME 2>/dev/null || true"
run "useradd -u $NEW_UID -g $NEW_UID -m -s /bin/bash $USERNAME 2>/dev/null || true"
run "usermod -aG $GPUUSERS_GROUP $USERNAME"
[ "$ADMIN" = 1 ] && run "getent group $ADMIN_GROUP >/dev/null 2>&1 && usermod -aG $ADMIN_GROUP $USERNAME || true"
ok "login: $USERNAME created"

# ── 3. Create on GPU host (same UID) ───────────────────────────────────────────
step "Creating $USERNAME on GPU host ($GPU_HOST_SSH) ..."
run "ssh $GPU_HOST_SSH \"sudo groupadd -g $NEW_UID $USERNAME 2>/dev/null || true; \
    sudo useradd -u $NEW_UID -g $NEW_UID -m -s /bin/bash $USERNAME 2>/dev/null || true; \
    sudo usermod -aG $GPUUSERS_GROUP $USERNAME\""
ok "GPU host: $USERNAME created (UID $NEW_UID)"

# ── 4. SLURM association ────────────────────────────────────────────────────────
step "Registering SLURM association ..."
run "sacctmgr -i add user $USERNAME account=$SLURM_ACCOUNT qos=$SLURM_QOS 2>/dev/null || true"
ok "SLURM: $USERNAME → account=$SLURM_ACCOUNT qos=$SLURM_QOS"

# ── 5. Shared workspace (private 0700) + ~/shared convenience symlink ──────────
# Create + chown ON THE GPU HOST: it is the NFS server, so root is real there.
# With root_squash on the export, an admin chown over NFS from the login node
# would be squashed to nobody and fail.
step "Creating $NFS_ROOT/$USERNAME on the NFS server (GPU host) ..."
run "ssh $GPU_HOST_SSH \"sudo mkdir -p $NFS_ROOT/$USERNAME && \
    sudo chown $NEW_UID:$NEW_UID $NFS_ROOT/$USERNAME && \
    sudo chmod 0700 $NFS_ROOT/$USERNAME\""
run "ln -sfn $NFS_ROOT/$USERNAME /home/$USERNAME/shared 2>/dev/null || true"
ok "workspace ready (owned $NEW_UID:$NEW_UID, 0700)"

# ── 6. Verify ──────────────────────────────────────────────────────────────────
if [ "$DRY" = 0 ]; then
    step "Verifying ..."
    luid=$(id -u "$USERNAME"); ruid=$(ssh "$GPU_HOST_SSH" "id -u $USERNAME")
    [ "$luid" = "$ruid" ] || fail "UID mismatch: login=$luid gpu=$ruid"
    id "$USERNAME" | grep -q "$GPUUSERS_GROUP" || fail "$USERNAME not in $GPUUSERS_GROUP"
    ok "UID matched ($luid) · in $GPUUSERS_GROUP · forced-TUI applies via group"
fi

echo
echo "Done. Set a password or install an SSH key:"
echo "    sudo passwd $USERNAME            # or: install ~$USERNAME/.ssh/authorized_keys"
echo "$USERNAME will land directly in the TUI on next SSH login."
