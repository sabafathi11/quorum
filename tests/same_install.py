"""Is the server the browser suite is about to edit *this* checkout's server?

The browser suite makes real edits: it deletes a track and undoes it, moves a
clock and moves it back, creates a mask through SAM. That is the point — those
are the paths worth testing — and it is safe exactly as long as the server on
the other end is the disposable one in `data/`.

`tests/run.sh` has always carried a comment warning about the way that stops
being true:

    Point 8600 somewhere else for a minute — an ssh forward to the lab box,
    say — and a plain `tests/run.sh` would write test ops into production and
    delete them again.

It warned and did not check, and then it happened. An `ssh vm.3090` session
with a `LocalForward 8600` in `~/.ssh/config` owns 127.0.0.1:8600, so:

  · `./dev.sh start` cannot bind the port and dies;
  · its readiness probe curls 8600, the *forward* answers, and it reports
    success;
  · the whole suite runs against the lab box, editing production;
  · and every failure it reports is about production's dataset rather than
    about the code, so the run is worse than useless — it is misleading.

That last part is the real cost. Eight checks failed, all of them explicable by
the remote capture having no identities, and the one genuine bug hiding among
them looked like more of the same.

So: prove the server is backed by the database this checkout would use, by
asking both for their captures and comparing. Cheap, portable, and it fails on
the thing that actually matters — writing somewhere unexpected — rather than on
a proxy for it like a port number.

Exit 0 if they match, 1 if they do not. `QUORUM_ALLOW_FOREIGN=1` turns the
refusal into a warning, for the rare case where pointing the suite at another
machine is what you meant.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quorum.config import Config          # noqa: E402


def local_captures(db_path: Path) -> list[tuple[int, str]]:
    if not db_path.exists():
        return []
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return [(int(i), str(k)) for i, k in
                con.execute("SELECT id, key FROM captures ORDER BY id")]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def served_captures(url: str) -> list[tuple[int, str]]:
    req = urllib.request.Request(f"{url.rstrip('/')}/api/captures")
    with urllib.request.urlopen(req, timeout=15) as r:
        body = json.loads(r.read().decode())
    return [(int(c["id"]), str(c["key"])) for c in body.get("captures", [])]


def who_is_listening(url: str) -> str:
    """Name the process holding the port, when the platform will say.

    Not load-bearing — the comparison above is the check. But "that port is an
    ssh forward" is the sentence that ends the investigation, and printing it
    costs one subprocess that is allowed to fail.
    """
    host_port = url.split("//", 1)[-1]
    port = host_port.rsplit(":", 1)[-1].split("/")[0]
    if not port.isdigit():
        return ""
    try:
        out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in out.splitlines():
        if f":{port} " in line and "users:" in line:
            return line.split("users:", 1)[1].strip()
    return ""


def main(argv):
    url = (argv[1] if len(argv) > 1 else "").strip() or "http://127.0.0.1:8600"
    cfg = Config.load()
    mine = local_captures(cfg.db_path)
    try:
        theirs = served_captures(url)
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        print(f"could not ask {url} what captures it has: {e}")
        return 1

    if mine == theirs:
        return 0

    lenient = os.environ.get("QUORUM_ALLOW_FOREIGN") == "1"
    print()
    print(f"   the server at {url} is NOT this checkout's server.")
    print(f"   {cfg.db_path} has: {mine or '(no captures)'}")
    print(f"   {url} serves:      {theirs or '(no captures)'}")
    listener = who_is_listening(url)
    if listener:
        print(f"   the port is held by: {listener}")
        if "ssh" in listener:
            print("   — that is an ssh forward. Something else is answering for it,")
            print("     and the browser suite MAKES EDITS, so this would write into")
            print("     whatever is on the far end. Check ~/.ssh/config for a")
            print("     LocalForward, or run the suite on another port:")
            print("       .venv/bin/python -m quorum serve --port 8611")
            print("       QUORUM_URL=http://127.0.0.1:8611 tests/run.sh")
    if lenient:
        print("   QUORUM_ALLOW_FOREIGN=1 is set — continuing anyway.")
        return 0
    print("   refusing to run. Set QUORUM_ALLOW_FOREIGN=1 if this is deliberate.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
