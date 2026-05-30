#!/usr/bin/env bash
# redeploy-igm.sh — Sync with GitHub (push local edits OR pull remote ones) then redeploy.
# Run as slurmadmin on the login node (192.168.122.10).
#
# Behaviour:
#   Local changes present  → commit all, push to GitHub, then deploy
#   No local changes       → pull latest from GitHub, then deploy
#   Both sides have changes→ commit local, rebase onto remote, push, then deploy
set -euo pipefail

REPO="/home/slurmadmin/IIT-Secure-SLURM-Job-Gateway"
INSTALL="/opt/iit-gpu"
BRANCH="main"

ok()   { echo "  ✔  $*"; }
warn() { echo "  ⚠  $*"; }
fail() { echo "  ✘  $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }

# ── 1. Sync with GitHub ───────────────────────────────────────────────────────
step "Syncing with GitHub..."

cd "$REPO"
git config --global --add safe.directory "$REPO" 2>/dev/null || true

# Detect uncommitted local changes (tracked files only — ignores untracked)
if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "  Local changes detected — committing and pushing..."

    git add -A
    COMMIT_MSG="redeploy: push local changes $(date '+%Y-%m-%d %H:%M:%S')"
    git commit -m "$COMMIT_MSG" \
        || fail "git commit failed"

    # If remote also moved ahead, rebase our commit on top before pushing
    git pull --rebase origin "$BRANCH" 2>&1 \
        || fail "git pull --rebase failed — resolve conflicts manually in $REPO"

    git push origin "$BRANCH" 2>&1 \
        || fail "git push failed — check token or network"

    ok "Local changes committed and pushed"

else
    echo "  No local changes — pulling from GitHub..."

    git pull origin "$BRANCH" 2>&1 \
        || fail "git pull failed — check network or token"

    ok "Pulled latest"
fi

ok "HEAD: $(git log --oneline -1)"

# ── 2. Tests ──────────────────────────────────────────────────────────────────
step "Running test suite..."
PYTHONPATH="$REPO" python3 -m pytest "$REPO/tests/" -q --tb=short \
    || fail "Tests failed — aborting deploy (no changes made to /opt/iit-gpu)"
ok "All tests passed"

# ── 3. Stop service ───────────────────────────────────────────────────────────
step "Stopping iit-gpu-audit..."
sudo systemctl stop iit-gpu-audit
ok "Service stopped"

# ── 4. Sync code ──────────────────────────────────────────────────────────────
step "Syncing code to ${INSTALL}..."
# rsync --delete ensures files removed from the repo are also removed from /opt/iit-gpu
sudo rsync -a --delete "$REPO/iitgpu/"    "$INSTALL/iitgpu/"
sudo rsync -a --delete "$REPO/deploy/"    "$INSTALL/deploy/"
sudo cp "$REPO/requirements.txt" "$INSTALL/"
# Remove ALL bytecode so Python recompiles from freshly synced source
sudo find "$INSTALL/iitgpu" -name "*.pyc" -delete 2>/dev/null || true
sudo find "$INSTALL/iitgpu" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
ok "Code synced"

# ── 5. Rebuild launcher from current install.sh values ───────────────────────
CONDA_PREFIX="${CONDA_PREFIX_SHARED:-/shared/miniforge3}"
NFS_ROOT_VAL="${NFS_ROOT:-/shared}"
sudo tee /usr/local/bin/iit-gpu-manager > /dev/null << LAUNCHER
#!/bin/bash
exec env -i \\
    HOME="\$HOME" \\
    USER="\$USER" \\
    LOGNAME="\$LOGNAME" \\
    PATH="${CONDA_PREFIX}/bin:/usr/local/bin:/usr/bin:/bin" \\
    SSH_CLIENT="\${SSH_CLIENT:-}" \\
    TERM="\${TERM:-xterm}" \\
    PYTHONPATH="/opt/iit-gpu" \\
    CONDA_PREFIX_SHARED="${CONDA_PREFIX}" \\
    NFS_ROOT="${NFS_ROOT_VAL}" \\
    /usr/bin/python3 -m iitgpu
LAUNCHER
sudo chmod 0755 /usr/local/bin/iit-gpu-manager
ok "Launcher updated"

# ── 6. Restart service ────────────────────────────────────────────────────────
step "Starting iit-gpu-audit..."
sudo systemctl start iit-gpu-audit
sleep 1
if systemctl is-active --quiet iit-gpu-audit; then
    ok "Service is running  (PID $(systemctl show iit-gpu-audit --property=MainPID --value))"
else
    fail "Service failed to start — check: journalctl -u iit-gpu-audit -n 30"
fi

# ── 7. Smoke import ───────────────────────────────────────────────────────────
step "Verifying Python import..."
sudo -u public env -i \
    HOME=/home/public USER=public LOGNAME=public \
    PATH="/shared/miniforge3/bin:/usr/local/bin:/usr/bin:/bin" \
    PYTHONPATH="/opt/iit-gpu" \
    CONDA_PREFIX_SHARED="/shared/miniforge3" NFS_ROOT="/shared" \
    /usr/bin/python3 -c "
from iitgpu.config import load_config
from iitgpu.envbuilder import _find_conda
cfg = load_config()
conda = _find_conda(cfg)
assert conda, 'conda not found'
print(f'    config OK  |  conda: {conda}')
" || fail "Import check failed — check /opt/iit-gpu"
ok "Import OK"

echo
echo "Deploy complete.  Commit: $(git -C "$REPO" log --oneline -1)"
