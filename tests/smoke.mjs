// Browser smoke test: boot the real client against a running server in jsdom,
// with a recording 2D context, and check that the app gets as far as painting
// masks. It catches the class of mistake a syntax check cannot — a wrong
// property name, a missing registration, a renderer that never draws.
//
//   .venv/bin/python -m quorum serve &      # or QUORUM_URL=... for a remote one
//   node tests/smoke.mjs
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const BASE = process.env.QUORUM_URL || 'http://127.0.0.1:8600';
const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');

const problems = [];
const note = (m) => problems.push(m);

// frames the invariants sample; loading them first means entriesAt has data
const SAMPLE_LOAD_FRAMES = [54, 1150, 4400, 6900, 8500];

const dom = new JSDOM(readFileSync(resolve(root, 'web/index.html'), 'utf8'), {
  url: `${BASE}/#/`, pretendToBeVisual: true, runScripts: 'outside-only',
});
const { window } = dom;

// ---- a canvas that records instead of rasterising -------------------------
const calls = { drawImage: 0, fillRect: 0, strokeRect: 0, fillText: 0, putImageData: 0 };
const ctx2d = new Proxy({
  canvas: null, globalAlpha: 1, font: '', fillStyle: '', strokeStyle: '', lineWidth: 1,
  measureText: () => ({ width: 10 }),
  getImageData: (x, y, w, h) => ({ data: new Uint8ClampedArray(w * h * 4) }),
}, {
  get(t, k) {
    if (k in t) return t[k];
    return (...a) => { if (k in calls) calls[k]++; return undefined; };
  },
  set(t, k, v) { t[k] = v; return true; },
});
window.HTMLCanvasElement.prototype.getContext = function () { return ctx2d; };
window.ImageData = class { constructor(w, h) { this.width = w; this.height = h; this.data = new Uint8ClampedArray(w * h * 4); } };
window.ResizeObserver = class { observe() {} disconnect() {} };
window.HTMLMediaElement.prototype.play = function () { return Promise.resolve(); };
window.HTMLMediaElement.prototype.pause = function () {};
window.HTMLElement.prototype.setPointerCapture = function () {};
window.HTMLElement.prototype.releasePointerCapture = function () {};
Object.defineProperty(window.HTMLElement.prototype, 'clientWidth', { get() { return 1200; } });
Object.defineProperty(window.HTMLElement.prototype, 'clientHeight', { get() { return 600; } });
window.Element.prototype.getBoundingClientRect = function () {
  return { x: 0, y: 0, left: 0, top: 0, right: 1200, bottom: 600, width: 1200, height: 600 };
};
class FakeSocket {
  constructor(url) { this.url = url; this.readyState = 1; FakeSocket.last = this;
    setTimeout(() => this.onopen?.(), 0); }
  send() {} close() {}
}
window.WebSocket = FakeSocket;

// Relative URLs go to the live server. Plugin ES modules are declared as
// absolute URLs (`/plugins/x/web/x.js`), which a browser resolves against the
// origin and node resolves against the filesystem — so the manifest is rewritten
// to file:// paths here. That is a harness detail, not a difference in the app.
const realFetch = globalThis.fetch;

// Retry once on a *connection* failure. uvicorn drops idle keep-alive sockets
// after 5 s, and a check that spends that long computing before its next
// request gets handed the dead one. A browser retries this transparently;
// node's fetch surfaces it as "fetch failed", which would make the suite
// flaky in a way that has nothing to do with the app.
const fetchRetrying = async (url, o) => {
  for (let attempt = 0; ; attempt++) {
    try { return await realFetch(url, o); }
    catch (e) {
      const transient = /fetch failed|other side closed|socket hang up|ECONNRESET/i
        .test(`${e.message} ${e.cause?.message || ''}`);
      if (!transient || attempt >= 2) throw e;
      await new Promise((r) => setTimeout(r, 60 * (attempt + 1)));
    }
  }
};

window.fetch = async (u, o) => {
  const url = String(u).startsWith('http') ? String(u) : BASE + u;
  const res = await fetchRetrying(url, o);
  if (!url.includes('/api/session')) return res;
  const body = await res.json();
  for (const m of body.plugins) {
    if (m.web) m.web = `file://${root}${m.web}`;
  }
  return new globalThis.Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
};

for (const k of ['window', 'document', 'location', 'history', 'localStorage',
  'requestAnimationFrame', 'cancelAnimationFrame', 'HTMLElement', 'Node', 'Element',
  'CustomEvent', 'Event', 'ImageData', 'WebSocket', 'getComputedStyle', 'ResizeObserver',
  'MutationObserver',
  'devicePixelRatio', 'fetch']) {
  try { globalThis[k] = window[k]; }
  catch { Object.defineProperty(globalThis, k, { value: window[k], configurable: true }); }
}
globalThis.getComputedStyle = () => ({ getPropertyValue: () => '#888' });

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const step = async (what, fn) => {
  try { await fn(); console.log(`  ok   ${what}`); }
  catch (e) { note(`${what}: ${e.message}`); console.log(`  FAIL ${what}: ${e.message}`); }
};

window.addEventListener('error', (e) => note(`window error: ${e.message}`));
process.on('unhandledRejection', (e) => note(`unhandled rejection: ${e?.message || e}`));

// ---- boot ----------------------------------------------------------------
const app = (await import(resolve(root, 'web/core/app.js'))).default;

// Wait for boot to *finish*, not for 700 ms to pass.
//
// `app.start()` is a session fetch plus a dynamic import of every plugin's ES
// module, over HTTP. On a warm server that is comfortably under 700 ms; on a
// cold one — which is exactly what `tests/run.sh` produces, because it starts
// the server and runs this immediately — it is not, and the next five checks
// fail with "no session", "identity not loaded", "no renderer for mask.rle"
// and an empty lobby. Five failures, none of them about the app, all of them
// looking like a real regression until you have chased them once.
//
// Polling for the condition costs nothing when it is already true and removes
// the whole class. The timeout is the honest failure: boot did not happen.
const bootedBy = Date.now() + 20000;
while (Date.now() < bootedBy) {
  if (app.session?.user && app.plugins.plugins.size && document.getElementById('main')?.childNodes.length) break;
  await wait(25);
}

await step('session loaded', () => {
  if (!app.session?.user) throw new Error('no session');
});
await step('plugins activated', () => {
  const ids = [...app.plugins.plugins.keys()];
  for (const need of ['identity', 'proposals', 'mask_layer']) {
    if (!ids.includes(need)) throw new Error(`${need} not loaded`);
  }
  if (app.plugins.problems.length) throw new Error(app.plugins.problems.join('; '));
});
await step('tools registered', () => {
  if (!app.plugins.tools.has('identity')) throw new Error('identity tool missing');
  if (!app.plugins.tools.has('review')) throw new Error('review tool missing');
});
await step('mask renderer registered', () => {
  if (!app.plugins.renderers.has('mask.rle')) throw new Error('no renderer for mask.rle');
});
await step('lobby lists the capture', () => {
  const txt = window.document.getElementById('main').textContent;
  if (!/Capture/.test(txt)) throw new Error(`main is: ${txt.slice(0, 120)}`);
});

// The capture with the most annotation layers, not simply the first: a fresh
// draft somebody made this morning sorts above the real one and would turn
// every check below into "no layer data", which reads as a broken client.
const allCaps = (await (await window.fetch('/api/captures')).json()).captures;
const capId = [...allCaps].sort((a, b) => (b.n_layers - a.n_layers) || (b.n_frames - a.n_frames))[0]?.id;
window.location.hash = `#/c/${capId}/identity`;
await app.route();

// Wait for the thing being tested, not for a number of milliseconds. Opening a
// capture fetches a frame map *and* a timestamp table per stream and then the
// objects of every layer; how long that takes is the machine's business, and a
// fixed wait here is how this suite starts failing at random on a busy laptop.
const until = async (what, ok, ms = 15000) => {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    if (ok()) return;
    await wait(100);
  }
  throw new Error(`timed out waiting for ${what}`);
};
await until('the capture to open', () => app.store.get('capture')?.streams?.length);
await until('layer objects to load', () => (app.primaryData()?.objects.size || 0) > 0);

await step('capture opened', () => {
  const cap = app.store.get('capture');
  if (!cap) throw new Error('no capture in the store');
  if (!cap.streams.length) throw new Error('no streams');
  if (!cap.streams[0].frameMap?.length) throw new Error('no frame map');
});
await step('layer data loaded', () => {
  const d = app.primaryData();
  if (!d) throw new Error('no layer data');
  if (!d.objects.size) throw new Error('no objects');
});

app.setFrame(4400);
await until('the frame window at 4400', () => app.primaryData()?.activeAtRaw(4400).length > 0);
await step('masks are active at frame 4400', () => {
  const d = app.primaryData();
  const n = d.activeAtRaw(4400).length;
  if (!n) throw new Error('activeAt returned nothing — window fetch or frame map is wrong');
  console.log(`       ${n} masks active, ${d.objects.size} objects total`);
});
await step('the overlay actually paints', () => {
  app.viewport.invalidate();
  app.viewport.draw();
  if (!calls.drawImage) throw new Error('the mask renderer drew no images');
  console.log(`       drawImage×${calls.drawImage}  strokeRect×${calls.strokeRect}  fillText×${calls.fillText}`);
});
await step('identity state folded', async () => {
  const st = await (await window.fetch(`/api/p/identity/${capId}/state`)).json();
  if (!st.cids.length) throw new Error('no identities');
  console.log(`       ${st.cids.length} identities`);
});
await step('identity is a colour mode, available in any tool', () => {
  // It used to be a styler that only fired while the Identity tab was open, so
  // its colouring could not be asked for anywhere else or refused while it was.
  const d = app.primaryData();
  const e = app.entriesAt(4400)[0] || d.activeAtRaw(4400)[0];
  if (!e) throw new Error('nothing on screen at frame 4400');
  if (!app.display.modes.has('identity')) throw new Error('identity registered no colour mode');
  const before = app.display.mode;
  app.display.setMode('track');
  const byTrack = app.display.paint(e, { color: '#fff', alpha: 0.4, label: 'x' });
  app.display.setMode('identity');
  const byIdentity = app.display.paint(e, { color: '#fff', alpha: 0.4, label: 'x' });
  app.display.setMode('class');
  const byClass = app.display.paint(e, { color: '#fff', alpha: 0.4, label: 'x' });
  app.display.setMode(before || 'track');
  if (byTrack.color === byIdentity.color && byIdentity.label === byTrack.label) {
    throw new Error('the identity mode changed neither colour nor label');
  }
  if (!byClass.label.includes(e.object.label || 'unlabelled')) {
    throw new Error(`colouring by class does not name the class: ${byClass.label}`);
  }
});

await step('a hidden class is hidden at every door', async () => {
  // The one that matters: not "the filter works", but "nothing can get around
  // it". Drawing, hit testing, selection and navigation are checked, plus the
  // filter the server is told about.
  const label = app.display.labels[0]?.label;
  if (!label) { console.log('       (this capture has no labels — skipped)'); return; }
  const f = app.store.get('frame');
  const before = app.entriesAt(f);
  const victim = before.find((e) => e.object.label === label);
  if (!victim) throw new Error(`no ${label} on screen at frame ${f}`);
  const box = app.viewport.renderers.get(victim.object.layer_id)?.boundsOf(victim, app.viewport.viewFor(null));

  app.display.setLabelHidden(label, true);
  try {
    if (app.entriesAt(f).some((e) => e.object.label === label)) throw new Error('door 1: still drawn');
    if (box && app.viewport.pickAll(box.x + box.w / 2, box.y + box.h / 2)
        .some((e) => e.object.label === label)) throw new Error('door 2: still hit-testable');
    const r = app.select([victim.object.id], 'set');
    if (r.selected !== 0) throw new Error('door 3: still selectable');
    const rev = await app.reveal([victim.object.id], {});
    if (rev.shown.length) throw new Error('door 4: reveal still pointed at it');
    if (app.store.get('selection').has(victim.object.id)) throw new Error('it stayed selected');
    if (!(app.display.filter.labels?.exclude || []).includes(label)) {
      throw new Error('the server is not told which class is hidden');
    }
    if (app.visibleObjects().some((o) => o.label === label)) {
      throw new Error('visibleObjects still counts it');
    }
  } finally {
    app.display.setLabelHidden(label, false);
  }
  if (!app.entriesAt(f).some((e) => e.object.label === label)) {
    throw new Error('unhiding did not bring it back');
  }
  console.log(`       "${label}" hidden and restored; 4 doors held`);
});
await step('inspector renders for the active tool', () => {
  app.renderInspector();
  const txt = window.document.getElementById('inspector').textContent;
  for (const need of ['Identity', 'Layers', 'Jobs']) {
    if (!txt.includes(need)) throw new Error(`inspector lacks ${need}: ${txt.slice(0, 160)}`);
  }
});
await step('review tool renders its queue', async () => {
  app.activateTool('review');
  await wait(500);
  app.renderInspector();
  const txt = window.document.getElementById('inspector').textContent;
  if (!/Review queue/.test(txt)) throw new Error(`no queue panel: ${txt.slice(0, 160)}`);
  if (!/open \d+/.test(txt)) throw new Error('queue counts missing');
});
await step('transport paints the timeline', () => {
  app.timeline.paint();
  if (!calls.fillRect) throw new Error('timeline drew nothing');
});
await step('keyboard: Space toggles playback', () => {
  const before = app.store.get('playing');
  window.dispatchEvent(new window.KeyboardEvent('keydown', { key: ' ', bubbles: true }));
  if (app.store.get('playing') === before) throw new Error('Space did nothing');
  app.pause();
});
// ---- selection and navigation (the three gestures asked for) -------------
app.activateTool('identity');
await wait(200);

let stack = [];
await step('hit testing reports the whole stack, not just the top', () => {
  const d = app.primaryData();
  const view = app.viewport.viewFor(null);
  const entries = d.activeAtRaw(app.store.get('frame'));
  if (!entries.length) throw new Error('nothing active to hit-test');
  const r = app.viewport.renderers.get(d.layer.id);
  const boxes = entries.map((e) => r.boundsOf(e, view)).filter(Boolean);
  if (!boxes.length) throw new Error('no drawable bounds');
  let best = [];
  // Where two boxes overlap is where a stack can exist; sample that rectangle
  // rather than hoping a mask centre lands on another mask.
  const rects = [];
  for (let i = 0; i < boxes.length && rects.length < 60; i++) {
    for (let j = i + 1; j < boxes.length; j++) {
      const a = boxes[i], b = boxes[j];
      const x = Math.max(a.x, b.x), y = Math.max(a.y, b.y);
      const w = Math.min(a.x + a.w, b.x + b.w) - x, hh = Math.min(a.y + a.h, b.y + b.h) - y;
      if (w > 1 && hh > 1) rects.push({ x, y, w, h: hh });
    }
  }
  const probe = rects.length ? rects : boxes;
  outer: for (const box of probe) {
    for (let sy = 1; sy < 6; sy++) {
      for (let sx = 1; sx < 6; sx++) {
        const hits = app.viewport.pickAll(box.x + box.w * sx / 6, box.y + box.h * sy / 6);
        if (hits.length > best.length) best = hits;
        if (best.length > 1) break outer;
      }
    }
  }
  stack = best;
  console.log(`       ${boxes.length} masks drawn, ${rects.length} overlapping box pair(s)`);
  if (!best.length) throw new Error('pickAll found nothing over any mask');
  console.log(`       deepest stack found: ${best.length} mask(s)`);
});
await step('alt-click cycles down the stack', () => {
  const fake = [{ object: { id: 1 } }, { object: { id: 2 } }, { object: { id: 3 } }];
  const vp = app.viewport;
  vp.stackAt = null;
  const a = vp.cycle(fake), b = vp.cycle(fake), c = vp.cycle(fake);
  if (a.object.id !== 2) throw new Error('the first alt-click must go one deeper, got ' + a.object.id);
  if (b.object.id !== 3 || c.object.id !== 1) throw new Error('cycling did not wrap');
  if (vp.cycle([]) !== null) throw new Error('an empty stack must yield null');
});
await step('Ctrl+click takes one track where a plain click takes the identity', () => {
  const pick = app.plugins.pickHandlers.get('identity');
  if (!pick) throw new Error('identity registered no pick handler');
  const d = app.primaryData();
  // an entry whose track has an identity shared with at least one other track
  const view = app.viewport.viewFor(null);
  const entries = d.activeAtRaw(app.store.get('frame'));
  let hit = null;
  for (const e of entries) {
    if (!app.display.visible(e)) continue;
    // the identity colour mode labels an assigned track `key → cid`
    const styled = app.display.modes.get('identity')?.labelOf(e) || '';
    if (/→ \d+/.test(styled)) { hit = e; break; }
  }
  if (!hit) throw new Error('no entry with an identity at this frame');
  const ev = { hit, stack: [hit], alt: false, event: { ctrlKey: false, shiftKey: false } };
  pick(ev);
  const wide = app.store.get('selection').size;
  pick({ ...ev, event: { ctrlKey: true, shiftKey: false } });
  const narrow = app.store.get('selection').size;
  if (narrow !== 1) throw new Error(`Ctrl+click selected ${narrow} tracks, not 1`);
  if (wide < 1) throw new Error('a plain click selected nothing');
  console.log(`       plain click ${wide} track(s), Ctrl+click ${narrow}`);
});
await step('Shift+Space walks to a frame that needs a human, and rings it', async () => {
  // The walk needs identity's folded state; without it there is nothing to
  // call a problem and the frame never moves.
  await until('identity state', () => app.store.get('tool') === 'identity');
  app.setFrame(0);
  await wait(200);
  // Ask the server whether this capture has anything to walk to, *before*
  // pressing the key.
  //
  // Without this the check has one failure mode for two unrelated causes: the
  // walk is broken, or this fixture simply has no problem frames. Both look
  // like "timed out waiting for the walk to move the frame", and when the
  // suite was accidentally pointed at a capture with no identities the real
  // Shift+Space bug sat in that pile looking exactly like the noise around it.
  // A precondition that names itself is the difference between a red line you
  // investigate and one you learn to skip.
  const seen = await (await window.fetch(
    `/api/p/identity/${capId}/problems?frame=1&direction=1&limit=1`)).json();
  if (!seen.problems?.length) {
    console.log(`       (this capture has no problem frames — ${seen.spans} live span(s), ` +
                'nothing for the walk to stop on — skipped)');
    return;
  }
  window.dispatchEvent(new window.KeyboardEvent('keydown',
    { key: ' ', shiftKey: true, bubbles: true }));
  // The walk asks the server, then loads the masks it is about to ring, so how
  // long it takes depends on the machine. Poll rather than guess — a fixed
  // wait here made this the one test that failed at random.
  // The walk moves the frame, then loads the masks it is about to ring, and
  // only then rings them — so waiting for the frame is not waiting for the
  // ring. Wait for the thing being asserted.
  await until('the walk to move the frame', () => app.store.get('frame') !== 0);
  await until('the ring', () => app.viewport.pulses?.length);
  const f = app.store.get('frame');
  if (!app.store.get('selection').size) throw new Error('the walk selected nothing');
  if (!app.viewport.pulses?.length) throw new Error('no ring was pulsed at the offered mask');
  console.log(`       landed on frame ${f}, ${app.store.get('selection').size} track(s) ringed`);
  app.viewport.draw();                       // the ring must survive a paint
});
await step('the identity panel states the scope of a destructive op', () => {
  app.renderInspector();
  const txt = window.document.getElementById('inspector').textContent;
  if (!/a click selects/.test(txt)) throw new Error('no scope switch in the panel');
  if (!/selected: \d+ track/.test(txt)) throw new Error(`no selection scope line: ${txt.slice(0, 200)}`);
});

// ---- mask editing ---------------------------------------------------------
await step('the edit tool is in the rail', () => {
  if (!app.plugins.tools.has('edit')) throw new Error('no edit tool');
});
await step('edited tracks replace the imported ones rather than double-drawing', async () => {
  const st = await (await window.fetch(`/api/p/masks/${capId}/state`)).json();
  const shadowed = Object.entries(st.shadow || {}).flatMap(([s, ks]) => ks.map((k) => [s, k]));
  if (!shadowed.length) { console.log('       (no edits replayed on this capture — skipped)'); return; }
  if (!st.derivedLayer) throw new Error('shadowing without a derived layer');
  app.activateTool('edit');
  await wait(400);
  const src = app.data.get(st.sourceLayer);
  let checkedRaw = 0, checkedDerived = 0;
  for (let f = 0; f < 9000 && checkedRaw < 3; f += 137) {
    for (const e of src.activeAtRaw(f)) {
      if (!shadowed.some(([s, k]) => s === e.stream.key && k === e.object.key)) continue;
      // Superseded copies are a registered structural filter now, not a
      // styler's `hidden`, so the authority is who to ask.
      if (app.display.visible(e)) throw new Error(`${e.stream.key}/${e.object.key} is edited but still drawn from the import`);
      checkedRaw++;
    }
    if (checkedRaw) break;
  }
  const der = app.data.get(st.derivedLayer);
  if (!der?.objects.size) throw new Error('the derived layer has no tracks');
  for (const o of der.objects.values()) { checkedDerived++; }
  console.log(`       ${shadowed.length} shadowed, ${checkedDerived} edited tracks, ${checkedRaw} verified hidden`);
  if (!checkedRaw) throw new Error('never found a shadowed track on screen to check');
});
await step('the edit inspector offers the structural edits', () => {
  app.activateTool('edit');
  app.renderInspector();
  const txt = window.document.getElementById('inspector').textContent;
  for (const need of ['Delete', 'Cut here', 'Join', 'Unjoin', 'History']) {
    if (!txt.includes(need)) throw new Error(`edit panel lacks ${need}`);
  }
});
await step('a joined track can be selected and acted on', async () => {
  // The bug this pins: identity looked up the selection in one layer, so a
  // joined track (`1+2+3`, which lives in the derived layer) selected fine and
  // then every identity operation reported "nothing selected".
  const st = await (await window.fetch(`/api/p/masks/${capId}/state`)).json();
  if (!st.derivedLayer) { console.log('       (no edits on this capture — skipped)'); return; }
  const der = app.data.get(st.derivedLayer);
  const joined = [...der.objects.values()].find((o) => o.key.includes('+'));
  const cut = [...der.objects.values()].find((o) => o.key.includes('@'));
  if (!joined) throw new Error('no joined track to test with');
  for (const [what, o] of [['joined', joined], ['cut', cut]]) {
    if (!o) continue;
    if (!app.objectOf(o.id)) throw new Error(`objectOf cannot find the ${what} track`);
    if (app.findObject(o.stream, o.key)?.id !== o.id) {
      throw new Error(`findObject cannot resolve ${o.stream}/${o.key}`);
    }
    app.activateTool('identity');
    app.select([o.id], 'set');
    app.renderInspector();
    const txt = window.document.getElementById('inspector').textContent;
    if (!/selected: 1 track/.test(txt)) {
      throw new Error(`selecting the ${what} track ${o.key} yielded no actionable selection`);
    }
  }
  console.log(`       joined ${joined.key}${cut ? `, cut ${cut.key}` : ''} both selectable`);
});
await step('Ctrl+click on a joined track selects just it', async () => {
  const st = await (await window.fetch(`/api/p/masks/${capId}/state`)).json();
  if (!st.derivedLayer) return;
  const der = app.data.get(st.derivedLayer);
  const joined = [...der.objects.values()].find((o) => o.key.includes('+'));
  if (!joined) return;
  app.setFrame(joined.first_frame + 2);
  await wait(400);
  const entry = der.activeAtRaw(app.store.get('frame')).find((e) => e.object.id === joined.id);
  if (!entry) { console.log('       (join not drawable at its own first frame — skipped)'); return; }
  const pick = app.plugins.pickHandlers.get('identity');
  pick({ hit: entry, stack: [entry], alt: false, event: { ctrlKey: true, shiftKey: false } });
  const sel = app.store.get('selection');
  if (sel.size !== 1 || !sel.has(joined.id)) throw new Error(`Ctrl+click gave ${sel.size} tracks`);
  app.renderInspector();
  if (!/selected: 1 track/.test(window.document.getElementById('inspector').textContent)) {
    throw new Error('the joined track is selected but identity cannot act on it');
  }
});
await step('a key in two layers resolves to the copy that is actually drawn', async () => {
  // The first half of a cut track keeps its original key, so `86` exists in the
  // imported layer *and* in the derived one. Resolving it to the imported copy
  // selects something invisible — which is what made group select look broken.
  const st = await (await window.fetch(`/api/p/masks/${capId}/state`)).json();
  if (!st.derivedLayer) { console.log('       (no edits — skipped)'); return; }
  const der = app.data.get(st.derivedLayer);
  const src = app.data.get(st.sourceLayer);
  const dupes = [];
  for (const o of der.objects.values()) {
    const twin = (src.byStream.get(o.stream) || []).find((x) => x.key === o.key);
    if (twin) dupes.push([o, twin]);
  }
  if (!dupes.length) { console.log('       (no duplicated keys — skipped)'); return; }
  for (const [derived, imported] of dupes) {
    const got = app.findObject(derived.stream, derived.key);
    if (got.id !== derived.id) {
      throw new Error(`${derived.stream}/${derived.key} resolved to the imported copy ` +
        `(${imported.id}) instead of the edited one (${derived.id})`);
    }
  }
  console.log(`       ${dupes.length} key(s) live in both layers, all resolve to the edited copy`);
});
await step('group select never picks a track that is not on screen', async () => {
  // Plain click takes the whole identity. Every member it selects must be a
  // track someone can actually see and act on.
  const st = await (await window.fetch(`/api/p/masks/${capId}/state`)).json();
  if (!st.derivedLayer) return;
  const der = app.data.get(st.derivedLayer);
  const joined = [...der.objects.values()].find((o) => o.key.includes('+'));
  if (!joined) return;
  app.activateTool('identity');
  app.setFrame(joined.first_frame + 2);
  await wait(400);
  const entry = der.activeAtRaw(app.store.get('frame'))?.find((e) => e.object.id === joined.id);
  if (!entry) { console.log('       (join not drawable here — skipped)'); return; }
  const pick = app.plugins.pickHandlers.get('identity');
  pick({ hit: entry, stack: [entry], alt: false, event: { ctrlKey: false, shiftKey: false } });
  const sel = [...app.store.get('selection')];
  if (sel.length < 2) throw new Error(`group select gave ${sel.length} track(s); expected the identity`);
  const visible = new Set(app.visibleObjects().map((o) => o.id));
  const ghosts = sel.filter((id) => !visible.has(id)).map((id) => {
    const o = app.objectOf(id);
    return o ? `${o.stream}/${o.key}` : `#${id}`;
  });
  if (ghosts.length) throw new Error(`selected ${ghosts.length} invisible track(s): ${ghosts.slice(0, 4).join(', ')}`);
  app.renderInspector();
  const txt = window.document.getElementById('inspector').textContent;
  if (!new RegExp(`selected: ${sel.length} tracks`).test(txt)) {
    throw new Error(`the panel does not report the selection: ${txt.slice(0, 200)}`);
  }
  console.log(`       identity of ${joined.key}: ${sel.length} tracks, all on screen`);
});
await step('the walk never stops on a track that was cut, joined or deleted', async () => {
  const st = await (await window.fetch(`/api/p/masks/${capId}/state`)).json();
  // Superseded is a fact about a *copy*, not about a key. A cut leaves `86` in
  // the imported layer and `86` + `86@25` in the derived one: the imported `86`
  // must never be offered, and the derived `86` is a live track that may well
  // need an identity. Keying this set by stream/key alone cannot tell them
  // apart, so it demanded the walk hide both — which is the bug it was meant to
  // be guarding against, written into the guard.
  const superseded = new Set();      // only in the layer that stopped drawing them
  for (const [s, keys] of Object.entries(st.shadow || {})) {
    for (const k of keys) superseded.add(`${s}/${k}`);
  }
  const gone = new Set();            // deleted or purged: in every layer
  for (const m of [st.deleted, st.purged]) {
    for (const [s, keys] of Object.entries(m || {})) for (const k of keys) gone.add(`${s}/${k}`);
  }
  const r = await (await window.fetch(`/api/p/identity/${capId}/problems?frame=0&limit=9999`)).json();
  const byId = new Map();
  for (const d of app.data.values()) {
    for (const o of d.objects.values()) byId.set(o.id, { ...o, layer: d.layer.id });
  }
  const bad = [];
  for (const p of r.problems) {
    for (const oid of p.objects) {
      const o = byId.get(oid);
      if (!o) continue;
      const name = `${o.stream}/${o.key}`;
      if (gone.has(name)) bad.push(`${name} (deleted)`);
      else if (st.sourceLayer && o.layer === st.sourceLayer && superseded.has(name)) {
        bad.push(`${name} (the replaced copy)`);
      }
    }
  }
  if (bad.length) throw new Error(`the walk offers superseded tracks: ${bad.slice(0, 4).join(', ')}`);
  console.log(`       checked a ${r.problems.length}-problem batch over ${r.spans} live spans, ` +
              `${superseded.size} replaced + ${gone.size} deleted skipped`);
});

await step('history lists ops with who did them', async () => {
  await app.loadHistory();
  if (!app.history?.length) throw new Error('no history');
  const o = app.history[0];
  if (!o.actor || !o.kind) throw new Error('history rows lack attribution');
  console.log(`       ${app.history.length} ops, newest ${o.kind} by ${o.actor}`);
});

await step('a refused edit says why, in the app, not just the console', async () => {
  // The server refuses a cut on a joined track with a readable reason. If the
  // client swallows it, the key silently does nothing — which is worse than an
  // error, because the user re-presses it.
  const st = await (await window.fetch(`/api/p/masks/${capId}/state`)).json();
  const stream = Object.keys(st.joins || {}).find((s) => (st.joins[s] || []).length);
  if (!stream) { console.log('       (no joins on this capture — skipped)'); return; }
  const member = st.joins[stream][0][0];

  const direct = await window.fetch(`/api/p/masks/${capId}/op`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ kind: 'split', payload: { stream, key: member, frame: 100 } }),
  });
  if (direct.status !== 409) throw new Error(`cutting a joined track returned ${direct.status}, not 409`);
  const body = await direct.json();
  if (!/unjoin/i.test(body.detail || '')) throw new Error(`the reason is not actionable: ${body.detail}`);

  // …and now through the client, which must show it rather than doing nothing.
  const target = app.findObject(stream, member);
  if (!target) throw new Error(`cannot find ${stream}/${member} to click`);
  const data = app.layerData((l) => l.id === target.layer_id);
  // park the playhead inside the track, or the cut is refused locally for a
  // different (also correct) reason and this proves nothing
  const mid = Math.floor((target.first_frame + target.last_frame) / 2);
  app.setFrame(data.unmapFrame(stream, mid));
  await wait(300);

  // …and the client can no longer even ask. A member of a join has been
  // superseded by the joined track, so the display authority refuses to select
  // it (door 3) — which is a better answer than the 409, because it happens
  // before the user has pressed anything destructive. The server's refusal
  // stays as the guarantee for anything that is not this client.
  const overlays = window.document.getElementById('overlays');
  const before = overlays.textContent;      // never clear it: the toast host lives here
  app.activateTool('edit');
  const r = app.select([target.id], 'set');
  if (r.selected !== 0 || r.dropped !== 1) {
    throw new Error(`a superseded join member was selectable: ${JSON.stringify(r)}`);
  }
  const shown = overlays.textContent.slice(before.length);
  if (!/hidden|skipped/i.test(shown)) {
    throw new Error(`the skip was silent: "${shown.slice(0, 200)}"`);
  }
  console.log(`       server said: ${body.detail.slice(0, 70)}`);
});

await step('a client that has fallen behind is told why, not left with a dead key', async () => {
  // With the display authority closing the four doors, none of the structural
  // 409s can be *asked for* by an up-to-date client — you cannot select a
  // superseded track, so you cannot try to cut one. That does not make the
  // client's refusal handling dead code: it makes it the multi-user path.
  //
  // Two people, one capture. She joins two tracks; his client has not heard
  // yet, so it still shows one of them and lets him aim a cut at it. The
  // server refuses with a reason, and if his client swallows that, the key
  // does nothing and he presses it again. Simulated here by closing the
  // socket, which is exactly what being behind means.
  const plain = new Map();
  for (const o of app.visibleObjects((l) => l.type === 'mask.rle')) {
    if (!/^\d+$/.test(o.key)) continue;
    if (!plain.has(o.stream)) plain.set(o.stream, []);
    plain.get(o.stream).push(o);
  }
  const pair = [...plain.values()].find((v) => v.length >= 2);
  if (!pair) { console.log('       (no two plain tracks to join — skipped)'); return; }
  const [a, b] = pair;

  app.socket.close();                                   // he stops hearing about her edits
  const made = await (await window.fetch(`/api/p/masks/${capId}/op`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ kind: 'join', payload: { stream: a.stream, keys: [a.key, b.key] } }),
  })).json();
  try {
    const data = app.layerData((l) => l.id === a.layer_id);
    const mid = Math.floor((a.first_frame + a.last_frame) / 2);
    app.setFrame(data.unmapFrame(a.stream, mid));
    await wait(250);
    const overlays = window.document.getElementById('overlays');
    // Match on the whole toast area, not on a suffix: an older toast expiring
    // mid-step shortens the text and a slice would silently come back empty.
    const had = /cannot apply|unjoin/i.test(overlays.textContent);
    app.activateTool('edit');
    const sel = app.select([a.id], 'set');
    if (!sel.selected) throw new Error('the stale client could not even select it — setup is wrong');
    await app.keymap.commands.get('masks.split').run({ key: 'T' });
    await wait(900);
    const shown = overlays.textContent;
    if (had || !/cannot apply|unjoin/i.test(shown)) {
      throw new Error(`the refusal never reached the user: "${shown.replace(/\s+/g, ' ').slice(-200)}"`);
    }
    console.log(`       stale client told: ${shown.replace(/\s+/g, ' ').slice(0, 80)}…`);
  } finally {
    if (made?.op?.id) {
      await window.fetch(`/api/captures/${capId}/ops/${made.op.id}/undo`, { method: 'POST' });
      const still = await (await window.fetch(
        `/api/captures/${capId}/ops?all=true&limit=400`)).json();
      const mine = still.ops.find((o) => o.id === made.op.id);
      if (mine && !mine.undone) note('the stale-client test left its join in the capture');
    }
    await window.fetch(`/api/p/masks/${capId}/materialise`, { method: 'POST' });
    await app.openCapture(capId);
    app.activateTool('identity');
    await wait(700);
  }
});

await step('a job whose requirements are unmet is refused by the server, with the reason', async () => {
  // The greyed-out rail button is a courtesy. This is the guarantee — and it
  // holds for the CLI and for another plugin, not only for this client.
  const res = await window.fetch('/api/jobs', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ plugin: 'voxel_carve', kind: 'suggest',
                           params: { capture_id: capId }, capture_id: capId }),
  });
  if (res.status !== 409) throw new Error(`an unmet requirement returned ${res.status}, not 409`);
  const body = await res.json();
  if (!/calibration/i.test(body.detail || '')) {
    throw new Error(`the refusal does not name what is missing: ${body.detail}`);
  }
  // and the tool is greyed rather than gone, with that same reason attached
  await app.loadReadiness();
  const why = app.blockedReason({ id: 'carve', plugin: 'voxel_carve' });
  if (!/calibration/i.test(why)) throw new Error(`the rail does not explain it: "${why}"`);
  console.log(`       ${body.detail.slice(0, 80)}…`);
});

await step('the gear on a capture card opens the capture workspace, not just the capture', async () => {
  // Reported from the running app: it opened the capture and landed on the
  // annotation workspace, so "settings" appeared to do nothing.
  location.hash = '#/';
  await app.route();
  await wait(400);
  const main = window.document.getElementById('main');
  const gear = [...main.querySelectorAll('button')].find((b) => /⚙/.test(b.textContent));
  if (!gear) throw new Error('no settings button on a capture card');
  gear.click();
  for (let i = 0; i < 60 && app.store.get('tool') !== 'capture'; i++) await wait(100);
  if (app.store.get('tool') !== 'capture') {
    throw new Error(`the gear left the app on tool "${app.store.get('tool')}" ` +
                    `and hash "${location.hash}"`);
  }
  app.renderInspector();
  if (!window.document.getElementById('inspector').textContent.includes('clock')) {
    throw new Error('the capture workspace opened without its Streams panel');
  }
  // and it must be tellable apart from every other workspace at a glance
  const top = window.document.getElementById('topbar').textContent;
  if (!/Capture/.test(top)) throw new Error('the top bar does not name the workspace');
  console.log('       gear → capture workspace, named in the top bar, with the clock controls');
});

// ---- the mosaic, the clocks, and the capture workspace --------------------
await step('the mosaic is composed from the streams, not from a file', () => {
  const comp = app.viewport.comp;
  const cap = app.store.get('capture');
  const enabled = cap.streams.filter((st) => st.enabled !== 0);
  if (comp.cells.size !== enabled.length) {
    throw new Error(`${comp.cells.size} cells for ${enabled.length} streams`);
  }
  for (const cell of comp.cells.values()) {
    if (!cell.el.isConnected) throw new Error(`${cell.stream.key} has no element in the scene`);
    const c = app.viewport.cellOf(cell.stream.key);
    if (!c || c.w <= 0) throw new Error(`${cell.stream.key} has no cell rectangle`);
  }
  console.log(`       ${comp.cells.size} cells composed` +
    (comp.mosaic ? ' over a legacy mosaic rendition' : ' with no mosaic file at all'));
});

await step('zooming a camera asks for more resolution, and zooming out gives it back', () => {
  // The level-of-detail decision is one function, so this drives *that* rather
  // than a key: every zoom path — wheel, hold-Z, fit, resize — ends up here.
  const comp = app.viewport.comp;
  const cell = [...comp.cells.values()][0];
  const wide = cell.choose(cell.spec.w * 0.2, true);          // whole grid on screen
  const close = cell.choose(cell.spec.w * 8, true);           // one camera filling the window
  const off = cell.choose(cell.spec.w * 8, false);            // scrolled out of view
  if (off !== null) throw new Error('a cell that is off screen still wants a decoder');
  if (!close) throw new Error('zoomed in, a cell asked for nothing at all');
  const bigger = (a, b) => (a?.kind === 'video' ? a.w : Infinity) >= (b?.kind === 'video' ? b.w : 0);
  if (cell.renditions.length) {
    if (!bigger(close, wide)) throw new Error('zooming in did not ask for a larger rendition');
    console.log(`       ${cell.stream.key}: ${wide?.id ?? 'mosaic'} → ${close.id} when zoomed`);
  } else {
    // No browser-playable copy of this camera exists yet. Zooming must still do
    // something better than magnifying the mosaic: a full-resolution still.
    if (close.kind !== 'still') {
      throw new Error(`no rendition and no still — zoom gave ${JSON.stringify(close)}`);
    }
    console.log(`       ${cell.stream.key}: no rendition yet, so zoom asks for a ` +
      `${close.w}px still cut from the source`);
  }
});

await step('playback never asks the server for stills, and never for more than a few', async () => {
  // Reported from the running app as flicker, masks trailing the video, and
  // the picture "catching up". The cause was here: zoomed past 1.15×, every
  // cell wanted a full-resolution still, each one an ffmpeg seek on the server
  // — six of them measured at 858 ms — refreshed every 400 ms *during
  // playback*. The mask windows queued behind them.
  const comp = app.viewport.comp;
  const cells = [...comp.cells.values()];
  const zoomed = (c) => c.spec.w * 40;            // far past any rendition or mosaic
  const wasPlaying = app.store.get('playing');

  comp.playing = true;
  const whilePlaying = cells.map((c) => c.choose(zoomed(c), true));
  if (comp.mosaic && whilePlaying.some((w) => w?.kind === 'still')) {
    throw new Error('a still was requested during playback — that is the flicker and the lag');
  }

  comp.playing = false;
  const whilePaused = cells.map((c) => c.choose(zoomed(c), true));
  const wantStills = whilePaused.filter((w) => w?.kind === 'still').length;
  if (cells.length && !cells[0].renditions.length && !wantStills) {
    throw new Error('paused and zoomed right in, no cell offered a full-resolution still');
  }

  // …and even paused, the budget holds: the update pass is what enforces it.
  const realScale = app.viewport.scale;
  app.viewport.scale = 40;
  comp.update(true);
  const onStills = cells.filter((c) => c.current?.kind === 'still').length;
  app.viewport.scale = realScale;
  comp.playing = wasPlaying;
  comp.update(true);
  if (onStills > 2) throw new Error(`${onStills} cells on stills at once; the budget is 2`);
  console.log(`       playing → ${whilePlaying.filter((w) => w?.kind === 'still').length} stills, ` +
    `paused and zoomed → ${onStills} (budget 2)`);
});

await step('the timeline does not repaint every lane on every frame', async () => {
  // Six streams of activity, every selected track and every plugin's lane
  // decorator, redrawn 25 times a second, was most of a frame's budget on this
  // capture — and a frame budget overrun is what makes the masks trail.
  let decorated = 0;
  const spy = () => { decorated++; };
  app.plugins.laneDecorators.push(spy);
  const N = 60, FROM = 4400;
  try {
    // Walk the range once and let it settle *before* counting. A mask window
    // arriving is a `data` event and a `data` event legitimately rebuilds the
    // lanes, so counting during the first pass measures how fast the machine
    // fetched — which is why this read as flaky (1 rebuild here, 4 on a loaded
    // box) while testing nothing about the timeline at all. Warmed, the
    // property is exact: replaying frames the client already has must rebuild
    // *nothing*.
    for (let f = FROM; f < FROM + N; f++) { app.setFrame(f); }
    await wait(600);
    app.setFrame(FROM);
    await wait(300);

    // Counted from zero on each side: the warm-up above paints too, and folding
    // it into the denominator is how a mutation that dirties the lanes on every
    // frame escaped — 360 warm-up calls made 360 real ones look like "one".
    decorated = 0;
    app.timeline.lanesDirty = true;
    app.timeline.paint();                          // one full lane rebuild
    const perStream = decorated;                   // one pass = one call per stream
    if (!perStream) throw new Error('the lanes were never painted at all');

    decorated = 0;
    for (let f = FROM; f < FROM + N; f++) { app.setFrame(f); }
    await wait(200);
    const rebuilds = decorated / perStream;
    // The property is that rebuilds do not *scale* with frames. Warmed, this is
    // normally 0; a window that arrives late still legitimately rebuilds once
    // or twice, and a threshold tight enough to catch that is measuring the
    // machine rather than the timeline. Three is a ceiling noise does not
    // reach and the bug clears twentyfold — `timeline_repaints_lanes_every_frame`
    // makes it 60.
    if (rebuilds > 3) {
      throw new Error(`${N} frames the client already had caused ` +
                      `${rebuilds} full lane rebuild(s)`);
    }
    console.log(`       ${N} frames of playback → ${rebuilds} lane rebuild(s), not ${N}`);
  } finally {
    app.plugins.laneDecorators = app.plugins.laneDecorators.filter((f) => f !== spy);
  }

  // …and reopening a capture must not leave the old one's subscriptions behind
  // to do it all again. Each leaked set is another full pass per frame.
  const countSubs = () => [...app.store.subs.values()].reduce((n, set) => n + set.size, 0);
  const before = countSubs();
  await app.openCapture(capId);
  await wait(600);
  const after = countSubs();
  if (after > before) {
    throw new Error(`reopening left ${after - before} extra store subscription(s) behind`);
  }
  app.activateTool('identity');
  await wait(300);
  console.log(`       reopening a capture: ${before} store subscriptions before and after`);
});

await step('a clock offset moves the masks and the pixels together, and Ctrl+Z takes it back', async () => {
  const cap = app.store.get('capture');
  const st = cap.streams.find((x) => x.has_timestamps);
  if (!st) { console.log('       (no stream has timestamps — skipped)'); return; }
  await app.api.patchStream(cap.id, st.key, { offset: 0 });   // a previous run may have left one
  await app.refreshCapture();
  Object.assign(st, app.store.get('capture').streams.find((x) => x.key === st.key));
  const was = st.frameMap[4400];
  const wasTime = app.viewport.comp.cells.get(st.key)?.timeFor(4400, { t_offset: 0 });

  await app.api.patchStream(cap.id, st.key, { offset: 2 });
  await app.refreshCapture();
  const now = app.store.get('capture').streams.find((x) => x.key === st.key);
  const moved = now.frameMap[4400];
  if (moved === was) throw new Error('a 2 s offset did not move the frame map at all');
  const nowTime = app.viewport.comp.cells.get(st.key)?.timeFor(4400, { t_offset: 0 });
  if (Math.abs((wasTime - nowTime) - 2) > 0.2) {
    throw new Error(`the pixels moved ${(wasTime - nowTime).toFixed(2)}s, the masks moved with ` +
                    'the map — they must move by the same 2 s');
  }

  const undone = await app.undo('core.');
  if (!undone) throw new Error('the offset was not an op, so Ctrl+Z could not reach it');
  await app.refreshCapture();
  const back = app.store.get('capture').streams.find((x) => x.key === st.key);
  if (back.frameMap[4400] !== was || back.time_offset !== 0) {
    throw new Error(`undo left the offset at ${back.time_offset} and frame ${back.frameMap[4400]}`);
  }
  console.log(`       ${st.key}: +2 s moved source frame ${was} → ${moved}, undo put it back`);
});

await step('the capture workspace edits everything it says it does', async () => {
  app.activateTool('capture');
  await wait(200);
  app.renderInspector();
  const txt = window.document.getElementById('inspector').textContent;
  for (const need of ['Capture', 'Streams', 'Data', 'Layers', 'Display']) {
    if (!txt.includes(need)) throw new Error(`the workspace has no ${need} panel: ${txt.slice(0, 200)}`);
  }
  if (!/clock/.test(txt)) throw new Error('no clock control on the Streams panel');
  const cap = app.store.get('capture');
  const renamed = `${cap.name} ·`;
  await app.api.patchCapture(cap.id, { name: renamed });
  await app.refreshCapture();
  if (app.store.get('capture').name !== renamed) throw new Error('renaming a capture did nothing');
  await app.api.patchCapture(cap.id, { name: cap.name });
  await app.refreshCapture();
  console.log('       overview, streams, data, layers — and the rename round-tripped');
  app.activateTool('identity');
  await wait(200);
});

await step('the upload dialog is built from what plugins declared, and nothing else', async () => {
  const { kinds } = await app.api.assets(app.store.get('capture').id);
  if (!kinds.length) throw new Error('no asset kinds are declared at all');
  const owned = new Set(kinds.map((k) => k.plugin));
  for (const k of kinds) {
    if (!k.id.startsWith(`${k.plugin}.`)) throw new Error(`${k.id} is not namespaced to ${k.plugin}`);
    if (!k.title) throw new Error(`${k.id} has no title to show`);
  }
  // the core must not know any of them by name
  const core = await (await window.fetch('/core/upload.js')).text();
  for (const k of kinds) {
    if (core.includes(k.id)) throw new Error(`the upload code names ${k.id} — it must not`);
  }
  console.log(`       ${kinds.length} kind(s) from ${owned.size} plugin(s), none named in core`);
});

// ---- invariants ----------------------------------------------------------
// The steps above each pin one thing that once went wrong. These hold over the
// whole capture and every registered tool, so they catch the *next* one too.
const { checkAll, CHECKS } = await import(resolve(here, 'invariants.mjs'));

const invariantStep = async (where) => {
  for (const f of SAMPLE_LOAD_FRAMES) { app.setFrame(f); await wait(120); }
  app.setFrame(4400);
  await wait(250);
  await step(`invariants hold ${where}`, async () => {
    const bad = await checkAll(app, window.fetch, where);
    if (bad.length) throw new Error(`\n         · ${bad.join('\n         · ')}`);
    console.log(`       ${CHECKS.length} invariants over ${app.visibleObjects().length} visible tracks`);
  });
};

await invariantStep('after load');

// The same invariants must survive an edit and its undo — the moment when the
// derived layer, the shadow set and the client's caches can most easily fall
// out of step with each other.
await step('an edit and its undo leave the app consistent', async () => {
  const before = app.visibleObjects().length;
  const target = app.visibleObjects((l) => l.type === 'mask.rle')
    .find((o) => !o.key.includes('+') && !o.key.includes('@'));
  if (!target) throw new Error('no plain track to edit');

  const post = (kind, payload) => window.fetch(`/api/p/masks/${capId}/op`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ kind, payload }),
  }).then((r) => r.json());

  const r = await post('delete', { stream: target.stream, keys: [target.key] });
  if (!r.op) throw new Error('the edit produced no op');
  // From here the capture is modified, and this runs against the real one. The
  // undo belongs in a `finally`: an assertion that throws in between used to
  // leave the delete in place, and fifteen of those had quietly accumulated on
  // one track before anybody noticed.
  try {
    app.bus.emit('op', r.op);
    await wait(700);
    if (app.visibleObjects().length !== before - 1 && app.visibleObjects().length !== before) {
      throw new Error(`deleting one track changed the visible count by ` +
        `${app.visibleObjects().length - before}`);
    }
    const mid = await checkAll(app, window.fetch, 'mid-edit');
    if (mid.length) throw new Error(`after the delete:\n         · ${mid.join('\n         · ')}`);
  } finally {
    const undone = await window.fetch(`/api/captures/${capId}/ops/${r.op.id}/undo`,
      { method: 'POST' }).then((x) => x.json());
    app.bus.emit('op', undone.op);
    await wait(900);
    await app.reloadLayers();
  }
  const after = app.visibleObjects().length;
  if (after !== before) throw new Error(`undo left ${after} visible tracks, started with ${before}`);
  console.log(`       ${before} tracks → delete → undo → ${after}, invariants held throughout`);
});

await invariantStep('after an edit and undo');

await step('SAM batches prompt dots until explicitly sent', async () => {
  // The points are the prompt. Stacking a second one where you already put
  // one is never what somebody means, and without this the only way to undo a
  // misplaced negative is to throw the whole prompt away and start again.
  const handler = app.plugins.pickHandlers.get('sam');
  if (!handler) throw new Error('the sam tool registered no pick handler');
  const cell = (app.store.get('capture').layout?.cells || [])[0];
  if (!cell) throw new Error('the capture has no cells to click in');
  const prev = app.store.get('tool');
  app.activateTool('sam');

  // Asking the model must be an explicit action. Counting these calls proves
  // dots stay local until Ctrl+Enter, and that removing one does not start a
  // second expensive inference.
  const real = globalThis.fetch;
  let asked = 0;
  globalThis.fetch = (url, opt) => {
    if (String(url).includes('/interact')) asked++;
    return real(url, opt);
  };
  const world = { x: cell.x + cell.w / 2, y: cell.y + cell.h / 2 };
  const event = { ctrlKey: false, shiftKey: false, altKey: false, clientX: 0, clientY: 0 };
  const click = () => handler({ hit: null, stack: [], alt: false, event, world, cell,
                                right: false });
  try {
    click();
    await wait(250);
    if (asked) throw new Error(`one prompt dot made ${asked} model request(s) before Send prompts`);
    window.dispatchEvent(new window.KeyboardEvent('keydown',
      { key: 'Enter', ctrlKey: true, bubbles: true }));
    await wait(250);
    if (!asked) throw new Error('Ctrl+Enter did not send the prompt to SAM');
    const afterSend = asked;
    click();                       // the very same point removes it
    await wait(250);
    if (asked !== afterSend) throw new Error('removing a prompt dot made another model request');
    console.log(`       dots sent 0 request(s) before Ctrl+Enter, then ${afterSend} request(s)`);
  } finally {
    globalThis.fetch = real;
    if (prev) app.activateTool(prev);
  }
});

await step('command palette lists plugin commands', () => {
  app.activateTool('review');            // the palette shows the active tool's keys
  app.keymap.palette();
  const txt = window.document.getElementById('overlays').textContent;
  if (!/Accept this proposal/.test(txt)) throw new Error('review commands missing from the palette');
  window.document.querySelector('.modal-scrim')?.remove();
});

await step('an export can actually be taken off the server', async () => {
  // Exporting wrote files into data/exports/ and returned a path on the
  // server's disk, which is not an answer to "how do I get my annotations".
  const cap = app.store.get('capture');
  const before = (await app.api.exports(cap.id)).exports.length;

  const io = (app.manifests || []).flatMap((m) => (m.provides.io || [])
    .filter((x) => x.direction === 'export').map((x) => ({ ...x, plugin: m.id })))
    .find((x) => x.plugin === 'identity');
  if (!io) { console.log('       (no identity exporter — skipped)'); return; }
  const { job } = await app.api.submit({ plugin: io.plugin, kind: `export:${io.id}`,
                                         params: { capture_id: cap.id }, capture_id: cap.id });
  let j = null;
  for (let i = 0; i < 100; i++) {
    j = (await app.api.req(`/api/jobs/${job.id}`)).job;
    if (['done', 'failed', 'cancelled'].includes(j.state)) break;
    await wait(100);
  }
  if (j.state !== 'done') throw new Error(`the export ${j.state}: ${j.message}`);

  const { exports: files } = await app.api.exports(cap.id);
  if (files.length < Math.max(1, before)) throw new Error('the export is not listed');
  const one = files[0];
  const res = await window.fetch(app.api.exportUrl(one.path));
  if (!res.ok) throw new Error(`downloading ${one.name} gave ${res.status}`);
  const body = await res.arrayBuffer();
  if (!body.byteLength) throw new Error(`${one.name} downloaded as zero bytes`);

  // …and only from the exports directory.
  const escape = await window.fetch(app.api.exportUrl('../quorum.db'));
  if (escape.ok) throw new Error('a relative path reached outside the exports directory');
  console.log(`       ${files.length} export(s) listed; ${one.name} came down as ` +
    `${body.byteLength} bytes; ../ refused`);
});

await step('files already on the server can be reached without an upload', async () => {
  // The other half of uploading. On the box that matters the recordings are
  // already mounted, so the question is not "can I send this file" but "can the
  // server be pointed at the one it can already open" — and, just as important,
  // can it be pointed at *only* those.
  const roots = await app.api.browse();
  if (!roots.entries.length) throw new Error('no media roots listed to browse');
  const root = roots.entries[0].path;
  const here = await app.api.browse(root);
  if (!here.entries.length) throw new Error(`${root} listed nothing at all`);
  if (here.parent && !here.at_root) throw new Error('the root offered a parent above itself');

  // The boundary is the media roots, and it is the same one that guards
  // serving. A picker that could list /etc would be a file-disclosure bug with
  // a friendly UI on it.
  const out = await window.fetch('/api/browse?path=/etc');
  if (out.ok) throw new Error('browsing reached /etc — outside the media roots');
  const up = await window.fetch(`/api/browse?path=${encodeURIComponent(`${root}/../../..`)}`);
  if (up.ok) throw new Error('.. walked out of the media roots');

  // Filtering happens on the server, because `records` is one flat directory
  // with ~1,400 files in it and the filter is how you find the six of a session.
  const dirs = here.entries.filter((e) => e.dir);
  const filtered = await app.api.browse(root, 'zzz-nothing-matches-this');
  if (filtered.entries.filter((e) => !e.dir).length) throw new Error('the filter matched nothing but returned files');
  if (filtered.entries.filter((e) => e.dir).length !== dirs.length) {
    throw new Error('the filter hid directories — you can no longer navigate through them');
  }

  // A linked file is registered, not copied, and deleting the row must never
  // delete the recording.
  const capId2 = app.store.get('capture').id;
  const file = here.entries.find((e) => !e.dir && e.readable);
  if (!file) { console.log('       (no readable file at the root — link half skipped)'); return; }
  const { asset } = await app.api.linkAsset(capId2, file.path);
  if (!asset.linked) throw new Error('a linked asset did not say it was linked');
  if (!asset.present) throw new Error('a file that is right there reported as missing');
  if (asset.state !== 'ready') throw new Error(`a linked asset is ${asset.state}, not ready`);
  await app.api.deleteAsset(asset.id);
  const after = await window.fetch(`/api/browse?path=${encodeURIComponent(root)}`);
  const still = (await after.json()).entries.some((e) => e.path === file.path);
  if (!still) throw new Error(`deleting the link deleted ${file.name} off the server`);
  console.log(`       ${here.entries.length} entries under one root; /etc and .. refused; ` +
    `linked ${file.name} without copying it, and unlinking left it alone`);
});

await step('the suite leaves the capture as it found it', async () => {
  // These run against a real capture with real work in it. An edit left behind
  // by a run that failed halfway is not a test problem, it is data loss with a
  // long fuse — fifteen stacked deletes on one track got here that way.
  const { ops } = await (await window.fetch(
    `/api/captures/${capId}/ops?all=true&limit=1000`)).json();
  const mine = ops.filter((o) => o.kind.startsWith('masks.') && !o.undone
    && !('imported_from' in (o.payload || {})));
  if (mine.length) {
    throw new Error(`${mine.length} edit(s) left behind: ` +
      mine.slice(0, 3).map((o) => `#${o.id} ${o.kind}`).join(', '));
  }
  console.log('       no edits left behind');
});

// Print what they were. A bare count told nobody anything, and an unhandled
// rejection with no message is exactly the kind of thing this suite exists to
// notice.
if (problems.length) {
  console.log(`\n${problems.length} problem(s):`);
  for (const p of problems) console.log(`  · ${p}`);
} else {
  console.log('\nall good');
}
process.exit(problems.length ? 1 : 0);
