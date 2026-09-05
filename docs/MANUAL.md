# Quorum — the user's manual

Everything this server can do, where to find it, and what to press.

This is the *using* document. `README.md` says what Quorum is and why it is
built the way it is; `docs/PLUGINS.md` says how to write a plugin;
`docs/HANDOFF.md` says what is decided and what is not. This one assumes you
have a browser open and something to annotate.

---

## Contents

1. [What you are looking at](#1-what-you-are-looking-at)
2. [Starting the server](#2-starting-the-server)
3. [The shell: where everything lives](#3-the-shell-where-everything-lives)
4. [Getting a capture in — four doors](#4-getting-a-capture-in--four-doors)
5. [The Capture workspace (⚙)](#5-the-capture-workspace-)
6. [Watching: the viewport, playback and the timeline](#6-watching-the-viewport-playback-and-the-timeline)
7. [Display: colour, classes, filters](#7-display-colour-classes-filters)
8. [Layers, History, Jobs and Data](#8-layers-history-jobs-and-data)
9. [The Edit workspace (✎) — mask editing](#9-the-edit-workspace---mask-editing)
10. [The Identity workspace (◈)](#10-the-identity-workspace-)
11. [The Review workspace (✓) — proposals](#11-the-review-workspace---proposals)
12. [The SAM workspace (✦)](#12-the-sam-workspace-)
13. [The Carve workspace (⬚)](#13-the-carve-workspace-)
14. [Importing and exporting](#14-importing-and-exporting)
15. [Every job, and what its parameters mean](#15-every-job-and-what-its-parameters-mean)
16. [Working with other people](#16-working-with-other-people)
17. [Keyboard reference](#17-keyboard-reference)
18. [The command line](#18-the-command-line)
19. [Configuration](#19-configuration)
20. [Running in a container, and on the GPU box](#20-running-in-a-container-and-on-the-gpu-box)
21. [Where files end up](#21-where-files-end-up)
22. [When something looks wrong](#22-when-something-looks-wrong)
23. [What is not built yet](#23-what-is-not-built-yet)

---

## 1. What you are looking at

Quorum is a multi-view annotation server. The unit of work is a **capture**: N
video **streams** (usually six cameras) sharing one time base. Everything you
do to a capture is an **op** in an append-only log — attributed to you, visible
in the History panel, and undoable.

Four words are worth learning before anything else, because every panel uses
them:

| word | means |
|---|---|
| **capture** | the session: six cameras, one timeline, `n_frames` at `fps`. |
| **stream** | one camera. Has its own frame count, its own frame timestamps, and a clock offset. |
| **layer** | a set of annotations of one kind (e.g. RLE masks). A capture can have several. |
| **object / track** | one thing in one stream, with a `key`. A person in six cameras is six tracks — joining them into one person is what the Identity workspace is for. |

The mosaic you see is **composed in your browser** from the individual cameras.
There is no mosaic file. That is why zooming into a cell shows that camera's
own pixels, and why correcting a camera's clock is a slider rather than a
re-encode.

---

## 2. Starting the server

### On your own machine

```bash
cd quorum
python3 -m venv --system-site-packages .venv
.venv/bin/pip install fastapi "uvicorn[standard]"      # numpy comes from the system
./run.sh                                               # → http://127.0.0.1:8600
```

`./run.sh` passes any extra arguments through, so `./run.sh --port 8611` works.

For day-to-day development there is `./dev.sh`, which keeps a pidfile instead of
killing things by name:

```bash
./dev.sh start        # start in the background, wait for /health
./dev.sh stop
./dev.sh restart
./dev.sh log 80       # last 80 lines of data/dev.log
```

Open **http://127.0.0.1:8600**. You land on the lobby.

### ffmpeg

Quorum reads frame timestamps and cuts still frames with `ffmpeg`/`ffprobe`. If
they are not on `PATH`, capture building, video preparation and still frames all
refuse with that reason. Everything else still works.

### Logging in

With the default `auth_mode = "open"` there is no login: you are `local` and you
are an admin. On a server configured with `auth_mode = "token"` the first
request that needs a token pops a dialog asking for one — paste the token an
admin gave you and the page reloads. It is kept in that browser's
`localStorage`, so you do it once per browser.

An admin makes tokens with:

```bash
.venv/bin/python -m quorum user alice --role annotator
# or, in the container:
docker compose exec quorum python -m quorum user alice --role annotator
```

Roles are `annotator` < `reviewer` < `admin`. Today only deleting a whole
capture and listing users require `admin`; everything else is open to any
signed-in role.

---

## 3. The shell: where everything lives

```
┌──────────────────────────────────────────────────────────────┐
│ topbar   Quorum · ← captures · name · workspace · presence · ◑ ⌘K ◐ │
├───┬────────────────────────────────────────┬─────────────────┤
│ r │                                        │                 │
│ a │            the viewport                │   inspector     │
│ i │      (the mosaic + the overlay)        │  (panels)       │
│ l │                                        │                 │
├───┴────────────────────────────────────────┴─────────────────┤
│ transport   ◀ ▶ ▶  00:04:12 / 10:00  ──────●──────  speed lanes │
│ lanes       per-stream activity, one row per camera           │
└──────────────────────────────────────────────────────────────┘
```

**Top bar.** Brand (click to go back to the lobby), `← captures`, the capture's
name, a crumb naming the workspace you are in, the stream/frame count, then on
the right: the link badge, other people's presence chips, the count of running
jobs, the **Display** button (`◑`), the command palette button (`⌘ K`), the
light/dark toggle (`◐`), and your user name.

**Rail** (left). One icon per workspace. `⚙` Capture is always there; the rest
come from plugins: `✎` Edit, `◈` Identity, `✦` SAM, `✓` Review, `⬚` Carve. A
workspace whose plugin says it cannot work here is greyed with a `!` badge —
hover it for the reason, click it to be taken to Capture settings where you can
fix it. Some icons carry a count badge (Review shows how many proposals are
open).

**Inspector** (right). A stack of collapsible panels. The top ones belong to the
current workspace; below them are the five panels present in every workspace:
**Display**, **Layers**, **History**, **Data**, **Jobs**. Click a panel's title
to collapse it. `Ctrl+B` hides the whole inspector.

**Transport** (bottom). Step/play buttons, the frame counter and timecode, the
scrubber, a speed selector (0.25× … 8×), and a `lanes` button that shows or
hides the per-stream activity lanes.

**Overlays.** Toasts appear bottom-right and explain every refusal — if a key
appears to do nothing, the toast says why. Modals dim the screen; `Esc` cancels,
`Enter` accepts.

**Theme.** `◐` in the top bar toggles light/dark; the choice is remembered per
browser.

**URLs.** `#/` is the lobby, `#/c/12` opens capture 12, `#/c/12/identity` opens
it on a workspace. The back button works, and links are shareable.

---

## 4. Getting a capture in — four doors

All four end in the same place: a capture with streams, frame timestamps and a
frame map per stream.

### Door 1 — Files on your machine (the usual one)

1. Lobby → **New capture**.
2. Give it a name. Pick who builds it — **Build from uploaded videos**
   (`multiview`) is the default and is what you want.
3. Press **Create**. You get a *draft*: an empty capture that reads nothing yet.
4. On the draft screen press **Upload files…** (the same as the Data panel's
   **Upload…**). Drop the six camera videos in, choose the kind **View video**,
   press Upload. Uploads are chunked and resumable — a dropped connection at
   140 MB continues from 140 MB, and each file has its own progress bar and
   `stop` button.
5. Press **Build**.

Building probes each video for its dimensions, codec, duration and *every
frame's timestamp*, picks a capture frame rate, computes a frame map per stream
and lays the cameras out in a grid. It is idempotent: upload another camera,
press **Rebuild**, and you get the same capture updated rather than a second one.

The stream key comes from the file name (`cam3_20260612.mp4` → `cam3`). Two
files that would produce the same key are refused by name rather than silently
overwriting each other.

### Door 2 — Files already on the server

This is the door that matters on the deployment with the recordings share.

Lobby → under **Other ways in**, press **Import from the server**. A remote file
browser opens, showing only what is inside the server's configured media roots.

- The **filter box is the primary control** and it filters *on the server*.
  `records` is one flat directory of ~1,400 files; typing a stamp like
  `20260612_021454` narrows it to that session's six cameras.
- **Select all** then takes them in one click. Picked files show as chips you
  can click to remove.
- Directories are navigable; `..` goes up; the crumb line shows where you are.
- A file the server cannot actually read is shown as `unreadable` and cannot be
  picked — that is a mount or permissions problem, not a Quorum one.

Press **Use these files**, fill in the same options as door 1 (name, fps,
columns, cell width, duration), and press Run. **Nothing is uploaded and nothing
is copied**: the stream's media path is the file on the share. The capture is
keyed by the stamp the filenames share, so re-importing the session updates it.

### Door 3 — A prepared directory (`grid_capture`)

For captures `sync_id_ui/prepare.py` already prepared. Lobby → **Import a
prepared capture**, give the stamp. It reads `sync_id_ui/data/<stamp>/` and the
videos in `raw_vids/<stamp>/`, and reuses that script's pts-exact `sync.npy`
rather than recomputing it. **Scan for captures** lists what is on disk without
importing anything.

### Door 4 — The command line

```bash
.venv/bin/python -m quorum ingest grid_capture.prepared stamp=20260612_021454
.venv/bin/python -m quorum ingest multiview.disk paths='["/records/cam1_x.mp4"]'
```

Same providers, same code, progress printed to the terminal.

### Making the video playable

If a source is in a codec your browser cannot decode (these captures are HEVC),
the cells stay blank and each stream row says `needs a rendition`. Fix it once
per capture:

**Capture workspace → Streams panel → Prepare video…**

- **Renditions**: `grid` (small, for the whole mosaic) and/or `full` (the
  camera's own resolution, for zooming in). The default `grid,full` is right.
- **Rebuild existing**: only if you want to redo work already done.

One transcode per camera, at full geometry, timestamps passed through. Nothing
is stacked into a mosaic and no clock offset is baked into anything. Minutes per
camera; it is a job, so it has progress and a Stop button.

Quorum then *proves* each rendition is frame-aligned with its source and
**refuses to use one that is not** — an unaligned rendition would put every mask
one frame out, which looks fine and is wrong. If that happens the job says so
and the cell falls back to full-resolution stills.

### Deleting a capture

Lobby → the `×` on a capture's row. It asks for confirmation and takes the
capture, its streams, its annotations and its uploaded files. Files that were
*linked* from the server are not touched. Requires the `admin` role.

---

## 5. The Capture workspace (⚙)

Reached from the rail's `⚙`, `Ctrl+,`, or the **⚙ settings** button on a lobby
row. Everything you can change about a capture is here, and it sits beside the
viewport on purpose — aligning a clock is something you do *while watching the
cameras*.

### Capture panel

- **Name** — free text.
- **Frame rate** — the capture's own timeline. Changing it does not move any
  pixels: every stream is re-read at the new rate through its own timestamps, so
  all the frame maps are rebuilt.
- **Save** applies both.
- **Build / Rebuild** re-runs this capture's provider over its files and
  settings. Safe and idempotent.

### Streams panel

One block per camera:

- A colour swatch, the stream key, its resolution.
- **shown / hidden** — take a camera out of the mosaic without deleting
  anything. The grid re-lays out.
- **clock** — the offset row. `−1 −0.1 [ 0.00 ] +0.1 +1`, a number box, and
  `reset`. `+` **delays** a camera whose clock runs ahead of the others (the
  same sign convention `prepare.py --offset` used). Nudge it and watch the same
  event line up across two cells. The frame map is recomputed from that camera's
  own timestamps, so the masks move with the picture. It is an op: it appears in
  History and `Ctrl+Z` takes it back.
- A status line: codec, `browser-playable` or `needs a rendition`, which
  renditions exist, and whether timestamps have been read.
- **probe** appears when a stream has no timestamps yet (~0.2 s).
- **Prepare video…** at the bottom — see above.

### Data panel

Files belonging to this capture. Each row shows the name, size, a **kind**
dropdown, tags and a delete button.

- **Upload…** opens the upload dialog. The list of *kinds* comes from the
  plugins installed on this server — View video, CVAT track XML, Camera
  calibration, and whatever else is installed. Drag files in or click to choose;
  the dialog tells you what each kind is for and what extensions it expects.
- **From the server…** registers a file that is already on the server as an
  asset, **without copying a byte**. Same picker as door 2. A linked file's row
  says `linked`, or `missing` in red if the share is unmounted. Deleting it
  removes the link and never the file.
- **The kind dropdown is editable after the fact.** File something as the wrong
  kind and you can re-file it; you never upload twice. A kind no installed
  plugin claims shows a `?` tag.
- Uploads in flight appear here with progress, a `stop` button, and `clear
  finished`.

Deleting an uploaded file asks first and says exactly what it will do —
different text for uploaded files (removed from the server) and linked ones
(only the link).

### Not ready panel

Appears only when some plugin says it cannot work here. It names the plugin, the
reason, and — when the missing thing is a file — gives you the upload button for
exactly that kind. This is the panel behind a greyed rail icon.

The refusal is enforced on the server: a blocked job is refused *before the
handler runs*, with the reason, including from the CLI. The grey button is a
courtesy.

### Layers panel (collapsed by default)

One row per layer: name, object count, a **type** dropdown, delete. Changing a
layer's type re-labels what is in it — nothing is converted, because the core
never opened the payloads; it says who should draw them. Deleting a layer takes
its objects and shapes; the ops that named them stay in the log.

---

## 6. Watching: the viewport, playback and the timeline

### Moving around

| gesture | does |
|---|---|
| wheel | zoom about the cursor |
| `Ctrl` + wheel | zoom faster |
| drag with the left button | pan |
| middle-drag, or `Alt`+drag | pan (`Alt`+*click* without moving is still a click) |
| double-click | fit the whole scene |
| `F` | fit the whole scene |
| hold `Z` | zoom to the camera under the cursor; release to go back exactly where you were |

### The HUD

The line at the corner of the stage says, live: zoom percentage, which camera
you are over, that camera's own source frame number for this capture frame,
**which copy of the pixels you are looking at** (`grid`, `full res`, `still ·
full res`, `mosaic`), what is under the cursor, and — when masks overlap —
`N overlapping — alt-click to go deeper`.

"Why is this blurry" should never need a guess.

### Level of detail

You do not manage this; it is worth knowing it exists. Each cell shows the
smallest rendition that is not smaller than the cell is on screen. Zoom in far
enough and the cell promotes itself to `full`; zoom in past that *while paused*
and it fetches a full-resolution still frame from the server. Stills are never
used while playing (they cannot keep up) and never for more than two cells at
once (each one is an ffmpeg seek on the server).

### Playback

| key | does |
|---|---|
| `Space` | play / pause — in every workspace, always |
| `→` / `←` | step one frame (hold to repeat) |
| `Shift+→` / `Shift+←` | jump one second |

The scrubber and the speed selector are in the transport bar. Playback is driven
by the master video's own frame-presentation callback where the browser has one,
so the overlay is drawn for the picture actually on screen.

### The lanes

Under the transport, one row per camera: how many objects are drawn at each
point in the capture, so you can see where there is anything to look at without
scrubbing. The selection is highlighted in its lane. Click or drag anywhere in
the lanes to scrub there. The `lanes` button hides them.

Workspaces decorate their own lanes: Identity draws a coloured bar per assigned
track, Edit draws a tick at every cut.

---

## 7. Display: colour, classes, filters

One control, present in **every** workspace, because "how are these coloured"
and "which of these am I looking at" are not a tool's business.

Two ways in: the **`◑` button in the top bar** (a quick menu) and the **Display
panel** in the inspector (the full thing).

### Colour modes

`K` cycles them; the panel and the menu list them.

- **by track** — a stable colour per `stream/key`.
- **by class** — by the object's label.
- **by layer**.
- **by identity** — from the Identity plugin: one colour per person, grey for
  unassigned, and it works in every workspace, not just Identity's.

Opening the Identity workspace *suggests* its mode; a mode you picked yourself
always wins.

### Hiding a class

The panel lists every class in the capture with a swatch and a count. Click one
to hide it. **A hidden class is hidden everywhere**: it is not drawn, cannot be
clicked, cannot be selected, is not exported by the default export, and the
Identity walk and the proposal queue step over it. That is enforced in the core,
so a plugin cannot accidentally hand you something you hid.

`Shift+H` shows every hidden class again; the panel has a `show all (N hidden)`
button, and the top-bar button says `N hidden` while any are.

If a click seems to select nothing, a toast says `N hidden tracks skipped` and
tells you where to look.

### Filters

Chips under `show`. Plugins register these. Today: **deleted tracks** (Edit's
`H` toggle, showing them tagged `DEL` in dead red). Structural filters —
superseded copies of a cut track, purged tracks — are always on and deliberately
have no toggle: turning them off would put two copies of the same track on
screen.

Your colour mode, hidden classes and filter choices are remembered per capture,
in that browser.

---

## 8. Layers, History, Jobs and Data

These four panels are in every workspace.

### Layers

One row per layer: a swatch, the name, the type, and a provenance tag —
`human`, `model` or `derived`. **Click a row to hide or show that layer.**

### History

The op log, newest first, with who did what. Each row has an **undo** button;
an already-undone row is dimmed and offers **redo**. `↻` re-reads it.

`Ctrl+Z` undoes *your* last op. It never inverts anything: the op is marked
undone and everything that derives state from the log gets the answer it would
have got had you not done it. That is why two people editing one capture is
ordinary rather than frightening.

The Edit workspace has its own **Undo edit** button, which undoes your last
`masks.*` op specifically.

### Jobs

Ingest, imports, exports, transcodes and generators all run through one queue.
Each shows plugin, kind, state, a progress bar, its latest message and a **stop**
button while it is queued or running. A count of running jobs also appears in
the top bar. A failed job raises a toast with the reason.

Long work is *always* a job — a request never blocks on it, and closing the tab
does not stop it.

### Data

- **Importers** (plain buttons) and **exporters** (accented buttons), collected
  from every installed plugin. Pressing one opens a dialog built from that
  importer's declared parameters — including pickers for uploaded files of the
  right kind and for files on the server.
- **Exported files** — what is in `data/exports/`, with size, a `↓` download
  button (a directory of per-stream files comes down as one zip) and a delete
  button that removes only the exported copy.

When an export finishes, the toast itself carries the download button.

---

## 9. The Edit workspace (✎) — mask editing

Rail `✎`. Fixing what the model drew: hide, cut, merge, repaint. Every edit is
an op.

**Editing is per-track by default** — the opposite of Identity. A click selects
exactly one track, because a keystroke that deletes a track in six cameras at
once is how you lose an afternoon.

### Selecting

- **Click** a mask to select it. `Shift`+click toggles a second one.
- **`Alt`+click** reaches the mask *underneath* the one on top, and clicking the
  same spot again goes one deeper.
- **Right-click** lists everything under the cursor, with each track's state
  (`deleted`, `purged`, `joined`, `segment`), plus `— cut here` and
  `— delete this track` at the bottom. Hovering a row rings that mask in the
  viewport.
- `Esc` clears the selection.

### The operations

| key | button | does |
|---|---|---|
| `D` | Delete | hide the selected tracks. Kept in the log; `H` shows them again. |
| `Shift+D` | Restore | undo a delete. |
| `P` | Purge | take tracks out of the working set entirely — not drawn, not hit-tested, not exported. Asks first, because nothing on screen will remind you they existed. Still reversible from History. |
| `T` | Cut here | split the selected track at the frame you are on. `86` becomes `86` and `86@2470`; the new half is selected and pulsed. |
| `J` | Join | make the selected tracks travel as one. They become `1+2+3`. |
| `U` | Unjoin | take a joined track apart again. |
| `H` | show deleted | toggle whether deleted tracks are drawn (tagged `DEL`). Same state as the Display panel's chip. |

All of them work within **one camera at a time**; selecting across two cameras
is refused with that reason. A cut lands on the frame you are on, and refuses if
the track does not run there — the toast names the range it does run.

When the server refuses a structural edit it says *why* ("3 is joined to 1+2+3,
unjoin it first"), and that reason is shown to you rather than swallowed.

The panel shows running counts of deleted / purged / cuts / joins, and the
selected track's stream, key, frame range and state.

### Replay panel (collapsed)

For captures that came from the desktop tool:

- **Replay edits from consistent_ids.json** — that tool kept its deletions,
  cuts and joins in the same file as the identities. Replaying them here makes
  identity keys like `86@2470` and `1+2+3` point at real tracks.
- **Count dangling identity keys** — how many identity assignments point at a
  track that does not exist.

---

## 10. The Identity workspace (◈)

Rail `◈`. The only entity that spans cameras: give the same id to the tracks
that are the same person in different views.

**Here a click means "this person"** — it selects the whole identity across
every camera. That is almost always what you mean, and both the scope switch and
every destructive button say how wide the blast radius is.

### Selecting

- **Click** — the whole identity (or just the track, if you set the scope that
  way).
- **`Ctrl`+click** — always exactly one track, whatever the scope.
- **`Alt`+click** — one track, and reach the mask underneath; a toast names what
  you got and its depth in the stack.
- **Right-click** — every mask under the cursor with its identity, plus
  `— select whole identity` and `— clear identity from this track`.
- `Q` toggles the scope between **whole identity** and **one track**; the chips
  in the panel do the same and it is remembered.

### The operations

| key | button | does |
|---|---|---|
| `M` | Link | give every selected track one identity. |
| `A` | Assign… | type an identity number. `200` means "outside". |
| `O` | Outside | flag the selection as outside the area of interest. |
| `C` | Clear | remove the identity from the selection. |

Anything destructive that reaches more than one track asks first, and the dialog
names the scope: *"3 tracks · 2 cameras · id 7"*. The panel shows the same line
for the current selection.

### Walking the work

`N` or `Shift+Space` — **next frame that needs a human**. That is a track with
no identity, or one identity on two masks in the same camera. It pauses, jumps
to that frame, selects the offender, rings it and says which kind of problem it
is. `Shift+P` walks backwards.

The walk respects what you are hiding: the server is told, so it steps over a
hidden class rather than offering it. The panel says how many frames still need
a human.

### The panel

Counts of tracks / with an identity / identities / flagged outside, a progress
bar, the scope switch, the four buttons, the walk buttons, and:

- **Suggest links…** — runs the `link_gaps` generator over this capture and puts
  its opinions in the **Review** queue. It never assigns anything.
- **Identities (N)** — one row per identity with its colour and track count.
  Click a row to select that whole identity.

Identity's colours are a registered colour mode, so you can use them in the Edit
or SAM workspace too, and refuse them here. While this workspace is open,
hovering a mask emphasises every other member of that identity across all six
cameras, and the lanes show a coloured bar per assigned track.

---

## 11. The Review workspace (✓) — proposals

Rail `✓`, with a badge showing how many are open. A queue of machine-generated
suggestions, one at a time, with the frame already on screen and the tracks
already selected — so the decision is made by looking at the video, not by
reading a confidence number.

Generators write here. **Nothing else may write an annotation on a machine's
say-so.** Accepting a proposal appends *that* op, attributed to the person who
accepted it.

| key | does |
|---|---|
| `Enter` | Accept |
| `Backspace` | Reject |
| `↓` / `↑` | next / previous proposal |
| `G` | show this one in the viewport again |

(`Space` is deliberately not bound here: play/pause means the same thing in
every workspace.)

The card shows the source, a confidence bar, a note, the supporting evidence
(gap in frames, drift, how many cameras agree — whatever the generator
provided), and Accept / Reject / navigation buttons. Tabs across the top filter
`open / accepted / rejected / stale / all` with counts, and **Up next** lists
the queue — click any row to jump to it.

**Stale** is its own state: a suggestion goes stale when an edit removes a track
it named. It is kept rather than deleted, so the generator can still be scored
on it, and the card says why.

If proposals name classes you are hiding, the panel says `N suggestion(s) held
back` — they are not lost; show the class and they come back.

---

## 12. The SAM workspace (✦)

Rail `✦`. Three things, deliberately kept apart because they cost wildly
different amounts and only one of them is free to get wrong.

Requires the SAM service to be up (see §20). The panel says `endpoint up` or
`endpoint down` with the reason, and **recheck** asks again — useful in the
minute after somebody redeploys the function.

### Prompt — click a person

- **Left-click** adds a positive point (on the thing).
- **Right-click**, or `Shift`+click, adds a negative point (on what to leave
  out).
- **Click a point again to remove that particular point.**
- `Backspace` takes the most recent point back.
- A preview mask appears in blue after each click. **Nothing is written.**
- `Esc` throws the prompt away, at no cost.
- One camera at a time: a prompt lives in one cell, and clicking in another says
  so rather than mixing them.
- Stepping to another frame **drops the prompt**, out loud — a mask you never
  looked at on the picture it lands on is the wrong kind of wrong.

`Enter` commits. What it writes depends on the selection:

- **With a track selected** (`Alt`+click a mask) it repaints *that* track's mask
  at this frame.
- **With nothing selected** it creates a new track, with the label from the
  panel's text box (`person` by default).

Either way it is written **through `masks`, as you, on one track**: it is in
History, `Ctrl+Z` takes it back, and it is the same kind of edit as a cut or a
join.

Note the one difference from every other workspace: **a plain click here is a
prompt point, not a selection.** Selecting is `Alt`+click, which means the same
thing it means everywhere else. That is deliberate — the selection is the track
your commit will land on, and it has to survive the four clicks it takes to get
a good mask.

Two toggles:

- **crop to object** (`C`) — crops the frame to a box around your prompt before
  sending it. SAM rescales its input to a fixed square, so a person 130 px wide
  in a 1920×1080 frame otherwise arrives about 70 px tall. On by default; turn
  it off for something large.
- **use track box** — seeds the model with the selected track's existing box.

The panel shows the endpoint state, the round trip in ms (frame fetch + model),
the preview size, the target track, and the crop size.

### Propagate — carry it forward

With a mask selected:

| key | does |
|---|---|
| `R` | carry it forward across the next frames of its own camera |
| `Shift+R` | the same, backwards |

**Frames** (default 60) and **Every Nth** (stride; the mask is interpolated
between keyframes, so 2 or 3 costs a third of the GPU on slow movement) are in
the panel.

This is a job — a minute of GPU — so it has progress and a Stop button. The
**whole propagation is one op**: you made one decision, and one `Ctrl+Z` takes
all sixty keyframes back. The seed frame's own mask is kept exactly as you
accepted it. If SAM loses the track partway the result says on how many frames.

### Auto-annotate — everything, from text

**Run the auto-annotator…** opens a dialog. Set text prompts (one per line or
comma separated — use singular nouns), optional labels, which streams, and the
tuning parameters (see §15). Press Start.

It runs SAM 3.1 over whole cameras, one camera at a time, for hours. Progress is
per window in this panel and in Jobs; **Stop** cancels at the next window. A run
that finishes lands as its own `provenance="model"` layer with a label per
prompt, and touches nothing you edited.

The panel also shows:

- **VRAM** — how much of the card is in use and how much is free. A run needs
  about 10 GB on top of the model, and shares the card with the interactor.
- **busy** when the card is working. One thing at a time: a click during an auto
  run waits for the current window, which is seconds.
- **Produced** — the XML files each run left, with `partial` marked on a run
  that exited nonzero.
- **Discontinuities** — per camera: seconds lost, holes, stalls, and how many
  segments the camera fragmented into. These recorders drop up to half their
  wall clock and **no track identity is ever carried across a hole**, so a
  camera that fragments into 136 pieces can say why.

---

## 13. The Carve workspace (⬚)

Rail `⬚`. Cross-camera identity proposals from silhouette intersection.

**This is the worked example of a plugin that declares what it cannot work
without, and the carve itself is not ported.** What is here:

- The rail icon is greyed with a `!` until this capture has a **Camera
  calibration** asset — `camera_pics` writes one as
  `calibration_result.json`. The Not ready panel gives you the upload button.
- When one lands it is **validated automatically**: does it parse, are the
  rotation matrices real rotations, and does it name *these* cameras? A
  calibration from another room satisfies a file count and nothing else, so the
  gate refuses it by name and says which cameras are missing.
- With a valid calibration the workspace opens and **Propose cross-camera
  links** can be submitted — and then stops with a message saying the carve is
  not ported (`calib_ui/carve.py` plus a C kernel). It deliberately does not
  return an empty proposal queue, because an empty queue reads as "I looked and
  found nothing", which would be a claim it has not earned.

---

## 14. Importing and exporting

All of it is in the **Data panel**, in every workspace. Everything runs as a job.

### Importers

| button | plugin | brings in |
|---|---|---|
| **Import CVAT XML** | `cvat_xml` | one exported XML per stream. Match by filename; RLE strings are carried verbatim, so the round trip is lossless. |
| **Import tracks.pkl** | `grid_capture` | the pickle `sync_id_ui/prepare.py` writes. |
| **Import consistent_ids.json** | `identity` | hand-made identities, as ops — so History starts where the desktop tool left off rather than at zero. |
| **Import edits from consistent_ids.json** | `masks` | the deletions, cuts and joins that were in the same file. |

Each dialog offers, for the file it needs, both an **uploaded-file picker** and
a path box / server browser. The importer never learns which door the file came
through.

### Exporters

| button | plugin | writes |
|---|---|---|
| **Export CVAT XML** | `cvat_xml` | one XML per stream, keyed to that stream's own frames. |
| **Export consistent_ids.json** | `identity` | `consistent_ids_<stamp>.json` in Quorum's own export directory — it never overwrites a file the desktop tools own. |

**Leave the CVAT exporter's layer id at 0.** That exports *what is on screen*:
every mask layer, minus tracks that were replaced by a cut or a join, deleted,
or purged. Naming one layer explicitly exports that layer's rows — which, for
the imported layer, means shipping the unedited masks.

Exports land on the server and are listed in the Data panel with a `↓` button.
A directory of per-stream files comes down as one zip.

---

## 15. Every job, and what its parameters mean

Reachable from the buttons named above, from the Jobs API, or from the CLI with
`python -m quorum job <plugin>.<kind> key=value …`.

### `multiview` — captures from videos

- **`provider:videos`** — build from this capture's uploaded videos.
  `fps` (0 = the fastest view's rate, so no view is sampled below its own),
  `cols` (0 = square-ish), `cell_w` (0 = the widest view), `duration`
  (0 = the longest view).
- **`provider:disk`** — the same build from `paths` already on the server, plus
  `name` (blank takes the timestamp the filenames share) and `replace`.
- **`layout`** — re-lay out the grid. `cols`, `cell_w`. Instant: it is a layout,
  not a video.
- **`probe_asset`** — read an uploaded video's dimensions, codec, duration and
  every frame's timestamp.

### `media` — renditions

- **`prepare`** — `which` (`grid`, `full`), `streams` (blank = all), `force`.
- **`drop`** — delete prepared renditions to free disk. The sources are
  untouched; rebuild any time.

### `grid_capture`

- **`provider:scan`** — list prepared captures on disk without importing.
- **`provider:prepared`** — `stamp`, `replace`.
- **`import:tracks`** — `layer`, `label_filter`, `max_frame` (0 = all).

### `cvat_xml`

- **`import:tracks`** — `assets` (uploaded XMLs) or `dir`; `layer`, `name`,
  `type` (default `mask.rle`), `provenance` (`human`/`model`/`derived`),
  `max_frame`.
- **`export:tracks`** — `layer_id` (**0 = the effective set**; see §14).

### `mask_layer`

- **`stats`** — per-stream track and keyframe counts, and mean mask area.

### `masks`

- **`import:consistent_ids`** — replay the desktop tool's edits.
- **`materialise`** — replay the edit log onto the imported masks. Safe to run
  any time; normally automatic.
- **`orphans`** — which identity assignments point at a track that does not
  exist.

### `identity`

- **`import:consistent_ids`** — `path` (blank looks beside `sync_id_ui/` for
  this capture's stamp).
- **`export:consistent_ids`**.
- **`link_gaps`** — the generator behind **Suggest links…**.
  `layer_id`, `max_gap` (largest gap to bridge, in source frames; default 60),
  `max_move` (allowed drift in box widths; default 1.2). Within one camera: a
  track that ends where another begins, a moment later, is usually the same
  person. **Emits proposals — it never assigns anything.**

### `sam`

- **`track`** — propagation. `stream`, `key`, `frame` (seed, in that stream's own
  numbering), `count` (60), `stride` (1), `backward`, `roi` (crop first).
- **`auto`** — the auto-annotator:
  - `prompts` — one per line or comma separated. Singular nouns: text grounding
    keys on the noun.
  - `labels` — one per prompt; blank reuses the prompt text. Use these when the
    phrase that segments best is not the name you want ("person sitting down" →
    `customer`).
  - `streams` — blank = all.
  - `frame_stride` (5) — process every Nth frame.
  - `chunk_frames` (64) — window size.
  - `overlap_frames` (4) — shared frames identities are matched on.
  - `max_resolution` (720) — tuned for people. Hats and phones are far smaller
    in frame; **raise this first if recall is poor**, at the cost of VRAM.
  - `fill_hole_area` (64).
  - `freeze_gap_ms` (400) — the fidelity dial. Call a timestamp hole a
    discontinuity after this long. Higher gives fewer, longer, less trustworthy
    tracks; no identity is ever carried across a hole. 0 disables the check.
  - `freeze_min_frames` (10) — repeated frames that count as a stall.
  - `layer` / `layer_name` — where the result lands.
  - `import_result` (on), `replace_layer` (on), `force` (redo streams that
    already have an XML).

### `voxel_carve`

- **`validate`** — does this calibration parse, and does it cover this capture's
  cameras? Run for you automatically when one is uploaded.
- **`suggest`** — gated by the calibration requirement; not ported (§13).

---

## 16. Working with other people

Everything is live. Open the same capture in two browsers and:

- **Presence.** Each person appears as a coloured chip in the top bar; hovering
  one shows the frame they are on.
- **Edits broadcast.** An op somebody else appends arrives in your History,
  their mask edit rebuilds your view, their identity assignment recolours your
  masks.
- **Undo is per person.** `Ctrl+Z` takes back *your* last op, not the last op.
- **Proposals are shared.** Accepting one removes it from everyone's queue.

### The link badge

Silent while the connection is healthy and quick, because a permanent "online"
badge is furniture — the whole value is that it appears.

- `reconnecting…` (amber) — the connection dropped; retrying with backoff.
- `out of date` (red) — it came back but the missed edits could not be fetched.
  Reload.
- `NNN ms` — the round trip, shown only past 250 ms.

A reconnect re-fetches the ops you missed and replays them, *and* re-reads what
is derived rather than appended — because an undo during the gap flips a flag on
an op already in the log, and no message would ever mention it. You get a
`Caught up — N edit(s) arrived while you were offline` toast.

Everything here exists because the GPU box is reached over `ssh -L`, where a
dead tunnel keeps accepting connections and the page quietly stops being true.

---

## 17. Keyboard reference

`Ctrl+K` opens the **command palette**, which lists every command available
right now — global ones and the current workspace's — with its keys, and runs
whichever you pick. When you cannot remember a key, that is the answer.

Bindings belong to whoever registered them. A workspace's bindings only fire
while that workspace is active, which is why `C` means "clear identity" in one
and "crop toggle" in another.

### Always

| key | does |
|---|---|
| `Space` | play / pause |
| `→` `←` | step one frame |
| `Shift+→` `Shift+←` | jump one second |
| `F` | fit to window |
| hold `Z` | zoom the hovered camera; release to return |
| `K` | next colour mode |
| `Shift+H` | show every hidden class |
| `L` | mask labels on / off |
| `Esc` | clear the selection |
| `Ctrl+Z` | undo my last edit |
| `Ctrl+K` | command palette |
| `Ctrl+B` | show / hide the inspector |
| `Ctrl+,` | Capture settings |

### Edit (✎)

| key | does |
|---|---|
| `D` / `Shift+D` | delete / restore |
| `P` | purge (asks first) |
| `T` | cut at this frame |
| `J` / `U` | join / unjoin |
| `H` | show or hide deleted tracks |

### Identity (◈)

| key | does |
|---|---|
| `M` | link the selection into one identity |
| `A` | assign to a numbered identity |
| `O` | flag as outside |
| `C` | clear the identity |
| `N` or `Shift+Space` | walk to the next problem |
| `Shift+P` | walk back |
| `Q` | switch selection scope |

### Review (✓)

| key | does |
|---|---|
| `Enter` | accept |
| `Backspace` | reject |
| `↓` `↑` | next / previous |
| `G` | show it in the viewport |

### SAM (✦)

| key | does |
|---|---|
| left-click | positive prompt point |
| right-click / `Shift`+click | negative prompt point |
| `Alt`+click | select the track a commit will land on |
| `Enter` | write the previewed mask |
| `Esc` | throw the prompt away |
| `Backspace` | take back the last point |
| `R` / `Shift+R` | propagate forward / backward |
| `C` | crop to the object before segmenting |

### Mouse, everywhere

| gesture | does |
|---|---|
| wheel | zoom |
| drag | pan |
| `Alt`+click | reach the mask underneath; click again to go deeper |
| right-click | list everything under the cursor, with actions |
| `Shift`+click | add to / toggle in the selection |
| double-click | fit |

---

## 18. The command line

```bash
.venv/bin/python -m quorum <command>
```

| command | does |
|---|---|
| `serve [--host H] [--port P] [--reload]` | run the server. `./run.sh` is a wrapper. |
| `plugins` | list every discovered plugin with its tools, layer types, jobs, providers and importers/exporters — and any that failed to load. |
| `ingest <plugin>.<provider> key=value …` | build a capture without the UI. |
| `job <plugin>.<kind> key=value …` | run any job and wait, printing progress and the result. |
| `user <name> [--role annotator\|reviewer\|admin]` | create a user and print their token. |

Values are parsed as JSON when they parse, and as strings otherwise — so
`count=60`, `backward=true` and `paths='["/a.mp4","/b.mp4"]'` all work.

The readiness gate applies here too: a job whose plugin says it cannot work is
refused with the reason, before the handler runs.

A worked example, on this repository's real data:

```bash
# the capture
.venv/bin/python -m quorum ingest grid_capture.prepared stamp=20260612_021454
# the masks: 1,832 tracks, 136,582 keyframes  (~5 s)
.venv/bin/python -m quorum job grid_capture.import:tracks capture_id=1
# the identities made by hand, imported as ops
.venv/bin/python -m quorum job identity.import:consistent_ids capture_id=1
# a generator's opinion — proposals, never assignments
.venv/bin/python -m quorum job identity.link_gaps capture_id=1 layer_id=1
./run.sh        # then open the capture and press N
```

---

## 19. Configuration

Copy `quorum.toml.example` to `quorum.toml`. Paths in it are relative to the
file.

| setting | default | means |
|---|---|---|
| `host`, `port` | `127.0.0.1`, `8600` | where the server binds. |
| `auth_mode` | `"open"` | `open` trusts one local operator; `token` requires bearer tokens from the users table. |
| `dev_user` | `"local"` | who you are in `open` mode. |
| `jobs_workers` | `2` | how many jobs run at once. |
| `media_roots` | `[".."]` | **the security boundary.** Nothing outside these is ever readable over HTTP, whatever a provider puts in a media path — and it is the same rule the server file browser uses, so a path the picker offers is a path the video endpoint would have served. |
| `plugin_paths` | `["plugins"]` | where plugins are looked for, in order; later paths win on an id collision. |
| `plugins_enabled` | all | restrict to a list of plugin ids. |
| `[plugins.<id>]` | — | per-plugin settings, reaching the plugin as `ctx.settings("key")`. The core never looks inside one. |

Environment overrides: `QUORUM_CONFIG`, `QUORUM_HOST`, `QUORUM_PORT`,
`QUORUM_AUTH`.

Notable per-plugin settings:

```toml
[plugins.grid_capture]
root = ".."
prepared_dir = "../sync_id_ui/data"

[plugins.sam]
url = "http://sam-service:8080/"   # $SAM_URL wins. That is the whole SAM config.
```

---

## 20. Running in a container, and on the GPU box

```bash
cp .env.example .env        # set TREE to this tree's absolute path
docker compose up -d --build
```

**The tree is mounted at the same path it has on the host.** A capture's media
path is stored as an absolute path, so a container that mounted the tree
somewhere tidy would come up fine, list every capture, and show six black
rectangles. Mounting it where it already lives makes the two ways of running
this the *same installation*: stop the container, run `./run.sh`, and nothing
has moved.

`plugins/` and `web/` are bind-mounted, because there is no build step — editing
a plugin's UI is a page reload, not an image rebuild.

**It runs as you, not as root.** Set `PUID`/`PGID` (and `DOCKER_GID` so it can
reach the socket) in `.env`. `data/` is shared with `./run.sh`, and a container
writing root-owned files into it breaks the host-side server later and quietly.

Two things `.env` asks you to decide:

- **`SAM_NETWORK`** — the docker network nuclio already put the SAM function's
  container on. Compose joins it and never tries to own it.
- **`WITH_DOCKER`** — only the auto-annotator needs the docker socket. A
  container that can talk to the daemon can do anything the daemon can, so this
  is one line you can turn off: the interactor and the tracker are untouched and
  the auto-annotator refuses with the reason.

### On the GPU box

```bash
docker compose -f docker-compose.yml -f docker-compose.vm3090.yml up -d --build
ssh -L 8600:127.0.0.1:8600 vm.3090          # it stays on loopback there
```

Three things differ there, each on one line of the override file: `$RECORDS`
adds a second media root for the camera share, the container runs as root
because that mount is root-only, and it binds loopback because that box already
publishes fifteen services on `0.0.0.0`.

### The SAM service

One model, in one container, behind one endpoint. The interactor, the tracker
and the auto-annotator all go through it.

```bash
cd plugins/sam/service && docker compose up -d --build
```

`scripts/sam-up.sh` is the one to copy to the GPU box and keep: it creates the
service if it is missing, starts it if it is down, **waits** rather than
restarting one that is merely loading, and prints how much VRAM it is holding
against how much is left. `--watch` keeps doing it every 30 s.

When nothing in the SAM workspace works, that is the one process to check — and
the workspace's **recheck** button is how you pick it up from the browser.

---

## 21. Where files end up

Everything Quorum owns lives under `quorum/data/`:

| path | holds |
|---|---|
| `data/quorum.db` | captures, streams, layers, objects, shapes, ops, assets, users, docs. |
| `data/blobs/`, `data/uploads/` | uploaded files, and partial uploads in flight. |
| `data/renditions/<capture>/` | the browser-playable copies `media.prepare` builds. `media.drop` frees them. |
| `data/cache/` | still frames. Disposable. |
| `data/exports/` | what exporters write, and what the Data panel lists and serves. |
| `data/sam_auto/<capture>/` | the auto-annotator's XML and its discontinuity reports. |

Files you **linked** from the server are not in here and are never written or
deleted by Quorum.

---

## 22. When something looks wrong

**A rail icon is greyed with a `!`.** That plugin declared something it needs
and it is not there. Hover for the reason, or open Capture settings → **Not
ready**, which gives you the button that fixes it. The server refuses the work
either way, so this is information rather than an obstacle.

**The cells are black.** Either the codec is one your browser cannot decode —
each Streams row says `needs a rendition`; run **Prepare video…** — or the media
files are not where the capture says they are. A linked asset whose share is
unmounted says `missing` in red.

**Clicking a mask selects nothing.** A toast says `N hidden tracks skipped`.
They belong to a class you have hidden (Display panel), or they have been
replaced by a cut or a join, deleted, or purged.

**A key appears to do nothing.** Look at the toast. Structural refusals name the
reason ("3 is joined to 1+2+3, unjoin it first"); scope refusals say "one camera
at a time"; a cut outside the track's range names the range.

**The masks are one frame out.** That is a rendition that is not frame-aligned
with its source. `media.prepare` proves alignment and refuses to offer one that
fails, and says so in its result — re-run it with **Rebuild existing**.

**The picture and the masks disagree across cameras.** A clock offset. Capture
settings → Streams → nudge that camera's clock while watching the two cells.

**The top bar says `reconnecting…`.** The connection dropped; it is retrying
with backoff and will replay the ops you missed. `out of date` means it came
back but could not catch up — reload.

**`out of date` on an export or an odd number of tracks.** Run
`masks.materialise`; it replays the edit log onto the imported masks and is safe
at any time.

**SAM says `endpoint down`.** The service is not answering. On the GPU box run
`scripts/sam-up.sh`, then press **recheck** in the SAM panel.

**An auto run is holding the card.** The SAM panel shows VRAM and a `busy` tag;
`Stop` on the job cancels at the next window. Clicking still works meanwhile —
it just waits for the current window, which is seconds.

**A job failed.** The Jobs panel keeps the message, and a toast repeats it. Jobs
survive the tab being closed.

### Running the tests

```bash
tests/run.sh              # unit + property + browser        (~35 s)
tests/run.sh --quick      # python only, no server needed    (a couple of seconds)
tests/run.sh --mutants    # …then check the tests can fail   (~90 s)
```

The browser tests need the server running; `run.sh` starts one if there is not
one already and leaves things as it found them. It refuses to run at all if
`mutants.py --verify` finds a leftover mutation on disk, because a suite whose
result cannot be trusted is worse than no suite.

---

## 23. What is not built yet

Stated plainly so you do not go looking:

- **The calibration workspace.** Calibrations upload, validate and gate; there
  is no UI for solving or inspecting one.
- **The voxel carve itself.** `voxel_carve` ships the requirement gate and
  refuses without a valid calibration; the carve is still `calib_ui/carve.py`
  plus a C kernel.
- **`ppe_review_ui`'s freeze statistics** as a workspace of their own.
- **A settings UI for plugin configuration** — `quorum.toml` is the way.
- **Split/join lineage has no owner.** Identity keys like `86@2470` and `1+2+3`
  that came from `sync_id_ui` are kept and simply refer to objects the mask
  layer has not been told about; the **Replay** panel in the Edit workspace is
  what makes them point at real tracks.

Known rough edges: a frame window is JSON, so a two-second prefetch of dense
masks is ~100 kB gzipped (fine on a LAN); the SAM interactor is a route rather
than a job, deliberately, and is the one place a request waits on something
external.
