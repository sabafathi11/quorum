// Keyboard first, because the tools this replaces were. A binding belongs to
// whoever registered it; tool bindings only fire while that tool is active, so
// two plugins can both want `D` without a negotiation.
import { h, mount } from './util.js';

const NORM = { ' ': 'Space', Escape: 'Esc', ArrowLeft: 'Left', ArrowRight: 'Right', ArrowUp: 'Up', ArrowDown: 'Down', Delete: 'Del' };

export function chord(e) {
  let k = NORM[e.key] || (e.key.length === 1 ? e.key.toUpperCase() : e.key);
  const mods = [];
  if (e.ctrlKey || e.metaKey) mods.push('Ctrl');
  if (e.altKey) mods.push('Alt');
  if (e.shiftKey && (k.length > 1 || !/[A-Z0-9]/.test(k) || mods.length)) mods.push('Shift');
  else if (e.shiftKey) mods.push('Shift');
  return [...mods, k].join('+');
}

export class Keymap {
  constructor(store) {
    this.store = store;
    this.commands = new Map();      // id -> {id,title,run,keys,when,owner,group}
    this.held = new Set();
    window.addEventListener('keydown', (e) => this.onKey(e), true);
    window.addEventListener('keyup', (e) => this.onUp(e), true);
    window.addEventListener('blur', () => { for (const id of [...this.held]) this.release(id); });
  }

  register(cmd) {
    this.commands.set(cmd.id, cmd);
    return () => this.commands.delete(cmd.id);
  }

  find(key) {
    const tool = this.store.get('tool');
    let best = null;
    for (const c of this.commands.values()) {
      if (!(c.keys || []).includes(key)) continue;
      if (c.tool && c.tool !== tool) continue;
      if (c.when && !c.when()) continue;
      if (!best || (c.tool && !best.tool)) best = c;      // a tool binding beats a global one
    }
    return best;
  }

  onKey(e) {
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
    const key = chord(e);
    const cmd = this.find(key);
    if (!cmd) return;
    if (cmd.hold) {
      if (this.held.has(cmd.id)) { e.preventDefault(); return; }
      this.held.add(cmd.id);
      e.preventDefault();
      cmd.run({ down: true, key, event: e });
      return;
    }
    if (e.repeat && !cmd.repeat) return;
    e.preventDefault();
    cmd.run({ key, event: e });
  }

  onUp(e) {
    const key = chord(e);
    for (const id of [...this.held]) {
      const c = this.commands.get(id);
      if (!c) { this.held.delete(id); continue; }
      // release on the same physical key, ignoring modifier state changes
      if ((c.keys || []).some((k) => k.split('+').pop() === key.split('+').pop())) this.release(id);
    }
  }

  release(id) {
    this.held.delete(id);
    this.commands.get(id)?.run({ down: false });
  }

  // ------------------------------------------------------------- palette
  palette() {
    const list = [...this.commands.values()]
      .filter((c) => c.title && (!c.tool || c.tool === this.store.get('tool')))
      .filter((c) => !c.when || c.when());
    let sel = 0, shown = list;
    const results = h('div', { class: 'results' });
    const input = h('input', {
      placeholder: 'Run a command…', oninput: () => { sel = 0; render(); },
      onkeydown: (e) => {
        if (e.key === 'ArrowDown') { sel = Math.min(sel + 1, shown.length - 1); render(); e.preventDefault(); }
        else if (e.key === 'ArrowUp') { sel = Math.max(sel - 1, 0); render(); e.preventDefault(); }
        else if (e.key === 'Enter') { close(); shown[sel]?.run({ key: 'palette' }); }
        else if (e.key === 'Escape') close();
        e.stopPropagation();
      },
    });
    const box = h('div', { class: 'palette' }, input, results);
    const scrim = h('div', { class: 'modal-scrim', onclick: (e) => { if (e.target === scrim) close(); } }, box);
    const close = () => scrim.remove();

    const render = () => {
      const q = input.value.toLowerCase().trim();
      shown = list.filter((c) => !q || (c.title + ' ' + (c.group || '')).toLowerCase().includes(q))
        .sort((a, b) => (a.group || '').localeCompare(b.group || ''));
      mount(results, shown.slice(0, 60).map((c, i) => h('div', {
        class: `res${i === sel ? ' on' : ''}`, onclick: () => { close(); c.run({ key: 'palette' }); },
      },
        h('span', {}, c.title),
        h('span', { class: 'where' }, (c.keys || []).join(' / ') || (c.group || '')))));
    };
    render();
    document.getElementById('overlays').append(scrim);
    input.focus();
  }
}
