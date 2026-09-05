# TODO

The working list for the session that continues [HANDOFF.md](HANDOFF.md). Order
is the user's, from the end of the previous session. `[x]` means done *and* a
test or a measurement backs it; `[~]` means built but only smoke-tested.

## 0 · This session (2026-08-25) — design in [DESIGN-uploads-time-display.md](DESIGN-uploads-time-display.md)

Five asks; four of them turned out to be the same mistake in four places — a
decision that belongs in one place, spread across every caller. Each now has
one owner, and the wrong answer is hard to reach rather than merely discouraged.

### A · core: assets, readiness, time
- [x] db migration: `assets` table; `streams.timestamps/duration/time_offset/enabled`;
      `captures.state/provider_kind`. ALTER, never recreate — ran clean on the
      387 MB database with the real capture in it.
- [x] `quorum/probe.py`: ffprobe packet-pts (0.2 s for a 140 MB HEVC camera),
      dims/codec/duration, one full-resolution still. `NoFFmpeg` rather than a
      plausible guess when ffmpeg is missing.
- [x] The frame map is **derived** — `bisect(timestamps, f/fps - offset)` — and
      cached in the column. Recomputing it reproduces `prepare.py`'s `sync.npy`
      **exactly**: 0 differing entries over 6 cameras × 9,002 frames. Getting
      there found a real bug: without rounding to whole microseconds first,
      `3/5.0 - 0.2` is `0.39999999999999997` and lands a frame early.
- [x] `core.stream_offset` op, folded last-write-wins, so a clock correction is
      attributed, appears in History and comes back with `Ctrl+Z`.
- [x] Asset routes: create / resumable `PUT` by offset / complete / list /
      `PATCH` (change the kind — never re-upload 150 MB) / delete / serve.
      A wrong resume offset is refused with the byte to restart from.
- [x] SDK: `AssetKind`, `Requirement`, `@PLUGIN.readiness`, `@PLUGIN.cleanup`,
      `ctx.asset(id)`, `ctx.assets(...)`, param type `asset`.
- [x] `jobs.submit` **refuses** a job whose requirements are unmet, with the
      reason — for the UI, the CLI and another plugin alike.
- [x] Capture editing: `POST /captures` (draft), `PATCH`, `/build`, stream
      `PATCH`/`DELETE`/`probe`, layer `PATCH`/`DELETE`, `/labels`, `/readiness`,
      `/frame/{stream}` (a still), `/streams/{key}/timestamps`.
- [x] `quorum/filters.py`: the serialisable object filter, SQL side. A filter
      that fails to parse shows everything — the safe direction.

### B · client: display authority, then the viewport
- [x] `web/core/display.js`: colour modes, filters, the serialisable filter.
- [x] **The four doors**, enforced in core code: drawing (`app.entriesAt`),
      hit testing (`viewport.pickAll`), selection (`app.select` drops what
      cannot be seen and says so once), navigation (`app.reveal`, the only
      sanctioned jump-and-point, which returns what it dropped).
      `LayerData.activeAt` → `activeAtRaw`, so the unfiltered set reads like a
      warning at every call site.
- [x] Display panel and a top-bar control: colour by track / class / layer /
      identity, class list with counts, plugin filter toggles.
- [x] `web/core/media.js`: the mosaic composed from the streams, a clock with a
      master and slaved cameras (playbackRate nudges, not seeks), and **one**
      level-of-detail policy consulted by every zoom path.
- [x] A cell is seeked to the timestamp of the frame the map names, not to
      `frame / fps` — measured: one boundary in 304 frames was wrong before.
- [x] Stills fallback, so zoom shows real pixels on a capture nobody has
      transcoded yet instead of a magnified re-encode.
- [x] Capture workspace: overview, streams (clocks, probe, enable), assets,
      layers, readiness, build.
- [x] Upload UI: resumable, progress, drag and drop, kinds from the server.

### C · plugins
- [x] `multiview`: a capture from uploaded videos. No `sync_id_ui/` anywhere.
      Verified end to end over HTTP: 3 cameras at 203/350/301 frames over the
      same 12 s, mapped onto one 24.98 fps timeline.
- [x] `media`: `prepare` / `drop`. Per-camera, full geometry, timestamps passed
      through — and it **proves** each rendition is frame-aligned with its
      source and refuses to offer one that is not. It caught a real 0.12 s
      rebase on the first file it was pointed at.
- [x] `voxel_carve`: declares its calibration requirement, validates what was
      uploaded (covers *these* cameras, R is a rotation), and refuses. The carve
      itself is still §5.
- [x] `cvat_xml`: takes uploaded assets where it took a directory.
- [x] `masks`: its `hidden` stylers became registered filters — two structural,
      one the `H` toggle, which is now the same state as the Display panel row.
- [x] `identity`: its recolouring became a colour mode available in any tool;
      the walk goes through `reveal` and tells the server what is hidden.
- [x] `proposals`: the queue withholds suggestions about a hidden class, and
      says how many rather than dropping them.

### D · tests and docs
- [x] 11 invariants (was 7): hiding holds at every door for every tool; a
      cell seeks to the frame the map names; frame maps match their timestamps;
      cells cover every enabled stream exactly once.
- [x] 24 mutations, all caught (was 13). New ones cover each door, the frame-map
      rule, the seek, the alignment proof and the requirement gate.
- [x] `mutants.py` parks originals in a sidecar and heals a killed run — a
      `finally` does not survive `SIGTERM`, and a leftover mutation silently
      disabled a client error path for an afternoon.
- [x] De-flaked the `Shift`+`Space` walk test (polling, not a fixed wait).
- [x] Code is served `no-cache`, so a zero-build-step app cannot leave somebody
      debugging yesterday's module.
- [x] PLUGINS.md, README.md.

### E · playback (reported from the running app: flicker, masks trailing)

The masks trailing the video was not a synchronisation bug. It was a frame
budget overrun with four causes, and once the client cannot keep up, the clock
ticks late and the overlay is drawn for a frame that has already gone.

- [x] **Stills were being requested during playback.** Zoomed past 1.15×, every
      cell wanted a full-resolution still, each one an ffmpeg seek on the
      server — six of them measured at **858 ms** — refreshed every 400 ms. The
      mask windows queued behind them. A still is a paused-inspection
      affordance: never while playing, never more than two at once, and only
      once a cell is magnified past 1.6× where it is worth anything.
- [x] **The timeline repainted every lane on every frame** — six streams of
      activity, every selected track and every plugin's lane decorator, 25
      times a second. The lanes are cached to an offscreen canvas now and only
      the playhead moves. Measured: 60 frames → 1 rebuild, was 60.
- [x] **Two lifecycle leaks.** A `Timeline` was never torn down, so every
      capture you opened left another one painting on every frame forever; and
      `openCapture` re-subscribed to the store each time without unsubscribing.
      Both now have a `destroy`, and a test asserts the subscription count does
      not grow across a reopen.
- [x] **The mask renderer** decoded and tinted ~50 masks a frame, allocating a
      canvas each. The run-length decode is cached per (object, frame) so
      recolouring and hit-testing never decode; the tint is one 32-bit store per
      pixel instead of four 8-bit ones, into a pooled canvas.
- [x] The clock is driven by the master video's `requestVideoFrameCallback`
      where it exists — it fires when a frame is *presented*, and at the
      video's rate rather than 60 Hz.
- [x] Prefetch runway scales with playback speed (a second of it), so a window
      never runs out mid-playback; presence is sent 4×/s rather than per frame.
- [x] Two navigations racing (click a card, change your mind, click another)
      left an unhandled rejection and a half-rendered screen. `goto` now takes a
      token and the loser stops touching the UI.
- [x] Three more mutations, all caught. **24/24.**

### F · the test suite was editing the real capture

- [x] The delete/undo step only undid its edit if every assertion passed, so a
      run that failed halfway left the delete in place. **Fifteen stacked
      deletes on one track had accumulated** before a drifting invariant count
      gave it away. Cleanup is in a `finally` now, and a final step asserts the
      suite left the capture exactly as it found it.
- [x] Waits are on conditions, not milliseconds — the suite was failing at
      random once `openCapture` also fetched a timestamp table per stream.

### G · getting results back out

- [x] Exporters wrote files into `data/exports/` and returned a path on the
      server's disk. There was no way to fetch one from the browser: uploads
      worked, downloads did not. `GET /api/exports` lists them, `/download`
      serves them — a directory of per-stream files as one zip (224 MB of XML
      → 27.5 MB), and a relative path that tries to leave the directory is
      refused. Listed in the Data panel with a download link, and an export job
      now offers its result rather than reporting a path.

**Left undone, deliberately:** the carve itself (§5); no human has seen the
running UI beyond the reporter's own session — jsdom proves it builds, paints
and routes, and says nothing about whether it looks right.

## 1 · Selection and navigation  (user asks 2, 3, 4)

- [x] Single-track selection — `Ctrl`+click picks one track, plain click still
      picks the whole identity, and a persistent scope switch (`Q`) flips the
      default. Destructive ops must name their scope before they run.
- [x] Overlapping masks — `Alt`+click cycles the stack under the cursor;
      `Alt`+right-click opens a disambiguation list. HUD shows `2/3`.
- [x] `Shift`+`Space` walk with the yellow ring pulse around the offered mask.

## 2 · Mask editing  (the biggest hole)

- [x] Core: undo — an op is marked undone, folds stop
      being told about it, and a History panel shows who did what.
- [x] `masks` plugin: structural ops (delete, restore, purge, split, join,
      unjoin) folded into a derived projection, then materialised into the
      core's objects/shapes so segment keys (`86@2470`, `1+2+3`) resolve.
- [x] Replay the 44 orphan identity assignments the import kept — **44 → 11**,
      and the 11 left are superseded join states from the desktop tool's own
      history, each already carrying the same identity as its final group.
- [x] Editing UI: tool, keys `D` `H` `P` `T` `J` `U` `Ctrl+Z`, scope-aware.
- [x] **Two mask layers, not one — a whole class of bug, not one bug.** Once `masks` materialises a derived layer,
      `app.primaryData()` is the wrong lookup everywhere: a joined track
      (`1+2+3`) or a segment (`49@1506`) lives in the derived layer, so identity
      selected it and then reported "nothing selected". Use `app.objectOf(id)`,
      `app.findObject(stream, key)` and `app.visibleObjects()` instead. The same
      assumption made the `Shift`+`Space` walk stop on superseded and deleted
      tracks — it now scans every mask layer and skips them (121 → 68 problem
      runs on the real capture).

      Two further traps behind the same door, both found by writing the test
      before believing the fix:

      1. **A key can exist in two layers at once.** The first half of a cut
         track keeps its original key, so `86` is both the imported track and
         the derived one that replaced it — 11 such keys on this capture.
         Resolution must prefer the copy a styler is actually drawing.
      2. **The assignment map outlives the tracks it names.** Joining 1, 2 and
         3 leaves entries for `1`, `2`, `3` *and* `1+2+3`, all on one identity,
         so group select handed back the superseded originals and selected 8
         invisible tracks. Membership is now indexed off what is on screen.

      Swept every other caller: `cvat_xml`'s exporter was shipping the
      **unedited** masks (it took one `layer_id`; it now exports the effective
      set by default — cam1 goes 442 → 422 tracks), `identity.link_gaps` was
      proposing links between tracks a human had already joined or deleted, and
      identity's timeline lane decorator drew no bar for a joined track.
      `primaryData()` survives only where any layer will do (frame mapping).

## 2b · The test suite  (added after group select shipped broken)

The example tests were all written *after* a bug was found by hand, so they
re-checked one case each and stayed green while group select was selecting
tracks nobody could see. Four layers now, run by `tests/run.sh`:

- [x] **Invariants** (`tests/invariants.mjs`) — seven checks over the whole
      capture and *every registered tool*, so a plugin added later is covered
      without anyone writing a test for it: one visible object per
      `(stream, key)`, lookups resolve to the drawn copy, nothing is painted
      twice, every tool selects only visible tracks, open proposals name live
      tracks, server and client agree on what exists, identity membership
      matches what is drawn. Run after load *and* around an edit-and-undo cycle.
- [x] **Property tests** (`tests/test_props.py`) — seeded random edit sessions,
      then assert the invariants; plus "undo everything and the capture is
      exactly as imported" and "a cut never loses or duplicates a keyframe".
      These caught what examples could not: the bugs come from *combinations*.
- [x] **Mutation testing** (`tests/mutants.py`) — breaks 13 load-bearing lines
      one at a time and fails if the suite still passes, naming each survivor
      and what goes wrong in the real world. It found two genuine holes on the
      day it was written (a purge that only checked the export path, and the
      OUTSIDE sentinel leaking into id allocation via `assign` rather than
      `link`). Both closed; 13/13 now caught, including three client-side ones.
- [x] Suite runs in **19 s** (`tests/run.sh`), 105 s before profiling: the pick
      sweep was repainting the inspector on every simulated click. A suite
      nobody waits for is a suite nobody runs.

Found by the invariants, not by hand — **68 of 1103 queued proposals named
tracks that no longer existed**. Fixed with a new SDK hook: `@PLUGIN.validator`
lets the plugin that owns an op kind say whether it still applies, so the queue
flags them `stale` (kept, never deleted, so a generator can still be scored) and
refuses to accept one with a readable reason. Listing that queue went 2 s → 140 ms
once the validator memoised its world lookup.

## 3 · Compute  — done 2026-08-29, see §8

- [x] SAM interactor: a route (`plugins/sam`), because it is the round trip of
      a gesture rather than long work. Measured on the real capture: **265 ms**
      to cut frame 5767 out of a 140 MB HEVC camera, ~1 ms of model on a stub.
      Writes nothing; the browser commits the preview through `masks`.
- [x] Tracker propagation (`R` / `Shift+R`) as a job, and **one op** for the
      whole propagation — 30 keyframes onto `cam1/198`, one `Ctrl+Z` to undo.
- [x] Auto-annotator (`sam3_auto_annotator`, copied verbatim) as a job, with
      per-window progress, cancel-kills-the-container, and the XML imported as
      a `provenance="model"` layer.
- [~] The **job backend abstraction** was not built, and deliberately: the
      premise changed. The SAM function now runs on the same host, reachable
      over a docker network, so the ssh + remote-helper + `docker port`
      apparatus `sync_id_ui` needed buys nothing — pixels cross a loopback,
      not a 1.5 MB/s link. What replaced it is narrower and real: the
      interactor and the tracker POST to a URL, and the auto-annotator drives
      the local docker daemon. If compute ever moves back behind a slow link,
      restore `sam_remote.py`, not the tunnel.

**Verified 2026-08-22, so the next session does not have to re-check:**

- `ssh vm.3090` works from here with `BatchMode=yes`; both SAM functions are up
  (`nuclio-nuclio-pth-comfyorg-sam3-1-v2`, published on host port **32901**, and
  the `-tracker` proxy). The protocol to copy is `sync_id_ui/sam_client.py`:
  ship frame **pts**, never pixels — the link is ~1.5 MB/s and a 1080p JPEG is
  ~670 kB, so uploading a frame costs about what the GPU work costs.
- The venv has **no PyAV**, so `cam_video.CamFrames` cannot be reused as-is.
  `ffprobe -select_streams v:0 -show_entries packet=pts -of csv=p=0` gives the
  same packet-pts table in **0.2 s** for a 140 MB camera video (11,667 packets
  for `cam1_20260612_021454.mp4`). Build the table with that and cache it.
- Frame identity must travel as a pts, not an index: these captures are
  variable-rate and the whole sync map in `prepare.py` is built from packet-pts
  order. An index seek lands on a different frame.

## 4 · Review QA  (`ppe_review_ui`)

- [ ] Freeze / discontinuity statistics, the "looks corrupted" flag.

## 5 · Geometry  (`camera_pics`, `calib_ui`)

- [ ] A calibration entity — there is none at all today.
- [ ] Epipolar curves, floor-grid reprojection, residuals, BEV.
- [ ] The voxel carve as a cross-camera identity generator.

## 6 · Platform gaps

- [ ] Login page, user management, roles enforced past two endpoints.
- [ ] Layer `dim` state has no UI. Object search. Settings. Capture delete.
- [ ] Proposals: batch accept, conflict surfacing, reviewer agreement.
- [ ] Structural conflict handling now that structural ops exist.

## 7 · Packaging

- [x] Dockerised — `Dockerfile`, `docker-compose.yml`, `docker/entrypoint.sh`.
      It did not slow the cycle: `./run.sh` and `tests/run.sh` are untouched and
      still the fast path, `plugins/` and `web/` are bind-mounted so editing a
      plugin is a page reload, and the image is python + ffmpeg + ~300 MB.
      The one decision worth not re-deriving: **the tree is mounted at the same
      path it has on the host**, because a capture's media path is stored
      absolute — a tidier `/srv/annotation` would come up fine and show six
      black rectangles, and a database made inside would not work outside.

---

## 8 · Session of 2026-08-29 — SAM, and the container

Two asks: dockerise it, and finish the SAM feature — interactive like
`sync_id_ui`, but over a docker network rather than ssh, plus the auto-annotator
with custom prompts, progress and cancel.

### What was built

- `plugins/sam/` — one plugin, four files and a vendored pipeline.
  `client.py` (the nuclio protocol and the two mask encodings), `frames.py`
  (exact-pts extraction), `auto.py` (the batch job), `plugin.py`, `web/sam.js`,
  and `annotator/` — `sam3_auto_annotator` copied verbatim with a README
  stating the contract Quorum depends on.
- `Dockerfile`, `docker-compose.yml`, `docker/{entrypoint.sh,quorum.docker.toml}`,
  `.dockerignore`, `.env.example`.
- `tests/test_sam.py` (23), `tests/test_docker.py` (4, opt-in),
  `tests/stub_sam/main.py`, 7 new mutations.

### Core changes, each small and each forced

- **`[plugins.<id>]` was documented and never wired.** `Config` had no
  `plugins` field, so `quorum.toml.example`'s own `[plugins.grid_capture]`
  example would have exited with `unknown setting 'plugins'`. Added the field;
  `Host` now defaults to it.
- **Readiness reasons can be scoped.** `@PLUGIN.readiness` could only return
  sentences, which block the whole plugin. `sam` holds two unrelated abilities
  — an interactor that needs the model endpoint and a batch job that runs the
  model itself — so a reason may now be a dict with `tools`/`jobs`.
  `app.js:blockedReason` was made to agree with `Host.blocked`: a reason naming
  jobs and no tools does not grey the workspace.
- **`masks.keyframes`**, the bulk form of `masks.keyframe`. A propagation is
  one decision; sixty ops would each be individually undoable and collectively
  unusable.
- **`masks.materialise` no longer requires an imported layer** when there are
  creations, so the interactor can draw the first mask on a bare capture.
- **`mask_layer` exports `decodeRLE`**, for the SAM preview — which is not a
  layer and must not become one until somebody presses Enter.

### Things not to re-derive

- **A frame is fetched by presentation timestamp, never by index.** This is the
  whole reason `frames.py` exists; `sam_frame_by_index` is the mutation that
  proves the test can fail. Measured: on a 24-frame VFR fixture, index-as-time
  lands four frames away.
- **The ROI is not an optimisation, it is recall.** SAM rescales to a fixed
  square. Verified against a stub: the crop sends 11 kB with the prompt at the
  centre of a 224 px box; without it, 553 kB of 1920×1080 with the person 70 px
  tall.
- **`docker kill`, not `Popen.kill`.** Killing the client leaves the daemon
  running a 40-minute GPU job with nobody watching. `test_docker.py` asserts
  the container is gone.
- **The progress bar leaves 5% for the import**, because otherwise it goes
  99% → 97% at the end, and a bar that goes backwards is a bar nobody believes.
- **The container runs as the host user** (`user:` and `group_add:` in compose).
  `data/` is shared with `./run.sh`, and one root-owned cache file left by an
  earlier root container made a still un-overwritable, the composition retry,
  and `the timeline does not repaint every lane on every frame` fail with 3 and
  then 4 rebuilds against a budget of 2. It read exactly like flakiness. The
  SAM container the auto-annotator starts still runs as the image's own user by
  default — that is how upstream's `run.sh` has always run it, and changing how
  the model runs to tidy up ownership is the wrong trade — but the output
  *directory* is ours, so those files can still be replaced and deleted, and
  `[plugins.sam] run_as` exists for anyone who wants otherwise.
- **`tests/run.sh` starts with `mutants.py --verify`.** A killed or raced
  mutation run leaves a broken line on disk and it presents as an unrelated
  failing test, a long way from the cause — it happened twice. `--verify`
  checks every original is present exactly once and needs no sidecar to do it.
  It found a latent problem the moment it existed: doors 3 and 4 filter with
  the same line, so one mutation was matching by position.
- **`docker-cli`, not `docker.io`.** On Debian 13 the engine package no longer
  ships a client; installing it gives you containerd and no `docker`, which is
  a confusing way to find that out.
- The interactor is a **route**, not a job, and the reason is written at the top
  of `plugins/sam/plugin.py`. Do not "fix" it into the queue.

### Measured on vm.3090, 2026-08-29 — the VRAM question

Asked directly: do the interactive and the auto runs share one model? **No, and
they cannot as things stand.** Both halves of that are worth keeping:

- The nuclio function builds `build_sam3_multiplex_video_**model**` and
  *deliberately drops the text encoder* — `model_handler.py` maps only
  `tracker.model.*` and `detector.backbone.vision_backbone.*`, with the comment
  "detector.backbone.language_backbone.* -> text encoder (we skip this)". The
  auto pipeline builds `build_sam3_multiplex_video_**predictor**` from the full
  checkpoint because it needs `add_prompt(text)`. They are different assemblies
  of overlapping weights, so this is not a config change: the resident
  interactive model **cannot** answer a text prompt.
- Nor can the auto job become an HTTP call to that function: its `eventTimeout`
  is 120 s and `numWorkers` is 1, and a camera takes ~10 minutes.

Measured (RTX 3090 Ti, 24,564 MiB, shared with `beefline-cctv-deepstream` and
`mixture_pbrl`):

| | MiB |
|---|---|
| interactive model, resident from deploy | **4,902** |
| what an auto run adds on top of it, peak | **~10,800** |
| peak total during the run | **23,841 of 24,564 (97%)** |

It ran without OOM and the function took no restarts. **The duplicated weights
are the smaller half of the problem**: of the ~10.8 GB an auto run costs, only
about 5 GB is a second copy of the model — the rest is SAM 3's own session and
grounding-cache state, which is exactly what `automated_tracking_summary.md` §4
says is unsolved. So unifying the two models would buy ~5 GB of a ~10.8 GB
cost, and the levers that decide whether a run fits remain `--max_num_objects`,
`--chunk_frames`, `--max_resolution`, and who else is on the card.

Throughput, while measuring: 4,825 of 11,667 frames in ~4 minutes at
`--frame_stride 5` — about 10 minutes per camera.

### 2026-08-30 — one model, and CVAT is gone

The user's call, after the numbers above: **kill CVAT, unify the models, take
the 5 GB.** All three done.

- **CVAT stopped**, not removed — 19 containers of the `cvat` compose project,
  listed in `~/cvat-stopped-2026-08-30.txt` on the box so it is one
  `docker start` away. Its volumes are untouched. The two nuclio functions are
  stopped too, which is where the VRAM was.
- **`plugins/sam/service/`** — one process holding one
  `Sam3MultiplexVideoPredictor`, serving the nuclio protocol byte for byte
  (so `client.py` never changed) *plus* `/auto`, which runs the vendored
  `annotator/main.py` **in that process** by monkey-patching its builder to
  return the predictor already loaded. `apply_memory_fixes` is guarded to run
  once; `track_window` is guarded so a cancel lands at a window boundary.
- **`auto.py` no longer starts containers.** It POSTs a job description and
  polls for the pipeline's own stdout, which it parses with the same code it
  used to read off a pipe — the transport changed, the contract did not.

Measured, on the same capture and the same card:

| | two models | one |
|---|---|---|
| idle | 12,059 MiB | **7,152** |
| auto peak | 23,841 (97%) | **16,978 (69%)** |
| free at peak | 723 MiB | **7,586** |
| interactor | 369 ms | **158 ms** |

Three things that cost real time and are one line each now:

- **the tracker inside the predictor has no `backbone`.** Standalone it is built
  `with_backbone=True`; inside the full predictor the detector owns it. Attached
  by reference (`predictor.model.detector.backbone`, a `SAM3VLBackboneTri`), so
  it costs nothing.
- **`autocast` and grad mode are thread-local.** `main.py` enters autocast once
  at the top because it is a script; a threaded server doing the same serves
  every request without it and dies on "mat1 and mat2 must have the same dtype".
  Every GPU entry point now goes through one `gpu()` context that carries the
  lock *and* the dtype regime.
- **`docker restart` keeps the old image.** Rebuilding and restarting looks like
  it worked and runs the previous code. Recreate.

Two knobs deliberately removed rather than left lying: `--max_num_objects` is a
*constructor* argument of the predictor, so it is `SAM_MAX_OBJECTS` on the
service and no longer a per-job field that would be accepted and ignored; and
the prompts are sent as fields, not repeated in `flags`, or the pipeline gets
each one twice (`sam_prompts_sent_twice` is the mutation that proves the test).

`test_docker.py` is gone with the thing it tested; `test_service.py` replaces it
and needs no daemon, so it runs in the ordinary suite.

### Verified, and not

Everything was exercised end to end against a stub endpoint speaking the real
protocol, on this repository's real 9,002-frame capture: the interactor
(265 ms/frame, correct capture→stream frame map, ROI shifted back exactly), the
tracker (31 frames out of a 140 MB HEVC file, 30 keyframes, one op, undone
cleanly), and the auto-annotator (mounts, progress, cancel, import) against a
real docker daemon. The container was built and run, and drove the host daemon
from inside.

**The model answered, on 2026-08-29.** Quorum now runs on vm.3090 itself
(`~/annotation/quorum`, `docker-compose.vm3090.yml`), on CVAT's `cvat_cvat`
network, and both interactive paths were exercised against the real SAM 3.1:

- interactor on `cam1_20260612_021454.mp4` frame 5767 — 164 ms to cut the frame
  out of the 1920×1080 HEVC, **369 ms of model**, a real 224×146 mask of 284
  runs, correctly shifted back out of its 224×224 ROI;
- tracker — 20 keyframes in 8 s, none lost, written as one op;
- auto — ran the real pipeline over cam1 at 97% card occupancy (above).

On 2026-08-30 all three were re-verified on the **unified** model: the
interactor returns the same box (`[788, 466, 224, 146]`) in 158 ms, and an auto
run drove to 38% through Quorum with no second container on the card.

What is still unverified is a **human looking at the SAM workspace in a
browser**. Every path under it has been driven over HTTP; nobody has clicked
one.

---

## Conventions this list must not break

See HANDOFF.md. The short version: ops are the storage format, the core never
opens a payload, plugins get no tables, a model never writes an annotation, and
`Space` is play/pause in every tool.
