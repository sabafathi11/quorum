#!/usr/bin/env bash
# Dev server control that does not kill the caller's own shell: a pidfile,
# never `pkill -f quorum`. Usage: ./dev.sh start|stop|restart|log
set -euo pipefail
cd "$(dirname "$0")"
PID=.dev.pid
LOG=data/dev.log
case "${1:-restart}" in
  start)
    if [ -f $PID ] && kill -0 "$(cat $PID)" 2>/dev/null; then echo "already up ($(cat $PID))"; exit 0; fi
    mkdir -p data
    .venv/bin/python -m quorum serve "${@:2}" >$LOG 2>&1 &
    echo $! > $PID
    for _ in $(seq 40); do sleep .25; curl -sf http://127.0.0.1:8600/health >/dev/null && break; done
    curl -sf http://127.0.0.1:8600/health || { echo "did not come up:"; tail -20 $LOG; exit 1; }
    echo " ← pid $(cat $PID)" ;;
  stop)
    [ -f $PID ] && kill "$(cat $PID)" 2>/dev/null || true
    rm -f $PID; sleep 0.6 ;;
  restart) "$0" stop; "$0" start "${@:2}" ;;
  log) tail -n "${2:-40}" $LOG ;;
esac
