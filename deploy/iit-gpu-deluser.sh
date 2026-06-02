#!/usr/bin/env bash
# iit-gpu-deluser.sh — offboard a per-user account from both nodes.
#
# Usage:  sudo iit-gpu-deluser <username> [--dry-run] [--purge-data]
set -euo pipefail

SITE_ENV="${IIT_SITE_ENV:-/opt/iit-gpu/deploy/site.env}"
[ -f "$SITE_ENV" ] && set -a && . "$SITE_ENV" && set +a
NFS_ROOT="${NFS_ROOT:-/shared}"
GPU_HOST_SSH="${GPU_HOST_SSH:-}"
SHARED_USER="${GATEWAY_SHARED_USER_NAME:-daham}"
IIT_INSTALL_DIR="${IIT_INSTALL_DIR:-/opt/iit-gpu}"

ok(){ echo "  ✔  $*"; }; warn(){ echo "  ⚠  $*"; }
fail(){ echo "  ✘  $*" >&2; exit 1; }; step(){ echo; echo "==> $*"; }

USERNAME=""; DRY=0; PURGE=0
for a in "$@"; do
    case "$a" in
        --dry-run)    DRY=1 ;;
        --purge-data) PURGE=1 ;;
        -*)           fail "unknown flag: $a" ;;
        *)            USERNAME="$a" ;;
    esac
done
[ -n "$USERNAME" ] || fail "usage: iit-gpu-deluser <username> [--dry-run] [--purge-data]"
case "$USERNAME" in
    public|"$SHARED_USER"|root|slurm|slurmadmin|root-daham)
        fail "refusing to remove protected account: $USERNAME" ;;
esac
[ -n "$GPU_HOST_SSH" ] || fail "GPU_HOST_SSH not set"
[ "$(id -u)" = 0 ] || [ "$DRY" = 1 ] || fail "must run as root (sudo)"
run() { if [ "$DRY" = 1 ]; then echo "  [dry-run] $*"; else eval "$@"; fi; }

step "Removing SLURM association ..."
run "sacctmgr -i delete user $USERNAME 2>/dev/null || true"; ok "assoc removed"

step "Handling /shared data (on the NFS server; root_squash-safe) ..."
if [ "$PURGE" = 1 ]; then
    run "ssh $GPU_HOST_SSH \"sudo rm -rf $NFS_ROOT/$USERNAME\""; ok "data purged"
else
    run "ssh $GPU_HOST_SSH \"sudo mv $NFS_ROOT/$USERNAME $NFS_ROOT/$USERNAME.offboarded 2>/dev/null || true\""
    ok "data kept as $NFS_ROOT/$USERNAME.offboarded"
fi

step "Removing user on GPU host ..."
run "ssh $GPU_HOST_SSH \"sudo userdel $USERNAME 2>/dev/null || true; sudo groupdel $USERNAME 2>/dev/null || true\""
ok "GPU host cleaned"

step "Removing user on login node ..."
run "userdel -r $USERNAME 2>/dev/null || true"
run "groupdel $USERNAME 2>/dev/null || true"
ok "login cleaned"

# ── Update users.db via daemon (best-effort; daemon must be running) ──────────
step "Updating user database ..."
if [ "$DRY" = 0 ]; then
    PYTHONPATH="$IIT_INSTALL_DIR" python3 -m iitgpu.daemoncli users.offboard \
        --username "$USERNAME" 2>/dev/null \
        && ok "DB row offboarded" \
        || warn "Could not update user DB (daemon may be stopped — offboard manually)"
else
    echo "  [dry-run] would call users.offboard for $USERNAME in user DB"
fi

echo; echo "Offboarded $USERNAME."
