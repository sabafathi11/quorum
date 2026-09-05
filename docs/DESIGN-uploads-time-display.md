# Design — uploads, clocks, the mosaic, display policy, and zoom

Written 2026-08-25, before the code, for the five things asked for at the end of
the previous session. Read [HANDOFF.md](HANDOFF.md) first; this file adds to it
and does not contradict it.

The five asks are not five features. Four of them are the same mistake made in
four places: **a decision that belongs in one place was spread across every
caller.** Where the mosaic lives, what time a camera is at, which tracks are on
screen, which pixels a cell should show — each of those is currently answered by
whoever happens to be asking. The design below gives each question exactly one
owner and makes the wrong answer hard to reach, rather than adding a check at
each site that gets it wrong today.

---

## 0 · What changes in the core

Three additions, and one deletion.

| | what | why it is core and not a plugin |
|---|---|---|
| **assets** | uploaded files with a plugin-declared *kind* | the core already owns "a capture has files"; it must not own what is in them |
| **stream timestamps + offset** | every frame's presentation time, and this stream's clock error | the core owns `frame_map`; a map you cannot recompute is a map you cannot correct |
| **readiness** | a plugin declares what it needs; the core refuses to run it without | only the core sees every plugin, so only the core can gate one |
| ~~mosaic video~~ | *deleted as a concept* | the mosaic is a way of *looking* at streams, not a thing a capture has |

Everything else lands in the client or in plugins.

---

## 1 · Uploading, and editing what you uploaded

### What is wrong today

A capture can only be made from paths that already exist on the server's disk,
in the layout `sync_id_ui/prepare.py` happens to write. There is no way to make
one from a browser, and once made there is no way to change anything about it:
not its name, not its frame rate, not which file a stream reads, not what a
layer's type is. `grid_capture` is not a general provider, it is a reader for
one other program's output directory — which is exactly the dependency this
project exists to remove.

### The design: assets

An **asset** is a file somebody uploaded, plus a **kind**. The core stores the
bytes, the name, the size, the hash and the kind string. It never opens the
file.

```
assets(id, capture_id, kind, name, path, size, received, sha256,
       content_type, state, meta, actor, created, updated)
```

A **kind** is declared by a plugin and namespaced to it:

```python
PLUGIN.asset_kind(AssetKind(
    id="video",                                  # -> "multiview.video"
    title="Camera video",
    description="One file per view. Anything ffprobe can read.",
    accept=[".mp4", ".mkv", ".mov", ".ts"],
    multiple=True,
    on_ready="probe"))                           # a job of this plugin, given {asset_id}
```

That declaration is the *entire* coupling between the upload machinery and the
things people upload. The upload dialog is built from the list of declared
kinds; it has no idea that a video is a video or that a calibration is a
calibration. A plugin added next month gets an upload UI without the upload UI
changing.

Uploads are **resumable**, because a camera video here is 140 MB and the link
this will eventually run over is 1.5 MB/s:

```
POST   /api/captures/{cid}/assets      {name, size, kind}   -> {id}
PUT    /api/assets/{id}/data?offset=N  <raw bytes>          -> {received}
POST   /api/assets/{id}/complete                            -> {sha256, state}
GET    /api/assets?capture={cid}
GET    /api/assets/{id}/data                                 (byte ranges)
PATCH  /api/assets/{id}                {kind?, name?, meta?}
DELETE /api/assets/{id}
```

`PATCH` is the "change the data type later" ask: an asset uploaded as the wrong
kind is re-labelled, not re-uploaded. Nothing else has to know — whoever
consumes that kind will simply see it appear.

### Jobs consume assets without knowing about uploads

A parameter spec gains one type:

```python
params={"tracks": {"type": "asset", "kind": "cvat_xml.tracks", "required": True}}
```

The dialog renders a picker of that capture's assets of that kind (with an
"upload one now" button); the CLI takes `tracks=<asset id>`; the handler gets
`ctx.asset(id)` → a path. An importer therefore never learns that uploading
exists, and never grows a second code path for "file on disk" vs "uploaded".

### Captures you can edit

A capture gains `state` (`draft` | `ready`) and `provider_kind`. The flow:

```
POST /api/captures        {key, name, provider, provider_kind}   -> a draft
  … upload assets into it …
POST /api/captures/{cid}/build                                    -> runs the provider
PATCH /api/captures/{cid} {name?, fps?, config?, layout?}         -> and build again
```

The provider is required to be **idempotent**: building twice with the same
assets produces the same streams. `Writer.stream()` already upserts, so this
costs nothing. Rebuild is therefore the only "edit" primitive the provider needs
to implement, and everything configurable is configurable by editing config and
building again.

Editable without a rebuild, because they are core facts rather than provider
output: capture name and fps, stream name/order/offset/enabled, the grid layout,
layer name/type/provenance, and every asset's kind.

The **Capture workspace** is a core workspace (rail entry `⚙`) with panels for
Overview, Streams, Assets, Layers and Build. It is core because every noun on it
is a core noun; it contains no word that a plugin owns.

### Requirements — "make it not work without them"

A plugin says what it needs:

```python
PLUGIN.requires(Requirement(
    asset_kind="voxel_carve.calibration", at_least=1,
    why="the carve intersects camera frusta; without the solved calibration "
        "there is no geometry to intersect",
    fix="Upload the solved calibration_result.json as “Camera calibration”."))
```

and, for anything an asset count cannot express, a function:

```python
@PLUGIN.readiness
def ready(host, capture_id) -> list[str]:      # [] = ready, else the reasons
```

The core answers `GET /api/captures/{cid}/readiness` and — this is the part that
matters — **`jobs.submit` refuses a job whose requirements are unmet, with the
reason as the error**. The greyed-out button in the UI is a courtesy; the server
refusing is the guarantee. A plugin cannot half-run because somebody found the
CLI.

`voxel_carve` ships declaring exactly this and nothing else that would work
without it. Its generator is not written (TODO §5); what is written is the gate,
because the gate is what the upload design has to prove.

### Explicitly not built

The Qt tools' "transformations" — `split_outside.py`, which cuts every track at
every `outside` tag on import. Asked for by name to be left out. A track's
`outside` keyframes are carried through unchanged; the mask editor's `split` is
how a human cuts a track, and it is an op.

---

## 2 · Camera clock offsets

### What is wrong today

The offset is a *command-line argument to a preprocessing script*. It is baked
twice — into `sync.npy` by `prepare.py --offset`, and into the pixels by
`make_grid.sh --offset`, which prepends black frames with `tpad` and re-encodes
the whole mosaic. Getting it wrong costs a 40-minute re-encode, and the two
copies can silently disagree, which puts masks on the wrong frame with nothing
on screen to say so.

### The design: the frame map becomes derived

The core stores, per stream, what it needs to *compute* the map:

```
streams.timestamps  BLOB    int64 µs, one per source frame, ascending
streams.duration    REAL
streams.time_offset REAL    seconds; +ve = this camera runs ahead
```

and the map is a function, not a fact:

```
frame_map[f] = bisect_right(timestamps, f / fps - offset) - 1        clamped
```

which is `prepare.py`'s rule, moved to where it can be re-run. `frame_map` stays
in the table as a cache so the hot query (`/api/layers/{id}/frames`) does not
change at all.

The offset itself is **an op**, `core.stream_offset {stream, seconds}`, folded
last-write-wins. That buys attribution, the History panel, and `Ctrl+Z`, and it
keeps the promise that no state exists which the log cannot rebuild. Applying
the fold rewrites `time_offset` and the cached maps.

Nothing is baked into pixels, because after §3 there are no baked pixels: the
viewport seeks each camera to `t − offset` in its own timeline. Correcting a
clock is now a slider, not a re-encode, and the masks move with the picture
because they are read through the same offset.

### Where the timestamps come from

`ffprobe -select_streams v:0 -show_entries packet=pts_time` — 0.2 s for a 140 MB
camera video, no decode, and no PyAV (the venv has none). This lives in
`quorum/probe.py` rather than in a plugin: the core already serves these files
by byte range, and a core feature that needed a plugin to be installed before it
worked would be a lie about where the boundary is.

Legacy streams imported by `grid_capture` have no timestamp table. They are not
guessed at: the offset control says "this stream has no timestamps — probe it"
and offers the button. Refusing out loud beats a plausible wrong answer.

---

## 3 · The mosaic

### What is wrong today

`make_grid.sh` runs `ffmpeg xstack` over six cameras into one 1920×720 H.264
file at CRF 30 — a 200 MB derivative that (a) throws away 89% of every camera's
pixels, (b) has the clock offsets baked in, (c) must be rebuilt from scratch to
change any of that, and (d) is a second copy of the recording that can drift
from the first. Quorum then treats that file as *the* video of the capture, so
`layout.mosaic` sits in the core and the viewport has one `<video>`.

### The design: the mosaic is a composition, not a file

A capture has cells; a cell shows a stream; the browser draws them side by side.
That is the whole idea, and it deletes `layout.mosaic` from the core's model.

```
layout = {kind: "grid", cols: 3, scene: {w, h},
          cells: [{stream: "cam1", x, y, w, h}, …]}
```

Each cell gets its own `<video>`, seeked to `t − offset` in that camera's own
timeline. Playback plays them all; one is the **master** (the largest visible
cell, or the legacy mosaic if there is one) and drives the capture frame, and
the others are held to it — small drift by nudging `playbackRate` a few percent,
large drift by seeking. Cameras that run at different frame rates stay in step
for the same reason they did in the ffmpeg version: they share wall-clock time,
and each one's own timestamps say which of its frames belongs to it.

Consequences, all of them the point:

- changing an offset is instant, and correct for masks and pixels at once;
- a cell is drawn from that camera's real resolution, which is §5;
- adding, removing or re-ordering a camera is a layout edit, not a re-encode;
- there is no second copy of the recording to go stale.

### What a browser can actually decode

These sources are **HEVC 1920×1080**. Chrome plays HEVC only where the platform
has a hardware decoder, and six 1080p decodes at once is a lot to ask even where
it does. This is the honest reason the ffmpeg mosaic existed, and pretending
otherwise would ship something that shows six black rectangles.

So a stream may have **renditions** — the same pixels at different sizes:

```
stream.media = {path, codec, playable, width, height, duration,
                renditions: {grid: {path, w:640, h:360},
                             full: {path, w:1920, h:1080}}}
```

`playable` is decided from the codec at probe time, not guessed in the browser.
A `media.prepare` job builds the missing renditions with ffmpeg — **per camera,
at full geometry, with timestamps passed through** (`-fps_mode passthrough`), so
this is a transcode of one video and not a mosaic: no offsets baked, no cropping,
no re-run when anything changes, and it can be skipped entirely for sources a
browser can already play.

That is the difference being asked for. ffmpeg is not the jank; **baking six
cameras and a clock correction into one lossy file is the jank.**

Legacy captures keep working: an existing `layout.mosaic` is treated as one more
rendition — a capture-level one that happens to cover every cell — and is used
when the whole scene is small on screen, which is exactly when its resolution is
enough.

---

## 4 · Colour modes and hiding classes

This is the one that says "make it mistake-proof, don't just handle the edge
case everywhere". So the design is not "add a filter"; it is **close the doors**.

### What is wrong today

There is already a notion of a hidden track (`masks` returns `hidden: true` from
a styler) and there is already a helper that respects it (`app.visibleObjects`).
The trouble is that respecting it is *optional*: `data.objects`, `data.activeAt`
and `app.primaryData()` all hand back everything, and every feature has to
remember to filter. Every place that forgot has been a bug — the `Shift`+`Space`
walk stopping on superseded tracks, group select picking eight invisible ones,
the exporter shipping unedited masks. Those were fixed one at a time. The next
one will be too, unless the shape of the API changes.

Worse, the walk asks the *server* for the next problem frame, and the server has
never heard of anything the client is hiding. Adding a class filter on the client
alone would reproduce the same bug on day one — which is precisely the example
in the ask.

### The design: one authority, four doors, one vocabulary

**One authority.** `web/core/display.js` owns everything about what is on screen
and what colour it is:

```js
display.visible(objectOrEntry) -> bool
display.paint(entry, base)     -> {color, alpha, label}
display.filter                 -> a serialisable filter (below)
```

It is built from contributions:

- `ctx.registerColorMode({id, title, colorOf(entry)})` — core ships `track` and
  `class`; `identity` moves its recolouring here, where it becomes a mode the
  user can pick in any tool instead of a side effect of which tab is open.
- `ctx.registerFilter({id, title, structural, test(entry) -> bool})` —
  `structural: true` means correctness, always on, not shown as a toggle (that
  is what `masks` uses for superseded and purged copies). `structural: false` is
  a user toggle and appears in the Display panel (that is `masks`' existing
  "show deleted", which stops being a private flag and becomes one of these).
- Classes come free: a class **is** `objects.label`, which is core data. Hiding a
  class is `{labels: {exclude: [...]}}`, colouring by class is `keyColor(label)`.
  Nothing about this is mask-specific, which is why it belongs in the core rather
  than in a masks-shaped feature.

**Four doors.** Every way an object reaches a human goes through the authority,
in core code that a plugin cannot bypass by forgetting:

1. **Drawing** — the viewport builds `view.entries`, already filtered. A renderer
   cannot draw a hidden track because it is never handed one.
2. **Hit testing** — `pickAll` filters what the renderers report, so a hidden
   track cannot be clicked, alt-cycled, or listed in the right-click menu.
3. **Selection** — `app.select()` drops ids that are not visible and says so
   once. This is the load-bearing one: *no tool can act on something the user
   cannot see*, whatever route it took to get the id.
4. **Navigation** — `app.reveal(ids, {frame})` is the only sanctioned
   jump-and-point, and it returns what it accepted and what it dropped, so a
   caller that was handed a hidden object has to notice.

`LayerData.activeAt` is renamed `activeAtRaw`, so the unfiltered set has a name
that reads like a warning at every call site.

**One vocabulary.** The filter is a small serialisable value, identical on both
sides of the wire:

```json
{"labels": {"exclude": ["forklift"]}, "layers": {"exclude": [7]}}
```

`quorum/filters.py` parses it and returns a SQL fragment; `display.js` applies
the same rules in JS. Any endpoint that *offers objects to a human* takes
`?filter=<json>` and honours it — today that is `identity.problems` and the
proposals queue. The client-side doors are the backstop for the ones that
forget; the parameter is what makes the walk skip a hidden class efficiently
instead of returning a hundred answers the client throws away.

Plugin-registered filters stay client-side and are not serialisable — but they
cannot leak either, because of the doors.

**The button.** A palette control in the top bar (mode + a class list with counts
and swatches) and the same thing, larger, as a core Display panel. Colour mode is
remembered per capture per user; a tool may *suggest* a mode (Identity does) and
a mode the user picked explicitly wins over any suggestion.

---

## 5 · Zoom shows the camera's own pixels

### What is wrong today

`Z` zooms the *mosaic cell* — 640×360 of a CRF 30 re-encode, blown up. The
detail you are zooming in to look at was destroyed by `make_grid.sh` before the
file existed.

### The design: one level-of-detail policy

After §3 a cell is a stream and a stream has renditions, so this is not a special
case for the `Z` key. It is one function, consulted by every path that changes
how big a cell is on screen — wheel zoom, `Z` hold, fit, window resize, layout
change:

```
needed  = cell.w * viewport.scale * devicePixelRatio       // real pixels across the cell
choose  = smallest rendition whose width >= needed, else the largest available
```

with hysteresis, so a cell on the threshold does not flap, and a **promotion
budget** (default 2 cells at full resolution at once) so six 1080p decoders are
never asked for.

Two elements per promoted cell — the current one keeps playing while the new one
loads and seeks, and they swap on `seeked`, so promotion has no black frame.

**When there is no playable full rendition** — which is true of every capture in
this repository until someone runs `media.prepare` — the cell does not fail. It
asks the server for one full-resolution still:

```
GET /api/captures/{cid}/frame/{stream}?frame=N&w=1920      -> JPEG, cached on disk
```

seeked by that stream's own timestamps and its offset, so the still is the same
frame the masks are drawn for. Paused-and-zoomed is when you actually want to
look closely, and it is exactly when a still is the right answer. Playing and
zoomed keeps the mosaic underneath and refreshes the still at a throttled rate.

So the ladder is: full rendition if there is one, still if there is not, mosaic
or grid rendition underneath either way. Every rung is better than what exists
and none of them is "nothing happens".

---

## What this does not do

Stated so the next session does not have to find out by reading the code:

- **The voxel carve is not implemented.** `voxel_carve` ships as the requirement
  gate the upload design needed to prove, and refuses to run. The carve itself is
  `calib_ui/carve.py` plus a C kernel and stays TODO §5.
- **`media.prepare` needs ffmpeg** and takes minutes per camera. It is a job with
  progress and cancellation; it is not run for you.
- **No login page.** Unchanged from HANDOFF: token auth exists, the UI for it
  does not.
- **Uploads are not virus-scanned, quota'd or rate-limited.** One lab server, one
  trusted set of operators — same posture as plugin loading.

## Invariants added

Because the repo's rule is "add an invariant, not just a test":

- nothing hidden is drawn, picked, selected or revealed — asserted over every
  registered tool, with a class hidden at random;
- every stream's cached `frame_map` equals the map recomputed from its
  timestamps and offset;
- the layout's cells cover every enabled stream exactly once and nothing else;
- a job whose requirements are unmet is refused by the server, not merely greyed
  out in the UI.
