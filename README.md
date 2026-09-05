# Quorum

A multi-view annotation server whose core knows almost nothing.

It knows that a **capture** is some **streams** sharing one time base, that a
stream's frames map onto the capture's frames, that **layers** hold **objects**
with **shapes** on frames, that edits are **ops** in an append-only log, and
that long work belongs in a **job**. It does not know what a mask is, what a
person is, what a camera sees, or what your files are called on disk.

Everything you can actually *see* — the mosaic of six cameras, the masks drawn
over it, identities stitched across cameras, the review queue — comes from a
plugin. The five PyQt tools in this repository are the reason: they are one
product written five times, sharing a data model enforced by discipline rather
than by a package. Here that model is the core, and each of those tools becomes
a plugin that can be replaced on its own.

```
web browser
   │  ES modules, no build step: a plugin's UI is a file the server hands out
   │  the mosaic is composed here, from the cameras — there is no mosaic file
FastAPI  ── /api  REST · /ws  one room per open capture · /plugins/<id>/web
   │
core     ── captures · streams · timestamps · derived frame maps · layers
           objects · shapes · ops (append-only) · assets · readiness
           per-plugin document store · jobs · users
   │
plugins  ── multiview · media · grid_capture · cvat_xml · mask_layer
           masks · identity · proposals · sam · voxel_carve
```

New here, or picking this up in a fresh session? Read
**[docs/HANDOFF.md](docs/HANDOFF.md)** first — it states the goal, the decisions
already taken, what is not built, and the conventions to keep.

## Setup

Python 3.11 or newer — the config reader is `tomllib`, which is 3.11.

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install fastapi "uvicorn[standard]"      # numpy comes from the system
./run.sh                                               # → http://127.0.0.1:8600
```

`ffmpeg` and `ffprobe` must be on `PATH`. They are how every frame timestamp is
read and every still is cut, so without them building a capture, preparing a
video and the SAM interactor all refuse — with that reason, rather than
mysteriously. Everything else still works.

Copy `quorum.toml.example` to `quorum.toml` to change ports, auth or where
plugins are looked for. To run it in a container instead — which is how it runs
on the box with the GPU — see [In a container](#in-a-container).

### On Windows

The core is portable — sqlite, threads, stdlib paths — so the server itself
runs unchanged. Three things about the Linux recipe above do not carry over,
and all three are about the environment rather than the code:

```powershell
winget install Gyan.FFmpeg          # ffprobe/ffmpeg on PATH; reopen the shell
py -3.11 -m venv .venv              # no --system-site-packages: see below
.venv\Scripts\pip install -e .
.\run.ps1                           # → http://127.0.0.1:8600
```

- **No `--system-site-packages`.** On the lab boxes numpy is inherited from the
  system python, which is why the line above installs only fastapi and uvicorn.
  There is no system numpy on Windows, so install the package itself — numpy is
  already a dependency in `pyproject.toml` — and the venv is complete.
- **`.venv\Scripts\python.exe`, not `.venv/bin/python`.** That is the whole
  reason `run.ps1` and `dev.ps1` exist beside `run.sh` and `dev.sh`; they take
  the same arguments (`.\run.ps1 --port 8611`, `.\dev.ps1 start|stop|restart|log`).
- **ffmpeg is not there by default.** `winget install Gyan.FFmpeg`, or unpack a
  build and put its `bin` on `PATH`. `run.ps1` warns if `ffprobe` is missing
  rather than letting you find out at the first **Build**.

Set `media_roots` explicitly in `quorum.toml` while you are there. The default
is the clone's *parent* directory, which on a laptop is something like
`C:\Users\you` — legal, but far more of the disk than this server needs to be
able to read:

```toml
media_roots = ["C:/Users/you/videos"]     # forward slashes; TOML treats \ as an escape
```

**Segmenting on Windows.** The SAM *interactor* — click a person, get a mask —
needs two things: `ffmpeg` locally, to cut the frame out of the video, and an
HTTP endpoint that answers with a mask. It does not need docker or a GPU on
this machine; only the auto-annotator does, because that one starts a container.
So point it at a service that is already running:

```toml
[plugins.sam]
url = "http://127.0.0.1:9000/"      # e.g. ssh -L 9000:localhost:<sam port> gpubox
```

`$SAM_URL` overrides that. The SAM panel says which of the three sizes work and
why, so an unreachable endpoint reads as a sentence rather than as a dead key.
One caveat that is architectural rather than incidental: `plugins/sam/client.py`
posts the *frame* to the service — it was written for a server sitting beside
the function on a docker network. Over a tunnel that is ~670 kB of JPEG per
click, so expect the interactor to feel like the link, not like the GPU.

## Ten minutes, from a browser and nothing else

Press **New capture**, drop the camera videos on the Data panel, press **Build**.
That is the whole path: Quorum reads the videos itself — every view's real frame
timestamps, one constant-rate capture timeline, a frame map per view — with no
preprocessing script and no ffmpeg mosaic anywhere in it.

The grid you then look at is *composed in the browser* from the cameras. So
correcting a camera's clock is a slider rather than a 40-minute re-encode, the
masks move with the picture because both are read through the same map, and
zooming into a cell shows that camera's own pixels instead of a magnified
640×360 re-encode of them.

If a source is in a codec your browser cannot decode — these captures are HEVC —
run **Prepare video** once per camera. That transcodes each camera on its own, at
full geometry, with timestamps passed through. Nothing is stacked into a file
and no clock offset is baked into anything.

## Ten minutes, on this repository's real data

```bash
# 1. the capture: six cameras, one mosaic, the pts-exact frame map that
#    sync_id_ui/prepare.py already built
.venv/bin/python -m quorum ingest grid_capture.prepared stamp=20260612_021454

# 2. the masks: 1,832 tracks, 136,582 keyframes  (about 5 seconds)
.venv/bin/python -m quorum job grid_capture.import:tracks capture_id=1

# 3. the identities you made by hand, imported as ops rather than as a file
.venv/bin/python -m quorum job identity.import:consistent_ids capture_id=1

# 4. a generator's opinion — proposals, never assignments
.venv/bin/python -m quorum job identity.link_gaps capture_id=1 layer_id=1

./run.sh        # then open the capture, press N to walk unassigned tracks
```

Step 3 imports 15 identities over 862 tracks; 818 of those track keys match an
imported CVAT track. The other 44 are keys like `86@2470` and `1+2+3` — tracks
`sync_id_ui` *split* and *joined*, edits that live in its own state and would
arrive here as ops from a mask-editing plugin that does not exist yet. Nothing
is silently dropped: those assignments are kept and simply refer to objects the
mask layer has not been told about.

Step 4 found 1,104 candidate links on that capture. Scored against the
hand-made identities: **84.4%** of the scorable ones agree with what you did by
hand, and **98.9%** (87 of 88) agree at confidence ≥ 0.85. That number is the
whole argument for the review queue — a generator is worth having when you can
say where its opinion is worth trusting, and worth ignoring where you cannot.

## Ten minutes, on files that are already on the server

The deployment that matters does not have the videos on your laptop. The
cameras write to a share the GPU box has mounted read-only, and the way to a
capture used to run through downloading 900 MB and posting it back up to the
machine that could already open it. So there is a second door:

Press **Import from the server** on the lobby, browse its disks, tick the six
cameras of one session, press Run. The recordings are read where they lie —
nothing is uploaded, nothing is copied into the tree, and the stream's media
path *is* the file on the share.

The picker filters on the server, which is not a nicety: `records` is one flat
directory of ~1,400 files named `cam<n>_<stamp>.mp4`, and typing the stamp
narrows it to that session's six, which **Select all** then takes in one click.
The capture is keyed by that stamp, so re-importing the session updates it
rather than making a second one — and a stamp another provider already
imported is refused by name instead of being quietly overwritten.

The same door exists for every other kind of file. **Data → From the server…**
registers a path as an asset without copying a byte, so a CVAT XML, a track
pickle or a calibration on the server is one click from an importer that has
never heard of either upload or path. A linked asset says `linked` on its row,
says `missing` if the share is unmounted, and deleting it removes the link and
never the recording.

What bounds all of this is the boundary that already existed: `media_roots`,
checked with the same `is_media_allowed` that decides whether a file may be
served. There is no second list and no second policy — a path the picker
offers is a path the video endpoint would have served, and `/etc` is refused
here for exactly the reason it is refused there.

## What is in the box

| plugin | provides | what it is |
|---|---|---|
| `multiview` | providers `videos`, `disk`; asset kind `video` | the standalone path: a capture built from video files — uploaded, or already on the server. Probes each one for its frame timestamps, picks a timeline, lays out a grid. Reads nothing anybody else wrote. |
| `media` | jobs `prepare`, `drop` | per-camera renditions so a browser can decode them, and so six cells at once do not melt the machine. Proves each one is frame-aligned with its source, and refuses to offer one that is not. |
| `voxel_carve` | tool **Carve**; asset kind `calibration` | the worked example of a plugin that declares what it cannot work without. The carve itself is not ported; the gate is. |
| `grid_capture` | providers `scan`, `prepared`; importer `tracks` | knows what a *stamp* is, where `raw_vids/` lives, and that a mosaic is three cells to a row. Reuses `sync_id_ui/data/<stamp>/` rather than rebuilding it. |
| `cvat_xml` | importer + exporter `tracks` | CVAT track XML in and out. RLE strings are carried verbatim, so the round trip is lossless. |
| `mask_layer` | layer type `mask.rle`; job `stats` | the payload shape `{box, rle}`, the codec, and the renderer that puts it on the overlay and answers what is under the cursor. |
| `identity` | tool **Identity**; ops; importer/exporter `consistent_ids`; job `link_gaps` | the only entity that spans streams. Three ops folded into one map. Imports and exports `consistent_ids_<stamp>.json`. |
| `proposals` | tool **Review** | a queue of pre-built ops with their evidence. Accepting one appends *that* op, attributed to the person who accepted it. |
| `sam` | tool **SAM**; jobs `track`, `auto` | SAM 3.1, in three sizes: click a person and get a mask now, carry one mask across the next sixty frames, or set four text prompts loose on six ten-minute cameras. The first writes nothing; the other two are jobs you can watch and stop. |

## Segmenting with SAM

Three things, kept apart because they cost wildly different amounts and only
one of them is free to get wrong.

**Click a person.** Open the **SAM** workspace, left-click on somebody, and a
mask appears — left adds a point on the thing, right adds one on what to leave
out, `Backspace` takes the last one back. Nothing is written until you press
`Enter`, and then it is written *through `masks`*, as you, on one track: it is
in History, `Ctrl+Z` takes it back, and it is the same kind of edit as a cut or
a join. With a track selected (`Alt`+click) `Enter` repaints that track's mask
at this frame; with nothing selected it creates a new one.

**Carry it forward.** `R` propagates the selected mask across the next frames of
its own camera and `Shift+R` goes backwards. That is a job — a minute of GPU —
so it has progress and a Stop button, and the whole propagation is *one* op:
you made one decision, and one `Ctrl+Z` takes all sixty keyframes back.

**Annotate everything.** The **Auto-annotate** panel runs
[`sam3_auto_annotator`](plugins/sam/annotator/README.md) — copied here verbatim,
never rewritten — over whole cameras from text prompts you type, one camera at a
time, for hours. Progress is per window, Stop kills the container, and a run
that finishes lands as its own `provenance="model"` layer with a label per
prompt. Its discontinuity report is shown beside it, because these recorders
drop up to half their wall clock and every hole ends a track: a camera that
fragments into 136 pieces should be able to say why.

Two things it is careful about, both of which are the same care the rest of this
codebase takes:

- **A frame is fetched by its presentation timestamp, never by index.** These
  recordings are variable-rate. `n / fps` lands on a different picture, and SAM
  then segments whoever is standing there instead — a mask that looks entirely
  plausible, on the wrong frame, with nothing on screen to admit it. That is the
  whole reason `plugins/sam/frames.py` is its own file, and
  `test_a_frame_is_fetched_by_timestamp_not_by_index` is the test that would
  have caught it.
- **The prompt is cropped to what you are pointing at.** SAM rescales its input
  to a fixed square, so a person 130 px wide in a 1920×1080 frame arrives about
  70 px tall. Cropping to a 224 px box around the prompt sends 11 kB instead of
  550 kB and the person arrives nearly full size. `C` turns it off for something
  large.

### One model

The interactor, the tracker and the auto-annotator all run on **one** SAM 3.1,
in one process — `plugins/sam/service/`. They did not always: a nuclio function
held a tracker-only assembly for the first two, and every auto run started a
container that built the full predictor again. Measured on the 3090 Ti:

| | before | after |
|---|---|---|
| idle | 12,059 MiB | **7,152** |
| during an auto run, peak | 23,841 — **97% of the card** | **16,978 (69%)** |
| free at peak | 723 MiB | **7,586 MiB** |
| interactor round trip | 369 ms | **158 ms** |

They could not simply be pointed at each other, and the reason is worth
knowing: the nuclio handler builds `build_sam3_multiplex_video_model` and drops
the text encoder *on purpose* — "detector.backbone.language_backbone.\* -> we
skip this" — so it cannot answer a text prompt at all. The auto pipeline builds
`build_sam3_multiplex_video_predictor` because it must. The full predictor,
though, contains the other one:

```
Sam3MultiplexVideoPredictor              handle_request — text prompts, video
  .model         Sam3MultiplexTrackingWithInteractivity
    .tracker.model   Sam3VideoTrackingMultiplexDemo   ← what the interactor needs
  .detector.backbone SAM3VLBackboneTri                ← the vision backbone it does not own
```

so the service reaches for both rather than building either twice. Two things
that cost an afternoon to find and are now one line each in that file: inside
the predictor the tracker's `backbone` is `None` (the detector owns it, and it
is attached by reference — no extra VRAM), and `autocast` is **thread-local**,
so a threaded server that enters it once at import meets fp32 weights with
bf16 activations on every request.

A third, and it is the one that broke propagation on the GPU box while leaving
clicking perfectly fine. `SAM3VLBackboneTri.forward_image` returns the
interactive and propagation necks as named sub-dicts but the SAM3 detection
neck **flat** at the top level, and the tracker finishes with a clone pass that
assumes every top-level key is a neck — so it reaches
`backbone_out["vision_features"]["backbone_fpn"]`, subscripts a 4-D tensor with
a string, and dies with `IndexError: too many indices for tensor of dimension
4`. The interactor never met it because it leaves `need_sam3_out` at its
default of `False`; the tracker asks for all three. CVAT patched the same
defect on `TriHeadVisionOnly` from inside `ModelHandler.__init__` — which this
service deliberately never runs, and which names the sibling class anyway. So
the nesting is done here, on the tracker's *reference only*: the detector is
the one component that actually reads that neck, and patching the class or the
shared instance would trade a broken tracker for a broken auto-annotator.

**One thing at a time.** A single lock serialises the card. That is not new —
nuclio ran `numWorkers: 1` and the pipeline was never safe to run twice — it is
the same constraint stated in one place. A click during an auto run waits for
the current window, which is seconds.

**Pixels are posted to the model here, and that is a deliberate reversal.**
`sync_id_ui` went to great lengths not to — it shipped frame timestamps over ssh
and ran a helper inside a container on the far box, because the link ran at
~1.5 MB/s and a 1080p JPEG cost about what the GPU work cost. Quorum runs on the
same host as the function and reaches it over a docker network, so that trade is
gone and the ssh apparatus buys nothing. If this ever moves back behind a slow
link, restore the helper, not the tunnel.

```toml
[plugins.sam]
url = "http://sam-service:8080/"        # $SAM_URL wins
```

That is the whole configuration. The image, the GPU flag and the container→host
path map all belonged to starting a container per run, and there is no
container per run. One endpoint gates everything, because there is one model
behind it.

## In a container

```bash
cp .env.example .env        # set TREE to this tree's absolute path
docker compose up -d --build
```

**The tree is mounted at the same path it has on the host.** That is the one
decision here worth reading twice: a capture's media path is stored in the
database as an absolute path, so a container that mounted the tree somewhere
tidy would come up fine, list every capture, and show six black rectangles.
Mounting it where it already lives makes the two ways of running this the *same
installation* — stop the container, run `./run.sh`, and nothing has moved.

`plugins/` and `web/` are bind-mounted too, because there is no build step here:
a plugin's UI is a file the server hands out, so editing one is a page reload
rather than an image rebuild. That was the condition on dockerising at all.

**It runs as you, not as root.** `PUID`/`PGID` in `.env`, and `DOCKER_GID` so it
can reach the socket. That is not tidiness: `data/` is shared with `./run.sh`,
and a container writing root-owned files into it breaks the host-side server
later and quietly — a cached still ffmpeg cannot overwrite, a composition that
retries, a timeline repainting when it should not, and a test failure that reads
like flakiness. Found the hard way, in this session, in about an hour.

Two more things the compose file asks you to decide, both on one visible line:

- **the SAM network.** `SAM_NETWORK` is the network nuclio already put the
  function's container on; compose joins it and never tries to own it.
- **the docker socket.** Only the auto-annotator needs it, to start the GPU
  image. A container that can talk to the daemon can do anything the daemon can,
  so it is one line you can delete — and without it the interactor and the
  tracker are untouched while the auto-annotator refuses, with the reason.

The image carries python, ffmpeg and about 300 MB. The model is not in it and
should not be.

### On the GPU box

```bash
docker compose -f docker-compose.yml -f docker-compose.vm3090.yml up -d --build
ssh -L 8600:127.0.0.1:8600 vm.3090          # it stays on loopback there
```

Three things differ on that machine, each written on one line of the override
file: the camera videos are outside the tree under a root-only mount, so
`$RECORDS` adds a second `media_roots` entry; the container runs as root,
because that mount is `drwxr-x--- root:root` and there is no host-side
`./run.sh` there anyway (python 3.8); and it binds loopback, because that box
already publishes fifteen services on 0.0.0.0.

### Through the tunnel

That `ssh -L` is not a detail. A round trip to vm.3090 is ~700 ms rather than
the ~1 ms it is on the laptop, and a forwarded port fails in the least helpful
way there is: it keeps accepting, so the page looks fine and quietly stops
being true. Five things exist because of it, and each is a bug that was found
by using it from there rather than a precaution:

- **Nothing waits forever.** Every request has a timeout, reads are retried
  twice with backoff, and a chunked upload retries the chunk — asking the
  server where it actually got to — rather than throwing away 140 MB because
  one packet was lost. The server has always been resumable; the client was
  not, which came to the same thing.
- **The socket proves it is alive.** The client pings every 15 s and drops a
  socket that has not answered in 35. Without it a dead tunnel is a tab that
  keeps its socket, stops hearing about other people's edits, and says nothing.
- **A reconnect catches up.** It re-fetches the ops it missed and replays them,
  and re-reads what is *derived* rather than appended — because an undo during
  the gap flips a flag on an op already behind the watermark, and no message in
  the log would ever mention it.
- **The prefetch window is measured, not assumed.** 45 frames of mask runway is
  the right bet when the answer is back before three frames have played. Here it
  empties while its own replacement is in flight, once per window, for the whole
  of playback. The window and the lead are both functions of the observed round
  trip now.
- **Idle connections are kept for 75 s.** uvicorn hangs up after five by
  default, so reading a panel for six seconds cost a new connection — and a new
  connection is a round trip of its own.

The top bar says `reconnecting…` while it is, and shows the round trip once it
is over 250 ms. Both are silent on a normal network, which is the point.

The model runs beside it, from `plugins/sam/service/`:

```bash
cd plugins/sam/service && docker compose up -d --build
```

`scripts/sam-up.sh` is the one to copy over and keep: it creates the service if
it is missing, starts it if it is down, waits rather than restarting one that is
merely loading, and prints how much VRAM it is holding against how much is left.
`--watch` keeps doing it.

## The core, in full

**Captures and streams.** A capture has `n_frames` and `fps`. Each stream has its
own frame count, the presentation time of every one of its frames, a clock
offset, and a `frame_map` — `int32`, one entry per capture frame, giving that
stream's own frame number. That is how six cameras running at different rates
stay in step, and it is served to the browser as a typed array.

The map is **derived**, not stored as a fact: it is
`bisect(timestamps, f / fps - offset)`, which is the rule
`sync_id_ui/prepare.py` used, moved to where it can be re-run. That is what
makes a clock correction a slider instead of a preprocessing run — and it is
checkable, which matters more: recomputing it reproduces that script's
`sync.npy` exactly, 0 differing entries across 6 cameras × 9,002 frames. The
offset is an op, so it is attributed, appears in History, and comes back with
`Ctrl+Z`.

**Assets.** A file somebody uploaded, plus a **kind** — a string a plugin
declared. The core stores the bytes, the name, the size and the hash, and never
opens the file. That declaration is the entire coupling between the upload
machinery and the things people upload, which is why the voxel carve can need a
calibration without anything in the upload path knowing what a calibration is.
Uploads are resumable, because a camera video is 150 MB.

An asset can also be a file **already on the server**, registered by path and
never copied — `GET /api/browse` lists what is inside `media_roots` and
`POST …/assets/link` points a row at one. That is one endpoint and one rule
(the same `is_media_allowed` that guards serving), and it is what makes the
recordings share usable without a 900 MB round trip to a laptop and back.
Deleting a linked asset deletes the link; the file is not ours.

**Readiness.** A plugin declares what it cannot work without. The core greys the
tool out and says why — and `jobs.submit` **refuses**, with the reason, before
the handler is entered. The button is a courtesy; the refusal is the guarantee,
and it holds for the CLI too.

**What is on screen** is one decision, in `web/core/display.js`, and the core
closes the four doors an object reaches a person through: drawing, hit testing,
selection and navigation. Hiding a class, colouring by class, colouring by
identity — all of it is one authority rather than a rule each feature has to
remember. Every version of "each feature filters for itself" in this codebase
has shipped a bug.

**Layers, objects, shapes.** A layer has a `type` string owned by whichever
plugin declared it. An object is one thing in one stream, with a `key` that
means what the source says it means. A shape is `(object, frame, outside,
payload)` and the payload is opaque JSON the core never opens. The one piece of
interpretation the core does perform is temporal: `GET /api/layers/{id}/frames`
returns the shape active at a frame plus every keyframe in the window after it,
mapped through each stream's frame map. Everything else about the payload is the
layer type's business.

**Ops.** Every edit is an append-only record: `capture, layer, actor, ts, kind,
payload`, where `kind` is namespaced by plugin (`identity.link`). The core
appends, orders and broadcasts; it never interprets. Snapshots are derived —
`identity`'s whole state is a 40-line fold over its own ops, which is what makes
two people editing one capture ordinary rather than frightening.

**Exports.** An exporter writes files into `data/exports/` and returns their
paths — the right shape for a 40 MB XML per camera, and for a job that finishes
while the tab is closed. The core lists what is there and serves it, zipping a
directory of per-stream files on the way out, so "how do I get my annotations"
has an answer that is not `ls` on the server.

**The document store.** A namespaced JSON value per `(capture, plugin, key)`
with optimistic versioning: a stale write is refused with a reason, never
merged. Plugins keep derived state here; the ops remain the truth.

**Jobs.** Ingest, imports, exports and generators all run through one queue with
progress, cancellation and a live feed to the browser. A request never does long
work.

**Users.** `auth_mode = "open"` trusts one local operator, which is the posture
the desktop tools have today. `auth_mode = "token"` takes bearer tokens and the
three roles annotator / reviewer / admin. That switch is the only difference
between a laptop and a lab server.

What the core deliberately has **no** idea about: masks, RLE, identities,
cameras, calibration, geometry, SAM, CVAT, voxels, people — or what is inside
any file it was handed.

## Writing a plugin

See **[docs/PLUGINS.md](docs/PLUGINS.md)**. The shortest useful one is about
thirty lines. Both halves live in one directory:

```
plugins/my_thing/
  plugin.py            # PLUGIN = Plugin(id="my_thing", ...) — jobs, routes, importers
  web/my_thing.js      # export default { id, activate(ctx) { … } }
```

The server mounts `plugin.route` under `/api/p/my_thing/`, serves the `web/`
directory, and the browser imports the module at boot. There is no build step
and no bundler: a plugin is a file the server hands out.

## Tests

```bash
tests/run.sh              # unit + property + browser        (~35 s)
tests/run.sh --quick      # python only, no server needed    (~10 s)
tests/run.sh --mutants    # …then check the tests can fail   (~90 s)
tests/run.sh --docker     # …and the auto-annotator, for real (~60 s)
```

Four layers, because three of them were not enough, plus one that is opt-in:

| | what it does | why |
|---|---|---|
| `test_core.py`, `test_masks.py` | examples: one fold, one cut, one union | fast, and each one names a mistake somebody actually made |
| `test_props.py` | random edit sessions, then assert invariants | the bugs here come from *combinations* — a cut whose tail was joined to another track — and nobody writes those cases down in advance |
| `smoke.mjs` | boots the real client in jsdom against a running server, then runs `invariants.mjs` over the whole capture and every registered tool | catches what a syntax check cannot, and the invariants apply to plugins that do not exist yet |
| `mutants.py` | breaks a load-bearing line on purpose and fails if the suite still passes | a green suite proves nothing on its own |
| `test_docker.py` | runs the auto-annotator against a real daemon, with the model stubbed | mounts, a progress pipe, a cancel that kills the *container*, and the XML becoming tracks — none of which a unit test can reach |

That last one is not decoration. The suite was green while group select was
picking tracks nobody could see, because every test had been written *after* a
bug was found by hand and only ever re-checked that one case. `mutants.py`
found two more holes the day it was written, and it names each survivor along
with what goes wrong in the real world if that line is broken.

The invariants are the part to extend. They say nothing about any particular
track — "one visible object per (stream, key)", "every tool selects only visible
tracks", "hiding holds at every door, for every tool", "a cell seeks to the frame
the map names", "the server and the client agree on what exists" — so a plugin
added next month is checked without anyone writing a test for it. Eleven of
them, and 28 deliberately broken lines that all get caught.

One of those invariants was rewritten by the SAM workspace rather than waived
by it. "Every tool selects only visible tracks" also assumed every tool
*selects on a plain click*, and SAM must not: a click there is a prompt point,
and moving the selection out from under a half-finished prompt would be the bug.
So the check now runs whichever gesture actually selects — `Alt`+click, which
means the same thing everywhere — and the part that matters, that nothing
invisible is ever selected, still applies to both.

`mutants.py` edits real source files. It parks the original in a sidecar first
and restores anything a killed run left behind, because a `finally` does not run
through a `SIGTERM` — and a mutation left on disk silently disabled a client
error path for an afternoon. That was not quite enough: the sidecar can itself
be lost (a second run that healed and tidied it up), and then the broken line
simply stays. So `tests/run.sh` now begins with `mutants.py --verify`, which
needs no sidecar — it asserts every mutation's original line is present, exactly
once — and refuses to run a suite whose result could not be trusted. It caught
one thing immediately: two of the four "doors" filter with an identical line, so
the door-3 mutation was matching on position rather than on meaning and a
reordering would have moved it silently to door 4.

## Status, honestly

**Works:** capture ingest from uploaded videos *or* from a prepared directory,
resumable browser uploads with a plugin-declared kind per file, editing a
capture after the fact (name, frame rate, clocks, layout, layer types, which
file is what), CVAT XML and pickle import, mask rendering with per-pixel hit
testing, the mosaic composed from the cameras, level-of-detail so zooming shows
the camera's own pixels, per-camera clock offsets as ops you can undo, colour by
track / class / identity and hiding a class everywhere at once, playback and
stepping, the timeline with per-stream activity lanes, identity assignment with
the op log, multi-user broadcast and presence, the proposal queue with
accept/reject, `consistent_ids` import and export, jobs with progress and
cancellation, the command palette, light and dark — and the SAM interactor,
tracker propagation and text-prompt auto-annotator, in a container, against the
function on the same host.

**Not written yet:** the calibration workspace; the voxel carve itself —
`voxel_carve` ships the requirement gate and refuses without a calibration, but
the carve is still `calib_ui/carve.py` plus a C kernel; `ppe_review_ui`'s freeze
statistics as a workspace of their own; and a settings UI for plugin
configuration.

**Known rough edges:** a frame window is JSON, so a two-second prefetch of dense
masks is ~100 kB gzipped — fine on a LAN, worth revisiting as binary if this
ever runs over the 1.5 MB/s link; the jsdom smoke test needs the server running,
which is not obvious when it fails; split/join lineage has no owner yet, as
above; the SAM interactor is a route rather than a job, deliberately (it is the
round trip of a gesture, ~300 ms, and it writes nothing) but it is the one place
a request waits on something external; and no human has yet driven the SAM
workspace against the real function — every path here has been exercised
end to end against a stub that speaks the same protocol, on this repository's
real 9,002-frame capture, but the model itself has not answered.
