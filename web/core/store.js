// A 40-line observable. Enough for one app, small enough that a plugin author
// never has to learn a framework to keep a panel in sync.

export class Store {
  constructor(state = {}) {
    this.state = state;
    this.subs = new Map();      // key ('*' or field) -> Set<fn>
  }

  get(k) { return this.state[k]; }

  set(patch) {
    const changed = [];
    for (const [k, v] of Object.entries(patch)) {
      if (this.state[k] !== v) { this.state[k] = v; changed.push(k); }
    }
    if (changed.length) this.emit(changed);
    return changed;
  }

  // force a notification even when the value is the same object mutated in place
  touch(...keys) { this.emit(keys); }

  emit(keys) {
    const called = new Set();
    for (const k of keys) {
      for (const fn of this.subs.get(k) || []) if (!called.has(fn)) { called.add(fn); fn(this.state, k); }
    }
    for (const fn of this.subs.get('*') || []) if (!called.has(fn)) { called.add(fn); fn(this.state, keys); }
  }

  on(keys, fn) {
    for (const k of [].concat(keys)) {
      if (!this.subs.has(k)) this.subs.set(k, new Set());
      this.subs.get(k).add(fn);
    }
    return () => { for (const k of [].concat(keys)) this.subs.get(k)?.delete(fn); };
  }
}
