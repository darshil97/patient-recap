#!/usr/bin/env bash
#
# run_local.sh — start/stop the backend (FastAPI/uvicorn) and frontend (Vite)
# dev servers in the background. Unlike a plain foreground runner, `start`
# launches both and returns control of the terminal immediately; `stop` tears
# both down.
#
# Usage:
#   ./run_local.sh start
#   ./run_local.sh stop
#   ./run_local.sh status
#   ./run_local.sh restart
#
# Env overrides:
#   BACKEND_PORT   port for uvicorn (default 8000)
#   PYTHON_BIN     python interpreter for the backend
#                  (defaults to the 'reels' conda env if present, else python3)

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend/app"
RUN_DIR="$ROOT_DIR/.run_local"

BACKEND_PID_FILE="$RUN_DIR/backend.pid"
FRONTEND_PID_FILE="$RUN_DIR/frontend.pid"
BACKEND_LOG="$RUN_DIR/backend.log"
FRONTEND_LOG="$RUN_DIR/frontend.log"

BACKEND_PORT="${BACKEND_PORT:-8000}"

# --- pick a python interpreter for the backend ----------------------------
REELS_PY="/opt/anaconda3/envs/reels/bin/python"
if [[ -n "${PYTHON_BIN:-}" ]]; then
  PY="$PYTHON_BIN"
elif [[ -x "$REELS_PY" ]]; then
  PY="$REELS_PY"
else
  PY="python3"
fi

is_running() {
  # $1 = pid file
  [[ -f "$1" ]] && kill -0 "$(cat "$1")" 2>/dev/null
}

start() {
  mkdir -p "$RUN_DIR"

  if [[ ! -f "$BACKEND_DIR/.env" ]]; then
    echo "Warning: backend/.env not found. Copy backend/.env.example and fill in CONNSTR / API keys." >&2
  fi

  if [[ ! -d "$FRONTEND_DIR/node_modules" ]]; then
    echo "Frontend dependencies not installed yet — running npm install..."
    (cd "$FRONTEND_DIR" && npm install)
  fi

  if is_running "$BACKEND_PID_FILE"; then
    echo "Backend already running (pid $(cat "$BACKEND_PID_FILE"))."
  else
    (
      cd "$BACKEND_DIR"
      exec "$PY" -m uvicorn app.main:app --reload --port "$BACKEND_PORT"
    ) >"$BACKEND_LOG" 2>&1 &
    disown
    echo $! > "$BACKEND_PID_FILE"
    echo "Backend started  (pid $!) -> $BACKEND_LOG"
  fi

  if is_running "$FRONTEND_PID_FILE"; then
    echo "Frontend already running (pid $(cat "$FRONTEND_PID_FILE"))."
  else
    (
      cd "$FRONTEND_DIR"
      # Run the vite binary directly rather than "npm run dev": npm would be an
      # extra wrapper process between this script and vite, which makes a clean
      # `kill <pid>` on stop unreliable.
      exec ./node_modules/.bin/vite
    ) >"$FRONTEND_LOG" 2>&1 &
    disown
    echo $! > "$FRONTEND_PID_FILE"
    echo "Frontend started (pid $!) -> $FRONTEND_LOG"
  fi

  echo
  echo "Backend:  http://127.0.0.1:$BACKEND_PORT"
  echo "Frontend: http://localhost:5173"
  echo "Logs:     tail -f $BACKEND_LOG $FRONTEND_LOG"
  echo "Stop with: ./run_local.sh stop"
}

stop_one() {
  local pid_file="$1" label="$2"
  if is_running "$pid_file"; then
    local pid
    pid="$(cat "$pid_file")"
    kill "$pid" 2>/dev/null
    for _ in 1 2 3 4 5; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "$label (pid $pid) did not stop in time, forcing..."
      kill -9 "$pid" 2>/dev/null
    fi
    echo "$label stopped."
  else
    echo "$label is not running."
  fi
  rm -f "$pid_file"
}

stop() {
  stop_one "$BACKEND_PID_FILE" "Backend"
  stop_one "$FRONTEND_PID_FILE" "Frontend"
}

status() {
  if is_running "$BACKEND_PID_FILE"; then
    echo "Backend:  running (pid $(cat "$BACKEND_PID_FILE"))"
  else
    echo "Backend:  stopped"
  fi
  if is_running "$FRONTEND_PID_FILE"; then
    echo "Frontend: running (pid $(cat "$FRONTEND_PID_FILE"))"
  else
    echo "Frontend: stopped"
  fi
}

case "${1:-}" in
  start)   start ;;
  stop)    stop ;;
  status)  status ;;
  restart) stop; start ;;
  *)
    echo "Usage: $0 {start|stop|status|restart}" >&2
    exit 1
    ;;
esac
