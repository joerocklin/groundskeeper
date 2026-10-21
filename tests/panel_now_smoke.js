// Renders the whole panel element against a stub DOM with a canned Now view;
// prints the Now tab markup (tests/test_panel_now.py::test_panel_renders_the_now_tab).
const vm = require('node:vm');
const fs = require('node:fs');
const [, , file] = process.argv;
const shadow = { innerHTML: '', querySelector(sel) { return sel === '#mower' ? { onchange: null } : null; }, querySelectorAll() { return []; } };
const ctx = vm.createContext({
  HTMLElement: class { attachShadow() { this.shadowRoot = shadow; return shadow; } },
  customElements: { define(name, cls) { ctx.Panel = cls; } },
  Date,
});
vm.runInContext(fs.readFileSync(file, 'utf8'), ctx);
const p = new ctx.Panel();
p._hass = { config: { unit_system: { length: 'km' } }, states: {} };
p._mowers = [{ entry_id: 'e', title: 'Example Mower' }];
p._entryId = 'e';
p._tab = 'now';
p._zoneView = { zones: [{ id: 'z1', name: 'Front', polygon: { type: 'Polygon', coordinates: [[[0, 0], [0.001, 0], [0.001, 0.001], [0, 0.001], [0, 0]]] } }], channels: [], gates: [] };
const run = { run_id: 'r1', plan_id: 'p', zone_id: 'z1', zone_name: 'Front', height_mm: 70, angle_deg: 40, n: 1, of: 2, can_up: false, can_down: true };
p._now = { status: 'waiting', pause_requested: false, current_run: null, current_zone_id: null, next_run: run, up_next: [run],
  waiting_on: [{ check: 'mower_ready', name: 'Mower ready', reason: 'battery_low' }], failure: null,
  checks: [{ id: 'mower_ready', name: 'Mower ready', state: 'failing' }], controls: ['pause'] };
p._prompts = [{ prompt_id: 'pp', title: 'Example Mower: Run failed', body: 'Zone: Front', actions: ['retry'] }];
p._sheet = { run_id: 'r1' };
p._render();
process.stdout.write(shadow.innerHTML);
