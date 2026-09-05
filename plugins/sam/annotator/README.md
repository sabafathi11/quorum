# `sam3_auto_annotator`, copied here verbatim

Upstream: `~/Codes/newlabelstudio/sam3_auto_annotator`. Copied 2026-08-29,
unedited, and **not to be edited here**. Fixes belong upstream and come back as
another copy; a divergent fork of a 1,200-line tuned pipeline is exactly the
duplication this whole project exists to end.

Copied: `main.py`, `freeze_probe.py`, `test_freeze.py`, `DESIGN.md`,
`automated_tracking_summary.md`, `run.sh`.
Not copied: `annotations.xml` and `vids.txt` (sample data), and the git history.

## What Quorum does with it

Nothing imports this. `plugins/sam/auto.py` starts `main.py` inside the SAM 3.1
GPU image — `docker run --gpus all -v <this dir>:/annotator:ro …` — which is
where it was built to run. Quorum contributes three things and no more:

1. the command line, from a capture's streams and the person's prompts;
2. progress out of stdout, and a cancel into `docker kill`;
3. the CVAT XML back in as a `provenance="model"` mask layer.

The **contract** is therefore narrow and worth stating, because it is what
would break if upstream changed:

| what | where it is used |
|---|---|
| the flags (`--video`, `--text_prompt`, `--label_name`, `--output`, the tuning knobs) | `auto.build_args` |
| `Video loaded: N frames, …` | the denominator of the progress bar |
| `=== Processing Window: Global Frames A to B ===` | the numerator |
| `Tracks found: …`, `[FREEZE]`, `PARTIAL CHUNK` | the job's log and result |
| CVAT Video 1.1 XML at `--output` | parsed by `cvat_xml.parse_tracks` |
| `<output>_freezes.json` | the discontinuity summary in the SAM panel |

`tests/stub_sam/main.py` implements exactly that contract and nothing else, so
`tests/test_docker.py` can check all of it without a GPU. It cannot tell you
upstream still honours it — only running the real thing does — but it fails the
moment *our* side stops understanding it.

## `run.sh`

Kept for its comments, which are the record of which flag values were actually
tuned on this footage (`--max_resolution 720` for people, `--frame_stride 5`,
`--chunk_frames 64`) and why. Those values are the defaults of the `sam.auto`
job. Do not run it from here: it looks for `vids.txt` and writes into its own
working directory, which is the batch workflow Quorum's job queue replaces.

## Pointing at your own checkout instead

```toml
[plugins.sam]
annotator_dir = "/home/you/Codes/newlabelstudio/sam3_auto_annotator"
```

Then this copy is ignored and your working tree is what runs — useful while
you are changing the pipeline itself.
