#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
elif [[ -x "$PROJECT_DIR/venv312/bin/python" ]]; then
  PYTHON_BIN="$PROJECT_DIR/venv312/bin/python"
else
  echo "Python interpreter not found at either: $PROJECT_DIR/.venv/bin/python or $PROJECT_DIR/venv312/bin/python" >&2
  exit 1
fi
MAIN_FILE="$PROJECT_DIR/main.py"

if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  echo ".env file not found at: $PROJECT_DIR/.env" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source "$PROJECT_DIR/.env"
set +a

CHECK_URL="${CHECK_URL:-${TELEGRAM_HEALTHCHECK_URL:-${TELEGRAM_BASE_URL:-https://api.telegram.org}}}"

check_internet() {
  if command -v curl >/dev/null 2>&1; then
    # Connectivity check only: do not fail on HTTP status codes.
    curl -sS --connect-timeout 5 --max-time 5 "$CHECK_URL" >/dev/null 2>&1
    return $?
  fi

  if command -v wget >/dev/null 2>&1; then
    # --spider fails on 404/5xx; download to /dev/null instead.
    wget -q --timeout=5 -O /dev/null "$CHECK_URL"
    return $?
  fi

  host="${CHECK_URL#*://}"
  host="${host%%/*}"
  host="${host%%:*}"
  [[ -n "$host" ]] || host="api.telegram.org"
  getent hosts "$host" >/dev/null 2>&1
}

until check_internet; do
  echo "$(date -Is) internet unavailable, retrying in 5 seconds..."
  sleep 5
done

cd "$PROJECT_DIR"
exec "$PYTHON_BIN" "$MAIN_FILE"
