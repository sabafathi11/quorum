// Boot and shell. Everything domain-specific you can see in the running app —
// masks, identities, proposals, the shape of a capture on disk — arrives from a
// plugin. This file knows about captures, streams, layers, frames and ops.
import { Api, Socket } from './api.js';
import { Store } from './store.js';
import { PluginHost, LayerData, Bus } from './host.js';
import { Viewport } from './viewport.js';
import { Timeline } from './timeline.js';
import { Keymap } from './keymap.js';
import { Display } from './display.js';
import { UploadQueue } from './upload.js';
import { capturePanels, newCaptureDialog, CAPTURE_TOOL } from './capture.js';
import { uploadDialog } from './upload.js';
import { displayPanel, displayButton } from './displayui.js';
import * as ui from './ui.js';
import { h, mount, clear, clamp, ago, bytes, keyColor, debounce } from './util.js';

class App {
  constructor() {
    this.api = new Api();
    this.bus = new Bus();
    this.store = new Store({
      frame: 0, playing: false, speed: 1, selection: new Set(), tool: null,
      capture: null, layerVis: {}, presence: [], jobs: [],
    });
    this.keymap = new Keymap(this.store);
    this.plugins = new PluginHost(this);
    // The one authority on what is on screen and what colour it is. Built
    // before any plugin loads, because plugins register colour modes and
    // filters into it during activate().
    this.display = new Display(this);
    this.uploads = new UploadQueue(this);
    this.data = new Map();          // layer id -> LayerData
    this.api.onError = (msg, status) => {
      if (status === 401) return this.askToken(msg);
      ui.toast('Request failed', msg, 'err');
    };
    this.toast = ui.toast;
    this.keyColor = keyColor;
  }

  // ------------------------------------------------------------------- boot
  async start() {
    this.applyTheme(localStorage.getItem('quorum.theme') || 'dark');
    let s;
    try { s = await this.api.session(); } catch { return; }
    this.session = s;
    this.manifests = s.plugins;
    document.title = 'Quorum';
    await this.plugins.loadAll(s.plugins);
    for (const p of s.pluginProblems || []) ui.toast('Plugin not loaded', p, 'warn');
    this.globalCommands();
    window.addEventListener('hashchange', () => this.route());
    await this.route();
  }

  askToken(msg) {
    ui.modal({
      title: 'This server needs a token',
      content: h('div', { class: 'field' },
        h('label', {}, 'Access token'),
        h('input', { class: 'txt', id: 'tok', placeholder: 'paste the token an admin gave you' }),
        h('div', { class: 'hint' }, msg)),
      ok: 'Save and reload',
      onOk: () => {
        const v = document.getElementById('tok').value.trim();
        if (!v) return false;
        localStorage.setItem('quorum.token', v);
        location.reload();
      },
    });
  }

  applyTheme(t) {
    document.documentElement.dataset.theme = t;
    localStorage.setItem('quorum.theme', t);
  }

  // Go somewhere, without depending on the hash event arriving, or on which
  // handler in a click's bubble path ran first. The hash is still set — the
  // back button has to work — but the work is done here and now, and `route`
  // is idempotent, so the hashchange that follows is a no-op.
  //
  // Reported from the running app: the gear on a capture card opened the
  // capture and landed on the annotation workspace instead of settings, so
  // "settings" appeared to do nothing. Routing by side effect is not worth
  // debugging twice.
  async goto(captureId, tool = '') {
    // Opening a capture takes long enough that a second navigation can start
    // while the first is still loading — click a card, change your mind, click
    // another. Whichever started last wins, and the older one stops touching
    // the UI the moment it notices. Without this the two interleave and the
    // loser renders a screen for a capture that is no longer open.
    const mine = this._nav = (this._nav || 0) + 1;
    const stale = () => this._nav !== mine;
    const want = `#/c/${captureId}${tool ? `/${tool}` : ''}`;
    if (location.hash !== want) location.hash = want;
    if (this.store.get('capture')?.id !== captureId) {
      await this.openCapture(captureId);
      if (stale()) return;
    }
    const cap = this.store.get('capture');
    if (!cap || cap.id !== captureId) return;
    const pick = tool || (!cap.streams.length ? CAPTURE_TOOL.id
      : [...this.plugins.tools.values()].sort((a, b) => a.order - b.order)[0]?.id);
    if (pick && pick !== this.store.get('tool')) this.activateTool(pick);
  }

  // ------------------------------------------------------------------ route
  async route() {
    const m = location.hash.match(/^#\/c\/(\d+)(?:\/([\w.-]+))?/);
    if (!m) { this._nav = (this._nav || 0) + 1; return this.lobby(); }
    return this.goto(parseInt(m[1], 10), m[2] || '');
  }

  // ------------------------------------------------------------------ lobby
  async lobby() {
    this.closeCapture();
    document.getElementById('app').classList.add('no-inspector');
    const { captures } = await this.api.captures();
    const providers = (this.manifests || []).flatMap((m) =>
      (m.provides.providers || []).map((p) => ({ ...p, plugin: m.id, pluginName: m.name })));

    mount(document.getElementById('topbar'),
      this.brand(),
      h('span', { class: 'dim' }, 'Captures'),
      h('span', { class: 'grow' }),
      ...this.topRight());
    mount(document.getElementById('rail'));
    mount(document.getElementById('transport'));

    const main = document.getElementById('main');
    const cards = captures.map((c) => h('div', {
      class: 'item', style: { padding: '12px 14px', textAlign: 'left' },
    },
      // The row is not clickable; the name is. Two buttons inside one big
      // click target is how "settings" turned into "open".
      h('button', {
        class: 'cardname grow', title: 'Open this capture',
        onclick: () => this.goto(c.id),
      },
        h('div', { class: 'mono', style: { fontSize: '14px', marginBottom: '3px' } }, c.name),
        h('div', { class: 'hint' }, c.state === 'draft'
          ? 'draft — nothing built yet'
          : `${c.n_streams} streams · ${c.n_frames.toLocaleString()} frames · ${c.fps} fps · ${c.n_layers} layer(s) · ${c.provider}`)),
      c.state === 'draft' ? h('span', { class: 'tag warn' }, 'draft') : null,
      h('button', { class: 'btn sm', title: 'Settings: name, clocks, files, layers',
        onclick: () => this.goto(c.id, CAPTURE_TOOL.id) }, '⚙ settings'),
      h('button', { class: 'btn sm danger', title: 'Delete this capture',
        onclick: async () => {
          if (!await ui.confirm(`Delete ${c.name}?`,
            'The capture, its streams, its annotations and its uploaded files. ' +
            'This cannot be undone.', 'Delete')) return;
          await this.api.deleteCapture(c.id);
          this.lobby();
        } }, '×')));

    mount(main, h('div', {
      style: { position: 'absolute', inset: '0', overflow: 'auto', padding: '28px 32px' },
    },
      h('div', { style: { maxWidth: '760px', margin: '0 auto', display: 'flex', flexDirection: 'column', gap: '14px' } },
        h('div', { class: 'row' },
          h('h2', { style: { margin: '0 0 2px', fontSize: '18px' }, class: 'grow' }, 'Captures'),
          h('button', { class: 'btn primary', onclick: () => newCaptureDialog(this) }, 'New capture')),
        h('div', { class: 'hint', style: { marginBottom: '6px' } },
          'A capture is N streams sharing one time base. Everything else about it — how it was recorded, what is drawn on it — belongs to a plugin.'),
        captures.length ? cards
          : ui.empty('Nothing here yet', h('div', { class: 'hint' },
              'Make a capture, upload its videos into it, and press Build.')),
        h('h3', { style: { margin: '18px 0 0', fontSize: '13px' } }, 'Other ways in'),
        h('div', { class: 'hint' },
          'These read files that are already on the server — press one and browse its disks. ' +
          'Nothing is uploaded and nothing is copied into the tree. New capture above is the ' +
          'other door: it takes the files from your machine.'),
        h('div', { class: 'row wrap' }, ...providers.map((p) => h('button', {
          class: 'btn sm', onclick: () => this.runProvider(p),
        }, `${p.title}`, h('span', { class: 'hint' }, p.pluginName)))))));
  }

  async runProvider(p) {
    const f = ui.form(p.params || {}, {}, { assets: this._assets?.assets || [], app: this });
    const go = await ui.modal({
      title: p.title, content: [h('div', { class: 'hint' }, p.description || ''), f.el], ok: 'Run',
    });
    if (!go) return;
    const { job } = await this.api.submit({ plugin: p.plugin, kind: `provider:${p.id}`, params: f.values });
    this.watchJob(job, () => this.lobby());
  }

  watchJob(job, done) {
    const t = ui.toast(job.kind, 'queued…');
    const poll = setInterval(async () => {
      const { job: j } = await this.api.req(`/api/jobs/${job.id}`);
      t.querySelector('.m').textContent = `${j.state} ${(j.progress * 100).toFixed(0)}% — ${j.message}`;
      if (['done', 'failed', 'cancelled'].includes(j.state)) {
        clearInterval(poll);
        t.classList.toggle('err', j.state === 'failed');
        done?.(j);
      }
    }, 400);
  }

  // ---------------------------------------------------------------- capture
  async openCapture(id) {
    this.closeCapture();
    const cap = await this.api.capture(id);
    // Both halves of "which picture is this": the map says which of a camera's
    // frames belongs to this capture frame, the timestamps say when that frame
    // is. A cell is seeked by the second, so the picture and the masks cannot
    // disagree. ~93 kB per stream, cached for a day; they never change.
    await Promise.all(cap.streams.map(async (st) => {
      st.frameMap = await this.api.frameMap(id, st.key);
      st.timestamps = await this.api.timestamps(id, st.key).catch(() => null);
    }));
    this.store.set({ capture: cap, frame: 0, selection: new Set(), playing: false });
    document.getElementById('app').classList.remove('no-inspector');

    const vis = {};
    for (const l of cap.layers) vis[l.id] = true;
    this.store.set({ layerVis: vis });

    // Before anything is drawn: what is on screen and how it is coloured have
    // to be settled first, or the first paint would show things the user hid
    // last time and then take them away.
    await this.display.attach(id);
    this.loadAssets();
    this.loadReadiness();
    this.loadExports();

    mount(document.getElementById('main'));
    // A draft has no streams and no timeline yet — showing an empty stage with
    // a scrubber for zero frames is worse than saying what to do next.
    if (!cap.streams.length) return this.draftScreen(cap);
    this.viewport = new Viewport(this, document.getElementById('main'));
    this.timeline?.destroy();
    this.timeline = new Timeline(this, document.getElementById('transport'));

    for (const l of cap.layers) {
      const d = new LayerData(this.api, l, cap.streams);
      this.data.set(l.id, d);
      d.loadObjects().then(() => { this.bus.emit('data', d); this.viewport?.invalidate(); });
    }
    await this.ensureData(0);

    this.socket = new Socket(this.api, id, {
      // Mark socket deliveries so a plugin can distinguish a confirmed local
      // edit from a genuinely remote one.  The server still sends the same op
      // to every client; this bit is only local browser metadata.
      op: (m) => { this.bus.emit('op', { ...m.op, _source: 'remote' }); },
      presence: (m) => this.store.set({ presence: m.users }),
      job: (m) => this.onJob(m.job),
      // Somebody else changed a clock, uploaded a file, or built a rendition.
      capture: () => this.refreshCapture(),
      streams: () => this.refreshCapture(),
      assets: () => this.loadAssets(),
      down: (tries) => {
        this.store.set({ presence: [] });
        this._link = { state: 'reconnecting', tries };
        this.renderTop();
      },
      state: (state) => {
        this._link = { ...(this._link || {}), state };
        this.renderTop();
      },
      // Ops that landed while the socket was away, replayed in order. They go
      // through the same `op` bus everything else does, so no plugin needs to
      // know a reconnect happened.
      resynced: (n) => {
        this._link = { ...(this._link || {}), stale: false };
        if (n) ui.toast('Caught up', `${n} edit(s) arrived while you were offline`);
        this.renderTop();
      },
      stale: () => { this._link = { ...(this._link || {}), stale: true }; this.renderTop(); },
      open: (reconnected) => {
        this._link = { state: 'open', tries: 0, stale: this._link?.stale || false };
        this.renderTop();
        this.api.jobs(id).then(({ jobs }) => this.store.set({ jobs })).catch(() => {});
        if (reconnected) this.afterReconnect();
      },
    });

    this.history = [];
    this.loadHistory();
    this.renderTop();
    this.renderRail();
    this.renderInspector();
    this.bus.emit('capture', cap);
    // Dropped again by `closeCapture`. Re-subscribing on every open without
    // unsubscribing means the second capture you look at does everything twice
    // per frame, the third three times, and nobody connects the stutter to
    // having opened a capture earlier.
    this.subs = [
      this.store.on(['frame'], () => {
        this.ensureData(this.store.get('frame'));
        this.viewport?.comp?.frameChanged(this.store.get('frame'));
      }),
      this.store.on(['selection', 'layerVis'], () => {
        this.viewport?.invalidate(); this.renderInspector();
      }),
      this.bus.on('display', () => { this.renderTop(); }),
    ];
  }

  // A capture with nothing in it yet. The whole first-run path lives here:
  // upload, then build.
  draftScreen(cap) {
    mount(document.getElementById('transport'));
    mount(document.getElementById('main'), h('div', {
      style: { position: 'absolute', inset: '0', overflow: 'auto', padding: '40px 32px' } },
      h('div', { style: { maxWidth: '620px', margin: '0 auto' } },
        ui.empty(`${cap.name} is empty`,
          h('div', { class: 'hint' },
            'Upload this capture\'s videos on the Data panel, then press Build. ' +
            'Nothing is read until you do, and you can come back to it later.'),
          h('div', { class: 'row', style: { marginTop: '14px', justifyContent: 'center' } },
            h('button', { class: 'btn primary', onclick: () =>
              uploadDialog(this, cap.id).then(() => this.renderInspector()) }, 'Upload files…'),
            cap.provider_kind ? h('button', { class: 'btn', onclick: async () => {
              try {
                const { job } = await this.api.buildCapture(cap.id);
                this.watchJob(job, async (j) => {
                  if (j.state === 'done') { ui.toast('Built', j.message || ''); await this.reopen(); }
                });
              } catch (e) { ui.toast('Cannot build yet', e.message, 'warn'); }
            } }, 'Build') : null)))));
    this.renderTop();
    this.renderRail();
    this.renderInspector();
    this.bus.emit('capture', cap);
  }

  // Re-read the capture without tearing the viewport down — for a clock
  // offset, a rename, a new rendition.
  async refreshCapture() {
    const cur = this.store.get('capture');
    if (!cur) return;
    const cap = await this.api.capture(cur.id);
    await Promise.all(cap.streams.map(async (st) => {
      st.frameMap = await this.api.frameMap(cap.id, st.key);
      st.timestamps = await this.api.timestamps(cap.id, st.key).catch(() => null);
    }));
    this.store.set({ capture: cap });
    this.store.touch('capture');
    for (const [, d] of this.data) d.streams = cap.streams;
    this.viewport?.comp?.restream();
    this.viewport?.comp?.update(true);
    this.viewport?.invalidate();
    this.timeline?.computeSpans();
    this.timeline?.paint();
    this.renderInspector();
  }

  // For changes that move the cells or the streams themselves: the viewport is
  // built around a layout, so a new layout wants a new viewport.
  async reopen() {
    const cur = this.store.get('capture');
    if (!cur) return;
    const tool = this.store.get('tool');
    await this.openCapture(cur.id);
    if (tool) this.activateTool(tool);
  }

  async loadAssets() {
    const cap = this.store.get('capture');
    if (!cap) return;
    try { this._assets = await this.api.assets(cap.id); }
    catch { this._assets = { assets: [], kinds: [] }; }
    this.renderInspector();
  }

  async loadExports() {
    const cap = this.store.get('capture');
    if (!cap) return;
    try { this._exports = await this.api.exports(cap.id); }
    catch { this._exports = { exports: [] }; }
    this.renderInspector();
  }

  async loadReadiness() {
    const cap = this.store.get('capture');
    if (!cap) return;
    try { this._readiness = (await this.api.readiness(cap.id)).plugins; }
    catch { this._readiness = {}; }
    this.renderRail();
    this.renderInspector();
  }

  // A socket came back after having been away. Replaying the missed ops is
  // done by the time this runs, and it is not enough: an undo during the gap
  // flips a flag on an op we already applied, and no message in the log would
  // ever mention it. So re-read the things that are *derived* rather than
  // appended — the capture record, the history panel, the running jobs, and
  // the mask windows — and let the frame we are on refill.
  //
  // This is deliberately a handful of requests on a rare event, not a poll. It
  // is the one moment where being certain is worth a round trip.
  async afterReconnect() {
    const cap = this.store.get('capture');
    if (!cap) return;
    try {
      await this.refreshCapture();
      for (const d of this.data.values()) { d.cov = null; d.pending = null; }
      await this.ensureData(this.store.get('frame'));
      this.loadHistory();
      this.loadAssets();
      this.loadReadiness();
      this.viewport?.invalidate();
      this.renderInspector();
    } catch {
      // Still not really back. The badge already says so, and the next
      // reconnect will try this again.
      this._link = { ...(this._link || {}), stale: true };
      this.renderTop();
    }
  }

  closeCapture() {
    this.socket?.close(); this.socket = null;
    this._link = null;
    this.viewport?.destroy(); this.viewport = null;
    this.timeline?.destroy(); this.timeline = null;
    for (const off of this.subs || []) { try { off(); } catch { /* already gone */ } }
    this.subs = [];
    this.data.clear();
    this.store.set({ capture: null, tool: null });
  }

  async ensureData(frame) {
    const cap = this.store.get('capture');
    const playing = this.store.get('playing');
    // Runway: how few frames may be left before the next window is asked for.
    // It has to cover the round trip, or the window empties while the request
    // for its replacement is still in flight — which on a 700 ms link is ten
    // frames of blank overlay, once per window, forever.
    //
    // So: the frames that will play while the answer is coming back, doubled
    // for the variance a forwarded port has plenty of, and never less than the
    // second of runway this always had.
    const fps = cap?.fps || 15;
    const speed = this.store.get('speed') || 1;
    const lead = playing
      ? Math.min(90, Math.max(Math.ceil(fps * speed),
                              Math.ceil(fps * speed * (this.api.rtt / 1000) * 2)))
      : 6;
    const jobs = [];
    for (const l of this.visibleLayers()) {
      const d = this.data.get(l.id);
      if (d) jobs.push(Promise.resolve(d.ensure(frame, lead)));
    }
    const r = await Promise.all(jobs);
    if (r.some(Boolean)) this.viewport?.invalidate();
  }

  visibleLayers() {
    const cap = this.store.get('capture');
    const vis = this.store.get('layerVis');
    return (cap?.layers || []).filter((l) => vis[l.id] !== false);
  }

  layerOpacity(id) { return this.store.get('layerVis')[id] === 'dim' ? 0.3 : 1; }

  // The first visible layer's data. Safe *only* for things every layer answers
  // identically — frame mapping, essentially. It is not "the mask layer": once
  // a plugin materialises a derived layer, a track may be served from either,
  // and the same key can exist in both. For anything about a specific track use
  // objectOf / findObject / visibleObjects, which look everywhere and prefer
  // the copy that is actually on screen.
  primaryData() {
    for (const l of this.visibleLayers()) if (this.data.has(l.id)) return this.data.get(l.id);
    return this.data.values().next().value || null;
  }

  layerData(pred) {
    for (const [, d] of this.data) if (!pred || pred(d.layer)) return d;
    return null;
  }

  // An object id, or a (stream, key) pair, in whichever layer holds it. Once a
  // plugin can add a derived layer beside an imported one, "the layer" stops
  // being a safe assumption anywhere.
  objectOf(id) {
    for (const [, d] of this.data) { const o = d.objects.get(id); if (o) return o; }
    return null;
  }

  // The same key can exist in two layers at once — the first half of a cut
  // track keeps the original key, so `86` is both the imported track and the
  // derived one that replaced it. Whichever is actually drawn is the one the
  // user means; returning the other selects something invisible.
  findObject(stream, key, pred) {
    const streams = new Map((this.store.get('capture')?.streams || []).map((s) => [s.key, s]));
    let fallback = null;
    for (const [, d] of this.data) {
      if (pred && !pred(d.layer)) continue;
      for (const o of d.byStream.get(stream) || []) {
        if (o.key !== String(key)) continue;
        fallback = fallback || o;
        const entry = { object: o, stream: streams.get(stream) || { key: stream } };
        if (this.display.visible(entry)) return o;
      }
    }
    return fallback;
  }

  // DOOR 1 of 4: what may be drawn at this frame. The viewport hands this to
  // every renderer, and `LayerData.activeAtRaw` — the unfiltered set — is
  // named to read like a warning at any other call site.
  entriesAt(frame, layer = null) {
    const out = [];
    for (const l of this.visibleLayers()) {
      if (layer && l.id !== layer.id) continue;
      const d = this.data.get(l.id);
      if (!d) continue;
      for (const e of d.activeAtRaw(frame)) if (this.display.visible(e)) out.push(e);
    }
    return out;
  }

  // Every object across every visible layer that the display authority is not
  // hiding. Once one layer can replace tracks in another, "how many tracks are
  // there" stops being `data.objects.size`; and once a class can be hidden, it
  // stops being anything a single plugin can answer.
  visibleObjects(pred) {
    const out = [];
    const streams = new Map((this.store.get('capture')?.streams || []).map((s) => [s.key, s]));
    for (const l of this.visibleLayers()) {
      if (pred && !pred(l)) continue;
      const d = this.data.get(l.id);
      if (!d) continue;
      for (const o of d.objects.values()) {
        const entry = { object: o, stream: streams.get(o.stream) || { key: o.stream } };
        if (this.display.visible(entry)) out.push(o);
      }
    }
    return out;
  }

  // Re-read the capture's layers and their objects: what a structural edit
  // changes is not one shape but which tracks exist at all.
  async reloadLayers() {
    const cap = this.store.get('capture');
    if (!cap) return;
    const fresh = await this.api.capture(cap.id);
    cap.layers = fresh.layers;
    const vis = { ...this.store.get('layerVis') };
    for (const l of cap.layers) if (vis[l.id] === undefined) vis[l.id] = true;
    for (const id of [...this.data.keys()]) if (!cap.layers.some((l) => l.id === id)) this.data.delete(id);
    this.store.set({ layerVis: vis });
    const frame = this.store.get('frame');
    await Promise.all(cap.layers.map(async (l) => {
      const d = new LayerData(this.api, l, cap.streams);
      this.data.set(l.id, d);
      await d.loadObjects();
      await d.ensure(frame);
    }));
    for (const r of this.viewport?.renderers.values() || []) r.dispose?.();
    this.viewport?.renderers.clear();
    this.bus.emit('data', this.primaryData());
    this.viewport?.invalidate();
    this.renderInspector();
  }

  // Structural mask edits normally change only the derived `edits` layer.
  // Re-reading every unchanged source layer made a two-track join feel like a
  // whole-capture reload. A first edit may create that layer, so fall back to
  // the complete refresh until it is part of this capture's known layout.
  async reloadLayer(layerId) {
    const cap = this.store.get('capture');
    const layer = cap?.layers?.find((l) => l.id === layerId);
    if (!layer) return this.reloadLayers();
    const d = new LayerData(this.api, layer, cap.streams);
    this.data.set(layer.id, d);
    await d.loadObjects();
    await d.ensure(this.store.get('frame'));
    const renderer = this.viewport?.renderers.get(layer.id);
    renderer?.dispose?.();
    this.viewport?.renderers.delete(layer.id);
    this.bus.emit('data', d);
    this.viewport?.invalidate();
    this.renderInspector();
  }

  // ------------------------------------------------------------------- ops
  async postOp(kind, payload, { source = null } = {}) {
    const cap = this.store.get('capture');
    const { op } = await this.api.postOp(cap.id, { kind, payload });
    // Keep one event shape for local and socket edits, while making the origin
    // explicit to browser-side optimistic views.  Nothing extra is sent to the
    // server and the persisted op is unchanged.
    this.bus.emit('op', source ? { ...op, _source: source } : op);
    return op;
  }

  // Undo takes back *your* last op. It never inverts anything: the op is
  // marked undone, folds stop being told about it, and whoever derives state
  // from the log gets the answer they would have got had you not done it.
  async undo(kind = '') {
    const cap = this.store.get('capture');
    if (!cap) return null;
    const { op, message } = await this.api.undoLast(cap.id, kind);
    if (!op) { ui.toast('Nothing to undo', message || 'you have not edited this capture'); return null; }
    ui.toast('Undone', `${op.kind}${op.actor === this.session?.user?.name ? '' : ` by ${op.actor}`}`);
    this.bus.emit('op', op);
    this.renderInspector();
    return op;
  }

  async loadHistory() {
    const cap = this.store.get('capture');
    if (!cap) return;
    const { ops } = await this.api.history(cap.id);
    this.history = ops.reverse();
    this.renderInspector();
  }

  async undoOp(opId) {
    const cap = this.store.get('capture');
    const { op } = await this.api.undoOp(cap.id, opId);
    this.bus.emit('op', op);
    this.renderInspector();
    return op;
  }

  async redo(opId) {
    const cap = this.store.get('capture');
    const { op } = await this.api.redoOp(cap.id, opId);
    this.bus.emit('op', op);
    this.renderInspector();
    return op;
  }

  onJob(job) {
    const jobs = [job, ...this.store.get('jobs').filter((j) => j.id !== job.id)].slice(0, 20);
    this.store.set({ jobs });
    this.store.touch('jobs');
    this.renderInspector();
    if (job.state === 'failed') ui.toast(`${job.plugin}.${job.kind} failed`, job.message, 'err');
  }

  // -------------------------------------------------------------- transport
  setFrame(f, fromVideo = false) {
    const cap = this.store.get('capture');
    const frame = clamp(Math.round(f), 0, (cap?.n_frames || 1) - 1);
    if (frame === this.store.get('frame')) return;
    this.store.set({ frame });
    if (!fromVideo) this.viewport?.seek(frame);
    // Where somebody's playhead is, four times a second. Sending it on every
    // frame of playback is 25 websocket writes a second per viewer, to tell
    // everyone something that changes by one.
    const now = performance.now();
    if (now - (this._toldPresence || 0) > 250) {
      this._toldPresence = now;
      this.socket?.send({ t: 'presence', frame, tool: this.store.get('tool') });
    }
    this.viewport?.invalidate();
  }

  step(d) { this.pause(); this.setFrame(this.store.get('frame') + d); }

  togglePlay() { this.store.get('playing') ? this.pause() : this.playOn(); }
  playOn() { this.store.set({ playing: true }); this.viewport?.play(true); }
  pause() { if (this.store.get('playing')) { this.store.set({ playing: false }); this.viewport?.play(false); } }

  // -------------------------------------------------------------- selection
  // DOOR 3 of 4 (see display.js). Ids that cannot be seen are dropped here, so
  // *no tool can act on something the user is not being shown* — whatever
  // route the id took to get here, and whether or not its author thought
  // about it. This one function retires a whole family of bugs: the walk
  // stopping on superseded tracks, group select picking eight invisible ones,
  // a destructive key reaching a track behind a hidden class.
  select(ids, mode = 'set') {
    const want = [].concat(ids).filter((x) => x != null);
    const ok = want.filter((id) => this.display.visibleId(id));
    const dropped = want.length - ok.length;
    const sel = new Set(mode === 'set' ? [] : this.store.get('selection'));
    for (const id of ok) {
      if (mode === 'toggle' && sel.has(id)) sel.delete(id); else sel.add(id);
    }
    this.store.set({ selection: sel });
    this.store.touch('selection');
    this.bus.emit('selection', sel);
    if (dropped) this.explainDrop(dropped);
    return { selected: ok.length, dropped };
  }

  // Said once, not once per object, and not silently: a click that appears to
  // do nothing gets clicked again.
  explainDrop(n) {
    const now = Date.now();
    if (now - (this._lastDrop || 0) < 2500) return;
    this._lastDrop = now;
    const hidden = this.display.hiddenLabels.size;
    ui.toast(`${n} hidden track${n === 1 ? '' : 's'} skipped`,
      hidden ? 'They belong to a class you have hidden — the Display panel shows which.'
        : 'They have been replaced, deleted or filtered out.', 'warn');
  }

  // Hiding a class must not leave it selected: everything downstream asks
  // "what is selected", and an answer containing invisible things is how the
  // old bugs got in.
  pruneSelection() {
    const sel = this.store.get('selection');
    if (!sel?.size) return;
    const keep = new Set([...sel].filter((id) => this.display.visibleId(id)));
    if (keep.size === sel.size) return;
    this.store.set({ selection: keep });
    this.store.touch('selection');
    this.bus.emit('selection', keep);
  }

  clearSelection() { this.store.set({ selection: new Set() }); this.store.touch('selection'); this.bus.emit('selection', new Set()); }

  // DOOR 4 of 4: the only sanctioned way to move the frame on somebody's
  // behalf and point at something. It returns what it accepted and what it
  // dropped, so a caller handed a hidden object has to notice — rather than
  // jumping to a frame and then pointing at nothing, which reads as a bug.
  async reveal(ids, { frame = null, pulse = {}, select = true } = {}) {
    const want = [].concat(ids).filter((x) => x != null);
    if (frame != null) { this.pause(); this.setFrame(frame); await this.ensureData(frame); }
    const ok = want.filter((id) => this.display.visibleId(id));
    if (select && ok.length) this.select(ok, 'set');
    if (ok.length) this.viewport?.pulse(ok, pulse);
    return { shown: ok, hidden: want.filter((id) => !ok.includes(id)) };
  }

  // Point at something. Any tool that moves the frame for you owes the eye a
  // hint about where it just went.
  pulse(ids, opts) {
    this.viewport?.pulse([].concat(ids).filter((id) => this.display.visibleId(id)), opts);
  }

  // ------------------------------------------------------------------- tools
  activateTool(id) {
    const t = id === CAPTURE_TOOL.id ? CAPTURE_TOOL : this.plugins.tools.get(id);
    this.store.set({ tool: id });
    const cap = this.store.get('capture');
    if (cap) history.replaceState(null, '', `#/c/${cap.id}/${id}`);
    this.renderRail();
    this.renderInspector();
    this.bus.emit('tool', t);
  }

  // ------------------------------------------------------------------ chrome
  brand() {
    return h('a', { class: 'brand', href: '#/', style: { textDecoration: 'none', color: 'inherit' } },
      h('svg', { width: 18, height: 18, viewBox: '0 0 24 24', fill: 'none', html:
        '<path d="M12 3l7.5 4.5v9L12 21l-7.5-4.5v-9z" stroke="var(--accent)" stroke-width="1.6"/>' +
        '<circle cx="12" cy="12" r="2.4" fill="var(--accent)"/>' }),
      'Quorum');
  }

  topRight() {
    const jobs = this.store.get('jobs').filter((j) => ['queued', 'running'].includes(j.state));
    return [
      jobs.length ? h('span', { class: 'tag warn' }, `${jobs.length} running`) : null,
      this.store.get('capture') ? displayButton(this) : null,
      h('button', {
        class: 'btn sm', title: 'Command palette  (Ctrl+K)', onclick: () => this.keymap.palette(),
      }, '⌘', h('span', { class: 'hint' }, 'K')),
      h('button', {
        class: 'btn sm icon', title: 'Light / dark',
        onclick: () => this.applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'),
      }, '◐'),
      h('span', { class: 'hint mono' }, this.session?.user?.name || ''),
    ];
  }

  renderTop() {
    const cap = this.store.get('capture');
    if (!cap) return;                       // the lobby draws its own top bar
    const presence = this.store.get('presence')
      .filter((p) => p.user !== this.session?.user?.name);
    const toolId = this.store.get('tool');
    const tool = toolId === CAPTURE_TOOL.id ? CAPTURE_TOOL : this.plugins.tools.get(toolId);
    mount(document.getElementById('topbar'),
      this.brand(),
      h('a', { class: 'btn sm', href: '#/' }, '← captures'),
      h('span', { class: 'mono', style: { fontWeight: 600 } }, cap.name),
      // Which workspace is this? Every one of them is a rail icon and a column
      // of panels, and without saying so they are hard to tell apart — the
      // Capture settings and the mask editor were mistaken for each other on
      // the first day, by the person who asked for both.
      tool ? h('span', { class: 'crumb' }, h('b', {}, tool.icon || '◉'), tool.title) : null,
      h('span', { class: 'hint' }, tool?.description || ''),
      h('span', { class: 'hint' }, `${cap.streams.length} streams · ${cap.n_frames.toLocaleString()} frames`),
      h('span', { class: 'grow' }),
      this.linkBadge(),
      ...presence.map((p) => h('span', {
        class: 'tag', style: { color: keyColor(p.user), borderColor: 'currentColor' },
        title: `frame ${p.frame ?? '—'}`,
      }, p.user)),
      ...this.topRight());
  }

  // What the link is doing, when it is doing something worth knowing about.
  // Silent while connected and quick, because a permanent "online" badge is
  // furniture; the whole value is that it appears.
  //
  // On the GPU box this client is reached through `ssh -L`, and a forwarded
  // port fails in the least helpful way there is: it keeps accepting, so the
  // page looks fine and quietly stops being true. Saying "reconnecting" is the
  // difference between a known pause and an annotator who does not find out
  // until their next edit lands on a stale view.
  linkBadge() {
    const st = this._link || {};
    if (st.state === 'reconnecting' || st.state === 'connecting') {
      return h('span', { class: 'tag', style: { color: 'var(--warn, #d08a26)',
                                                borderColor: 'currentColor' },
                         title: 'the connection dropped; retrying with backoff' },
               st.tries > 1 ? `reconnecting ×${st.tries}` : 'reconnecting…');
    }
    if (st.stale) {
      return h('span', { class: 'tag', style: { color: 'var(--err, #c8422f)',
                                                borderColor: 'currentColor' },
                         title: 'reconnected, but the missed edits could not be fetched — reload' },
               'out of date');
    }
    // Slow but working. Only past the point where it changes what you should
    // expect from a click, so it does not nag on an ordinary lab network.
    if (this.api.rtt > 250) {
      return h('span', { class: 'hint', title: 'round trip to the server' },
               `${Math.round(this.api.rtt)} ms`);
    }
    return null;
  }

  // A tool whose plugin says it cannot work here is shown, greyed, with the
  // reason one click away. Hiding it would leave somebody wondering where the
  // feature went; the server refuses the work either way.
  blockedReason(tool) {
    const st = (this._readiness || {})[tool.plugin];
    if (!st || st.ok) return '';
    for (const m of st.missing) {
      if (m.tools?.length && !m.tools.includes(tool.id)) continue;
      // A reason that names jobs and no tools is about those jobs. One plugin
      // can hold two unrelated abilities — an interactor that needs a model
      // endpoint and a batch job that does not — and greying the workspace
      // because the batch half is unavailable says something false. The server
      // draws the same line (`Host.blocked`), so this is the client agreeing
      // with it rather than a second opinion.
      if (!m.tools?.length && m.jobs?.length) continue;
      return [m.why, m.fix].filter(Boolean).join(' ');
    }
    return '';
  }

  renderRail() {
    const rail = document.getElementById('rail');
    if (!this.store.get('capture')) return mount(rail);
    const tools = [CAPTURE_TOOL, ...this.plugins.tools.values()].sort((a, b) => a.order - b.order);
    const cur = this.store.get('tool');
    mount(rail, ...tools.map((t) => {
      const why = t.plugin ? this.blockedReason(t) : '';
      const b = h('button', {
        class: `railbtn${t.id === cur ? ' on' : ''}${why ? ' blocked' : ''}`,
        title: why ? `${t.title} — not ready: ${why}` : `${t.title}${t.keys ? `  (${t.keys})` : ''}`,
        onclick: () => {
          if (why) {
            ui.toast(`${t.title} is not ready`, why, 'warn');
            this.goto(this.store.get('capture').id, CAPTURE_TOOL.id);
            return;
          }
          this.activateTool(t.id);
        },
      }, t.icon);
      if (why) b.append(h('span', { class: 'badge warn' }, '!'));
      else if (t.badge) { const n = t.badge(this); if (n) b.append(h('span', { class: 'badge' }, n)); }
      return b;
    }));
  }

  renderInspector() {
    const el = document.getElementById('inspector');
    if (!this.store.get('capture')) return mount(el);
    const cap = this.store.get('capture');
    const scroll = el.scrollTop;

    const layerPanel = ui.panel('Layers', (cap.layers.length ? cap.layers : [null]).map((l) => l ? h('div', {
      class: 'item', onclick: () => {
        const vis = { ...this.store.get('layerVis') };
        vis[l.id] = vis[l.id] === false ? true : false;
        this.store.set({ layerVis: vis });
        this.store.touch('layerVis');
      },
    },
      h('span', { class: 'swatch', style: { background: this.store.get('layerVis')[l.id] === false ? 'var(--line-2)' : 'var(--accent)' } }),
      h('span', { class: 'k grow' }, l.name),
      h('span', { class: 'hint mono' }, l.type),
      h('span', { class: `tag ${l.provenance === 'human' ? 'ok' : l.provenance === 'derived' ? 'info' : 'mute'}` }, l.provenance),
    ) : h('div', { class: 'hint' }, 'No layers yet — run an importer.')), { id: 'layers' });

    const importers = (this.manifests || []).flatMap((m) => (m.provides.io || [])
      .filter((io) => io.direction === 'import').map((io) => ({ ...io, plugin: m.id })));
    const exporters = (this.manifests || []).flatMap((m) => (m.provides.io || [])
      .filter((io) => io.direction === 'export').map((io) => ({ ...io, plugin: m.id })));

    // An exporter writes files on the server and hands back their paths. That
    // is the right shape for a 40 MB XML per camera and for a job that finishes
    // while the tab is closed — but a path on somebody else's disk is not an
    // answer to "how do I get my annotations", so the files are listed here and
    // handed over. A directory of per-stream files comes down as one zip.
    const files = (this._exports?.exports || []).map((f) => h('div', { class: 'item' },
      h('span', { class: 'k grow mono', title: f.name }, f.name),
      h('span', { class: 'hint' }, `${f.dir ? `${f.files} files · ` : ''}${bytes(f.size)}`),
      h('a', { class: 'btn sm', href: this.api.exportUrl(f.path), download: '',
               title: f.dir ? 'Download as a zip' : 'Download' }, f.dir ? '↓ zip' : '↓'),
      h('button', { class: 'btn sm danger', title: 'Delete from the server',
        onclick: async () => {
          if (!await ui.confirm(`Delete ${f.name}?`,
            'Only the exported copy on the server. The annotations it was made from ' +
            'are untouched, and running the export again remakes it.', 'Delete')) return;
          await this.api.deleteExport(f.path);
          this.loadExports();
        } }, '×')));

    const dataPanel = ui.panel('Data', [
      h('div', { class: 'hint' }, 'Bring annotations in, and take results out.'),
      h('div', { class: 'row wrap' }, ...importers.map((io) => h('button', {
        class: 'btn sm', onclick: () => this.runIO(io, 'import'),
      }, io.title))),
      exporters.length ? h('div', { class: 'row wrap' }, ...exporters.map((io) => h('button', {
        class: 'btn sm primary', onclick: () => this.runIO(io, 'export'),
      }, io.title))) : null,
      files.length ? h('div', {},
        h('div', { class: 'hint', style: { margin: '8px 0 4px' } }, 'Exported files'),
        h('div', { class: 'list' }, files)) : null,
    ], { id: 'data', collapsed: !files.length });

    const jobs = this.store.get('jobs').slice(0, 6);
    const jobPanel = ui.panel('Jobs', jobs.length ? jobs.map((j) => h('div', {},
      h('div', { class: 'row' },
        h('span', { class: 'k grow mono' }, `${j.plugin}.${j.kind}`),
        h('span', { class: `tag ${j.state === 'failed' ? 'bad' : j.state === 'done' ? 'ok' : 'warn'}` }, j.state),
        ['queued', 'running'].includes(j.state) ? h('button', {
          class: 'btn sm', onclick: () => this.api.cancelJob(j.id) }, 'stop') : null),
      h('div', { class: 'bar', style: { marginTop: '4px' } }, h('i', { style: { width: `${j.progress * 100}%` } })),
      h('div', { class: 'hint', style: { marginTop: '3px' } }, `${j.message || ''} · ${ago(j.updated)}`),
    )) : h('div', { class: 'hint' }, 'Nothing has run yet.'), { id: 'jobs', collapsed: !jobs.length });

    // Who did what, and the one button that takes it back. Attribution is the
    // point of an op log; a log nobody can read is just a slower database.
    const hist = (this.history || []).slice(0, 12);
    const me = this.session?.user?.name;
    const histPanel = ui.panel('History', hist.length ? hist.map((o) => h('div', {
      class: 'item', style: o.undone ? { opacity: '.45' } : {},
    },
      h('span', { class: 'swatch', style: { background: keyColor(o.actor) } }),
      h('span', { class: 'k grow' }, o.kind.replace(/^\w+\./, '')),
      h('span', { class: 'hint mono' }, o.actor === me ? 'you' : o.actor),
      o.undone
        ? h('button', { class: 'btn sm', title: 'Put it back',
                        onclick: () => this.redo(o.id) }, 'redo')
        : h('button', { class: 'btn sm', title: 'Undo this one',
                        onclick: () => this.undoOp(o.id) }, 'undo'),
    )) : h('div', { class: 'hint' }, 'No edits yet.'),
      { id: 'history', collapsed: !hist.length,
        actions: [h('button', { class: 'btn sm', onclick: (e) => { e.stopPropagation(); this.loadHistory(); } }, '↻')] });

    const toolId = this.store.get('tool');
    // The Display control is in every workspace, because "how are these
    // coloured" and "which of these am I looking at" are not a tool's
    // business — that assumption is what made Identity's recolouring
    // unavailable everywhere else and unrefusable while it was open.
    const parts = [displayPanel(this), layerPanel, histPanel, dataPanel, jobPanel];
    const toolPanels = h('div');
    const active = toolId === CAPTURE_TOOL.id ? CAPTURE_TOOL : this.plugins.tools.get(toolId);
    if (active) {
      toolPanels.append(h('div', { class: 'workspace' },
        h('span', { class: 'ws-icon' }, active.icon || '◉'),
        h('span', { class: 'grow' },
          h('div', { class: 'ws-title' }, active.title),
          h('div', { class: 'hint' }, active.description || ''))));
    }
    if (toolId === CAPTURE_TOOL.id) {
      // One panel throwing must not take the workspace with it — an empty
      // inspector is indistinguishable from "this button does nothing".
      try { toolPanels.append(capturePanels(this)); }
      catch (e) {
        console.error('capture workspace', e);
        toolPanels.append(ui.panel('Capture', h('div', { class: 'hint' },
          `This workspace failed to render: ${e.message}`)));
      }
    }
    for (const fn of this.plugins.inspectors.get(toolId) || []) {
      try { toolPanels.append(fn(this) || ''); } catch (e) { console.error('inspector', e); }
    }
    mount(el, toolPanels, ...parts);
    el.scrollTop = scroll;
  }

  async runIO(io, dir) {
    const cap = this.store.get('capture');
    const spec = { ...io.params };
    if (spec.capture_id) delete spec.capture_id;
    const f = ui.form(spec, {}, {
      assets: this._assets?.assets || [],
      onUpload: (kind) => uploadDialog(this, cap.id, { kind }).then(() => this.loadAssets()),
    });
    const go = await ui.modal({ title: io.title, content: f.el, ok: dir === 'import' ? 'Import' : 'Export' });
    if (!go) return;
    const { job } = await this.api.submit({
      plugin: io.plugin, kind: `${dir}:${io.id}`,
      params: { ...f.values, capture_id: cap.id }, capture_id: cap.id });
    this.watchJob(job, async (j) => {
      if (j.state !== 'done') return;
      if (dir === 'import') {
        ui.toast(io.title, j.message || 'done');
        await this.openCapture(cap.id);
        this.activateTool(this.store.get('tool'));
        return;
      }
      await this.loadExports();
      // Hand the file over rather than reporting a path on the server. The
      // result names either one file or the directory the exporter filled.
      const made = j.result?.path || j.result?.dir || (j.result?.files || [])[0];
      const entry = (this._exports?.exports || []).find(
        (f) => made && (made.endsWith(`/${f.name}`) || made.endsWith(f.name)));
      const t = ui.toast(io.title, j.message || 'done');
      if (entry) {
        t.append(h('div', { style: { marginTop: '6px' } },
          h('a', { class: 'btn sm primary', href: this.api.exportUrl(entry.path),
                   download: '' }, entry.dir ? '↓ download zip' : '↓ download')));
      }
    });
  }

  // -------------------------------------------------------------- commands
  globalCommands() {
    const K = this.keymap;
    const cap = () => this.store.get('capture');
    K.register({ id: 'core.play', title: 'Play / pause', keys: ['Space'], group: 'Transport',
      when: () => !!cap(), run: () => this.togglePlay() });
    K.register({ id: 'core.next', title: 'Step forward', keys: ['Right'], repeat: true, group: 'Transport',
      when: () => !!cap(), run: () => this.step(1) });
    K.register({ id: 'core.prev', title: 'Step back', keys: ['Left'], repeat: true, group: 'Transport',
      when: () => !!cap(), run: () => this.step(-1) });
    K.register({ id: 'core.jump', title: 'Jump forward 1 s', keys: ['Shift+Right'], repeat: true, group: 'Transport',
      when: () => !!cap(), run: () => this.step(Math.round(cap().fps)) });
    K.register({ id: 'core.jumpback', title: 'Jump back 1 s', keys: ['Shift+Left'], repeat: true, group: 'Transport',
      when: () => !!cap(), run: () => this.step(-Math.round(cap().fps)) });
    K.register({ id: 'core.fit', title: 'Fit to window', keys: ['F'], group: 'View',
      when: () => !!this.viewport, run: () => this.viewport.fit() });
    K.register({ id: 'core.clear', title: 'Clear selection', keys: ['Esc'], group: 'Select',
      when: () => !!cap(), run: () => this.clearSelection() });
    K.register({ id: 'core.editIdentity', title: 'Switch between Edit and Identity', keys: ['I'], group: 'App',
      when: () => ['edit', 'identity'].includes(this.store.get('tool')),
      run: () => this.activateTool(this.store.get('tool') === 'edit' ? 'identity' : 'edit') });
    K.register({ id: 'core.palette', title: 'Command palette', keys: ['Ctrl+K'], group: 'App',
      run: () => this.keymap.palette() });
    K.register({ id: 'core.inspector', title: 'Show / hide the inspector', keys: ['Ctrl+B'], group: 'App',
      run: () => document.getElementById('app').classList.toggle('no-inspector') });
    K.register({ id: 'core.zoomcell', title: 'Zoom the hovered camera (hold)', keys: ['Z'], hold: true, group: 'View',
      when: () => !!this.viewport, run: ({ down }) => {
        const vp = this.viewport;
        if (down) {
          const c = vp.cellAt(vp.toWorld(...lastPointer));
          if (!c) return;
          vp.zoomHold = { scale: vp.scale, tx: vp.tx, ty: vp.ty };
          vp.zoomTo(c);
        } else if (vp.zoomHold) {
          Object.assign(vp, vp.zoomHold); vp.zoomHold = null; vp.apply();
        }
      } });

    let lastPointer = [0, 0];
    window.addEventListener('pointermove', (e) => { lastPointer = [e.clientX, e.clientY]; }, { passive: true });

    K.register({ id: 'core.undo', title: 'Undo my last edit', keys: ['Ctrl+Z'], group: 'Edit',
      when: () => !!cap(), run: () => this.undo() });
    K.register({ id: 'core.capture', title: 'Capture settings: clocks, files, layers',
      keys: ['Ctrl+,'], group: 'App', when: () => !!cap(),
      run: () => this.goto(cap().id, CAPTURE_TOOL.id) });
    K.register({ id: 'core.colormode', title: 'Next colour mode', keys: ['K'], group: 'View',
      when: () => !!cap(), run: () => {
        const modes = [...this.display.modes.values()].sort((a, b) => (a.order ?? 100) - (b.order ?? 100));
        const i = modes.findIndex((m) => m.id === this.display.mode);
        const next = modes[(i + 1) % modes.length];
        this.display.setMode(next.id);
        ui.toast('Colour', next.title);
        this.renderTop(); this.renderInspector();
      } });
    K.register({ id: 'core.showall', title: 'Show every hidden class', keys: ['Shift+H'],
      group: 'View', when: () => !!cap(), run: () => {
        const n = this.display.hiddenLabels.size;
        this.display.showAllLabels();
        this.renderTop(); this.renderInspector();
        ui.toast('Classes', n ? `${n} class(es) shown again` : 'nothing was hidden');
      } });

    this.store.on(['jobs', 'presence'], () => { if (this.store.get('capture')) this.renderTop(); });
    const refreshHistory = debounce(() => this.loadHistory(), 250);
    this.bus.on('op', () => { this.renderInspector(); refreshHistory(); });
    const refreshUploads = debounce(() => this.renderInspector(), 200);
    this.bus.on('uploads', refreshUploads);
    this.bus.on('assets', () => { this.loadAssets(); this.loadReadiness(); });
    // A structural edit can create or retire a class; the class list is what
    // the Display panel is built from, so it has to keep up.
    this.bus.on('data', debounce(() => this.display.loadLabels(), 300));

    // Selection policy lives here so that clicking always does *something*,
    // and a tool can take it over while it is the active one.
    this.bus.on('pick', (ev) => {
      const fn = this.plugins.pickHandlers.get(this.store.get('tool'));
      if (fn && fn(ev) !== false) return;
      if (ev.right) return;
      if (!ev.hit) return this.clearSelection();
      this.select(ev.hit.object.id, ev.event.shiftKey ? 'toggle' : 'set');
    });
  }
}

const app = new App();
window.quorum = app;                 // the console is a legitimate debugging tool
app.start();
export default app;
