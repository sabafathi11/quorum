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

## Part 2 — on their machine (once)

```powershell
winget install Gyan.FFmpeg          # reopen the shell afterwards
winget install Python.Python.3.11
# install Tailscale, sign in, confirm the box is visible

git clone https://github.com/sabafathi11/quorum.git
cd quorum
py -3.11 -m venv .venv
.venv\Scripts\pip install -e .
```

Then `quorum.toml` in the clone — the SAM line is what buys them the interactor:

```toml
media_roots = ["beefline_records"]

[plugins.sam]
url = "http://100.95.156.1:32950/"
```

---

## Part 3 — a session, start to finish

**1. Get the videos.** Download `session_<stamp>.zip` from the Drive folder, then:

```powershell
cd beefline_records
.\fetch_session.ps1 <stamp> C:\Users\you\Downloads\session_<stamp>.zip
cd ..
```

**2. Build the capture.** Note the id it prints.

```powershell
.venv\Scripts\python -m quorum ingest multiview.disk paths="[\"beefline_records/session_<stamp>/cam1_<stamp>.mp4\", ...all cameras...]"
```

**3. Make the video playable.** These recordings are HEVC and no browser decodes
them, so until this finishes every cell is black. One ffmpeg transcode per
camera, minutes each.

```powershell
.\run.ps1
```
Then **⚙ Capture → Streams → Prepare video…**

**4. Auto-annotate — on the box, not here.** Open `http://100.95.156.1:8600`,
paste the token when asked, open the same session's capture, and:

- **SAM workspace (✦) → Auto-annotate**: type the prompts (`person`), press Run.
  Progress is per window and there is a Stop button. Hours, not minutes.
- When it finishes, **Data → Export → CVAT XML**, and download the file it
  leaves.

**5. Import what came back.** On their own machine, unzip the export into
`beefline_records/session_<stamp>/sam/` and:

```powershell
.venv\Scripts\python -m quorum job cvat_xml.import:tracks capture_id=<id> dir=beefline_records/session_<stamp>/sam layer=sam_auto name="SAM auto" provenance=model
```

**6. Annotate.** Everything from here is local and needs nothing from the box
except the interactor:

- **✎ Edit** — split, join, delete, repaint tracks. Every edit is an undoable op.
- **✦ SAM** — left-click a person for a mask, right-click to subtract, `Enter`
  to write it; `R` carries it forward. This is the part that works remotely.
- **◈ Identity** — stitch tracks across cameras.
- **✓ Review** — the proposal queue.

[docs/MANUAL.md](MANUAL.md) covers all of it in detail.

---

## When something looks wrong

- **Every cell is black.** The renditions are not built. Step 3.
- **The SAM panel says the service is not answering.** Either `SAM_BIND` was not
  set on the box, or their Tailscale is down. `curl http://100.95.156.1:32950/health`
  answers `{"ok": true, …}` when it is fine.
- **Auto-annotate is missing or refuses on their machine.** Expected — it is the
  one mode that cannot cross the network. Part 3 step 4.
- **A camera's masks lag then jump.** That is `frame_stride`. At stride 5 a mask
  is written every 5th frame and holds in between.
