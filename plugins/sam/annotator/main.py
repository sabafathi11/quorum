import os
# Must be set before torch initializes CUDA: reduces fragmentation-driven OOMs.
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
import argparse
import json
import sys
import cv2
import numpy as np
import xml.etree.ElementTree as ET
from xml.dom import minidom
import torch
import tempfile
import shutil
import threading
import queue
import time
import inspect
import traceback

from sam3.model_builder import build_sam3_multiplex_video_predictor


# ----------------------------------------------------------------------------
# CVAT export helpers
# ----------------------------------------------------------------------------

def calc_iou(mask1, mask2):
    intersection = np.logical_and(mask1, mask2).sum()
    union = np.logical_or(mask1, mask2).sum()
    if union == 0:
        return 0.0
    return intersection / union


def mask_to_cvat_rle(mask):
    [height, width] = mask.shape
    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)
    if not np.any(rows):
        return None
    ymin, ymax = np.where(rows)[0][[0, -1]]
    xmin, xmax = np.where(cols)[0][[0, -1]]
    cropped_mask = mask[ymin:ymax + 1, xmin:xmax + 1]
    pixels = (np.asarray(cropped_mask).reshape(-1) != 0).astype(np.uint8)
    if pixels.size == 0:
        return None
    changes = np.flatnonzero(pixels[1:] != pixels[:-1]) + 1
    rle = np.diff(np.concatenate(([0], changes, [pixels.size]))).tolist()
    if pixels[0] == 1:
        rle.insert(0, 0)

    rle_str = ",".join(map(str, rle))
    return rle_str, int(xmin), int(ymin), int(xmax), int(ymax)


# Distinct enough to tell apart at a glance in the CVAT canvas. Cycled if a run
# ever has more categories than this, which no sane prompt list will.
_LABEL_COLORS = ['#ff0000', '#00b300', '#0066ff', '#ff9900', '#cc00cc',
                 '#00cccc', '#ffcc00', '#8b4513', '#7b68ee', '#ff69b4']


def create_cvat_xml(tracks, video_name, num_frames, width, height, output_file,
                    labels=None, frame_stride=1):
    """Write CVAT Video 1.1 XML.

    `tracks` is a list of {"label": <name>, "frames": {frame_idx: rle_data}}: one
    entry per global track, each carrying the category it was prompted for.
    `labels` is the ordered category list to declare in the task meta -- passed
    explicitly so a category that found nothing in the whole video still exists
    as a label in CVAT, rather than silently vanishing from the imported task.
    """
    if labels is None:
        labels = list(dict.fromkeys(t['label'] for t in tracks if t.get('frames')))

    annotations = ET.Element('annotations')
    ET.SubElement(annotations, 'version').text = '1.1'
    meta = ET.SubElement(annotations, 'meta')
    task = ET.SubElement(meta, 'task')
    ET.SubElement(task, 'id').text = '1'
    ET.SubElement(task, 'name').text = 'Auto Annotation'
    ET.SubElement(task, 'size').text = str(num_frames)
    ET.SubElement(task, 'mode').text = 'interpolation'
    ET.SubElement(task, 'overlap').text = '0'

    labels_elem = ET.SubElement(task, 'labels')
    for i, name in enumerate(labels):
        label = ET.SubElement(labels_elem, 'label')
        ET.SubElement(label, 'name').text = name
        ET.SubElement(label, 'color').text = _LABEL_COLORS[i % len(_LABEL_COLORS)]
        ET.SubElement(label, 'type').text = 'any'

    source = ET.SubElement(task, 'source')
    ET.SubElement(source, 'name').text = video_name

    original_size = ET.SubElement(task, 'original_size')
    ET.SubElement(original_size, 'width').text = str(width)
    ET.SubElement(original_size, 'height').text = str(height)

    for obj_idx, entry in enumerate(tracks):
        track = entry['frames']
        if not track:
            continue

        track_elem = ET.SubElement(annotations, 'track', id=str(obj_idx),
                                   label=entry['label'], source='manual')
        sorted_frames = sorted(track.keys())
        if not sorted_frames:
            continue

        if sorted_frames[0] > 0:
            first_mask = track[sorted_frames[0]]
            ET.SubElement(track_elem, 'mask',
                          frame="0",
                          outside='1',
                          occluded='0',
                          keyframe='1',
                          rle=first_mask['rle'],
                          left=str(first_mask['left']),
                          top=str(first_mask['top']),
                          width=str(first_mask['width']),
                          height=str(first_mask['height']),
                          z_order='0')

        for i, frame_idx in enumerate(sorted_frames):
            mask_data = track[frame_idx]

            ET.SubElement(track_elem, 'mask',
                          frame=str(frame_idx),
                          outside='0',
                          occluded='0',
                          keyframe='1',
                          rle=mask_data['rle'],
                          left=str(mask_data['left']),
                          top=str(mask_data['top']),
                          width=str(mask_data['width']),
                          height=str(mask_data['height']),
                          z_order='0')

            if i < len(sorted_frames) - 1:
                next_frame_idx = sorted_frames[i + 1]
                if next_frame_idx > frame_idx + frame_stride:
                    ET.SubElement(track_elem, 'mask',
                                  frame=str(frame_idx + 1),
                                  outside='1',
                                  occluded='0',
                                  keyframe='1',
                                  rle=mask_data['rle'],
                                  left=str(mask_data['left']),
                                  top=str(mask_data['top']),
                                  width=str(mask_data['width']),
                                  height=str(mask_data['height']),
                                  z_order='0')
            else:
                if frame_idx + frame_stride < num_frames - 1:
                    ET.SubElement(track_elem, 'mask',
                                  frame=str(frame_idx + 1),
                                  outside='1',
                                  occluded='0',
                                  keyframe='1',
                                  rle=mask_data['rle'],
                                  left=str(mask_data['left']),
                                  top=str(mask_data['top']),
                                  width=str(mask_data['width']),
                                  height=str(mask_data['height']),
                                  z_order='0')

    xmlstr = minidom.parseString(ET.tostring(annotations)).toprettyxml(indent="  ")
    with open(output_file, "w") as f:
        f.write(xmlstr)


# ----------------------------------------------------------------------------
# Stage 1: Reader thread (CPU) + frozen-input detection
# ----------------------------------------------------------------------------

def _is_duplicate_frame(gray, prev_gray, pixel_thresh, ratio_thresh):
    """True when this frame carries no new information relative to the last one.

    A stalled NVR/encoder re-emits the same decoded picture, so the default
    (pixel_thresh=0, ratio_thresh=0.0) asks for byte-identical greyscale: not a
    single pixel moved. That is deliberately strict -- a live sensor always has
    read noise, so a merely *static* scene (nobody moving) still differs frame to
    frame and is NOT mistaken for a freeze. Loosen both only when the recording
    burns in a ticking timestamp: then the clock digits are the sole changing
    pixels, e.g. --freeze_pixel_thresh 8 --freeze_ratio 0.001.
    """
    d = cv2.absdiff(gray, prev_gray)
    if pixel_thresh > 0:
        _, d = cv2.threshold(d, pixel_thresh, 255, cv2.THRESH_BINARY)
    return cv2.countNonZero(d) <= ratio_thresh * d.size


class _GapWatch:
    """Flags wall-clock holes where the recorder dropped frames.

    The other failure mode. A stalled NVR does not always repeat the last
    picture -- these cameras simply stop writing, so two frames that are
    adjacent in the file can be seconds apart in reality (measured on this
    footage: cam6 lost 74s of 600s, cam7 lost 277s). Nothing about the pixels
    reveals it; only the presentation timestamps do. Left undetected it is the
    *worse* case, because the overlap IoU match sees two consecutive frames and
    happily carries an identity across a hole that everyone moved through.

    The threshold is `max(min_ms, mult * median inter-frame interval)`: the
    absolute term is what actually matters (how much motion was missed), and
    the relative term stops a genuinely low-framerate camera from breaking on
    every single frame. If the backend reports no timestamps at all, every gap
    reads as 0 and this degrades to doing nothing.
    """

    def __init__(self, min_ms, mult, warmup=200):
        self.min_ms, self.mult, self.warmup = min_ms, mult, warmup
        self.samples, self.nominal, self.prev = [], None, None

    def update(self, ts_ms):
        """Return the size in ms of the hole before this frame, else 0.0."""
        if ts_ms is None or ts_ms < 0:
            return 0.0
        if self.prev is None:
            self.prev = ts_ms
            return 0.0
        gap, self.prev = ts_ms - self.prev, ts_ms
        if gap <= 0:
            return 0.0
        if len(self.samples) < self.warmup:
            self.samples.append(gap)
            if len(self.samples) == self.warmup:
                self.nominal = float(np.median(self.samples))
        thr = self.min_ms
        if self.nominal:
            thr = max(thr, self.mult * self.nominal)
        return gap if gap > thr else 0.0


def _kept_frames(args, cap, store_dir, fps, freeze_log):
    """Decode the video and yield only the frames worth tracking.

    Yields ('frame', raw_idx, jpg_path) per kept frame, and ('break',) at every
    point where continuity is lost. Two ways that happens:

      * repeated frames -- the recorder re-sent the last picture. Those frames
        are dropped rather than yielded: they are copies, so tracking them only
        feeds the memory bank a scene that never happened.
      * a timestamp hole -- the recorder wrote nothing at all. There are no
        frames to drop; the damage is that the surviving frames lie about being
        adjacent.

    Either way the caller turns the break into a hard segment boundary, so no
    identity is carried across the discontinuity.
    """
    detect = args.freeze_min_frames > 0
    gapwatch = _GapWatch(args.freeze_gap_ms, args.freeze_gap_mult) \
        if args.freeze_gap_ms > 0 else None
    prev_gray = None
    raw_idx = -1
    pending = []          # duplicates seen so far, not yet long enough to be a freeze
    in_freeze = False
    freeze_start = None

    def save(idx, frame):
        if args.max_resolution > 0:
            h, w = frame.shape[:2]
            if max(h, w) > args.max_resolution:
                s = args.max_resolution / max(h, w)
                frame = cv2.resize(frame, (int(w * s), int(h * s)),
                                   interpolation=cv2.INTER_AREA)
        p = os.path.join(store_dir, f"{idx:07d}.jpg")
        cv2.imwrite(p, frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
        return p

    def record_repeat(a, b):
        n = b - a + 1
        secs = n / fps if fps > 0 else 0.0
        freeze_log.append({"type": "repeated_frames", "start_frame": a, "end_frame": b,
                           "num_frames": n, "seconds": round(secs, 2)})
        print(f" [FREEZE] repeated frames {a}..{b} ({n} frames, {secs:.1f}s) "
              f"-- not tracked, tracks are cut here")

    def record_gap(after, ms):
        freeze_log.append({"type": "dropped_frames", "after_frame": after,
                           "before_frame": after + 1, "seconds": round(ms / 1000.0, 2)})
        print(f" [FREEZE] {ms / 1000.0:.2f}s of wall-clock missing between frames "
              f"{after} and {after + 1} (recorder dropped frames) "
              f"-- tracks are cut here")

    while True:
        ts_ms = cap.get(cv2.CAP_PROP_POS_MSEC) if gapwatch else -1.0
        ret, frame = cap.read()
        if not ret:
            break
        raw_idx += 1

        # A hole in the timeline: resolve whatever duplicate state is open, then
        # cut. Nothing to drop here -- those frames were never recorded.
        gap_ms = gapwatch.update(ts_ms) if gapwatch else 0.0
        if gap_ms and raw_idx > 0:
            if in_freeze:
                record_repeat(freeze_start, raw_idx - 1)
                in_freeze, freeze_start = False, None
            else:
                for i, p in pending:
                    if p is not None:
                        yield ('frame', i, p)
                pending = []
            record_gap(raw_idx - 1, gap_ms)
            yield ('break',)
            prev_gray = None          # nothing meaningful to compare across the hole

        dup = False
        if detect:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if prev_gray is not None:
                dup = _is_duplicate_frame(gray, prev_gray,
                                          args.freeze_pixel_thresh, args.freeze_ratio)
            prev_gray = gray

        if dup:
            if in_freeze:
                continue                       # dead frame, drop it
            # Hold it back: a duplicate run shorter than the threshold is normal
            # video (a still moment), not a stall, and must be tracked as usual.
            pending.append((raw_idx, save(raw_idx, frame)
                            if raw_idx % args.frame_stride == 0 else None))
            if len(pending) >= args.freeze_min_frames:
                in_freeze = True
                freeze_start = pending[0][0]
                for _, p in pending:
                    if p is not None:
                        os.remove(p)
                pending = []
            continue

        if in_freeze:
            record_repeat(freeze_start, raw_idx - 1)
            yield ('break',)
            in_freeze = False
            freeze_start = None
        else:
            for i, p in pending:               # run was too short to be a freeze
                if p is not None:
                    yield ('frame', i, p)
            pending = []

        if raw_idx % args.frame_stride == 0:
            yield ('frame', raw_idx, save(raw_idx, frame))

    if in_freeze:
        record_repeat(freeze_start, raw_idx)   # the freeze ran to the end of the file
    else:
        for i, p in pending:
            if p is not None:
                yield ('frame', i, p)


def reader_thread(args, temp_dir, chunk_queue, fps, freeze_log):
    cap = cv2.VideoCapture(args.video)
    store_dir = os.path.join(temp_dir, "store")
    os.makedirs(store_dir, exist_ok=True)

    step = max(1, args.chunk_frames - args.overlap_frames)
    buf = []              # kept frames in the current window: [(raw_idx, path)]
    new_segment = True    # first chunk of a segment: nothing to match backwards to

    def emit(frames, is_new_segment):
        chunk_dir = os.path.join(temp_dir, f"chunk_{frames[0][0]}")
        os.makedirs(chunk_dir, exist_ok=True)
        for i, (_, src) in enumerate(frames):
            dst = os.path.join(chunk_dir, f"{i:05d}.jpg")
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy(src, dst)
        chunk_queue.put((chunk_dir, [idx for idx, _ in frames], is_new_segment))

    def drop(frames):
        # Safe right after emit(): the chunk dir holds its own hard link.
        for _, p in frames:
            try:
                os.remove(p)
            except OSError:
                pass

    try:
        for item in _kept_frames(args, cap, store_dir, fps, freeze_log):
            if item[0] == 'frame':
                buf.append((item[1], item[2]))
                if len(buf) >= args.chunk_frames:
                    emit(buf, new_segment)
                    new_segment = False
                    drop(buf[:step])
                    buf = buf[step:]
                continue

            # Freeze: close the segment on the last good frame. Anything already
            # covered by the previous chunk's overlap is not worth re-tracking.
            if buf and (new_segment or len(buf) > args.overlap_frames):
                emit(buf, new_segment)
            drop(buf)
            buf = []
            new_segment = True

        if buf and (new_segment or len(buf) > args.overlap_frames):
            emit(buf, new_segment)
        drop(buf)
    except Exception:
        traceback.print_exc()
    finally:
        cap.release()
        chunk_queue.put(None)


# ----------------------------------------------------------------------------
# Stage 3: Postprocess worker (CPU)
# ----------------------------------------------------------------------------

def postprocess_worker(args, post_queue, global_tracks, width, height):
    """Stitch each window's local object ids onto the running global tracks.

    Categories are stitched independently: `prev_overlap` is keyed by category,
    and the IoU match for one category only ever sees that category's previous
    overlap. A `person` and a `chair` masklet that happen to overlap heavily --
    the chair someone is sitting on, say -- must not be joined into one track,
    and they carry different labels in the output, so a shared id would be
    meaningless anyway.
    """
    prev_overlap = {}          # label -> {global_id: {frame: mask}}

    while True:
        item = post_queue.get()
        if item is None:
            break
        frame_indices, masks_by_label, new_segment = item

        # A frozen stretch destroyed the visual continuity the IoU match relies
        # on: whoever was on screen has teleported by the time the feed resumes.
        # Start identities fresh rather than inventing a correspondence.
        if new_segment:
            prev_overlap = {}

        for label in args.labels:
            prev_overlap[label] = _stitch_chunk(
                args, frame_indices, masks_by_label.get(label) or {},
                prev_overlap.get(label) or {}, label, global_tracks, width, height)


def _stitch_chunk(args, frame_indices, chunk_masks, prev_overlap, label,
                  global_tracks, width, height):
    """Merge one category's masks for one window. Returns its new tail overlap."""
    if not chunk_masks:
        return {}

    mapping = {}
    if not prev_overlap:
        for local_id in chunk_masks.keys():
            mapping[local_id] = len(global_tracks)
            global_tracks.append({"label": label, "frames": {}})
    else:
        iou_matrix = []
        for local_id, local_track in chunk_masks.items():
            for global_id, global_track in prev_overlap.items():
                ious = []
                for f in frame_indices[:args.overlap_frames]:
                    if f in local_track and f in global_track:
                        ious.append(calc_iou(local_track[f], global_track[f]))
                if ious:
                    avg_iou = sum(ious) / len(ious)
                    if avg_iou > 0.10:
                        iou_matrix.append((avg_iou, local_id, global_id))

        iou_matrix.sort(reverse=True, key=lambda x: x[0])
        assigned_globals = set()
        for iou_score, l_id, g_id in iou_matrix:
            if l_id not in mapping and g_id not in assigned_globals:
                mapping[l_id] = g_id
                assigned_globals.add(g_id)

        for local_id in chunk_masks.keys():
            if local_id not in mapping:
                mapping[local_id] = len(global_tracks)
                global_tracks.append({"label": label, "frames": {}})

    new_overlap = {}
    tail = set(frame_indices[-args.overlap_frames:]) if args.overlap_frames > 0 else set()

    for local_id, local_track in chunk_masks.items():
        global_id = mapping[local_id]
        for global_f_idx, mask in local_track.items():
            full = mask
            if full.shape[0] != height or full.shape[1] != width:
                full = cv2.resize(full.astype(np.uint8), (width, height),
                                  interpolation=cv2.INTER_NEAREST) > 0
            rle_data = mask_to_cvat_rle(full)
            if rle_data:
                rle_str, left, top, right, bottom = rle_data
                global_tracks[global_id]["frames"][global_f_idx] = {
                    "rle": rle_str,
                    "left": left, "top": top,
                    "width": right - left + 1, "height": bottom - top + 1
                }

            if global_f_idx in tail:
                new_overlap.setdefault(global_id, {})[global_f_idx] = mask

    return new_overlap


# ----------------------------------------------------------------------------
# VRAM diagnostics
# ----------------------------------------------------------------------------

def _walk_modules(root):
    seen, stack = set(), [root]
    while stack:
        m = stack.pop()
        if m is None or id(m) in seen:
            continue
        seen.add(id(m))
        yield m
        for attr in ("tracker", "detector", "model"):
            stack.append(getattr(m, attr, None))


def _cuda_bytes(obj, seen=None):
    if seen is None:
        seen = set()
    oid = id(obj)
    if oid in seen:
        return 0
    seen.add(oid)
    if isinstance(obj, torch.Tensor):
        return obj.element_size() * obj.nelement() if obj.is_cuda else 0
    if isinstance(obj, dict):
        return sum(_cuda_bytes(v, seen) for v in obj.values())
    if isinstance(obj, (list, tuple, set)):
        return sum(_cuda_bytes(v, seen) for v in obj)
    d = getattr(obj, "__dict__", None)
    if isinstance(d, dict):
        return _cuda_bytes(d, seen)
    return 0


def report_session_vram(predictor, session_id, tag=""):
    """Print total CUDA allocation and a two-level per-key breakdown of the
    live inference_state, to attribute memory growth."""
    try:
        sess = predictor._all_inference_states[session_id]
        state = sess["state"] if isinstance(sess, dict) and "state" in sess else sess
        if not isinstance(state, dict):
            print(f" [VRAMDBG {tag}] state is {type(state).__name__}, cannot walk")
            return
        sizes = []
        for k, v in state.items():
            n = len(v) if hasattr(v, "__len__") else -1
            sizes.append((k, _cuda_bytes(v), n, v))
        sizes.sort(key=lambda x: -x[1])
        alloc = torch.cuda.memory_allocated() / 2**30
        resv = torch.cuda.memory_reserved() / 2**30
        top = ", ".join(f"{k}={s / 2**30:.2f}G(n={n})" for k, s, n, _ in sizes[:8]
                        if s > 1e6)
        print(f" [VRAMDBG {tag}] alloc={alloc:.2f}G reserved={resv:.2f}G | {top or 'state holds <1MB on GPU'}")
        for k, s, n, v in sizes[:3]:
            if s < 0.5 * 2**30:
                continue
            children = None
            if isinstance(v, dict):
                children = list(v.items())
            elif isinstance(v, (list, tuple)):
                children = list(enumerate(v))
            elif hasattr(v, "__dict__"):
                children = list(vars(v).items())
            if not children:
                continue
            subs = sorted(((str(kk), _cuda_bytes(vv),
                            len(vv) if hasattr(vv, "__len__") else -1)
                           for kk, vv in children), key=lambda x: -x[1])
            det = ", ".join(f"{kk}={ss / 2**30:.2f}G(n={nn})"
                            for kk, ss, nn in subs[:8] if ss > 50e6)
            if det:
                print(f" [VRAMDBG {tag}]   {k} -> {det}")
    except Exception as e:
        print(f" [VRAMDBG {tag}] failed: {e}")


# ----------------------------------------------------------------------------
# Core Mitigations
# ----------------------------------------------------------------------------

def apply_memory_fixes(predictor, max_obj_ptrs=8, grounding_batch_size=8):
    """VRAM mitigations for long offline tracking chunks."""
    import inspect
    import traceback

    # 1. Global Stream Wrapper to print tracebacks transparently
    orig_handle_stream = predictor.handle_stream_request
    def patched_handle_stream(*args, **kwargs):
        try:
            for item in orig_handle_stream(*args, **kwargs):
                yield item
        except Exception as e:
            print(f"\n[CRITICAL] Stream Request Exception:\n")
            traceback.print_exc()
            raise
    predictor.handle_stream_request = patched_handle_stream

    for m in _walk_modules(predictor.model):
        # --- Fix #1: output caching ---------------------------------------
        if hasattr(m, "offload_output_to_cpu_for_eval"):
            setattr(m, "offload_output_to_cpu_for_eval", True)
            print(f"[VRAM] {type(m).__name__}.offload_output_to_cpu_for_eval = True")

        if any("_cache_frame_outputs" in klass.__dict__ for klass in type(m).__mro__):
            orig_cache = m._cache_frame_outputs

            def _make_patched(orig):
                def _cache_patched(inference_state, frame_idx, obj_id_to_mask, **kw):
                    moved = {k: (v.to("cpu") if isinstance(v, torch.Tensor) else v)
                             for k, v in obj_id_to_mask.items()}
                    return orig(inference_state, frame_idx, moved, **kw)
                return _cache_patched

            m._cache_frame_outputs = _make_patched(orig_cache)
            print(f"[VRAM] {type(m).__name__}._cache_frame_outputs -> cpu")

        # --- Fix #3: fewer obj_ptr tokens ---------------------------------
        if hasattr(m, "max_obj_ptrs_in_encoder"):
            setattr(m, "max_obj_ptrs_in_encoder", max_obj_ptrs)
            print(f"[VRAM] {type(m).__name__}.max_obj_ptrs_in_encoder = {max_obj_ptrs}")

        # --- Fix #4a: attribute-level grounding batch (heuristic, LOGGED) --
        for k, v in list(vars(m).items()):
            if isinstance(v, int) and v == 16 and any(x in k.lower() for x in ('batch', 'chunk')):
                setattr(m, k, grounding_batch_size)
                print(f"[VRAM] {type(m).__name__}.{k}: 16 -> {grounding_batch_size} (heuristic)")

    # --- Fix #4b: force batch_size kwarg on detector grounding methods -----
    detector = getattr(predictor.model, "detector", None)
    if detector is not None:
        for method_name in ("forward_video_grounding_multigpu",
                            "forward_video_grounding_batched_multigpu",
                            "_build_multigpu_buffer_next_chunk"):
            if not hasattr(detector, method_name):
                continue
            orig_method = getattr(detector, method_name)
            try:
                sig = inspect.signature(orig_method)
            except (TypeError, ValueError):
                continue
            if "batch_size" not in sig.parameters:
                continue

            def make_patched(orig_fn, name=method_name):
                def patched_fn(*args, **kwargs):
                    kwargs.setdefault("batch_size", grounding_batch_size)
                    return orig_fn(*args, **kwargs)
                return patched_fn

            setattr(detector, method_name, make_patched(orig_method))
            print(f"[VRAM] detector.{method_name}: batch_size defaults to {grounding_batch_size}")

    # --- Fix #5: Bypass Multiplex Zombie State Crash -----------------------
    orig_propagate = predictor.model._propogate_tracker_one_frame_local_gpu

    def patched_propagate(inference_states, frame_idx, reverse, run_mem_encoder=False,
                          filter_obj_ids=None):
        if filter_obj_ids is not None:
            # Partial-propagation path (mask refinement). It has its own index
            # bookkeeping and no zombie states to bypass, so leave it alone.
            return orig_propagate(inference_states, frame_idx=frame_idx, reverse=reverse,
                                  run_mem_encoder=run_mem_encoder,
                                  filter_obj_ids=filter_obj_ids)

        obj_ids_local = []
        low_res_masks_list = []
        obj_scores_list = []
        for inference_state in inference_states:
            if len(inference_state["obj_ids"]) == 0:
                continue
            try:
                num_frames_propagated = 0
                for out in predictor.model.tracker.propagate_in_video(
                    inference_state,
                    start_frame_idx=frame_idx,
                    max_frame_num_to_track=0,
                    reverse=reverse,
                    tqdm_disable=True,
                    run_mem_encoder=run_mem_encoder,
                ):
                    out_frame_idx, out_obj_ids, out_low_res_masks, _, out_obj_scores = out
                    num_frames_propagated += 1

                assert num_frames_propagated == 1 and out_frame_idx == frame_idx
                if len(out_obj_ids) > 0:
                    obj_ids_local.extend(out_obj_ids)
                    low_res_masks_list.append(out_low_res_masks.squeeze(1))
                    obj_scores_list.append(out_obj_scores.squeeze(1))

            except Exception as e:
                err_str = str(e)
                if "No points are provided" in err_str or "conditioning objects not found" in err_str:
                    out_obj_ids = inference_state["obj_ids"]
                    num_objs = len(out_obj_ids)
                    H_mask = W_mask = predictor.model.tracker.low_res_mask_size

                    out_low_res_masks = torch.zeros(num_objs, H_mask, W_mask, device=predictor.model.device)
                    out_obj_scores = torch.full((num_objs,), -1024.0, device=predictor.model.device)

                    obj_ids_local.extend(out_obj_ids)
                    low_res_masks_list.append(out_low_res_masks)
                    obj_scores_list.append(out_obj_scores)
                    continue
                print(f"\n[CRITICAL] Error inside patched_propagate:\n")
                traceback.print_exc()
                raise

        H_mask = W_mask = predictor.model.tracker.low_res_mask_size
        if len(low_res_masks_list) > 0:
            low_res_masks_local = torch.cat(low_res_masks_list, dim=0)
            obj_scores_local = torch.cat(obj_scores_list, dim=0)

            # The caller asserts these ids equal tracker_metadata["obj_ids_per_gpu"],
            # which is creation order, i.e. ascending. Concatenating the states in
            # list order does NOT give that: a masklet detected later can live in an
            # earlier inference state, producing e.g. [0..14, 17, 15, 16]. The assert
            # in run_tracker_propagation then fires, the whole propagate_in_video
            # stream dies, and the REST of the window is left with no masks at all
            # (measured: 8-16s holes in otherwise fine footage). Sort the ids and
            # permute the mask/score rows with them -- everything downstream indexes
            # those tensors positionally against the id list, so the two must agree.
            order = sorted(range(len(obj_ids_local)), key=lambda i: obj_ids_local[i])
            if order != list(range(len(order))):
                idx = torch.as_tensor(order, device=low_res_masks_local.device,
                                      dtype=torch.long)
                low_res_masks_local = low_res_masks_local.index_select(0, idx)
                obj_scores_local = obj_scores_local.index_select(0, idx)
                obj_ids_local = [obj_ids_local[i] for i in order]

            from sam3.model.sam3_tracker_utils import fill_holes_in_mask_scores
            low_res_masks_local = fill_holes_in_mask_scores(
                low_res_masks_local.unsqueeze(1),
                max_area=predictor.model.fill_hole_area,
                fill_holes=True,
                remove_sprinkles=True,
            ).squeeze(1)
        else:
            low_res_masks_local = torch.zeros(0, H_mask, W_mask, device=predictor.model.device)
            obj_scores_local = torch.zeros(0, device=predictor.model.device)

        return obj_ids_local, low_res_masks_local, obj_scores_local

    predictor.model._propogate_tracker_one_frame_local_gpu = patched_propagate
    print(f"[VRAM] Applied zombie-state bypass patch to _propogate_tracker_one_frame_local_gpu")

    # --- Fix #6: Frame-level Crash Bypass (with throttled logging) --------
    orig_det_track = predictor.model._det_track_one_frame

    def patched_det_track(*args, **kwargs):
        try:
            res = orig_det_track(*args, **kwargs)
            patched_det_track.consecutive_errors = 0
            return res
        except Exception as e:
            err_str = str(e)

            # Catch known sporadic boundary desync faults and cleanly bypass the frame
            if any(key in err_str for key in ("conditioning objects not found", "No points are provided", "cannot unpack non-iterable NoneType object", "expected Tensor as element 0")):
                frame_idx = kwargs.get("frame_idx", args[0] if len(args) > 0 else -1)

                if not hasattr(patched_det_track, "consecutive_errors"):
                    patched_det_track.consecutive_errors = 0

                if patched_det_track.consecutive_errors == 0:
                    print(f" [VRAM] Suppressed crash in _det_track_one_frame at frame {frame_idx}: {err_str}")
                    print(f" [VRAM] Silencing identical warnings for subsequent frames...")

                patched_det_track.consecutive_errors += 1

                tracker_states_local = kwargs.get("tracker_states_local", args[5] if len(args) > 5 else [])
                tracker_metadata_prev = kwargs.get("tracker_metadata_prev", args[6] if len(args) > 6 else {})
                orig_vid_height = kwargs.get("orig_vid_height", args[8] if len(args) > 8 else 1080)
                orig_vid_width = kwargs.get("orig_vid_width", args[9] if len(args) > 9 else 1920)

                obj_ids_all_gpu = tracker_metadata_prev.get("obj_ids_all_gpu", [])

                # Construct "empty" proxy states using 3D tensors (1, H, W) perfectly matching normal returns
                obj_id_to_mask = {
                    int(obj_id): torch.zeros((1, orig_vid_height, orig_vid_width), dtype=torch.bool, device=predictor.model.device)
                    for obj_id in obj_ids_all_gpu
                }

                # Use 0D tensors instead of floats to avoid 'expected Tensor as element 0 in argument 0'
                obj_id_to_score = {
                    int(obj_id): torch.tensor(-1024.0, dtype=torch.float32, device=predictor.model.device)
                    for obj_id in obj_ids_all_gpu
                }

                for score_key in ["obj_id_to_tracker_score_frame_wise", "obj_id_to_sam2_score_frame_wise"]:
                    if score_key in tracker_metadata_prev:
                        if frame_idx not in tracker_metadata_prev[score_key]:
                            tracker_metadata_prev[score_key][frame_idx] = {}
                        for obj_id in obj_ids_all_gpu:
                            tracker_metadata_prev[score_key][frame_idx][int(obj_id)] = torch.tensor(-1024.0, dtype=torch.float32, device=predictor.model.device)

                frame_stats = {"num_obj_tracked": len(obj_ids_all_gpu), "num_obj_dropped": 0}
                tracker_obj_scores_global = torch.full((len(obj_ids_all_gpu),), -1024.0, device=predictor.model.device)

                # Returns fully validated structured tuple keeping loop cleanly moving
                return (
                    obj_id_to_mask,
                    obj_id_to_score,
                    tracker_states_local,
                    tracker_metadata_prev,
                    frame_stats,
                    tracker_obj_scores_global
                )

            print(f"\n[CRITICAL] Unhandled exception inside patched_det_track:\n")
            traceback.print_exc()
            raise

    predictor.model._det_track_one_frame = patched_det_track
    print(f"[VRAM] Applied frame-level crash bypass patch to _det_track_one_frame")

# ----------------------------------------------------------------------------
# Main / Stage 2: GPU tracking loop
# ----------------------------------------------------------------------------

def track_window(predictor, args, chunk_dir, frame_indices, text_prompt, label):
    """Segment and track ONE category across ONE window.

    Returns {local_obj_id: {global_frame_idx: bool mask}}, empty when the phrase
    matches nothing in the window.

    One SAM3 session per call, even when several categories share the window:
    `add_prompt` with a text prompt calls `reset_state()` internally ("since
    it's a semantic prompt, we start over"), and the phrase is stored as the
    single `inference_state["text_prompt"]`. A session therefore tracks exactly
    one category, and the categories cannot be batched into one propagation.
    Re-running `start_session` on the same chunk dir re-reads its JPEGs, which is
    cheap next to the propagation itself and keeps each category's state fully
    isolated.
    """
    actual = len(frame_indices)
    prefix = f"[{label}]"

    start_resp = predictor.handle_request({
        "type": "start_session",
        "resource_path": chunk_dir,
        "offload_video_to_cpu": True
    })
    session_id = start_resp["session_id"]

    # ---- prompt search: errors are surfaced, NOT treated as absence
    prompt_success = False
    start_prop_idx = 0
    first_prompt_error = None
    identical_error_count = 0
    for local_f_idx in range(0, actual, args.prompt_search_stride):
        try:
            predictor.handle_request({
                "type": "add_prompt",
                "session_id": session_id,
                "frame_index": local_f_idx,
                "text": text_prompt,
            })
            prompt_success = True
            start_prop_idx = local_f_idx
            if local_f_idx == 0:
                print(f"{prefix} Added text prompt '{text_prompt}' at window start.")
            else:
                print(f"{prefix} Object found mid-window! Added text prompt at local frame {local_f_idx}.")
            break
        except Exception as pe:
            if first_prompt_error is None:
                first_prompt_error = repr(pe)
                print(f" {prefix} [WARN] add_prompt raised an exception (this may be a "
                      "real error, NOT 'object absent'):")
                traceback.print_exc()
                identical_error_count = 1
            elif repr(pe) == first_prompt_error:
                identical_error_count += 1
                if identical_error_count >= 3:
                    print(f" {prefix} [ERROR] add_prompt failed identically on 3 frames "
                          "-- this is a code/flag problem, not object absence. "
                          "Skipping the rest of the frame search.")
                    break
            else:
                first_prompt_error = repr(pe)
                identical_error_count = 1

    if not prompt_success:
        if first_prompt_error is not None:
            print(f" {prefix} !!! Window skipped due to add_prompt ERROR (not absence): "
                  f"{first_prompt_error}")
        else:
            print(f"{prefix} Object '{text_prompt}' not found anywhere in this window. Skipping...")
        predictor.handle_request({"type": "close_session", "session_id": session_id})
        return {}

    current_chunk_masks = {}
    frames_yielded = 0
    propagation_failed = False
    try:
        stream = predictor.handle_stream_request({
            "type": "propagate_in_video",
            "session_id": session_id,
            "start_frame_idx": start_prop_idx
        })

        for frame_resp in stream:
            local_f_idx = frame_resp.get("frame_index")
            if local_f_idx is None or local_f_idx >= actual:
                continue
            frames_yielded += 1

            if args.vram_debug and local_f_idx % 200 == 0:
                report_session_vram(predictor, session_id, tag=f"{label}/f{local_f_idx}")

            global_f_idx = frame_indices[local_f_idx]
            outputs = frame_resp.get("outputs", {})
            out_obj_ids = outputs.get("out_obj_ids", [])
            binary_masks = outputs.get("out_binary_masks")

            if binary_masks is not None and len(out_obj_ids) > 0:
                if isinstance(out_obj_ids, torch.Tensor):
                    out_obj_ids = out_obj_ids.cpu().numpy()
                if isinstance(binary_masks, torch.Tensor):
                    binary_masks = binary_masks.cpu().numpy()

                for i, obj_id in enumerate(out_obj_ids):
                    obj_id = int(obj_id)
                    mask = binary_masks[i]
                    if mask.ndim == 3:
                        mask = mask[0]
                    current_chunk_masks.setdefault(obj_id, {})[global_f_idx] = (mask > 0)

    except Exception as e:
        propagation_failed = True
        print(f"{prefix} Propagation error: {e}")
        report_session_vram(predictor, session_id, tag=f"{label}/at-OOM")

    predictor.handle_request({"type": "close_session", "session_id": session_id})

    if propagation_failed or frames_yielded < actual - start_prop_idx:
        first_missing = frame_indices[min(start_prop_idx + frames_yielded, actual - 1)]
        print(f" {prefix} !!! PARTIAL CHUNK: only {frames_yielded}/{actual - start_prop_idx} "
              f"frames propagated. Frames "
              f"{first_missing}..{frame_indices[-1]} have NO '{label}' masks; the next "
              f"window's overlap will NOT match -> {label} identities restart at the "
              f"boundary. Do not trust this range in the output. (Other categories in "
              f"this window are unaffected -- they run in their own sessions.)")

    return current_chunk_masks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=str, required=True, help="Path to input video")
    parser.add_argument("--chunk_frames", type=int, default=650,
                        help="Frames per tracking window. Set comfortably below OOM limit.")
    parser.add_argument("--overlap_frames", type=int, default=5,
                        help="Frames overlapped between windows for identity matching")
    parser.add_argument("--frame_stride", type=int, default=1,
                        help="Process every Nth frame")
    parser.add_argument("--max_resolution", type=int, default=0,
                        help="Downscale frames to max dimension (e.g. 720) before tracking")
    parser.add_argument("--prompt_search_stride", type=int, default=1,
                        help="When the text prompt finds nothing at the window start, "
                             "retry every Nth frame within the window")
    parser.add_argument("--freeze_min_frames", type=int, default=10,
                        help="Consecutive duplicate frames that make a stretch a 'freeze'. "
                             "Frozen frames are dropped instead of tracked, and track "
                             "identities are never matched across one (the data is already "
                             "lost there; guessing would corrupt the good data too). "
                             "10 is ~0.4s at 25fps. 0 disables detection entirely.")
    parser.add_argument("--freeze_pixel_thresh", type=int, default=0,
                        help="Per-pixel grey-level change ignored when comparing two frames. "
                             "0 (default) demands byte-identical frames, which a live sensor "
                             "never produces, so a static-but-live scene is not mistaken for "
                             "a freeze. Raise to ~8 if the recording burns in a ticking clock.")
    parser.add_argument("--freeze_ratio", type=float, default=0.0,
                        help="Fraction of pixels allowed to differ while still calling a "
                             "frame a duplicate. Pair with --freeze_pixel_thresh for burnt-in "
                             "timestamps, e.g. 0.001. Keep small: a raised value starts "
                             "eating real low-motion footage.")
    parser.add_argument("--freeze_gap_ms", type=float, default=400.0,
                        help="Wall-clock hole (ms) between consecutive frames that counts "
                             "as a freeze. Covers the case where the recorder DROPS frames "
                             "instead of repeating them -- the surviving frames then lie "
                             "about being adjacent, and matching identities across the hole "
                             "is guesswork. 0 disables timestamp checking.")
    parser.add_argument("--freeze_gap_mult", type=float, default=5.0,
                        help="Also require the hole to exceed this multiple of the median "
                             "inter-frame interval, so a genuinely low-framerate camera "
                             "does not break on every frame. Effective threshold is "
                             "max(--freeze_gap_ms, mult * median interval).")
    parser.add_argument("--text_prompt", type=str, action="append", required=True,
                        metavar="TEXT",
                        help="Text prompt to track. Repeat the flag to track several "
                             "categories in one pass: --text_prompt person "
                             "--text_prompt 'dining table'. Each is prompted and "
                             "propagated separately (SAM3 holds one text prompt per "
                             "session), so N prompts cost roughly N times the GPU time, "
                             "but the video is decoded once and everything lands in one "
                             "XML.")
    parser.add_argument("--label_name", type=str, action="append", metavar="NAME",
                        help="CVAT label for the corresponding --text_prompt, paired in "
                             "order. Either omit it entirely (labels default to the "
                             "prompt text) or give exactly as many as there are prompts. "
                             "Useful when the phrase that segments best is not the name "
                             "you want in CVAT, e.g. --text_prompt 'person sitting down' "
                             "--label_name customer.")
    parser.add_argument("--fill_hole_area", type=int, default=0,
                        help="Fill holes and remove stranded patches")
    parser.add_argument("--max_num_objects", type=int, default=50,
                        help="Maximum number of objects to track")
    parser.add_argument("--max_obj_ptrs", type=int, default=8,
                        help="max_obj_ptrs_in_encoder override. With grounding "
                             "batch fixed, 16 (the built default) may fit and "
                             "improves re-identification after occlusion.")
    parser.add_argument("--grounding_batch", type=int, default=8,
                        help="Detector grounding batch size (library default 16). "
                             "Halving halved both the resident grounding cache "
                             "and the batch-boundary VRAM spike.")
    parser.add_argument("--vram_debug", action="store_true",
                        help="Periodically print a per-key GPU breakdown of the "
                             "live session state during propagation.")
    parser.add_argument("--output", type=str, default="annotations.xml", help="Output CVAT XML file")

    args = parser.parse_args()

    if args.overlap_frames >= args.chunk_frames:
        print("Error: overlap_frames must be strictly less than chunk_frames.")
        sys.exit(1)

    # --- categories: one (prompt, label) pair per tracked category -----------
    if args.label_name is None:
        args.label_name = list(args.text_prompt)
    elif len(args.label_name) != len(args.text_prompt):
        print(f"Error: got {len(args.text_prompt)} --text_prompt and "
              f"{len(args.label_name)} --label_name. Pass one --label_name per "
              f"--text_prompt (paired in the order given), or none at all to reuse "
              f"the prompt text as the label.")
        sys.exit(1)

    # Two prompts sharing a label would silently compete for the same identity
    # pool in the stitcher and be indistinguishable in CVAT afterwards.
    dupes = {n for n in args.label_name if args.label_name.count(n) > 1}
    if dupes:
        print(f"Error: duplicate label name(s) {sorted(dupes)}. Each category needs "
              f"its own label.")
        sys.exit(1)

    # `labels` is the category list everything downstream keys on; `categories`
    # pairs each with the phrase that finds it.
    args.labels = args.label_name
    categories = list(zip(args.text_prompt, args.label_name))
    print("Tracking {} categor{}: {}".format(
        len(categories), "y" if len(categories) == 1 else "ies",
        ", ".join(f"'{t}' -> {l}" for t, l in categories)))

    torch.autocast(device_type="cuda", dtype=torch.bfloat16).__enter__()
    torch.set_grad_enabled(False)

    print("Loading SAM 3.1 Multiplex Predictor with Smart Routing...")

    original_torch_load = torch.load
    def patched_torch_load(f, *a, **kw):
        fname = getattr(f, "name", str(f))
        if fname.endswith('.safetensors'):
            from safetensors.torch import load_file
            ckpt = load_file(fname)
            return {k: v.float() for k, v in ckpt.items()}
        return original_torch_load(f, *a, **kw)
    torch.load = patched_torch_load

    original_load_state_dict = torch.nn.Module.load_state_dict
    def patched_load_state_dict(self, state_dict, *a, **kw):
        target_keys = self.state_dict().keys()
        custom_state_dict = {}
        for target_key in target_keys:
            if target_key in state_dict:
                custom_state_dict[target_key] = state_dict[target_key]
            elif f"tracker.model.{target_key}" in state_dict:
                custom_state_dict[target_key] = state_dict[f"tracker.model.{target_key}"]
            elif f"tracker.{target_key}" in state_dict:
                custom_state_dict[target_key] = state_dict[f"tracker.{target_key}"]
            elif f"detector.{target_key}" in state_dict:
                custom_state_dict[target_key] = state_dict[f"detector.{target_key}"]
        kw['strict'] = False
        return original_load_state_dict(self, custom_state_dict, *a, **kw)
    torch.nn.Module.load_state_dict = patched_load_state_dict

    predictor = build_sam3_multiplex_video_predictor(
        checkpoint_path="/opt/nuclio/sam/sam3.1_multiplex_fp16.safetensors",
        use_fa3=False,
        max_num_objects=args.max_num_objects
    )

    if args.fill_hole_area > 0:
        predictor.model.fill_hole_area = args.fill_hole_area
        if hasattr(predictor.model, 'tracker') and hasattr(predictor.model.tracker, 'model'):
            predictor.model.tracker.model.fill_hole_area = args.fill_hole_area

    original_init_state = predictor.model.init_state
    def patched_init_state(*a, **kw):
        kw.pop('offload_state_to_cpu', None)
        return original_init_state(*a, **kw)
    predictor.model.init_state = patched_init_state

    apply_memory_fixes(
        predictor,
        max_obj_ptrs=args.max_obj_ptrs,
        grounding_batch_size=args.grounding_batch,
    )

    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        print(f"Failed to open video: {args.video}")
        sys.exit(1)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    cap.release()
    print(f"Video loaded: {total_frames} frames, {width}x{height}, {fps:.2f} fps")

    temp_dir = tempfile.mkdtemp()
    global_tracks = []
    freeze_log = []

    chunk_queue = queue.Queue(maxsize=2)
    post_queue = queue.Queue(maxsize=1)

    reader = threading.Thread(target=reader_thread,
                              args=(args, temp_dir, chunk_queue, fps, freeze_log),
                              daemon=True)
    worker = threading.Thread(target=postprocess_worker,
                              args=(args, post_queue, global_tracks, width, height), daemon=True)
    reader.start()
    worker.start()

    t_start = time.time()
    frames_done = 0

    try:
        while True:
            item = chunk_queue.get()
            if item is None:
                break
            chunk_dir, frame_indices, new_segment = item
            actual = len(frame_indices)
            chunk_start = frame_indices[0]
            if new_segment and chunk_start > 0:
                print(f"\n--- Frozen input ended: starting a fresh tracking segment at "
                      f"frame {chunk_start}. Identities restart here and are NOT carried "
                      f"across the freeze. ---")
            print(f"\n=== Processing Window: Global Frames {chunk_start} to {frame_indices[-1]} ===")

            masks_by_label = {}
            for text_prompt, label in categories:
                masks_by_label[label] = track_window(
                    predictor, args, chunk_dir, frame_indices, text_prompt, label)

            shutil.rmtree(chunk_dir, ignore_errors=True)
            post_queue.put((frame_indices, masks_by_label, new_segment))

            frames_done += actual
            elapsed = time.time() - t_start
            # NOT `fps`: that holds the video's frame rate, which the freeze
            # sidecar still needs after this loop.
            throughput = frames_done / elapsed if elapsed > 0 else 0.0
            # Frames of *video* per second, not frames propagated: with several
            # categories each frame is propagated once per category.
            suffix = f" x{len(categories)} categories" if len(categories) > 1 else ""
            print(f" [Throughput] {frames_done} frames in {elapsed:.0f}s "
                  f"({throughput:.1f} fps{suffix})")

    finally:
        post_queue.put(None)
        worker.join()
        shutil.rmtree(temp_dir, ignore_errors=True)

    if args.freeze_min_frames <= 0 and args.freeze_gap_ms <= 0:
        print("\nFreeze detection was disabled entirely.")
    elif not freeze_log:
        print("\nNo frozen stretches detected in the input.")
    else:
        rep = [f for f in freeze_log if f["type"] == "repeated_frames"]
        gaps = [f for f in freeze_log if f["type"] == "dropped_frames"]
        lost = sum(f["seconds"] for f in freeze_log)
        print(f"\n{len(freeze_log)} discontinuity(ies): {len(rep)} repeated-frame "
              f"stretch(es), {len(gaps)} dropped-frame hole(s), {lost:.1f}s of input "
              f"lost in total. Nothing was tracked across them and no identity was "
              f"carried over, so expect fresh ids after each one.")
        for f in freeze_log:
            if f["type"] == "repeated_frames":
                print(f"  repeated  frames {f['start_frame']}..{f['end_frame']} "
                      f"({f['num_frames']} frames, {f['seconds']}s)")
            else:
                print(f"  dropped   {f['seconds']}s between frames "
                      f"{f['after_frame']} and {f['before_frame']}")
        freeze_path = os.path.splitext(args.output)[0] + "_freezes.json"
        with open(freeze_path, "w") as fh:
            json.dump({
                "video": os.path.basename(args.video),
                "fps": fps,
                "total_frames": total_frames,
                "seconds_lost": round(lost, 2),
                "detector": {
                    "min_frames": args.freeze_min_frames,
                    "pixel_thresh": args.freeze_pixel_thresh,
                    "ratio": args.freeze_ratio,
                    "gap_ms": args.freeze_gap_ms,
                    "gap_mult": args.freeze_gap_mult,
                },
                "freezes": freeze_log,
            }, fh, indent=2)
        print(f"Written to {freeze_path} -- downstream id-unification should not link "
              f"tracks across these points either.")

    per_label = {label: 0 for label in args.labels}
    for t in global_tracks:
        if t["frames"]:
            per_label[t["label"]] += 1
    print("\nTracks found: " + ", ".join(f"{n} {label}" for label, n in per_label.items()))
    for label, n in per_label.items():
        if n == 0:
            print(f" [WARN] '{label}' produced no tracks at all. The label is still "
                  f"declared in the XML, but check the prompt phrasing.")

    print(f"\nWriting XML to {args.output}...")
    create_cvat_xml(global_tracks, os.path.basename(args.video), total_frames, width,
                    height, args.output, labels=args.labels,
                    frame_stride=args.frame_stride)

    print("Done!")


if __name__ == "__main__":
    main()

