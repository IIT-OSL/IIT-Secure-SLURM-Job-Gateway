#!/usr/bin/env bash
# addUser.sh — interactive wrapper around iit-gpu-adduser.sh.
# Prompts for a username (and a couple of options), then provisions the user on
# BOTH nodes via the real onboarding script. Run as an admin (needs sudo).
#
#   ./addUser.sh
set -euo pipefail

# Locate the real provisioning script: prefer the installed one, then the repo copy.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADDUSER=""
for cand in /usr/local/bin/iit-gpu-adduser "$SELF_DIR/deploy/iit-gpu-adduser.sh"; do
    [ -x "$cand" ] && ADDUSER="$cand" && break
done
[ -n "$ADDUSER" ] || { echo "  ✘  iit-gpu-adduser.sh not found (looked in /usr/local/bin and $SELF_DIR/deploy)"; exit 1; }

echo "════════════════════════════════════════════════"
echo "   IIT GPU Cluster — Add a User"
echo "════════════════════════════════════════════════"

# ── Prompt: username ────────────────────────────────────────────────────────────
USERNAME=""
while :; do
    read -r -p "  New username: " USERNAME || { echo; echo "  cancelled."; exit 1; }
    USERNAME="$(echo "$USERNAME" | tr -d '[:space:]')"
    if [ -z "$USERNAME" ]; then
        echo "  ⚠  Username cannot be empty."
    elif ! [[ "$USERNAME" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]; then
        echo "  ⚠  Invalid: lowercase letters/digits/_/- , must start with a letter or _ (max 32)."
    elif getent passwd "$USERNAME" >/dev/null 2>&1; then
        echo "  ⚠  User '$USERNAME' already exists on this node."
    else
        break
    fi
done

# ── Prompt: admin? ──────────────────────────────────────────────────────────────
read -r -p "  Grant admin (cluster ops + provisioning)? [y/N]: " ADMIN_ANS
ADMIN_FLAG=""
case "${ADMIN_ANS,,}" in y|yes) ADMIN_FLAG="--admin" ;; esac

# ── Prompt: dry run? ────────────────────────────────────────────────────────────
read -r -p "  Dry run first (show actions, change nothing)? [y/N]: " DRY_ANS
DRY_FLAG=""
case "${DRY_ANS,,}" in y|yes) DRY_FLAG="--dry-run" ;; esac

echo
echo "  → Provisioning '$USERNAME' ${ADMIN_FLAG:+(admin) }${DRY_FLAG:+[dry-run] }on both nodes ..."
echo

# ── Provision (needs root) ──────────────────────────────────────────────────────
if [ "$(id -u)" -eq 0 ]; then
    "$ADDUSER" "$USERNAME" $ADMIN_FLAG $DRY_FLAG
else
    sudo "$ADDUSER" "$USERNAME" $ADMIN_FLAG $DRY_FLAG
fi

# ── Offer to set a password (skip on dry-run) ───────────────────────────────────
if [ -z "$DRY_FLAG" ]; then
    read -r -p "  Set a login password for '$USERNAME' now? [y/N]: " PW_ANS
    case "${PW_ANS,,}" in
        y|yes) if [ "$(id -u)" -eq 0 ]; then passwd "$USERNAME"; else sudo passwd "$USERNAME"; fi ;;
        *)     echo "  ℹ  Skipped. Set later with: sudo passwd $USERNAME  (or install an SSH key)." ;;
    esac
    echo
    echo "  ✔  '$USERNAME' will land directly in the TUI on next SSH login."
fi
