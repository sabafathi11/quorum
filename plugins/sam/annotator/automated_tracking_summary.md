# SAM3 Automated Tracking: Project Summary

Status of the standalone automation script (`sam3_auto_annotator/main.py`), which wraps SAM 3.1's
multiplex video predictor to segment and track objects across long CCTV recordings and export them as
CVAT mask tracks. See `DESIGN.md` for the architecture; this file is the operational summary.

## 1. What it does

`main.py` runs the model without the CVAT/Nuclio server: it builds the predictor directly, feeds the
video through in overlapping windows, prompts each window with one or more text phrases, propagates,
and stitches the per-window object ids into global tracks.

Three threads, connected by small bounded queues so the GPU is the only thing anyone waits on:

1. **Reader (CPU)** — decodes the video, applies `--frame_stride` / `--max_resolution`, writes JPEGs,
   and hands the tracker fixed-size chunks. Also detects frozen input (§3).
2. **Tracker (GPU)** — one SAM 3.1 session per chunk **per category**: `start_session` →
   `add_prompt` → `propagate_in_video`. If the prompt finds nothing at the window start it retries
   deeper into the window (`--prompt_search_stride`).
3. **Postprocess (CPU)** — matches each chunk's local object ids onto the running global tracks by
   mask IoU over the `--overlap_frames` shared frames, then RLE-encodes to CVAT geometry. Matching is
   per category, so tracks of different labels never merge.

**Output is CVAT Video 1.1 XML** (`<track>` of `<mask ... rle=...>`), not COCO — the earlier
`coco_annotations.json` exporter was replaced once mask tracks landed in the target CVAT fork. A
`<output>_freezes.json` sidecar is written alongside it whenever discontinuities are found.

## 2. Running it

`run.sh` iterates `vids.txt` and is the source of truth for the working parameters:

```bash
docker run --gpus all --rm \
  -v "$(pwd)":/workspace \
  -v /media/jvn-server/185A27335A270CD6/saba/records:/records:ro \
  -w /workspace \
  -e PYTHONPATH=/opt/nuclio/sam \
  -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
  --entrypoint python3 cvat.pth.comfyorg.sam3.1:latest-gpu \
  main.py \
    --video "/records/$filename" \
    --text_prompt "person" --label_name person \
    --max_num_objects 50 \
    --chunk_frames 64 --overlap_frames 4 --frame_stride 5 \
    --max_resolution 720 --fill_hole_area 64 \
    --output "/workspace/${name_no_ext}_annotations.xml"
```

Edit the `PROMPTS` / `LABELS` arrays at the top of `run.sh` to change what is tracked.

Key flags:

| flag | meaning |
|------|---------|
| `--text_prompt` / `--label_name` | repeatable; one pair per output category, see §2.1 |
| `--chunk_frames` / `--overlap_frames` | window size and the shared frames identity matching runs on |
| `--frame_stride` | process every Nth frame |
| `--max_resolution` | downscale before tracking (720 in production) |
| `--max_num_objects` | hard cap on simultaneously tracked objects; the main VRAM lever |
| `--fill_hole_area` | fill holes / remove stranded patches in masks |
| `--max_obj_ptrs`, `--grounding_batch` | VRAM knobs, see §4 |
| `--freeze_*` | frozen-input handling, see §3 |
| `--vram_debug` | periodic per-key GPU breakdown of the live session state |

### 2.1 Several categories in one pass

Repeat `--text_prompt` to get several mask categories out of one run, optionally with a `--label_name`
per prompt (paired by position; omit them all and the prompt text becomes the label):

```
--text_prompt person --text_prompt chair --text_prompt "dining table"
--text_prompt "person sitting down" --label_name customer     # label ≠ prompt
```

Each category is prompted and propagated in its **own** session — SAM 3's `add_prompt` resets the
state and stores one phrase per session, so they cannot be batched. Budget ~N× the GPU time for N
categories. What you get for it, versus running the script N times: the video is decoded and
freeze-analysed once, all categories see identical frames and identical segment boundaries, and one
XML comes out carrying every label, importable as a single CVAT task. Peak VRAM does not change (the
sessions are sequential), but `--max_num_objects` applies per category.

Identity stitching is per category, so a person and the chair they sit on never merge into one track
despite overlapping every frame. A category that finds nothing in a window is skipped for that window
alone, and a category that produced no tracks in the entire video still gets its label declared in the
XML so it can be annotated by hand.

Two helpers, both runnable in the same image:

- `test_freeze.py` — freeze-handling tests. No GPU, no model, no footage needed.
- `freeze_probe.py` — per-camera diagnostic: how a given recording loses data and how many tracking
  segments it will produce. Worth running before committing to a long job.

## 3. Frozen input

The recorders stall, in two ways that need opposite detectors — repeated frames (caught on pixels) and
dropped frames (caught only on presentation timestamps). Both are detected by default; both cut a hard
tracking-segment boundary so no track identity is carried across the hole. Full rationale, thresholds
and the per-camera measurements are in `DESIGN.md` §3.

The headline: **these cameras drop frames rather than repeating them.** Over 600s of
`20260605_174327`, cam1 lost 1.3s, cam6 76.1s, and cam7 307.2s — 51% of its wall clock. Expect fresh
track ids after each hole; that fragmentation is the data being honest, not a bug. `--freeze_gap_ms`
is the dial if you would rather trade fidelity for longer tracks.

## 4. VRAM / OOM

The long-standing hurdle. `apply_memory_fixes()` applies the mitigations that made long runs viable:

- `offload_output_to_cpu_for_eval`, plus a `_cache_frame_outputs` patch that moves masks to CPU before
  caching.
- `max_obj_ptrs_in_encoder` and the detector's grounding `batch_size` lowered (halving the grounding
  batch halved both the resident cache and the batch-boundary spike).
- Bypasses for two sporadic faults that otherwise abort a whole window: the multiplex "zombie state"
  crash in `_propogate_tracker_one_frame_local_gpu`, and frame-level desync faults in
  `_det_track_one_frame` — both substitute empty masks for the frame and keep the loop moving.
- `_propogate_tracker_one_frame_local_gpu` also sorts its object ids (and permutes the mask/score rows
  with them) before returning. Concatenating the inference states in list order does not reproduce
  `tracker_metadata["obj_ids_per_gpu"]`, which is creation order: a masklet detected later can land in
  an earlier state, giving e.g. `[0..14, 17, 15, 16]`. `run_tracker_propagation` asserts on that, the
  `propagate_in_video` stream dies, and everything after that frame in the window is left with **no
  masks** — the 4–18s mask holes seen in cam2/cam4 on 2026-07-29 were all this, not frozen input.

OOM is **not fully solved**. The residue lives in SAM3's own state management (`sam2_inference_states`
and the grounding `feature_cache` dominate at the point of failure), which needs native offloading
rather than patching from outside. Practical guidance: keep `--max_num_objects` and `--chunk_frames`
modest, and remember vm.3090's GPU is shared — a run can OOM purely because another process is holding
most of the card. When a window dies, the `PARTIAL CHUNK` warning names the frame range that has no
masks and whose identities will restart; do not trust that range.
