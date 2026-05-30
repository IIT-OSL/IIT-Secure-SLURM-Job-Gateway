#!/usr/bin/env bash
# redeploy.sh — Run from the GPU host (iit-MS-7E06) as root-daham.
#
# This machine has no git. All git/deploy work runs on the login node
# (192.168.122.10) via SSH. This script:
#   1. Triggers the login-node deploy (git pull → tests → /opt/iit-gpu → service restart)
#   2. Ensures the GPU stats writer daemon is running on this host
set -euo pipefail

LOGIN="slurmadmin@192.168.122.10"
STATS_WRITER="/tmp/iit-gpu-stats-writer"
STATS_LOG="/tmp/iit-gpu-stats.log"
STATS_JSON="/shared/.gpu_stats.json"

ok()   { echo "  ✔  $*"; }
warn() { echo "  ⚠  $*"; }
fail() { echo "  ✘  $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }

# ── 1. Deploy on login node ───────────────────────────────────────────────────
step "Running deploy on login node (192.168.122.10)..."
ssh "$LOGIN" "bash /home/slurmadmin/redeploy-igm.sh" \
    || fail "Login-node deploy failed"

# ── 2. Ensure GPU stats writer is running on this host ───────────────────────
step "Checking GPU stats writer..."

if [ ! -f "$STATS_WRITER" ]; then
    warn "Stats writer not found at $STATS_WRITER — hardware stats panel will use fallback data"
else
    # Check if running and producing fresh output
    RUNNING=0
    pgrep -f "iit-gpu-stats-writer" > /dev/null 2>&1 && RUNNING=1

    FRESH=0
    if [ -f "$STATS_JSON" ]; then
        AGE=$(( $(date +%s) - $(stat --format=%Y "$STATS_JSON") ))
        [ "$AGE" -lt 15 ] && FRESH=1
    fi

    if [ "$RUNNING" -eq 1 ] && [ "$FRESH" -eq 1 ]; then
        ok "Stats writer running and fresh (PID $(pgrep -f iit-gpu-stats-writer | head -1))"
    else
        # Kill stale instance if any, then restart
        pkill -f "iit-gpu-stats-writer" 2>/dev/null || true
        sleep 1
        nohup python3 "$STATS_WRITER" > "$STATS_LOG" 2>&1 &
        sleep 3
        if [ -f "$STATS_JSON" ] && [ "$(( $(date +%s) - $(stat --format=%Y "$STATS_JSON") ))" -lt 15 ]; then
            ok "Stats writer started (PID $(pgrep -f iit-gpu-stats-writer | head -1))"
        else
            warn "Stats writer launched but no output yet — check $STATS_LOG"
        fi
    fi
fi

echo
ok "Done."
