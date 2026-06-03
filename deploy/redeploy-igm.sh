#!/usr/bin/env bash
# redeploy-igm.sh — Update the single canonical clone at /opt/iit-gpu.
# Run as slurmadmin on the login node.
#
# Open-source deploy model (M03 §A): ONE git clone at /opt/iit-gpu IS the live
# tool. Every user's launcher points PYTHONPATH there. To ship an update:
#   git pull --ff-only  +  pytest
# The next TUI launch by any gpuusers member picks it up. No rsync, no per-user copy.
set -euo pipefail

INSTALL="${IIT_GPU_HOME:-/opt/iit-gpu}"
BRANCH="${IIT_GPU_BRANCH:-main}"

ok()   { echo "  ✔  $*"; }
warn() { echo "  ⚠  $*"; }
fail() { echo "  ✘  $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }

[ -d "$INSTALL/.git" ] || fail "$INSTALL is not a git clone. Run deploy/bootstrap-install.md first."

step "Updating canonical clone at $INSTALL ..."
cd "$INSTALL"
git config --global --add safe.directory "$INSTALL" 2>/dev/null || true
git fetch --quiet origin "$BRANCH" || fail "git fetch failed — check network/token"
git pull --ff-only origin "$BRANCH" 2>&1 || fail "git pull --ff-only failed (local commits? resolve manually)"
ok "HEAD: $(git log --oneline -1)"

step "Running test suite ..."
PYTHONPATH="$INSTALL" python3 -m pytest "$INSTALL/tests/" -q --tb=short \
    || fail "Tests failed — investigate before relying on this revision"
ok "All tests passed"

step "Verifying import as a gpuusers member ..."
PYTHONPATH="$INSTALL" python3 -c "
from iitgpu.config import load_config
cfg = load_config()
print(f'    config OK | NFS_ROOT={cfg.nfs_root} | shared_user_mode={cfg.gateway_shared_user}')
" || fail "Import check failed"
ok "Import OK"

# Audit daemon — restart and VERIFY the process actually changed.
if systemctl list-unit-files 2>/dev/null | grep -q '^iit-gpu-audit'; then
    step "Restarting iit-gpu-audit ..."
    _pid_before=$(systemctl show iit-gpu-audit --property=MainPID --value 2>/dev/null || echo 0)
    _ts_before=$(systemctl show iit-gpu-audit --property=ExecMainStartTimestamp --value 2>/dev/null || echo "")
    sudo systemctl restart iit-gpu-audit || fail "systemctl restart iit-gpu-audit failed"
    sleep 1
    _pid_after=$(systemctl show iit-gpu-audit --property=MainPID --value 2>/dev/null || echo 0)
    _ts_after=$(systemctl show iit-gpu-audit --property=ExecMainStartTimestamp --value 2>/dev/null || echo "")
    if [ "$_pid_after" = "0" ] || ! systemctl is-active --quiet iit-gpu-audit; then
        fail "iit-gpu-audit failed to start after restart — check: journalctl -u iit-gpu-audit -n 20"
    fi
    if [ "$_pid_after" = "$_pid_before" ] && [ "$_ts_after" = "$_ts_before" ]; then
        fail "iit-gpu-audit PID unchanged after restart (${_pid_after}) — daemon did not restart. Check sudoers and journalctl."
    fi
    ok "audit service restarted (PID ${_pid_before} -> ${_pid_after})"
fi

echo
echo "Deploy complete — every gpuusers member now runs $(git -C "$INSTALL" log --oneline -1)"
