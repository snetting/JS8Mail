#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    echo "Creating JS8Mail virtual environment..."
    python3 -m venv "${VENV_DIR}"
fi

if [[ ! -f "${VENV_DIR}/.js8mail-installed" || "${SCRIPT_DIR}/pyproject.toml" -nt "${VENV_DIR}/.js8mail-installed" ]]; then
    echo "Installing JS8Mail and development dependencies..."
    "${VENV_DIR}/bin/python" -m pip install --upgrade pip
    "${VENV_DIR}/bin/python" -m pip install -e "${SCRIPT_DIR}[test]"
    touch "${VENV_DIR}/.js8mail-installed"
fi

echo "Starting JS8Mail local mailbox. RF transmission requires --tx-mode approve."
exec "${VENV_DIR}/bin/python" -m js8mail.tools.app "$@"
