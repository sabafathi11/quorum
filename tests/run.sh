#!/usr/bin/env bash
# The whole suite, in one command.
#
#   tests/run.sh              unit + property + browser   (~35 s)
#   tests/run.sh --mutants    the above, then check the tests can actually fail (~60 s)
#   tests/run.sh --quick      python only, no server needed (~2 s)
#
# There is no `--docker` suite any more: the auto-annotator stopped starting a
# container per run (it runs inside the SAM service, on the one model), so what
# there is to check is a protocol, and `test_service.py` checks it against a
# stub in the ordinary suite.
#
# The browser tests need the server; it is started here if it is not already up
# and left exactly as it was found.
set -uo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
[ -x "$PY" ] || { echo "no .venv — see README.md (Setup)"; exit 1; }

# Before anything else, and in a second: is the tree what it should be?
# `mutants.py` edits real source files, and a run that is killed — or one that
# raced another — can leave a load-bearing line broken on disk. It then shows
# up as an unrelated failing test somewhere else, which is a long way from the
# file that caused it. Twice now.
if ! $PY tests/mutants.py --verify >/tmp/quorum-verify.$$ 2>&1; then
  cat /tmp/quorum-verify.$$; rm -f /tmp/quorum-verify.$$
  echo
  echo "the working tree is not what tests/mutants.py expects — see above."
  echo "if that is a leftover mutation, put the line back before trusting any result."
  exit 1
fi
rm -f /tmp/quorum-verify.$$

fail=0
run() {                       # run <label> <cmd...>
  local label=$1; shift
  echo
  echo "── $label"
  if "$@"; then :; else fail=1; echo "   ^ $label FAILED"; fi
}

run "unit — core: folds, RLE, op log, doc store" $PY tests/test_core.py
run "unit — mask editing: cuts, joins, undo, the effective set" $PY tests/test_masks.py
run "unit — SAM: the codec, the exact frame, one-op propagation" $PY tests/test_sam.py
run "unit — SAM service: the auto protocol over HTTP" $PY tests/test_service.py
run "unit — identity: the Shift+Space walk, per layer" $PY tests/test_identity.py
run "property — random edit sessions against every invariant" $PY tests/test_props.py

if [ "${1:-}" = "--quick" ]; then
  echo; [ $fail = 0 ] && echo "python suites passed" || echo "SOMETHING FAILED"; exit $fail
fi

# ---- browser -----------------------------------------------------------------
# $QUORUM_URL, not a hard-coded 8600, and it matters more than it looks: the
# browser suite *makes edits*. Point 8600 somewhere else for a minute — an ssh
# forward to the lab box, say — and a plain `tests/run.sh` would write test ops
# into production and delete them again. Set QUORUM_URL and it uses that;
# leave it and it uses, or starts, the local dev server.
#
# That paragraph was a warning for months and then it happened, because a
# warning is not a check: an `ssh vm.3090` holding a LocalForward on 8600 stops
# `dev.sh` binding the port, answers its readiness probe *for* it, and the whole
# suite runs against the lab box. So it is verified now — `same_install.py`,
# below, compares the served captures against this checkout's database and
# refuses if they differ.
export QUORUM_URL="${QUORUM_URL:-http://127.0.0.1:8600}"
echo
echo "── browser tests will use $QUORUM_URL"
started=0
if ! curl -sf "$QUORUM_URL/health" >/dev/null 2>&1; then
  if [ "$QUORUM_URL" != "http://127.0.0.1:8600" ]; then
    echo "   nothing is answering there, and it is not the dev server — start it yourself"
    exit 1
  fi
  echo "── starting a server for the browser tests"
  ./dev.sh start >/dev/null 2>&1 || { echo "   could not start it"; exit 1; }
  started=1
fi

# …and is it *ours*? `dev.sh` reports success when anything answers on 8600,
# which includes an ssh forward that stopped it binding the port in the first
# place. The suite edits whatever is on the other end, so this is checked every
# time — including when we just started it, because that is exactly the case
# that lied. See tests/same_install.py for the whole story.
if ! $PY tests/same_install.py "$QUORUM_URL"; then
  [ $started = 1 ] && ./dev.sh stop >/dev/null 2>&1
  exit 1
fi
[ -d tests/node_modules ] || (cd tests && npm i >/dev/null 2>&1)
run "browser — the real client in jsdom, plus whole-dataset invariants" node tests/smoke.mjs

# ---- do the tests have teeth? ------------------------------------------------
# Runs before the server is stopped: the client mutations need it up.
if [ "${1:-}" = "--mutants" ]; then
  run "mutation — break load-bearing lines and check the suite notices" $PY tests/mutants.py
fi

[ $started = 1 ] && ./dev.sh stop >/dev/null 2>&1

echo
if [ $fail = 0 ]; then echo "all suites passed"; else echo "SOMETHING FAILED — see above"; fi
exit $fail
