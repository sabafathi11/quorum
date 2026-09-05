// The viewport: a scene of cells composed from the streams themselves, one
// overlay canvas on top, zoom and pan shared by both. Layer types draw
// themselves through a renderer the owning plugin registered — the core
// positions cameras, hands out transforms, and decides what may be seen.
//
// Two things it is careful about, both of them "one owner, not many":
//
//   · what each cell shows is `media.js`'s decision, taken on every gesture
//     that changes how big a cell is, so "zoom shows full resolution" needed
//     no code of its own here;
//   · what a renderer is given, and what a click can hit, both go through the
//     display authority — a hidden track is never handed out, so no renderer
//     and no tool has to remember not to show it.
import { Composition } from './media.js';
import { h, clamp } from './util.js';

const MIN_SCALE = 0.02, MAX_SCALE = 64;

export class Viewport {
  constructor(app, el) {
    this.app = app;
    this.store = app.store;
    this.el = el;
    this.scale = 1; this.tx = 0; this.ty = 0;
    this.renderers = new Map();       // layer id -> renderer instance
    this.dirty = true;
    this.hover = null;
    this.zoomHold = null;             // saved view while a cell is held zoomed
    this.build();
    this.loop();
  }

  // ------------------------------------------------------------------ build
  build() {
    const cap = this.store.get('capture');
    const layout = cap.layout || {};
    this.layout = layout;
    this.world = layout.scene ? { w: layout.scene.w, h: layout.scene.h }
      : layout.mosaic ? { w: layout.mosaic.w, h: layout.mosaic.h }
        : this.worldFromCells(layout.cells || []);

    this.stage = h('div', { class: 'stage' });
    this.media = h('div', { class: 'media', style: { width: `${this.world.w}px`, height: `${this.world.h}px` } });
    this.canvas = h('canvas', { class: 'overlay' });
    this.g = this.canvas.getContext('2d');

    // Everything about which pixels appear where lives in here. The viewport
    // knows there is a scene and how to look at it, and nothing about codecs,
    // renditions, clock offsets or stills.
    this.comp = new Composition(this.app, this.media);

    this.hud = h('div', { class: 'viewhud' });
    this.stage.append(this.media, this.canvas, this.hud);
    this.el.append(this.stage);

    this.bindPointer();
    this.bindClock();
    new ResizeObserver(() => this.resize()).observe(this.el);
    this.resize();
    this.fit();
  }

  worldFromCells(cells) {
    let w = 0, h = 0;
    for (const c of cells) { w = Math.max(w, c.x + c.w); h = Math.max(h, c.y + c.h); }
    return { w: w || 1920, h: h || 1080 };
  }

  cellOf(streamKey) {
    return (this.layout.cells || []).find((c) => c.stream === streamKey);
  }

  // Is any of this cell inside the window? A cell scrolled off screen should
  // not be holding a decoder open.
  cellOnScreen(c) {
    const x0 = c.x * this.scale + this.tx, y0 = c.y * this.scale + this.ty;
    const x1 = x0 + c.w * this.scale, y1 = y0 + c.h * this.scale;
    return x1 > -40 && y1 > -40 && x0 < this.vw + 40 && y0 < this.vh + 40;
  }

  // ----------------------------------------------------------------- camera
  resize() {
    const r = this.el.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.canvas.width = Math.max(1, Math.round(r.width * dpr));
    this.canvas.height = Math.max(1, Math.round(r.height * dpr));
    this.canvas.style.width = `${r.width}px`;
    this.canvas.style.height = `${r.height}px`;
    this.dpr = dpr; this.vw = r.width; this.vh = r.height;
    this.apply();
  }

  fit(rect = null) {
    const box = rect || { x: 0, y: 0, w: this.world.w, h: this.world.h };
    const pad = rect ? 0.98 : 0.99;
    this.scale = Math.min(this.vw / box.w, this.vh / box.h) * pad;
    this.tx = (this.vw - box.w * this.scale) / 2 - box.x * this.scale;
    this.ty = (this.vh - box.h * this.scale) / 2 - box.y * this.scale;
    this.apply();
  }

  zoomTo(rect) { this.fit(rect); }

  // Every change of view funnels through here, which is exactly why the
  // level-of-detail decision can live in one place: wheel, hold-Z, fit, resize
  // and a layout edit all end up on this line.
  apply() {
    this.media.style.transform = `translate(${this.tx}px, ${this.ty}px) scale(${this.scale})`;
    this.comp?.update();
    this.invalidate();
  }

  toWorld(cx, cy) {
    const r = this.el.getBoundingClientRect();
    return { x: (cx - r.left - this.tx) / this.scale, y: (cy - r.top - this.ty) / this.scale };
  }

  // ---------------------------------------------------------------- pointer
  bindPointer() {
    let drag = null;
    this.stage.addEventListener('wheel', (e) => {
      e.preventDefault();
      const p = this.toWorld(e.clientX, e.clientY);
      const k = Math.exp(-e.deltaY * (e.ctrlKey ? 0.012 : 0.0022));
      const s = clamp(this.scale * k, MIN_SCALE, MAX_SCALE);
      const r = this.el.getBoundingClientRect();
      this.tx = e.clientX - r.left - p.x * s;
      this.ty = e.clientY - r.top - p.y * s;
      this.scale = s;
      this.apply();
    }, { passive: false });

    this.stage.addEventListener('pointerdown', (e) => {
      // Alt+drag pans, but Alt+*click* is how you reach the mask underneath the
      // one on top — so an alt press only becomes a pan once it actually moves.
      if (e.button === 1 || e.altKey) {
        drag = { x: e.clientX, y: e.clientY, tx: this.tx, ty: this.ty,
                 pan: true, moved: 0, alt: e.altKey && e.button === 0 };
        if (!drag.alt) this.stage.classList.add('panning');
        this.stage.setPointerCapture(e.pointerId);
        e.preventDefault();
        return;
      }
      if (e.button === 0) {
        drag = { x: e.clientX, y: e.clientY, tx: this.tx, ty: this.ty, moved: 0 };
        this.stage.setPointerCapture(e.pointerId);
      }
    });

    this.stage.addEventListener('pointermove', (e) => {
      if (drag) {
        const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
        drag.moved = Math.max(drag.moved || 0, Math.abs(dx) + Math.abs(dy));
        if (drag.pan || drag.moved > 4) {
          this.stage.classList.add('panning');
          this.tx = drag.tx + dx; this.ty = drag.ty + dy;
          this.apply();
        }
        return;
      }
      const p = this.toWorld(e.clientX, e.clientY);
      this.hoverStack = this.pickAll(p.x, p.y);
      const hit = this.hoverStack[0] || null;
      const key = hit ? `${hit.object.id}` : null;
      if (key !== (this.hover ? `${this.hover.object.id}` : null)) {
        this.hover = hit;
        this.app.bus.emit('hover', hit);
        this.invalidate();
      }
      this.updateHud(p);
    });

    const up = (e) => {
      if (!drag) return;
      const still = (drag.moved || 0) <= 4;
      const wasClick = still && (!drag.pan || drag.alt);
      this.stage.classList.remove('panning');
      try { this.stage.releasePointerCapture(e.pointerId); } catch { /* already gone */ }
      const alt = !!drag.alt;
      drag = null;
      if (wasClick) {
        const p = this.toWorld(e.clientX, e.clientY);
        const stack = this.pickAll(p.x, p.y);
        this.app.bus.emit('pick', {
          hit: alt ? this.cycle(stack) : stack[0] || null,
          stack, alt, event: e, world: p, cell: this.cellAt(p) });
      }
    };
    this.stage.addEventListener('pointerup', up);
    this.stage.addEventListener('pointercancel', up);
    this.stage.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      const p = this.toWorld(e.clientX, e.clientY);
      const stack = this.pickAll(p.x, p.y);
      this.app.bus.emit('pick', { hit: stack[0] || null, stack, alt: e.altKey,
                                  event: e, world: p, cell: this.cellAt(p), right: true });
    });
    this.stage.addEventListener('dblclick', () => this.fit());
    this.stage.addEventListener('pointerleave', () => {
      if (this.hover) { this.hover = null; this.app.bus.emit('hover', null); this.invalidate(); }
    });
  }

  cellAt(p) {
    return (this.layout.cells || []).find((c) =>
      p.x >= c.x && p.x < c.x + c.w && p.y >= c.y && p.y < c.y + c.h) || null;
  }

  updateHud(p) {
    const cell = this.cellAt(p);
    const frame = this.store.get('frame');
    const bits = [`${Math.round(this.scale * 100)}%`];
    if (cell) {
      const data = this.app.primaryData();
      bits.push(cell.stream);
      if (data) bits.push(`src ${data.mapFrame(cell.stream, frame)}`);
      // Say which copy of the pixels you are looking at. "Why is this blurry"
      // should never need a guess.
      const c = this.comp?.cells.get(cell.stream);
      const cur = c?.current;
      if (cur?.kind === 'video') bits.push(cur.id === 'source' ? 'full res' : cur.id);
      else if (cur?.kind === 'still') bits.push('still · full res');
      else if (this.comp?.mosaic) bits.push('mosaic');
    }
    if (this.hover) bits.push(`${this.hover.object.label || 'obj'} ${this.hover.object.key}`);
    const n = this.hoverStack?.length || 0;
    if (n > 1) bits.push(`${n} overlapping — alt-click to go deeper`);
    this.hud.textContent = bits.join('  ·  ');
  }

  pick(x, y) {
    return this.pickAll(x, y)[0] || null;
  }

  // DOOR 2 of 4 (see display.js): everything under the point, topmost first —
  // minus anything the display authority is hiding. A renderer may report
  // whatever it likes; a hidden track cannot be clicked, alt-cycled, or listed
  // in a right-click menu, because it never leaves this function.
  pickAll(x, y) {
    const view = this.viewFor(null);
    const display = this.app.display;
    const out = [];
    for (const layer of [...this.app.visibleLayers()].reverse()) {
      const r = this.renderers.get(layer.id);
      const data = this.app.data.get(layer.id);
      if (!data) continue;
      const v = { ...view, layer, data, entries: this.app.entriesAt(view.frame, layer) };
      if (r?.hitTestAll) out.push(...(r.hitTestAll(x, y, v) || []));
      else if (r?.hitTest) { const hit = r.hitTest(x, y, v); if (hit) out.push(hit); }
    }
    return out.filter((e) => display.visible(e));
  }

  // Alt+click walks the stack: the same point twice steps one deeper, a new
  // point starts again at the top.
  cycle(stack) {
    if (!stack.length) { this.stackAt = null; return null; }
    const sig = stack.map((e) => e.object.id).join(',');
    if (this.stackAt?.sig === sig) this.stackAt.i = (this.stackAt.i + 1) % stack.length;
    else this.stackAt = { sig, i: stack.length > 1 ? 1 % stack.length : 0 };
    return stack[this.stackAt.i];
  }

  // ------------------------------------------------------------------ clock
  // Driven by the master video's own frame callback where the browser has one:
  // it fires exactly when a new frame is *presented*, so the overlay is drawn
  // for the picture on screen rather than for whenever a 60 Hz timer happened
  // to look. It also fires at the video's rate — 15/s here, not 60 — which is
  // four times less work for a strictly better answer.
  //
  // Falling back to rAF is fine but is the reason the masks used to trail: if
  // the main thread is busy, rAF ticks late and the frame number lags the
  // pictures. Keeping this loop cheap is therefore not an optimisation, it is
  // how the masks stay on the right frame.
  bindClock() {
    const cap = this.store.get('capture');
    this.fps = cap.fps || 15;
    this._clockOn = true;

    const advance = () => {
      if (!this.store.get('playing')) return;
      const f = this.comp.currentFrame();
      if (f != null && f !== this.store.get('frame')) {
        this.app.setFrame(f, true);
        this.invalidate();
      }
      this.comp.syncSlaves(this.store.get('frame'));
    };

    const bindTo = (video) => {
      if (this._boundTo === video) return;
      this._boundTo = video;
      if (!video?.requestVideoFrameCallback) return;
      const step = () => {
        if (!this._clockOn || this._boundTo !== video) return;
        advance();
        video.requestVideoFrameCallback(step);
      };
      video.requestVideoFrameCallback(step);
    };

    // A slow poll keeps the callback attached to whichever video is currently
    // the master — promoting a cell changes that — and stands in entirely where
    // requestVideoFrameCallback does not exist. When it does exist the poll
    // does almost nothing, four times a second.
    let lastLook = 0;
    const poll = (now = 0) => {
      if (!this._clockOn) return;
      const bound = this._boundTo?.requestVideoFrameCallback && this._boundTo.isConnected;
      if (!bound || now - lastLook > 250) {
        lastLook = now;
        bindTo(this.comp.masterVideo()?.video || null);
      }
      if (!this._boundTo?.requestVideoFrameCallback) advance();
      this._clock = requestAnimationFrame(poll);
    };
    this._clock = requestAnimationFrame(poll);
  }

  seek(frame) {
    this.comp.seek(frame);
    this.comp.frameChanged(frame);
    this.invalidate();
  }

  play(on) { this.comp.play(on); }

  // ------------------------------------------------------------------- draw
  invalidate() { this.dirty = true; }

  // A ring that breathes around some objects for a moment. "The selection is
  // the cursor" only works if you can see where the cursor just went — the
  // desktop tool's walk had this and losing it made the walk feel blind.
  pulse(objectIds, { color = '#F5C542', ms = 1400, rings = 3 } = {}) {
    const ids = new Set([].concat(objectIds));
    if (!ids.size) return;
    this.pulses = (this.pulses || []).filter((p) => p.until > performance.now());
    this.pulses.push({ ids, color, rings, from: performance.now(), until: performance.now() + ms });
    this.invalidate();
  }

  drawPulses(g) {
    const now = performance.now();
    this.pulses = (this.pulses || []).filter((p) => p.until > now);
    if (!this.pulses.length) return;
    for (const layer of this.app.visibleLayers()) {
      const r = this.renderers.get(layer.id);
      const data = this.app.data.get(layer.id);
      if (!r?.boundsOf || !data) continue;
      const view = this.viewFor(layer);
      for (const entry of this.app.entriesAt(view.frame, layer)) {
        for (const p of this.pulses) {
          if (!p.ids.has(entry.object.id)) continue;
          const b = r.boundsOf(entry, view);
          if (!b) continue;
          const t = (now - p.from) / (p.until - p.from);            // 0 → 1
          const cx = b.x + b.w / 2, cy = b.y + b.h / 2;
          const rad = Math.hypot(b.w, b.h) / 2;
          g.strokeStyle = p.color;
          for (let i = 0; i < p.rings; i++) {
            const phase = (t * p.rings + i) % 1;
            g.globalAlpha = (1 - phase) * (1 - t) * 0.9;
            g.lineWidth = 2.5 / this.scale;
            g.beginPath();
            g.ellipse(cx, cy, rad * (1 + phase * 0.55) + 4 / this.scale,
                      rad * (1 + phase * 0.55) + 4 / this.scale, 0, 0, Math.PI * 2);
            g.stroke();
          }
          g.globalAlpha = 1;
        }
      }
    }
    this.invalidate();                    // keep animating while one is alive
  }

  viewFor(layer) {
    return {
      frame: this.store.get('frame'),
      scale: this.scale,
      selection: this.store.get('selection'),
      hover: this.hover,
      layout: this.layout,
      cellOf: (k) => this.cellOf(k),
      // A renderer asks "what colour is this"; the display authority answers,
      // having already applied the colour mode and every styler.
      styleFor: (entry, base) => this.app.display.paint(entry, base),
      layer,
    };
  }

  loop() {
    const tick = () => {
      if (this.dirty) { this.dirty = false; this.draw(); }
      this._raf = requestAnimationFrame(tick);
    };
    this._raf = requestAnimationFrame(tick);
  }

  draw() {
    const g = this.g;
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.clearRect(0, 0, this.canvas.width, this.canvas.height);
    const k = this.dpr * this.scale;
    g.setTransform(k, 0, 0, k, this.dpr * this.tx, this.dpr * this.ty);

    for (const layer of this.app.visibleLayers()) {
      const data = this.app.data.get(layer.id);
      if (!data) continue;
      let r = this.renderers.get(layer.id);
      if (!r) {
        const factory = this.app.plugins.renderers.get(layer.type);
        if (!factory) continue;
        r = factory({ layer, data, app: this.app });
        this.renderers.set(layer.id, r);
      }
      const view = this.viewFor(layer);
      // DOOR 1 of 4: a renderer is handed only what may be seen. It cannot
      // draw a hidden track because it is never given one.
      view.entries = this.app.entriesAt(view.frame, layer);
      view.opacity = this.app.layerOpacity(layer.id);
      try { r.draw(g, view); } catch (e) { console.error('renderer', layer.type, e); }
    }

    // cell frames and names, drawn over everything so a mask never hides them
    g.lineWidth = 1 / this.scale;
    g.strokeStyle = 'rgba(255,255,255,.18)';
    g.font = `${Math.round(12 / this.scale)}px ui-monospace, monospace`;
    for (const c of this.layout.cells || []) {
      g.strokeRect(c.x, c.y, c.w, c.h);
      g.fillStyle = 'rgba(0,0,0,.55)';
      const st = (this.store.get('capture')?.streams || []).find((s) => s.key === c.stream);
      const off = st?.time_offset || 0;
      const label = off ? `${c.stream}  ${off > 0 ? '+' : ''}${off.toFixed(2)}s` : c.stream;
      const tw = g.measureText(label).width + 8 / this.scale;
      g.fillRect(c.x, c.y, tw, 16 / this.scale);
      g.fillStyle = off ? '#DCA84A' : 'rgba(255,255,255,.85)';
      g.fillText(label, c.x + 4 / this.scale, c.y + 12 / this.scale);
    }
    this.drawPulses(g);
    this.app.bus.emit('drawn', { g, view: this.viewFor(null) });
  }

  destroy() {
    this._clockOn = false;
    this._boundTo = null;
    cancelAnimationFrame(this._raf);
    cancelAnimationFrame(this._clock);
    for (const r of this.renderers.values()) r.dispose?.();
    this.renderers.clear();
    this.comp?.destroy();
    this.stage.remove();
  }
}
