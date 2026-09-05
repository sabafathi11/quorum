"""media — make a stream's video playable in a browser, without baking a mosaic.

The tools this replaces built one 1920×720 H.264 file out of six cameras with
`ffmpeg xstack`, at CRF 30, with the clock offsets prepended as black frames.
That file is why zooming showed mush, why correcting a clock cost a 40-minute
re-encode, and why there were two copies of every recording that could drift
apart.

This does the one honest part of that job and none of the rest: it transcodes
**one camera at a time**, at **full geometry**, with **timestamps passed
through**, into whatever sizes are useful:

    full   the camera's own resolution, in a codec a browser can decode
    grid   small, for when six cells are on screen at once and none is big

Nothing is stacked, nothing is cropped, no offset is baked in, and every
rendition shares the source's timeline — so `currentTime` means the same thing
in all of them and the viewport can switch between them mid-playback. The
mosaic is composed in the browser from whichever rendition each cell needs
(see `web/core/viewport.js`), which is why changing an offset is now a slider.

A source a browser can already play needs none of this. `playable` is decided
from the codec at probe time, not guessed: these captures are HEVC, and Chrome
plays HEVC only where the platform has a hardware decoder.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from fastapi import Depends, Request

from quorum import probe
from quorum.api.deps import current_user
from quorum.sdk import Plugin

PLUGIN = Plugin(
    id="media",
    name="Media",
    description="Per-stream video renditions. Composed into a mosaic in the browser, never baked into one.",
)

# What each named rendition is for. `w=0` means "the source's own width".
RENDITIONS = {
    "grid": {"w": 640, "crf": 28, "preset": "veryfast",
             "why": "six cells on screen at once"},
    "full": {"w": 0, "crf": 23, "preset": "veryfast",
             "why": "one cell zoomed in, at the camera's real resolution"},
}


def _streams(ctx, cid: int, only: str = ""):
    rows = ctx.db.all("SELECT * FROM streams WHERE capture_id=? ORDER BY idx", cid)
    keys = {k.strip() for k in only.split(",") if k.strip()}
    return [dict(r) for r in rows if not keys or r["key"] in keys]


def _out_dir(ctx, cid: int) -> Path:
    d = ctx.cfg.data_dir / "renditions" / str(cid)
    d.mkdir(parents=True, exist_ok=True)
    return d


@PLUGIN.job(
    "prepare", title="Prepare video for the browser",
    params={"capture_id": {"type": "capture", "required": True},
            "which": {"type": "string", "default": "grid,full",
                      "label": "Renditions",
                      "description": "grid = small, for the whole mosaic. "
                                     "full = the camera's own resolution, for zooming."},
            "streams": {"type": "string", "default": "",
                        "label": "Only these streams (comma separated; blank = all)"},
            "force": {"type": "boolean", "default": False,
                      "label": "Rebuild ones that already exist"}},
    description="One transcode per camera. Nothing is stacked and no clock offset is baked in.")
def prepare(ctx):
    if not probe.have_ffmpeg():
        raise RuntimeError("ffmpeg is not on PATH — install it, or use sources a browser "
                           "can already decode (H.264/VP9/AV1 in mp4 or webm)")
    cid = int(ctx.params["capture_id"])
    want = [w.strip() for w in str(ctx.params.get("which") or "grid,full").split(",") if w.strip()]
    bad = [w for w in want if w not in RENDITIONS]
    if bad:
        raise ValueError(f"unknown rendition(s) {', '.join(bad)}; known: {', '.join(RENDITIONS)}")
    force = bool(ctx.params.get("force"))
    streams = _streams(ctx, cid, str(ctx.params.get("streams") or ""))
    if not streams:
        raise ValueError("this capture has no streams to prepare")
    out_dir = _out_dir(ctx, cid)

    made, skipped, unaligned = [], [], []
    steps = max(1, len(streams) * len(want))
    n = 0
    for st in streams:
        media = json.loads(st["media"] or "{}")
        src = media.get("path")
        if not src or not Path(src).is_file():
            ctx.log(f"{st['key']}: no media file — skipped")
            continue
        rends = dict(media.get("renditions") or {})
        for rid in want:
            ctx.check()
            n += 1
            spec = RENDITIONS[rid]
            dest = out_dir / f"{st['key']}.{rid}.mp4"
            if dest.exists() and not force and rends.get(rid, {}).get("path") == str(dest):
                skipped.append(f"{st['key']}/{rid}")
                ctx.progress(n / steps, f"{st['key']} {rid}: already there")
                continue
            ctx.progress(n / steps, f"{st['key']} {rid}: transcoding ({spec['why']})")
            _transcode(ctx, Path(src), dest, spec)
            f = probe.facts(dest)
            align = _alignment(Path(src), dest)
            rends[rid] = {"path": str(dest), "w": f["width"], "h": f["height"],
                          "codec": f["codec"], "size": f["size"], **align}
            if align["aligned"]:
                made.append(f"{st['key']}/{rid}")
            else:
                unaligned.append(f"{st['key']}/{rid}: {align['why']}")
                ctx.log(f"{st['key']} {rid}: {align['why']}")
        media["renditions"] = rends
        ctx.db.run("UPDATE streams SET media=? WHERE id=?", json.dumps(media), st["id"])

    ctx.host.publish(cid, {"t": "capture", "capture_id": cid})
    msg = f"{len(made)} rendition(s) built, {len(skipped)} already there"
    if unaligned:
        msg += f"; {len(unaligned)} NOT frame-aligned and will not be used ({unaligned[0]})"
    return {"made": made, "skipped": skipped, "unaligned": unaligned, "message": msg}


def _alignment(src: Path, dest: Path) -> dict:
    """Prove that the rendition shows the same frame as the source at the same
    time — or refuse to let anything use it.

    This is not defensive padding. A frame map says "capture frame 4400 is this
    camera's frame 7266", and everything downstream trusts that the *picture*
    for frame 7266 is what a mask was drawn on. A rendition off by one frame
    would look perfectly fine and put every mask one frame out, which is the
    worst kind of wrong: invisible. So an unaligned rendition is kept on disk,
    reported, and never offered — the still-frame path is cut from the source
    and is always right, so falling back to it costs nothing but sharpness.
    """
    import numpy as np
    try:
        a = probe.timestamps(src)
        b = probe.timestamps(dest)
    except Exception as e:
        return {"aligned": False, "t_offset": 0.0, "why": f"could not read timestamps ({e})"}
    if len(a) != len(b):
        return {"aligned": False, "t_offset": 0.0,
                "why": f"{len(b)} frames out of {len(a)} — ffmpeg dropped or added frames, "
                       f"so frame N is not frame N"}
    if len(a) < 2:
        return {"aligned": True, "t_offset": 0.0, "why": ""}
    d = b.astype("int64") - a.astype("int64")
    med = float(np.median(d))
    spread = float(np.max(np.abs(d - med))) / 1e6
    gap = float(np.median(np.diff(a))) / 1e6 or 0.04
    if spread > gap * 0.5:
        return {"aligned": False, "t_offset": 0.0,
                "why": f"timestamps wander by up to {spread * 1000:.0f} ms against a "
                       f"{gap * 1000:.0f} ms frame interval"}
    return {"aligned": True, "t_offset": med / 1e6, "why": ""}


def _transcode(ctx, src: Path, dest: Path, spec: dict) -> None:
    """One camera, one size. The flags that matter, and why:

    `-fps_mode passthrough`   keep every source frame and its own timestamp, so
                              the rendition and the source share a timeline and
                              `currentTime` means the same in both. Without it
                              ffmpeg would resample to a constant rate and every
                              frame map derived from the source would be a lie.
    `-copyts -start_at_zero`  do not rebase the clock.
    `-movflags +faststart`    the browser can start playing before it has the
                              whole 150 MB file.
    """
    scale = ["-vf", f"scale={spec['w']}:-2"] if spec["w"] else []
    args = ["ffmpeg", "-v", "error", "-y", "-nostdin",
            "-copyts", "-start_at_zero", "-i", str(src),
            *scale,
            "-c:v", "libx264", "-preset", spec["preset"], "-crf", str(spec["crf"]),
            "-pix_fmt", "yuv420p", "-fps_mode", "passthrough",
            "-movflags", "+faststart", "-an", str(dest)]
    p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        while p.poll() is None:
            try:
                ctx.check()
            except Exception:
                p.terminate()
                raise
            try:
                p.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                continue
    finally:
        if p.poll() is None:
            p.terminate()
    if p.returncode != 0:
        err = (p.stderr.read() if p.stderr else "").strip().splitlines()
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed on {src.name}: {err[-1] if err else p.returncode}")


@PLUGIN.job("drop", title="Delete prepared renditions",
            params={"capture_id": {"type": "capture", "required": True}},
            description="Free the disk. The sources are untouched; rebuild any time.")
def drop(ctx):
    cid = int(ctx.params["capture_id"])
    gone = 0
    for st in _streams(ctx, cid):
        media = json.loads(st["media"] or "{}")
        for rid, r in (media.get("renditions") or {}).items():
            try:
                Path(r.get("path", "")).unlink(missing_ok=True)
                gone += 1
            except OSError:
                pass
        media["renditions"] = {}
        ctx.db.run("UPDATE streams SET media=? WHERE id=?", json.dumps(media), st["id"])
    ctx.host.publish(cid, {"t": "capture", "capture_id": cid})
    return {"deleted": gone, "message": f"{gone} rendition file(s) deleted"}


@PLUGIN.cleanup
def forget(ctx, capture_id: int) -> None:
    """The core deletes its rows and the files people uploaded. It has no idea
    this plugin transcoded anything, so it asks."""
    import shutil
    d = ctx.cfg.data_dir / "renditions" / str(capture_id)
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)


@PLUGIN.route.get("/{capture_id}/status")
def status(capture_id: int, request: Request, user: dict = Depends(current_user)):
    """What each stream can and cannot do in a browser right now — which is
    what the Capture workspace shows instead of leaving somebody to work out
    why a cell is black."""
    host = request.app.state.host
    out = []
    for st in host.db.all("SELECT * FROM streams WHERE capture_id=? ORDER BY idx", capture_id):
        m = json.loads(st["media"] or "{}")
        out.append({
            "stream": st["key"],
            "codec": m.get("codec", ""),
            "playable": bool(m.get("playable")),
            "present": bool(m.get("path") and Path(m["path"]).is_file()),
            "renditions": sorted((m.get("renditions") or {}).keys()),
            "has_timestamps": bool(st["timestamps"]),
        })
    return {"streams": out, "ffmpeg": probe.have_ffmpeg(), "known": sorted(RENDITIONS)}
