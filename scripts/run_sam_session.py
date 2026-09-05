#!/usr/bin/env python3
"""Run SAM over one session's recordings and leave the result in the repo.

This is the *producer* half of the handoff. It is run here, on the box with the
GPU, by whoever holds the session. The person who clones the repository runs
nothing of the sort: they get what this script leaves behind.

    scripts/run_sam_session.py 20260811_033622
    scripts/run_sam_session.py 20260811_033622 --prompts "person,hat" --frame-stride 5

Four steps, in order, each of which is already a thing Quorum can do — the
script's whole job is to do them to the same session in the same order and put
the output where `beefline_records/README.md` says it will be:

    1. ingest   the session's videos as a capture (`multiview.disk`), keyed by
                the stamp the filenames share
    2. annotate every stream (`sam.auto`), which is the hours-long part
    3. harvest  the per-camera CVAT XML out of `data/sam_auto/<capture id>/`
                into `beefline_records/session_<stamp>/sam/`
    4. manifest what is now there, so the next person can check they got it all

Step 3 is the one that matters for the handoff, and it is a copy rather than a
move on purpose: `data/` is this installation's working state and the run
should still be re-importable here after the files have been committed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RECORDS = REPO / "beefline_records"


def sh(args: list[str]) -> None:
    """Run a quorum CLI step, letting its progress reach the terminal.

    Deliberately not capturing stdout: `sam.auto` is hours long and prints a
    window at a time, and a script that swallowed that would leave somebody
    watching a blank terminal wondering whether the GPU was doing anything.
    """
    print(f"\n$ {' '.join(args)}\n", flush=True)
    r = subprocess.run(args)
    if r.returncode != 0:
        raise SystemExit(f"failed: {' '.join(args)}")


def python_bin() -> str:
    """The venv's interpreter, whichever platform made it."""
    for p in (REPO / ".venv" / "bin" / "python", REPO / ".venv" / "Scripts" / "python.exe"):
        if p.exists():
            return str(p)
    return sys.executable


def videos(session: Path, stamp: str) -> list[Path]:
    """The session's recordings, in camera order.

    Sorted by the numeric part of the key rather than by name, so `cam10` does
    not land between `cam1` and `cam2` the day there are ten cameras.
    """
    vids = sorted(session.glob(f"*_{stamp}.mp4"),
                  key=lambda p: (len(p.name.split("_")[0]), p.name))
    missing = [p.name for p in vids if not p.resolve().is_file()]
    if missing:
        raise SystemExit(
            f"{len(missing)} of the videos in {session} are dangling: {', '.join(missing)}.\n"
            f"They are symlinks to the records disk here; if it is not mounted, either mount "
            f"it or fetch the session with beefline_records/fetch_session.sh {stamp}.")
    if not vids:
        raise SystemExit(f"no videos matching *_{stamp}.mp4 in {session}")
    return vids


def capture_id(db_path: Path, key: str) -> int:
    """The capture the ingest just made or updated, looked up by its key.

    Asked of the database rather than scraped from the CLI's JSON: the key is
    the stamp, the provider makes that guarantee out loud, and parsing a
    truncated progress dump for an integer is the kind of thing that works
    until the day it does not.
    """
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT id FROM captures WHERE key=?", (key,)).fetchone()
    finally:
        con.close()
    if row is None:
        raise SystemExit(f"the ingest did not leave a capture keyed {key!r} in {db_path}")
    return int(row[0])


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while blk := f.read(chunk):
            h.update(blk)
    return h.hexdigest()


def probe(path: Path) -> dict:
    """Codec, geometry and duration, for the manifest.

    Recorded because it is the answer to the next person's first question --
    these are HEVC, which no browser decodes, so they will have to run
    `media.prepare` before a single cell shows a picture. Better to say so in
    the manifest than to let them find a grid of black squares.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height,nb_frames",
             "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60)
        d = json.loads(out.stdout or "{}")
        st = (d.get("streams") or [{}])[0]
        return {"codec": st.get("codec_name", ""),
                "width": int(st.get("width") or 0), "height": int(st.get("height") or 0),
                "frames": int(st.get("nb_frames") or 0),
                "duration": round(float((d.get("format") or {}).get("duration") or 0), 3)}
    except Exception:
        return {}


def harvest(data_dir: Path, cid: int, session: Path, sam_params: dict, hashes: bool) -> dict:
    """`data/sam_auto/<cid>/` -> `beefline_records/<session>/sam/`.

    Everything the run produced, including the freeze reports: a camera that
    fragmented into 136 tracks is not a mystery the next person should have to
    re-derive, and the report that explains it costs kilobytes.
    """
    src = data_dir / "sam_auto" / str(cid)
    if not src.is_dir():
        raise SystemExit(f"the job left nothing in {src}")
    dst = session / "sam"
    dst.mkdir(parents=True, exist_ok=True)

    files, partial = {}, []
    for f in sorted(src.iterdir()):
        if f.suffix not in (".xml", ".json") and not f.name.endswith(".xml.PARTIAL"):
            continue
        shutil.copy2(f, dst / f.name)
        rec = {"bytes": f.stat().st_size}
        if hashes:
            rec["sha256"] = sha256(f)
        files[f.name] = rec
        if f.name.endswith(".PARTIAL"):
            partial.append(f.name)

    if partial:
        print(f"\n!! {len(partial)} stream(s) only produced a PARTIAL xml: {', '.join(partial)}"
              f"\n   Those cameras failed part-way. Re-run with --force to redo them.")

    (dst / "run.json").write_text(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generator": "sam3_auto_annotator, via the quorum `sam.auto` job",
        "capture_id": cid,
        "params": sam_params,
        "files": files,
        "partial": partial,
        # One command for the whole session: the importer takes a directory,
        # globs `*.xml` and matches each file to a stream by the key in its
        # name, so the JSON sidecars here are ignored and a `.xml.PARTIAL` is
        # left out rather than half-imported.
        "import": ("quorum job cvat_xml.import:tracks capture_id=<id> "
                   "dir=beefline_records/session_<stamp>/sam "
                   "layer=sam_auto name='SAM auto' provenance=model"),
    }, indent=2) + "\n")
    return files


def write_manifest(session: Path, stamp: str, vids: list[Path]) -> dict:
    """`session.json`: what is in this folder and what it is.

    Written from what is actually on disk rather than from what the run
    believed it produced, so it stays honest when a camera failed, and so
    `--manifest-only` can refresh it without re-running anything.
    """
    sam_dir = session / "sam"
    sam = sorted(f.name for f in sam_dir.iterdir()) if sam_dir.is_dir() else []
    manifest = {
        "session": f"session_{stamp}",
        "stamp": stamp,
        "cameras": [{"key": v.name.split("_")[0], "file": v.name,
                     "bytes": v.resolve().stat().st_size, **probe(v)} for v in vids],
        "videos": {"in_git": False,
                   "drive": f"gdrive:beefline_records/session_{stamp}.zip",
                   "fetch": f"beefline_records/fetch_session.sh {stamp}"},
        "sam": {"dir": "sam/", "format": "CVAT Video 1.1, mask RLE, one file per camera",
                "files": sam} if sam else
               {"dir": "sam/", "state": "not run yet"},
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (session / "session.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stamp", help="the session stamp, e.g. 20260811_033622")
    ap.add_argument("--prompts", default="person",
                    help="text prompts, comma separated (default: person)")
    ap.add_argument("--labels", default="", help="one label per prompt; blank reuses the prompt")
    ap.add_argument("--streams", default="", help="only these stream keys, e.g. cam1,cam2")
    ap.add_argument("--frame-stride", type=int, default=5, help="process every Nth frame")
    ap.add_argument("--max-resolution", type=int, default=720, help="downscale to this many px")
    ap.add_argument("--force", action="store_true", help="redo streams that already have an XML")
    ap.add_argument("--skip-ingest", action="store_true",
                    help="the capture is already built; go straight to annotating")
    ap.add_argument("--no-hashes", action="store_true",
                    help="skip sha256 of the outputs (faster; weaker manifest)")
    ap.add_argument("--manifest-only", action="store_true",
                    help="rewrite session.json from what is on disk; run nothing")
    args = ap.parse_args()

    stamp = args.stamp.removeprefix("session_")
    session = RECORDS / f"session_{stamp}"
    if not session.is_dir():
        raise SystemExit(f"no {session}. Sessions live in {RECORDS}; see its README.md.")

    vids_now = videos(session, stamp)
    if args.manifest_only:
        write_manifest(session, stamp, vids_now)
        print(f"manifest: {session / 'session.json'}")
        return 0

    py = python_bin()
    # Config is read the same way the server reads it, so a quorum.toml that
    # moves the data directory moves this script's idea of it too.
    sys.path.insert(0, str(REPO))
    from quorum.config import Config
    cfg = Config.load()

    vids = vids_now
    # A symlinked video resolves to wherever the records disk is mounted, which
    # is outside the media roots by default — and the failure that causes is a
    # 403 on the video endpoint long after the ingest said it succeeded. Check
    # it here, where the fix is one line of config, rather than there.
    outside = sorted({str(v.resolve().parent) for v in vids
                      if not cfg.is_media_allowed(v.resolve())})
    if outside:
        roots = ", ".join(str(r) for r in cfg.media_roots)
        raise SystemExit(
            f"these videos resolve outside the media roots, so the server would refuse to "
            f"serve them:\n  " + "\n  ".join(outside) +
            f"\n\nCurrent roots: {roots}\nAdd them in quorum.toml:\n"
            f"  media_roots = [\"..\", " + ", ".join(f'"{d}"' for d in outside) + "]")
    print(f"session_{stamp}: {len(vids)} camera(s)")
    for v in vids:
        print(f"  {v.name}")

    if not args.skip_ingest:
        paths = json.dumps([str(v.resolve()) for v in vids])
        sh([py, "-m", "quorum", "ingest", "multiview.disk", f"paths={paths}"])
    cid = capture_id(cfg.db_path, stamp)
    print(f"\ncapture {cid} (key {stamp})")

    sam_params = {
        "prompts": args.prompts, "labels": args.labels, "streams": args.streams,
        "frame_stride": args.frame_stride, "max_resolution": args.max_resolution,
    }
    job = [py, "-m", "quorum", "job", "sam.auto", f"capture_id={cid}",
           f"prompts={args.prompts}", f"frame_stride={args.frame_stride}",
           f"max_resolution={args.max_resolution}"]
    if args.labels:
        job.append(f"labels={args.labels}")
    if args.streams:
        job.append(f"streams={args.streams}")
    if args.force:
        job.append("force=true")
    sh(job)

    files = harvest(cfg.data_dir, cid, session, sam_params, hashes=not args.no_hashes)

    write_manifest(session, stamp, vids)

    print(f"\ndone. {len(files)} file(s) in {session / 'sam'}")
    print(f"manifest: {session / 'session.json'}")
    print("\nCommit sam/ and session.json. The videos are gitignored — the next person\n"
          "gets them with beefline_records/fetch_session.sh " + stamp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
