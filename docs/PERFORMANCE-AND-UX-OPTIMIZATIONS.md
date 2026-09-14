# Performance and UX Optimizations

This document records the performance, reliability, and workflow improvements
implemented on the `handoff` branch.  It is written as a technical presentation
of the changes: what was slow, where the work was happening, what changed in
the code, and what remains outside the application.

## Scope and commits

The work is contained in these commits, in this order:

| Commit | Change |
| --- | --- |
| `da9d061` | Optimistic identity editing and faster problem navigation |
| `318b4d9` | Bounded identity-problem batches instead of exhaustive lists |
| `e91c335` | Mask prefetch stays ahead of playback |
| `05c5810` | Direct frame-number entry in the timeline |
| `1685770` | Split a selected identity track with `T` |
| `cf420d6` | Batch SAM prompt dots and prevent duplicate mask rebuilds |
| `8afdf8c` | Keep nearby identity problems prefetched |
| `ca58e96` | Show progress while finding a problem |
| `e9a62bd` | Refill the identity-problem cache after edits |
| `6556f94` | Reload only affected edit data; optionally exclude hidden cameras |
| `b9e9137` | Switch Edit/Identity tools with `I` |
| `aeaea36` | Recover safely from stale cached problem targets |
| `bec50c6` | Keep problem notices visible until acknowledged |
| `1ae825d` | Do not advance video ahead of mask data during rapid stepping |
| `3c55953` | Flush Windows/Python server logs immediately |
| `c67fe4e` | Make delete/restore visually immediate and add Delete shortcuts |

## Architecture: where work runs

The browser is responsible for interaction state, drawing, local caches, and
optimistic visual feedback. Quorum's local backend persists edits and computes
data that must be authoritative. The SAM model runs on the separate GPU host.

```text
Browser
  |  local prompt dots, cached problem batches, immediate UI state
  v
Quorum backend (127.0.0.1:8600)
  |  edit operations, frame extraction, identity scan, SAM proxy request
  v
Remote SAM GPU service (Tailscale, port 32950)
  |  mask prediction / tracking
  v
Quorum backend -> durable mask operation -> browser WebSocket update
```

This division is deliberate: a browser can immediately show a local result,
but only a successful `POST` response from the Quorum backend means an edit is
durably saved. A `GET` only reads state, media, layers, or problem candidates;
it does not write an annotation.

## Identity workspace: bounded problem search and caching

### Previous behavior

The identity workflow could request a very large list of problems, including a
request with `limit=100000`. That made **Next Problem** appear frozen because
the backend had to scan and enumerate far more problems than the user needed.

### New backend contract

`plugins/identity/plugin.py` implements:

```text
GET /api/p/identity/{capture_id}/problems
  ?frame=<starting-frame>
  &direction=<1|-1>
  &limit=<small-number>
  &include_hidden=<optional boolean>
```

The endpoint scans in frame order and returns only the earliest nearby
candidate problems, stopping once it has reached `limit`. It does not calculate
or return a global number of all problems. The response includes the problem
objects and the number of spans inspected, not an expensive total count.

Identity visibility is memoised in `plugins/identity/plugin.py`. The cache is
keyed by capture, layer, and the included stream set, so choosing to ignore
hidden cameras does not accidentally reuse visibility calculated for all
cameras.

### Browser-side problem runway

`plugins/identity/web/identity.js` owns a `problemCache` map. Its cache key
contains:

```text
capture id | starting frame | direction | hidden-camera mode
```

The browser requests up to four nearby problems at a time. When a problem is
shown, it is removed from the local batch and a background refill starts if
fewer than four candidates remain. Therefore, the next two or three presses
usually consume already-known candidates instead of waiting for a new scan.

The cache is protected by `problemEpoch`. A change that invalidates problem
meaning increments the epoch; a late response belonging to an older epoch is
discarded rather than being displayed over newer data. Candidates are also
deduplicated with a stable key made from frame, kind, camera/id, and objects.

### Correct invalidation policy

Not every edit has the same impact:

| Edit | Cache handling |
| --- | --- |
| Assignment/identity change | Invalidate and prefetch a new nearby batch. |
| Delete / mark Outside | Remove only affected cached candidates locally. It cannot create a new identity problem, so it avoids an expensive rescan. |
| Cut, join, or restore | Clear the cache because track topology can change the problem set. |
| Hidden-camera setting changed | Clear the cache because the scan scope changed. |

This makes the UI responsive without pretending that a structural change can
be safely predicted only in the browser.

### Stale candidate recovery and feedback

A cached candidate can become obsolete if another edit changes or removes its
track. Before revealing a candidate, the browser checks that its objects are
still displayable. If not, it invalidates the cache and performs one fresh
server check. This avoids a blank **Next Problem** result while avoiding an
unbounded retry loop.

While a request is in progress, the button changes to **Finding next
problem…** and navigation is disabled. Once found, the explanatory problem
notice is sticky: it stays on screen until the user clicks **OK**, rather than
disappearing before the user has found the object in the video grid.

### Hidden-camera scan option

The Identity panel has a local preference:

```text
quorum.identity.includeHidden
```

By default, cameras disabled in Capture settings are omitted from identity
scanning. The server receives `include_hidden=true` only if the user explicitly
enables hidden cameras. This improves initial scans and prevents ignored
cameras from producing identity problems. It does not itself hide a camera;
the stream must first be disabled in Capture settings.

## Faster edit operations

### Optimistic delete and restore

`plugins/masks/web/masks.js` now applies delete/restore to local display state
before waiting for the backend:

1. Update `S.deleted` locally.
2. Mark display state as changed and repaint the viewport.
3. Show the selected track immediately in the deleted style (`DEL`, red) when
   deleted tracks are visible.
4. Send the durable `masks.delete` or `masks.restore` operation.
5. If the request fails, reload authoritative mask state to roll the local
   optimistic state back safely.

Delete/restore changes display policy, not mask geometry. The implementation
therefore avoids rebuilding every layer for that simple operation.

Keyboard shortcuts are:

| Action | Shortcuts |
| --- | --- |
| Delete selected tracks | `D`, `Delete` |
| Restore selected tracks | `Shift+D`, `Shift+Delete` |
| Show/hide deleted tracks | `H` |

### Avoiding unnecessary reloads

Structural operations such as cut, join, and split can alter which effective
tracks exist. They still need derived data to be refreshed, but
`web/core/app.js` adds `reloadLayer(layerId)` so the app reloads only the
materialised edits layer when that layer already exists. It falls back to a full
layer refresh only when the first structural edit creates the layer.

This replaces a complete capture/layer refresh for many normal edit actions.

### Duplicate WebSocket work prevention

The browser receives its own saved operations through the WebSocket as well as
through the original HTTP response. Local operation IDs and `client_ref` values
are tracked in the masks client. The WebSocket echo is recognised as the same
operation and does not trigger duplicate materialisation/reload work.

`quorum/host.py` carries the transient `client_ref` metadata needed for that
matching. It is delivery metadata, not annotation data to be persisted as part
of the mask content.

## Playback: keep masks synchronized with video

Two related changes eliminate mask lag and disappearing overlays during rapid
frame navigation.

### Layer-data runway

`web/core/host.js` keeps a small loaded frame coverage window for each mask
layer. It coalesces concurrent fetches and records the newest requested frame.
When playback reaches the edge of current coverage, it starts the next batch
from the newest target instead of allowing a queue of stale requests to form.

### Ordered stepping

`web/core/app.js` now stores the latest requested step in `_stepTarget`. Rapid
Next Frame presses update that target instead of starting unrelated concurrent
step operations. The stepping loop calls `ensureFrameData(target)` before it
commits the frame change. If mask data has not arrived, the previous annotated
frame remains visible rather than showing the video with missing or ghost
masks.

The important invariant is:

```text
The displayed annotation frame must not be older/newer than the committed video frame.
```

## SAM interaction optimization

### Prompt batching

In `plugins/sam/web/sam.js`, clicks add positive or negative prompt dots to
browser-local arrays (`S.pos` and `S.neg`). A dot no longer sends a model
request. The user can place all desired points and then select **Send prompts**
or press `Ctrl+Enter`.

The one request contains the complete prompt:

```json
{
  "stream": "camN",
  "frame": 123,
  "stream_frame": 123,
  "pos": [[x, y]],
  "neg": [[x, y]],
  "bbox": [x1, y1, x2, y2],
  "roi": true
}
```

`Enter` has a separate meaning: it commits the previewed mask through the
normal masks edit path. `Esc` clears the local prompt. A prompt is discarded if
the user leaves its frame, preventing a preview from being written on a frame
that was never inspected.

### SAM request path

The browser never contacts the GPU host directly:

```text
Browser POST /api/p/sam/{capture}/interact
  -> Quorum extracts a correctly scaled source frame with ffmpeg
  -> applies optional region-of-interest crop
  -> base64-encodes the JPEG and posts it to SAM
  -> shifts the returned RLE mask from crop coordinates to stream coordinates
  -> browser receives a preview only
  -> Enter writes a standard masks operation
```

The interactor itself writes nothing. This keeps model experimentation
reversible: only an explicit mask commit is durable and undoable.

## SAM GPU and Tailscale audit

Current configuration in `quorum.toml`:

```toml
[plugins.sam]
url = "http://100.95.156.1:32950/"
probe_timeout = 15
```

The live service was healthy during the audit:

| Measurement | Result |
| --- | --- |
| GPU VRAM | 6,743 MiB used / 24,564 MiB total / 17,821 MiB free |
| GPU busy state | false |
| Direct health request | approximately 0.93–1.27 seconds |
| Cached local `/api/p/sam/status` | approximately 0.03 seconds |
| Forced/expired status check | approximately 1.3–1.4 seconds |

Tailscale reported `direct connection not established`; requests are relayed
through a DERP relay (measured ping about 300 ms). This is the main remaining
latency source. The service uses HTTP/1.1 keep-alive, but the current Quorum
SAM client creates a fresh HTTP request connection for each model call, so the
relay connection setup is visible on each new prompt.

The main infrastructure improvement is to establish a direct Tailscale UDP
path. Run `tailscale netcheck` on both the Quorum machine and GPU machine and
ensure their firewall/router permits outbound UDP. Restrictive or symmetric NAT
can force DERP relay even though Tailscale itself is connected.

MagicDNS was verified and may replace the raw Tailscale IP for clearer,
hostname-based configuration:

```toml
[plugins.sam]
url = "http://ehsan-iref-desk.tailbb2ec1.ts.net:32950/"
```

The SAM service has no application-level authentication. Tailscale encrypts
traffic on the tailnet, but the Tailscale ACL should limit access to port 32950
to the Quorum machine/users that require it. The Docker configuration is
designed to bind the port to a specific Tailscale address, never `0.0.0.0`.

There is one remote-topology limitation:

| SAM feature | Remote GPU compatibility | Reason |
| --- | --- | --- |
| Interactive prompt | Works | A cropped JPEG is sent as bytes. |
| Tracking | Works, but can be network-heavy | Many JPEG frames are sent as bytes. |
| Auto annotation | Not valid without shared storage | The job sends video/output filesystem paths; the GPU machine must see those paths and files. |

## Timeline and navigation workflow

`web/core/timeline.js` adds a wider editable frame-number field to the display:

```text
<current frame> / <total frames>  <time>
```

Entering a valid frame number jumps directly to that frame. The wider field
prevents multi-digit frame numbers from being visually truncated.

Tool navigation also gained `I`, registered in `web/core/app.js`, to switch
between Edit and Identity modes. In Identity, `T` splits the selected track at
the current frame. In Edit, `T` remains the existing cut behavior; both are
structural operations that create a new track segment from the split point.

## Server log reliability on Windows

`quorum/__main__.py` reconfigures stdout and stderr to use line buffering and
write-through where the runtime supports it. This prevents PowerShell from
appearing to hold HTTP request logs until `Ctrl+C` is pressed.

`Ctrl+C` should not be used to flush logs: it begins server shutdown and can
interrupt an in-flight edit before its successful `200 OK` response. A write is
considered completed only after the backend returns success. The application
uses optimistic browser feedback for responsiveness, but a failed request is
not durable and must be retried.

## Validation performed

The changes were checked with:

- JavaScript module syntax checks using Node's module parser.
- Python AST parsing for changed Python modules.
- `git diff --check` for whitespace/patch correctness.
- Live read-only API checks against the running local server and remote SAM
  health endpoint.
- Tailscale peer status, ping, and network diagnostics.

The broad test run was intentionally not used because its Windows SQLite
cleanup behavior blocks independently of these changes.

## Remaining opportunities

1. Make the Tailscale path direct; this has the largest user-visible SAM
   latency impact.
2. Reuse the HTTP connection from Quorum to the remote SAM service, reducing
   repeated relay connection setup on consecutive prompts.
3. If remote tracking is used frequently, move frame extraction closer to the
   GPU or add a deliberate compressed/batched transfer protocol.
4. Add a transfer/shared-storage design before enabling remote auto annotation.
5. Keep Tailscale ACLs scoped to the SAM service port and intended devices.
