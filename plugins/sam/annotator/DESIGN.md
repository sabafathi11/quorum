# SAM 3 Automated Annotator Design

## Overview
This project automates the segmentation and tracking of objects in long videos (e.g., 1 hour long) using the SAM 3 library natively. 
It processes video in chunks, utilizes periodic text prompting for drift correction and re-discovery, and exports pixel-perfect segmentations as tracks in CVAT XML format.

## Requirements & Inputs
1. **Video File**: Long video file (e.g., 1 hour).
2. **Chunk Size**: Number of frames to track at once in memory before flushing.
3. **Segment Frames**: Interval $N$ (e.g., every 50 frames) to inject a new text prompt to anchor the object.
4. **Text Prompts**: One or more phrases defining the categories to track (§2.5), each with the CVAT
   label its tracks should carry.

## Architecture

### 1. SAM 3 Predictor Setup
- We instantiate `Sam3VideoPredictorMultiGPU` or `Sam3VideoPredictor` from the `sam3` library.
- We call `handle_request({"type": "start_session", "resource_path": video_file, "offload_video_to_cpu": True, "offload_state_to_cpu": True})` to avoid GPU OOM for long videos.

### 2. Processing Loop
- Iterate through the video in steps of `chunk_size`.
- Inside each chunk, for every `segment_frames` interval, use `handle_request({"type": "add_prompt", "text": TEXT_PROMPT, "frame_index": i})`.
- Call `handle_stream_request({"type": "propagate_in_video", ...})` to track the chunk.
- At the end of the chunk, clear old states/memory banks if necessary, while preserving the last frame's mask as the start for the next chunk if continuous tracking is desired.

### 2.5. Multiple Prompt Categories

`--text_prompt` is repeatable, and each occurrence is one output mask category:

```
--text_prompt person --text_prompt chair --text_prompt "dining table"
--text_prompt "person sitting down" --label_name customer
```

`--label_name` is the CVAT label for the corresponding prompt, paired by position. Omit it entirely
and the labels default to the prompt text; give it and it must appear exactly as many times as
`--text_prompt`. Two categories may not share a label — they would compete for one identity pool in
the stitcher and be indistinguishable in CVAT afterwards, so that exits with an error rather than
producing a quietly wrong file.

**One session per category per window, not one session with many prompts.** This is forced by SAM 3,
not a choice: `add_prompt` with a text prompt calls `reset_state()` ("since it's a semantic prompt, we
start over") and stores the phrase as the single `inference_state["text_prompt"]`. A session tracks
exactly one phrase. So `track_window()` runs the whole `start_session` → prompt search →
`propagate_in_video` → `close_session` cycle per category, on the same chunk directory.

What that costs and what it buys:

- **Cost:** roughly N× the GPU time for N categories. Nothing is shared between the passes.
- **Buys:** the video is decoded, downscaled and freeze-analysed exactly once; the categories see
  identical frames and identical segment boundaries; and one XML comes out with all labels, ready to
  import as a single CVAT task. Running the script N times gives none of that and costs the same GPU.
- Peak VRAM is unchanged — the sessions are sequential, and `--max_num_objects` still applies per
  category, so N categories can track N × `max_num_objects` objects overall.
- A category that finds nothing in a window is skipped for that window only; the others propagate
  normally. Likewise a `PARTIAL CHUNK` failure is scoped to the category whose session died.

**Identities are stitched per category.** `postprocess_worker` keeps `prev_overlap` keyed by label,
and the IoU match for one category only ever sees that category's previous overlap. A person and the
chair they are sitting on overlap heavily every frame; without the split they would merge into one
track. Frozen-input segment boundaries still reset every category at once, since the discontinuity is
a property of the footage rather than of any one prompt.

### 3. Frozen Input Handling
The recorders stall. That data is already lost; the pipeline's job is to not *compound* the loss by
tracking a scene that isn't happening, or by inventing identity links across the hole.

Stalls show up in **two different ways**, which need opposite detectors. Both are handled, both are on
by default, and both do the same thing once found: cut a hard tracking-segment boundary.

**(a) Repeated frames** — the stream re-sends the last picture.
- Detected on pixels, in the reader thread, on every decoded frame (before stride/downscale, so
  sensitivity is not blunted). A frame is a *duplicate* when it is byte-identical in greyscale to its
  predecessor; `--freeze_min_frames` (default 10) consecutive duplicates make a freeze.
- **Why byte-identical by default:** a live sensor always has read noise, so a merely *static* scene
  (nobody moving) still differs frame to frame and is never mistaken for a freeze. Only relax it
  (`--freeze_pixel_thresh 8 --freeze_ratio 0.001`) when the recording burns in a ticking clock, where
  the digits are the only changing pixels — `freeze_probe.py` tells you whether you are in that case.
- Duplicate runs *shorter* than the threshold are kept, not dropped: that is ordinary still footage.
- Frozen frames never reach a chunk dir, so SAM3's memory bank is not fed a stalled scene.

**(b) Dropped frames** — the recorder writes nothing at all. **This is what the restaurant cameras
actually do**, and it is the more dangerous case: no pixel evidence exists, the surviving frames look
adjacent, and only the presentation timestamps reveal the hole. Measured on `20260605_174327`:

| camera | holes | wall-clock lost (of 600s) | segments |
|--------|-------|---------------------------|----------|
| cam1   | 2     | 1.3s                      | 3        |
| cam6   | 38    | 76.1s                     | 39       |
| cam7   | 135   | 307.2s (51%)              | 136      |

- `_GapWatch` compares `CAP_PROP_POS_MSEC` between consecutive frames. A hole counts when it exceeds
  `max(--freeze_gap_ms (400ms), --freeze_gap_mult (5) * median inter-frame interval)`. The absolute
  term is what matters (how much motion was missed); the relative term keeps a genuinely low-framerate
  camera from breaking on every frame. If the backend reports no timestamps, every gap reads as 0 and
  the check quietly does nothing.
- `--freeze_gap_ms` is the fidelity/continuity dial. Raising it yields fewer, longer, less trustworthy
  tracks; lowering it yields more, shorter, more honest ones.

**What a segment boundary does:** the chunk in progress is closed on the last good frame and the
frames after the discontinuity start a fresh segment. `postprocess_worker` clears `prev_overlap` for a
segment's first chunk, so the IoU overlap match cannot link an identity across it — after a stall
everyone on screen has teleported, and a guessed link would corrupt the good data on both sides.
Tracks simply end at the cut and new ids begin after it. The cut must also be a *chunk* boundary, not
just an id-matching reset, because SAM3 would otherwise propagate its own object ids straight through
the hole inside a single session.

**Reporting:** every discontinuity is logged during the run, summarised at the end, and written to
`<output>_freezes.json` (typed `repeated_frames` / `dropped_frames`) so downstream cross-camera id
unification can refuse to link across the same points.

`--freeze_min_frames 0` and `--freeze_gap_ms 0` disable the two detectors independently.

Chunks carry an explicit `frame_indices` list rather than deriving global frame numbers by
`chunk_start + local_idx * frame_stride`, since segments no longer start on predictable boundaries.

### 4. XML Export Format
- Generate an `annotations.xml` file conforming to CVAT Video 1.1 format.
- Mask tracks are exported using `<track>` elements populated with `<mask frame="X" left="Y" top="Z" width="W" height="H" rle="a, b, c, ...">` elements instead of shapes/polygons.
- The `rle` string represents the Run-Length Encoding of the boolean mask.
- Every category is declared in the task `<labels>` block with its own colour, **including one that
  produced no tracks at all** — the label must exist in the imported task so it can be annotated by
  hand, rather than silently vanishing because the prompt happened to match nothing. Each `<track>`
  carries the label of the prompt that found it.

## Note on CVAT Mask Tracks
CVAT traditionally only supported `box`, `polygon`, `polyline`, `points`, `cuboid` within `<track>`. Mask tracking was implemented custom in the target CVAT instance. The XML generated must match this custom implementation's expectations (specifically, how `<mask ...>` tags are parsed inside `<track>` tags).
