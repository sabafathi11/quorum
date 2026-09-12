// Shared chrome: panels, toasts, modals, and the little form builder plugins
// use so a job's parameters get a dialog without anyone hand-writing one.
import { h, mount, clear } from './util.js';

const overlays = () => document.getElementById('overlays');

// ------------------------------------------------------------------- toasts
let toastHost;
export function toast(title, message = '', kind = '', { sticky = false } = {}) {
  if (!toastHost) overlays().append(toastHost = h('div', { class: 'toasts' }));
  let el;
  const close = () => {
    el.style.opacity = '0'; el.style.transition = 'opacity .3s';
    setTimeout(() => el.remove(), 320);
  };
  el = h('div', { class: `toast ${kind}` },
    h('div', { class: 't' }, title),
    message && h('div', { class: 'm' }, message),
    sticky && h('div', { class: 'row', style: { marginTop: '7px', justifyContent: 'flex-end' } },
      h('button', { class: 'btn sm primary', onclick: close }, 'OK')));
  toastHost.append(el);
  if (sticky) return el;
  const life = kind === 'err' ? 9000 : 4200;
  setTimeout(close, life);
  return el;
}

// ------------------------------------------------------------------- panels
export function panel(title, content, { actions = [], collapsed = false, id = '' } = {}) {
  const body = h('div', { class: 'content' }, content);
  const el = h('section', { class: `panel${collapsed ? ' collapsed' : ''}`, dataset: { panel: id } },
    h('h3', { onclick: (e) => { if (!e.target.closest('button')) el.classList.toggle('collapsed'); } },
      h('span', { class: 'grow' }, title), ...actions),
    body);
  el.body = body;
  return el;
}

export function stat(label, value, cls = '') {
  return h('div', { class: 'stat' }, h('span', { class: 'dim' }, label),
    h('b', { class: cls }, value));
}

// ------------------------------------------------------------------- modals
export function modal({ title, content, ok = 'OK', cancel = 'Cancel', onOk, wide = false }) {
  return new Promise((resolve) => {
    const close = (v) => { scrim.remove(); document.removeEventListener('keydown', esc, true); resolve(v); };
    const esc = (e) => {
      if (e.key === 'Escape') { e.stopPropagation(); close(null); }
      if (e.key === 'Enter' && e.target.tagName !== 'TEXTAREA') { e.stopPropagation(); accept(); }
    };
    const accept = async () => {
      const v = onOk ? await onOk() : true;
      if (v !== false) close(v);
    };
    const scrim = h('div', { class: 'modal-scrim', onclick: (e) => { if (e.target === scrim) close(null); } },
      h('div', { class: 'modal', style: wide ? { width: 'min(820px,100%)' } : {} },
        h('header', {}, title),
        h('div', { class: 'content' }, content),
        h('footer', {},
          cancel && h('button', { class: 'btn', onclick: () => close(null) }, cancel),
          h('button', { class: 'btn primary', onclick: accept }, ok))));
    overlays().append(scrim);
    document.addEventListener('keydown', esc, true);
    setTimeout(() => scrim.querySelector('input,select,textarea')?.focus(), 10);
  });
}

export const confirm = (title, message, ok = 'Confirm') =>
  modal({ title, content: h('div', { class: 'dim' }, message), ok });

// -------------------------------------------------------------- context menu
// A list pinned to a point on screen. Used for "which of these overlapping
// masks did you mean?", where a modal would lose the thing you are pointing at.
export function menu(x, y, items, { title = '' } = {}) {
  const close = () => { root.remove(); document.removeEventListener('keydown', esc, true); };
  const esc = (e) => { if (e.key === 'Escape') { e.stopPropagation(); close(); } };
  const el = h('div', { class: 'ctxmenu' },
    title && h('div', { class: 'ctxmenu-title' }, title),
    ...items.map((it) => h('button', {
      class: `ctxmenu-item${it.on ? ' on' : ''}`,
      onmouseenter: () => it.onhover?.(),
      onclick: () => { close(); it.onclick?.(); },
    },
      it.color && h('span', { class: 'swatch', style: { background: it.color } }),
      h('span', { class: 'k grow' }, it.label),
      it.hint && h('span', { class: 'hint mono' }, it.hint))));
  const root = h('div', { class: 'menu-scrim', onpointerdown: (e) => { if (e.target === root) close(); },
                          oncontextmenu: (e) => { e.preventDefault(); close(); } }, el);
  overlays().append(root);
  const r = el.getBoundingClientRect();
  el.style.left = `${Math.min(x, window.innerWidth - r.width - 8)}px`;
  el.style.top = `${Math.min(y, window.innerHeight - r.height - 8)}px`;
  document.addEventListener('keydown', esc, true);
  return close;
}

// ---------------------------------------------------------------- form from
// a job/provider parameter spec — {name: {type, label, default, required}}
//
// `assets` is the list of the current capture's uploaded files, so a param of
// type `asset` renders a picker of the ones of the right kind. That is how an
// importer consumes an upload without ever learning that uploading exists: it
// asked for a file, and it gets a path.
export function form(spec, initial = {}, { assets = [], onUpload = null, app = null } = {}) {
  const values = { ...initial };
  const fields = Object.entries(spec || {}).map(([name, s]) => {
    const label = s.label || name;
    let input;
    // A parameter that is one or more files *on the server*. The sibling of
    // `asset`: that one asks for something uploaded, this one for something
    // already there. A provider gets a path either way and never learns which
    // door the file came through.
    if (s.type === 'paths' || s.type === 'path') {
      const multiple = s.type === 'paths';
      const chosen = h('div', { class: 'hint' });
      const show = () => {
        const v = values[name] || (multiple ? [] : '');
        const list = multiple ? v : (v ? [v] : []);
        clear(chosen);
        chosen.append(list.length
          ? h('div', { class: 'row wrap' }, ...list.map((pth) => h('span', { class: 'chip mono' },
              pth.split('/').pop())))
          : h('span', {}, 'Nothing picked — press Browse.'));
      };
      values[name] = values[name] ?? (multiple ? [] : '');
      show();
      const browse = h('button', {
        class: 'btn sm', onclick: async (e) => {
          e.preventDefault();
          if (!app) return toast('Cannot browse', 'No server connection here.', 'warn');
          const { pick } = await import('./browse.js');
          const got = await pick(app, { multiple, accept: s.accept || [], title: label });
          if (!got) return;
          values[name] = multiple ? got : got[0];
          show();
        },
      }, 'Browse the server…');
      return h('div', { class: 'field' }, h('label', {}, label),
        h('div', { class: 'row' }, browse), chosen,
        s.description && h('div', { class: 'hint' }, s.description));
    }
    if (s.type === 'asset') {
      const mine = assets.filter((a) => a.state === 'ready' && (!s.kind || a.kind === s.kind));
      if (!mine.length) {
        return h('div', { class: 'field' }, h('label', {}, label),
          h('div', { class: 'hint' },
            `Nothing uploaded of this kind yet${s.kind ? ` (${s.kind})` : ''}.`),
          onUpload ? h('button', { class: 'btn sm', onclick: (e) => {
            e.preventDefault(); onUpload(s.kind || ''); } }, 'Upload one…') : null);
      }
      if (s.multiple) {
        const boxes = mine.map((a) => {
          const cb = h('input', { type: 'checkbox', onchange: () => {
            values[name] = mine.filter((_, i) => boxes[i].firstChild.checked).map((x) => x.id);
          } });
          cb.checked = true;
          return h('label', { class: 'row' }, cb, h('span', { class: 'mono' }, a.name));
        });
        values[name] = mine.map((a) => a.id);
        return h('div', { class: 'field' }, h('label', {}, label), ...boxes,
          s.description && h('div', { class: 'hint' }, s.description));
      }
      input = h('select', { class: 'sel', onchange: (e) => values[name] = parseInt(e.target.value, 10) },
        ...mine.map((a) => h('option', { value: a.id }, a.name)));
      values[name] = mine[0].id;
      return h('div', { class: 'field' }, h('label', {}, label), input,
        s.description && h('div', { class: 'hint' }, s.description));
    }
    if (s.type === 'boolean') {
      input = h('input', { type: 'checkbox', onchange: (e) => values[name] = e.target.checked });
      input.checked = values[name] ?? s.default ?? false;
      values[name] = input.checked;
      return h('label', { class: 'row' }, input, h('span', {}, label));
    }
    if (s.options) {
      input = h('select', { class: 'sel', onchange: (e) => values[name] = e.target.value },
        ...s.options.map((o) => h('option', { value: o.value ?? o }, o.label ?? o)));
      input.value = values[name] ?? s.default ?? '';
    } else {
      input = h('input', {
        class: 'txt', type: s.type === 'integer' || s.type === 'number' ? 'number' : 'text',
        placeholder: s.placeholder || '',
        oninput: (e) => values[name] = s.type === 'integer' ? parseInt(e.target.value || 0, 10)
          : s.type === 'number' ? parseFloat(e.target.value || 0) : e.target.value,
      });
      input.value = values[name] ?? s.default ?? '';
    }
    values[name] = values[name] ?? s.default ?? (s.type === 'integer' ? 0 : '');
    return h('div', { class: 'field' }, h('label', {}, label), input,
      s.description && h('div', { class: 'hint' }, s.description));
  });
  return { el: h('div', { class: 'content', style: { padding: 0 } }, fields), values };
}

// --------------------------------------------------------------- empty state
export function empty(title, ...rest) {
  return h('div', { class: 'emptystate' }, h('h2', {}, title), ...rest);
}

export { h, mount, clear };
