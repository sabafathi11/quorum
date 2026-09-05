# Writing a Quorum plugin

A plugin is one directory. The Python half declares what it provides and does
the server-side work; the JavaScript half is an ES module the browser imports at
boot. Neither half is required — `grid_capture` has no UI, and a pure
visualiser needs no Python.

```
plugins/my_thing/
  plugin.py          # PLUGIN = Plugin(id="my_thing", …)
  web/my_thing.js    # export default { id: "my_thing", activate(ctx) {…} }
```

The loader imports `plugin.py`, checks that `PLUGIN.id` matches the directory
name and that the declared web module exists, then mounts `PLUGIN.route` under
`/api/p/my_thing/` and serves `web/` at `/plugins/my_thing/web/`. A plugin that
throws on import is reported and skipped; the rest of the server comes up.

**Trust:** plugins are installed by an admin and are as trusted as any other
Python dependency. There is no sandbox. Do not install a plugin you would not
`pip install`.

---

## The smallest useful plugin

```python
# plugins/hello/plugin.py
from fastapi import Request

from quorum.sdk import Plugin, Tool

PLUGIN = Plugin(id="hello", name="Hello", web="hello.js")
PLUGIN.tool(Tool(id="hello", title="Hello", icon="☺", order=90))

@PLUGIN.route.get("/{capture_id}/count")
def count(capture_id: int, request: Request):
    db = request.app.state.host.db
    return {"objects": db.scalar(
        "SELECT COUNT(*) FROM objects o JOIN layers l ON l.id=o.layer_id "
        "WHERE l.capture_id=?", capture_id)}
```

```js
// plugins/hello/web/hello.js
export default {
  id: 'hello',
  activate(ctx) {
    ctx.registerInspector('hello', () => ctx.ui.panel('Hello', [
      ctx.ui.stat('frame', String(ctx.store.get('frame'))),
      ctx.h('button', { class: 'btn sm', onclick: async () => {
        const r = await ctx.call(`/${ctx.store.get('capture').id}/count`);
        ctx.toast('Objects', String(r.objects));
      } }, 'Count objects'),
    ]));
    ctx.registerCommand({ id: 'wave', title: 'Wave', keys: ['W'], tool: 'hello',
                          run: () => ctx.toast('👋') });
  },
};
```

Restart the server; the tool appears in the rail.

---

## Server side

### `Plugin`

| | |
|---|---|
| `Plugin(id, name, version, description, web)` | `web` is a filename inside `web/`. |
| `PLUGIN.tool(Tool(...))` | a workspace: id, title, icon, order. Declared on the server so the tab exists even if the ES module fails to load. |
| `PLUGIN.layer_type(LayerType(...))` | a geometry kind: id, name, `renderer` (an export name in your module). |
| `PLUGIN.route` | a FastAPI `APIRouter`, mounted under `/api/p/<id>/`. |
| `@PLUGIN.job(kind, title, params)` | background work. Also reachable from the CLI. |
| `@PLUGIN.provider(id, title, params)` | makes captures. Runs as job `provider:<id>`. |
| `@PLUGIN.importer(id, …)` / `@PLUGIN.exporter(id, …)` | run as jobs `import:<id>` / `export:<id>`. |
| an exporter's output | write into `ctx.cfg.data_dir / "exports"` and return `path` (or `dir` / `files`). The core lists it and hands it to the browser — a directory as one zip. |
| `@PLUGIN.validator(*kinds)` | say whether one of your ops still applies. |
| `PLUGIN.asset_kind(AssetKind(...))` | a kind of file people upload for you. |
| `PLUGIN.requires(Requirement(...))` | something you cannot work without. |
| `@PLUGIN.readiness` | `(host, capture_id) -> [reasons]`, for what a count cannot say. |
| `@PLUGIN.startup` | called once when the server boots, with a `Ctx`. |
| `@PLUGIN.cleanup` | `(ctx, capture_id)` — a capture is being deleted; drop your files. |

`params` is a small schema the UI turns into a dialog and the CLI into
`key=value` arguments:

```python
params={"stamp":  {"type": "string",  "label": "Capture stamp", "required": True},
        "replace":{"type": "boolean", "label": "Replace", "default": False},
        "limit":  {"type": "integer", "default": 0},
        "mode":   {"type": "string",  "options": ["fast", "exact"], "default": "fast"},
        "tracks": {"type": "asset", "kind": "cvat_xml.tracks", "multiple": True}}
```

A parameter of type `asset` becomes a picker of the capture's uploaded files of
that kind, with an "upload one now" button beside it. Your handler calls
`ctx.asset(id)` and gets a `Path` — so an importer never learns that uploading
exists, and never grows a second code path for "a file on disk" versus "a file
somebody sent us".

### Validators — when a stored op goes off

An op is normally written and applied in the same breath. Proposals break that:
the queue is a pile of *pre-built* ops waiting for a human, and the world moves
under them. A suggestion to link two tracks stops meaning anything the moment
somebody joins one of them into something else, and accepting it then would
write an identity onto a key nothing points at — silent, invisible, and
impossible to notice later.

Only the plugin that owns the op kind can judge that, so the core asks:

```python
@PLUGIN.validator("link", "assign", "clear")
def still_applies(host, capture_id, payload):
    live = live_keys(host, capture_id)
    gone = [f"{s}/{k}" for s, k in payload.get("items", [])
            if str(k) not in live.get(str(s), ())]
    return f"{gone[0]} no longer exists" if gone else None
```

Return `None` when the op is fine, or a short reason when it is not. The
proposal queue calls this when it lists (flagging those items `stale`, kept
rather than deleted so a generator can still be scored) and again before it
accepts one. A plugin that registers no validator is taken at its word.

Validators run once per queued item, so make them cheap or memoise them — this
one caches on `(max op id, object count)`, which took listing a thousand-item
queue from two seconds to 140 ms.

### Asset kinds — files people upload for you

The core stores bytes, a name, a size, a hash and a **kind** string. It never
opens the file. You declare the kinds you understand:

```python
PLUGIN.asset_kind(AssetKind(
    id="calibration",                       # -> "voxel_carve.calibration"
    title="Camera calibration",
    description="Camera poses, intrinsics and lens model.",
    accept=[".json"], multiple=False,
    on_ready="validate"))                   # a job of yours, given {asset_id, capture_id}
```

That declaration is the **entire** contact between your plugin and the upload
machinery. The upload dialog is built from the list of declared kinds, so a
plugin added next month gets an upload UI without the upload UI changing, and
nothing in the upload path knows that a calibration exists. Read them back with
`ctx.assets(capture_id, "calibration")` or `ctx.asset(id, "calibration")`.

A person can re-file an asset under a different kind at any time — `on_ready`
runs again — so never assume the file you were given is the one you expected;
say so out loud if it is not.

### Requirements — refusing, rather than failing oddly

```python
PLUGIN.requires(Requirement(
    asset_kind="calibration", at_least=1,
    why="the carve intersects camera frusta, and without a solved calibration "
        "there is no geometry to intersect",
    fix="Upload the solved calibration on the capture's Data panel.",
    jobs=["suggest"]))                      # omit to mean every job
```

The core answers `GET /captures/{id}/readiness` from these — the rail greys your
tool and shows `why` — **and `jobs.submit` refuses**, with the reason, before
your handler is entered. The greyed-out button is a courtesy; the refusal is the
guarantee, and it holds for the CLI and for another plugin too.

For anything a count of files cannot express, add a function:

```python
@PLUGIN.readiness
def also(host, capture_id):
    """A calibration that covers *these* cameras, not merely a file of the
    right name."""
    return ["the uploaded calibration has no camera for cam4"] if wrong else []
```

Return `[]` when you are ready. Write the reason as a sentence somebody can act
on; it is the only thing they will see.

A reason may also be a **dict**, when it applies to only some of what your
plugin does:

```python
@PLUGIN.readiness
def ready(host, capture_id):
    return [{"why": "the SAM endpoint is not answering",
             "fix": "Deploy the function and put this server on its network.",
             "tools": ["sam"], "jobs": ["track"]}]     # …but not job `auto`
```

`Requirement` could always say "only these jobs"; a check that runs code could
not, so a plugin holding two unrelated abilities — an interactor that needs a
model endpoint and a batch job that runs the model itself — had to block both
or neither. A reason that names `jobs` and no `tools` is about those jobs and
does not grey the workspace.

**Memoise anything that touches the network or a subprocess.** Readiness is
answered on every capture open and before every job submission; `sam` caches
its endpoint probe for 20 s and `docker info` for 60 s.

### Settings — `[plugins.<id>]`

```toml
[plugins.sam]
url = "http://nuclio-nuclio-pth-comfyorg-sam3-1-v2:8080/"
image = "cvat.pth.comfyorg.sam3.1:latest-gpu"
```

reaches you as `ctx.settings("url")`. The core never looks inside one of these
tables: which keys a plugin understands is the plugin's business, and a core
that validated them would have to know what every plugin is for.

### `Ctx` and `JobContext`

Handlers take a context. `Ctx` gives `db`, `cfg`, `settings(key)`, `emit_op`,
`doc_get` / `doc_put` and `blob_path`. `JobContext` adds `params`,
`capture_id`, `progress(frac, message)`, `log(line)`, `check()` (raises if
cancelled), `cancelled`, and `write`.

```python
@PLUGIN.job("crunch", title="Crunch", params={"capture_id": {"type": "capture"}})
def crunch(ctx):
    for i, row in enumerate(rows):
        ctx.check()                       # cooperative cancellation
        ctx.progress(i / len(rows), f"row {i}")
    ctx.emit_op(ctx.params["capture_id"], "crunched", {"rows": len(rows)})
    return {"message": f"{len(rows)} rows"}   # shown in the UI, stored on the job
```

`ctx.emit_op` namespaces the kind for you: `emit_op(cid, "crunched", …)` becomes
`my_thing.crunched`. The core refuses ops whose namespace no plugin owns.

### `ctx.write` — the only way to write core tables

```python
cap = ctx.write.capture(key="stamp", name="…", n_frames=9002, fps=15.0,
                        layout={"kind": "mosaic",
                                "mosaic": {"path": "/abs/path.mp4", "w": 1920, "h": 720},
                                "cells": [{"stream": "cam1", "x": 0, "y": 0, "w": 640, "h": 360}]})
sid = ctx.write.stream(cap, key="cam1", idx=0, width=1920, height=1080,
                       n_frames=11667, media={"path": "/abs/cam1.mp4", "kind": "video/mp4"},
                       frame_map=np.ascontiguousarray(sync, dtype="<i4").tobytes())
lay = ctx.write.layer(cap, key="masks", name="Masks", type="mask.rle", provenance="model")
ctx.write.objects(lay, sid, [{"key": "17", "label": "person", "frames": [
    {"frame": 100, "outside": 0, "payload": {"box": [1, 2, 3, 4], "rle": "…"}}]}])
```

`objects()` upserts and replaces that object's shapes in one transaction. A
media `path` is only served if it is under a configured `media_roots` entry.

### The document store

```python
value, version = ctx.doc_get(capture_id, "queue", default={"items": []})
ctx.doc_put(capture_id, "queue", value, expect_version=version)   # 409 if stale
```

Namespaced by plugin id. Good for derived state and small queues; when yours
outgrows it, that is a change to your plugin only.

### Reaching another plugin

```python
other = ctx.host.plugins.get("proposals")
import sys; sys.modules[other.module_name].add(ctx.host, capture_id, "my_thing", items)
```

Explicit, checkable, and it fails loudly when the other plugin is not installed.
There is no implicit dependency resolution.

That includes **writing an op somebody else owns**, which is legitimate when the
dependency is stated: `sam` produces mask edits, `masks` owns those, so the
propagation job appends `masks.keyframes` and then calls that plugin's
`materialise` — rather than growing a second, subtly different idea of what a
mask edit is. The rule it must not break is the other one: do not *interpret*
another plugin's payload without depending on it out loud.

### More than one file

The loader gives your `plugin.py` the module name `quorum_plugins.<id>`, which
is not a package, so `from . import helpers` has nothing to resolve against.
Load siblings by path:

```python
def _load(name):
    import importlib.util, sys
    key = f"quorum_plugins.{PLUGIN.id}.{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod
```

The client half has no such problem: a **relative** import between plugin web
directories (`../../mask_layer/web/mask.js`) resolves identically in the browser
and in the jsdom harness.

---

## Client side

The module's default export is `{ id, activate(ctx) }`. `activate` runs once at
boot, before any capture is open.

### The context

| call | what it does |
|---|---|
| `ctx.registerTool(spec)` | add a workspace (usually declared on the server instead) |
| `ctx.registerLayerRenderer(type, factory)` | draw and hit-test a layer type |
| `ctx.registerColorMode(spec)` | a way of colouring everything: `{id, title, colorOf(entry), labelOf}` |
| `ctx.registerFilter(spec)` | something that should not be on screen: `{id, title, structural, test(entry)}` |
| `ctx.registerStyler(fn)` | decorate what is already coloured: `(entry, base) => style` |
| `ctx.registerInspector(toolId, fn)` | build the right-hand panels for a tool |
| `ctx.registerCommand({id, title, keys, tool, run, hold, repeat})` | keyboard + palette |
| `ctx.registerLaneDecorator(fn)` | paint into the timeline lanes |
| `ctx.setPickHandler(toolId, fn)` | decide what a click means while that tool is active |
| `ctx.op(kind, payload)` / `ctx.job(kind, params)` / `ctx.call(path, opts)` | act |
| `ctx.doc(key)` / `ctx.putDoc(key, value, version)` | your document store |
| `ctx.on(evt, fn)` / `ctx.emit(evt, data)` / `ctx.onOp(fn)` | events |
| `ctx.objectOf(id)` / `ctx.findObject(stream, key)` | find a track **in whichever layer holds it** |
| `ctx.visibleObjects(pred)` | every track that is actually on screen — the honest answer to "what is there" |
| `ctx.entriesAt(frame, layer)` | what may be drawn at a frame, already filtered |
| `ctx.reveal(ids, {frame, pulse})` | the only sanctioned jump-and-point; returns `{shown, hidden}` |
| `ctx.display` | the display authority: `visible(entry)`, `paint(entry, base)`, `query` |
| `ctx.reloadLayers()` | re-read the capture's layers after a structural edit |
| `ctx.pulse(ids, {color, ms})` | ring some masks, for when you moved the frame on someone's behalf |
| `ctx.undo(kindPrefix)` | take back this user's last op, optionally within one namespace |
| `ctx.store`, `ctx.api`, `ctx.ui`, `ctx.h`, `ctx.toast`, `ctx.invalidate()` | the rest |

Events on the bus: `capture`, `tool`, `op`, `selection`, `hover`, `pick`,
`data`, `drawn`.

**Never assume there is one layer of a type.** `app.primaryData()` is the first
visible layer and is safe only for things every layer answers identically, like
frame mapping. A plugin can materialise a derived layer beside an imported one —
`masks` does — and then the *same key exists in both*: the first half of a cut
track keeps its original key. Use `objectOf` / `findObject` / `visibleObjects`,
which look everywhere and prefer the copy that is actually being drawn. Getting
this wrong is not theoretical; it shipped, and it made a joined track selectable
but impossible to act on.

### A layer renderer

```js
ctx.registerLayerRenderer('my.type', ({ layer, data, app }) => ({
  draw(g, view) {
    // g is already transformed into capture space; view.entries are the shapes
    // active at view.frame, one per visible object
    for (const e of view.entries) {
      const cell = view.cellOf(e.stream.key);           // where that stream sits
      const style = view.styleFor(e, { color: '#0f0', alpha: .4, label: e.object.key });
      if (style.hidden) continue;
      // … draw e.payload …
    }
  },
  hitTest(x, y, view) { return entryUnderCursor || null; },
  dispose() {},
}));
```

`view` also carries `frame`, `scale`, `selection` (a `Set` of object ids),
`hover`, `opacity` and `layer`. Coordinates are capture space: a stream's own
pixels scale by `cell.w / stream.width`.

### The display authority — colour, and what is on screen at all

There is **one** owner of "what is being shown and what colour it is"
(`web/core/display.js`), and the core closes the four doors an object can reach
a person through: drawing, hit testing, selection and navigation. A plugin
cannot show, click, select or walk to something the user has hidden — not
because every plugin remembers, but because it is never handed one.

That is not tidiness. Every version of "each feature filters for itself" in this
codebase has shipped a bug: the walk stopping on superseded tracks, group select
picking eight invisible ones, the exporter shipping unedited masks.

**A colour mode** is a way of colouring everything, available in any tool:

```js
ctx.registerColorMode({
  id: 'identity', title: 'by identity', order: 5,
  colorOf: (entry) => cidColour(cidOf(entry)),
  labelOf: (entry) => `${entry.object.key} → ${cidOf(entry)}`,
});
ctx.on('tool', (t) => { if (t?.id === 'identity') ctx.display.suggestMode('identity'); });
```

`suggestMode` never overrides a mode the user picked on purpose. Do not recolour
from a styler based on which tool is open — that was the old shape, and it made
identity colours unavailable everywhere else and unrefusable while they were on.

**A filter** says something should not be on screen:

```js
ctx.registerFilter({
  id: 'superseded', title: 'superseded copies', structural: true,
  test: (entry) => isReplaced(entry),          // true = hide
});
```

`structural: true` means correctness, not taste — showing it would put two
copies of one track on screen — so it is always on and has no toggle.
`structural: false` is a user preference and appears in the Display panel.

**A styler** now only *decorates* what the mode has already coloured — an
emphasis while your tool is open, a "DEL" tint. It must not decide visibility;
return a filter instead.

```js
ctx.registerStyler((entry, base) => {
  if (ctx.store.get('tool') !== 'my_tool') return base;      // stay out of the way
  return { ...base, alpha: emphasised(entry) ? 0.8 : 0.4 };
});
```

**If your endpoint offers objects to a human, take the filter.** Anything that
seeks to a frame and points at something — a walk, a review queue — must accept
`?filter=<json>` and honour it (`quorum.filters.ObjectFilter`), because
otherwise it offers what the person is hiding and the client then declines to
point at it, which reads as a bug. The client sends it as `ctx.display.query`.
Endpoints that merely return data the client is about to filter itself (a frame
window) do not need it, and should not take it.

### Time, renditions and the mosaic

The mosaic is composed in the browser from the streams themselves; there is no
mosaic file to build. Three things follow that a plugin should know:

- **A stream's `frame_map` is derived**, from its own frame timestamps and its
  clock offset, and the offset is an op (`core.stream_offset`). Do not write a
  frame map you cannot recompute; give `ctx.write.stream(..., timestamps=...)`
  and the core derives it.
- **A rendition is a second copy of a camera's pixels**, at another size, in a
  codec a browser can decode. It is only usable if its frame *N* is the source's
  frame *N* — `media.prepare` proves that and refuses to offer one it cannot.
- **A cell is seeked to the timestamp of the frame the frame map named**, not to
  `frame / fps`. That is the contract with the annotations: the picture on
  screen is the picture the mask was drawn on.

### Commands and keys

`keys` are chords: `'M'`, `'Shift+Space'`, `'Ctrl+K'`, `'Left'`, `'Esc'`.
A command with `tool: 'x'` only fires while that tool is active and beats a
global binding on the same key. `hold: true` fires with `{down: true}` on press
and `{down: false}` on release — that is how hold-to-zoom works. Leave `Space`
alone: play/pause should mean the same thing in every tool.

### Proposals, if you generate anything

Never write an annotation from a model. Put a *pre-built op* plus its evidence
in the queue and let a person accept it:

```python
sys.modules[ctx.host.plugins["proposals"].module_name].add(
    ctx.host, capture_id, "my_thing.generator", [{
        "kind": "identity.link",
        "payload": {"items": [["cam1", "63"], ["cam1", "76"]]},
        "title": "cam1: link 63 → 76",
        "confidence": 0.96,
        "support": {"gap_frames": 4, "drift_box_widths": 0.0},
        "focus": {"stream": "cam1", "stream_frame": 4412, "objects": [1201, 1288]},
        "note": "same camera, short gap, the box barely moved",
    }])
```

`focus` is what the reviewer sees: the queue seeks there and selects those
objects before asking. Accepting appends `kind`/`payload` to the log as the
reviewer; rejecting is recorded and kept, so a generator can be scored against
what humans actually did with it.

---

## Rules

- **Namespace your ops.** `kind` must start with your plugin id.
- **Do not add tables.** Use the document store or your own blobs. If you truly
  need a table, that is a core change, discussed as one.
- **Do not interpret another plugin's payload** without depending on it out loud.
- **Long work is a job.** A request that takes seconds is a bug.
- **Say what you do not know.** A generator that abstains where its evidence is
  weak is worth more than one that always answers.
- **Refuse out loud.** If you reject an edit, say why in a sentence the person
  can act on ("3 is joined to 1+2+3 — unjoin it first"). A key that silently
  does nothing is worse than an error, because it gets pressed again.
- **Add an invariant, not just a test.** `tests/invariants.mjs` runs over the
  whole capture and every registered tool, including yours. A check there is
  worth several example tests, and `tests/mutants.py` will tell you whether it
  can actually fail.
