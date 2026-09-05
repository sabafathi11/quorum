// The mask renderer. Given a payload the core never opened, put pixels on the
// overlay — and answer "what is under the cursor", which is the other half of
// what a layer type owes the host.

// Every keyframe of every visible track is a fresh mask, so during playback
// this decodes and paints ~50 of them per frame — about 1.5 M pixels at 15 fps
// on this capture. It is the single most expensive thing the client does, and
// when it overruns the frame budget the clock ticks late and the masks visibly
// trail the video. Three things keep it inside the budget:
//
//   · the run-length decode is cached per (object, frame), so recolouring —
//     switching colour mode, hovering, selecting — never decodes again;
//   · the tint is written as one 32-bit store per pixel instead of four 8-bit
//     ones, into a canvas taken from a pool rather than a fresh element;
//   · both caches are large enough to hold a couple of seconds of playback, so
//     stepping back and forth over the same frames costs nothing.
const LITTLE_ENDIAN = (() => {
  const probe = new ArrayBuffer(4);
  new Uint32Array(probe)[0] = 1;
  return new Uint8Array(probe)[0] === 1;
})();

const ALPHA_MAX = 2400;           // `${objectId}:${frame}` -> {alpha, w, h}
const TINT_MAX = 1200;            // `${objectId}:${frame}:${color}` -> {canvas, …}
const alphaCache = new Map();
const cache = new Map();
const spare = [];                 // canvases waiting to be used again

function lru(map, max, onEvict) {
  while (map.size > max) {
    const k = map.keys().next().value;
    const v = map.get(k);
    map.delete(k);
    onEvict?.(v);
  }
}

// Exported because it is the *codec*, not the renderer: anything that has a
// mask payload and needs its pixels — a SAM preview that is not a layer yet —
// should read it with this rather than carry a second copy of the run
// convention. `masks/plugin.py` borrows the server-side half the same way.
export function decodeRLE(rle, w, h) {
  const alpha = new Uint8Array(w * h);
  if (!rle) return alpha;
  let i = 0, on = false;
  for (const part of rle.split(',')) {
    const run = +part;
    if (on) alpha.fill(1, i, i + run);
    i += run;
    on = !on;
    if (i >= alpha.length) break;
  }
  return alpha;
}

function alphaOf(entry) {
  const key = `${entry.object.id}:${entry.frame}`;
  let hit = alphaCache.get(key);
  if (hit) { alphaCache.delete(key); alphaCache.set(key, hit); return hit; }
  const { box, rle } = entry.payload;
  const [, , w, h] = box;
  if (!w || !h) return null;
  hit = { alpha: decodeRLE(rle, w, h), w, h };
  alphaCache.set(key, hit);
  lru(alphaCache, ALPHA_MAX);
  return hit;
}

function canvasOf(w, h) {
  const cv = spare.pop() || document.createElement('canvas');
  if (cv.width !== w) cv.width = w;
  if (cv.height !== h) cv.height = h;
  return cv;
}

function bake(entry, color) {
  const a = alphaOf(entry);
  if (!a) return null;
  const { alpha, w, h } = a;
  const img = new ImageData(w, h);
  // One store per pixel rather than four. The byte order is the platform's, so
  // the colour is packed to match rather than assumed.
  const px32 = new Uint32Array(img.data.buffer);
  const [r, g, b] = rgb(color);
  const packed = LITTLE_ENDIAN
    ? (255 << 24) | (b << 16) | (g << 8) | r
    : (r << 24) | (g << 16) | (b << 8) | 255;
  for (let i = 0; i < alpha.length; i++) if (alpha[i]) px32[i] = packed;
  const cv = canvasOf(w, h);
  cv.getContext('2d').putImageData(img, 0, 0);
  return { canvas: cv, alpha, w, h };
}

const rgbCache = new Map();
function rgb(color) {
  let v = rgbCache.get(color);
  if (v) return v;
  const cv = document.createElement('canvas');
  cv.width = cv.height = 1;
  const c = cv.getContext('2d', { willReadFrequently: true });
  c.fillStyle = color;
  c.fillRect(0, 0, 1, 1);
  v = [...c.getImageData(0, 0, 1, 1).data].slice(0, 3);
  rgbCache.set(color, v);
  return v;
}

function baked(entry, color) {
  const key = `${entry.object.id}:${entry.frame}:${color}`;
  let hit = cache.get(key);
  if (hit) { cache.delete(key); cache.set(key, hit); return hit; }
  hit = bake(entry, color);
  if (!hit) return null;
  cache.set(key, hit);
  // An evicted canvas goes back to the pool; allocating fifty a frame is what
  // makes the garbage collector show up in the middle of playback.
  lru(cache, TINT_MAX, (v) => { if (v?.canvas && spare.length < 64) spare.push(v.canvas); });
  return hit;
}

export function MaskRenderer({ app }) {
  let showLabels = false;
  app.bus.on('mask.labels', (v) => { showLabels = v; app.viewport?.invalidate(); });

  const place = (entry, view) => {
    const cell = view.cellOf(entry.stream.key);
    if (!cell) return null;
    const sx = cell.w / (entry.stream.width || cell.w);
    const sy = cell.h / (entry.stream.height || cell.h);
    const [l, t, w, h] = entry.payload.box;
    return { x: cell.x + l * sx, y: cell.y + t * sy, w: w * sx, h: h * sy, sx, sy, cell };
  };

  return {
    draw(g, view) {
      g.save();
      g.imageSmoothingEnabled = false;
      for (const entry of view.entries) {
        const p = place(entry, view);
        if (!p || p.w <= 0) continue;
        const selected = view.selection.has(entry.object.id);
        const hovered = view.hover?.object?.id === entry.object.id;
        const style = view.styleFor(entry, {
          color: app.keyColor(`${entry.stream.key}/${entry.object.key}`),
          alpha: 0.42, label: `${entry.object.key}`, hidden: false,
        });
        if (style.hidden) continue;
        const img = baked(entry, style.color);
        if (!img) continue;
        g.globalAlpha = (view.opacity ?? 1) * (selected ? 0.82 : hovered ? 0.66 : style.alpha);
        g.drawImage(img.canvas, p.x, p.y, p.w, p.h);
        g.globalAlpha = 1;
        if (selected || hovered) {
          g.strokeStyle = style.color;
          g.lineWidth = (selected ? 2 : 1) / view.scale;
          g.strokeRect(p.x, p.y, p.w, p.h);
        }
        if (showLabels || selected) {
          const s = 11 / view.scale;
          g.font = `${s}px ui-monospace, monospace`;
          const text = style.label;
          const tw = g.measureText(text).width;
          g.fillStyle = 'rgba(0,0,0,.72)';
          g.fillRect(p.x, p.y - s - 3 / view.scale, tw + 6 / view.scale, s + 3 / view.scale);
          g.fillStyle = style.color;
          g.fillText(text, p.x + 3 / view.scale, p.y - 3 / view.scale);
        }
      }
      g.restore();
    },

    hitTest(x, y, view) {
      return this.hitTestAll(x, y, view)[0] || null;
    },

    // Every mask whose *pixels* cover this point, topmost (last drawn) first.
    // Returning the whole stack rather than the winner is what lets the host
    // offer the one underneath.
    hitTestAll(x, y, view) {
      const out = [];
      for (let i = view.entries.length - 1; i >= 0; i--) {
        const entry = view.entries[i];
        const p = place(entry, view);
        if (!p) continue;
        if (x < p.x || y < p.y || x >= p.x + p.w || y >= p.y + p.h) continue;
        // Only the run-length decode is needed to answer "is this pixel set",
        // and that is cached — so hovering never tints anything.
        const a = alphaOf(entry);
        if (!a) continue;
        const ix = Math.floor((x - p.x) / p.w * a.w);
        const iy = Math.floor((y - p.y) / p.h * a.h);
        if (a.alpha[iy * a.w + ix]) out.push(entry);
      }
      return out;
    },

    // Where this entry sits in world coordinates — the host draws pulses and
    // focus rings with it without learning what a mask is.
    boundsOf(entry, view) {
      const p = place(entry, view);
      return p && p.w > 0 ? { x: p.x, y: p.y, w: p.w, h: p.h } : null;
    },

    dispose() { cache.clear(); alphaCache.clear(); spare.length = 0; },
  };
}

export default {
  id: 'mask_layer',
  activate(ctx) {
    ctx.registerLayerRenderer('mask.rle', ({ layer, data, app }) => MaskRenderer({ layer, data, app }));

    let labels = false;
    ctx.registerCommand({
      id: 'labels', title: 'Mask labels on / off', keys: ['L'], group: 'Masks',
      run: () => { labels = !labels; ctx.emit('mask.labels', labels); },
    });

    // What a click *means* is the active tool's business; a layer type only
    // says what is under the cursor (see hitTest above).
  },
};
