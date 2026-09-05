#!/usr/bin/env bash
# Get one session's videos out of Google Drive and into its folder here.
#
#   ./fetch_session.sh 20260811_033622          # via rclone, if you have the remote
#   ./fetch_session.sh 20260811_033622 ~/Downloads/session_20260811_033622.zip
#
# The second form is the one to use if you do not have rclone configured:
# download the zip from the shared `beefline_records` Drive folder by hand and
# give this script the path. It unpacks to the same place either way, which is
# the point — the layout the repository documents does not depend on how the
# bytes arrived.
set -euo pipefail
cd "$(dirname "$0")"

STAMP="${1:-}"
ZIP="${2:-}"
[ -n "$STAMP" ] || { echo "usage: $0 <stamp> [local-zip]"; exit 1; }
STAMP="${STAMP#session_}"
DEST="session_${STAMP}"
REMOTE="${RCLONE_REMOTE:-gdrive}:beefline_records/session_${STAMP}.zip"

mkdir -p "$DEST"
if [ -z "$ZIP" ]; then
  command -v rclone >/dev/null || {
    echo "rclone is not installed and no local zip was given."
    echo "Download session_${STAMP}.zip from the shared beefline_records Drive folder, then:"
    echo "  $0 $STAMP /path/to/session_${STAMP}.zip"
    exit 1; }
  ZIP="$DEST/.session.zip"
  echo "fetching $REMOTE"
  rclone copyto "$REMOTE" "$ZIP" --progress
  CLEANUP=1
fi

echo "unpacking into $DEST/"
# -j flattens: the zips are made flat, but a zip somebody rebuilt with a
# leading directory would otherwise nest the videos one level too deep and
# every path in session.json would be wrong.
unzip -o -j "$ZIP" '*.mp4' -d "$DEST"
[ "${CLEANUP:-0}" = 1 ] && rm -f "$ZIP"

echo
ls -la "$DEST"/*.mp4
echo
echo "Next: these are HEVC, which no browser decodes. Build the capture and then"
echo "run Prepare video (Capture workspace -> Streams), or the cells stay black."
echo "The SAM annotations are already in $DEST/sam/ - see README.md."
