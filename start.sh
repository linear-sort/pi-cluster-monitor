#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: ./start.sh <agent|dashboard> [host] [port] [--with-dev-deps] [--debug]"
  exit 1
fi

TARGET="$1"
HOST="${2:-0.0.0.0}"
PORT="${3:-}"
WITH_DEV_DEPS="false"
DEBUG_MODE="false"

for arg in "$@"; do
  if [[ "$arg" == "--with-dev-deps" ]]; then
    WITH_DEV_DEPS="true"
  elif [[ "$arg" == "--debug" ]]; then
    DEBUG_MODE="true"
  fi
done

if [[ "$TARGET" != "agent" && "$TARGET" != "dashboard" ]]; then
  echo "Target must be one of: agent, dashboard"
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$ROOT_DIR/$TARGET"

if [[ ! -d "$APP_DIR" ]]; then
  echo "App directory not found: $APP_DIR"
  exit 1
fi

if [[ -z "$PORT" ]]; then
  if [[ "$TARGET" == "dashboard" ]]; then
    PORT="8000"
  else
    PORT="8001"
  fi
fi

if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  echo "Python not found in PATH"
  exit 1
fi

VENV_DIR="$APP_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"

if [[ ! -x "$VENV_PY" ]]; then
  echo "Creating virtual environment in $VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

echo "Installing runtime dependencies for $TARGET"
"$VENV_PY" -m pip install -r "$APP_DIR/requirements.txt"

if [[ ( "$WITH_DEV_DEPS" == "true" || "$DEBUG_MODE" == "true" ) && -f "$APP_DIR/requirements-dev.txt" ]]; then
  echo "Installing dev dependencies for $TARGET"
  "$VENV_PY" -m pip install -r "$APP_DIR/requirements-dev.txt"
fi

ENV_EXAMPLE="$ROOT_DIR/deploy/$TARGET.env.example"
ENV_LOCAL="$APP_DIR/.env"
if [[ -f "$ENV_EXAMPLE" && ! -f "$ENV_LOCAL" ]]; then
  echo "Creating $ENV_LOCAL from example file"
  cp "$ENV_EXAMPLE" "$ENV_LOCAL"
fi

cd "$APP_DIR"

if [[ "$DEBUG_MODE" == "true" ]]; then
  echo "Running $TARGET test suite before startup"
  "$VENV_PY" -m pytest -q
fi

if [[ "$TARGET" == "dashboard" ]]; then
  echo "Starting dashboard on http://$HOST:$PORT"
  exec "$VENV_PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT" --reload
else
  echo "Starting agent on http://$HOST:$PORT"
  exec "$VENV_PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT"
fi
