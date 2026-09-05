"""The batch auto-annotator: `sam3_auto_annotator` run as a Quorum job.

`annotator/` is a **verbatim copy** of `~/Codes/newlabelstudio/sam3_auto_annotator`
— `main.py`, its two helpers and its design notes, unedited. That is on purpose:
it is a tuned, measured, 1,200-line pipeline whose hard-won parts (the VRAM
mitigations, the two frozen-input detectors, the per-category identity
stitching) are exactly the parts a rewrite would quietly lose. Nothing in here
imports it. It runs where it was built to run — inside the SAM 3.1 GPU image —
and this file is only the three things Quorum has to add:

    1. build the command line from a capture's streams and the person's prompts
    2. turn its stdout into progress, and a cancel into a dead container
    3. import the CVAT XML it writes, as a `provenance="model"` mask layer

**One filesystem, and this is the constraint that matters.** The job is an HTTP
call to the service, but what it sends is a *path*: the service runs
`main.py --video <path> --output <path>` and opens both itself. So Quorum and
the service have to see the same files at the same names — which they do when
they are on one host, and cannot when they are not.

That makes the three sizes of SAM differ in where they can run, and it is worth
being blunt about it because the failure is otherwise mystifying:

    interactor   posts the frame as bytes        -> works against any endpoint
    tracker      posts the frames as bytes       -> works against any endpoint
    auto         posts a path, gets a path back  -> same machine only

An earlier design had Quorum drive `docker run` and translate paths for the
daemon; `hostpath()` and `path_map` were that translation. Both are gone. If
this ever needs to run against a remote service, the thing to add is a transfer
— the video up, the XML back — not a mapping table.

**Never parallel.** The GPU is shared — with the interactor, and on that box
with whatever else is using it. Streams run one after another, exactly as the
upstream `run.sh` does, and for the same reason: two of these at once do not
go twice as fast, they OOM.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from quorum.sdk import Cancelled

# Lines the pipeline prints that are worth turning into progress or a note.
RE_LOADED = re.compile(r"^Video loaded:\s*(\d+)\s*frames")
RE_WINDOW = re.compile(r"^=== Processing Window: Global Frames (\d+) to (\d+)")
RE_TRACKS = re.compile(r"^Tracks found:\s*(.+)$")
RE_FREEZE = re.compile(r"^(\d+) discontinuity")
RE_PARTIAL = re.compile(r"PARTIAL CHUNK")
# Anything matching this is kept in the job's log even when it is not progress:
# these are the lines that explain a bad result afterwards.
RE_KEEP = re.compile(r"\[FREEZE\]|\[VRAM\]|\[WARN\]|\[ERROR\]|\[CRITICAL\]|PARTIAL CHUNK|"
                     r"Frozen input ended|discontinuity|Tracks found|not found anywhere")

DEFAULT_IMAGE = "cvat.pth.comfyorg.sam3.1:latest-gpu"
MODEL_SHARE = 0.95              # of the progress bar; the last 5% is the import


class NoService(RuntimeError):
    """The one thing this cannot work without: the process that holds the model."""

# ------------------------------------------------------------ the arguments
def categories(prompts: str, labels: str) -> list[tuple[str, str]]:
    """`--text_prompt` / `--label_name` pairs, from two lines of user text.

    Split on newlines or commas so both "hat, glove" and a pasted list work.
    Two categories may not share a label: upstream exits rather than produce a
    file where they are indistinguishable, and finding that out 40 minutes into
    a GPU run is worse than finding it out here.
    """
    def split(s):
        return [x.strip() for x in re.split(r"[\n,]", str(s or "")) if x.strip()]
    ps = split(prompts)
    ls = split(labels) or list(ps)
    if not ps:
        raise ValueError("give at least one text prompt — that is what SAM searches for")
    if len(ls) != len(ps):
        raise ValueError(
            f"{len(ps)} prompt(s) but {len(ls)} label(s) — they pair by position, so give "
            f"one label per prompt, or none at all and the prompt text is used")
    if len(set(ls)) != len(ls):
        raise ValueError(
            "two categories share a label; they would compete for one identity pool and be "
            "indistinguishable afterwards. Give each prompt its own label.")
    return list(zip(ps, ls))


def build_args(cats, params: dict, prompts: bool = True) -> list[str]:
    """The pipeline's flags. Defaults are the ones `run.sh` actually ships with,
    not the argparse defaults — those were tuned on this footage.

    `prompts=False` leaves the categories out: the service takes them as their
    own fields and builds those two flags itself, and sending them twice would
    give the pipeline four prompts where the person asked for two.
    """
    out: list[str] = []
    if prompts:
        for p, _ in cats:
            out += ["--text_prompt", p]
        for _, l in cats:
            out += ["--label_name", l]
    for flag, key, cast in [
        # `--max_num_objects` is deliberately absent: it is a *constructor*
        # argument of the predictor, and the predictor is built once when the
        # service starts. Sending it per job would be accepted and ignored,
        # which is worse than not offering it. `SAM_MAX_OBJECTS` on the service
        # is the real knob.
        ("--chunk_frames", "chunk_frames", int),
        ("--overlap_frames", "overlap_frames", int),
        ("--frame_stride", "frame_stride", int),
        ("--max_resolution", "max_resolution", int),
        ("--fill_hole_area", "fill_hole_area", int),
        ("--prompt_search_stride", "prompt_search_stride", int),
        ("--max_obj_ptrs", "max_obj_ptrs", int),
        ("--grounding_batch", "grounding_batch", int),
        ("--freeze_min_frames", "freeze_min_frames", int),
        ("--freeze_gap_ms", "freeze_gap_ms", float),
        ("--freeze_gap_mult", "freeze_gap_mult", float),
    ]:
        v = params.get(key)
        if v is None or v == "":
            continue
        out += [flag, str(cast(v))]
    return out



# ------------------------------------------------------------ the transport
def _service(settings) -> str:
    """Where the SAM service is. The same address the interactor uses — there
    is one model and therefore one endpoint."""
    url = (os.environ.get("SAM_URL") or "").strip()
    if not url and callable(settings):
        url = str(settings("url", "") or "").strip()
    if not url:
        raise NoService(
            "no SAM endpoint is configured — set [plugins.sam] url in quorum.toml "
            "(or SAM_URL) to the service that holds the model")
    return url.rstrip("/")


def _post(url: str, body: dict, timeout: float = 120.0) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return _read(req, timeout)


def _get(url: str, timeout: float = 60.0) -> dict:
    return _read(urllib.request.Request(url), timeout)


def _read(req, timeout: float) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode(errors="replace") or "{}")
    except urllib.error.HTTPError as e:
        detail = (e.read().decode(errors="replace") or "").strip()[:300]
        raise NoService(f"the SAM service answered {e.code}: {detail or e.reason}")
    except urllib.error.URLError as e:
        raise NoService(
            f"cannot reach the SAM service ({e.reason}). It holds the model, so nothing "
            f"here works without it — check it is up and on the same docker network.")


# --------------------------------------------------------------- one stream
class Run:
    """One stream's annotation run, watched over HTTP.

    The progress parsing is unchanged from when this read a subprocess pipe,
    which is the point: the pipeline still prints the same lines, and moving it
    inside the model's own process did not change what it says. Cancelling is a
    POST rather than `docker kill`, and lands at the next window boundary —
    seconds — because that is where the service checks.
    """

    def __init__(self, ctx, base: str, params: dict):
        self.ctx = ctx
        self.base = base
        self.params = params
        self.job_id = None
        self.total = 0
        self.at = 0
        self.tracks = ""
        self.notes: list[str] = []

    def go(self, on_progress) -> str:
        started = _post(f"{self.base}/auto", self.params, timeout=60)
        self.job_id = started.get("id")
        if not self.job_id:
            raise NoService(f"the service did not start the job: {started}")
        seen = 0
        while True:
            time.sleep(2.0)
            if self.ctx.cancelled:
                _post(f"{self.base}/auto/{self.job_id}/cancel", {}, timeout=30)
                self.ctx.check()                 # raises Cancelled
            d = _get(f"{self.base}/auto/{self.job_id}?since={seen}")
            for line in d.get("lines") or []:
                seen += 1
                self._line(line, on_progress)
            state = d.get("state", "?")
            if state in ("done", "failed", "cancelled"):
                if state == "cancelled":
                    self.ctx.check()
                    raise Cancelled()
                if state == "failed":
                    raise RuntimeError(d.get("error") or "the pipeline failed")
                return state

    def _line(self, line: str, on_progress) -> None:
        m = RE_LOADED.match(line)
        if m:
            self.total = int(m.group(1))
            self.notes.append(line)
            on_progress(0.0, line)
            return
        m = RE_WINDOW.match(line)
        if m:
            self.at = int(m.group(2))
            frac = (self.at / self.total) if self.total else 0.0
            on_progress(min(frac, 0.999), f"frames {m.group(1)}\u2013{m.group(2)}"
                                          + (f" of {self.total}" if self.total else ""))
            return
        m = RE_TRACKS.match(line)
        if m:
            self.tracks = m.group(1).strip()
        if RE_KEEP.search(line):
            self.notes.append(line.strip())
            if RE_PARTIAL.search(line) or RE_FREEZE.match(line):
                self.ctx.log(line.strip())


# ------------------------------------------------------------------- the job
def run(ctx, plugin_dir: Path) -> dict:
    """Auto-annotate one or more of a capture's streams. Sequentially.

    Sequential is not a policy this file enforces any more — the service holds
    one lock over one card — but sending them one at a time keeps the progress
    and the failures attributable to a stream, which a queue of six would not.
    """
    cid = int(ctx.params["capture_id"])
    base = _service(ctx.settings)
    cats = categories(ctx.params.get("prompts"), ctx.params.get("labels"))

    health = _get(f"{base}/health", timeout=20)
    if not health.get("ok"):
        raise NoService(f"the SAM service is not ready: {health}")
    ctx.log(f"service up, {health.get('vram', {}).get('free', '?')} MiB of GPU free")

    only = {s.strip() for s in str(ctx.params.get("streams") or "").split(",") if s.strip()}
    streams = [dict(r) for r in ctx.db.all(
        "SELECT * FROM streams WHERE capture_id=? ORDER BY idx", cid)
        if (not only or r["key"] in only) and r["enabled"]]
    if not streams:
        raise ValueError("no streams to annotate — check the stream keys you gave")

    out_dir = ctx.cfg.data_dir / "sam_auto" / str(cid)
    out_dir.mkdir(parents=True, exist_ok=True)

    force = bool(ctx.params.get("force"))
    done, skipped, failed = [], [], []
    per_stream: dict[str, dict] = {}

    for i, st in enumerate(streams):
        ctx.check()
        media = json.loads(st["media"] or "{}")
        src = media.get("path")
        if not src or not Path(src).is_file():
            failed.append(f"{st['key']}: no media file on disk")
            ctx.log(f"{st['key']}: no media file — skipped")
            continue
        xml = out_dir / f"{st['key']}.xml"
        if xml.exists() and not force:
            skipped.append(st["key"])
            ctx.log(f"{st['key']}: {xml.name} is already there — skipped (tick Force to redo)")
            per_stream[st["key"]] = {"xml": str(xml), "reused": True}
            continue

        # The service sees the videos and this data directory at the same paths
        # Quorum does — they are bind-mounted alike — so a path is a path and
        # there is no translation table any more.
        params = {"video": str(Path(src).resolve()), "output": str(xml),
                  "prompts": [p for p, _ in cats], "labels": [l for _, l in cats],
                  "flags": build_args(cats, ctx.params, prompts=False)}
        base_frac = MODEL_SHARE * i / len(streams)
        span = MODEL_SHARE / len(streams)
        r = Run(ctx, base, params)
        t0 = time.time()
        try:
            r.go(lambda f, msg, b=base_frac, s=span, k=st["key"]:
                 ctx.progress(b + s * f, f"{k}: {msg}"))
        except Cancelled:
            raise
        except Exception as e:
            failed.append(f"{st['key']}: {type(e).__name__}: {e}")
            ctx.log(failed[-1])
            if xml.exists():
                xml.replace(xml.with_suffix(".xml.PARTIAL"))
            continue
        mins = (time.time() - t0) / 60.0
        done.append(st["key"])
        per_stream[st["key"]] = {"xml": str(xml), "tracks": r.tracks,
                                 "minutes": round(mins, 1), "notes": r.notes[-12:]}
        ctx.log(f"{st['key']}: done in {mins:.0f}m — {r.tracks or 'no track summary'}")

    result = {"done": done, "skipped": skipped, "failed": failed, "streams": per_stream,
              "prompts": [{"prompt": p, "label": l} for p, l in cats],
              "out_dir": str(out_dir)}
    if not (done or skipped):
        result["message"] = f"nothing produced; {len(failed)} stream(s) failed"
        return result

    if ctx.params.get("import_result", True):
        ctx.progress(0.97, "importing the tracks")
        result.update(import_xml(ctx, cid, per_stream, cats))
    else:
        result["message"] = (f"{len(done)} annotated, {len(skipped)} reused, "
                             f"{len(failed)} failed — XML in {out_dir}")
    return result


# ---------------------------------------------------------------- importing
def import_xml(ctx, cid: int, per_stream: dict, cats) -> dict:
    """The XML into a mask layer, `provenance="model"`.

    Reusing `cvat_xml.parse_tracks` rather than copying it: this file is CVAT
    Video 1.1 with `<mask rle=…>`, which is the one that plugin already owns.
    Depending on it out loud beats a second parser that drifts.
    """
    cvat = ctx.host.plugins.get("cvat_xml")
    if cvat is None:
        raise RuntimeError("the cvat_xml plugin is not installed, so the XML cannot be read")
    parse = sys.modules[cvat.module_name].parse_tracks

    streams = {r["key"]: r["id"] for r in
               ctx.db.all("SELECT id, key FROM streams WHERE capture_id=?", cid)}
    key = str(ctx.params.get("layer") or "sam_auto")
    lay = ctx.write.layer(
        cid, key=key, name=str(ctx.params.get("layer_name") or "SAM auto"),
        type="mask.rle", provenance="model", replace=bool(ctx.params.get("replace_layer", True)),
        config={"generator": "sam3_auto_annotator",
                "prompts": [{"prompt": p, "label": l} for p, l in cats],
                "stride": int(ctx.params.get("frame_stride") or 1)})
    total, n_tracks = 0, 0
    for skey, info in per_stream.items():
        ctx.check()
        sid = streams.get(skey)
        if sid is None:
            continue
        tracks = parse(Path(info["xml"]))
        ctx.write.objects(lay, sid, tracks)
        n_tracks += len(tracks)
        total += sum(len(t["frames"]) for t in tracks)
        info["imported"] = len(tracks)
    ctx.emit_op(cid, "auto_imported",
                {"layer": lay, "tracks": n_tracks, "keyframes": total,
                 "streams": sorted(per_stream), "prompts": [l for _, l in cats]})
    ctx.host.publish(cid, {"t": "capture", "capture_id": cid})
    return {"layer_id": lay, "tracks": n_tracks, "keyframes": total,
            "message": f"{n_tracks} track(s), {total} keyframes into layer {key!r}"}


# ------------------------------------------------------------------ freezes
def freeze_summary(out_dir: Path) -> list[dict]:
    """What the run said about discontinuities, per stream.

    Worth surfacing rather than leaving in a sidecar: these recordings drop up
    to half their wall clock, every hole ends a track, and somebody looking at
    a camera that fragmented into 136 pieces should be able to see *why*
    without being told to go and read a JSON file.
    """
    out = []
    for f in sorted(out_dir.glob("*_freezes.json")):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        fr = d.get("freezes") or []
        out.append({
            "stream": f.name[:-len("_freezes.json")],
            "video": d.get("video", ""),
            "seconds_lost": d.get("seconds_lost", 0),
            "repeated": sum(1 for x in fr if x.get("type") == "repeated_frames"),
            "dropped": sum(1 for x in fr if x.get("type") == "dropped_frames"),
            "segments": len(fr) + 1,
        })
    return out
