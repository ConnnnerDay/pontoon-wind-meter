#!/usr/bin/env bash
# install_service.sh — deploy Pontoon Wind Meter on a Raspberry Pi
#
# Run as root (or with sudo) from any directory:
#   sudo bash scripts/install_service.sh
#
# What it does:
#   1. Stops the service if it is already running.
#   2. Copies project files to /home/pizero/pontoon-wind-meter.
#   3. Installs / updates Python packages into /home/pizero/tftenv.
#   4. Installs / updates the systemd unit.
#   5. Reloads systemd, enables and restarts the service.
#   6. Prints current service status.

set -euo pipefail

# ── configurable paths ────────────────────────────────────────────────────
SERVICE_USER="${SERVICE_USER:-pizero}"
DEPLOY_DIR="/home/${SERVICE_USER}/pontoon-wind-meter"
VENV_DIR="/home/${SERVICE_USER}/tftenv"
SERVICE_NAME="pontoon-meter"
UNIT_FILE="systemd/${SERVICE_NAME}.service"
SYSTEMD_DEST="/etc/systemd/system/${SERVICE_NAME}.service"
# ─────────────────────────────────────────────────────────────────────────

# Must run as root so we can install the systemd unit and restart the service.
if [[ "${EUID}" -ne 0 ]]; then
    echo "Error: this script must be run as root (use sudo)." >&2
    exit 1
fi

# Resolve the project root (directory containing this script's parent).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "==> Project root : ${PROJECT_ROOT}"
echo "==> Deploy to    : ${DEPLOY_DIR}"
echo "==> Virtualenv   : ${VENV_DIR}"
echo ""

# 1. Stop the service (ignore errors if it is not yet installed).
echo "==> Stopping ${SERVICE_NAME} (if running)..."
systemctl stop "${SERVICE_NAME}" 2>/dev/null || true

# 2. Copy project files.
echo "==> Copying project files to ${DEPLOY_DIR}..."
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${DEPLOY_DIR}"
rsync -a --delete \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.pytest_cache' \
    "${PROJECT_ROOT}/" "${DEPLOY_DIR}/"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${DEPLOY_DIR}"

# 3. Install / update Python packages.
echo "==> Updating Python packages in ${VENV_DIR}..."
if [[ ! -x "${VENV_DIR}/bin/pip" ]]; then
    echo "    Virtualenv not found at ${VENV_DIR}."
    echo "    Create it first with:"
    echo "      python3 -m venv --system-site-packages ${VENV_DIR}"
    exit 1
fi
sudo -u "${SERVICE_USER}" "${VENV_DIR}/bin/pip" install \
    --quiet --upgrade \
    -r "${DEPLOY_DIR}/requirements.txt"

# 4. Install / update the systemd unit.
echo "==> Installing systemd unit to ${SYSTEMD_DEST}..."
install -m 644 "${DEPLOY_DIR}/${UNIT_FILE}" "${SYSTEMD_DEST}"

# 5. Reload systemd and enable the service.
echo "==> Reloading systemd daemon..."
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

# 6. Create the cache directory with correct ownership.
CACHE_DIR="/home/${SERVICE_USER}/.cache/pontoon-meter"
echo "==> Creating cache directory ${CACHE_DIR}..."
install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${CACHE_DIR}"

# 7. Start the service.
echo "==> Starting ${SERVICE_NAME}..."
systemctl restart "${SERVICE_NAME}"

echo ""
echo "==> Done. Service status:"
systemctl status "${SERVICE_NAME}" --no-pager || true
