// Picking files that are already on the server.
//
// The upload dialog answers "this file is on my laptop". This is the other
// half, and on the deployment that matters it is the only half: the cameras
// write to a share the server has mounted, and the files never touch your
// machine at all. So the picker is a *remote* file browser — a directory
// listing fetched from the server, inside the media roots and nowhere else.
//
// One thing about it is not decoration. `records` is a single flat directory
// with about 1,400 recordings in it, named `cam<n>_<stamp>.mp4`, and picking
// the six that belong to one session out of that by scrolling is miserable.
// So the filter box is the primary control, it filters *on the server*, and
// typing a stamp narrows 1,400 files to the six cameras of one capture —
// which "Select all" then takes in one click.
import { h, mount, bytes } from './util.js';
import * as ui from './ui.js';

const isVideo = (name) => /\.(mp4|mkv|mov|avi|ts|webm|m4v)$/i.test(name);

function matches(name, accept) {
  if (!accept || !accept.length) return true;
  return accept.some((ext) => name.toLowerCase().endsWith(ext.toLowerCase()));
}

// Open the picker. Resolves to an array of absolute server paths, or null.
//   pick(app, { multiple, accept, title, start })
export async function pick(app, {
  multiple = true, accept = [], title = 'Files on the server', start = '',
} = {}) {
  const chosen = new Map();                 // path -> entry, across directories
  let at = start || sessionStorage.getItem('quorum.browse.at') || '';
  let filter = '';
  let listing = null;

  const list = h('div', { class: 'browse-list' });
  const crumbs = h('div', { class: 'browse-crumbs mono hint' });
  const status = h('div', { class: 'hint grow' });
  const picked = h('div', { class: 'browse-picked' });
  const filterBox = h('input', {
    class: 'txt', placeholder: 'filter by name — a stamp like 20260612_021454',
    oninput: (e) => { filter = e.target.value; schedule(); },
  });

  let timer = null;
  const schedule = () => { clearTimeout(timer); timer = setTimeout(load, 220); };

  const drawPicked = () => {
    mount(picked, ...(chosen.size ? [
      h('div', { class: 'row' },
        h('span', { class: 'hint grow' },
          `${chosen.size} selected · ${bytes([...chosen.values()].reduce((a, e) => a + e.size, 0))}`),
        h('button', { class: 'btn sm', onclick: () => { chosen.clear(); drawPicked(); draw(); } },
          'Clear')),
      h('div', { class: 'row wrap' }, ...[...chosen.values()].map((e) => h('button', {
        class: 'chip', title: e.path,
        onclick: () => { chosen.delete(e.path); drawPicked(); draw(); },
      }, e.name, h('span', { class: 'hint' }, '×')))),
    ] : [h('div', { class: 'hint' }, 'Nothing selected yet.')]));
  };

  const toggle = (e) => {
    if (chosen.has(e.path)) chosen.delete(e.path);
    else {
      if (!multiple) chosen.clear();
      chosen.set(e.path, e);
    }
    drawPicked();
    draw();
  };

  const draw = () => {
    if (!listing) return;
    const entries = listing.entries.filter((e) => e.dir || matches(e.name, accept));
    const files = entries.filter((e) => !e.dir);
    mount(crumbs, listing.path
      ? h('span', {}, listing.path)
      : h('span', {}, 'Media roots — everything this server is willing to read'));
    mount(list,
      listing.parent ? h('button', {
        class: 'browse-row', onclick: () => { at = listing.parent; load(); },
      }, h('span', { class: 'ic' }, '↰'), h('span', { class: 'k grow mono' }, '..')) : null,
      ...entries.map((e) => {
        if (e.dir) {
          return h('button', {
            class: 'browse-row', onclick: () => { at = e.path; load(); },
          }, h('span', { class: 'ic' }, '📁'), h('span', { class: 'k grow mono' }, e.name),
             h('span', { class: 'hint' }, '›'));
        }
        const on = chosen.has(e.path);
        return h('button', {
          class: `browse-row${on ? ' on' : ''}${e.readable ? '' : ' unreadable'}`,
          title: e.readable ? e.path
            : `${e.path} — this server cannot read it (check the mount and who it runs as)`,
          onclick: () => e.readable && toggle(e),
        },
          h('span', { class: 'ic' }, on ? '☑' : (isVideo(e.name) ? '🎞' : '▤')),
          h('span', { class: 'k grow mono' }, e.name),
          h('span', { class: 'hint mono' }, e.readable ? bytes(e.size) : 'unreadable'));
      }),
      entries.length ? null : ui.empty('Nothing here',
        h('div', { class: 'hint' }, filter ? 'No file in this directory matches that filter.'
          : 'This directory has no files this picker will show.')));
    const more = listing.total > listing.shown
      ? ` · showing ${listing.shown} of ${listing.total} — narrow the filter to see the rest` : '';
    mount(status, h('span', {},
      `${files.length} file(s)${more}`),
      files.length && multiple ? h('button', {
        class: 'btn sm', style: { marginLeft: '8px' },
        onclick: () => { files.forEach((e) => e.readable && chosen.set(e.path, e)); drawPicked(); draw(); },
      }, 'Select all') : null);
  };

  const load = async () => {
    try {
      listing = await app.api.browse(at, filter);
      at = listing.path;
      if (at) sessionStorage.setItem('quorum.browse.at', at);
      draw();
    } catch (err) {
      // A path that has gone away must not strand the dialog at a directory it
      // can never list again — fall back to the roots and say why.
      mount(list, ui.empty('Cannot list that', h('div', { class: 'hint' }, err.message),
        h('button', { class: 'btn sm', onclick: () => { at = ''; load(); } }, 'Back to the roots')));
    }
  };

  drawPicked();
  load();

  const ok = await ui.modal({
    title, wide: true, ok: multiple ? 'Use these files' : 'Use this file',
    content: [
      h('div', { class: 'row' }, filterBox),
      crumbs,
      list,
      h('div', { class: 'row' }, status),
      h('hr'),
      picked,
    ],
    onOk: () => (chosen.size ? true : (ui.toast('Nothing selected',
      'Pick at least one file, or Cancel.', 'warn'), false)),
  });
  return ok ? [...chosen.keys()] : null;
}
