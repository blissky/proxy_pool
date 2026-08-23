#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="${SCRIPT_DIR}/proxy_pool.pid"
PYTHON="${PYTHON:-python}"

is_running() {
    kill -0 "$1" 2>/dev/null
}

start_service() {
    local foreground=false
    if [[ "${1:-}" == "--fg" || "${1:-}" == "--foreground" ]]; then
        foreground=true
    fi
    cd "$SCRIPT_DIR"
    if [[ -f "$PID_FILE" ]]; then
        while read -r pid; do
            if [[ -n "$pid" ]] && is_running "$pid"; then
                echo "ProxyPool is already running (PID: $pid)"
                return 1
            fi
        done < "$PID_FILE"
        rm -f "$PID_FILE"
    fi

    if [[ "${START_REDIS:-1}" == "1" ]] && command -v redis-server >/dev/null 2>&1; then
        redis_dir="${REDIS_DIR:-${DATA_DIR:-${SCRIPT_DIR}/data}}"
        mkdir -p "$redis_dir"
        redis-server --bind 127.0.0.1 --port "${REDIS_PORT:-6379}" \
            --dir "$redis_dir" --save "" --appendonly no &
        REDIS_PID=$!
        sleep 1
    else
        REDIS_PID=""
    fi

    "$PYTHON" proxy_service.py \
        --listen "${PROXY_LISTEN:-0.0.0.0}" \
        --port "${PROXY_PORT:-8082}" \
        --stats-port "${STATS_PORT:-8083}" &
    SERVICE_PID=$!
    echo "$REDIS_PID $SERVICE_PID" > "$PID_FILE"

    cleanup() {
        trap - EXIT INT TERM
        kill "$SERVICE_PID" "$REDIS_PID" 2>/dev/null || true
        wait "$SERVICE_PID" "$REDIS_PID" 2>/dev/null || true
        rm -f "$PID_FILE"
    }
    trap cleanup EXIT INT TERM

    if [[ "$foreground" == "true" ]]; then
        if [[ -n "$REDIS_PID" ]]; then
            wait -n "$SERVICE_PID" "$REDIS_PID"
        else
            wait "$SERVICE_PID"
        fi
    else
        echo "ProxyPool started (service PID: $SERVICE_PID)"
        trap - EXIT INT TERM
    fi
}

stop_service() {
    if [[ ! -f "$PID_FILE" ]]; then
        echo "ProxyPool is not running"
        return 0
    fi
    while read -r redis_pid service_pid; do
        for pid in "$service_pid" "$redis_pid"; do
            if [[ -n "$pid" ]] && is_running "$pid"; then
                kill "$pid" 2>/dev/null || true
            fi
        done
    done < "$PID_FILE"
    rm -f "$PID_FILE"
}

status_service() {
    if [[ ! -f "$PID_FILE" ]]; then
        echo "ProxyPool is not running"
        return 0
    fi
    while read -r redis_pid service_pid; do
        echo "service: $service_pid $(is_running "$service_pid" && echo running || echo stopped)"
        if [[ -n "$redis_pid" ]]; then
            echo "redis: $redis_pid $(is_running "$redis_pid" && echo running || echo stopped)"
        fi
    done < "$PID_FILE"
}

case "${1:-help}" in
    start) shift; start_service "$@" ;;
    stop) stop_service ;;
    restart) stop_service; sleep 1; shift; start_service "$@" ;;
    status) status_service ;;
    *) echo "Usage: $0 {start [--fg]|stop|restart [--fg]|status}" ;;
esac
