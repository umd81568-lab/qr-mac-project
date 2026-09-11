#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

VENV_DIR="${VENV_DIR:-.venv}"
if [[ ! -d "$VENV_DIR" ]]; then
  python3 -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

python -c "from app import create_app; create_app()"

BASE_PORT="${PORT:-8000}"
BIND_HOST="${BIND_HOST:-0.0.0.0}"
CHOSEN_PORT="$(python -c "from app import find_free_port; print(find_free_port(int('${BASE_PORT}'), host='${BIND_HOST}'))" )"
printf '%s' "$CHOSEN_PORT" > .port

IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [[ -z "${IP_ADDR}" ]]; then
  IP_ADDR="127.0.0.1"
fi

echo "Chosen port: ${CHOSEN_PORT}"
echo "Open: http://${IP_ADDR}:${CHOSEN_PORT}"

python -c "from app import can_bind; import sys; sys.exit(0 if can_bind('${BIND_HOST}', int('${CHOSEN_PORT}')) else 1)" \
  || { echo "Port ${CHOSEN_PORT} on ${BIND_HOST} is no longer free. Please retry."; exit 1; }

if python -c "import gunicorn" >/dev/null 2>&1; then
  exec gunicorn -b "${BIND_HOST}:${CHOSEN_PORT}" app:app
fi

PORT="$CHOSEN_PORT" BIND_HOST="$BIND_HOST" exec python app.py
