#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/home/muthyaladaksh/.gemini/antigravity/scratch/vimona"
export PYTHONPATH="${APP_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

cd "${APP_DIR}"
exec /usr/bin/python3 "${APP_DIR}/run_vimona.py" "$@"
