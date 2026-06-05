#!/usr/bin/env bash
# iit-gpu-adduser.sh — provision a real per-user account across both cluster nodes.
#
# Usage:  sudo iit-gpu-adduser <username> [--dry-run] [--admin] [--shell-user]
#
# Three user types:
#   (default)     → gpuusers; forced-TUI via ForceCommand; audited
#   --admin       → gpuusers + gpuadmins; forced-TUI + admin panel; audited
#   --shell-user  → NO gpuusers / NO gpuadmins; real bash shell; NOT audited
#                   Still gets a SLURM association and /shared/users/<user>.
#
# --admin and --shell-user are mutually exclusive.
#
# Creates the user on the login node AND (over SSH) the GPU host with a UID free
# on BOTH nodes, registers their SLURM association, and makes their /shared area.
# Group membership is the whole mechanism — the TUI itself is never copied per user.
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
USERNAME=""; DRY=0; ADMIN=0; SHELL_USER=0
for a in "$@"; do
    case "$a" in
        --dry-run)    DRY=1 ;;
        --admin)      ADMIN=1 ;;
        --shell-user) SHELL_USER=1 ;;
        -*)           fail "unknown flag: $a" ;;
        *)            USERNAME="$a" ;;
    esac
done
[ -n "$USERNAME" ] || fail "usage: iit-gpu-adduser <username> [--dry-run] [--admin|--shell-user]"
[[ "$USERNAME" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || fail "invalid username: $USERNAME"
[ "$ADMIN" = 1 ] && [ "$SHELL_USER" = 1 ] && fail "--admin and --shell-user are mutually exclusive"
[ -n "$GPU_HOST_SSH" ] || fail "GPU_HOST_SSH not set (in $SITE_ENV or environment)"

run() { if [ "$DRY" = 1 ]; then echo "  [dry-run] $*"; else eval "$@"; fi; }

[ "$(id -u)" = 0 ] || [ "$DRY" = 1 ] || fail "must run as root (sudo)"

if [ "$SHELL_USER" = 1 ]; then
    step "Shell user — will NOT be added to $GPUUSERS_GROUP or $ADMIN_GROUP"
    warn "Activity will NOT be audited by the gateway tool"
fi

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
# A pre-existing home from an earlier incarnation of this name can be left owned
# by a stale UID (useradd -m won't re-chown an existing dir). The user then can't
# read their own dotfiles -- e.g. ~/.config/conda/.condarc -- so `conda activate`
# crashes and notebook/job env activation fails. Force home ownership to match.
run "chown -R $NEW_UID:$NEW_UID /home/$USERNAME"
if [ "$SHELL_USER" = 0 ]; then
    run "usermod -aG $GPUUSERS_GROUP $USERNAME"
fi
[ "$ADMIN" = 1 ] && run "getent group $ADMIN_GROUP >/dev/null 2>&1 && usermod -aG $ADMIN_GROUP $USERNAME || true"
ok "login: $USERNAME created"

# ── 3. Create on GPU host (same UID) ───────────────────────────────────────────
step "Creating $USERNAME on GPU host ($GPU_HOST_SSH) ..."
if [ "$SHELL_USER" = 0 ]; then
    run "ssh $GPU_HOST_SSH \"sudo groupadd -g $NEW_UID $USERNAME 2>/dev/null || true; \
        sudo useradd -u $NEW_UID -g $NEW_UID -m -s /bin/bash $USERNAME 2>/dev/null || true; \
        sudo chown -R $NEW_UID:$NEW_UID /home/$USERNAME; \
        sudo usermod -aG $GPUUSERS_GROUP $USERNAME\""
else
    run "ssh $GPU_HOST_SSH \"sudo groupadd -g $NEW_UID $USERNAME 2>/dev/null || true; \
        sudo useradd -u $NEW_UID -g $NEW_UID -m -s /bin/bash $USERNAME 2>/dev/null || true; \
        sudo chown -R $NEW_UID:$NEW_UID /home/$USERNAME\""
fi
ok "GPU host: $USERNAME created (UID $NEW_UID)"

# ── 4. SLURM association ────────────────────────────────────────────────────────
step "Registering SLURM association ..."
run "sacctmgr -i add user $USERNAME account=$SLURM_ACCOUNT qos=$SLURM_QOS 2>/dev/null || true"
ok "SLURM: $USERNAME → account=$SLURM_ACCOUNT qos=$SLURM_QOS"

# ── 5. Shared workspace (private 0700) + ~/shared convenience symlink ──────────
# Create + chown ON THE GPU HOST: it is the NFS server, so root is real there.
# With root_squash on the export, an admin chown over NFS from the login node
# would be squashed to nobody and fail. Shell users get 0700 too — they are not
# in gpuusers, so a group-readable mode would not help them anyway.
step "Creating $NFS_ROOT/users/$USERNAME on the NFS server (GPU host) ..."
run "ssh $GPU_HOST_SSH \"sudo mkdir -p $NFS_ROOT/users/$USERNAME && \
    sudo chown $NEW_UID:$NEW_UID $NFS_ROOT/users/$USERNAME && \
    sudo chmod 0700 $NFS_ROOT/users/$USERNAME\""
run "ln -sfn $NFS_ROOT/users/$USERNAME /home/$USERNAME/shared 2>/dev/null || true"
ok "workspace ready (owned $NEW_UID:$NEW_UID, 0700)"

# ── 6. Verify ──────────────────────────────────────────────────────────────────
if [ "$DRY" = 0 ]; then
    step "Verifying ..."
    luid=$(id -u "$USERNAME"); ruid=$(ssh "$GPU_HOST_SSH" "id -u $USERNAME")
    [ "$luid" = "$ruid" ] || fail "UID mismatch: login=$luid gpu=$ruid"
    lho=$(stat -c %u "/home/$USERNAME" 2>/dev/null || echo "?")
    rho=$(ssh "$GPU_HOST_SSH" "stat -c %u /home/$USERNAME 2>/dev/null || echo '?'")
    [ "$lho" = "$luid" ] || fail "login home /home/$USERNAME owned by UID $lho, expected $luid"
    [ "$rho" = "$ruid" ] || fail "gpu home /home/$USERNAME owned by UID $rho, expected $ruid"
    if [ "$SHELL_USER" = 0 ]; then
        id "$USERNAME" | grep -q "$GPUUSERS_GROUP" || fail "$USERNAME not in $GPUUSERS_GROUP"
        ok "UID matched ($luid) · in $GPUUSERS_GROUP · forced-TUI applies via group"
    else
        id "$USERNAME" | grep -qw "$GPUUSERS_GROUP" && fail "shell user must NOT be in $GPUUSERS_GROUP"
        ok "UID matched ($luid) · NOT in $GPUUSERS_GROUP · real shell, cluster-capped by SLURM"
    fi
fi

echo
if [ "$SHELL_USER" = 1 ]; then
    echo "Done (shell user). Set a password or install an SSH key:"
    echo "    sudo passwd $USERNAME"
    echo "NOTE: $USERNAME has a real shell. Their activity is NOT audited by the tool."
    echo "      They are subject to SLURM gres/gpu limits via their association."
else
    echo "Done. Set a password or install an SSH key:"
    echo "    sudo passwd $USERNAME            # or: install ~$USERNAME/.ssh/authorized_keys"
    echo "$USERNAME will land directly in the TUI on next SSH login."
fi
