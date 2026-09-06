#!/usr/bin/env bash
# Bring the SAM service back up. Run this ON the GPU box.
#
#   ssh vm.3090 ./sam-up.sh
#   ./sam-up.sh --watch          keep checking every 30 s and restart if it drops
#
# There is one model now, in one container, and everything that segments
# anything goes through it: the interactor, the tracker propagation and the
# text-prompt auto-annotator. So this is the one process to check when nothing
# in Quorum's SAM workspace works.
#
# It is `restart: unless-stopped`, so the daemon puts it back after a crash.
# The cases this covers are the ones it does not: somebody stopped it by hand,
# the daemon came up with it stopped, or it is running but wedged. It also
# prints the two numbers you actually want on a shared card — how much VRAM the
# model is holding, and how much is left for a run.
set -uo pipefail

NAME="${SAM_CONTAINER:-sam-service}"
COMPOSE_DIR="${SAM_COMPOSE_DIR:-$HOME/annotation/quorum/plugins/sam/service}"
PORT="${SAM_PORT:-32950}"
WAIT="${SAM_WAIT:-300}"          # the model takes ~10 s, but a cold page cache is slower

say()  { printf '%s\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
bad()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }
ok()   { printf '\033[32m%s\033[0m\n' "$*"; }

exists()  { docker inspect "$NAME" >/dev/null 2>&1; }
running() { [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" = "true" ]; }

# The address it is published on, which is not always loopback. The compose
# file publishes on one address — $SAM_BIND — so setting that to the tailnet,
# which is what lets an annotator's laptop reach /health at all, takes the
# loopback binding away. Probing 127.0.0.1 regardless then calls a perfectly
# healthy service wedged, and under --watch restarts it every 30 s forever.
# Ask docker where it actually is instead.
addr() {
  if [ -n "${SAM_HOST:-}" ]; then printf '%s' "$SAM_HOST"; return; fi
  docker port "$NAME" 8080/tcp 2>/dev/null | head -1 | cut -d: -f1 | grep . \
    || printf '127.0.0.1'
}

answers() { curl -sf -m 8 "http://$(addr):${PORT}/health" >/dev/null 2>&1; }

health() {
  curl -s -m 8 "http://$(addr):${PORT}/health" 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
v = d.get("vram") or {}
if v:
    print("  GPU: %d MiB used of %d — %d MiB free" % (v["used"], v["total"], v["free"]))
print("  busy: %s%s" % (d.get("busy"), "  job " + d["job"] if d.get("job") else ""))
print("  max objects per category: %s" % d.get("max_objects"))'
  # what this container specifically is holding, which is the model
  local cid; cid="$(docker inspect -f '{{.Id}}' "$NAME" 2>/dev/null | cut -c1-12)"
  [ -n "$cid" ] || return 0
  command -v nvidia-smi >/dev/null || return 0
  for pid in $(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null); do
    if grep -qs "$cid" "/proc/$pid/cgroup" 2>/dev/null; then
      local mem
      mem="$(nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits \
             | awk -F', *' -v p="$pid" '$1==p {print $2}')"
      say "  $NAME is holding ${mem} MiB of it"
    fi
  done
}

wait_for() {
  local n=0
  printf '  loading the model'
  while [ "$n" -lt "$WAIT" ]; do
    if answers; then printf '\n'; ok "$NAME is answering on $(addr):${PORT}."; return 0; fi
    running || { printf '\n'; bad "$NAME exited while starting:"; \
                 docker logs --tail 25 "$NAME" 2>&1 | sed 's/^/    /'; return 1; }
    printf '.'
    sleep 3
    n=$((n + 3))
  done
  printf '\n'
  bad "$NAME did not answer within ${WAIT}s. Last lines:"
  docker logs --tail 25 "$NAME" 2>&1 | sed 's/^/    /'
  return 1
}

bring_up() {
  if ! exists; then
    warn "$NAME does not exist. Creating it from $COMPOSE_DIR."
    if [ ! -f "$COMPOSE_DIR/docker-compose.yml" ]; then
      bad "no compose file at $COMPOSE_DIR — set SAM_COMPOSE_DIR."
      return 2
    fi
    ( cd "$COMPOSE_DIR" && docker compose up -d --build ) || { bad "compose failed"; return 1; }
    wait_for || return 1
  elif running; then
    if answers; then
      ok "$NAME is up and answering on $(addr):${PORT}."
    else
      # Not answering is not the same as wedged: the model takes ~10 s to load
      # and this script is often run *because* somebody just started it. Give it
      # the wait first; restarting a container that is merely loading throws
      # away the load and, on a --watch loop, does it forever.
      warn "$NAME is running but not answering yet — waiting."
      if ! wait_for; then
        warn "still nothing after ${WAIT}s — restarting it."
        docker restart "$NAME" >/dev/null || { bad "could not restart it"; return 1; }
        wait_for || return 1
      fi
    fi
  else
    warn "$NAME is down. Starting it."
    docker start "$NAME" >/dev/null || { bad "could not start it"; return 1; }
    wait_for || return 1
  fi
  return 0
}

if [ "${1:-}" = "--watch" ]; then
  say "watching $NAME every 30 s — Ctrl-C to stop"
  while true; do
    if ! running || ! answers; then
      say "[$(date '+%F %T')] it is not answering"
      bring_up
      health
    fi
    sleep 30
  done
fi

bring_up
rc=$?
health
exit $rc
