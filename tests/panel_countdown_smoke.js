// Drives the Now tab's settling countdown against a stub DOM, a fake clock and
// fake timers (tests/test_panel_now.py::test_the_countdown_ticks_in_place_...).
// Prints JSON describing what the panel did at each step.
const vm = require('node:vm');
const fs = require('node:fs');
const [, , file] = process.argv;

let clock = 1000000;
class FakeDate extends Date { static now() { return clock; } }
const timers = new Map();
let nextId = 1;
const starting = { textContent: '' };
const shadow = {
  innerHTML: '',
  querySelector(sel) {
    if (sel === '#mower') return { onchange: null };
    if (sel === '#starting') return this.innerHTML.includes('id="starting"') ? starting : null;
    return null;
  },
  querySelectorAll() { return []; },
};
const ctx = vm.createContext({
  HTMLElement: class { attachShadow() { this.shadowRoot = shadow; return shadow; } },
  customElements: { define(name, cls) { ctx.Panel = cls; } },
  Date: FakeDate,
  setInterval(fn) { const id = nextId++; timers.set(id, fn); return id; },
  clearInterval(id) { timers.delete(id); },
});
vm.runInContext(fs.readFileSync(file, 'utf8'), ctx);
const p = new ctx.Panel();
p._hass = { config: { unit_system: { length: 'km' } }, states: {} };
p._mowers = [{ entry_id: 'e', title: 'Example Mower' }];
p._entryId = 'e';
p._tab = 'now';
p._zoneView = { zones: [], channels: [], gates: [] };
p._prompts = [];
const run = { run_id: 'r1', plan_id: 'p', zone_id: 'z1', zone_name: 'Front', height_mm: 70, angle_deg: 40, n: 1, of: 1, can_up: false, can_down: false };
const waiting = { status: 'waiting', pause_requested: false, current_run: null, current_zone_id: null,
  next_run: run, up_next: [run], failure: null, checks: [] };
const windowOpen = { ...waiting, waiting_on: [], starting_in_s: 38, controls: ['start_now', 'pause'] };
const failing = { ...waiting, starting_in_s: null, controls: ['pause'],
  waiting_on: [{ check: 'mower_ready', name: 'Mower ready', reason: 'battery_low' }] };
const tick = () => [...timers.values()].forEach((fn) => fn());
const push = (view) => { p._now = view; p._nowAt = clock; p._render(); };

const out = {};
push(windowOpen);
out.rendered = shadow.innerHTML.includes('Starting in 38 s.');
out.timersWhileOpen = timers.size;
push(windowOpen); // a pushed view re-renders; it must not stack a second timer
out.timersAfterRepush = timers.size;
clock += 10000; tick();
out.afterTenSeconds = starting.textContent;
clock += 100000; tick();
out.afterTheWindow = starting.textContent;
push(failing);
out.timersWhileFailing = timers.size;
push(windowOpen);
p._tab = 'zones'; p._render();
out.timersOnAnotherTab = timers.size;
p._tab = 'now'; push(windowOpen);
p.disconnectedCallback();
out.timersAfterDisconnect = timers.size;
process.stdout.write(JSON.stringify(out));
