# Quorum cheat sheet

Fast reference for reviewing multi-camera captures, correcting tracks, assigning identities, and using SAM. Press the `?` button in the top bar for the guided, first-run walkthrough.

## Everyday workflow

1. In **Capture**, make sure the required cameras are shown and browser video has been prepared.
2. In **SAM**, click a person, send the prompt, inspect the preview, then create or replace a mask.
3. In **Edit**, remove false tracks, restore mistakes, and join or split fragments.
4. In **Identity**, link the same person across cameras and resolve the next unassigned or conflicting case.
5. Use **History** or `Ctrl+Z` to reverse an edit.

## Global controls

| Shortcut | Action | Use it when… |
|---|---|---|
| `Space` | Play / pause | Scanning for movement or an event. |
| `Left` / `Right` | Previous / next frame | Checking an exact transition. |
| `Shift+Left` / `Shift+Right` | Jump one second back / forward | Moving quickly through a recording. |
| `F` | Fit mosaic to window | You have panned or zoomed away from the full view. |
| `Z` (hold) | Zoom the hovered camera | Inspecting one camera without changing layout. |
| `Esc` | Clear selection | Starting a new selection. |
| `Ctrl+Z` | Undo your latest edit | Reversing a mask, track, or identity edit. |
| `Ctrl+K` | Command palette | Finding any available command. |
| `Ctrl+B` | Show / hide inspector | Making more room for video. |
| `Ctrl+,` | Open Capture workspace | Managing files, cameras, clocks, and layers. |
| `I` | Switch Edit ↔ Identity | Moving from track repair to identity work. |
| `Shift+H` | Show all hidden classes | Restoring labels hidden through Display. |

## Transport and timeline

| Control | What it does |
|---|---|
| Previous / Play / Next | Step one frame back, play or pause, and step one frame forward. |
| Frame number | Type a capture-frame number and press `Enter` to jump precisely there. |
| Timeline scrubber | Drag to move through the capture; camera lanes show where tracks exist. |
| Playback speed | Change scan speed without changing the recording or annotations. |

## Capture workspace

| Feature | What it does | Typical use |
|---|---|---|
| **shown / hidden** on a stream | Adds or removes one camera from the mosaic without deleting it. | Hide an obstructed camera while reviewing the others. |
| **Prepare video** | Builds browser-playable renditions with the source geometry and timing retained. | Run after adding HEVC recordings. |
| **Upload** | Copies a local file to the server as capture data. | Upload CVAT XML before importing tracks. |
| **From the server** | Links an already-accessible server file without copying it. | Use recordings from a mounted shared drive. |
| **clock** controls | Shift a camera’s timing while masks and video stay aligned. | Match the same event across cameras. |
| **Display** | Chooses visible annotation classes and colour mode. | Hide a noisy label class during focused review. |

## SAM workspace

| Shortcut | Action | Notes |
|---|---|---|
| Left-click | Add a positive prompt dot | Click the object you want. |
| Right-click | Add a negative prompt dot | Click background or an object to exclude. |
| `Ctrl+Enter` | Send prompt | Produces a preview; it writes nothing. |
| `Enter` | Create track / replace mask | Saves the accepted preview at this frame. |
| `Esc` | Clear prompt | Discards dots and preview; nothing is saved. |
| `Backspace` | Remove the most recent prompt dot | Refine a prompt before sending it. |
| `R` / `Shift+R` | Propagate forward / backward | Carries the selected mask through its camera. |
| `C` | Toggle crop to object | Faster and often better for small objects; inspect the preview before saving. |

**Safe SAM sequence:** add dots → `Ctrl+Enter` → inspect the preview → `Enter` → optionally use `R` to propagate. A prompt belongs to its current frame; moving away drops it intentionally.

## Edit workspace

Select a mask first. Edit operations are per camera and are recorded as reversible edits.

| Shortcut | Action | Typical use |
|---|---|---|
| `D` / `Delete` | Delete selected tracks | Hide a false-positive track. |
| `Shift+D` / `Shift+Delete` | Restore deleted tracks | Recover a valid track deleted by mistake. |
| `P` | Purge | Remove a track from the working set and export; use deliberately. |
| `T` | Cut at current frame | Split a track at an occlusion or identity change. |
| `J` | Join selected tracks | Reconnect fragments of the same person. |
| `U` | Unjoin | Separate fragments joined incorrectly. |
| `H` | Show / hide deleted tracks | Review deleted tracks tagged `DEL`. |

## Identity workspace

Identity actions can apply to the selected track or the entire selected identity, depending on scope. `Ctrl`-click or `Alt`-click selects one track when needed.

| Shortcut | Action | Typical use |
|---|---|---|
| `M` | Link | Give selected tracks one shared identity. |
| `A` | Assign | Enter a known identity number for the selection. |
| `O` | Outside | Mark a track as outside the task area or scope. |
| `C` | Clear | Remove an incorrect identity assignment. |
| `N` or `Shift+Space` | Next problem | Jump to the next unassigned or conflicting identity. |
| `Shift+P` | Previous problem | Return to the previous identity problem. |
| `Q` | Toggle scope | Switch between whole-identity and one-track selection. |
| `T` | Split selected track here | Split a track at the current frame before assigning separately. |

## Useful mouse actions

| Gesture | Result |
|---|---|
| Click a mask | Select according to the active workspace’s selection scope. |
| `Alt`-click a mask | Select exactly that one track. |
| `Ctrl`-click a mask | Add/select one track when working with identities. |
| Right-click a mask | Choose among overlapping tracks and related actions. |
| `?` in the top bar | Reopen the guided walkthrough at any time. |

For full explanations, data formats, jobs, and troubleshooting, see [MANUAL.md](MANUAL.md).
