#!/bin/sh
# Render the container's config, then get out of the way.
#
# It also runs as *you*, not as root — see the `user:` line in compose. The data
# directory is shared with `./run.sh` on the host, and a container writing root
# owned files into it breaks the host-side server in a way that takes a while to
# recognise: a cached still that ffmpeg cannot overwrite, a composition that
# retries, and a timeline repainting when it should not.

# Two paths are not knowable at build time and are load-bearing at run time:
# where the annotation tree is, and where the data directory is. Everything the
# container needs to be told follows from those two, so they are the only
# things this asks for — and it fails immediately if they are missing rather
# than starting a server that will be mysteriously unable to find any video.
set -eu

: "${TREE:?TREE must be the absolute path of the annotation tree, mounted at the same path inside this container}"
DATA="${DATA:-$TREE/quorum/data}"

# $QUORUM_CONFIG comes from the image (so `docker exec` inherits it and the CLI
# reads the same database the server does). A config *file* already sitting at
# that path was mounted there on purpose and wins outright; otherwise render it.
QUORUM_CONFIG="${QUORUM_CONFIG:-/tmp/quorum.toml}"
export QUORUM_CONFIG
if [ -f "$QUORUM_CONFIG" ]; then
  echo "quorum: using the config mounted at $QUORUM_CONFIG"
else
  # The camera videos are not always inside the tree. On the GPU box they are
  # under a root-only mount somewhere else, so $RECORDS adds a second
  # media root rather than forcing everything into one directory.
  ROOTS="\"${TREE}\""
  if [ -n "${RECORDS:-}" ]; then
    ROOTS="${ROOTS}, \"${RECORDS}\""
    [ -d "$RECORDS" ] || echo "quorum: warning — RECORDS=$RECORDS is not mounted here" >&2
  fi
  sed -e "s#@TREE@#${TREE}#g" -e "s#@DATA@#${DATA}#g" -e "s#@ROOTS@#${ROOTS}#g" \
      /srv/quorum/docker/quorum.docker.toml > "$QUORUM_CONFIG"
  echo "quorum: tree $TREE, data $DATA${RECORDS:+, records $RECORDS}"
fi

# The tree is mounted read-only on purpose (inputs are never written), so a
# missing data directory has to be reported rather than created into a
# read-only mount and failing later with something about sqlite.
[ -d "$TREE" ] || { echo "quorum: $TREE is not mounted in this container" >&2; exit 1; }
mkdir -p "$DATA" 2>/dev/null || true
[ -d "$DATA" ] || { echo "quorum: $DATA does not exist and cannot be created — mount it read-write" >&2; exit 1; }
[ -w "$DATA" ] || { echo "quorum: $DATA is not writable by uid $(id -u) — check the bind mount" >&2; exit 1; }

exec "$@"
