#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
API_HOST="${API_HOST:-127.0.0.1}"
API_PORT="${API_PORT:-8787}"
WEB_HOST="${WEB_HOST:-127.0.0.1}"
WEB_PORT="${WEB_PORT:-5500}"
REUSE_SERVERS="${REUSE_SERVERS:-0}"
API_TXT_PATH="${API_TXT_PATH:-$ROOT_DIR/api.txt}"
API_STARTED=0
WEB_STARTED=0

load_api_config() {
  local file="$1"
  local raw_line
  local key
  local value

  [[ -f "$file" ]] || return 0

  while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
    # Trim CR in case file was edited on Windows.
    raw_line="${raw_line%$'\r'}"

    # Skip blank/commented lines.
    if [[ -z "${raw_line//[[:space:]]/}" ]] || [[ "${raw_line:0:1}" == "#" ]]; then
      continue
    fi

    if [[ "$raw_line" == *=* ]]; then
      key="${raw_line%%=*}"
      value="${raw_line#*=}"

      # Trim surrounding spaces.
      key="${key#"${key%%[![:space:]]*}"}"
      key="${key%"${key##*[![:space:]]}"}"
      value="${value#"${value%%[![:space:]]*}"}"
      value="${value%"${value##*[![:space:]]}"}"

      if [[ -n "$key" ]]; then
        export "$key=$value"
      fi
      continue
    fi

    # Allow one-line api.txt files that contain only a SerpAPI key.
    if [[ -z "${SERPAPI_KEY:-}" ]]; then
      export SERPAPI_KEY="$raw_line"
    fi
  done <"$file"
}

if [[ -f "$API_TXT_PATH" ]]; then
  load_api_config "$API_TXT_PATH"
fi

if [[ -z "${SERPAPI_KEY:-}" ]]; then
  if [[ -z "${AMADEUS_CLIENT_ID:-}" || -z "${AMADEUS_CLIENT_SECRET:-}" ]]; then
    echo "[warn] No provider credentials detected."
    echo "[warn] Create api.txt with AMADEUS_CLIENT_ID+AMADEUS_CLIENT_SECRET (recommended) or SERPAPI_KEY."
  fi
fi

cleanup() {
  local code=$?
  if [[ "$API_STARTED" == "1" ]] && [[ -n "${API_PID:-}" ]] && kill -0 "$API_PID" 2>/dev/null; then
    kill "$API_PID" 2>/dev/null || true
  fi
  if [[ "$WEB_STARTED" == "1" ]] && [[ -n "${WEB_PID:-}" ]] && kill -0 "$WEB_PID" 2>/dev/null; then
    kill "$WEB_PID" 2>/dev/null || true
  fi
  wait 2>/dev/null || true
  exit "$code"
}
trap cleanup INT TERM EXIT

kill_listener_on_port() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    local pids
    pids="$(lsof -ti tcp:"$port" 2>/dev/null || true)"
    if [[ -n "$pids" ]]; then
      echo "[info] Stopping process(es) on port $port: $pids"
      kill $pids 2>/dev/null || true
      sleep 1
    fi
  fi
}

if curl -fsS "http://${API_HOST}:${API_PORT}/health" >/dev/null 2>&1; then
  if [[ "$REUSE_SERVERS" == "1" ]]; then
    echo "[info] Reusing existing backend at http://${API_HOST}:${API_PORT}"
  else
    echo "[info] Existing backend detected on ${API_HOST}:${API_PORT}; restarting to apply current environment."
    kill_listener_on_port "$API_PORT"
    echo "[info] Starting backend at http://${API_HOST}:${API_PORT}"
    (
      cd "$ROOT_DIR"
      HOST="$API_HOST" PORT="$API_PORT" python3 server.py
    ) &
    API_PID=$!
    API_STARTED=1
  fi
else
  echo "[info] Starting backend at http://${API_HOST}:${API_PORT}"
  (
    cd "$ROOT_DIR"
    HOST="$API_HOST" PORT="$API_PORT" python3 server.py
  ) &
  API_PID=$!
  API_STARTED=1
fi

if curl -fsS "http://${WEB_HOST}:${WEB_PORT}/index.html" >/dev/null 2>&1; then
  if [[ "$REUSE_SERVERS" == "1" ]]; then
    echo "[info] Reusing existing frontend static server at http://${WEB_HOST}:${WEB_PORT}"
  else
    echo "[info] Existing frontend detected on ${WEB_HOST}:${WEB_PORT}; restarting."
    kill_listener_on_port "$WEB_PORT"
    echo "[info] Starting frontend static server at http://${WEB_HOST}:${WEB_PORT}"
    (
      cd "$ROOT_DIR"
      python3 -m http.server "$WEB_PORT" --bind "$WEB_HOST"
    ) &
    WEB_PID=$!
    WEB_STARTED=1
  fi
else
  echo "[info] Starting frontend static server at http://${WEB_HOST}:${WEB_PORT}"
  (
    cd "$ROOT_DIR"
    python3 -m http.server "$WEB_PORT" --bind "$WEB_HOST"
  ) &
  WEB_PID=$!
  WEB_STARTED=1
fi

sleep 1

if ! curl -fsS "http://${API_HOST}:${API_PORT}/health" >/dev/null 2>&1; then
  echo "[error] Backend is not reachable at http://${API_HOST}:${API_PORT}" >&2
  exit 1
fi

if ! curl -fsS "http://${WEB_HOST}:${WEB_PORT}/index.html" >/dev/null 2>&1; then
  echo "[error] Frontend static server is not reachable at http://${WEB_HOST}:${WEB_PORT}/index.html" >&2
  exit 1
fi

echo
echo "[ready] Open: http://${WEB_HOST}:${WEB_PORT}/index.html"
echo "[ready] In app, set Flight API endpoint to: http://${API_HOST}:${API_PORT}"
echo "[ready] Press Ctrl+C to stop both servers."
echo

wait
