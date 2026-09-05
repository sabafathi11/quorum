# Handoff — where this is and what it is for

Written 2026-08-18 at the end of the session that built the core. Read this
first, then [README.md](../README.md) for how to run it and
[PLUGINS.md](PLUGINS.md) for the SDK contract.

---

## The goal

The `jlab/annotation` tree holds five PyQt5 desktop tools (~19k lines) that are
really one product written five times: `camera_pics` (correspondence clicking +
fisheye SfM), `sync_id_ui` (cross-camera identity + SAM mask editing),
`ppe_mask_ui` (a fork of it with identity removed), `ppe_review_ui` (read-only
QA of a remote SAM3 run), and `calib_ui` (calibration inspector + voxel-carve
workbench). They share a data model — `sync_id_ui/data/<stamp>/{tracks.pkl,
sync.npy, meta.json}` and a `Track` class copied four times — enforced by
discipline rather than by a package. `calib_ui/session.py` even carries a
comment saying it must mirror `sync_id_ui/app.py:rebuild_cam` exactly.

**Merge them into one professional, multi-user web platform.** Two constraints
the user stated explicitly:

1. **The core must be as bare as possible.** Every visualiser and annotation
   tool is a plugin. The core knows captures, streams, frame maps, layers,
   objects, shapes, ops, jobs and users — and nothing about masks, identities,
   cameras, calibration or voxels.
2. **General, without making this lab's workflow any harder.** Someone else's
   multi-view setup should be a plugin + config, while opening a capture here
   stays as fast as it is in the desktop tools.

### Decisions already taken (do not re-litigate)

| question | answer |
|---|---|
| build on CVAT? | **No.** CVAT stays a stock upstream instance on `127.0.0.1:8085`; it is the label *source*, integrated over its REST API, never forked. |
| who runs it? | **Lab server, multi-user.** Accounts, roles, an op log with attribution. |
| how general? | **Plugin SDK.** Seven extension points; the five desktop tools are meant to become the first five plugins. |
| what generates identities? | A **proposal generator** feeding a human review queue. The voxel carve is *one such generator*, not the centrepiece — an earlier design doc over-weighted it and the user corrected that. |

---

## What exists now

```
quorum/
  quorum/          the core: config, db (sqlite), sdk, plugins loader, jobs,
                   realtime hub, host, api/{app,routes,deps,media,ws}
  web/             zero-build client: index.html, app.css, core/*.js
  plugins/         grid_capture · cvat_xml · mask_layer · identity · proposals
  tests/           test_core.py (python) · smoke.mjs (jsdom client boot)
  docs/            PLUGINS.md · HANDOFF.md (this file)
  data/            sqlite db, blobs, exports — gitignored, disposable
```

~4,550 lines. Run with `./run.sh` → http://127.0.0.1:8600.

### Measured, not assumed

- Capture ingest from `sync_id_ui/data/<stamp>/`: 6 streams, 9,002 frames.
- Mask import: **136,582 keyframes in 4.5 s**; the sqlite file lands at ~387 MB
  for one capture.
- `GET /api/layers/{id}/frames?frame=N&span=30`: **~18 ms, ~103 kB gzipped**
  (a 2-second prefetch). A single frame is ~11 kB.
- `identity.import:consistent_ids` on the real file: 15 identities, 862 tracks.
- `identity.link_gaps` (the example generator): 1,104 proposals; scored against
  the user's hand labels, **84.4% agree overall, 98.9% (87/88) at confidence
  ≥ 0.85**. That spread is the argument for the review queue.
- `cvat_xml` export ran once: 6 files, 39.8 MB for cam1 against a 39.7 MB
  source. **RLE-level fidelity was never verified** — an unfinished check.

### Tests

```bash
.venv/bin/python tests/test_core.py            # 7/7 — fold, RLE, op log, doc store
./run.sh & (cd tests && npm i) && node tests/smoke.mjs   # 16/16
```

`smoke.mjs` boots the *real* client in jsdom against a running server with a
recording 2D context and asserts it gets as far as painting masks. It caught two
real bugs (tools never reaching the rail; the review tool stealing `Space` from
play/pause). Keep it green — it is the only thing standing in for a browser.

---

## Not implemented

**Whole tools with no plugin yet**

- **Mask editing** — the editing half of `sync_id_ui`/`ppe_mask_ui`: delete,
  purge, restore, split (`T`), join/unjoin (`J`), undo (`Ctrl+Z`), the SAM
  interactor (`S`/`Enter`), tracker propagation (`R`). Masks are read-only
  today. *This is the biggest hole and the recommended next piece.*
- **`ppe_review_ui`** — freeze/discontinuity stats from `_freezes.json`, the
  "looks corrupted" flag, remote re-run, refresh-from-server.
- **`camera_pics`** — correspondence clicking, fisheye SfM, plumb-line edges,
  surface groups, floor levelling. There is no calibration entity at all.
- **`calib_ui`** — epipolar curves, floor-grid reprojection, residuals, the 3D
  view, BEV, dense scene prior, metrics, the voxel carve, and the carve→CID
  generator. `identity.link_gaps` is a placeholder holding that slot open.
- **Remote compute** — jobs run in a local thread pool. The ssh+docker backend
  that ships timestamps to `vm.3090` and gets RLEs back does not exist, so
  nothing GPU-bound can run.

**Built but partial**

- Auth: token mode written, never exercised; no login page, no user management
  UI; roles enforced on two endpoints; no per-capture assignment or claiming.
- Multi-user: presence and op broadcast work. The structural-conflict handling
  (serialise per track lineage, reject with a readable reason) is **not** built
  — there are no structural ops yet, and identity ops genuinely commute.
- Undo: `ops.undone` exists and nothing uses it.
- Viewport: hold-`Z` zooms the mosaic cell, not the full-res source video. The
  non-mosaic per-stream layout is coded but never exercised. Hit-testing only
  works after the first paint.
- Proposals: one at a time; no batch accept, no spot-check re-queue, no reviewer
  agreement stats, no surfacing of conflicts with existing human assignments.
- Layers: on/off only (`layerOpacity()` has a `dim` state no UI can set).
- No object search, settings UI, capture-delete UI, or pagination anywhere.

**Written, never run:** `cvat_xml` importer, `grid_capture.scan`,
`mask_layer.stats`, token auth, non-mosaic layout, websocket reconnect.

**From the design doc, not built:** `mvcore` was never extracted — the five
desktop tools are **completely untouched**, four duplicate `Track` classes and
all. Quorum reads their output; nothing imports from Quorum. So this is
currently *additive*, not a de-duplication. Also skipped: a required lens-model
field on calibrations, Postgres/S3 (sqlite + filesystem), HLS (plain mp4 with
byte ranges).

**Never verified:** no human has looked at the running UI. jsdom proves it
builds and paints; it says nothing about whether the layout, contrast or the
dark/light palettes actually look right.

---

## Requested next, by the user (2026-08-18)

Stated at the end of the session and deferred to a fresh one. Recorded here so
they are not lost; the user has not been asked to re-confirm them.

1. **Everything in "Not implemented" above**, roughly in that order. It need not
   all fit one screen — separate tabs/workspaces are fine and expected.
2. **Selecting a single mask must be possible.** In `sync_id_ui`, clicking
   always selected the whole consistent-id group, so deleting one track deleted
   the group in every camera unless its cid was cleared first. Painful, and a
   real data-loss trap. Quorum currently reproduces this: `identity`'s pick
   handler selects the whole identity. Needs a modifier (or a mode) for
   single-track selection, and the destructive ops must make the scope obvious.
3. **Selecting an overlapping mask** — when two masks overlap it was very hard
   to reach the one underneath. Wants alt-click cycling, a disambiguation
   popup, or similar.
4. **The `Shift`+`Space` walk.** Quorum has it as `N` (`identity.next`); the
   user wants the original chord *and* the missing **yellow ring pulse** around
   the mask being offered. The desktop behaviour to match is in
   `sync_id_ui/README.md` — "the selection is the cursor".
5. **Dockerise it**, if that does not slow the build/test cycle much.
6. **Use a todo list** while working.
7. The user noticed the previous session used `bash` (`cat`/heredocs) for file
   edits instead of the Read/Write/Edit tools and asked why. It was this
   session's auto-mode instruction, not a preference — a fresh session should
   just use the normal file tools.

---

---

## Added 2026-08-25 — uploads, clocks, the mosaic, display policy, zoom

Design: **[DESIGN-uploads-time-display.md](DESIGN-uploads-time-display.md)**.
Five asks, and four of them were the same mistake in four places: a decision
that belongs in one place, spread across every caller. What changed, and the
things a fresh session should not re-derive:

- **The mosaic is not a file.** `make_grid.sh`'s stacked 200 MB re-encode is
  gone from the model; a capture has cells, and the browser composes them from
  the cameras themselves. A legacy `layout.mosaic` is kept and treated as one
  more rendition — the one that happens to cover every cell.
- **A stream's `frame_map` is derived**, from its own packet timestamps and a
  clock offset, and the offset is an op. The derivation reproduces
  `sync_id_ui/prepare.py`'s `sync.npy` exactly (0 differing entries over 6
  cameras × 9,002 frames), which is the reason to trust it.
- **A cell is seeked by timestamp, not by `frame / fps`.** This is the contract
  with the annotations and it is an invariant now. Do not "simplify" it back.
- **The display authority owns what is on screen.** Four doors are closed in
  core code — drawing, hit testing, selection, navigation. A plugin cannot leak
  a hidden object by forgetting, because it is never handed one. Anything that
  *offers objects to a human* over HTTP takes `?filter=`.
- **A plugin declares what it cannot work without**, and `jobs.submit` refuses.
  `voxel_carve` exists to demonstrate exactly this and nothing else.
- **The upload machinery knows only a kind string.** Adding a plugin that needs
  its own file requires no change to any of it.

Two things bitten by, worth remembering:

- `tests/mutants.py` edits real source. A `SIGTERM` (a `timeout`, a CI kill)
  skips `finally`, and a leftover mutation silently disabled a client error path
  for an afternoon. It now parks originals in a sidecar and heals on start.
- The client is served with `no-cache` now. Without it, a zero-build-step app
  leaves people debugging yesterday's module.

Still not done and still wanted: the carve itself (TODO §5), remote compute for
SAM (§3), and a human actually looking at the UI on more than one screen.

## Added 2026-08-29 — SAM, and the container

Two asks, both done; the working record is **[TODO.md §8](TODO.md#8--session-of-2026-08-29--sam-and-the-container)**
and the user-facing account is README.md's *Segmenting with SAM* and
*In a container*. What a fresh session should not re-derive:

- **The compute premise changed.** The plan was a job backend abstracting
  ssh+docker to `vm.3090`. It is not needed and was not built: the SAM function
  runs on the same host now, reachable over a docker network, so the whole
  `sam_remote.py` apparatus — ship timestamps, run a helper in a container over
  there, never send pixels — buys nothing. Pixels cross a loopback. **If compute
  ever moves back behind the 1.5 MB/s link, restore the helper, not the tunnel.**
- **`plugins/sam/annotator/` is a verbatim copy** of
  `~/Codes/newlabelstudio/sam3_auto_annotator` and must stay one. Its README
  states the six-line contract Quorum actually depends on (the flags, three
  stdout lines, the XML, the freeze sidecar), and `tests/stub_sam/main.py`
  implements exactly that so the plumbing can be tested without a GPU. Fixes go
  upstream and come back as another copy.
- **A frame is addressed by presentation timestamp, never by index.** Same rule
  as the frame map, same reason, and it is now load-bearing in a second place.
  `plugins/sam/frames.py` exists for it alone.
- **The interactor is a route and the two long things are jobs.** That split is
  argued at the top of `plugins/sam/plugin.py`; it is not an oversight.
- **The container mounts the tree at its own host path.** Media paths are stored
  absolute, so this is what makes `./run.sh` and `docker compose up` the same
  installation rather than two.
- Three core gaps were closed on the way, each because something needed it:
  `[plugins.<id>]` config was documented but never wired to `Config`; readiness
  reasons could not be scoped to some of a plugin's jobs; and `masks` had no
  bulk keyframe op, so a 60-frame propagation would have been 60 undos.

**Still never verified:** the real SAM function has not answered. Everything is
exercised end to end against a stub speaking the same protocol — on the real
9,002-frame capture, and through a real docker daemon — but this machine has no
GPU and no SAM container. The protocol is copied from `sync_id_ui/sam_client.py`,
which is known to work against it; the copy is what is untested.

## Added 2026-08-30 — one model, and CVAT is gone

Full record in [TODO.md §8](TODO.md). The short version, and the things not to
undo:

- **CVAT is stopped, not deleted.** 19 containers, listed in
  `~/cvat-stopped-2026-08-30.txt` on vm.3090; volumes intact. The nuclio SAM
  functions are stopped with it — that is where the duplicated 4,902 MiB was.
- **There is one model now**, in `plugins/sam/service/`, and everything that
  segments anything goes through it. It serves the old nuclio protocol
  unchanged *and* runs the vendored auto pipeline inside its own process. Do
  not reintroduce a container per run: that was the 97%-full card.
- **`annotator/` is still a verbatim copy.** The service adapts to it by
  monkey-patching from outside — the builder, `apply_memory_fixes`,
  `track_window` — precisely so the copy stays a copy.
- `model_handler.py` in `service/` is CVAT's, also verbatim. Its `__init__`
  builds a model, which is the one thing that must not happen twice, so the
  object is constructed without it and given the four attributes its methods
  use. The wrapper class CVAT defines *inside* that `__init__` is lifted out
  unchanged because a local class cannot be imported.
- **`autocast` is thread-local**, and this is a threaded server. Every GPU
  entry point goes through `gpu()`. Do not call the model outside it.

## Conventions to keep

- **Ops are the storage format.** Every edit is an append-only, namespaced,
  attributed record; snapshots are derived (`identity.fold` is 40 lines and is
  the model to copy). Never add a "current state" table that the log cannot
  rebuild.
- **The core never opens a payload.** If you find yourself teaching
  `quorum/` what a mask is, that belongs in a plugin.
- **Plugins get no tables.** Namespaced document store or blobs. Adding a table
  is a core change, discussed as one.
- **Never write an annotation from a model.** Generators emit proposals with
  evidence; a human accepts, and the op is attributed to them.
- **Inputs are read-only.** Nothing under `sync_id_ui/`, `raw_vids/`,
  `camera_pics/` or `ppe_*/` is ever written. Exports go to
  `quorum/data/exports/`, uploads to `quorum/data/uploads/`, transcodes to
  `quorum/data/renditions/`.
- **Nothing derived is stored as a fact.** A frame map is recomputed from
  timestamps and an offset; a clock offset is a fold over ops. If you cannot
  rebuild it from the log and the inputs, it does not belong in a column.
- **One owner per decision.** What is on screen, what colour it is, which copy
  of the pixels a cell shows, what time a camera is at — each has exactly one
  place that decides. Adding a second is how every bug in this file's "gotchas"
  section started.
- **`Space` is play/pause in every tool.** Tool-scoped keys must not steal it.
- **Abstention is a first-class answer.** A generator that says "no opinion
  here" beats one that always answers — for the carve this is load-bearing,
  since only ~15% of that shop has 3+ unblocked camera views.

## Gotchas found the hard way

- `pkill -f "quorum serve"` kills the agent's own shell (its command line
  contains the pattern). Use a pidfile.
- **Never run `tests/mutants.py` while another suite is running.** Both edit
  the same source files; the second one's heal removes the first one's sidecar
  and a mutation stays on disk. `tests/run.sh` opens with `mutants.py --verify`
  now, which catches it in a second instead of an hour, but the fix is not to
  overlap them.
- 44 of the 862 imported identity assignments refer to keys like `1+2+3` and
  `86@2470` — tracks `sync_id_ui` split and joined. They are kept, and point at
  objects no layer has declared. A mask-editing plugin replaying those ops is
  what closes the gap.
- `200` is the OUTSIDE sentinel, not an identity. It must never seed id
  allocation (this was a real bug; see `test_fold_outside_is_a_sentinel`).
- `tests/smoke.mjs` rewrites plugin `web` URLs to `file://` because node cannot
  import over http. Harness detail, not an app difference.
- The published artifact at
  `https://claude.ai/code/artifact/ae50de5b-fffb-443b-af4d-4c5dda57d3aa`
  ("Quorum Workbench") predates the code and over-weights the voxel carve.
  Treat this file as authoritative where they disagree.

---

## Added 2026-09-01 — reading the server's own disks

One ask: *"import from remote — get its stuff from vm.3090 (or wherever it is
deployed) without me uploading or copying anything into the docker mount."*

The premise was right and worth stating plainly, because it changes what the
upload path is *for*: on the deployment that matters the files are never on the
laptop. The cameras write to `/media/jvn-server/…/records`, which the GPU box
already has mounted and which `docker-compose.vm3090.yml` already names as a
second media root. Everything needed to open those files was in place; the only
missing piece was a way to say *which* ones.

So, three pieces and no new plugin:

- **`GET /api/browse`** (`quorum/api/browse.py`) — list what is inside
  `media_roots`. It calls `cfg.is_media_allowed`, the same function
  `serve_file` calls, so there is exactly one answer to "what will this server
  read" and the picker cannot offer a path the video endpoint would refuse.
  Server-side `?q=` filtering, because `records` is one flat directory of
  ~1,400 files and finding one session's six cameras in it *is* the task.
- **`POST …/assets/link`** — an asset that points at a file where it lies.
  `meta.linked`, `state='ready'` immediately, no bytes copied. `delete_asset`
  must never unlink one; getting that backwards would erase a recording.
- **`multiview.disk`** — the provider. It shares `_finish()` with `videos`, so
  a capture built from the share and one built from uploads are the same
  capture; only where the paths came from differs.

Client: `web/core/browse.js` (the picker), a `paths`/`path` field type in
`ui.form` beside the existing `asset` one, and a **From the server…** button on
the Data panel.

Two things that are load-bearing rather than tidy:

- **The key comes from the filenames**, so a re-import updates rather than
  duplicating. That also means the stamp `grid_capture` already imported would
  have landed on *its* capture and rewritten six streams under 136,582
  keyframes. It is refused by name now. "Idempotent" and "overwrites somebody
  else's row" are one keystroke apart.
- **A linked asset is only as stable as the mount.** `present` says so on the
  row. That is the honest cost of not duplicating 150 MB per camera, and it is
  visible rather than discovered.

Verified on the real files: six 1920×1080 HEVC cameras from
`raw_vids/20260604_045509/` imported in about three seconds — real per-camera
timestamps, derived frame maps (cam3 is short and its map ends where it should),
byte-range playback straight off the source. `smoke.mjs` has a step for it.
