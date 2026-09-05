// The display authority: the one thing that knows what is on screen and what
// colour it is.
//
// There was already a notion of a hidden track — a styler returning
// `hidden: true` — and already a helper that respected it. The trouble was that
// respecting it was *optional*: `data.objects` and `data.activeAt` handed back
// everything, so every feature had to remember to filter, and every place that
// forgot has been a bug. The walk stopping on superseded tracks, group select
// picking eight invisible ones, the exporter shipping unedited masks: all the
// same mistake, fixed one at a time, with the next one already queued up.
//
// So this is not "a filter". It is one authority, and the core closes the four
// doors an object can come through — drawing, hit testing, selection and
// navigation — so a plugin that forgets *cannot* show you something you hid.
// See docs/DESIGN-uploads-time-display.md §4.
import { keyColor } from './util.js';

export class Display {
  constructor(app) {
    this.app = app;
    this.modes = new Map();        // id -> {id, title, colorOf, plugin}
    this.filters = new Map();      // id -> {id, title, structural, test, plugin, on}
    this.labels = [];              // [{label, n, layers}] from the server
    this.hiddenLabels = new Set();
    this.mode = 'track';
    this.pinned = false;           // did the user choose the mode themselves?
    this.captureId = null;
    this.epoch = 0;                // bumped on any change; caches key off it
    this._cache = new Map();
    this.builtin();
  }

  // -------------------------------------------------------------- built in
  // A *class* is `objects.label`, which the core has always stored. So
  // colouring by class and hiding a class are core features about core data,
  // and every layer type gets them without writing a line — rather than a
  // mask-shaped feature that boxes and polygons would each reimplement.
  builtin() {
    this.registerMode({
      id: 'track', title: 'by track', order: 0,
      colorOf: (e) => keyColor(`${e.stream.key}/${e.object.key}`),
    });
    this.registerMode({
      id: 'class', title: 'by class', order: 1,
      colorOf: (e) => (e.object.label ? keyColor(`label:${e.object.label}`)
        : 'hsl(200 6% 62%)'),
      labelOf: (e) => `${e.object.label || 'unlabelled'} · ${e.object.key}`,
    });
    this.registerMode({
      id: 'layer', title: 'by layer', order: 2,
      colorOf: (e) => keyColor(`layer:${e.object.layer_id}`),
    });
  }

  // ------------------------------------------------------------ registries
  registerMode(spec) {
    this.modes.set(spec.id, { order: 100, ...spec });
    this.changed();
    return () => { this.modes.delete(spec.id); this.changed(); };
  }

  // `structural: true` means correctness, not taste — a superseded copy of a
  // cut track, a purged one. Those are always on and never appear as a toggle,
  // because offering to turn them off would be offering to show two of the
  // same track. Everything else is a user toggle and shows up in the panel.
  registerFilter(spec) {
    const on = spec.structural ? true
      : (localStorage.getItem(`quorum.filter.${spec.id}`) ?? (spec.on === false ? '0' : '1')) === '1';
    this.filters.set(spec.id, { structural: false, ...spec, on });
    this.changed();
    return () => { this.filters.delete(spec.id); this.changed(); };
  }

  setFilter(id, on) {
    const f = this.filters.get(id);
    if (!f || f.structural) return;
    f.on = !!on;
    localStorage.setItem(`quorum.filter.${id}`, f.on ? '1' : '0');
    this.changed();
  }

  // ----------------------------------------------------------------- state
  async attach(captureId) {
    this.captureId = captureId;
    this._cache.clear();
    const saved = JSON.parse(localStorage.getItem(`quorum.display.${captureId}`) || '{}');
    this.mode = this.modes.has(saved.mode) ? saved.mode : 'track';
    this.pinned = !!saved.pinned;
    this.hiddenLabels = new Set(saved.hiddenLabels || []);
    await this.loadLabels();
    this.changed();
  }

  async loadLabels() {
    if (!this.captureId) return;
    try {
      const r = await this.app.api.labels(this.captureId);
      this.labels = r.labels || [];
    } catch { this.labels = []; }
    // A class that no longer exists must not stay hidden invisibly: it would
    // be a filter nobody can see, on data nobody can find.
    const known = new Set(this.labels.map((l) => l.label));
    for (const l of [...this.hiddenLabels]) if (!known.has(l)) this.hiddenLabels.delete(l);
    this.changed();
  }

  save() {
    if (!this.captureId) return;
    localStorage.setItem(`quorum.display.${this.captureId}`, JSON.stringify({
      mode: this.mode, pinned: this.pinned, hiddenLabels: [...this.hiddenLabels] }));
  }

  // The user picking a mode pins it. A tool may *suggest* one — Identity does,
  // because that is what its whole workspace is about — and a suggestion never
  // overrides a choice somebody made on purpose.
  setMode(id, { pinned = true } = {}) {
    if (!this.modes.has(id)) return;
    this.mode = id;
    if (pinned) this.pinned = true;
    this.save();
    this.changed();
  }

  suggestMode(id) {
    if (this.pinned || !this.modes.has(id) || this.mode === id) return;
    this.mode = id;
    this.changed();
  }

  setLabelHidden(label, hidden) {
    if (hidden) this.hiddenLabels.add(label); else this.hiddenLabels.delete(label);
    this.save();
    this.changed();
  }

  showAllLabels() { this.hiddenLabels.clear(); this.save(); this.changed(); }

  changed() {
    this.epoch++;
    this._cache.clear();
    this.app.bus?.emit('display', this);
    this.app.viewport?.invalidate();
    // Something already selected may have just become invisible. Dropping it
    // here rather than at every use site is the whole design: nothing
    // downstream has to wonder whether its selection is still real.
    this.app.pruneSelection?.();
  }

  // -------------------------------------------------------------- the rule
  // One predicate. Everything else in this file is bookkeeping around it.
  visible(entryOrObject) {
    const entry = entryOrObject?.object ? entryOrObject : this.entryFor(entryOrObject);
    if (!entry) return false;
    const o = entry.object;
    if (o.label && this.hiddenLabels.has(o.label)) return false;
    for (const f of this.filters.values()) {
      if (!f.on) continue;
      try { if (f.test(entry)) return false; } catch { /* a filter that needs a payload abstains */ }
    }
    // Stylers predate this file and some still answer `hidden` (masks does, for
    // superseded copies). They are part of the same decision, so they are asked
    // here rather than consulted separately by whoever happens to remember.
    try {
      if (this.app.plugins.styleFor(entry, { hidden: false }).hidden) return false;
    } catch { /* likewise */ }
    return true;
  }

  visibleId(objectId) {
    const key = `v${objectId}`;
    let hit = this._cache.get(key);
    if (hit === undefined) this._cache.set(key, hit = this.visible(objectId));
    return hit;
  }

  entryFor(objectOrId) {
    const o = typeof objectOrId === 'object' ? objectOrId : this.app.objectOf(objectOrId);
    if (!o) return null;
    const st = (this.app.store.get('capture')?.streams || []).find((s) => s.key === o.stream);
    return { object: o, stream: st || { key: o.stream } };
  }

  // What colour a thing is drawn: the mode decides, then stylers decorate
  // (identity's hover emphasis, masks' DEL tint). Two jobs, in that order, so
  // "what colour is this" has one answer and tools stop fighting over it.
  paint(entry, base) {
    const mode = this.modes.get(this.mode);
    let style = { ...base };
    if (mode) {
      try {
        const c = mode.colorOf?.(entry);
        if (c) style.color = c;
        const l = mode.labelOf?.(entry);
        if (l) style.label = l;
      } catch { /* a mode that needs state it has not loaded yet abstains */ }
    }
    try { style = this.app.plugins.styleFor(entry, style) || style; } catch { /* as above */ }
    return style;
  }

  // ------------------------------------------------------------ the wire
  // The serialisable half, in the core's own vocabulary — a label, a layer, a
  // (stream, key) pair. Sent to any endpoint that offers objects to a human,
  // so the server skips a hidden class instead of the client walking to a
  // frame and then declining to point at anything.
  //
  // Plugin filters are deliberately absent: they are predicates over payloads
  // the server never opens. They cannot leak, because of the four doors.
  get filter() {
    const f = {};
    if (this.hiddenLabels.size) f.labels = { exclude: [...this.hiddenLabels] };
    const layers = (this.app.store.get('capture')?.layers || [])
      .filter((l) => this.app.store.get('layerVis')[l.id] === false).map((l) => l.id);
    if (layers.length) f.layers = { exclude: layers };
    return f;
  }

  get query() {
    const f = this.filter;
    return Object.keys(f).length ? `filter=${encodeURIComponent(JSON.stringify(f))}` : '';
  }

  // ------------------------------------------------------------- counting
  // What the panel shows: how many objects each class has, and how many of
  // them survive everything else. Counting what is drawn rather than what is
  // stored is the same honesty rule as `visibleObjects`.
  labelStats() {
    const shown = new Map();
    for (const o of this.app.visibleObjects()) {
      const k = o.label || '';
      shown.set(k, (shown.get(k) || 0) + 1);
    }
    const rows = this.labels.map((l) => ({
      ...l, shown: this.hiddenLabels.has(l.label) ? 0 : (shown.get(l.label) || 0),
      hidden: this.hiddenLabels.has(l.label),
    }));
    // a class present in the data but not in the server's list (a plugin
    // materialised it since) still deserves a row
    for (const [k, n] of shown) {
      if (!rows.some((r) => r.label === k)) rows.push({ label: k, n, shown: n, hidden: false, layers: 1 });
    }
    return rows.sort((a, b) => b.n - a.n);
  }

  swatch(label) { return label ? keyColor(`label:${label}`) : 'hsl(200 6% 62%)'; }
}
