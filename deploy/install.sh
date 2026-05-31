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

# Configurable via env vars so the installer works for non-default layouts.
NFS_ROOT="${NFS_ROOT:-/shared}"
CONDA_PREFIX="${CONDA_PREFIX_SHARED:-${NFS_ROOT}/miniforge3}"
CONDA_SH="${CONDA_PREFIX}/etc/profile.d/conda.sh"
GATEWAY_USER="${GATEWAY_USER:-public}"

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: must be run as root." >&2; exit 1
fi

# ── System packages ───────────────────────────────────────────────────────────
echo "==> Installing system packages..."
apt-get update -qq
apt-get install -y \
    wget curl rsync git \
    python3 python3-pip \
    bc jq acl \
    nfs-common \
    --no-install-recommends

# ── Groups and system users ───────────────────────────────────────────────────
echo "==> Creating groups and system users..."
getent group  gpuusers  >/dev/null || groupadd --system gpuusers
getent group  auditadmin >/dev/null || groupadd --system auditadmin
id slurmsvc &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin slurmsvc
id gpusync  &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin gpusync

# ── Shared directory structure ────────────────────────────────────────────────
echo "==> Creating shared directory structure under ${NFS_ROOT}..."
mkdir -p \
    "${NFS_ROOT}/scripts" \
    "${NFS_ROOT}/jobs" \
    "${NFS_ROOT}/data" \
    "${NFS_ROOT}/envs" \
    "${NFS_ROOT}/models" \
    "${NFS_ROOT}/templates"
# Multi-user shared state: any gpuusers member (not just the first creator) must
# be able to read/write the model & env registries, templates, job dirs, and
# uploads. Own by group gpuusers, make group-writable, set the setgid bit on
# directories so new files/sub-dirs inherit the gpuusers group, and (if the acl
# tools are present) add a default ACL so new files are gpuusers-writable
# regardless of the creating process's umask. Paired with `umask 002` in the
# launcher this yields group-writable 0664 files owned by group gpuusers.
echo "==> Setting shared permissions for multi-user (gpuusers) access..."
for _d in scripts jobs data envs models templates; do
    _p="${NFS_ROOT}/${_d}"
    [ -d "${_p}" ] || continue
    chown -R "${GATEWAY_USER}:gpuusers" "${_p}" 2>/dev/null || true
    chmod -R g+rwX "${_p}" 2>/dev/null || true
    find "${_p}" -type d -exec chmod g+s {} + 2>/dev/null || true
    if command -v setfacl >/dev/null 2>&1; then
        setfacl -R    -m g:gpuusers:rwX "${_p}" 2>/dev/null || true
        setfacl -R -d -m g:gpuusers:rwX "${_p}" 2>/dev/null || true
    fi
done

# ── Miniforge (conda) ─────────────────────────────────────────────────────────
echo "==> Installing Miniforge to ${CONDA_PREFIX}..."
if [ ! -f "${CONDA_PREFIX}/bin/conda" ]; then
    MINIFORGE_SH="/tmp/Miniforge3.sh"
    wget -q \
        "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh" \
        -O "${MINIFORGE_SH}"
    bash "${MINIFORGE_SH}" -b -p "${CONDA_PREFIX}"
    rm -f "${MINIFORGE_SH}"
else
    echo "   conda already present at ${CONDA_PREFIX}/bin/conda — skipping download."
fi

# ── conda init for gateway user ───────────────────────────────────────────────
echo "==> Initialising conda for ${GATEWAY_USER}..."
sudo -u "${GATEWAY_USER}" "${CONDA_PREFIX}/bin/conda" init bash 2>/dev/null || true

# ── conda.sh in /etc/bash.bashrc (covers non-interactive sbatch scripts) ──────
echo "==> Adding conda.sh to /etc/bash.bashrc..."
if ! grep -q "miniforge3\|CONDA_PREFIX_SHARED\|iit-gpu conda" /etc/bash.bashrc 2>/dev/null; then
    cat >> /etc/bash.bashrc << BASHRC

# conda — added by IIT-GPU-Manager installer
[ -f "${CONDA_SH}" ] && source "${CONDA_SH}"
BASHRC
fi

# ── Copy repo files ───────────────────────────────────────────────────────────
echo "==> Copying files to ${INSTALL_DIR}..."
install -d -o root -g root -m 0755 "${INSTALL_DIR}"
cp -r "${SCRIPT_DIR}/.." "${INSTALL_DIR}/"
find "${INSTALL_DIR}" -type f -exec chmod 644 {} \;
find "${INSTALL_DIR}" -type d -exec chmod 755 {} \;

# ── Python dependencies ───────────────────────────────────────────────────────
echo "==> Installing Python dependencies..."
pip3 install --quiet --break-system-packages -r "${INSTALL_DIR}/requirements.txt"

# ── Audit log state dir ───────────────────────────────────────────────────────
echo "==> Setting up audit log state directory..."
usermod -aG auditadmin gpusync
usermod -aG auditadmin slurmadmin
install -d -o gpusync -g auditadmin -m 0750 "${STATE_DIR}"

# ── Launcher ──────────────────────────────────────────────────────────────────
echo "==> Installing launcher at ${BIN_PATH}..."
# The launcher uses env -i to sanitise the environment. Conda's bin dir must be
# included explicitly so envbuilder's shutil.which("conda") succeeds at runtime.
cat > "${BIN_PATH}" << LAUNCHER
#!/bin/bash
# umask 002 so shared state the tool writes (registries, templates, job dirs) is
# group-writable by gpuusers; survives the env -i below (umask is not an env var).
umask 002
exec env -i \\
    HOME="\$HOME" \\
    USER="\$USER" \\
    LOGNAME="\$LOGNAME" \\
    PATH="${CONDA_PREFIX}/bin:/usr/local/bin:/usr/bin:/bin" \\
    SSH_CLIENT="\${SSH_CLIENT:-}" \\
    TERM="\${TERM:-xterm}" \\
    PYTHONPATH="/opt/iit-gpu" \\
    CONDA_PREFIX_SHARED="${CONDA_PREFIX}" \\
    NFS_ROOT="${NFS_ROOT}" \\
    /usr/bin/python3 -m iitgpu
LAUNCHER
chmod 0755 "${BIN_PATH}"

# ── systemd service ───────────────────────────────────────────────────────────
echo "==> Installing systemd service..."
cp "${SCRIPT_DIR}/iit-gpu-audit.service" "${SERVICE_FILE}"
systemctl daemon-reload
systemctl enable iit-gpu-audit.service
systemctl restart iit-gpu-audit.service

# ── sshd drop-in ─────────────────────────────────────────────────────────────
echo "==> Installing sshd drop-in..."
install -d /etc/ssh/sshd_config.d
cp "${SCRIPT_DIR}/sshd-gateway.conf" "${SSHD_DROP_IN}"
chmod 0600 "${SSHD_DROP_IN}"
if ! sshd -t; then
    echo "ERROR: sshd config validation failed — removing drop-in." >&2
    rm -f "${SSHD_DROP_IN}"; exit 1
fi
systemctl reload sshd

# ── sudoers ───────────────────────────────────────────────────────────────────
echo "==> Installing sudoers rules..."
cp "${SCRIPT_DIR}/sudoers-gateway" "${SUDOERS_FILE}"
chmod 0440 "${SUDOERS_FILE}"
if ! visudo -cf "${SUDOERS_FILE}"; then
    echo "ERROR: sudoers validation failed — removing file." >&2
    rm -f "${SUDOERS_FILE}"; exit 1
fi

# ── admin log viewer ──────────────────────────────────────────────────────────
echo "==> Installing admin log viewer..."
install -o root -g auditadmin -m 0750 "${SCRIPT_DIR}/iit-gpu-log" /usr/local/bin/iit-gpu-log

echo ""
echo "Installation complete."
echo "  conda prefix : ${CONDA_PREFIX}"
echo "  NFS root     : ${NFS_ROOT}"
echo "  Add a user   : usermod -aG gpuusers <username>"
echo "  Daemon status: systemctl status iit-gpu-audit"
