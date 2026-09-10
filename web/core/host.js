// The plugin host: registries, the plugin context, and the layer data cache.
// The core's rule is visible here — it moves records around and knows their
// frames, and it never looks inside a payload.
import { Api } from './api.js';
import * as ui from './ui.js';
import { h, keyColor, clamp } from './util.js';

// --------------------------------------------------------------- layer data
export class LayerData {
  constructor(api, layer, streams) {
    this.api = api;
    this.layer = layer;
    this.streams = streams;                 // [{key, id, width, height, frameMap}]
    this.objects = new Map();               // object id -> {id, key, stream, label, first, last, meta}
    this.byStream = new Map();              // stream key -> [object]
    this.keys = new Map();                  // object id -> {f:[], o:[], p:[]}
    this.cov = null;                        // covered capture-frame range
    this.pending = null;
    this.wanted = null;                     // newest playhead while a batch is in flight
    this.version = 0;                       // bumped whenever drawable data changed
  }

  // How many capture frames to ask for at a time.
  //
  // A fixed window is a bet on the round trip, and 45 frames was the right bet
  // on a laptop, where the answer is back before three frames have played. On
  // the GPU box, reached over `ssh -L`, a round trip is ~700 ms — ten frames at
  // 15 fps — and the fetch for the *next* window is issued when six frames of
  // runway are left. The window runs out before its replacement lands, every
  // time, and the masks blink out and come back for the whole of playback.
  //
  // So the window is a function of the measured link instead of a constant.
  // Asking for more frames costs bandwidth, which is the thing there is enough
  // of; asking too late costs the picture, which is the thing there is not.
  // The ceiling is there because a mask window is RLE payload per object per
  // keyframe, and on a busy layer an unbounded window is its own stall.
  get span() {
    const rtt = this.api?.rtt || 0;
    if (rtt <= 80) return 45;
    return clamp(Math.round(45 * rtt / 80), 45, 180);
  }

  async loadObjects() {
    const { objects } = await this.api.objects(this.layer.id);
    for (const o of objects) {
      this.objects.set(o.id, o);
      if (!this.byStream.has(o.stream)) this.byStream.set(o.stream, []);
      this.byStream.get(o.stream).push(o);
    }
    this.version++;
    return this;
  }

  streamOf(key) { return this.streams.find((s) => s.key === key); }

  mapFrame(streamKey, captureFrame) {
    const st = this.streamOf(streamKey);
    const fm = st?.frameMap;
    if (!fm || !fm.length) return captureFrame;
    return fm[clamp(captureFrame, 0, fm.length - 1)];
  }

  // capture frame for a stream frame — the frame map is nondecreasing
  unmapFrame(streamKey, streamFrame) {
    const fm = this.streamOf(streamKey)?.frameMap;
    if (!fm || !fm.length) return streamFrame;
    let lo = 0, hi = fm.length - 1;
    while (lo < hi) { const m = (lo + hi) >> 1; if (fm[m] < streamFrame) lo = m + 1; else hi = m; }
    return lo;
  }

  // `lead` is how many frames of runway to keep in front of the playhead. At
  // rest six is plenty; while playing it has to cover the round trip, or the
  // window runs out mid-second and the masks stop until it arrives — which is
  // what "the masks lag and then catch up" looked like.
  async ensure(frame, lead = 6) {
    // Playback calls this once per presented video frame.  Keep only the most
    // recent target while a batch is travelling: starting one GET per frame
    // turns a small runway into a queue of stale responses, which is how masks
    // visibly trail the video and briefly draw old keyframes.
    this.wanted = { frame, lead };
    if (this.cov && frame >= this.cov.from && frame <= this.cov.to - lead) return false;
    if (this.pending) return this.pending.p;
    // Read the window size once. It is derived from a live latency estimate, so
    // a second read could disagree with the first and leave `cov` claiming a
    // range the request never asked for — masks missing at the seam, on a link
    // that has just changed speed, which is not a bug anybody would find twice.
    const span = this.span;
    const adjacent = this.cov && frame >= this.cov.from && frame <= this.cov.to + span;
    const p = this.api.frames(this.layer.id, frame, span).then((data) => {
      if (!adjacent) this.keys.clear();
      for (const [, s] of Object.entries(data.streams)) {
        for (const [oid, f, out, payload] of s.keys) {
          let e = this.keys.get(oid);
          if (!e) this.keys.set(oid, e = { f: [], o: [], p: [] });
          const i = lowerBound(e.f, f);
          if (e.f[i] === f) { e.o[i] = out; e.p[i] = payload; continue; }
          e.f.splice(i, 0, f); e.o.splice(i, 0, out); e.p.splice(i, 0, payload);
        }
      }
      this.cov = adjacent && this.cov
        ? { from: Math.min(this.cov.from, frame), to: Math.max(this.cov.to, frame + span) }
        : { from: frame, to: frame + span };
      this.version++;
      return true;
    }).catch(() => false).finally(() => {
      this.pending = null;
      // If the video crossed the freshly loaded runway while this request was
      // in flight, immediately start the next batch from the latest frame.
      // Nobody awaits this follow-up; it is the piggyback prefetch for the
      // frames already being presented.
      const wanted = this.wanted;
      if (wanted && (!this.cov || wanted.frame < this.cov.from ||
          wanted.frame > this.cov.to - wanted.lead)) this.ensure(wanted.frame, wanted.lead);
    });
    this.pending = { frame, span, p };
    return p;
  }

  // Every object with a shape at this capture frame — *including* ones the
  // display authority is hiding. The name is a warning: almost nothing wants
  // this. Use `app.entriesAt(frame, layer)`, which is the same set with the
  // hidden ones removed, and which is what the viewport and the hit test are
  // given. See display.js.
  activeAtRaw(frame) {
    // A keyframe from the preceding window is not evidence about the current
    // video frame.  Rather than draw that stale mask as a ghost, wait for the
    // prefetched batch that covers this frame.  During normal playback the
    // single-flight runway above keeps this branch cold.
    if (!this.loaded(frame)) return [];
    const out = [];
    for (const st of this.streams) {
      const objs = this.byStream.get(st.key);
      if (!objs) continue;
      const mapped = this.mapFrame(st.key, frame);
      for (const o of objs) {
        if (mapped < o.first_frame || mapped > o.last_frame) continue;
        const e = this.keys.get(o.id);
        if (!e) continue;
        const i = lowerBound(e.f, mapped + 1) - 1;
        if (i < 0 || e.o[i]) continue;
        out.push({ object: o, stream: st, frame: e.f[i], payload: e.p[i] });
      }
    }
    return out;
  }

  loaded(frame) {
    return !!this.cov && frame >= this.cov.from && frame <= this.cov.to;
  }
}

function lowerBound(arr, v) {
  let lo = 0, hi = arr.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m] < v) lo = m + 1; else hi = m; }
  return lo;
}

// -------------------------------------------------------------- plugin host
export class PluginHost {
  constructor(app) {
    this.app = app;
    this.plugins = new Map();       // id -> {manifest, module, ctx}
    this.tools = new Map();         // tool id -> {…spec, plugin}
    this.renderers = new Map();     // layer type -> factory
    this.stylers = [];              // (entry, base) -> style
    this.laneDecorators = [];
    this.inspectors = new Map();    // tool id -> [fn(app)]
    this.pickHandlers = new Map();  // tool id -> fn({hit, event, right, world, cell})
    this.problems = [];
  }

  async loadAll(manifests) {
    // Tools are declared on the server so the rail exists even for a plugin
    // whose ES module fails to load — a broken panel must not hide the tab
    // that tells you which plugin is broken.
    for (const m of manifests) {
      for (const t of m.provides?.tools || []) {
        this.tools.set(t.id, { icon: '◉', order: 100, ...t, plugin: m.id });
      }
    }
    for (const m of manifests) {
      if (!m.web) { this.plugins.set(m.id, { manifest: m, module: null }); continue; }
      try {
        const mod = await import(m.web);
        const plugin = mod.default || mod;
        const ctx = this.context(m);
        await plugin.activate?.(ctx);
        this.plugins.set(m.id, { manifest: m, module: plugin, ctx });
      } catch (e) {
        console.error('plugin failed', m.id, e);
        this.problems.push(`${m.id}: ${e.message}`);
        ui.toast(`Plugin ${m.id} failed to load`, e.message, 'err');
      }
    }
  }

  context(manifest) {
    const app = this.app;
    const id = manifest.id;
    return {
      id, manifest, settings: manifest.settings || {},
      api: app.api, store: app.store, ui, h, keyColor,
      get session() { return app.session; },

      // -- registries ------------------------------------------------------
      registerTool: (spec) => {
        this.tools.set(spec.id, { icon: '◉', order: 100, ...spec, plugin: id });
        app.renderRail();
      },
      registerLayerRenderer: (type, factory) => this.renderers.set(type, factory),
      registerStyler: (fn) => { this.stylers.push(fn); return () => {
        this.stylers = this.stylers.filter((f) => f !== fn); }; },

      // How things are coloured, and what is on screen at all. Both go to the
      // one authority (display.js) rather than being decided by whoever draws
      // last — which is what let a tool recolour masks only while its own tab
      // was open, and what let a hidden track reach four different features.
      registerColorMode: (spec) => app.display.registerMode({ ...spec, plugin: id }),
      registerFilter: (spec) => app.display.registerFilter({
        ...spec, plugin: id, id: spec.id.includes('.') ? spec.id : `${id}.${spec.id}` }),
      get display() { return app.display; },
      registerLaneDecorator: (fn) => this.laneDecorators.push(fn),
      // While `toolId` is the active tool, it decides what a click means.
      // Return false to fall through to the core's default (select what was hit).
      setPickHandler: (toolId, fn) => this.pickHandlers.set(toolId, fn),
      registerInspector: (toolId, fn) => {
        if (!this.inspectors.has(toolId)) this.inspectors.set(toolId, []);
        this.inspectors.get(toolId).push(fn);
      },
      registerCommand: (cmd) => app.keymap.register({ group: manifest.name, ...cmd,
        id: cmd.id.includes('.') ? cmd.id : `${id}.${cmd.id}` }),

      // -- doing things ----------------------------------------------------
      op: (kind, payload) => app.postOp(`${id}.${kind}`, payload),
      job: (kind, params = {}) => app.api.submit({
        plugin: id, kind, params, capture_id: app.store.get('capture')?.id }),
      call: (path, opts) => app.api.plugin(id, path, opts),
      doc: (key) => app.api.doc(app.store.get('capture').id, id, key),
      putDoc: (key, value, version) => app.api.putDoc(app.store.get('capture').id, id, key, value, version),

      // -- viewport / timeline ---------------------------------------------
      get viewport() { return app.viewport; },
      get timeline() { return app.timeline; },
      invalidate: () => app.viewport?.invalidate(),
      pulse: (ids, opts) => app.pulse(ids, opts),
      layerData: (pred) => app.layerData(pred),
      objectOf: (id) => app.objectOf(id),
      visibleObjects: (pred) => app.visibleObjects(pred),
      entriesAt: (frame, layer) => app.entriesAt(frame, layer),
      // The only sanctioned way to move the frame on somebody's behalf and
      // point at something. It drops what cannot be seen and tells you, so a
      // caller handed a hidden object has to notice rather than silently
      // pointing at nothing.
      reveal: (ids, opts) => app.reveal(ids, opts),
      findObject: (stream, key, pred) => app.findObject(stream, key, pred),
      reloadLayers: () => app.reloadLayers(),
      undo: (kind) => app.undo(kind),
      onOp: (fn) => app.bus.on('op', fn),
      on: (evt, fn) => app.bus.on(evt, fn),
      emit: (evt, data) => app.bus.emit(evt, data),
      toast: ui.toast,
    };
  }

  styleFor(entry, base) {
    let style = base;
    for (const fn of this.stylers) style = fn(entry, style) || style;
    return style;
  }
}

// A minimal event bus so plugins can talk to each other without importing
// each other — `identity` announces a selection change, `proposals` listens.
export class Bus {
  constructor() { this.map = new Map(); }
  on(evt, fn) {
    if (!this.map.has(evt)) this.map.set(evt, new Set());
    this.map.get(evt).add(fn);
    return () => this.map.get(evt)?.delete(fn);
  }
  emit(evt, data) { for (const fn of this.map.get(evt) || []) fn(data); }
}
