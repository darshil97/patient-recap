#!/usr/bin/env bash
#
# run_local.sh — start/stop the backend (FastAPI/uvicorn) and frontend (Next.js)
# dev servers in the background. `start` launches both and returns the terminal
# immediately; `stop` tears both down.
#
# Usage:
#   ./run_local.sh start
#   ./run_local.sh stop
#   ./run_local.sh status
#   ./run_local.sh restart
#   ./run_local.sh logs        # tail both logs
#
# Env overrides:
#   BACKEND_PORT   port for uvicorn (default 8000)
#   FRONTEND_PORT  port for Next (default 3000)
#   PYTHON_BIN     python interpreter for the backend
#                  (defaults to the 'reels' conda env if present, else python3)
#
# The frontend is served over HTTPS, not HTTP, and that is not a preference:
# Neon Auth's session-challenge cookie is __Secure- prefixed, and Safari refuses
# to store Secure cookies over plain HTTP even on localhost. Over http:// the
# Google sign-in callback fails with no visible error and nothing in either log.
# See frontend/README.md.

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
# The Next.js project root. NOT frontend/app — that is the App Router source
# directory now (it used to be the Vite project root, which is why this once
# pointed there; running npm install against it would litter frontend/app).
FRONTEND_DIR="$ROOT_DIR/frontend"
CERT_DIR="$FRONTEND_DIR/certificates"
RUN_DIR="$ROOT_DIR/.run_local"

BACKEND_PID_FILE="$RUN_DIR/backend.pid"
FRONTEND_PID_FILE="$RUN_DIR/frontend.pid"
BACKEND_LOG="$RUN_DIR/backend.log"
FRONTEND_LOG="$RUN_DIR/frontend.log"

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-3000}"

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

port_pids() {
  # Anything listening on $1. Used as a stop fallback: `npm run dev` sits between
  # this script and the real server, so killing the recorded pid does not always
  # reap the child. Whatever still holds the port is what actually matters.
  lsof -nP -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null || true
}

# --- preflight -------------------------------------------------------------
preflight() {
  local problems=0

  if [[ ! -f "$BACKEND_DIR/.env" ]]; then
    echo "  ! backend/.env not found. Copy backend/.env.example and fill in CONNSTR." >&2
    problems=1
  fi

  if [[ ! -f "$FRONTEND_DIR/.env.local" ]]; then
    echo "  ! frontend/.env.local not found. Copy frontend/.env.local.example." >&2
    problems=1
  fi

  # The single most confusing local failure: if these disagree, every screen 401s
  # in a way that looks like a session bug. Worth catching before startup.
  if [[ -f "$BACKEND_DIR/.env" && -f "$FRONTEND_DIR/.env.local" ]]; then
    local bk fk
    bk="$(grep -m1 '^API_KEY=' "$BACKEND_DIR/.env" 2>/dev/null | cut -d= -f2-)"
    fk="$(grep -m1 '^BACKEND_API_KEY=' "$FRONTEND_DIR/.env.local" 2>/dev/null | cut -d= -f2-)"
    if [[ -n "$bk" && "$bk" != "$fk" ]]; then
      echo "  ! backend API_KEY and frontend BACKEND_API_KEY differ." >&2
      echo "    Every screen will 401 and it will look like a session problem." >&2
      problems=1
    fi
  fi

  if [[ ! -d "$FRONTEND_DIR/node_modules" ]]; then
    echo "  Frontend dependencies not installed — running npm install..."
    (cd "$FRONTEND_DIR" && npm install) || return 1
  fi

  # The dev server needs a cert. Generate a self-signed one if it is missing,
  # rather than falling back to HTTP (see the header comment for why that breaks
  # sign-in). `npm run dev:mkcert` gets a *trusted* cert but needs sudo, so it
  # cannot be done unattended.
  if [[ ! -f "$CERT_DIR/localhost.pem" || ! -f "$CERT_DIR/localhost-key.pem" ]]; then
    echo "  Dev certificate missing — generating a self-signed one..."
    mkdir -p "$CERT_DIR"
    openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes \
      -keyout "$CERT_DIR/localhost-key.pem" \
      -out "$CERT_DIR/localhost.pem" \
      -subj "/CN=localhost/O=Consultation Insights local dev" \
      -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1" 2>/dev/null \
      || { echo "  ! openssl failed; run 'npm run dev:mkcert' in frontend/ instead." >&2
           return 1; }
    echo "  Wrote $CERT_DIR/localhost.pem (self-signed, gitignored)."
  fi

  return $problems
}

start() {
  mkdir -p "$RUN_DIR"

  echo "Preflight:"
  if preflight; then
    echo "  ok"
  else
    echo "  (continuing anyway — fix the above if something misbehaves)"
  fi
  echo

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
      # `npm run dev`, not the next binary directly. This used to invoke the
      # binary to avoid npm sitting in between and making `kill <pid>` unreliable
      # — but the dev script now carries the --experimental-https* flags, and
      # duplicating those here would leave two places to keep in sync. stop()
      # falls back to killing whatever holds the port, which handles the wrapper.
      exec npm run dev -- --port "$FRONTEND_PORT"
    ) >"$FRONTEND_LOG" 2>&1 &
    disown
    echo $! > "$FRONTEND_PID_FILE"
    echo "Frontend started (pid $!) -> $FRONTEND_LOG"
  fi

  echo
  echo "Backend:  http://127.0.0.1:$BACKEND_PORT      (docs at /docs)"
  echo "Frontend: https://localhost:$FRONTEND_PORT     <- note the https"
  echo
  echo "The certificate is self-signed, so the browser warns once per origin:"
  echo "  Safari:  Show Details -> visit this website"
  echo "  Chrome:  Advanced -> Proceed to localhost"
  echo
  echo "Logs:      ./run_local.sh logs"
  echo "Stop with: ./run_local.sh stop"
}

stop_one() {
  local pid_file="$1" label="$2" port="$3"

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
    echo "$label is not running (no live pid file)."
  fi
  rm -f "$pid_file"

  # The recorded pid may have been a wrapper (npm, or uvicorn's reloader), so its
  # child can outlive it and keep the port bound — which then makes the next
  # `start` silently attach to a stale server. Reap by port to be sure.
  local leftover
  leftover="$(port_pids "$port")"
  if [[ -n "$leftover" ]]; then
    echo "  port $port still held by pid(s): $(echo "$leftover" | tr '\n' ' ')— stopping"
    # shellcheck disable=SC2086
    kill $leftover 2>/dev/null
    sleep 1
    leftover="$(port_pids "$port")"
    # shellcheck disable=SC2086
    [[ -n "$leftover" ]] && kill -9 $leftover 2>/dev/null
  fi
}

stop() {
  stop_one "$BACKEND_PID_FILE" "Backend" "$BACKEND_PORT"
  stop_one "$FRONTEND_PID_FILE" "Frontend" "$FRONTEND_PORT"
}

status() {
  local pids

  if is_running "$BACKEND_PID_FILE"; then
    echo "Backend:  running (pid $(cat "$BACKEND_PID_FILE"))  http://127.0.0.1:$BACKEND_PORT"
  else
    pids="$(port_pids "$BACKEND_PORT")"
    if [[ -n "$pids" ]]; then
      echo "Backend:  port $BACKEND_PORT held by pid(s) $(echo "$pids" | tr '\n' ' ')but no pid file"
    else
      echo "Backend:  stopped"
    fi
  fi

  if is_running "$FRONTEND_PID_FILE"; then
    echo "Frontend: running (pid $(cat "$FRONTEND_PID_FILE"))  https://localhost:$FRONTEND_PORT"
  else
    pids="$(port_pids "$FRONTEND_PORT")"
    if [[ -n "$pids" ]]; then
      echo "Frontend: port $FRONTEND_PORT held by pid(s) $(echo "$pids" | tr '\n' ' ')but no pid file"
    else
      echo "Frontend: stopped"
    fi
  fi
}

logs() {
  mkdir -p "$RUN_DIR"
  touch "$BACKEND_LOG" "$FRONTEND_LOG"
  tail -f "$BACKEND_LOG" "$FRONTEND_LOG"
}

case "${1:-}" in
  start)   start ;;
  stop)    stop ;;
  status)  status ;;
  restart) stop; start ;;
  logs)    logs ;;
  *)
    echo "Usage: $0 {start|stop|status|restart|logs}" >&2
    exit 1
    ;;
esac
