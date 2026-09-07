"""sam — the model that draws, and the batch that draws everything.

Three abilities, one plugin, because they are one story: *get a mask from SAM
3.1 onto this capture*. They differ in who asks and how long it takes.

    interactor   you click points on a person and get a mask back, now.
                 A route, not a job — see below.
    tracker      you have one mask and want the next 60 frames of it.
                 A job: it is a minute of GPU work.
    auto         you have nothing and want "hat, glove, hairnet, phone" over
                 six ten-minute cameras. A job: it is hours, in `auto.py`.

**All three run on one model.** They used to be two: a nuclio function holding
a tracker-only assembly for the first two, and a container per auto run that
built the full predictor again. That cost 4,902 MiB of resident duplication and
peaked the card at 97%. `service/` now holds one predictor and does all three,
so this plugin has exactly one thing it cannot work without — and the readiness
check says so once rather than twice.

**Why the interactor is a route when "long work is a job".** That rule is about
requests that make somebody wait without knowing why. The interactor is the
other thing: it is the round trip of a *gesture*, ~300 ms, in the same class as
a hit test, and putting it through the job queue would mean a click, a poll and
a websocket message to draw a mask that is already computed. It is also the
only call here that never writes anything — it returns a preview, and the
person decides. The two that do write, and that do take minutes, are jobs.

**Nothing here writes an annotation on its own.** The interactor returns a
preview; the browser commits it through `masks`, as the person who clicked. The
tracker writes `masks.keyframes` attributed to whoever pressed the key — it is
a power tool with a human's hand on it, not a generator, which is the same
distinction `sync_id_ui` made when `R` filled in a track. Only `auto` produces
work nobody looked at, and that arrives as its own `provenance="model"` layer,
clearly labelled, exactly like an import from CVAT.

Configuration (`quorum.toml`):

    [plugins.sam]
    url = "http://sam-service:8080/"        # $SAM_URL wins

That is the whole configuration now. The image, the GPU flag, the annotator
checkout and the container→host path map all belonged to starting a container
per run, and there is no container per run.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import numpy as np
from fastapi import Body, Depends, HTTPException, Request

from quorum import probe
from quorum.api.deps import current_user
from quorum.sdk import Plugin, Tool

PLUGIN = Plugin(
    id="sam",
    name="SAM",
    description="Segment with SAM 3.1: click a person and get a mask, propagate one "
                "across frames, or annotate whole cameras from text prompts.",
    web="sam.js",
)

PLUGIN.tool(Tool(id="sam", title="SAM", icon="✦", order=25,
                 description="Click to segment, propagate a mask, or run the "
                             "auto-annotator over whole cameras."))

HERE = Path(__file__).resolve().parent
DEFAULT_URL = "http://sam-service:8080/"


# The three sibling modules are loaded by path rather than imported by name:
# the plugin loader gives this file the module name `quorum_plugins.sam`, which
# is not a package, so `from . import client` has nothing to resolve against.
# Every plugin that grows past one file meets this; doing it here in five lines
# beats each of them inventing its own answer.
def _load(name: str):
    import importlib.util
    key = f"quorum_plugins.sam.{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


client = _load("client")
frames = _load("frames")
auto = _load("auto")


# ----------------------------------------------------------------- settings
def endpoint(plugin_or_ctx) -> "client.Sam":
    settings = getattr(plugin_or_ctx, "settings", None)

    def setting(key, default):
        if callable(settings):
            return settings(key, default)
        return (settings or {}).get(key, default)

    url = os.environ.get("SAM_URL") or ""
    if not url:
        url = setting("url", "") or DEFAULT_URL
    # How long the readiness probe waits. The default suits a service on the
    # same host; a laptop reaching this one across a relayed tailnet needs more
    # than three seconds to complete a round trip, and without this the panel
    # says "not answering" about a service that answers every real call fine —
    # the probe is the only call in this file with a LAN-sized budget.
    probe = os.environ.get("SAM_PROBE_TIMEOUT") or setting("probe_timeout", 3.0)
    return client.Sam(url, probe_timeout=float(probe or 3.0))


# ------------------------------------------------------------------ streams
def _stream(db, capture_id: int, key: str) -> dict:
    r = db.one("SELECT * FROM streams WHERE capture_id=? AND key=?", capture_id, key)
    if r is None:
        raise HTTPException(404, f"this capture has no stream {key!r}")
    return dict(r)


def _source(st: dict) -> Path:
    """The camera's own file, never a rendition.

    A rendition is downscaled — that is what it is for — and a mask segmented
    on 640 px and stored against a 1920 px stream would be off by a factor of
    three with nothing to say so.
    """
    media = json.loads(st["media"] or "{}")
    p = media.get("path")
    if not p or not Path(p).is_file():
        raise HTTPException(409, f"{st['key']} has no media file on disk to read a frame from")
    return Path(p)


def to_stream_frame(st: dict, capture_frame: int) -> int:
    fm = np.frombuffer(st["frame_map"], dtype="<i4") if st["frame_map"] else None
    if fm is None or not len(fm):
        return int(capture_frame)
    return int(fm[max(0, min(int(capture_frame), len(fm) - 1))])


# ---------------------------------------------------------------- readiness
# Both checks are memoised: readiness is answered on every capture open and
# before every job submission, and neither a TCP connect nor `docker info`
# belongs on that path several times a second.
_CACHE: dict[str, tuple[float, object]] = {}


def _cached(key: str, ttl: float, fn):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception as e:
        val = e
    _CACHE[key] = (time.time(), val)
    return val


@PLUGIN.readiness
def ready(host, capture_id):
    """What is missing, scoped to what it actually stops.

    Everything here needs the service, because the service *is* the model.
    ffmpeg is separate and narrower: it reads a frame out of a video, which the
    interactor and the tracker need and the auto-annotator does not (the
    pipeline decodes its own). So the reasons are still scoped rather than
    collapsed into one sentence about the plugin.
    """
    out = []
    sam = endpoint(host.plugins["sam"])
    alive, why = _cached("alive", 20.0, lambda: sam.alive())
    if not alive:
        # One endpoint now gates everything, because there is one model behind
        # it: the interactor, the tracker and the auto-annotator all run in the
        # service's process. That is the whole point of having merged them.
        out.append({
            "why": f"the SAM service is not answering — {why}",
            "fix": "Start it (plugins/sam/service) and put this server on the same "
                   "docker network, then set [plugins.sam] url or SAM_URL.",
            "tools": ["sam"], "jobs": ["track", "auto"]})
    if not probe.have_ffmpeg():
        out.append({"why": "ffmpeg is not on PATH, so a frame cannot be read out of a video",
                    "fix": "Install ffmpeg on this server.",
                    "tools": ["sam"], "jobs": ["track"]})
    return out


@PLUGIN.route.get("/status")
def status(request: Request, fresh: bool = False,
           user: dict = Depends(current_user)):
    """Everything the panel needs to say what will and will not work, and why.

    Deliberately one call with four answers rather than four endpoints: the
    question a person actually has is "can I use this right now", and it has
    one answer made of several parts.

    Cached, like the readiness check and for a sharper reason: the panel asks
    on every capture open and every switch into this tool, and a *down*
    endpoint costs the full connect timeout each time — three seconds of a dead
    workspace, repeatedly, for an answer that has not changed. `fresh=1` is the
    recheck button, for the minute after somebody deploys the function.
    """
    host = request.app.state.host
    sam = endpoint(host.plugins["sam"])
    if fresh:
        _CACHE.pop("alive", None)
        _CACHE.pop("health", None)
    alive, why = _cached("alive", 20.0, lambda: sam.alive())
    health = _cached("health", 20.0, lambda: _service_health(sam.url)) if alive else {}
    if isinstance(health, Exception):
        health = {}
    return {
        "url": sam.url,
        "endpoint": {"ok": alive, "why": why},
        # The service answers what it is holding, which on a shared card is the
        # number somebody actually wants before starting an hours-long run.
        "vram": health.get("vram", {}),
        "busy": health.get("busy", False),
        "job": health.get("job"),
        "max_objects": health.get("max_objects"),
        "ffmpeg": probe.have_ffmpeg(),
    }


def _service_health(url: str) -> dict:
    import urllib.request
    with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=5) as r:
        return json.loads(r.read().decode() or "{}")


# --------------------------------------------------------------- interactor
@PLUGIN.route.post("/{capture_id}/interact")
def interact(capture_id: int, request: Request, body: dict = Body(...),
             user: dict = Depends(current_user)):
    """Points in, one mask out. Writes nothing.

    Coordinates are that **stream's own pixels** — the browser divides out the
    cell scale before it asks, because only the browser knows how big it drew
    the cell, and a plugin that had to be told the zoom level would be wrong
    the moment somebody resized a window.
    """
    host = request.app.state.host
    st = _stream(host.db, capture_id, str(body.get("stream", "")))
    src = _source(st)
    pos = [[float(p[0]), float(p[1])] for p in (body.get("pos") or [])]
    neg = [[float(p[0]), float(p[1])] for p in (body.get("neg") or [])]
    bbox = body.get("bbox") or None
    if not pos and not bbox:
        raise HTTPException(400, "give at least one positive point, or a box")

    sf = (int(body["stream_frame"]) if body.get("stream_frame") is not None
          else to_stream_frame(st, int(body.get("frame", 0))))
    stamps = probe.unpack(st["timestamps"])
    try:
        t = frames.times_of(stamps, [sf])[0]
    except frames.NoFrame as e:
        raise HTTPException(409, str(e))

    roi = body.get("roi")
    if roi in (None, True):
        roi = frames.roi_for(pos + neg, bbox, st["width"], st["height"])
    elif roi is False:
        roi = None
    ox, oy = (int(roi[0]), int(roi[1])) if roi else (0, 0)

    t0 = time.time()
    try:
        jpeg = frames.one(src, t, roi)
    except (frames.NoFrame, probe.NoFFmpeg) as e:
        raise HTTPException(409, str(e))
    t_cut = time.time()
    try:
        payload = endpoint(host.plugins["sam"]).interact(
            jpeg,
            pos=[[p[0] - ox, p[1] - oy] for p in pos],
            neg=[[p[0] - ox, p[1] - oy] for p in neg],
            bbox=([bbox[0] - ox, bbox[1] - oy, bbox[2] - ox, bbox[3] - oy] if bbox else None))
    except client.SamError as e:
        raise HTTPException(502, str(e))
    # The RLE is relative to its own box, so putting the crop back is arithmetic
    # on four numbers rather than a re-encode.
    return {"payload": client.shift(payload, ox, oy),
            "stream_frame": sf, "time": t, "roi": roi,
            "ms": {"frame": int((t_cut - t0) * 1000), "model": int((time.time() - t_cut) * 1000)},
            "empty": payload is None}


@PLUGIN.route.get("/{capture_id}/newkey")
def newkey(capture_id: int, request: Request, stream: str,
           user: dict = Depends(current_user)):
    """The next free `samN` key in this stream.

    Free means free *everywhere*: in every mask layer, and in the create ops
    that have not been materialised yet. A key that is merely unused in the
    layer you are looking at is how a new track gets born into an id something
    else already filters out — `sam_edits.new_track` learned that the hard way
    and took a `taken` argument for it.
    """
    host = request.app.state.host
    used = {str(r["key"]) for r in host.db.all(
        "SELECT o.key FROM objects o JOIN streams s ON s.id=o.stream_id "
        "JOIN layers l ON l.id=o.layer_id WHERE l.capture_id=? AND s.key=? "
        "AND l.type='mask.rle'", capture_id, stream)}
    for op in host.ops_since(capture_id, 0, include_undone=True):
        d = op.get("payload") or {}
        if op["kind"] in ("masks.create", "masks.keyframe", "masks.keyframes") \
                and str(d.get("stream")) == stream:
            used.update(str(k) for k in (d.get("keys") or ([d["key"]] if d.get("key") else [])))
    n = 1
    while f"sam{n}" in used:
        n += 1
    return {"key": f"sam{n}", "stream": stream}


# ------------------------------------------------------------------ tracker
def _masks_module(host):
    p = host.plugins.get("masks")
    if p is None:
        raise RuntimeError(
            "the masks plugin is not installed, so there is nowhere to put a mask — "
            "SAM produces mask edits and `masks` owns those")
    return sys.modules[p.module_name]


def _seed(host, capture_id: int, stream: str, key: str, stream_frame: int):
    """The mask showing for this track at this frame, whichever layer holds it.

    Not "the mask layer": a cut track keeps its original key and so exists in
    two layers at once, and seeding the tracker from the superseded copy would
    propagate the version the person already edited away.
    """
    m = _masks_module(host)
    rows = [r for r in m.effective_objects(host, capture_id)
            if r["stream"] == stream and r["key"] == str(key)]
    if not rows:
        raise HTTPException(404, f"{stream} has no track {key} to propagate")
    oid = rows[-1]["object_id"]
    r = host.db.one(
        "SELECT frame, outside, payload FROM shapes WHERE object_id=? AND frame<=? "
        "ORDER BY frame DESC LIMIT 1", oid, int(stream_frame))
    if r is None or r["outside"]:
        raise HTTPException(
            409, f"{stream}/{key} has no mask at frame {stream_frame} — move to a frame "
                 f"where it is visible, or draw one with the interactor first")
    return json.loads(r["payload"]), rows[-1]["label"]


@PLUGIN.job(
    "track", title="Propagate a mask forward",
    params={"capture_id": {"type": "capture", "required": True},
            "stream": {"type": "string", "required": True, "label": "Stream key"},
            "key": {"type": "string", "required": True, "label": "Track key"},
            "frame": {"type": "integer", "required": True,
                      "label": "Seed frame (this stream's own numbering)"},
            "count": {"type": "integer", "default": 60,
                      "label": "How many frames to carry it",
                      "description": "Counted in this stream's frames, from the seed."},
            "stride": {"type": "integer", "default": 1,
                       "label": "Write every Nth frame",
                       "description": "The mask is interpolated between keyframes, so 2 or 3 "
                                      "costs little on slow movement and a third of the GPU."},
            "backward": {"type": "boolean", "default": False,
                         "label": "Go backwards from the seed instead"},
            "roi": {"type": "boolean", "default": True,
                    "label": "Crop around the object first (not yet applied here)",
                    "description": "Accepted and ignored: propagation sends whole frames. "
                                   "The interactor crops, and for one frame that is right — "
                                   "but a crop fixed on the seed box loses a person who walks "
                                   "out of it, and a run is 60 frames long. Until the crop "
                                   "follows the mask, this switch does nothing."}},
    description="Carry one mask across the next frames of its own camera. The keyframes it "
                "writes are one op, attributed to you, and Ctrl+Z takes all of them back.")
def track(ctx):
    """SAM's video tracker, seeded by a mask a person already accepted.

    One op for the whole propagation, not one per frame. Sixty ops would each
    be individually undoable and collectively unusable — the person made *one*
    decision ("carry this forward"), and taking it back should be one keystroke.
    """
    host = ctx.host
    cid = int(ctx.params["capture_id"])
    skey = str(ctx.params["stream"])
    key = str(ctx.params["key"])
    st = _stream(ctx.db, cid, skey)
    src = _source(st)
    seed, label = _seed(host, cid, skey, key, int(ctx.params["frame"]))

    n = max(1, int(ctx.params.get("count") or 60))
    stride = max(1, int(ctx.params.get("stride") or 1))
    step = -stride if ctx.params.get("backward") else stride
    start = int(ctx.params["frame"])
    stamps = probe.unpack(st["timestamps"])
    n_frames = len(stamps) if stamps is not None else st["n_frames"]
    want = [f for f in range(start, start + step * (n + 1), step) if 0 <= f < n_frames][:n + 1]
    if len(want) < 2:
        raise ValueError(f"there are no frames {'before' if step < 0 else 'after'} {start} "
                         f"in {skey} to carry it to")

    # Whole frames, deliberately, and `roi` above says so rather than pretending.
    # A tracked object moves; `frames.roi_for` pads the seed box by 1.6 and caps
    # the crop at 45% of the frame, which is sized for one prompt on one frame,
    # not for 60. Cropping to that and then losing the person at frame 20 is
    # worse than sending pixels. The cost of not cropping is real, though, and
    # it lands on a shared GPU: 61 frames of 1920x1080 rather than 61 crops.
    # The fix is a crop that follows the mask between chunks, not this flag.
    ctx.progress(0.05, f"reading {len(want)} frames of {skey}")
    times = frames.times_of(stamps, want)
    jpegs = frames.many(src, times, check=ctx.check)

    ctx.progress(0.35, f"tracking {len(want)} frames")
    sam = endpoint(ctx.plugin)
    per_obj = sam.track(jpegs, [seed])
    ctx.check()
    got = per_obj[0] if per_obj else []
    if len(got) != len(want):
        raise RuntimeError(
            f"asked SAM for {len(want)} frames and it answered {len(got)} — refusing to "
            f"guess which mask belongs to which frame")

    # The seed frame comes back re-segmented. Keep what the person accepted:
    # they looked at that mask, and quietly replacing it is how a tool loses
    # the trust that made them press the key.
    items = [{"frame": f, "payload": {**p, "outside": 0}}
             for f, p in zip(want[1:], got[1:]) if p is not None]
    lost = len(want) - 1 - len(items)
    if not items:
        return {"written": 0, "message": f"SAM lost {skey}/{key} immediately — nothing written"}

    ctx.progress(0.85, f"writing {len(items)} keyframes")
    m = _masks_module(host)
    payload = {"stream": skey, "key": key, "label": label, "frames": items,
               "source": "sam.track", "seed_frame": start}
    op = host.append_op(cid, "masks.keyframes", payload, ctx.actor)
    out = m.materialise(host, cid, ctx)
    end = items[-1]["frame"]
    return {"op": op["id"], "written": len(items), "lost": lost, "layer_id": out.get("layer_id"),
            "range": [start, end],
            "message": f"{skey}/{key}: {len(items)} keyframes to frame {end}"
                       + (f"; SAM lost it on {lost}" if lost else "")}


# ------------------------------------------------------------- auto-annotate
@PLUGIN.job(
    "auto", title="Auto-annotate from text prompts",
    params={
        "capture_id": {"type": "capture", "required": True},
        "prompts": {"type": "string", "required": True, "label": "Text prompts",
                    "default": "person",
                    "description": "One per line, or comma separated. Singular nouns: text "
                                   "grounding keys on the noun, and “hats” sometimes biases "
                                   "toward group shots while scoring the same objects."},
        "labels": {"type": "string", "default": "", "label": "Labels (one per prompt)",
                   "description": "Blank reuses the prompt text. Give them when the phrase "
                                  "that segments best is not the name you want "
                                  "(“person sitting down” → customer)."},
        "streams": {"type": "string", "default": "", "label": "Only these streams"},
        "frame_stride": {"type": "integer", "default": 5, "label": "Process every Nth frame"},
        "chunk_frames": {"type": "integer", "default": 64, "label": "Window size"},
        "overlap_frames": {"type": "integer", "default": 4,
                           "label": "Shared frames identities are matched on"},
        "max_resolution": {"type": "integer", "default": 720, "label": "Downscale to (px)",
                           "description": "720 was tuned for people. Hats and phones are far "
                                          "smaller in frame — raise this first if recall is "
                                          "poor, at the cost of VRAM."},
        "fill_hole_area": {"type": "integer", "default": 64, "label": "Fill holes up to (px)"},
        "freeze_gap_ms": {"type": "number", "default": 400,
                          "label": "Call a timestamp hole a discontinuity after (ms)",
                          "description": "The fidelity dial. Higher gives fewer, longer, less "
                                         "trustworthy tracks; no identity is ever carried "
                                         "across a hole. 0 disables the check."},
        "freeze_min_frames": {"type": "integer", "default": 10,
                              "label": "Repeated frames that count as a stall"},
        "layer": {"type": "string", "default": "sam_auto", "label": "Layer key"},
        "layer_name": {"type": "string", "default": "SAM auto", "label": "Layer name"},
        "import_result": {"type": "boolean", "default": True,
                          "label": "Import the tracks when it finishes"},
        "replace_layer": {"type": "boolean", "default": True,
                          "label": "Replace that layer if it exists"},
        "force": {"type": "boolean", "default": False,
                  "label": "Redo streams that already have an XML"},
    },
    description="Run SAM 3.1 over whole cameras from text prompts. Hours of GPU work, on "
                "the same model the interactor uses: one camera at a time, progress per "
                "window, and cancel stops it at the next window.")
def auto_annotate(ctx):
    return auto.run(ctx, HERE)


@PLUGIN.route.get("/{capture_id}/runs")
def runs(capture_id: int, request: Request, user: dict = Depends(current_user)):
    """What the auto-annotator has produced for this capture, and what it said.

    The Jobs panel shows what is *running*; this shows what is *there* — the
    XML on disk that a finished run left, and the discontinuity summary that
    explains a camera which fragmented into 136 tracks.
    """
    host = request.app.state.host
    d = host.cfg.data_dir / "sam_auto" / str(capture_id)
    files = []
    if d.is_dir():
        for f in sorted(d.glob("*.xml")):
            files.append({"stream": f.stem, "name": f.name, "size": f.stat().st_size,
                          "modified": f.stat().st_mtime})
        for f in sorted(d.glob("*.xml.PARTIAL")):
            files.append({"stream": f.name.split(".")[0], "name": f.name,
                          "size": f.stat().st_size, "modified": f.stat().st_mtime,
                          "partial": True})
    jobs = [j for j in host.jobs.list(capture_id, 40) if j["plugin"] == "sam"]
    return {"files": files, "freezes": auto.freeze_summary(d) if d.is_dir() else [],
            "jobs": jobs, "dir": str(d)}


@PLUGIN.cleanup
def forget(ctx, capture_id: int) -> None:
    """A capture is going away; the XML and logs this plugin wrote go with it."""
    import shutil
    d = ctx.cfg.data_dir / "sam_auto" / str(capture_id)
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
