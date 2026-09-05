// Whole-dataset invariants for the running client.
//
// The example tests in smoke.mjs each pin one thing that once went wrong. That
// is not enough on its own: every one of them was written *after* the bug was
// found by hand, and they all passed while group select was still selecting
// tracks nobody could see.
//
// These are different. They say nothing about any particular track and check
// every object, every identity, every tool — so a plugin added next month gets
// checked without anyone writing a test for it, and the answers are derived
// from the live server's real capture rather than a fixture.
//
// Each check returns null when it holds, or a string saying what broke.

const SAMPLE_FRAMES = [54, 1150, 4400, 6900, 8500];

const idOf = (o) => `${o.stream}/${o.key}`;

function visibleSet(app) {
  return new Set(app.visibleObjects().map((o) => o.id));
}

// -- 1 ----------------------------------------------------------------------
// Two layers may both hold a key (the first half of a cut track keeps its
// original key), but only one of them may be *drawn*. If both are, the same
// person is on screen twice and every count in the app is wrong.
function oneVisibleObjectPerKey(app) {
  const seen = new Map();
  for (const o of app.visibleObjects()) {
    const k = idOf(o);
    if (seen.has(k)) {
      return `${k} is drawn from two layers at once ` +
             `(objects ${seen.get(k).id} and ${o.id})`;
    }
    seen.set(k, o);
  }
  return null;
}

// -- 2 ----------------------------------------------------------------------
// Looking a track up by name must find the copy that is on screen. Returning
// the superseded one selects something invisible, which is how group select
// broke: it was resolving `3` to the imported original after `1+2+3` replaced
// it.
function lookupsResolveToTheDrawnCopy(app) {
  const bad = [];
  for (const o of app.visibleObjects()) {
    const got = app.findObject(o.stream, o.key);
    if (!got) { bad.push(`${idOf(o)} resolves to nothing`); continue; }
    if (got.id !== o.id) bad.push(`${idOf(o)} resolves to #${got.id}, not the drawn #${o.id}`);
    if (app.objectOf(o.id)?.id !== o.id) bad.push(`objectOf lost #${o.id}`);
  }
  return bad.length ? `${bad.length} bad lookup(s): ${bad.slice(0, 3).join('; ')}` : null;
}

// -- 3 ----------------------------------------------------------------------
// Nothing may be painted twice on one frame. This is the drawing-side twin of
// invariant 1 and catches a styler that stops hiding what it should.
function nothingIsDrawnTwice(app) {
  for (const f of SAMPLE_FRAMES) {
    if (f >= (app.store.get('capture')?.n_frames || 0)) continue;
    const seen = new Map();
    for (const layer of app.visibleLayers()) {
      const d = app.data.get(layer.id);
      if (!d || !d.loaded(f)) continue;
      for (const e of app.entriesAt(f, layer)) {
        const k = `${e.stream.key}/${e.object.key}`;
        if (seen.has(k)) return `frame ${f}: ${k} is painted by two layers`;
        seen.set(k, e);
      }
    }
  }
  return null;
}

// -- 4 ----------------------------------------------------------------------
// Whatever a click does, it may only ever select tracks that are on screen.
// Every tool is checked, including ones that did not exist when this was
// written — which is the point.
function everyToolSelectsOnlyVisibleTracks(app) {
  const visible = visibleSet(app);
  const before = new Set(app.store.get('selection'));
  const tool = app.store.get('tool');
  const problems = [];
  // Selecting normally repaints the inspector and the overlay. This check makes
  // hundreds of selections and cares about none of that — rendering is covered
  // by its own steps — so stub both out or the suite costs a minute and a half
  // and stops getting run.
  const realRender = app.renderInspector.bind(app);
  const realInvalidate = app.viewport?.invalidate?.bind(app.viewport);
  app.renderInspector = () => {};
  if (app.viewport) app.viewport.invalidate = () => {};
  try {
    for (const [toolId, handler] of app.plugins.pickHandlers) {
      app.activateTool(toolId);
      for (const f of SAMPLE_FRAMES) {
        if (f >= (app.store.get('capture')?.n_frames || 0)) continue;
        const entries = app.entriesAt(f).filter((e) => app.data.get(e.object.layer_id)?.loaded(f) !== false);
        for (const entry of entries.slice(0, 20)) {
          app.clearSelection();
          const click = (alt) => handler({
            hit: entry, stack: [entry], alt,
            event: { ctrlKey: false, shiftKey: false, altKey: alt, clientX: 0, clientY: 0 } });
          try {
            click(false);
            // A plain click does not have to *select* — in the SAM workspace it
            // adds a prompt point, and moving the selection out from under a
            // half-finished prompt would be the bug, not the fix. What every
            // tool must have is *some* gesture that selects, and alt+click is
            // the one the whole app shares (it is how you reach the mask
            // underneath). So the rule is checked through whichever of the two
            // actually selects, and the ghost test below — the part that
            // matters — applies to both.
            if (![...app.store.get('selection')].length) click(true);
          } catch (e) {
            problems.push(`${toolId} threw on ${idOf(entry.object)}: ${e.message}`);
            continue;
          }
          const sel = [...app.store.get('selection')];
          if (!sel.length) {
            problems.push(`${toolId}: neither clicking nor alt-clicking ` +
                          `${idOf(entry.object)} selected anything`);
            continue;
          }
          const ghosts = sel.filter((id) => !visible.has(id));
          if (ghosts.length) {
            const names = ghosts.map((id) => {
              const o = app.objectOf(id);
              return o ? idOf(o) : `#${id}`;
            });
            problems.push(`${toolId}: clicking ${idOf(entry.object)} also selected ` +
              `${ghosts.length} track(s) that are not on screen: ${names.slice(0, 3).join(', ')}`);
          }
          if (problems.length > 4) break;
        }
        if (problems.length > 4) break;
      }
      if (problems.length > 4) break;
    }
  } finally {
    app.renderInspector = realRender;
    if (app.viewport && realInvalidate) app.viewport.invalidate = realInvalidate;
    app.clearSelection();
    if (before.size) app.select([...before], 'set');
    if (tool) app.activateTool(tool);
    app.renderInspector();
  }
  return problems.length ? `${problems.length} problem(s): ${problems.slice(0, 3).join(' | ')}` : null;
}

// -- 5 ----------------------------------------------------------------------
// A queued proposal must talk about tracks that still exist. A generator that
// argues with an edit a human already made is worse than one that says nothing.
async function proposalsReferenceLiveTracks(app, fetch) {
  const cap = app.store.get('capture');
  const r = await (await fetch(`/api/p/proposals/${cap.id}?state=open`)).json();
  const visible = visibleSet(app);
  const bad = [];
  for (const p of r.items || []) {
    for (const oid of p.focus?.objects || []) {
      if (!visible.has(oid)) {
        const o = app.objectOf(oid);
        bad.push(`${p.id} points at ${o ? idOf(o) : `#${oid}`}`);
      }
    }
  }
  return bad.length
    ? `${bad.length} of ${r.items.length} open proposal(s) name a track that is gone: ` +
      bad.slice(0, 3).join('; ')
    : null;
}

// -- 6 ----------------------------------------------------------------------
// What the server says is exportable must be exactly what the client draws.
// The two are computed independently — one from the op log, one from stylers —
// so agreement is real evidence rather than a tautology.
async function serverAndClientAgreeOnWhatExists(app, fetch) {
  const cap = app.store.get('capture');
  let r;
  try { r = await (await fetch(`/api/p/masks/${cap.id}/effective`)).json(); }
  catch { return null; }                       // masks not installed: nothing to compare
  if (!r?.objects) return null;
  const server = new Set(r.objects.map((o) => `${o.stream}/${o.key}`));
  const client = new Set(app.visibleObjects((l) => l.type === 'mask.rle').map(idOf));
  const onlyServer = [...server].filter((k) => !client.has(k));
  const onlyClient = [...client].filter((k) => !server.has(k));
  // deleted tracks are drawn (tagged DEL) but not exported, so they may differ
  // in that one direction only
  // The one legitimate difference, in one direction only: a deleted track is
  // still drawn (dimmed, tagged DEL, so you can undo it) but is not exported.
  const st = await (await fetch(`/api/p/masks/${cap.id}/state`)).json();
  const deleted = new Set(Object.entries(st.deleted || {})
    .flatMap(([s, ks]) => ks.map((k) => `${s}/${k}`)));
  const unexplainedClient = onlyClient.filter((k) => !deleted.has(k));
  if (unexplainedClient.length) {
    return `the client draws ${unexplainedClient.length} track(s) the server would not ` +
      `export, and they are not deleted: ${unexplainedClient.slice(0, 3).join(', ')}`;
  }
  if (onlyServer.length) {
    return `the server would export ${onlyServer.length} track(s) the client never draws: ` +
      onlyServer.slice(0, 3).join(', ');
  }
  return null;
}

// -- 7 ----------------------------------------------------------------------
// Identity membership is a selection like any other: it may not name a track
// that is not on screen, and it may not lose one that is.
async function identityMembershipMatchesWhatIsDrawn(app, fetch) {
  const cap = app.store.get('capture');
  const st = await (await fetch(`/api/p/identity/${cap.id}/state`)).json();
  const visible = new Map(app.visibleObjects((l) => l.type === 'mask.rle').map((o) => [idOf(o), o.id]));
  const bad = [];
  for (const [stream, m] of Object.entries(st.assignments || {})) {
    for (const key of Object.keys(m)) {
      const k = `${stream}/${key}`;
      if (!visible.has(k)) continue;           // superseded assignments are expected
      if (app.findObject(stream, key)?.id !== visible.get(k)) {
        bad.push(`${k} is drawn but identity resolves it elsewhere`);
      }
    }
  }
  return bad.length ? `${bad.length}: ${bad.slice(0, 3).join('; ')}` : null;
}

// ---------------------------------------------------------------------------
// -- 8 ----------------------------------------------------------------------
// The one the class filter was designed around. Hiding something must hold at
// every door for *every registered tool*, including tools that did not exist
// when this was written — which is the point of putting the rule in the core
// instead of asking each feature to remember it.
//
// It hides a real class, then tries every way an object reaches a human:
// drawing, hit testing, selection (through each tool's own pick handler), and
// navigation. Anything that gets through is a leak.
async function hidingHoldsAtEveryDoor(app) {
  const label = app.display.labels[0]?.label;
  if (!label) return null;                       // nothing labelled: nothing to check
  const tool = app.store.get('tool');
  const before = new Set(app.store.get('selection'));
  const realRender = app.renderInspector.bind(app);
  const realInvalidate = app.viewport?.invalidate?.bind(app.viewport);
  app.renderInspector = () => {};
  if (app.viewport) app.viewport.invalidate = () => {};
  app.display.setLabelHidden(label, true);
  try {
    for (const f of SAMPLE_FRAMES) {
      if (f >= (app.store.get('capture')?.n_frames || 0)) continue;
      const leaked = app.entriesAt(f).find((e) => e.object.label === label);
      if (leaked) return `frame ${f}: ${idOf(leaked.object)} is drawn despite "${label}" being hidden`;
    }
    const victim = app.visibleObjects().length
      ? [...app.data.values()].flatMap((d) => [...d.objects.values()])
          .find((o) => o.label === label)
      : null;
    if (!victim) return null;
    for (const [toolId, handler] of app.plugins.pickHandlers) {
      app.activateTool(toolId);
      app.clearSelection();
      const entry = { object: victim, stream: { key: victim.stream } };
      try { handler({ hit: entry, stack: [entry], alt: false,
                      event: { ctrlKey: false, shiftKey: false, clientX: 0, clientY: 0 } }); }
      catch { /* a tool refusing outright is fine */ }
      const got = [...app.store.get('selection')];
      const bad = got.filter((id) => app.objectOf(id)?.label === label);
      if (bad.length) return `tool ${toolId} selected ${bad.length} hidden "${label}" track(s)`;
    }
    app.clearSelection();
    const rev = await app.reveal([victim.id], {});
    if (rev.shown.length) return `reveal pointed at a hidden "${label}" track`;
    const wire = app.display.filter.labels?.exclude || [];
    if (!wire.includes(label)) return `the server is never told that "${label}" is hidden`;
  } finally {
    app.display.setLabelHidden(label, false);
    app.renderInspector = realRender;
    if (app.viewport && realInvalidate) app.viewport.invalidate = realInvalidate;
    app.activateTool(tool);
    app.store.set({ selection: before });
  }
  return null;
}

// -- 9 ----------------------------------------------------------------------
// A stream's cached frame map must be exactly what its timestamps and its
// clock offset say it should be. The map is a *cache* of a derivation now, and
// a cache that has drifted from its source puts every mask on the wrong
// picture with nothing on screen to admit it.
async function frameMapsMatchTheirTimestamps(app, fetch) {
  const cap = app.store.get('capture');
  if (!cap) return null;
  for (const st of cap.streams) {
    if (!st.has_timestamps) continue;            // nothing to check it against
    const res = await fetch(`/api/captures/${cap.id}/streams/${st.key}/timestamps`);
    if (!res.ok) continue;
    const buf = await res.arrayBuffer();
    if (!buf.byteLength) continue;
    const stamps = new BigInt64Array(buf);
    const fm = st.frameMap;
    if (!fm || !fm.length) return `${st.key} has timestamps but no frame map`;
    const off = BigInt(Math.round((st.time_offset || 0) * 1e6));
    for (const f of [0, 1, 137, 4400, fm.length - 1]) {
      if (f < 0 || f >= fm.length) continue;
      const t = BigInt(Math.round((f / cap.fps) * 1e6)) - off;
      let lo = 0, hi = stamps.length;            // last index with stamps[i] <= t
      while (lo < hi) { const m = (lo + hi) >> 1; if (stamps[m] <= t) lo = m + 1; else hi = m; }
      const want = Math.max(0, Math.min(stamps.length - 1, lo - 1));
      if (fm[f] !== want) {
        return `${st.key} frame ${f}: the cached map says ${fm[f]}, the timestamps say ${want}`;
      }
    }
  }
  return null;
}

// -- 10 ---------------------------------------------------------------------
// The mosaic is a layout now, not a file, so the layout has to be right: every
// enabled stream gets exactly one cell, and no cell names a stream that is not
// there. A missing cell is a camera that silently stops being shown.
function cellsCoverEveryEnabledStream(app) {
  const cap = app.store.get('capture');
  if (!cap) return null;
  const cells = cap.layout?.cells || [];
  if (!cells.length) return null;                // a capture with no layout hint is allowed
  const enabled = cap.streams.filter((s) => s.enabled !== 0).map((s) => s.key);
  const named = cells.map((c) => c.stream);
  const dupes = named.filter((k, i) => named.indexOf(k) !== i);
  if (dupes.length) return `${dupes[0]} has two cells`;
  const missing = enabled.filter((k) => !named.includes(k));
  if (missing.length) return `${missing.join(', ')} enabled but not laid out`;
  const ghosts = named.filter((k) => !cap.streams.some((s) => s.key === k));
  if (ghosts.length) return `cell(s) for ${ghosts.join(', ')}, which are not streams`;
  for (const c of cells) {
    if (!(c.w > 0 && c.h > 0)) return `${c.stream}'s cell has no size`;
  }
  return null;
}

// -- 11 ---------------------------------------------------------------------
// The contract between the pixels and the annotations: the instant a cell is
// seeked to must be inside the very frame the frame map named, because that is
// the frame every mask on it was drawn against. A mask one frame out looks
// perfectly fine and is wrong, so nothing on screen would admit it.
//
// This is checked against the composition's own arithmetic, not a copy of it,
// so it also fails if somebody "simplifies" timeFor back to frame / fps.
function cellsSeekToTheFrameTheMapNames(app) {
  const comp = app.viewport?.comp;
  if (!comp) return null;
  for (const cell of comp.cells.values()) {
    const stamps = cell.stream.timestamps;
    const fm = cell.stream.frameMap;
    if (!stamps?.length || !fm?.length) continue;      // nothing to check against
    for (const f of SAMPLE_FRAMES) {
      if (f >= fm.length) continue;
      const want = fm[f];
      const t = cell.timeFor(f, { t_offset: 0 });
      // a <video> at time t shows the last frame whose pts <= t
      let lo = 0, hi = stamps.length;
      while (lo < hi) { const m = (lo + hi) >> 1; if (stamps[m] <= t) lo = m + 1; else hi = m; }
      const shown = Math.max(0, Math.min(stamps.length - 1, lo - 1));
      if (shown !== want) {
        return `${cell.stream.key} at capture frame ${f}: seeking to ${t.toFixed(4)}s shows ` +
               `source frame ${shown}, but the masks are on frame ${want}`;
      }
      if (cell.frameAt(t) !== f && fm[cell.frameAt(t)] !== want) {
        return `${cell.stream.key}: frameAt is not the inverse of timeFor at frame ${f}`;
      }
    }
  }
  return null;
}

export const CHECKS = [
  ['one visible object per (stream, key)', oneVisibleObjectPerKey],
  ['lookups resolve to the drawn copy', lookupsResolveToTheDrawnCopy],
  ['nothing is drawn twice', nothingIsDrawnTwice],
  ['every tool selects only visible tracks', everyToolSelectsOnlyVisibleTracks],
  ['open proposals reference live tracks', proposalsReferenceLiveTracks],
  ['server and client agree on what exists', serverAndClientAgreeOnWhatExists],
  ['identity membership matches what is drawn', identityMembershipMatchesWhatIsDrawn],
  ['hiding holds at every door, for every tool', hidingHoldsAtEveryDoor],
  ['frame maps match their timestamps and offset', frameMapsMatchTheirTimestamps],
  ['cells cover every enabled stream exactly once', cellsCoverEveryEnabledStream],
  ['a cell seeks to the frame the map names', cellsSeekToTheFrameTheMapNames],
];

/** Run every invariant. `where` labels the moment (after load, after an undo…). */
export async function checkAll(app, fetch, where = '') {
  const failures = [];
  for (const [name, fn] of CHECKS) {
    let msg;
    try { msg = await fn(app, fetch); }
    catch (e) { msg = `check itself threw: ${e.message}${e.cause ? ` (${e.cause.message || e.cause})` : ''}`; }
    if (msg) failures.push(`${name}${where ? ` (${where})` : ''}: ${msg}`);
  }
  return failures;
}
