// The mosaic, composed here rather than baked by ffmpeg — and the one policy
// that decides which pixels each cell shows.
//
// The tools this replaces stacked six cameras into a single 1920×720 H.264
// file at CRF 30 with the clock offsets prepended as black frames. That is why
// zooming showed mush, why correcting a clock cost a re-encode, and why there
// were two copies of every recording that could drift apart.
//
// Here a cell is a stream, a stream has renditions, and the composition is
// three CSS transforms. Which rendition a cell shows is decided by ONE
// function (`choose`) consulted by every path that changes how big a cell is
// on screen — wheel zoom, hold-Z, fit, resize, layout change — because "zoom
// switches to full resolution" is not a special case, it is that function
// returning a bigger rendition.
//
// See docs/DESIGN-uploads-time-display.md §3 and §5.
import { h, clamp } from './util.js';

// Promote a cell when it is being shown at more real pixels than the rendition
// it is using has, with a little slack so a cell sitting on the boundary does
// not flap between two decoders.
const PROMOTE_AT = 1.15;
const DEMOTE_AT = 0.7;
// Six 1080p decoders at once is not a thing to ask a browser for. Cells past
// the budget keep whatever they had.
const DEFAULT_BUDGET = 2;
// A still is a *paused-inspection* affordance and nothing else. Each one is an
// ffmpeg seek-and-decode on the server: six of them measured at 858 ms, and
// playback was asking for six every 400 ms, which saturated the server so the
// mask windows queued behind them. That is what "the masks lag and then catch
// up" was. So: never while playing, never for more than a couple of cells, and
// only once a cell is magnified enough for it to be worth anything.
const MAX_STILLS = 2;
const STILL_AT = 1.6;                 // × the underlying picture before a still helps
const STILL_MS = 700;                 // floor between refreshes of one cell
const SYNC_TOLERANCE = 0.08;          // seconds of drift before a slaved video is nudged
const SYNC_HARD = 0.6;                // …and before it is seeked instead

export class Composition {
  constructor(app, root) {
    this.app = app;
    this.api = app.api;
    this.root = root;                 // the .media element, in scene coordinates
    this.cells = new Map();           // stream key -> Cell
    this.budget = DEFAULT_BUDGET;
    this.build();
  }

  get capture() { return this.app.store.get('capture'); }

  build() {
    const cap = this.capture;
    const layout = cap.layout || {};
    this.layout = layout;

    // A legacy capture has one file covering the whole scene. It is not "the
    // video of the capture" any more — it is one more rendition, the one that
    // happens to cover every cell, and it is what plays while the scene is
    // small enough that its resolution is enough.
    this.mosaic = null;
    if (layout.mosaic?.path || layout.mosaic?.w) {
      this.mosaic = h('video', {
        class: 'cell-video',
        src: this.api.mediaUrl(cap.id, '_mosaic'),
        preload: 'auto', playsinline: true, muted: true, crossorigin: 'anonymous',
        draggable: 'false',
        style: { position: 'absolute', left: '0', top: '0',
                 width: `${layout.mosaic.w}px`, height: `${layout.mosaic.h}px` },
      });
      this.mosaic.addEventListener('error', () => {
        this.mosaicBroken = true;
        this.app.toast('Mosaic unavailable',
          'Falling back to the cameras themselves — that is the better picture anyway.', 'warn');
        this.mosaic.remove();
        this.mosaic = null;
        this.update(true);
      });
      this.root.append(this.mosaic);
    }

    for (const c of layout.cells || []) {
      const stream = (cap.streams || []).find((s) => s.key === c.stream);
      if (!stream || stream.enabled === 0) continue;
      const cell = new Cell(this, c, stream);
      this.cells.set(c.stream, cell);
      this.root.append(cell.el);
    }
    this.master = null;
  }

  // A capture refresh replaces every stream object — new frame map, new clock
  // offset, new renditions. The cells hold references to the old ones, so
  // without this a corrected clock would move the masks and leave the pixels
  // exactly where they were, which is the failure the correction exists to fix.
  restream() {
    const cap = this.capture;
    let changed = false;
    for (const [key, cell] of this.cells) {
      const fresh = (cap?.streams || []).find((s) => s.key === key);
      if (fresh && fresh !== cell.stream) { cell.stream = fresh; changed = true; }
    }
    if (changed) {
      this.seek(this.app.store.get('frame'));
      this.update(true);
    }
    return changed;
  }

  destroy() {
    for (const c of this.cells.values()) c.destroy();
    this.cells.clear();
    this.mosaic?.remove();
  }

  // -------------------------------------------------------------- the clock
  // A cell is not seeked by capture time. It is seeked to the *timestamp of
  // the frame the frame map names* — because that is the frame every mask on
  // it was drawn against, and anything else is only approximately right.
  //
  // The difference is not theoretical. Seeking to `frame / fps` lands on the
  // neighbouring source frame whenever a camera's rate does not divide the
  // capture's, which on this repository's real captures is most of them. A
  // mask one frame out looks perfectly fine and is wrong, which is the worst
  // kind of wrong; measured on a three-camera test capture it happened at one
  // boundary in 304 frames, and seeking by timestamp removes it entirely.
  //
  // The clock offset needs no term here: it is already folded into the frame
  // map by the server, so applying it again would double it.
  get fps() { return this.capture?.fps || 15; }
  timeOf(frame) { return (frame + 0.25) / this.fps; }
  frameOf(t) { return clamp(Math.round(t * this.fps), 0, (this.capture?.n_frames || 1) - 1); }

  seek(frame) {
    if (this.mosaic) this.setTime(this.mosaic, this.timeOf(frame));
    for (const c of this.cells.values()) c.seek(frame);
  }

  setTime(video, want) {
    if (!video || want == null) return;
    const t = clamp(want, 0, Math.max(0, (video.duration || 1e9) - 0.001));
    if (Math.abs(video.currentTime - t) > 0.25 / this.fps) video.currentTime = t;
  }

  play(on) {
    const changed = this.playing !== on;
    this.playing = on;
    const rate = this.app.store.get('speed') || 1;
    const vids = [this.mosaic, ...[...this.cells.values()].map((c) => c.video)].filter(Boolean);
    for (const v of vids) {
      v.playbackRate = rate;
      if (on) v.play().catch(() => {}); else v.pause();
    }
    // Starting and stopping changes what each cell should be showing — a still
    // appears when you stop to look, and gets out of the way when you play.
    if (changed) this.update(true);
  }

  // Whoever is actually decoding drives the frame — deriving it from a
  // wall clock instead would drift away from the pictures on screen the first
  // time the machine is busy.
  masterVideo() {
    if (this.mosaic && !this.mosaicBroken) return { video: this.mosaic, cell: null };
    let best = null;
    for (const c of this.cells.values()) {
      if (!c.video || c.video.readyState < 2) continue;
      const area = c.spec.w * c.spec.h;
      if (!best || area > best.area) best = { video: c.video, cell: c, area };
    }
    return best;
  }

  currentFrame() {
    const m = this.masterVideo();
    if (!m) return null;
    if (!m.cell) return this.frameOf(m.video.currentTime);
    return m.cell.frameAt(m.video.currentTime);
  }

  // Everything that is not the master is held to it. Small drift is corrected
  // by leaning on playbackRate — a seek every few seconds is visible, a 3%
  // rate change is not.
  syncSlaves(frame) {
    const rate = this.app.store.get('speed') || 1;
    const master = this.masterVideo()?.video;
    for (const c of this.cells.values()) {
      const v = c.video;
      if (!v || v === master || v.readyState < 2) continue;
      const want = c.timeFor(frame);
      if (want == null) continue;
      const drift = v.currentTime - clamp(want, 0, Math.max(0, (v.duration || 1e9) - 0.001));
      if (!this.playing) { if (Math.abs(drift) > 0.5 / this.fps) this.setTime(v, want); continue; }
      if (Math.abs(drift) > SYNC_HARD) this.setTime(v, want);
      else if (Math.abs(drift) > SYNC_TOLERANCE) v.playbackRate = rate * (drift > 0 ? 0.97 : 1.03);
      else if (v.playbackRate !== rate) v.playbackRate = rate;
    }
  }

  // ------------------------------------------------------------------- LOD
  // One function. Called on every zoom, pan, resize and layout change, by
  // every gesture, so there is nothing to remember and nothing to forget.
  update(force = false) {
    const vp = this.app.viewport;
    if (!vp) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const wanted = [];
    for (const cell of this.cells.values()) {
      const onScreen = cell.spec.w * vp.scale * dpr;
      const visible = vp.cellOnScreen?.(cell.spec) !== false;
      wanted.push({ cell, onScreen, want: cell.choose(onScreen, visible), visible });
    }
    // The biggest cells get the budget: if you have zoomed one camera to fill
    // the window, that is the one you are looking at.
    wanted.sort((a, b) => b.onScreen - a.onScreen);
    let spent = 0;
    let stills = 0;
    for (const w of wanted) {
      if (w.want?.kind === 'still') {
        // Each still is an ffmpeg run. Two of them is a considered look at a
        // seam between cameras; six is a stalled server.
        if (++stills > MAX_STILLS) { w.cell.apply(null, force); continue; }
        w.cell.apply(w.want, force);
        continue;
      }
      const heavy = w.want && w.want.kind === 'video';
      if (heavy && !this.mosaic) {
        // With no mosaic underneath, every cell must carry itself — the budget
        // cannot apply, or half the grid would be blank. Small renditions are
        // cheap; this is what they are for.
        w.cell.apply(w.want, force);
        continue;
      }
      if (heavy && spent >= this.budget && !w.cell.isFull) { w.cell.apply(null, force); continue; }
      if (heavy) spent++;
      w.cell.apply(w.want, force);
    }
  }

  frameChanged(frame) {
    for (const c of this.cells.values()) c.onFrame(frame);
  }
}

// ---------------------------------------------------------------------- cell
class Cell {
  constructor(comp, spec, stream) {
    this.comp = comp;
    this.spec = spec;                    // {stream, x, y, w, h}
    this.stream = stream;
    this.el = h('div', { class: 'cell', style: {
      position: 'absolute', left: `${spec.x}px`, top: `${spec.y}px`,
      width: `${spec.w}px`, height: `${spec.h}px`, overflow: 'hidden' } });
    this.video = null;
    this.pending = null;                 // the element loading a better rendition
    this.still = null;
    this.current = null;                 // {kind, id}
    this.stillAt = 0;
  }

  // Two corrections, and they are different things. `time_offset` is the
  // camera's clock error, a fact about the world. `t_offset` is a constant
  // rebase of the rendition's own timeline against its source — usually zero,
  // and `media.prepare` refuses to offer any rendition where it is not
  // constant, because that would put every mask one frame out.
  // -- time, exactly ------------------------------------------------------
  // `timeFor(captureFrame)` is the whole contract with the annotations: the
  // instant at which this cell shows the very frame the frame map named. Both
  // corrections live here and nowhere else — the frame map has already folded
  // in the camera's clock offset, and `t_offset` is the constant rebase of a
  // rendition's timeline against its source (usually zero; `media.prepare`
  // refuses to offer any rendition where it is not constant).
  timeFor(captureFrame, rend = this.current) {
    const stamps = this.stream.timestamps;
    const fm = this.stream.frameMap;
    const t0 = rend?.t_offset || 0;
    if (!stamps || !stamps.length) {
      // No timestamps for this stream: fall back to capture time and the raw
      // clock offset. Less exact, and the Capture panel says so and offers the
      // probe that fixes it.
      return this.comp.timeOf(captureFrame) - (this.stream.time_offset || 0) + t0;
    }
    const i = fm && fm.length
      ? fm[clamp(captureFrame, 0, fm.length - 1)]
      : clamp(captureFrame, 0, stamps.length - 1);
    const j = clamp(i, 0, stamps.length - 1);
    // land a quarter of the way into the frame rather than exactly on its
    // boundary, where float error decides which side you get
    const gap = j + 1 < stamps.length ? stamps[j + 1] - stamps[j] : 0.04;
    return stamps[j] + gap * 0.25 + t0;
  }

  // The inverse, for when this cell is the one driving playback.
  frameAt(videoTime) {
    const stamps = this.stream.timestamps;
    const fm = this.stream.frameMap;
    if (!stamps?.length || !fm?.length) {
      return this.comp.frameOf(videoTime + (this.stream.time_offset || 0));
    }
    const t = videoTime - (this.current?.t_offset || 0);
    let lo = 0, hi = stamps.length;                 // last i with stamps[i] <= t
    while (lo < hi) { const m = (lo + hi) >> 1; if (stamps[m] <= t) lo = m + 1; else hi = m; }
    const i = clamp(lo - 1, 0, stamps.length - 1);
    let a = 0, b = fm.length - 1;                   // first capture frame mapping to i
    while (a < b) { const m = (a + b) >> 1; if (fm[m] < i) a = m + 1; else b = m; }
    return clamp(a, 0, fm.length - 1);
  }
  get isFull() { return this.current?.kind === 'video' && this.current.id !== 'grid'; }
  get renditions() { return this.stream.renditions || []; }

  // What this cell should be showing, given how big it is right now.
  //
  //   · the smallest rendition that is not smaller than the cell on screen —
  //     anything bigger is decoding pixels nobody can see;
  //   · if none is big enough, the biggest there is;
  //   · if there are none at all (an HEVC source nobody has transcoded), a
  //     still, which is the whole reason zoom does something useful today
  //     rather than after a ten-minute transcode.
  choose(onScreen, visible) {
    if (!visible) return null;
    const rends = this.renditions;
    if (!rends.length) {
      // No decodable video for this stream at all. Something else has to be
      // showing the moving picture — the legacy mosaic — and a still is only
      // worth its cost once this cell is magnified well past what that holds.
      const behind = this.comp.mosaic && !this.comp.mosaicBroken;
      const dpr = window.devicePixelRatio || 1;
      const magnified = onScreen > this.spec.w * STILL_AT * dpr;
      // While playing, a still is the wrong tool: it cannot keep up, it strobes
      // as each one swaps in, and it takes the server away from serving masks.
      // The moving picture underneath is the better answer, even blurrier.
      if (this.comp.playing && behind) return null;
      if (!magnified && behind) return null;
      return { kind: 'still', id: 'still', w: Math.round(onScreen) };
    }
    const cur = this.current;
    let pick = rends[rends.length - 1];
    for (const r of rends) {
      if (r.w >= onScreen) { pick = r; break; }
    }
    // Hysteresis: stay where we are unless the cell has grown past this
    // rendition or shrunk well below it.
    if (cur?.kind === 'video') {
      const now = rends.find((r) => r.id === cur.id);
      if (now && pick.w > now.w && onScreen < now.w * PROMOTE_AT) pick = now;
      if (now && pick.w < now.w && onScreen > now.w * DEMOTE_AT) pick = now;
    }
    return { kind: 'video', id: pick.id, w: pick.w, t_offset: pick.t_offset || 0 };
  }

  apply(want, force) {
    if (!want) {
      if (this.current && this.current.kind !== 'none') this.clear();
      return;
    }
    if (!force && this.current && this.current.kind === want.kind && this.current.id === want.id) {
      if (want.kind === 'still') this.refreshStill();
      return;
    }
    if (want.kind === 'still') { this.clearVideo(); this.current = want; this.refreshStill(); return; }
    this.promote(want);
  }

  // Load the new rendition *beside* the old one and swap on `seeked`, so
  // promotion has no black frame — the thing you were looking at stays on
  // screen until the better copy of it is ready to replace it.
  promote(want) {
    if (this.pending?.dataset.rid === want.id) return;
    const comp = this.comp;
    const cap = comp.capture;
    const frame = comp.app.store.get('frame');
    const at = this.timeFor(frame, want);
    const v = h('video', {
      class: 'cell-video', preload: 'auto', playsinline: true, muted: true,
      crossorigin: 'anonymous', draggable: 'false',
      src: comp.api.mediaUrl(cap.id, this.stream.key, want.id),
      dataset: { rid: want.id, toff: String(want.t_offset || 0) },
      style: { position: 'absolute', inset: '0', width: '100%', height: '100%',
               objectFit: 'fill', opacity: '0' },
    });
    v.addEventListener('error', () => {
      if (this.pending === v) { this.pending = null; v.remove(); }
      // A rendition the server promised but cannot serve: drop to a still
      // rather than leaving a hole, and say nothing — the Capture workspace
      // is where a missing file gets explained.
      this.current = { kind: 'still', id: 'still', w: this.spec.w };
      this.refreshStill();
    }, { once: true });
    const ready = () => {
      if (this.pending !== v) return;
      this.pending = null;
      this.clearVideo();
      this.clearStill();
      v.style.opacity = '1';
      this.video = v;
      this.current = { kind: 'video', id: want.id, t_offset: want.t_offset || 0 };
      v.playbackRate = comp.app.store.get('speed') || 1;
      if (comp.playing) v.play().catch(() => {});
      comp.app.viewport?.invalidate();
    };
    v.addEventListener('seeked', ready, { once: true });
    v.addEventListener('loadeddata', () => {
      comp.setTime(v, at);
      if (v.readyState >= 2 && Math.abs(v.currentTime - at) < 0.25 / comp.fps) ready();
    }, { once: true });
    this.pending = v;
    this.el.append(v);
  }

  // -------------------------------------------------------------- stills
  // The rung of the ladder that makes zoom work *today*, on a capture whose
  // sources a browser cannot decode and which nobody has transcoded: ask the
  // server for one full-resolution frame. Paused-and-zoomed is when you
  // actually want to look closely, and it is exactly when a still is right.
  refreshStill() {
    const comp = this.comp;
    const frame = comp.app.store.get('frame');
    const width = Math.min(4096, Math.max(640, Math.round(
      this.spec.w * (comp.app.viewport?.scale || 1) * (window.devicePixelRatio || 1))));
    const bucket = Math.pow(2, Math.ceil(Math.log2(width)));   // few distinct sizes, so the cache hits
    const url = `/api/captures/${comp.capture.id}/frame/${this.stream.key}` +
      `?frame=${frame}&w=${bucket}${comp.api.token ? `&token=${encodeURIComponent(comp.api.token)}` : ''}`;
    if (this.stillUrl === url) return;
    const now = performance.now();
    if (now - this.stillAt < STILL_MS) return;
    this.stillAt = now;
    this.stillUrl = url;
    const img = h('img', { src: url, draggable: 'false', style: {
      position: 'absolute', inset: '0', width: '100%', height: '100%',
      objectFit: 'fill', opacity: '0', transition: 'opacity .12s' } });
    img.addEventListener('load', () => {
      if (this.stillUrl !== url) { img.remove(); return; }
      this.clearStill();
      img.style.opacity = '1';
      this.still = img;
    });
    img.addEventListener('error', () => img.remove());
    this.el.append(img);
  }

  onFrame(frame) {
    if (this.current?.kind === 'still' && !this.comp.playing) this.refreshStill();
  }

  seek(frame) {
    if (this.video) this.comp.setTime(this.video, this.timeFor(frame));
    if (this.pending) {
      this.comp.setTime(this.pending, this.timeFor(frame,
        { t_offset: parseFloat(this.pending.dataset.toff || '0') }));
    }
    if (this.current?.kind === 'still') this.refreshStill();
  }

  clearVideo() { this.video?.remove(); this.video = null; }
  clearStill() { this.still?.remove(); this.still = null; this.stillUrl = null; }
  clear() { this.clearVideo(); this.clearStill(); this.pending?.remove(); this.pending = null;
            this.current = { kind: 'none', id: '' }; }
  destroy() { this.clear(); this.el.remove(); }
}
