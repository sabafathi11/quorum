// First-run, control-level guidance.  It lives in the browser because it is
// personal learning state, not capture data: reopening the server must not
// make an experienced annotator repeat the tour.
import { h } from './util.js';

// Bump when a materially new section is introduced, so existing users see the
// new guidance once while future opens remain quiet.
const KEY = 'quorum.onboarding.v3.complete';

const steps = [
  ['capture', 'workspace-capture', 'Capture workspace', 'Ctrl+,',
   'Open capture settings for recordings, files, camera clocks, and layers.', 'Start here after opening a new recording session to check that every camera is ready.'],
  ['capture', 'capture-camera', 'Show or hide a camera', 'Click shown / hidden',
   'Include or remove one camera from the mosaic without deleting its recording.', 'Hide a camera that is obstructed so you can concentrate on the useful views.'],
  ['capture', 'capture-prepare-video', 'Prepare video for the browser', 'No shortcut',
   'Create browser-playable video renditions while keeping source-frame alignment.', 'Run this once after adding HEVC recordings so every camera can play in the mosaic.'],
  ['capture', 'capture-upload', 'Upload data', 'Click Upload',
   'Upload annotation files or other capture data to the server, then file them under a type.', 'Upload a CVAT XML export before importing its tracks into a layer.'],
  ['capture', 'capture-link-file', 'Link a server file', 'Click From the server',
   'Register a file already on the server without copying it.', 'Link recordings from a shared drive when they are already inside Quorum’s media roots.'],
  [null, 'display-controls', 'Show and hide annotation classes', 'Display menu',
   'Choose which annotation classes are visible, clickable, and included in review.', 'Hide a distracting label class temporarily, then restore it with Show all when reviewing.'],
  ['identity', 'identity-link', 'Link tracks', 'M',
   'Give selected tracks one shared identity.', 'Select the same shopper in cam2 and cam5, then link them before reviewing the next person.'],
  ['identity', 'identity-assign', 'Assign an identity', 'A',
   'Set the selected track or identity to a known identity number.', 'Use this when the person is already known as ID 14 and you want this camera track to match.'],
  ['identity', 'identity-outside', 'Mark Outside', 'O',
   'Flag a selection as outside the area or task scope.', 'Mark a person walking beyond the checkout boundary as Outside instead of assigning an identity.'],
  ['identity', 'identity-clear', 'Clear identity', 'C',
   'Remove an incorrect identity assignment while keeping the track.', 'Clear a mistaken ID 14 assignment, then use Next problem to resolve it correctly.'],
  ['identity', 'identity-next', 'Next problem', 'Shift+Space or N',
   'Jump to the next unassigned or conflicting identity and select it.', 'Use it to work through a review queue without manually searching the timeline.'],
  ['edit', 'edit-delete', 'Delete track', 'D',
   'Hide the selected track from the working set; it remains recoverable.', 'Delete a false-positive model track that is not a person.'],
  ['edit', 'edit-restore', 'Restore track', 'Shift+D',
   'Bring a deleted selected track back into the working set.', 'Restore a real customer you deleted by mistake.'],
  ['edit', 'edit-join', 'Join tracks', 'J',
   'Make selected fragments behave as one track.', 'Join two fragments of the same person after an occlusion.'],
  ['edit', 'edit-unjoin', 'Unjoin tracks', 'U',
   'Separate a joined track back into its original members.', 'Unjoin a group when you discover its fragments belong to different people.'],
  ['identity', 'workspace-identity', 'Switch Edit and Identity', 'I',
   'Toggle between the Edit and Identity workspaces without using the rail.', 'After fixing a track in Edit, press I to assign or link that person in Identity.'],
  ['sam', 'sam-send-prompt', 'Send prompt', 'Ctrl+Enter',
   'Send your positive and negative dots to SAM and preview a mask.', 'Click a person, add a negative dot on the background, then send the prompt to check the mask.'],
  ['sam', 'sam-clear-prompt', 'Clear prompt', 'Esc',
   'Discard the dots and preview on the current frame; nothing is saved.', 'Use it when the preview covers the wrong person and you want to start over.'],
  ['sam', 'sam-create-track', 'Create track', 'Enter',
   'Save the accepted preview as a new track, or replace the selected track mask.', 'When the preview outlines a new person correctly, press Enter before moving to another frame.'],
  [null, 'transport-previous', 'Previous frame', 'Left Arrow',
   'Pause and step back one frame.', 'Use it to inspect the exact frame before a person becomes hidden.'],
  [null, 'transport-play', 'Play or pause', 'Space',
   'Start or pause playback without leaving your workspace.', 'Press Space to scan movement, then press it again when you spot an identity problem.'],
  [null, 'transport-next', 'Next frame', 'Right Arrow',
   'Pause and step forward one frame.', 'Use it to check whether two masks still belong to the same person.'],
  [null, 'transport-frame', 'Go to a frame', 'Type a number, then Enter',
   'Jump directly to a precise capture frame.', 'Enter 1250 when a reviewer tells you the issue begins at frame 1250.'],
  [null, 'guide-reopen', 'Need this guide again?', '? button',
   'Open this guided tour again whenever you want a refresher.', 'Use the ? button in the top bar when you return to a tool after a break or teach a new teammate.'],
];

export class Onboarding {
  constructor(app) { this.app = app; this.index = 0; this.root = null; }

  maybeStart() {
    if (!localStorage.getItem(KEY) && !this.scheduled && !this.root) {
      this.scheduled = true;
      setTimeout(() => { this.scheduled = false; this.start(); }, 500);
    }
  }

  async start() {
    this.stop(false);
    this.index = 0;
    this.root = h('div', { class: 'guide-root' });
    document.getElementById('overlays').append(this.root);
    await this.show();
  }

  stop(complete = true) {
    this.root?.remove(); this.root = null;
    if (complete) localStorage.setItem(KEY, '1');
  }

  async show() {
    if (!this.root) return;
    const [tool, target, title, shortcut, what, example] = steps[this.index];
    if (tool && this.app.store.get('tool') !== tool) this.app.activateTool(tool);
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const el = document.querySelector(`[data-guide="${target}"]`);
    if (!el) { this.index++; return this.index < steps.length ? this.show() : this.stop(); }
    el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    await new Promise((resolve) => requestAnimationFrame(resolve));
    const r = el.getBoundingClientRect();
    const card = h('section', { class: 'guide-card', role: 'dialog', 'aria-label': `Getting started: ${title}` },
      h('div', { class: 'guide-eyebrow' }, `Getting started · ${this.index + 1} of ${steps.length}`),
      h('h2', {}, title),
      h('p', {}, what),
      h('div', { class: 'guide-shortcut' }, h('span', {}, 'Shortcut'), h('kbd', {}, shortcut)),
      h('div', { class: 'guide-example' }, h('b', {}, 'Example'), ` ${example}`),
      h('footer', {},
        h('button', { class: 'btn sm', onclick: () => this.stop() }, 'Skip tour'),
        this.index ? h('button', { class: 'btn sm', onclick: async () => { this.index--; await this.show(); } }, 'Back') : null,
        h('button', { class: 'btn sm primary', onclick: async () => {
          if (++this.index >= steps.length) this.stop(); else await this.show();
        } }, this.index === steps.length - 1 ? 'Finish' : 'Next')));
    const spot = h('div', { class: 'guide-spotlight' });
    spot.style.left = `${Math.max(4, r.left - 5)}px`; spot.style.top = `${Math.max(4, r.top - 5)}px`;
    spot.style.width = `${r.width + 10}px`; spot.style.height = `${r.height + 10}px`;
    const left = Math.min(Math.max(12, r.left), window.innerWidth - 392);
    const below = r.bottom + 14;
    card.style.left = `${left}px`;
    card.style.top = `${below + 280 < window.innerHeight ? below : Math.max(12, r.top - 300)}px`;
    this.root.replaceChildren(h('div', { class: 'guide-scrim' }), spot, card);
  }
}
