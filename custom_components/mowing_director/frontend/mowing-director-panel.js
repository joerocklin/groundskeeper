/*
 * Mowing Director panel shell (spec §16).
 *
 * Plain HTMLElement + shadow DOM, matching WashData; no framework, no bundler.
 * Lit was considered and not chosen: the panel is a handful of tabs over
 * server-pushed snapshots, so a template-string render keeps the repo
 * buildless and HACS-shippable as-is. The minified sibling is produced by
 * devtools/build_panel.mjs and is optional.
 *
 * HA sets `hass`, `narrow`, `route` and `panel` on this element.
 */
const TABS = [
  ['now', 'Now'],
  ['schedule', 'Schedule & Plans'],
  ['zones', 'Zones & Gates'],
  ['checks', 'Checks'],
  ['history', 'History'],
  ['settings', 'Settings'],
];

const STYLE = `
  :host { display: block; min-height: 100%; background: var(--primary-background-color);
    color: var(--primary-text-color); font-family: var(--paper-font-body1_-_font-family, sans-serif); }
  header { display: flex; align-items: center; gap: 12px; padding: 12px 16px;
    background: var(--app-header-background-color, var(--primary-color));
    color: var(--app-header-text-color, #fff); }
  header h1 { font-size: 20px; margin: 0; flex: 1; font-weight: 400; }
  select { font: inherit; padding: 6px 8px; border-radius: 6px; }
  nav { display: flex; overflow-x: auto; white-space: nowrap;
    border-bottom: 1px solid var(--divider-color); background: var(--card-background-color); }
  nav button { background: none; border: 0; border-bottom: 2px solid transparent; font: inherit;
    padding: 14px 16px; cursor: pointer; color: var(--secondary-text-color); }
  nav button[aria-selected="true"] { color: var(--primary-color); border-color: var(--primary-color); }
  .banner { margin: 16px; padding: 12px 16px; border-radius: 8px;
    background: var(--warning-color, #ffa600); color: #000; }
  main { padding: 16px; display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .card { background: var(--card-background-color); border-radius: 12px; padding: 16px;
    box-shadow: var(--ha-card-box-shadow, none); border: 1px solid var(--divider-color); }
  .empty { padding: 32px 16px; text-align: center; color: var(--secondary-text-color); }
  @media (max-width: 760px) { main { grid-template-columns: 1fr; } }
`;

const esc = (s) =>
  String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

class MowingDirectorPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._hass = null;
    this._mowers = [];
    this._entryId = null;
    this._snapshot = null;
    this._tab = 'now';
    this._unsub = null;
    this._started = false;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._start();
  }
  get hass() { return this._hass; }
  set narrow(v) { this._narrow = v; }
  set route(v) { this._route = v; }
  set panel(v) { this._panel = v; }

  connectedCallback() { this._render(); if (this._hass) this._start(); }
  disconnectedCallback() { this._unsubscribe(); this._started = false; }

  async _start() {
    if (this._started) return;
    this._started = true;
    try {
      const res = await this._hass.connection.sendMessagePromise({ type: 'mowing_director/list_mowers' });
      this._mowers = res.mowers;
      if (!this._mowers.some((m) => m.entry_id === this._entryId)) {
        this._entryId = this._mowers.length ? this._mowers[0].entry_id : null;
      }
    } catch (err) {
      this._error = err.message || String(err);
    }
    await this._subscribe();
    this._render();
  }

  async _subscribe() {
    this._unsubscribe();
    this._snapshot = null;
    if (!this._entryId) return;
    this._unsub = await this._hass.connection.subscribeMessage(
      (snapshot) => { this._snapshot = snapshot; this._render(); },
      { type: 'mowing_director/subscribe', entry_id: this._entryId },
    );
  }

  _unsubscribe() {
    if (this._unsub) { this._unsub(); this._unsub = null; }
  }

  _render() {
    const mowerOptions = this._mowers
      .map((m) => `<option value="${esc(m.entry_id)}"${m.entry_id === this._entryId ? ' selected' : ''}>${esc(m.title)}</option>`)
      .join('');
    const tabs = TABS
      .map(([id, label]) => `<button role="tab" data-tab="${id}" aria-selected="${id === this._tab}">${esc(label)}</button>`)
      .join('');
    const checklist = this._snapshot && this._snapshot.setup_checklist;
    const banner = checklist && !checklist.complete
      ? `<div class="banner" id="setup-checklist">Finish setup: ${checklist.items.length} item(s) left</div>`
      : '';
    const label = (TABS.find(([id]) => id === this._tab) || [])[1];
    let body;
    if (this._error) body = `<div class="empty">${esc(this._error)}</div>`;
    else if (!this._entryId) body = '<div class="empty">No Mowers configured.</div>';
    else body = `<main><section class="card"><h2>${esc(label)}</h2><p>Coming soon.</p></section></main>`;
    this.shadowRoot.innerHTML = `<style>${STYLE}</style>
      <header><h1>Mowing Director</h1><select id="mower" aria-label="Mower">${mowerOptions}</select></header>
      <nav role="tablist">${tabs}</nav>${banner}${body}`;
    this.shadowRoot.querySelector('#mower').onchange = (e) => {
      this._entryId = e.target.value;
      this._subscribe().then(() => this._render());
    };
    this.shadowRoot.querySelectorAll('nav button').forEach((b) => {
      b.onclick = () => { this._tab = b.dataset.tab; this._render(); };
    });
  }
}

customElements.define('mowing-director-panel', MowingDirectorPanel);
