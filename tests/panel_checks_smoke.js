// Drives the panel element against a stub DOM and a recording connection
// (tests/test_panel_checks_fetch.py); prints JSON of what was sent and rendered.
const vm = require('node:vm');
const fs = require('node:fs');
const [, , file] = process.argv;
const nodes = { '#mower': {}, '#cc-entity': {}, '#custom-ids': { innerHTML: '' } }; // selector -> stub element
const shadow = {
  innerHTML: '',
  querySelector(sel) { return nodes[sel] || null; },
  querySelectorAll(sel) { return nodes[sel] ? [nodes[sel]] : []; },
};
const ctx = vm.createContext({
  HTMLElement: class { attachShadow() { this.shadowRoot = shadow; return shadow; } },
  customElements: { define(name, cls) { ctx.Panel = cls; } },
  Date,
});
vm.runInContext(fs.readFileSync(file, 'utf8'), ctx);

const sent = [];
let push = null;
const checksView = { checks: [], anomaly_actions: {}, mower_rain_detection: true };
const p = new ctx.Panel();
p._hass = {
  config: { unit_system: { length: 'km' } },
  states: { 'switch.b': {}, 'weather.home': {}, 'sensor.a': {} },
  connection: {
    sendMessagePromise: async (msg) => { sent.push(msg.type); return msg.type.endsWith('list_checks') ? checksView : {}; },
    subscribeMessage: async (cb, msg) => { if (msg.type.endsWith('/subscribe')) push = cb; return () => {}; },
  },
};
p._mowers = [{ entry_id: 'e', title: 'M' }];
p._entryId = 'e';
const settle = () => new Promise((r) => setTimeout(r, 0));
const count = (t) => sent.filter((s) => s.endsWith(t)).length;
const render = () => {
  p._render();
  p._wireChecks(shadow);
};

(async () => {
  const out = {};
  await p._subscribe();
  p._tab = 'checks';
  await p._loadChecks(); // what the tab button does on entry
  out.enter = count('list_checks');
  for (let i = 0; i < 5; i++) { push({}); await settle(); }
  out.after_pushes = count('list_checks');
  await p._checkCmd('delete_check', { check_id: 'x' });
  out.after_mutation = count('list_checks');
  await p._checkCmd('set_anomaly_action', { detector: 'run_too_long', action: 'off' });
  out.after_anomaly = count('list_checks');

  render();
  out.render_all_options = (shadow.innerHTML.match(/switch\.b/g) || []).length;
  out.render_entity_input = shadow.innerHTML.includes('id="cc-entity"');
  nodes['#cc-entity'].onfocus();
  out.focus_options = nodes['#custom-ids'].innerHTML;
  const first = p._allEntityIds;
  nodes['#cc-entity'].onfocus();
  out.built_once = p._allEntityIds === first;
  render();
  out.rerender_keeps_list = (shadow.innerHTML.match(/switch\.b/g) || []).length;

  const fresh = new ctx.Panel();
  Object.assign(fresh, { _hass: p._hass, _tab: 'checks', _checksView: checksView });
  fresh._customDraft = { ...fresh._customDraft, mode: 'template' };
  fresh._render();
  out.template_has_all_list = fresh.shadowRoot.innerHTML.includes('custom-ids');
  process.stdout.write(JSON.stringify(out));
})();
