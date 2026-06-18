#!/usr/bin/env bash
# BonsAI IDE — start the IDE server
# Usage: bash start.sh [workspace_dir] [--port PORT]
#   --bg   run in background (survives terminal close, logs to logs/ide.log)
#   --stop stop a running background instance
#
# Defaults: workspace = $HOME, port = 3000

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$SCRIPT_DIR/logs/ide.pid"
LOGFILE="$SCRIPT_DIR/logs/ide.log"
WORKSPACE="${1:-$HOME}"
PORT="${BONSAI_IDE_PORT:-3000}"

mkdir -p "$SCRIPT_DIR/logs"

# --stop flag
if [[ "${1:-}" == "--stop" ]]; then
    if [[ -f "$PIDFILE" ]]; then
        PID="$(cat "$PIDFILE")"
        kill "$PID" 2>/dev/null && echo "Stopped IDE server (pid $PID)" || echo "Already stopped"
        rm -f "$PIDFILE"
    else
        echo "No PID file found — server may not be running"
    fi
    exit 0
fi

# --bg flag (background mode)
if [[ "${*}" == *"--bg"* ]]; then
    nohup python3 "$SCRIPT_DIR/server.py" "$WORKSPACE" --port "$PORT" \
        > "$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
    echo "BonsAI IDE started in background (pid $(cat "$PIDFILE"))"
    echo "  Open:  http://127.0.0.1:$PORT"
    echo "  Log:   $LOGFILE"
    echo "  Stop:  bash $SCRIPT_DIR/start.sh --stop"
    exit 0
fi

# foreground (default)
echo "BonsAI IDE  →  http://127.0.0.1:$PORT"
echo "Workspace   →  $WORKSPACE"
echo "Ctrl-C to stop. Use --bg to run in background."
exec python3 "$SCRIPT_DIR/server.py" "$WORKSPACE" --port "$PORT"
