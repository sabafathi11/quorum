# Setting up an annotator on their own machine

What this is for: somebody works on their own laptop, with their own database,
against sessions from `beefline_records/` — and still gets SAM, which lives on
the GPU box.

**The one thing that shapes everything below.** SAM has three sizes, and they do
not all cross a network:

| | how it reaches the service | where it can run |
|---|---|---|
| **Interactor** — click a person, get a mask | posts the frame as bytes | anywhere |
| **Tracker** — `R`, carry a mask forward | posts the frames as bytes | anywhere |
| **Auto-annotate** — text prompts, whole cameras | sends a *path*, gets a *path* back | the GPU box only |

So the annotator does the first two on their own machine and triggers the third
on the box, in a browser, and brings the result back as CVAT XML. That is not a
workaround — it is the same import path the repository already uses.

---

## Part 1 — on the GPU box (once, by an admin)

**1. Publish the SAM service on the tailnet.** It defaults to loopback, which
their laptop cannot reach. Set `SAM_BIND` to the tailnet address — never
`0.0.0.0`, because the service has no authentication and this box has a public
address too:

```bash
cd plugins/sam/service
RECORDS=/media/jvn-server/185A27335A270CD6/saba/records \
DATA=/home/jvn-server/saba/quorum/data \
SAM_BIND=$(tailscale ip -4) \
docker compose up -d
```

`SAM_BIND` **moves** the published address, it does not add one — loopback is
gone the moment you set it. So set `[plugins.sam] url` in `quorum.toml` to the
same address, or this server, on this very box, gets connection-refused against
a container that is up and healthy and answers their laptop fine:

```toml
[plugins.sam]
url = "http://100.95.156.1:32950/"    # the address SAM_BIND published on
```

`docker port sam-service 8080` says where it actually is, and the two have to
agree. Restart Quorum after changing this — the config is read at startup.

**2. Bind Quorum to the tailnet and turn on tokens.** In `quorum.toml`:

```toml
host = "100.95.156.1"     # tailscale ip -4.  NOT 0.0.0.0
port = 8600
auth_mode = "token"
```

**3. Make them a token.** `annotator` is the lowest role: it can annotate, run
jobs and export, and cannot delete a capture or list users.

```bash
python3 -m quorum user <their-name> --role annotator
```

**4. Invite their device to the tailnet**, and give them three things: the token,
the URL `http://100.95.156.1:8600`, and a share link to the `beefline_records`
folder in Drive.

---

## Part 2 — on their machine, once

### 2.1 Join the tailnet

Install Tailscale, sign in, accept the invitation. Then prove the box is
reachable before touching anything else — every later step depends on it:

```powershell
ping 100.95.156.1
curl.exe http://100.95.156.1:32950/health
```

The second should answer `{"ok": true, "vram": {...}, "busy": false, ...}`. If it
does not, nothing SAM-related will work and the fix is on the box (Part 1.1),
not here.

### 2.2 Install the two things Python cannot install for itself

```powershell
winget install Gyan.FFmpeg
winget install Python.Python.3.11
```

**Close and reopen PowerShell**, or `ffprobe` will not be on `PATH` yet. Check:

```powershell
ffprobe -version
py -3.11 --version
```

`ffmpeg` is not optional. It is how every frame timestamp is read, how stills
are cut, and how the interactor gets a frame out of a video. Without it, capture
building refuses outright.

### 2.3 Clone and install

```powershell
git clone https://github.com/sabafathi11/quorum.git
cd quorum
py -3.11 -m venv .venv
.venv\Scripts\pip install -e .
```

`pip install -e .` rather than installing fastapi and uvicorn by hand: on the
lab boxes numpy comes from the system python, and on Windows there is none, so
installing the package itself is what makes the venv complete.

### 2.4 Configure

Create `quorum.toml` in the clone:

```toml
media_roots = ["beefline_records"]

[plugins.sam]
url = "http://100.95.156.1:32950/"
```

`media_roots` is what the server is willing to read from disk. The default is
the clone's *parent* directory, which on a laptop is a whole home folder;
naming `beefline_records` keeps it to the videos. The `[plugins.sam]` block is
what buys the interactor — without it the SAM panel has nothing to talk to.

---

## Part 3 — a session, in detail

Everything below uses `20260811_033622` as the stamp. Substitute another and
nothing else changes.

### 3.1 Get the videos

Download `session_20260811_033622.zip` (1.1 GB) from the shared Drive folder,
then unpack it into place:

```powershell
cd beefline_records
.\fetch_session.ps1 20260811_033622 C:\Users\you\Downloads\session_20260811_033622.zip
cd ..
```

Expect seven files, `cam1_20260811_033622.mp4` through `cam7_`, about 1.1 GB
total. **Do not rename them.** Quorum takes each stream's key from the filename
— `cam3_20260811_033622.mp4` → `cam3` — and keys the whole capture by the stamp
they share. Renaming breaks the link between a video and the annotations for it.

### 3.2 Build the capture

Let PowerShell assemble the path list rather than typing seven paths by hand:

```powershell
$files = @(Get-ChildItem beefline_records\session_20260811_033622\*.mp4 |
           ForEach-Object { $_.FullName -replace '\\','/' })
$json  = ConvertTo-Json -InputObject $files -Compress
.venv\Scripts\python -m quorum ingest multiview.disk "paths=$json"
```

This reads every video's real frame timestamps, picks one capture timeline,
builds a frame map per camera and lays out a grid. A minute or two for seven
cameras. It prints a JSON block ending in something like:

```
"capture_id": 1,
"message": "7 view(s), 15009 frames at 25.014 fps — cam1 … cam7 need a browser rendition"
```

**Write down the `capture_id`.** Every later command needs it. The "need a
browser rendition" warning is expected and is the next step.

### 3.3 Make the video playable

```powershell
.\run.ps1
```

Open **http://127.0.0.1:8600**, open the capture, then **⚙ Capture → Streams →
Prepare video…**, leave the renditions at `grid,full`, press Run.

These recordings are HEVC 1920×1080 (cam7 is 2592×1904) and no browser decodes
HEVC, so **until this finishes every cell is black** — that is not a bug and it
is the single most common way this looks broken. It is one ffmpeg transcode per
camera, several minutes each, seven cameras. It is a job: progress and a Stop
button, and it survives closing the tab.

Quorum then proves each rendition is frame-aligned with its source and refuses
one that is not, because an unaligned rendition puts every mask a frame out —
which looks fine and is wrong.

### 3.4 Auto-annotate — on the box, in a browser

This is the one step that does not happen on their machine.

1. Open **http://100.95.156.1:8600**.
2. A dialog appears saying *"paste the token an admin gave you"*. Paste it. It
   is kept in that browser's localStorage, so this happens once.
3. Open the capture for the same session.
4. Go to the **SAM workspace (✦)** and find the **Auto-annotate** panel.
5. Press **Run the auto-annotator…**. In the dialog: prompts `person`, one per
   line. Use singular nouns — text grounding keys on the noun. Leave the rest at
   their defaults unless told otherwise. Press **Start**.
6. Progress appears per window, in the panel and in **Jobs**. **Stop** kills it.
   This is hours, not minutes — one camera at a time, on a shared GPU.

When it finishes, the tracks land as a `provenance="model"` layer on that
capture. To bring them home:

7. **Data → Export**, choose **CVAT XML**, run it.
8. The Data panel lists what the exporter wrote, with a **↓** button. The export
   is one file per camera, so it comes down as a single **zip**.

### 3.5 Import the annotations locally

Unzip what they downloaded into the session's `sam/` folder:

```powershell
Expand-Archive tracks.zip -DestinationPath beefline_records\session_20260811_033622\sam
```

Then import — one command for the whole session, because the importer takes a
directory, globs `*.xml` and matches each file to a stream by the key in its
name:

```powershell
.venv\Scripts\python -m quorum job cvat_xml.import:tracks capture_id=1 `
  dir=beefline_records/session_20260811_033622/sam `
  layer=sam_auto name="SAM auto" provenance=model
```

It prints how many files matched and how many keyframes landed. A file whose
name matches no stream is skipped and said so, rather than silently dropped.

### 3.6 Annotate

Reload **http://127.0.0.1:8600**. The masks are now ordinary tracks.

- **✎ Edit** — `Alt`+click selects a track. Split, join, delete, restore. Every
  edit is an op: it is in **History** and `Ctrl+Z` takes it back.
- **✦ SAM** — left-click a person to add a positive point, right-click for a
  negative one, `Backspace` drops the last point, **`Enter` writes the mask**.
  Nothing is stored until `Enter`. With a track selected, `Enter` repaints that
  track at this frame; with nothing selected it makes a new one. **`R`**
  propagates the selected mask forward, `Shift+R` backward — one job, one op, so
  one `Ctrl+Z` takes all of it back. *This is the part that works over the
  tailnet, because it posts frames as bytes.*
- **◈ Identity** — the only thing that spans cameras. `N` walks unassigned tracks.
- **✓ Review** — the proposal queue. Accepting one appends that op, attributed
  to them.

[MANUAL.md](MANUAL.md) is the full reference; §9 is mask editing, §12 is SAM.

---

## When something looks wrong

- **Every cell is black.** The renditions are not built. §3.3.
- **`ffprobe is not on PATH`.** They did not reopen PowerShell after installing
  ffmpeg. §2.2.
- **The SAM panel says the service is not answering.** Either `SAM_BIND` was not
  set on the box (Part 1.1) or their Tailscale is down. `curl.exe
  http://100.95.156.1:32950/health` is the one-line test.
- **The SAM panel says that *on the server*, naming `127.0.0.1:32950`.** The
  health check above passes and the server still cannot reach it: `SAM_BIND`
  took the loopback binding away, and `[plugins.sam] url` was left on
  `127.0.0.1`. Point it at the published address and restart Quorum. Part 1.1.
- **A 403 on a video, right after a successful ingest.** The videos are outside
  `media_roots`. §2.4.
- **Auto-annotate is missing or refuses on their machine.** Expected. It is the
  one mode that cannot cross a network — it sends a path and gets a path back.
  §3.4.
- **Masks lag the person, then jump.** That is `frame_stride`. At the default 5
  a mask is written every 5th frame and holds in between.
- **A camera fragmented into dozens of short tracks.** Read its
  `*_freezes.json`. These recorders drop up to half their wall clock and every
  hole ends a track; no identity is ever carried across a hole.
