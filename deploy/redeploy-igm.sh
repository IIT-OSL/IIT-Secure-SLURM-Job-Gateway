#!/usr/bin/env bash
# redeploy-igm.sh — Update the single canonical clone at /opt/iit-gpu.
# Run as slurmadmin on the login node.
#
# Open-source deploy model (M03 §A): ONE git clone at /opt/iit-gpu IS the live
# tool. Every user's launcher points PYTHONPATH there. To ship an update:
#   git fetch <source>  +  git merge --ff-only  +  pytest
# The next TUI launch by any gpuusers member picks it up. No rsync, no per-user copy.
#
# Deploy SOURCE: the GitHub remote (origin) has diverged from the deployed line,
# so updates are pulled from the canonical dev clone on this host by default.
# Override with IIT_GPU_SOURCE=<git-remote-or-path> to deploy from elsewhere.
set -euo pipefail

# -- Self-modification guard (CRITICAL) ----------------------------------------
# The "git pull" further down rewrites THIS script in place while bash is still
# reading it. bash reads scripts incrementally, so an in-place rewrite can
# truncate the run and silently skip the steps near the end -- most importantly
# the audit-daemon restart. That left the live daemon running stale code (e.g.
# the old auto-BCC mail behaviour) even though the fix was pulled. Re-exec from a
# private temp copy that git can never touch, so the whole script always runs.
if [ -z "${IIT_REEXEC:-}" ]; then
    _self_copy="$(mktemp "${TMPDIR:-/tmp}/redeploy-igm.XXXXXX")"
    cat "$0" > "$_self_copy"
    export IIT_REEXEC=1
    exec bash "$_self_copy" "$@"
fi
# We are now the temp copy; unlink it immediately (the open inode survives until
# this process exits) so it never lingers in /tmp.
rm -f "$0" 2>/dev/null || true

INSTALL="${IIT_GPU_HOME:-/opt/iit-gpu}"
BRANCH="${IIT_GPU_BRANCH:-main}"
# Where to pull updates FROM. Defaults to the canonical dev clone on this host
# because the GitHub origin has diverged from the deployed history. Can be any
# git remote name, URL, or local path that holds the blessed $BRANCH.
SOURCE="${IIT_GPU_SOURCE:-/home/slurmadmin/IIT-Secure-SLURM-Job-Gateway}"

ok()   { echo "  ✔  $*"; }
warn() { echo "  ⚠  $*"; }
fail() { echo "  ✘  $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }

[ -d "$INSTALL/.git" ] || fail "$INSTALL is not a git clone. Run deploy/bootstrap-install.md first."

step "Updating canonical clone at $INSTALL (source: $SOURCE) ..."
cd "$INSTALL"
git config --global --add safe.directory "$INSTALL" 2>/dev/null || true
git config --global --add safe.directory "$SOURCE" 2>/dev/null || true
git fetch --quiet "$SOURCE" "$BRANCH" \
    || fail "git fetch from '$SOURCE' failed — check the source path/remote and network/token"
git merge --ff-only FETCH_HEAD 2>&1 \
    || fail "git merge --ff-only failed — the deployed clone has local commits or has diverged from '$SOURCE'. Resolve manually."
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

# site.env MUST be world-readable. It holds only non-secret cluster config
# (GATEWAY_HOST/PORT, CLUSTER_NAME, …); the Resend API key lives ONLY in
# secrets.env (locked 0640 root:gpusync). Welcome/login emails are built
# CLIENT-SIDE in each user's TUI process, so every TUI user must be able to
# read site.env — otherwise load_config() silently falls back to defaults
# (localhost:22) and the welcome email shows the wrong ssh command.
if [ -f "$INSTALL/deploy/site.env" ]; then
    step "Ensuring site.env is readable by all TUI users ..."
    sudo chmod 0644 "$INSTALL/deploy/site.env" \
        && ok "site.env is 0644 (TUI users read GATEWAY_HOST/PORT for emails)" \
        || warn "could not chmod site.env — non-root TUI emails may show localhost:22"
fi


# Sync mailer script to /usr/local/bin (MailProg for SLURM).
if [ -f "$INSTALL/deploy/iit-gpu-mailer" ]; then
    step "Syncing iit-gpu-mailer to /usr/local/bin ..."
    sudo cp "$INSTALL/deploy/iit-gpu-mailer" /usr/local/bin/iit-gpu-mailer
    sudo chmod 755 /usr/local/bin/iit-gpu-mailer
    ok "iit-gpu-mailer updated"
fi

# Ensure the SlurmUser (`slurm`) can read the Resend key in secrets.env.
# slurmctld runs MailProg as `slurm`, which must be in the gpusync group or
# every job-completion email is silently dropped. Idempotent: only restart
# slurmctld when we actually add the membership (a fresh group needs a restart
# for the running daemon to pick up its new supplementary groups).
if id slurm &>/dev/null; then
    if id -nG slurm | tr ' ' '\n' | grep -qx gpusync; then
        ok "slurm already in gpusync (job mail can read the API key)"
    else
        step "Adding slurm to gpusync so MailProg can read the Resend key ..."
        sudo usermod -aG gpusync slurm
        sudo systemctl restart slurmctld \
            && ok "slurm added to gpusync; slurmctld restarted" \
            || warn "usermod done but slurmctld restart failed — restart it manually"
    fi
fi

# Sync user-provisioning scripts to /usr/local/bin (the admin panel calls these via sudo).
for _s in iit-gpu-adduser iit-gpu-deluser; do
    if [ -f "$INSTALL/deploy/${_s}.sh" ]; then
        step "Syncing ${_s} to /usr/local/bin ..."
        sudo install -o root -g root -m 0755 "$INSTALL/deploy/${_s}.sh" "/usr/local/bin/${_s}"
        ok "${_s} updated"
    fi
done

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
