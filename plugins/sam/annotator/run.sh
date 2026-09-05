#!/bin/bash
#
# Batch annotator. Reads the video list from vids.txt and processes it top to
# bottom, one docker run per video, sequentially -- the GPU is shared, so never
# make this parallel.
#
# Built to be started once in tmux and left alone:
#   tmux new -s ppe
#   cd ~/sam3_auto_annotator && ./run.sh
#   (detach with ctrl-b d, reattach later with `tmux attach -t ppe`)
#
# The pane shows one progress block per video; the full per-video output goes to
# OUTDIR/logs/<name>.log. A video whose output XML already exists is skipped, so
# if the batch is interrupted -- ctrl-c, OOM, reboot -- just run it again and it
# picks up where it stopped. A run that exits nonzero has its partial XML moved
# aside to .PARTIAL so the retry does not mistake it for finished work.

if [ ! -f "vids.txt" ]; then
    echo "Error: vids.txt not found in the current directory!"
    exit 1
fi

# Stock SAM 3.1 image. (The old :reid-gpu tag was an OSNet/torchreid layer for
# the --enable_reid path; that feature was reverted out of main.py, the image no
# longer exists on vm.3090, and its Dockerfile has been deleted.)
IMAGE="cvat.pth.comfyorg.sam3.1:latest-gpu"

# Categories to segment. Several entries = several mask categories out of one
# pass: SAM 3 holds a single text prompt per session, so each category is
# prompted and propagated on its own (~N times the GPU time for N categories),
# but the video is decoded once, freeze detection runs once, and every category
# lands in the same XML with its own CVAT label.
#
# Singular phrases -- text grounding keys on the noun, and "hat" scores the same
# objects as "hats" while pluralising sometimes biases toward group shots.
PROMPTS=("hat" "glove" "hairnet" "phone")

# CVAT label per prompt, in the same order and count. Leave the array empty to
# reuse the prompt text as the label -- useful when the phrase that segments
# best is not the name you want in CVAT ("person sitting down" -> customer).
LABELS=("hat" "glove" "hairnet" "phone")

# Results go in their own directory: these are PPE passes, and writing
# <name>_annotations.xml into the module root would overwrite the person-tracking
# XMLs already sitting there from earlier runs.
OUTDIR="ppe_runs"

if [ ${#LABELS[@]} -gt 0 ] && [ ${#LABELS[@]} -ne ${#PROMPTS[@]} ]; then
    echo "Error: LABELS has ${#LABELS[@]} entries but PROMPTS has ${#PROMPTS[@]}."
    exit 1
fi

prompt_args=()
for p in "${PROMPTS[@]}"; do prompt_args+=(--text_prompt "$p"); done
for l in "${LABELS[@]}"; do prompt_args+=(--label_name "$l"); done

mkdir -p "$OUTDIR/logs"

mapfile -t videos < <(tr -d '\r' < vids.txt | grep '\.mp4$')
total=${#videos[@]}
if [ "$total" -eq 0 ]; then
    echo "Error: vids.txt lists no .mp4 files."
    exit 1
fi

echo "$total video(s), categories: ${PROMPTS[*]} -> ${LABELS[*]}"
echo "Output: $OUTDIR/  (logs in $OUTDIR/logs/)"

batch_start=$(date +%s)
done_count=0 skipped=0 failed=0

for i in "${!videos[@]}"; do
    filename=$(basename "${videos[$i]}")
    name_no_ext="${filename%.*}"
    out="$OUTDIR/${name_no_ext}_ppe.xml"
    log="$OUTDIR/logs/${name_no_ext}.log"

    echo
    echo "=================================================="
    echo "[$((i + 1))/$total] $filename   $(date '+%F %T')"

    if [ -f "$out" ]; then
        echo "  already done ($out) -- skipping"
        skipped=$((skipped + 1))
        continue
    fi

    echo "=================================================="

    # Recorder stalls are detected automatically, both kinds: repeated frames
    # (dropped, never tracked) and dropped frames / timestamp holes -- which is
    # what these cameras actually do. Neither is crossed by a track id. Details
    # land in <output>_freezes.json; run freeze_probe.py to see what a given
    # camera will produce before committing to a long run.
    #
    # --freeze_gap_ms is the dial: raise it for fewer, longer, less trustworthy
    # tracks. cam7 loses ~51% of its wall clock and fragments into ~136
    # segments at the 400ms default; cam1 into 3.
    #
    # --max_resolution 720 was tuned for tracking people. Hats, gloves, hairnets
    # and phones are far smaller in frame, so if recall looks poor this is the
    # first knob to raise (at the cost of VRAM -- the GPU is shared and this
    # pipeline still OOMs, see automated_tracking_summary.md §4).
    video_start=$(date +%s)
    docker run --gpus all --rm \
      -v "$(pwd)":/workspace \
      -v /media/jvn-server/185A27335A270CD6/saba/records:/records:ro \
      -w /workspace \
      -e PYTHONPATH=/opt/nuclio/sam \
      -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
      --entrypoint python3 "$IMAGE" \
      main.py \
        --video "/records/$filename" \
        "${prompt_args[@]}" \
        --max_num_objects 50 \
        --chunk_frames 64 \
        --overlap_frames 4 \
        --frame_stride 5 \
        --max_resolution 720 \
        --fill_hole_area 64 \
        --output "/workspace/$out" \
      > "$log" 2>&1
    rc=$?
    mins=$(( ($(date +%s) - video_start) / 60 ))

    if [ $rc -ne 0 ]; then
        echo "  FAILED (exit $rc) after ${mins}m -- see $log"
        # Keep whatever it produced, but under a name the resume check ignores.
        [ -f "$out" ] && mv "$out" "$out.PARTIAL"
        failed=$((failed + 1))
    else
        echo "  done in ${mins}m -- $(grep -m1 'Tracks found' "$log" || echo 'no track summary in log')"
        grep -m1 -E '^[0-9]+ discontinuity' "$log" | sed 's/^/  /'
        done_count=$((done_count + 1))
    fi

    elapsed=$(( ($(date +%s) - batch_start) / 60 ))
    processed=$((done_count + failed))
    if [ "$processed" -gt 0 ]; then
        remaining=$(( (total - processed - skipped) * elapsed / processed ))
        echo "  batch: ${processed} run, ${skipped} skipped, ${elapsed}m elapsed, ~${remaining}m left"
    fi
done

echo
echo "=================================================="
echo "Finished: $done_count ok, $failed failed, $skipped skipped, $total total"
echo "$(( ($(date +%s) - batch_start) / 60 ))m elapsed. Results in $OUTDIR/"
[ $failed -gt 0 ] && echo "Re-run ./run.sh to retry the failures (finished videos are skipped)."
exit 0
