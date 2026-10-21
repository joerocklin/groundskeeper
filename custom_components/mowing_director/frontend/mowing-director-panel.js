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

// -- Map component and Zones & Gates helpers (spec §16.3, §4.5, §5.4) ----------
// Pure string builders; the Now tab and the Gate editor reuse `renderMap`.

const MAP_W = 400;
const MAP_H = 300;
const MAP_PAD = 16;
const STRIPE_GAP = 10; // px between synthetic stripes (illustrative, not to scale)

const num = (x) => Math.round(x * 100) / 100;
const ringOf = (polygon) => {
  if (!polygon || !polygon.coordinates) return null;
  const c = polygon.type === 'MultiPolygon' ? polygon.coordinates[0] : polygon.coordinates;
  return c && c[0] && c[0].length >= 3 ? c[0] : null;
};

// Project every Zone ring into the map box together, keeping north up.
function projectZones(zones) {
  const rings = zones.map((z) => ringOf(z.polygon));
  const pts = rings.flatMap((r) => r || []);
  if (!pts.length) return { rings: rings.map(() => null), at: () => [MAP_W / 2, MAP_H / 2] };
  const lat0 = pts.reduce((a, p) => a + p[1], 0) / pts.length;
  const kx = Math.cos((lat0 * Math.PI) / 180);
  const xs = pts.map((p) => p[0] * kx);
  const ys = pts.map((p) => -p[1]);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const minY = Math.min(...ys), maxY = Math.max(...ys);
  const scale = Math.min(
    (MAP_W - 2 * MAP_PAD) / Math.max(maxX - minX, 1e-9),
    (MAP_H - 2 * MAP_PAD) / Math.max(maxY - minY, 1e-9),
  );
  const at = ([lon, lat]) => [
    MAP_PAD + (lon * kx - minX) * scale + ((MAP_W - 2 * MAP_PAD) - (maxX - minX) * scale) / 2,
    MAP_PAD + (-lat - minY) * scale + ((MAP_H - 2 * MAP_PAD) - (maxY - minY) * scale) / 2,
  ];
  return { rings: rings.map((r) => (r ? r.map(at) : null)), at };
}

const centroid = (pts) => [
  pts.reduce((a, p) => a + p[0], 0) / pts.length,
  pts.reduce((a, p) => a + p[1], 0) / pts.length,
];

// Stripe segments at an absolute compass angle (0 = north, clockwise) across
// the ring's bounding circle, as <line> elements clipped by the zone clipPath.
function stripesSvg(pts, angle, clipId) {
  const [cx, cy] = centroid(pts);
  const r = Math.max(...pts.map((p) => Math.hypot(p[0] - cx, p[1] - cy))) + STRIPE_GAP;
  const a = (angle * Math.PI) / 180;
  const dx = Math.sin(a), dy = -Math.cos(a); // along the stripe
  const nx = -dy, ny = dx; // across stripes
  let out = '';
  for (let o = -r; o <= r; o += STRIPE_GAP) {
    const x = cx + nx * o, y = cy + ny * o;
    out += `<line class="stripe" x1="${num(x - dx * r)}" y1="${num(y - dy * r)}" x2="${num(x + dx * r)}" y2="${num(y + dy * r)}"/>`;
  }
  return `<g class="stripes" clip-path="url(#${clipId})">${out}</g>`;
}

/**
 * Map of Zones and Channels. `opts`: angle (absolute compass degrees; with
 * stripeZoneId draws synthetic stripes in that Zone), selectedZoneId,
 * currentZoneId (the Mower's Zone), highlightZoneId.
 */
function renderMap(zones, channels, opts = {}) {
  const { rings } = projectZones(zones);
  const centre = {};
  zones.forEach((z, i) => { if (rings[i]) centre[z.id] = centroid(rings[i]); });
  const dock = [MAP_PAD, MAP_H - MAP_PAD];
  const end = (ref) => (ref === 'dock' ? dock : centre[ref]);
  const lines = (channels || [])
    .map((c) => [end(c.end_a), end(c.end_b)])
    .filter(([a, b]) => a && b)
    .map(([a, b]) => `<line class="channel" x1="${num(a[0])}" y1="${num(a[1])}" x2="${num(b[0])}" y2="${num(b[1])}"/>`)
    .join('');
  const hasDock = (channels || []).some((c) => c.end_a === 'dock' || c.end_b === 'dock');
  let defs = '', shapes = '';
  zones.forEach((z, i) => {
    const pts = rings[i];
    if (!pts) return;
    const cls = ['zone', z.missing && 'missing', z.hidden && 'hidden',
      z.id === opts.selectedZoneId && 'selected', z.id === opts.currentZoneId && 'current',
      z.id === opts.highlightZoneId && 'highlight'].filter(Boolean).join(' ');
    const d = pts.map((p) => `${num(p[0])},${num(p[1])}`).join(' ');
    defs += `<clipPath id="clip-${esc(z.id)}"><polygon points="${d}"/></clipPath>`;
    shapes += `<polygon class="${cls}" data-zone="${esc(z.id)}" points="${d}"/>`;
    if (opts.angle != null && opts.stripeZoneId === z.id) shapes += stripesSvg(pts, opts.angle, `clip-${esc(z.id)}`);
    const [x, y] = centroid(pts);
    shapes += `<text class="label" x="${num(x)}" y="${num(y)}">${esc(z.name)}</text>`;
  });
  const dockMark = hasDock ? `<circle class="dock" cx="${dock[0]}" cy="${dock[1]}" r="5"/>` : '';
  return `<svg class="map" viewBox="0 0 ${MAP_W} ${MAP_H}" role="img" aria-label="Zones and Channels"><defs>${defs}</defs>${shapes}${lines}${dockMark}</svg>`;
}

// Values are shown in HA units: lengths follow the unit system, times are
// minutes/hours, angles are degrees.
const fmtLength = (m, lengthUnit) =>
  lengthUnit === 'mi' ? `${Math.round(m * 3.28084)} ft` : `${Math.round(m)} m`;

function fmtDuration(s) {
  const min = Math.round(s / 60);
  return min >= 60 ? `${Math.floor(min / 60)} h ${min % 60} min` : `${min} min`;
}

const fmtDeg = (v) => `${num(v)}°`;
const fmtSigned = (v) => `${v > 0 ? '+' : ''}${num(v)}°`;

function bearingHtml(z) {
  const b = z.bearing || {};
  const warns = [];
  if (b.status === 'pending') warns.push('Bearing pending: the Zone outline has not arrived yet.');
  if (b.status === 'unset') warns.push('Bearing unset: the Zone outline could not be used. Set a manual bearing.');
  if (b.ambiguous) warns.push('Bearing ambiguous: the Zone is nearly square, so its long axis is a guess. Check it, or set a manual bearing.');
  if (b.irregular) warns.push('Irregular shape: set a manual bearing, or fix it by splitting the Zone in the Mammotion app. Parts of this Zone get perimeter laps only.');
  const manual = b.source === 'manual';
  const value = b.value == null ? 'not set' : fmtDeg(b.value);
  const derived = b.derived_value == null ? '' : ` (derived ${fmtDeg(b.derived_value)})`;
  return `<section class="bearing"><h3>Bearing</h3>
    <p><strong>${value}</strong> <span class="tag">${manual ? 'manual' : 'derived'}</span>${manual ? derived : ''}</p>
    ${warns.map((w) => `<p class="warn">${esc(w)}</p>`).join('')}
    <p><input type="number" min="0" max="360" step="1" id="bearing-input" aria-label="Manual bearing in degrees">
    <button data-act="set-bearing">Set manual bearing</button>
    ${manual ? '<button data-act="reset-bearing">Reset to derived</button>' : ''}</p></section>`;
}

// Wedge of allowed path angles over stripes at the Zone bearing. The drawing
// is rotated so the bearing points up; angles are clockwise from it.
function wedgeSvg(rng) {
  const R = 60, cx = 70, cy = 70;
  const pt = (deg) => {
    const a = (deg * Math.PI) / 180;
    return [num(cx + R * Math.sin(a)), num(cy - R * Math.cos(a))];
  };
  let stripes = '';
  for (let x = cx - R + 8; x < cx + R; x += 10) stripes += `<line class="stripe" x1="${x}" y1="${cy - R}" x2="${x}" y2="${cy + R}"/>`;
  let shape;
  if (rng === 'full') shape = `<circle class="wedge" cx="${cx}" cy="${cy}" r="${R}"/>`;
  else {
    const mirror = (deg) => pt(deg + 180);
    const [x1, y1] = pt(rng.min), [x2, y2] = pt(rng.max);
    const [x3, y3] = mirror(rng.min), [x4, y4] = mirror(rng.max);
    const large = rng.max - rng.min > 180 ? 1 : 0;
    // Stripe directions are lines, so the wedge is drawn on both sides.
    shape = `<path class="wedge" d="M${cx},${cy} L${x1},${y1} A${R},${R} 0 ${large} 1 ${x2},${y2} Z"/>`
      + `<path class="wedge" d="M${cx},${cy} L${x3},${y3} A${R},${R} 0 ${large} 1 ${x4},${y4} Z"/>`;
  }
  return `<svg class="wedge-map" viewBox="0 0 140 140" role="img" aria-label="Allowed angle range"><circle class="dial" cx="${cx}" cy="${cy}" r="${R}"/>${stripes}${shape}</svg>`;
}

function rangeHtml(z, lengthUnit) {
  const r = z.angle_range || {};
  const rng = r.angle_range;
  const manual = r.source === 'manual';
  const reset = manual ? '<button data-act="reset-range">Reset to derived</button>' : '';
  if (rng == null || rng === 'unset') {
    return `<section class="range"><h3>Allowed angle range</h3>
      <p class="warn">Range unset until the bearing is known. Plans use provisional angles meanwhile.</p>${reset}</section>`;
  }
  const est = z.estimates || {};
  const row = (label, e) => (e
    ? `<tr><td>${label}</td><td>${fmtDuration(e.time_s)}</td><td>${fmtLength(e.stripe_length, lengthUnit)} stripes</td></tr>` : '');
  const label = rng === 'full' ? 'full (any angle)' : `${fmtSigned(rng.min)} to ${fmtSigned(rng.max)}`;
  const inputs = `<p><input type="number" min="-90" max="0" step="1" id="range-min" aria-label="Range minimum in degrees">
    <input type="number" min="0" max="90" step="1" id="range-max" aria-label="Range maximum in degrees">
    <button data-act="set-range">Set manual range</button>
    <button data-act="set-range-full">Allow any angle</button> ${reset}</p>`;
  return `<section class="range"><h3>Allowed angle range</h3>${wedgeSvg(rng)}
    <p><strong>${label}</strong> <span class="tag">${manual ? 'manual' : 'derived'}</span></p>
    <table><tr><th>Angle</th><th>Estimated time</th><th></th></tr>
    ${row('Base angle (0°)', est.base)}${row(rng === 'full' ? '' : `Edge ${fmtSigned(rng.min)}`, est.min_edge)}${row(rng === 'full' ? '' : `Edge ${fmtSigned(rng.max)}`, est.max_edge)}</table>
    ${inputs}</section>`;
}

function zoneListHtml(zones, selectedId) {
  const rows = zones.map((z) => `<li data-zone="${esc(z.id)}" class="${z.id === selectedId ? 'selected' : ''}">
      <span class="name">${esc(z.name)}</span>
      ${z.missing ? '<span class="tag missing">missing</span>' : ''}
      <label><input type="checkbox" data-act="toggle-hidden" data-zone="${esc(z.id)}"${z.hidden ? ' checked' : ''}> hidden</label></li>`).join('');
  return `<ul class="zones">${rows}</ul><p><button data-act="refresh-zones">Refresh Zones</button></p>`;
}

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
