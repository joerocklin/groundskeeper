// Drives the Now tab's Mower marker (issue #113) against a stub DOM, a fake
// clock, fake timers and a fake websocket connection
// (tests/test_panel_mower_position.py). Prints JSON describing what the panel
// did. `renders` counts whole-panel re-renders (writes to the shadow root's
// innerHTML): the marker must move without any.
const vm = require('node:vm');
const fs = require('node:fs');
const [, , file] = process.argv;
const source = fs.readFileSync(file, 'utf8');

const SQUARE = { type: 'Polygon', coordinates: [[[0, 0], [0.001, 0], [0.001, 0.001], [0, 0.001], [0, 0]]] };
const run = { run_id: 'r1', plan_id: 'p', zone_id: 'z1', zone_name: 'Front', height_mm: 70, angle_deg: 40, n: 1, of: 1, can_up: false, can_down: false };
const view = { status: 'waiting', pause_requested: false, current_run: null, current_zone_id: null,
  next_run: run, up_next: [run], failure: null, checks: [], waiting_on: [], controls: ['pause'], starting_in_s: null };
const later = () => new Promise((resolve) => setImmediate(resolve));

// An element parsed back out of the marker markup, with just the calls the panel makes.
function markerEl(markup) {
  const attrs = { 'data-shape': /data-shape="(\w+)"/.exec(markup)[1], transform: /<g class="mower"[^>]*transform="([^"]*)"/.exec(markup)[1] };
  const glyphAttrs = { transform: (/class="glyph"[^>]*transform="([^"]*)"/.exec(markup) || [])[1] };
  const title = { textContent: /<title>([^<]*)<\/title>/.exec(markup)[1] };
  const fixMatch = /<text class="fix"[^>]*>([^<]*)<\/text>/.exec(markup);
  const nodes = {
    title,
    '.glyph': { getAttribute: (k) => glyphAttrs[k], setAttribute: (k, v) => { glyphAttrs[k] = v; } },
    '.fix': fixMatch ? { textContent: fixMatch[1] } : null,
  };
  return {
    getAttribute: (k) => attrs[k], setAttribute: (k, v) => { attrs[k] = v; },
    querySelector: (sel) => nodes[sel] || null, attrs, glyphAttrs, title, nodes,
  };
}

function setup({ failing = false, hold = false } = {}) {
  let clock = 1000000;
  class FakeDate extends Date { static now() { return clock; } }
  const timers = new Map();
  let nextTimer = 1;
  const state = { renders: 0, html: '', slot: null, attempts: 0, resolveHeld: null };
  const subs = [];
  const shadow = {
    get innerHTML() { return state.html; },
    set innerHTML(v) {
      state.html = v;
      state.renders += 1;
      const m = /<g id="mower-slot">(.*)<\/g><\/svg>/s.exec(v);
      state.slot = m ? makeSlot(m[1]) : null;
    },
    querySelector(sel) {
      if (sel === '#mower') return { onchange: null };
      if (sel === '#mower-slot') return state.slot;
      return null;
    },
    querySelectorAll(sel) { return sel === '#mower-slot' && state.slot ? [state.slot] : []; },
  };
  function makeSlot(markup) {
    const slot = {
      _html: markup, el: markup ? markerEl(markup) : null, listeners: {},
      get innerHTML() { return this._html; },
      set innerHTML(v) { this._html = v; this.el = v ? markerEl(v) : null; },
      querySelector(sel) { return sel === '.mower' ? this.el : null; },
      addEventListener(type, fn) { this.listeners[type] = fn; },
    };
    return slot;
  }
  const conn = {
    async subscribeMessage(cb, msg) {
      state.attempts += 1;
      if (failing) throw new Error('refused');
      if (hold) await new Promise((resolve) => { state.resolveHeld = resolve; });
      const sub = { msg, cb, unsubbed: false };
      subs.push(sub);
      return () => { sub.unsubbed = true; };
    },
    async sendMessagePromise() { return { zones: [], channels: [], gates: [] }; },
  };
  const ctx = vm.createContext({
    HTMLElement: class { attachShadow() { this.shadowRoot = shadow; return shadow; } },
    customElements: { define(name, cls) { ctx.Panel = cls; } },
    Date: FakeDate,
    setInterval(fn) { const id = nextTimer++; timers.set(id, fn); return id; },
    clearInterval(id) { timers.delete(id); },
  });
  vm.runInContext(source, ctx);
  const p = new ctx.Panel();
  p._hass = { connection: conn, config: { unit_system: { length: 'km' } }, states: {} };
  p._started = true;
  p._mowers = [{ entry_id: 'e', title: 'Example Mower' }];
  p._entryId = 'e';
  p._tab = 'now';
  p._zoneView = { zones: [{ id: 'z1', name: 'Front', polygon: SQUARE }], channels: [], gates: [] };
  p._now = view;
  p._prompts = [];
  return {
    p, state, subs, timers,
    live: () => subs.filter((s) => !s.unsubbed),
    push: (position) => subs[subs.length - 1].cb({ position }),
    fire: () => [...timers.values()].forEach((fn) => fn()),
    advance: (s) => { clock += s * 1000; },
    slotHtml: () => (state.slot ? state.slot.innerHTML : null),
  };
}

const fix = (o = {}) => ({ latitude: 0.0005, longitude: 0.0005, direction: 90, age_s: 4, ...o });

(async () => {
  const out = {};

  // -- subscribing: only while the Now tab shows, once however often it renders --
  let t = setup();
  t.p._render(); t.p._render(); t.p._render();
  await later();
  out.subscribedOnce = t.state.attempts === 1 && t.live().length === 1;
  out.subscribeMessage = t.subs[0].msg;
  out.slotDrawnEmpty = t.slotHtml() === '';
  out.noTimerWithoutFix = t.timers.size === 0;

  // -- a fix draws the marker without re-rendering; the next ones move it in place --
  let renders = t.state.renders;
  t.push(fix());
  out.firstMarker = t.slotHtml();
  const el = t.state.slot.el;
  t.push(fix({ latitude: 0.0007, direction: 180, age_s: 1 }));
  out.sameElementKept = t.state.slot.el === el;
  out.movedTransform = el.getAttribute('transform');
  out.turnedTo = el.nodes['.glyph'].getAttribute('transform');
  out.title = el.title.textContent;
  out.rendersForPushes = t.state.renders - renders;
  out.timerWhileFix = t.timers.size;

  // the age counts on by itself, still without a re-render
  t.advance(10); t.fire();
  out.titleAfterTenSeconds = el.title.textContent;
  out.rendersForTick = t.state.renders - renders;

  // a stationary Mower: nothing is pushed, the age goes on
  t.advance(120); t.fire();
  out.titleAfterTwoMinutes = el.title.textContent;

  // -- tap shows the age as a label, tap again hides it --
  t.push(fix());
  const tap = () => t.state.slot.listeners.click({ target: { closest: (s) => (s === '.mower' ? {} : null) } });
  tap();
  out.labelAfterTap = /class="fix"[^>]*>([^<]*)</.exec(t.slotHtml())[1];
  tap();
  out.labelAfterSecondTap = t.slotHtml().includes('class="fix"');

  // -- shapes: a dot without a heading; hidden with no fix or outside the map --
  t.push(fix({ direction: null }));
  out.dotWithoutDirection = t.slotHtml().includes('<circle class="glyph"') && !t.slotHtml().includes('<polygon');
  t.push(fix({ latitude: 5, longitude: 5 }));
  out.hiddenOutsideTheMap = t.slotHtml() === '';
  t.push(fix());
  t.push(null);
  out.hiddenWithNoFix = t.slotHtml() === '';
  out.timerStopsWithNoFix = t.timers.size === 0;
  out.noErrorShown = !/class="warn"|id="notice"/.test(t.state.html);

  // -- a full re-render keeps the marker --
  t.push(fix());
  t.p._render();
  out.survivesARender = /<g id="mower-slot"><g class="mower"/.test(t.state.html);

  // -- leaving the tab closes the subscription and the timer; coming back reopens it --
  t.p._tab = 'schedule';
  t.p._render();
  out.unsubscribedOnLeaving = t.live().length === 0;
  out.timersOnAnotherTab = t.timers.size;
  out.fixForgotten = t.p._mower === null;
  t.p._tab = 'now';
  t.p._render();
  await later();
  out.resubscribed = t.live().length === 1 && t.subs.length === 2;

  // -- the panel going away closes it too --
  t.push(fix());
  t.p.disconnectedCallback();
  out.unsubscribedOnDisconnect = t.live().length === 0 && t.timers.size === 0;

  // -- the Mower switched: the other Mower's fix is not shown --
  t = setup();
  t.p._render(); await later();
  t.push(fix());
  t.p._unsubscribe();
  out.otherMowerForgotten = t.p._mower === null && t.live().length === 0;

  // -- a subscribe that lands after the tab was left is closed at once --
  t = setup({ hold: true });
  t.p._render();
  t.p._tab = 'schedule';
  t.p._render();
  t.state.resolveHeld();
  await later();
  out.lateSubscribeClosed = t.subs.length === 1 && t.live().length === 0;

  // -- a refused subscribe is not an error and not retried on every render --
  t = setup({ failing: true });
  t.p._render(); t.p._render();
  await later();
  t.p._render();
  out.refusedOnce = t.state.attempts === 1;
  out.refusedQuietly = !/class="warn"|id="notice"/.test(t.state.html) && t.slotHtml() === '';
  t.p._tab = 'schedule'; t.p._render();
  t.p._tab = 'now'; t.p._render();
  await later();
  out.retriedAfterLeaving = t.state.attempts === 2;

  process.stdout.write(JSON.stringify(out));
})();
