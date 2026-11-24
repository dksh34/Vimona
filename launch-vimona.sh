#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
export PYTHONPATH="${APP_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

cd "${APP_DIR}"
exec /usr/bin/python3 "${APP_DIR}/run_vimona.py" "$@"
