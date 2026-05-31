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

# Audit daemon (if present) just needs a restart to pick up new daemon code.
if systemctl list-unit-files 2>/dev/null | grep -q '^iit-gpu-audit'; then
    step "Restarting iit-gpu-audit ..."
    sudo systemctl restart iit-gpu-audit && ok "audit service restarted" || warn "audit restart failed"
fi

echo
echo "Deploy complete — every gpuusers member now runs $(git -C "$INSTALL" log --oneline -1)"
