// Small DOM + formatting helpers. No framework: a plugin should be able to read
// the host's source in an afternoon, and a build step would put a wall between
// "I wrote a plugin" and "the server is serving it".

export function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k === 'html') el.innerHTML = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  add(el, kids);
  return el;
}

function add(el, kids) {
  for (const k of kids.flat(4)) {
    if (k == null || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
}

export const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };
export const mount = (el, ...kids) => { clear(el); add(el, kids); return el; };
export const qs = (sel, root = document) => root.querySelector(sel);

export const clamp = (v, a, b) => v < a ? a : v > b ? b : v;
export const pad = (n, w = 2) => String(n).padStart(w, '0');

export function timecode(frame, fps) {
  if (!fps) return String(frame);
  const t = frame / fps;
  const m = Math.floor(t / 60), s = Math.floor(t % 60), f = Math.round(frame % fps);
  return `${pad(m)}:${pad(s)}.${pad(f)}`;
}

export function bytes(n) {
  const u = ['B', 'kB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n < 10 && i ? n.toFixed(1) : Math.round(n)} ${u[i]}`;
}

export function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

// Deterministic, readable colours for arbitrary keys (track ids, identities).
// Golden-angle hues keep neighbours apart; the S/L band keeps them legible on
// video in both themes.
export function keyColor(key, sat = 68, light = 58) {
  const s = String(key);
  let hash = 2166136261;
  for (let i = 0; i < s.length; i++) { hash ^= s.charCodeAt(i); hash = Math.imul(hash, 16777619); }
  const hue = ((hash >>> 0) * 137.508) % 360;
  return `hsl(${hue.toFixed(1)} ${sat}% ${light}%)`;
}

export function cidColor(cid) {
  if (cid == null) return null;
  if (cid === 200) return 'hsl(0 0% 62%)';            // the OUT sentinel
  const hue = (cid * 137.508) % 360;
  return `hsl(${hue.toFixed(1)} 72% 58%)`;
}

export const debounce = (fn, ms = 120) => {
  let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
};

export const raf = () => new Promise(r => requestAnimationFrame(r));
