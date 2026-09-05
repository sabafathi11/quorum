// Transport and per-stream lanes. The lanes answer the question the desktop
// tools made you scrub for: *where in this capture is there anything to look
// at, and where is the thing I have selected?*
import { h, mount, timecode, clamp } from './util.js';

export class Timeline {
  constructor(app, el) {
    this.app = app;
    this.store = app.store;
    this.el = el;
    this.laneH = 13;
    this.gutter = 44;
    this.spans = new Map();          // stream key -> {counts:Uint16Array, sel:[]}
    this.lanesDirty = true;
    this.build();
    // The playhead moves every frame; the lanes do not. Separating them is the
    // difference between repainting six streams' activity, every selected
    // track and every plugin's decorator 25 times a second — which is most of a
    // frame's budget on this capture — and blitting one cached image.
    //
    // Every subscription is kept so it can be dropped again. A timeline that
    // outlives its capture keeps painting on every frame for the rest of the
    // session, and reopening a capture — which an edit does — leaves another
    // one behind each time. That is invisible until playback stutters for no
    // reason anybody can point at.
    const dirty = () => { this.lanesDirty = true; this.paint(); };
    this.off = [
      this.store.on(['frame'], () => this.paint()),
      this.store.on(['selection', 'playing', 'speed'], dirty),
      app.bus.on('data', () => { this.computeSpans(); dirty(); }),
      app.bus.on('display', () => { this.computeSpans(); dirty(); }),
      app.bus.on('tool', dirty),
    ];
  }

  destroy() {
    for (const off of this.off || []) { try { off(); } catch { /* already gone */ } }
    this.off = [];
    this.ro?.disconnect();
    this.themeWatch?.disconnect();
    this.cache = null;
  }

  build() {
    const cap = this.store.get('capture');
    this.scrub = h('input', {
      class: 'scrub', type: 'range', min: 0, max: Math.max(0, cap.n_frames - 1), value: 0,
      oninput: (e) => this.app.setFrame(parseInt(e.target.value, 10)),
    });
    this.counter = h('span', { class: 'mono' });
    this.playBtn = h('button', { class: 'btn icon', title: 'Play / pause  (Space)', onclick: () => this.app.togglePlay() }, '▶');
    this.speed = h('select', { class: 'sel', style: { width: '68px' }, onchange: (e) => {
      this.store.set({ speed: parseFloat(e.target.value) });
      this.app.viewport?.play(this.store.get('playing'));
    } }, ...[0.25, 0.5, 1, 2, 3, 5, 8].map((s) => h('option', { value: s, selected: s === 1 }, `${s}×`)));

    this.lanes = h('canvas');
    this.laneWrap = h('div', { class: 'tl-lanes' }, this.lanes);
    this.lanes.addEventListener('pointerdown', (e) => this.scrubFrom(e));
    this.lanes.addEventListener('pointermove', (e) => { if (e.buttons & 1) this.scrubFrom(e); });

    mount(this.el,
      h('div', { class: 'tl-head' },
        h('button', { class: 'btn icon', title: 'Step back  (←)', onclick: () => this.app.step(-1) }, '◀'),
        this.playBtn,
        h('button', { class: 'btn icon', title: 'Step forward  (→)', onclick: () => this.app.step(1) }, '▶'),
        this.counter,
        this.scrub,
        h('span', { class: 'hint' }, 'speed'), this.speed,
        h('button', { class: 'btn sm', title: 'Show / hide lanes', onclick: () => this.toggleLanes() }, 'lanes')),
      this.laneWrap);
    this.showLanes = true;
    this.toggleLanes(true);
    this.themeWatch = new MutationObserver(() => {
      this.colours = null; this.lanesDirty = true; this.paint();
    });
    this.themeWatch.observe(document.documentElement, { attributes: true,
                                                        attributeFilter: ['data-theme'] });
    this.ro = new ResizeObserver(() => { this.lanesDirty = true; this.paint(); });
    this.ro.observe(this.laneWrap);
  }

  toggleLanes(force) {
    this.showLanes = force === undefined ? !this.showLanes : force;
    const n = this.store.get('capture').streams.length;
    this.laneWrap.style.height = this.showLanes ? `${n * this.laneH + 10}px` : '0px';
    this.paint();
  }

  scrubFrom(e) {
    const r = this.lanes.getBoundingClientRect();
    const cap = this.store.get('capture');
    const x = clamp((e.clientX - r.left - this.gutter) / (r.width - this.gutter), 0, 1);
    this.app.setFrame(Math.round(x * (cap.n_frames - 1)));
  }

  // Per-stream activity: how many objects are drawn on each pixel column, and
  // where the selection lives. Computed once per data change, not per frame.
  computeSpans() {
    const cap = this.store.get('capture');
    const W = Math.max(200, Math.round(this.laneWrap.clientWidth - this.gutter));
    this.spans.clear();
    const data = this.app.primaryData();
    if (!data) return;
    // every layer, minus what a styler has superseded — a track that was cut in
    // two should read as two spans here, not as the original plus both halves
    const live = new Map();
    for (const o of this.app.visibleObjects()) {
      if (!live.has(o.stream)) live.set(o.stream, []);
      live.get(o.stream).push(o);
    }
    for (const st of cap.streams) {
      const counts = new Uint16Array(W);
      const objs = live.get(st.key) || [];
      for (const o of objs) {
        const a = data.unmapFrame(st.key, o.first_frame);
        const b = data.unmapFrame(st.key, o.last_frame);
        const x0 = clamp(Math.floor(a / cap.n_frames * W), 0, W - 1);
        const x1 = clamp(Math.ceil(b / cap.n_frames * W), 0, W - 1);
        for (let x = x0; x <= x1; x++) if (counts[x] < 65535) counts[x]++;
      }
      this.spans.set(st.key, { counts, W });
    }
    this.maxCount = Math.max(1, ...[...this.spans.values()].flatMap((s) => [Math.max(...s.counts)]));
  }

  paint() {
    const cap = this.store.get('capture');
    const frame = this.store.get('frame');
    this.counter.textContent = `${String(frame).padStart(5, ' ')} / ${cap.n_frames - 1}   ${timecode(frame, cap.fps)}`;
    this.scrub.value = frame;
    this.playBtn.textContent = this.store.get('playing') ? '❚❚' : '▶';
    if (!this.showLanes) return;

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    // `clientWidth` is a layout read and `getComputedStyle` is a style read;
    // both are cheap once and neither belongs on a path that runs 25 times a
    // second. They are re-read whenever the lanes are rebuilt, which is every
    // occasion that could have changed them.
    if (this.lanesDirty || this.W === undefined) {
      this.W = this.laneWrap.clientWidth;
      this.H = cap.streams.length * this.laneH + 10;
      this.colours = null;
    }
    const W = this.W, H = this.H;
    if (!W) return;
    if (this.lanes.width !== Math.round(W * dpr)) {
      this.lanes.width = Math.round(W * dpr); this.lanes.height = Math.round(H * dpr);
      this.lanes.style.height = `${H}px`;
      this.computeSpans();
      this.lanesDirty = true;
    }
    const g = this.lanes.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (this.lanesDirty) this.paintLanes(W, H, dpr);
    if (!this.colours) {
      const css = getComputedStyle(document.documentElement);
      this.colours = { fg: css.getPropertyValue('--fg').trim(),
                       accent: css.getPropertyValue('--accent').trim() };
    }
    g.clearRect(0, 0, W, H);
    if (this.cache) g.drawImage(this.cache, 0, 0, W, H);

    const plotW = W - this.gutter;
    const px = this.gutter + frame / Math.max(1, cap.n_frames - 1) * plotW;
    g.fillStyle = this.colours.fg;
    g.fillRect(px - 0.5, 0, 1.5, H);
    g.fillStyle = this.colours.accent;
    g.fillRect(px - 3, 0, 6, 3);
  }

  // Everything that does not move with the playhead, drawn once and kept.
  paintLanes(W, H, dpr) {
    this.lanesDirty = false;
    const cap = this.store.get('capture');
    if (!this.cache) this.cache = document.createElement('canvas');
    this.cache.width = Math.round(W * dpr);
    this.cache.height = Math.round(H * dpr);
    const g = this.cache.getContext('2d');
    if (!g) return;
    const css = getComputedStyle(document.documentElement);
    const c = (n) => css.getPropertyValue(n).trim();
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);
    g.font = '9px ui-monospace, monospace';

    const plotW = W - this.gutter;
    const data = this.app.primaryData();
    const sel = this.store.get('selection');
    if (!data) return;
    cap.streams.forEach((st, i) => {
      const y = 5 + i * this.laneH;
      g.fillStyle = c('--sunk');
      g.fillRect(this.gutter, y, plotW, this.laneH - 3);
      g.fillStyle = c('--fg-3');
      g.fillText(st.key, 6, y + 8);

      const s = this.spans.get(st.key);
      if (s) {
        g.fillStyle = c('--line-2');
        const scale = (this.laneH - 4) / this.maxCount;
        for (let x = 0; x < s.W; x++) {
          const n = s.counts[x];
          if (!n) continue;
          const hgt = Math.max(1, n * scale);
          g.fillRect(this.gutter + x * plotW / s.W, y + (this.laneH - 3) - hgt, Math.max(1, plotW / s.W), hgt);
        }
      }
      // selected objects, in the accent, on top
      if (sel.size) {
        g.fillStyle = c('--accent');
        for (const oid of sel) {
          const o = this.app.objectOf(oid);
          if (!o || o.stream !== st.key) continue;
          const a = data.unmapFrame(st.key, o.first_frame) / cap.n_frames;
          const b = data.unmapFrame(st.key, o.last_frame) / cap.n_frames;
          g.fillRect(this.gutter + a * plotW, y, Math.max(2, (b - a) * plotW), this.laneH - 3);
        }
      }
      for (const dec of this.app.plugins.laneDecorators) {
        try { dec({ g, stream: st, y, h: this.laneH - 3, x0: this.gutter, w: plotW, cap, data, color: c }); }
        catch (e) { console.error('lane decorator', e); }
      }
    });
  }
}
