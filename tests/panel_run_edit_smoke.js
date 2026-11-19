// Drives the two Run edit surfaces (the Schedule tab's Plan cards and the Now
// tab's Run sheet) against a stub DOM and a recording connection
// (tests/test_panel_run_edit_wiring.py). The stub reads its inputs and buttons
// back out of the markup the panel rendered, so a typed value goes into exactly
// the input the user would see. Prints JSON of what was sent and shown.
const vm = require('node:vm');
const fs = require('node:fs');
const [, , file] = process.argv;

const tagsOf = (html, attr) => [...html.matchAll(new RegExp(`<(?:input|button)[^>]*${attr}[^>]*>`, 'g'))].map((m) => m[0]);
const attrOf = (tag, name) => { const m = new RegExp(`(?:^|\\s)${name}="([^"]*)"`).exec(tag); return m ? m[1] : undefined; };

// A shadow root whose innerHTML is the panel's last render.
function makeShadow() {
  const typed = new Map();
  let handlers = [];
  let html = '';
  const shadow = {
    get innerHTML() { return html; },
    // A render replaces every input: what was typed is gone unless the markup carries it.
  set innerHTML(v) { html = v; handlers = []; typed.clear(); },
    type(sel, value) { typed.set(sel, value); },
    handlers: () => handlers,
    querySelector(sel) {
      if (sel === '#mower') return { onchange: null };
      let tag;
      const byId = /^#([\w-]+)$/.exec(sel);
      const byClass = /^\.([\w-]+)\[data-run="([^"]+)"\]$/.exec(sel);
      if (byId) tag = tagsOf(html, `id="${byId[1]}"`).find((t) => attrOf(t, 'id') === byId[1]);
      if (byClass) tag = tagsOf(html, `data-run="${byClass[2]}"`).find((t) => attrOf(t, 'class') === byClass[1]);
      if (!tag) return null;
      return { get value() { return typed.has(sel) ? typed.get(sel) : attrOf(tag, 'value'); } };
    },
    querySelectorAll(sel) {
      if (sel !== '[data-act]') return [];
      return tagsOf(html, 'data-act').map((tag) => {
        const el = {
          dataset: { act: attrOf(tag, 'data-act'), run: attrOf(tag, 'data-run'), plan: attrOf(tag, 'data-plan') },
          addEventListener(type, fn) { handlers.push({ el, type, fn }); },
        };
        return el;
      });
    },
  };
  return shadow;
}

const settle = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setTimeout(r, 0)); };

function panel(tab, reply) {
  const shadow = makeShadow();
  const ctx = vm.createContext({
    HTMLElement: class { attachShadow() { this.shadowRoot = shadow; return shadow; } },
    customElements: { define(name, cls) { ctx.Panel = cls; } },
    Date,
    window: { confirm: () => true },
  });
  vm.runInContext(fs.readFileSync(file, 'utf8'), ctx);
  const sent = [];
  const p = new ctx.Panel();
  p._hass = {
    config: { unit_system: { length: 'km' } }, states: {},
    connection: { sendMessagePromise: async (msg) => { sent.push(msg); return reply(msg); } },
  };
  p._mowers = [{ entry_id: 'e', title: 'M' }];
  p._entryId = 'e';
  p._tab = tab;
  p._zoneView = { zones: [], channels: [], gates: [] };
  p._prompts = [];
  const click = async (act, run) => {
    const hit = shadow.handlers().find((h) => h.type === 'click' && h.el.dataset.act === act && (run == null || h.el.dataset.run === run));
    await hit.fn({ stopPropagation() {} });
    await settle();
  };
  return { p, shadow, sent, click };
}

const edits = (sent) => sent.filter((m) => m.type === 'groundskeeper/edit_run');
const REJECTION = { code: 'invalid_settings', message: 'spacing 5 cm is outside the device limits (20-60 cm)' };

// -- the Schedule tab's Plan cards --------------------------------------------------

const plannedRun = (id, index) => ({ id, plan_id: 'p', index, of: 3, status: 'pending', height_mm: 70, angle_deg: 10,
  zone_name: 'Front', settings: { spacing_cm: 30, speed_m_s: 0.5 } });
const scheduleView = { schedule: { status: 'idle' }, plans: [{ id: 'p', zone_name: 'Front', status: 'active', start_height_mm: 79, end_height_mm: 61 }],
  runs: [plannedRun('r1', 1), plannedRun('r2', 2), plannedRun('r3', 3)] };

async function schedule(reply, type) {
  const s = panel('schedule', (msg) => (msg.type.endsWith('get_schedule') ? scheduleView : reply(msg)));
  s.p._scheduleView = scheduleView;
  s.p._render();
  for (const [sel, v] of Object.entries(type || {})) s.shadow.type(sel, v);
  await s.click('edit-run', 'r2');
  return s;
}

// -- the Now tab's Run sheet ----------------------------------------------------------

const upNext = { run_id: 'r1', plan_id: 'p', zone_id: 'z1', zone_name: 'Front', height_mm: 70, angle_deg: 40, n: 1, of: 3,
  can_up: false, can_down: false, spacing_cm: 30, speed_m_s: 0.5 };
const nowView = { status: 'waiting', pause_requested: false, current_run: null, current_zone_id: null, next_run: upNext,
  up_next: [upNext], waiting_on: [], failure: null, checks: [], controls: ['pause'], starting_in_s: null };

async function sheet(reply, type) {
  const s = panel('now', reply);
  s.p._now = nowView;
  s.p._sheet = { run_id: 'r1' };
  s.p._render();
  for (const [sel, v] of Object.entries(type || {})) s.shadow.type(sel, v);
  await s.click('save-run');
  return s;
}

(async () => {
  const out = {};
  const ok = () => ({});
  const reject = (msg) => { if (msg.type.endsWith('edit_run')) throw REJECTION; return {}; };

  let s = await schedule(ok, { '.edit-spacing[data-run="r2"]': '22' });
  out.schedule_changed = edits(s.sent);

  s = await schedule(ok);
  out.schedule_untouched = edits(s.sent);

  s = await schedule(reject, { '.edit-spacing[data-run="r2"]': '5' });
  out.schedule_rejected = { sent: edits(s.sent).length, notice: s.p._notice, shown: s.shadow.innerHTML.includes(REJECTION.message),
    reloaded: s.sent.some((m) => m.type.endsWith('get_schedule')),
    spacingAfter: s.shadow.querySelector('.edit-spacing[data-run="r2"]').value };

  s = await schedule(ok, { '.edit-speed[data-run="r2"]': '' });
  out.schedule_blank = { sent: edits(s.sent).length, notice: s.p._notice, shown: s.shadow.innerHTML.includes('Enter a speed above zero.') };

  s = await sheet(ok, { '#run-spacing': '22', '#run-speed': '0.3' });
  out.sheet_changed = { sent: edits(s.sent), closed: s.p._sheet === null };

  s = await sheet(reject, { '#run-spacing': '5' });
  out.sheet_rejected = { sent: edits(s.sent).length, error: s.p._sheet && s.p._sheet.error, shown: s.shadow.innerHTML.includes(REJECTION.message),
    typedKept: s.shadow.querySelector('#run-spacing').value };

  s = await sheet(ok, { '#run-spacing': '' });
  out.sheet_blank = { sent: edits(s.sent).length, error: s.p._sheet && s.p._sheet.error };

  // A pushed re-render while typing keeps what was typed in the new inputs.
  s = panel('now', ok);
  s.p._now = nowView;
  s.p._sheet = { run_id: 'r1' };
  s.p._render();
  s.shadow.type('#run-spacing', '41');
  s.shadow.type('#run-speed', '0.77');
  s.p._render();
  out.rerender = { spacing: s.shadow.querySelector('#run-spacing').value, speed: s.shadow.querySelector('#run-speed').value };

  process.stdout.write(JSON.stringify(out));
})();
