"""Tests for frozen-input handling. No GPU, no model, no real footage needed.

Run anywhere cv2 + numpy exist (e.g. inside the SAM 3.1 image):
    docker run --rm -v "$(pwd)":/workspace -w /workspace \
      --entrypoint python3 cvat.pth.comfyorg.sam3.1:latest-gpu test_freeze.py

Builds synthetic videos with freezes injected at known frame ranges and asserts
the two properties that matter: frozen frames never reach the tracker, and no
track identity is ever carried across a freeze.
"""
import os
import queue
import shutil
import sys
import tempfile
import threading
import types
import xml.etree.ElementTree as ET

import numpy as np
import cv2

# torch and sam3 are imported by main.py but unused by the code under test.
if "torch" not in sys.modules:
    _t = types.ModuleType("torch")
    _t.Tensor = type("Tensor", (), {})
    _t.nn = types.SimpleNamespace(Module=type("Module", (), {}))
    sys.modules["torch"] = _t
if "sam3" not in sys.modules:
    _s, _b = types.ModuleType("sam3"), types.ModuleType("sam3.model_builder")
    _b.build_sam3_multiplex_video_predictor = lambda **kw: None
    _s.model_builder = _b
    sys.modules["sam3"], sys.modules["sam3.model_builder"] = _s, _b

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import main as M  # noqa: E402

W, H = 160, 120
FREEZES = [(30, 59), (100, 119)]      # inclusive raw-frame ranges that are dead
NFRAMES = 150
FOURCC = cv2.VideoWriter_fourcc(*"FFV1")   # lossless: a repeated frame stays byte-identical

_failures = []


def check(label, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        _failures.append(label)


class Args:
    def __init__(self, **kw):
        self.video = None
        self.chunk_frames = 20
        self.overlap_frames = 5
        self.frame_stride = 1
        self.max_resolution = 0
        self.freeze_min_frames = 10
        self.freeze_pixel_thresh = 0
        self.freeze_ratio = 0.0
        self.freeze_gap_ms = 400.0
        self.freeze_gap_mult = 5.0
        self.labels = ["person"]
        self.__dict__.update(kw)


def write_video(path, n, frozen_ranges):
    vw = cv2.VideoWriter(path, FOURCC, 25.0, (W, H))
    last = None
    for i in range(n):
        if any(a <= i <= b for a, b in frozen_ranges) and last is not None:
            frame = last.copy()                       # the stall: an exact repeat
        else:
            frame = np.random.randint(0, 40, (H, W, 3), np.uint8)   # sensor noise
            cv2.circle(frame, (10 + i, 60), 8, (255, 255, 255), -1)  # something moving
            last = frame
        vw.write(frame)
    vw.release()


def run_reader(args, video):
    args.video = video
    td = tempfile.mkdtemp()
    q, flog = queue.Queue(), []
    t = threading.Thread(target=M.reader_thread, args=(args, td, q, 25.0, flog))
    t.start()
    t.join(60)
    chunks = []
    while True:
        item = q.get()
        if item is None:
            break
        chunks.append(item)
    shutil.rmtree(td, ignore_errors=True)
    return chunks, flog


def run_postprocess(items, overlap=5, labels=("person",)):
    args = Args(overlap_frames=overlap, labels=list(labels))
    tracks, q = [], queue.Queue()
    t = threading.Thread(target=M.postprocess_worker, args=(args, q, tracks, W, H))
    t.start()
    for it in items:
        q.put(it)
    q.put(None)
    t.join(60)
    return tracks


def mask_at(x):
    m = np.zeros((H, W), bool)
    m[50:70, x:x + 20] = True
    return m


tmp = tempfile.mkdtemp()
dead = set(i for a, b in FREEZES for i in range(a, b + 1))
vid = os.path.join(tmp, "freeze.avi")
write_video(vid, NFRAMES, FREEZES)

print("\n[1] freezes are detected and never tracked")
chunks, flog = run_reader(Args(), vid)
check("exact freeze ranges detected",
      [(f["start_frame"], f["end_frame"]) for f in flog] == FREEZES)
tracked = [i for _, idxs, _ in chunks for i in idxs]
check("no frozen frame reaches the tracker", not (set(tracked) & dead))
check("every live frame is still covered", set(tracked) == set(range(NFRAMES)) - dead)
check("no chunk straddles a freeze (local->global index math stays valid)",
      all(idxs == list(range(idxs[0], idxs[0] + len(idxs))) for _, idxs, _ in chunks))
check("a new segment starts after each freeze",
      [idxs[0] for _, idxs, ns in chunks if ns] == [0, 60, 120])
check("chunk dirs hold exactly their frames",
      all(len(os.listdir(cd)) == len(idxs) for cd, idxs, _ in chunks if os.path.isdir(cd)))

print("\n[2] a static but LIVE scene is not mistaken for a freeze")
vid2 = os.path.join(tmp, "static.avi")
vw = cv2.VideoWriter(vid2, FOURCC, 25.0, (W, H))
base = np.full((H, W, 3), 100, np.uint8)
for _ in range(80):
    vw.write(np.clip(base.astype(int) + np.random.randint(-2, 3, (H, W, 3)),
                     0, 255).astype(np.uint8))
vw.release()
check("read noise keeps a motionless scene out of the freeze log",
      run_reader(Args(), vid2)[1] == [])

print("\n[3] stalls shorter than --freeze_min_frames are kept, not discarded")
vid3 = os.path.join(tmp, "short.avi")
write_video(vid3, 60, [(20, 24)])                     # 5 dups, threshold is 10
ch3, flog3 = run_reader(Args(), vid3)
check("short stall is not called a freeze", flog3 == [])
check("its frames are still tracked",
      set(range(60)) <= set(i for _, idxs, _ in ch3 for i in idxs))

print("\n[4] --freeze_min_frames 0 restores the previous behaviour")
ch4, flog4 = run_reader(Args(freeze_min_frames=0), vid)
check("detection disabled", flog4 == [])
check("all frames tracked, freezes included",
      set(i for _, idxs, _ in ch4 for i in idxs) == set(range(NFRAMES)))

print("\n[5] --frame_stride keeps grid alignment across a freeze")
ch5, flog5 = run_reader(Args(frame_stride=5, chunk_frames=8, overlap_frames=2), vid)
check("freezes still found with stride 5",
      [(f["start_frame"], f["end_frame"]) for f in flog5] == FREEZES)
f5 = [i for _, idxs, _ in ch5 for i in idxs]
check("kept frames stay on the stride grid", all(i % 5 == 0 for i in f5))
check("no frozen frame kept", not (set(f5) & dead))
check("strided chunks stay contiguous on the grid",
      all(idxs == list(range(idxs[0], idxs[0] + 5 * len(idxs), 5)) for _, idxs, _ in ch5))

print("\n[6] dropped-frame holes (the mode this footage actually exhibits)")
# Measured on the real cameras: cam6 wrote 2.0s holes, cam7 up to 4.4s, while
# cam1 ticks along at a steady 40ms. No pixels reveal these -- only timestamps.
gw = M._GapWatch(400.0, 5.0, warmup=10)
t = 0.0
for _ in range(12):                                   # learn a 40ms nominal
    gw.update(t)
    t += 40.0
check("steady 25fps stream reports no holes", gw.update(t) == 0.0)
t += 2000.0                                           # the recorder stalls, writing nothing
check("a 2.0s hole is flagged", gw.update(t) == 2000.0)
t += 100.0
check("a 100ms wobble is not flagged", gw.update(t) == 0.0)

slow = M._GapWatch(400.0, 5.0, warmup=10)             # 2fps camera: 500ms IS its nominal
t = 0.0
for _ in range(12):
    slow.update(t)
    t += 500.0
check("a genuinely slow camera does not break on every frame (mult floor)",
      slow.update(t) == 0.0)
t += 4000.0
check("but a 4s hole still breaks it", slow.update(t) == 4000.0)

blind = M._GapWatch(400.0, 5.0, warmup=10)
check("a backend with no timestamps degrades to doing nothing",
      all(blind.update(0.0) == 0.0 for _ in range(50)))

print("\n[7] identities cross a normal chunk boundary but never a freeze")
a = (list(range(0, 20)), {"person": {1: {f: mask_at(30) for f in range(0, 20)}}}, True)
b = (list(range(15, 35)), {"person": {7: {f: mask_at(30) for f in range(15, 35)}}}, False)
check("same object across a normal boundary -> 1 global track",
      len(run_postprocess([a, b])) == 1)
split = run_postprocess([a, (b[0], b[1], True)])   # identical, but post-freeze
check("same object across a freeze -> 2 separate global tracks", len(split) == 2)
check("no track spans the boundary",
      all(not (set(t["frames"]) & set(range(0, 15)))
          or not (set(t["frames"]) & set(range(20, 35))) for t in split))

print("\n[8] exported XML shows tracks ending at the freeze, not interpolating over it")


def rle_at(x):
    r, l, t, rr, bb = M.mask_to_cvat_rle(mask_at(x))
    return {"rle": r, "left": l, "top": t, "width": rr - l + 1, "height": bb - t + 1}


out = os.path.join(tmp, "a.xml")
M.create_cvat_xml([{"label": "human", "frames": {f: rle_at(5) for f in range(0, 30)}},
                   {"label": "human", "frames": {f: rle_at(30) for f in range(60, 100)}}],
                  "v.mp4", NFRAMES, W, H, out, labels=["human"], frame_stride=1)
root = ET.parse(out).getroot()
for tr in root.findall("track"):
    vis = sorted(int(e.get("frame")) for e in tr.findall("mask") if e.get("outside") == "0")
    check(f"track {tr.get('id')} has no visible mask inside the frozen range",
          not (set(vis) & set(range(30, 60))))
    check(f"track {tr.get('id')} has no interpolated gap", all(
        b - a == 1 for a, b in zip(vis, vis[1:])))
check("pre-freeze track closes with outside=1 at the freeze start",
      any(int(e.get("frame")) == 30 and e.get("outside") == "1"
          for e in root.findall("track")[0].findall("mask")))
check("post-freeze track is marked absent before it first appears",
      any(int(e.get("frame")) == 0 and e.get("outside") == "1"
          for e in root.findall("track")[1].findall("mask")))

print("\n[9] several prompt categories in one pass stay separate")
# Two categories whose masks overlap perfectly across the chunk boundary: if the
# stitcher matched by IoU alone, ignoring the category, the chair would be
# absorbed into the person's track (or vice versa).
cats = ("person", "chair")
c1 = (list(range(0, 20)), {"person": {1: {f: mask_at(30) for f in range(0, 20)}},
                           "chair": {1: {f: mask_at(30) for f in range(0, 20)}}}, True)
c2 = (list(range(15, 35)), {"person": {4: {f: mask_at(30) for f in range(15, 35)}},
                            "chair": {9: {f: mask_at(30) for f in range(15, 35)}}}, False)
multi = run_postprocess([c1, c2], labels=cats)
check("identical person and chair masks do not merge into one track", len(multi) == 2)
check("each category keeps its own label", sorted(t["label"] for t in multi) == ["chair", "person"])
check("each category still stitches across the boundary",
      all(set(t["frames"]) == set(range(0, 35)) for t in multi))

# A category that is absent from a window must not disturb the others.
sparse = run_postprocess(
    [c1,
     (list(range(15, 35)), {"person": {4: {f: mask_at(30) for f in range(15, 35)}}}, False)],
    labels=cats)
by_label = {t["label"]: t for t in sparse}
check("a category missing from a window does not break the others",
      set(by_label["person"]["frames"]) == set(range(0, 35)))
check("the absent category's track simply ends",
      set(by_label["chair"]["frames"]) == set(range(0, 20)))

out2 = os.path.join(tmp, "b.xml")
M.create_cvat_xml(multi, "v.mp4", NFRAMES, W, H, out2, labels=list(cats), frame_stride=1)
root2 = ET.parse(out2).getroot()
check("every category is declared in the task meta",
      [e.findtext("name") for e in root2.iter("label")] == ["person", "chair"])
check("declared categories get distinct colours",
      len({e.findtext("color") for e in root2.iter("label")}) == 2)
check("each exported track carries its own label",
      sorted(t.get("label") for t in root2.findall("track")) == ["chair", "person"])

out3 = os.path.join(tmp, "c.xml")   # a prompt that matched nothing all video
M.create_cvat_xml(multi, "v.mp4", NFRAMES, W, H, out3,
                  labels=["person", "chair", "dog"], frame_stride=1)
check("a category with no tracks is still declared as a label",
      "dog" in [e.findtext("name") for e in ET.parse(out3).getroot().iter("label")])

shutil.rmtree(tmp, ignore_errors=True)
if _failures:
    print(f"\n{len(_failures)} CHECK(S) FAILED:")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nALL CHECKS PASSED")
