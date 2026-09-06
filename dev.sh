#!/usr/bin/env bash
# Dev server control that does not kill the caller's own shell: a pidfile,
# never `pkill -f quorum`. Usage: ./dev.sh start|stop|restart|log
set -euo pipefail
cd "$(dirname "$0")"
PID=.dev.pid
LOG=data/dev.log

# The interpreter. A .venv is the documented setup, but this box has no .venv
# and runs the server from a python3 that already has the deps — so hardcoding
# .venv/bin/python here fails instantly, and the health check below used to
# report success anyway. $QUORUM_PYTHON overrides both guesses.
PY="${QUORUM_PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
fi

# Where the server actually listens, read rather than assumed. Hardcoding
# 127.0.0.1:8600 is wrong on any box that binds a specific address: this one
# binds the tailnet, and a loopback probe lands on the *docker* quorum
# container instead, which answers a cheerful healthy for a server that is not
# the one we just started.
URL="$("$PY" - <<'EOF'
import tomllib, pathlib
try:
    d = tomllib.loads(pathlib.Path("quorum.toml").read_text())
except Exception:
    d = {}
print("http://%s:%s/health" % (d.get("host", "127.0.0.1"), d.get("port", 8600)))
EOF
)"

alive() { [ -f $PID ] && kill -0 "$(cat $PID)" 2>/dev/null; }

case "${1:-restart}" in
  start)
    if alive; then echo "already up ($(cat $PID))"; exit 0; fi
    mkdir -p data
    "$PY" -m quorum serve "${@:2}" >$LOG 2>&1 &
    echo $! > $PID
    for _ in $(seq 40); do
      sleep .25
      alive || break                      # it exited; nothing to wait for
      curl -sf "$URL" >/dev/null 2>&1 && break
    done
    # Liveness first, and separately from the health probe. A port answering is
    # not evidence that *our* process is the one answering it — that is exactly
    # how a missing interpreter got reported as a successful start.
    alive || { echo "it exited immediately:"; tail -20 $LOG; rm -f $PID; exit 1; }
    curl -sf "$URL" || { echo "started but is not answering on $URL:"; tail -20 $LOG; exit 1; }
    echo " ← pid $(cat $PID)  [$PY, $URL]" ;;
  stop)
    [ -f $PID ] && kill "$(cat $PID)" 2>/dev/null || true
    rm -f $PID; sleep 0.6 ;;
  restart) "$0" stop; "$0" start "${@:2}" ;;
  log) tail -n "${2:-40}" $LOG ;;
esac
