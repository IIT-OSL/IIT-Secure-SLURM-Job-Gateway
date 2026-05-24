#!/usr/bin/env bash
# deploy/install.sh — IIT-GPU-Manager system installer. Run as root.
set -euo pipefail

INSTALL_DIR="/opt/iit-gpu"
BIN_PATH="/usr/local/bin/iit-gpu-manager"
SERVICE_FILE="/etc/systemd/system/iit-gpu-audit.service"
SSHD_DROP_IN="/etc/ssh/sshd_config.d/99-iit-gpu-gateway.conf"
SUDOERS_FILE="/etc/sudoers.d/iit-gpu-gateway"
STATE_DIR="/var/lib/iit-gpu"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: must be run as root." >&2; exit 1
fi

echo "==> Creating groups and system users..."
getent group  gpuusers >/dev/null || groupadd --system gpuusers
id slurmsvc &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin slurmsvc
id gpusync  &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin gpusync

echo "==> Copying files to ${INSTALL_DIR}..."
install -d -o root -g root -m 0755 "${INSTALL_DIR}"
cp -r "${SCRIPT_DIR}/.." "${INSTALL_DIR}/"
find "${INSTALL_DIR}" -type f -exec chmod 644 {} \;
find "${INSTALL_DIR}" -type d -exec chmod 755 {} \;

echo "==> Installing Python dependencies..."
pip3 install --quiet rich questionary google-api-python-client google-auth

echo "==> Creating state directory (gpusync-owned, not world-readable)..."
install -d -o gpusync -g gpusync -m 0750 "${STATE_DIR}"

echo "==> Installing launcher at ${BIN_PATH}..."
cat > "${BIN_PATH}" << 'LAUNCHER'
#!/bin/bash
exec env -i \
    HOME="$HOME" \
    USER="$USER" \
    LOGNAME="$LOGNAME" \
    PATH="/usr/local/bin:/usr/bin:/bin" \
    SSH_CLIENT="${SSH_CLIENT:-}" \
    TERM="${TERM:-xterm}" \
    PYTHONPATH="/opt/iit-gpu" \
    python3 -m iitgpu --no-splash
LAUNCHER
chmod 0755 "${BIN_PATH}"

echo "==> Installing systemd service..."
cp "${SCRIPT_DIR}/iit-gpu-audit.service" "${SERVICE_FILE}"
systemctl daemon-reload
systemctl enable iit-gpu-audit.service
systemctl restart iit-gpu-audit.service

echo "==> Installing sshd drop-in..."
install -d /etc/ssh/sshd_config.d
cp "${SCRIPT_DIR}/sshd-gateway.conf" "${SSHD_DROP_IN}"
chmod 0600 "${SSHD_DROP_IN}"
if ! sshd -t; then
    echo "ERROR: sshd config validation failed — removing drop-in." >&2
    rm -f "${SSHD_DROP_IN}"; exit 1
fi
systemctl reload sshd

echo "==> Installing sudoers rules..."
cp "${SCRIPT_DIR}/sudoers-gateway" "${SUDOERS_FILE}"
chmod 0440 "${SUDOERS_FILE}"
if ! visudo -cf "${SUDOERS_FILE}"; then
    echo "ERROR: sudoers validation failed — removing file." >&2
    rm -f "${SUDOERS_FILE}"; exit 1
fi

echo ""
echo "Installation complete."
echo "  Add a user: usermod -aG gpuusers <username>"
echo "  Daemon status: systemctl status iit-gpu-audit"
