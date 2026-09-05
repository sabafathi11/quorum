# beefline_records — sessions, and the SAM annotations that go with them

One directory per recording session. **The person who clones this repository
does not run SAM.** The annotations are already here; the videos are one command
away; everything else is ordinary Quorum.

```
beefline_records/
├── README.md                       ← this file
├── fetch_session.sh / .ps1         ← pull a session's videos from Drive
└── session_20260811_033622/
    ├── cam1_20260811_033622.mp4    ← the recordings.  NOT in git — see below
    ├── cam2_20260811_033622.mp4
    ├── …                              (7 cameras)
    ├── session.json                ← what is here, and what it is
    └── sam/                        ← the SAM output.  In git.
        ├── cam1.xml                ← CVAT Video 1.1, one file per camera
        ├── …
        ├── cam1_freezes.json       ← discontinuity report, per camera
        ├── …
        └── run.json                ← prompts, parameters, sizes, checksums
```

Google Drive mirrors this, one zip per session:

```
beefline_records/
├── session_20260811_033622.zip     ← the 7 mp4s, flat, original names
└── session_XXXXXXXX_XXXXXX.zip
```

## Why the videos are not in git

They are 150–207 MB each. GitHub refuses any file over 100 MB, so a repository
with them committed is one that cannot be pushed. They are in `.gitignore`, they
live in Drive, and `fetch_session.sh` puts them where this file says they are.
That is the only thing the split changes: the layout above is what you end up
with either way.

The filenames are load-bearing and must not be changed. Quorum takes each
stream's key from the name — `cam3_20260811_033622.mp4` → `cam3` — and keys the
whole capture by the stamp the files share. Renaming them to `camera_01.mp4`
would silently change every key and break the match between a video and the
`sam/cam3.xml` that annotates it.

## Getting a session

```bash
./fetch_session.sh 20260811_033622                  # needs an rclone `gdrive:` remote
./fetch_session.sh 20260811_033622 ~/Downloads/session_20260811_033622.zip
```
```powershell
.\fetch_session.ps1 20260811_033622
.\fetch_session.ps1 20260811_033622 C:\Users\you\Downloads\session_20260811_033622.zip
```

The second form needs nothing installed: download the zip from the shared Drive
folder and hand the script the path.

## Starting from the SAM results

Three commands. None of them is SAM.

```bash
# 1. build the capture from the videos you just fetched
.venv/bin/python -m quorum ingest multiview.disk \
  paths="$(python3 -c 'import json,glob;print(json.dumps(sorted(glob.glob("beefline_records/session_20260811_033622/*.mp4"))))')"

# 2. import the annotations — the whole session in one go
.venv/bin/python -m quorum job cvat_xml.import:tracks capture_id=1 \
  dir=beefline_records/session_20260811_033622/sam \
  layer=sam_auto name="SAM auto" provenance=model

# 3. make the video playable, because these are HEVC
./run.sh      # then Capture workspace → Streams → Prepare video…
```

Step 1 prints the capture id; use it in step 2. On Windows the same three,
with `.venv\Scripts\python` and `.\run.ps1`.

**Step 3 is not optional and it is not fast.** These recordings are HEVC 1920×1080,
which no browser decodes, so until a rendition exists every cell is black. It is
one ffmpeg transcode per camera — minutes each, seven cameras — and it is a job,
so it has progress and a Stop button. See [docs/MANUAL.md §4](../docs/MANUAL.md).

From there the masks are ordinary tracks: the Edit workspace splits, joins and
repaints them, Identity stitches them across cameras, and History has every edit
as an op. The imported layer is `provenance="model"` — a generator's output, not
somebody's decision — which is exactly the distinction the Review queue is built
on.

## The annotation format

`sam/<stream>.xml` is **CVAT Video 1.1** with `<mask rle=…>` shapes — the format
`cvat_xml` reads and writes, so the round trip through Quorum is lossless and the
files open in CVAT itself unchanged.

- **One file per camera**, named for the stream key it belongs to. The importer
  globs `*.xml` and matches on that key, which is why `run.json` and the
  `_freezes.json` sidecars can sit in the same directory without confusing it.
- **Frame numbers are that camera's own frames**, not capture frames. The frame
  map Quorum builds when it ingests the videos is what lines them up, and it is
  derived from real presentation timestamps rather than from `n / fps` — these
  recorders are variable-rate and index arithmetic lands on the wrong picture.
- **RLE is carried verbatim.** Nothing re-encodes a mask on the way in or out.
- **A `.xml.PARTIAL`** means that camera failed part-way through its run. It is
  deliberately not imported — `*.xml` does not match it — and it is kept so the
  failure is visible rather than silent.

`sam/<stream>_freezes.json` is the discontinuity report. These recorders drop up
to half their wall clock and **every hole ends a track**, so a camera that
fragments into 136 pieces can say why. No identity is ever carried across a
hole; that is a property of the data, not a bug in the tracker.

`sam/run.json` records what produced the rest: the prompts and labels, the
stride and resolution, the capture id it ran against, and a size and sha256 for
every file — so "did I get all of it" has an answer that is not a guess.

## Adding a session

On the machine with the GPU and the recordings:

```bash
mkdir beefline_records/session_<stamp>
# put the videos there (copy, or symlink them from the records disk)
scripts/run_sam_session.py <stamp> --prompts person
```

That ingests, annotates every camera, harvests the XML into `sam/` and writes
`session.json`. Then zip the videos flat and put the zip in Drive:

```bash
cd beefline_records/session_<stamp> && zip ../../session_<stamp>.zip *.mp4
rclone copy ../../session_<stamp>.zip gdrive:beefline_records/
```

Commit `sam/` and `session.json`. Never the videos.
