// The Display control: the button, and the panel behind it.
//
// One control, present in every workspace, because "how are these coloured"
// and "which of these am I looking at" are not questions that belong to a
// tool. Before this, the Identity tool recoloured masks as a side effect of
// being open, and there was no way to ask for that colouring anywhere else, or
// to refuse it while it was.
import { h, mount } from './util.js';
import * as ui from './ui.js';

function modeRow(app) {
  const d = app.display;
  const modes = [...d.modes.values()].sort((a, b) => (a.order ?? 100) - (b.order ?? 100));
  return h('div', { class: 'row wrap' },
    h('span', { class: 'hint grow' }, 'colour'),
    ...modes.map((m) => h('button', {
      class: `chipbtn${d.mode === m.id ? ' on' : ''}`,
      title: m.plugin ? `from ${m.plugin}` : 'built in',
      onclick: () => { d.setMode(m.id); app.renderInspector(); app.renderTop(); },
    }, m.title)));
}

function classRows(app) {
  const d = app.display;
  const rows = d.labelStats();
  if (!rows.length) return h('div', { class: 'hint' }, 'This capture has no labelled objects yet.');
  return h('div', { class: 'list' }, ...rows.map((r) => h('div', {
    class: `item${r.hidden ? ' off' : ''}`,
    title: r.hidden ? 'Hidden — nothing can draw, click, select or walk to it'
      : `${r.n} object(s)`,
    onclick: () => { d.setLabelHidden(r.label, !r.hidden); app.renderInspector(); },
  },
    h('span', { class: 'swatch', style: { background: r.hidden ? 'var(--line-2)' : d.swatch(r.label) } }),
    h('span', { class: 'k grow' }, r.label || 'unlabelled'),
    h('span', { class: 'hint mono' }, r.hidden ? 'hidden' : `${r.shown}`),
  )));
}

function filterRows(app) {
  const d = app.display;
  const toggles = [...d.filters.values()].filter((f) => !f.structural);
  if (!toggles.length) return null;
  return h('div', { class: 'row wrap' },
    h('span', { class: 'hint grow' }, 'show'),
    ...toggles.map((f) => h('button', {
      class: `chipbtn${f.on ? '' : ' on'}`,     // the filter being *off* means "shown"
      title: f.description || `from ${f.plugin}`,
      onclick: () => { d.setFilter(f.id, !f.on); app.renderInspector(); },
    }, f.title)));
}

export function displayPanel(app) {
  const d = app.display;
  const hidden = d.hiddenLabels.size;
  return ui.panel('Display', [
    modeRow(app),
    filterRows(app),
    h('div', { class: 'row' },
      h('span', { class: 'hint grow' }, 'classes'),
      hidden ? h('button', { class: 'btn sm', onclick: () => { d.showAllLabels(); app.renderInspector(); } },
        `show all (${hidden} hidden)`) : null),
    classRows(app),
    hidden ? h('div', { class: 'hint' },
      'A hidden class is hidden everywhere: it is not drawn, cannot be clicked or selected, ' +
      'and the walk steps over it. That is enforced in the core, not asked of each tool.') : null,
  ], { id: 'display' });
}

// The button in the top bar. Same state, fewer clicks away.
export function displayButton(app) {
  const d = app.display;
  const mode = d.modes.get(d.mode);
  const hidden = d.hiddenLabels.size;
  return h('button', {
    class: `btn sm${hidden ? ' warn' : ''}`,
    title: 'How annotations are coloured, and which classes are shown',
    onclick: (e) => {
      const modes = [...d.modes.values()].sort((a, b) => (a.order ?? 100) - (b.order ?? 100));
      const items = modes.map((m) => ({
        label: `colour ${m.title}`, on: m.id === d.mode, hint: m.plugin || '',
        onclick: () => { d.setMode(m.id); app.renderTop(); app.renderInspector(); },
      }));
      for (const f of d.filters.values()) {
        if (f.structural) continue;
        items.push({ label: f.on ? `show ${f.title}` : `hide ${f.title}`, on: !f.on,
                     onclick: () => { d.setFilter(f.id, !f.on); app.renderTop(); app.renderInspector(); } });
      }
      for (const r of d.labelStats()) {
        items.push({
          label: `${r.hidden ? 'show' : 'hide'} ${r.label || 'unlabelled'}`,
          hint: String(r.n), color: d.swatch(r.label), on: !r.hidden,
          onclick: () => { d.setLabelHidden(r.label, !r.hidden); app.renderTop(); app.renderInspector(); },
        });
      }
      const r = e.currentTarget.getBoundingClientRect();
      ui.menu(r.left, r.bottom + 4, items, { title: 'Display' });
    },
  }, '◑', h('span', { class: 'hint' }, hidden ? `${mode?.title || ''} · ${hidden} hidden`
    : (mode?.title || '')));
}
