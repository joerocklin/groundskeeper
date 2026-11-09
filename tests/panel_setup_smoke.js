// Drives the panel element against a stub DOM and a recording connection
// (tests/test_panel_setup_checklist.py); prints JSON of what the banner's
// buttons did.
const vm = require('node:vm');
const fs = require('node:fs');
const [, , file] = process.argv;
const buttons = {}; // data-act -> stub buttons with a dataset, as the banner rendered them
const nodes = { '#mower': {} }; // selector -> stub element
const shadow = {
  innerHTML: '',
  querySelector(sel) { return nodes[sel] || null; },
  querySelectorAll(sel) {
    const m = sel.match(/^\[data-act="(.+)"\]$/);
    return m ? (buttons[m[1]] || []) : [];
  },
};
const ctx = vm.createContext({
  HTMLElement: class { attachShadow() { this.shadowRoot = shadow; return shadow; } },
  customElements: { define(name, cls) { ctx.Panel = cls; } },
  Date,
});
vm.runInContext(fs.readFileSync(file, 'utf8'), ctx);

const sent = [];
const checklist = {
  complete: false,
  items: [
    { id: 'bearings', label: 'Review Zone bearings', tab: 'zones', done: false },
    { id: 'gates', label: 'Does M pass through any gates?', tab: 'zones', done: false },
    { id: 'notify', label: 'Where should M reach you?', tab: 'settings', done: false },
  ],
};
const p = new ctx.Panel();
p._hass = {
  config: { unit_system: { length: 'km' } },
  states: {},
  connection: {
    sendMessagePromise: async (msg) => {
      sent.push(msg);
      if (msg.step === 'notify') throw { code: 'safe_idle', message: 'Mower is in safe idle' };
      return {};
    },
  },
};
p._mowers = [{ entry_id: 'e', title: 'M' }];
p._entryId = 'e';
p._snapshot = { entry_id: 'e', title: 'M', setup_checklist: checklist };
p._tab = 'history'; // a tab with nothing of its own: the banner is what is under test
for (const loader of ['_loadZones', '_loadSchedule', '_loadChecks', '_loadNotify']) {
  p[loader] = async () => {};
}
const click = (act, match) => {
  const b = buttons[act].find((x) => Object.entries(match).every(([k, v]) => x.dataset[k] === v));
  return b.onclick && b.onclick();
};

(async () => {
  const out = {};
  // Stand-ins for the buttons the banner just rendered.
  buttons['open-tab'] = [{ dataset: { tab: 'settings' } }];
  buttons['setup-step'] = [
    { dataset: { step: 'bearings' } },
    { dataset: { step: 'gates', answer: 'no' } },
    { dataset: { step: 'notify' } },
  ];
  p._render();
  out.banner_on_any_tab = shadow.innerHTML.includes('id="setup-checklist"');

  click('open-tab', { tab: 'settings' });
  out.tab_after_link = p._tab;

  await click('setup-step', { step: 'bearings' });
  await click('setup-step', { step: 'gates' });
  out.sent = sent.map((m) => ({ ...m }));

  await click('setup-step', { step: 'notify' });
  out.error_shown = shadow.innerHTML.includes('Mower is in safe idle');

  // A refusal is about one Mower at one moment: it must not follow the owner around.
  const settle = () => new Promise((r) => setTimeout(r, 0));
  p._openTab('history');
  out.error_after_tab_change = shadow.innerHTML.includes('Mower is in safe idle');
  await click('setup-step', { step: 'notify' });
  // Switching Mower re-subscribes; the new Mower's snapshot arrives and the banner is back.
  p._hass.connection.subscribeMessage = async (cb, msg) => {
    if (msg.type.endsWith('/subscribe')) cb({ entry_id: 'e', title: 'M', setup_checklist: checklist });
    return () => {};
  };
  nodes['#mower'].onchange({ target: { value: 'e' } });
  await settle();
  out.banner_after_mower_switch = shadow.innerHTML.includes('id="setup-checklist"');
  out.error_after_mower_switch = shadow.innerHTML.includes('Mower is in safe idle');
  process.stdout.write(JSON.stringify(out));
})();
