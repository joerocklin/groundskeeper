/*
 * Groundskeeper panel shell (spec §16).
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
  svg.map { width: 100%; height: auto; background: var(--secondary-background-color); border-radius: 8px; }
  svg .zone { fill: var(--primary-color); fill-opacity: .25; stroke: var(--primary-color); stroke-width: 1.5; cursor: pointer; }
  svg .zone.selected { fill-opacity: .5; stroke-width: 3; }
  svg .zone.current { stroke: var(--success-color, #43a047); stroke-width: 3; }
  svg .zone.hidden { stroke-dasharray: 4 3; fill-opacity: .08; }
  svg .zone.missing { stroke: var(--error-color, #db4437); fill: none; stroke-dasharray: 2 3; }
  svg .channel, svg .channel-hit { fill: none; }
  svg .channel { stroke: var(--secondary-text-color); stroke-width: 2; stroke-dasharray: 6 3; }
  svg .channel.picked { stroke: var(--primary-color); stroke-width: 4; stroke-dasharray: none; }
  svg .channel-hit { stroke: transparent; stroke-width: 18; cursor: pointer; }
  svg .gate rect { stroke: var(--primary-text-color); stroke-width: 1.5; }
  svg .gate.closed rect { fill: var(--error-color, #db4437); }
  svg .gate.open rect { fill: var(--success-color, #43a047); }
  svg .stripe { stroke: var(--primary-text-color); stroke-opacity: .35; stroke-width: 1; }
  svg .label { font-size: 11px; text-anchor: middle; fill: var(--primary-text-color); pointer-events: none; }
  svg .dock { fill: var(--primary-text-color); }
  svg .dial { fill: none; stroke: var(--divider-color); }
  svg .wedge { fill: var(--primary-color); fill-opacity: .35; stroke: var(--primary-color); }
  svg.wedge-map { width: 140px; height: 140px; float: right; }
  ul.zones { list-style: none; margin: 0; padding: 0; }
  ul.zones li { display: flex; gap: 8px; align-items: center; padding: 8px; border-radius: 6px; cursor: pointer; }
  ul.zones li.selected { background: var(--secondary-background-color); }
  ul.zones .name { flex: 1; }
  .tag { font-size: 12px; padding: 1px 6px; border-radius: 8px; background: var(--secondary-background-color); }
  .tag.missing { background: var(--error-color, #db4437); color: #fff; }
  .warn { color: var(--warning-color, #b26a00); }
  input[type=number] { width: 5em; font: inherit; }
  /* -- Now tab (spec §16.2) -- */
  svg .zone.highlight { fill-opacity: .55; stroke-width: 4; stroke: var(--accent-color, #ff9800); }
  .now-status { display: flex; flex-direction: column; gap: 8px; }
  .now-status .state { font-size: 18px; font-weight: 500; }
  .now-status dl { display: grid; grid-template-columns: auto 1fr; gap: 2px 12px; margin: 0; }
  .now-status dt { color: var(--secondary-text-color); }
  .now-status dd { margin: 0; }
  .btns { display: flex; flex-wrap: wrap; gap: 8px; }
  .btns button, .run-row button, .prompt-card button, .sheet button { font: inherit; padding: 8px 14px; min-height: 40px;
    border-radius: 8px; border: 1px solid var(--divider-color); background: var(--card-background-color);
    color: var(--primary-text-color); cursor: pointer; }
  .btns button.danger, .prompt-card button[data-action=abort] { color: var(--error-color, #db4437); }
  button:disabled { opacity: .35; cursor: default; }
  ul.runs { list-style: none; margin: 0; padding: 0; }
  ul.runs li.run { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 6px 4px; border-radius: 6px; cursor: pointer; }
  ul.runs li.run.selected { background: var(--secondary-background-color); }
  ul.runs .what { flex: 1; min-width: 10em; }
  ul.runs .what small { display: block; color: var(--secondary-text-color); }
  .pills { display: flex; flex-wrap: wrap; gap: 6px; }
  .pill { font-size: 13px; padding: 3px 10px; border-radius: 12px; background: var(--secondary-background-color); }
  .pill.ok { background: var(--success-color, #43a047); color: #fff; }
  .pill.failing { background: var(--error-color, #db4437); color: #fff; }
  .prompt-card { border-left: 4px solid var(--warning-color, #ffa600); padding-left: 12px; margin: 8px 0; }
  .prompt-card h3 { margin: 0 0 4px; font-size: 16px; }
  .prompt-card p { margin: 2px 0; }
  .sheet { position: fixed; inset: auto 0 0 0; max-width: 480px; margin: 0 auto; z-index: 5;
    background: var(--card-background-color); border-radius: 16px 16px 0 0; padding: 16px;
    box-shadow: 0 -4px 24px rgba(0,0,0,.35); }
  .sheet label { display: block; margin: 8px 0; }
  table.schedule, table.preview { width: 100%; border-collapse: collapse; }
  table.schedule td, table.schedule th, table.preview td, table.preview th { padding: 4px 6px; text-align: left; }
  ul.plan-runs { list-style: none; margin: 0; padding: 0; }
  ul.plan-runs li { padding: 6px 0; }
  ul.targets, ul.categories { list-style: none; margin: 0 0 8px; padding: 0; }
  ul.targets li, ul.categories li { padding: 6px 0; }
  .hint { color: var(--secondary-text-color); }
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

const DOCK = 'dock'; // a Channel end that is the dock, not a Zone (mirrors Channel.DOCK)
const dist = (p, q) => Math.hypot(p[0] - q[0], p[1] - q[1]);
const isDockChannel = (c) => c.end_a === DOCK || c.end_b === DOCK;

// A Channel's path as [lon, lat] points, or null when it has no usable geometry.
const pathOf = (c) => {
  const g = c && c.path;
  return g && g.type === 'LineString' && g.coordinates && g.coordinates.length >= 2 ? g.coordinates : null;
};

// Project every Zone ring into the map box together, keeping north up.
// `extra` are further [lon, lat] points (Channel paths) the fit must include.
function projectZones(zones, extra = []) {
  const rings = zones.map((z) => ringOf(z.polygon));
  const pts = [...rings.flatMap((r) => r || []), ...extra];
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

// Segment lengths of a polyline and their sum.
function arcLengths(pts) {
  const lens = pts.slice(1).map((p, i) => dist(p, pts[i]));
  return { lens, total: lens.reduce((a, l) => a + l, 0) };
}

// A Gate with no saved position sits half-way along its Channel.
const GATE_MID = 0.5;

// Point a fraction `f` (0..1) of the way along a polyline, by arc length.
function polylinePoint(pts, f) {
  const { lens, total } = arcLengths(pts);
  if (!total) return pts[0];
  let want = Math.min(Math.max(f, 0), 1) * total;
  let i = 0;
  while (i < lens.length - 1 && want > lens[i]) want -= lens[i++];
  const t = lens[i] ? Math.min(want / lens[i], 1) : 0;
  return [pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t, pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t];
}

// Fraction (0..1) of the way along a polyline, by arc length, of the point
// on it nearest `p`: where a tap or drag snaps to.
function nearestFraction(pts, p) {
  const { lens, total } = arcLengths(pts);
  if (!total) return 0;
  let best = Infinity, bestAt = 0, before = 0;
  lens.forEach((len, i) => {
    const a = pts[i], b = pts[i + 1];
    const t = len ? Math.min(Math.max(((p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])) / (len * len), 0), 1) : 0;
    const d = dist(p, [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]);
    if (d < best) { best = d; bestAt = before + t * len; }
    before += len;
  });
  return bestAt / total;
}

// Source point order is unreliable, so run the polyline from `a` to `b`: the
// order with the shorter total end-to-anchor distance wins, a tie keeps it.
function orientPath(pts, a, b) {
  const first = pts[0], last = pts[pts.length - 1];
  return dist(first, b) + dist(last, a) < dist(first, a) + dist(last, b) ? [...pts].reverse() : pts;
}

// A dock Channel's path has no dock anchor to compare with, so the end nearer
// its Zone is the Zone end; a tie keeps the given order.
function orientDockPath(pts, zoneAnchor, dockFirst) {
  const near = dist(pts[0], zoneAnchor), far = dist(pts[pts.length - 1], zoneAnchor);
  if (near === far) return pts;
  return (near < far) === dockFirst ? [...pts].reverse() : pts;
}

// Each Channel's path, projected and run end_a -> end_b, keyed by Channel id.
// Channels without a usable path, or whose Zone end can't be placed, are left out.
function channelPaths(channels, centre, at) {
  const paths = {};
  (channels || []).forEach((c) => {
    const lonLat = pathOf(c);
    if (!lonLat) return;
    const pts = lonLat.map(at);
    if (isDockChannel(c)) {
      const anchor = centre[c.end_a === DOCK ? c.end_b : c.end_a];
      if (anchor) paths[c.id] = orientDockPath(pts, anchor, c.end_a === DOCK);
    } else if (centre[c.end_a] && centre[c.end_b]) {
      paths[c.id] = orientPath(pts, centre[c.end_a], centre[c.end_b]);
    }
  });
  return paths;
}

// Where the first dock Channel (by native_ref) that has a path reaches the dock.
function dockPoint(channels, paths) {
  const c = (channels || [])
    .filter((ch) => paths[ch.id] && isDockChannel(ch))
    .sort((x, y) => String(x.native_ref).localeCompare(String(y.native_ref)))[0];
  if (!c) return null;
  const route = paths[c.id];
  return c.end_a === DOCK ? route[0] : route[route.length - 1];
}

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
 * Map of Zones, Channels and Gates. `opts`: gates (drawn on their Channels
 * with believed open/closed state), pickedChannelIds (enables Channel
 * picking and highlights those), angle (absolute compass degrees; with
 * stripeZoneId draws synthetic stripes in that Zone), selectedZoneId,
 * currentZoneId (the Mower's Zone), highlightZoneId.
 */
function renderMap(zones, channels, opts = {}) {
  // Only paths that can be drawn (every Zone end has an outline) widen the fit.
  const placed = new Set(zones.filter((z) => ringOf(z.polygon)).map((z) => z.id));
  const drawable = (channels || []).filter((c) => [c.end_a, c.end_b].every((r) => r === DOCK || placed.has(r)));
  const { rings, at } = projectZones(zones, drawable.flatMap((c) => pathOf(c) || []));
  const centre = {};
  zones.forEach((z, i) => { if (rings[i]) centre[z.id] = centroid(rings[i]); });
  const paths = channelPaths(channels, centre, at);
  // The dock sits where a dock path ends; the corner pin is only the fallback.
  const dock = dockPoint(channels, paths) || [MAP_PAD, MAP_H - MAP_PAD];
  const end = (ref) => (ref === DOCK ? dock : centre[ref]);
  const picked = new Set(opts.pickedChannelIds || []);
  const routes = (channels || [])
    .map((c) => ({ c, route: paths[c.id] || [end(c.end_a), end(c.end_b)] }))
    .filter(({ route }) => route.every(Boolean));
  const pt = (p) => `${num(p[0])},${num(p[1])}`;
  const shape = (cls, attr, route, hasPath) => (hasPath
    ? `<polyline class="${cls}" ${attr} points="${route.map(pt).join(' ')}"/>`
    : `<line class="${cls}" ${attr} x1="${num(route[0][0])}" y1="${num(route[0][1])}" x2="${num(route[1][0])}" y2="${num(route[1][1])}"/>`);
  // Channels carry their id so the Gate editor can pick them; the wide
  // transparent twin underneath is the tap target (only while picking).
  const lines = routes
    .map(({ c, route }) => {
      const hasPath = Boolean(paths[c.id]);
      const hit = opts.pickedChannelIds
        ? shape('channel-hit', `data-pick="${esc(c.id)}"`, route, hasPath) : '';
      return hit + shape(`channel${picked.has(c.id) ? ' picked' : ''}`,
        `data-channel="${esc(c.id)}"`, route, hasPath);
    })
    .join('');
  // Gates sit on their Channels, drawn with the believed state (spec §4.7), at
  // their saved fraction along the drawn route by arc length (half-way if unset).
  const routeOf = {};
  routes.forEach(({ c, route }) => { routeOf[c.id] = route; });
  // The Gate being edited (opts.editGate) replaces its saved twin and gets a
  // draggable handle per Channel plus a tap target on each Channel's line.
  const editing = opts.editGate;
  const mark = (g, id, edit) => {
    const f = (g.positions || {})[id];
    const p = polylinePoint(routeOf[id], f == null ? GATE_MID : f);
    const handle = edit ? ` data-gate-handle="${esc(g.id)}" data-channel-id="${esc(id)}"` : '';
    return `<g class="gate ${g.open ? 'open' : 'closed'}${edit ? ' editing' : ''}" data-gate="${esc(g.id)}"${handle}><title>${esc(g.name)}: ${g.open ? 'open' : 'closed'}</title>`
      + `<rect x="${num(p[0] - 6)}" y="${num(p[1] - 6)}" width="12" height="12" rx="2"/></g>`;
  };
  const marksOf = (g, edit) => (g.channels || []).filter((id) => routeOf[id]).map((id) => mark(g, id, edit));
  const places = editing
    ? (editing.channels || []).filter((id) => routeOf[id]).map((id) => {
      const route = routeOf[id];
      const attr = `data-place="${esc(id)}" data-route="${route.map(pt).join(' ')}"`;
      return shape('channel-hit', attr, route, Boolean(paths[id]));
    }).join('') : '';
  const gateMarks = places
    + (opts.gates || []).filter((g) => !editing || g.id !== editing.id).flatMap((g) => marksOf(g, false)).join('')
    + (editing ? marksOf(editing, true).join('') : '');
  const hasDock = (channels || []).some(isDockChannel);
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
  const dockMark = hasDock ? `<circle class="dock" cx="${num(dock[0])}" cy="${num(dock[1])}" r="5"/>` : '';
  return `<svg class="map" viewBox="0 0 ${MAP_W} ${MAP_H}" role="img" aria-label="Zones and Channels"><defs>${defs}</defs>${shapes}${lines}${gateMarks}${dockMark}</svg>`;
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


// -- Gate editor helpers (spec §4.7, §16.3) -----------------------------------

const endName = (ref, zones) =>
  (ref === 'dock' ? 'Dock' : ((zones || []).find((z) => z.id === ref) || {}).name || ref);
const channelName = (c, zones) => `${endName(c.end_a, zones)} – ${endName(c.end_b, zones)}`;

const actionsText = (a) => (a && a.length ? JSON.stringify(a) : '');

// Editable copy of a stored Gate (or a blank one). Actions are edited as JSON.
function gateDraft(g) {
  if (!g) return { id: null, name: '', channels: [], positions: {}, state_entity_id: '', open_action: '', close_action: '', notify: null };
  return {
    id: g.id, name: g.name, channels: [...(g.channels || [])], positions: { ...(g.positions || {}) },
    state_entity_id: g.state_entity_id || g.state_entity || '',
    open_action: actionsText(g.open_action), close_action: actionsText(g.close_action),
    notify: g.notify,
  };
}

function parseActions(text, label) {
  const t = (text || '').trim();
  if (!t) return { value: null };
  try {
    const v = JSON.parse(t);
    const list = Array.isArray(v) ? v : [v];
    if (!list.every((a) => a && typeof a === 'object' && !Array.isArray(a))) throw new Error('not actions');
    return { value: list };
  } catch (e) {
    return { error: `The ${label} must be a JSON action or list of actions.` };
  }
}

// Websocket fields for create_gate / update_gate, or { error } to show.
// Blank optional fields are sent as null so an edit can clear them; a null
// notify keeps the "on when there is no open action" default.
function gatePayload(d) {
  const name = (d.name || '').trim();
  if (!name) return { error: 'A Gate needs a name.' };
  if (!(d.channels || []).length) return { error: 'Pick at least one Channel on the map.' };
  const open = parseActions(d.open_action, 'open action');
  if (open.error) return { error: open.error };
  const close = parseActions(d.close_action, 'close action');
  if (close.error) return { error: close.error };
  return {
    payload: {
      name, channels: [...d.channels],
      positions: Object.fromEntries(Object.entries(d.positions || {}).filter(([id]) => d.channels.includes(id))),
      state_entity: (d.state_entity_id || '').trim() || null,
      open_action: open.value, close_action: close.value,
      notify: d.notify == null ? null : d.notify,
    },
  };
}

const toggleChannel = (picked, id) => (picked.includes(id) ? picked.filter((c) => c !== id) : [...picked, id]);

function gateEditorHtml(d, view, entityIds) {
  const zones = view.zones || [];
  const byId = Object.fromEntries((view.channels || []).map((c) => [c.id, c]));
  const picked = d.channels.map((id) => `<li>${esc(byId[id] ? channelName(byId[id], zones) : id)}</li>`).join('');
  const notify = d.notify == null ? !(d.open_action || '').trim() : d.notify;
  const options = (entityIds || []).map((e) => `<option value="${esc(e)}"></option>`).join('');
  return `<section class="gate-editor"><h3>${d.id ? 'Edit Gate' : 'New Gate'}</h3>
    <p><label>Name <input id="gate-name" value="${esc(d.name)}" placeholder="Fence gate"></label></p>
    <p>Channels the Gate sits on. Pick the Channels on the map.</p>
    <ul class="picked-channels">${picked || '<li class="warn">None picked yet.</li>'}</ul>
    <p><label>Open-state entity (optional) <input id="gate-state-entity" list="gate-entities" value="${esc(d.state_entity_id)}" placeholder="binary_sensor.fence_gate"></label>
      <datalist id="gate-entities">${options}</datalist></p>
    <p><label>Open action (optional, JSON) <textarea id="gate-open-action" rows="3">${esc(d.open_action)}</textarea></label></p>
    <p><label>Close action (optional, JSON) <textarea id="gate-close-action" rows="3">${esc(d.close_action)}</textarea></label></p>
    <p><label><input type="checkbox" id="gate-notify"${notify ? ' checked' : ''}> Notify me before crossings</label></p>
    ${d.error ? `<p class="warn" id="gate-error">${esc(d.error)}</p>` : ''}
    <p><button data-act="save-gate">Save Gate</button> <button data-act="cancel-gate">Cancel</button>
    ${d.id ? '<button data-act="delete-gate">Delete Gate</button>' : ''}</p></section>`;
}

// Gates with believed state, the Zones behind each for confirmation, and the
// Zones no Channel route reaches.
function gateListHtml(view, selectedId) {
  const gates = view.gates || [];
  const zones = view.zones || [];
  const names = (ids) => ids.map((id) => esc((zones.find((z) => z.id === id) || {}).name || id)).join(', ');
  const rows = gates.map((g) => `<li data-gate="${esc(g.id)}" class="${g.id === selectedId ? 'selected' : ''}">
      <span class="name">${esc(g.name)}</span>
      <span class="tag">${g.open ? 'open' : 'closed'}</span>
      <button data-act="edit-gate" data-gate="${esc(g.id)}">Edit</button>
      <div class="behind">Zones behind this Gate: ${g.zone_ids && g.zone_ids.length ? names(g.zone_ids) : 'none'}.
        Check that this matches your fence.</div></li>`).join('');
  const unrouted = (view.unrouted_zones || []).length
    ? `<p class="warn">No Channel route to the dock, treated as not gated: ${names(view.unrouted_zones.map((z) => z.id))}.
        Zones with no route to the dock cannot be reached through any Gate.</p>` : '';
  const th = view.thresholds;
  const thLine = (label, t) => `<li>${label}: ${esc(t.level)} % (${t.source === 'learned' ? `learned from ${esc(t.samples)} samples`
    : `${esc(t.source)}, ${esc(t.samples)} of 3 samples so far`})</li>`;
  const thresholds = th ? `<ul class="thresholds"><li class="hint">When the Mower recharges, Gate warnings come 10 minutes ahead.</li>
    ${thLine('Return level', th.return)}${thLine('Resume level', th.resume)}</ul>` : '';
  return `<ul class="zones gates">${rows || '<li class="empty">No Gates declared.</li>'}</ul>${unrouted}${thresholds}
    <p><button data-act="new-gate">Add Gate</button></p>`;
}

// -- Now tab helpers (spec §16.2) ----------------------------------------------
// Pure string builders over the `get_now` / `subscribe_now` view.


const REASONS = {
  battery_low: 'Battery too low to start',
  below_threshold: 'Battery below the Check threshold',
  outside_work_hours: 'Outside work hours',
  unavailable: 'Mower unavailable',
  unknown_state: 'Mower state unknown',
  job_active: 'The Mower already has a job running',
  new_error: 'The Mower reported an error',
  device_non_work_hours: 'Outside the Mower\'s own work hours',
  bearing_unset: 'Zone bearing not set',
  zone_missing: 'The Zone is missing',
  start_failed: 'The Mower did not start',
  ended_by_user: 'Ended by you',
};
const reasonText = (r) => REASONS[r] || String(r || 'unknown').replace(/_/g, ' ');

const NOTES = {
  height_increases: 'This height is higher than the Run before it in the Plan.',
  step_above_max_cut: 'This step cuts more than the Max cut.',
  angle_outside_range: 'This angle is outside the Zone\'s allowed range.',
  move_before_earlier_run: 'A Run cannot go ahead of an earlier Run of its own Plan.',
  move_after_later_run: 'A Run cannot go behind a later Run of its own Plan.',
  move_not_pending: 'Only a Run that has not started can be moved.',
  run_not_pending: 'Only a Run that has not started can be changed.',
  no_failure_to_answer: 'There is no failed Run to answer any more.',
  prompt_not_pending: 'That question has already been answered.',
};
const errorText = (code, message) => NOTES[code] || message;

// A rejected websocket call is a plain `{code, message}` object, and `message` may be empty.
const errMessage = (err) => {
  const text = (v) => (typeof v === 'string' && v ? v : '');
  if (err && typeof err === 'object') return text(err.message) || text(err.code) || 'Request failed';
  return text(err) || 'Request failed';
};

const STATUS_LABEL = { idle: 'Idle', waiting: 'Waiting', running: 'Running', paused: 'Paused' };
const CTL_LABEL = {
  pause: 'Pause after Run', stop_now: 'Stop now', done_today: 'Done for today',
  resume: 'Resume', retry: 'Retry', skip_plan: 'Skip Plan', abort: 'Abort',
  resume_run: 'Resume Run', end_run: 'End Run',
};
const ACTION_LABEL = {
  retry: 'Retry', skip_plan: 'Skip Plan', abort: 'Abort', resume: 'Resume', end_run: 'End Run',
  no: 'No', yes: 'Yes', yes_done_today: 'Yes, done for today', opened: 'I opened it',
  closed: 'Closed', dismiss: 'Dismiss',
};
const actionLabel = (a) => ACTION_LABEL[a] || String(a).replace(/_/g, ' ');

// The panel confirms Abort (spec §16.3); other actions go straight through.
const confirmText = (action) => (action === 'abort'
  ? 'Abort? This cancels the whole Schedule: every pending Run is removed.' : null);

// Map options: stripes at the next Run's absolute angle (the active Run's while
// one is mowing); tapping a Run highlights its Zone.
function nowMapOpts(view, highlightRunId) {
  const head = view.current_run || view.next_run;
  const picked = (view.up_next || []).find((r) => r.run_id === highlightRunId);
  return {
    stripeZoneId: head ? head.zone_id : undefined,
    angle: head ? head.angle_deg : undefined,
    currentZoneId: view.current_zone_id || null,
    highlightZoneId: picked ? picked.zone_id : null,
  };
}

const ctlButton = (ctl, extra = '') =>
  `<button data-ctl="${ctl}"${extra}${ctl === 'abort' || ctl === 'stop_now' ? ' class="danger"' : ''}>${CTL_LABEL[ctl]}</button>`;

function statusCardHtml(view, lengthUnit) {
  const c = view.current_run;
  const has = (ctl) => (view.controls || []).includes(ctl);
  let body = '';
  if (c) {
    body += `<dl><dt>Zone</dt><dd>${esc(c.zone_name)}</dd>
      <dt>Height</dt><dd>${fmtHeight(c.height_mm, lengthUnit)}</dd>
      <dt>Angle</dt><dd>${fmtDeg(c.angle_deg)}</dd>
      <dt>Progress</dt><dd>Run ${esc(c.n)} of ${esc(c.of)}</dd>
      <dt>Battery</dt><dd>${c.battery == null ? 'unknown' : `${esc(c.battery)} %`}</dd>
      <dt>Height check</dt><dd>${esc(String(c.height_check || 'pending').replace(/_/g, ' '))}</dd></dl>`;
    if (c.status === 'on_hold') {
      body += `<p class="warn">On hold: ${esc(reasonText(c.on_hold_reason))}.</p>`;
    }
  }
  if (view.status === 'waiting') {
    const w = view.waiting_on || [];
    body += w.length
      ? `<p>Waiting for:</p><ul class="waiting">${w.map((x) => `<li><strong>${esc(x.name)}</strong>: ${esc(reasonText(x.reason))}</li>`).join('')}</ul>`
      : '<p>Waiting for the Mower to settle before starting.</p>';
  }
  if (view.status === 'paused' && view.failure) {
    const f = view.failure;
    body += `<p class="warn">Run ${esc(f.n)} of ${esc(f.of)} (${esc(f.zone_name)}) failed: ${esc(reasonText(f.reason))}.</p>`;
  } else if (view.status === 'paused' && view.paused_reason) {
    body += `<p>Paused: ${esc(reasonText(view.paused_reason))}.</p>`;
  }
  if (view.pause_requested && view.status !== 'paused') body += '<p class="hint">Pausing after this Run.</p>';
  const order = ['pause', 'stop_now', 'resume_run', 'end_run', 'retry', 'skip_plan', 'resume', 'abort', 'done_today'];
  const btns = order.filter(has).map((ctl) => ctlButton(ctl)).join('');
  return `<div class="now-status"><div class="state">${esc(STATUS_LABEL[view.status] || view.status)}</div>${body}
    ${btns ? `<div class="btns">${btns}</div>` : ''}</div>`;
}

// Position (among the pending Runs) a ↑/↓ press moves the Run to.
function movePosition(rows, runId, dir) {
  const i = rows.findIndex((r) => r.run_id === runId);
  return dir === 'up' ? i - 1 : i + 1;
}

function upNextHtml(view, lengthUnit, selectedRunId) {
  const rows = (view.up_next || []).map((r) => `<li data-run="${esc(r.run_id)}" class="run${r.run_id === selectedRunId ? ' selected' : ''}">
      <span class="what"><strong>${esc(r.zone_name)}</strong>
        <small>Run ${esc(r.n)} of ${esc(r.of)} · ${fmtHeight(r.height_mm, lengthUnit)} · ${fmtDeg(r.angle_deg)}</small></span>
      <span class="run-row"><button data-act="move-up" data-run="${esc(r.run_id)}" aria-label="Move earlier"${r.can_up ? '' : ' disabled'}>↑</button>
      <button data-act="move-down" data-run="${esc(r.run_id)}" aria-label="Move later"${r.can_down ? '' : ' disabled'}>↓</button>
      <button data-act="run-menu" data-run="${esc(r.run_id)}" aria-label="Edit or delete">⋯</button></span></li>`).join('');
  return `<ul class="runs">${rows || '<li class="empty">No Runs coming up.</li>'}</ul>
    <p><button data-act="add-plan">+ Plan</button></p>`;
}

// Bottom sheet for one pending Run. `state` carries { warnings, error } from the
// last save; values are shown and entered in HA units.
function runSheetHtml(run, lengthUnit, state) {
  const h = lengthUnit === 'mi' ? Math.round((run.height_mm / 25.4) * 100) / 100 : num(run.height_mm);
  const st = state || {};
  return `<div class="sheet" role="dialog" aria-label="Edit Run" data-run="${esc(run.run_id)}">
    <h3>${esc(run.zone_name)}, Run ${esc(run.n)} of ${esc(run.of)}</h3>
    <label>Height (${lengthUnit === 'mi' ? 'in' : 'mm'}) <input id="run-height" type="number" step="any" value="${st.draft ? esc(st.draft.height) : h}"></label>
    <label>Angle (°) <input id="run-angle" type="number" step="any" value="${st.draft ? esc(st.draft.angle) : num(run.angle_deg)}"></label>
    ${(st.warnings || []).map((w) => `<p class="warn">${esc(NOTES[w] || w)}</p>`).join('')}
    ${st.error ? `<p class="warn" id="run-error">${esc(st.error)}</p>` : ''}
    <div class="btns"><button data-act="save-run">Save</button>
    <button data-act="delete-run" class="danger">Delete Run</button>
    <button data-act="close-sheet">Close</button></div></div>`;
}

// edit_run fields from the sheet's raw inputs, or { error }.
function runEditPayload(d, lengthUnit) {
  const height = d.height === '' || d.height == null ? NaN : Number(d.height);
  const angle = d.angle === '' || d.angle == null ? NaN : Number(d.angle);
  if (!Number.isFinite(height) || height <= 0) return { error: 'Enter a height above zero.' };
  if (!Number.isFinite(angle)) return { error: 'Enter an angle in degrees.' };
  const mm = lengthUnit === 'mi' ? height * 25.4 : height;
  return { payload: { height_mm: Math.round(mm * 10) / 10, angle_deg: angle } };
}

function checkPillsHtml(checks) {
  if (!checks || !checks.length) return '<p class="hint">No Checks enabled.</p>';
  return `<div class="pills">${checks.map((c) => `<span class="pill ${esc(c.state)}" title="${esc(c.state)}">${esc(c.name)}</span>`).join('')}</div>`;
}

function promptCardsHtml(prompts) {
  return (prompts || []).map((p) => `<div class="prompt-card" data-prompt-card="${esc(p.prompt_id)}">
      <h3>${esc(p.title)}</h3>
      ${String(p.body || '').split('\n').filter(Boolean).map((l) => `<p>${esc(l)}</p>`).join('')}
      <div class="btns">${(p.actions || []).map((a) => `<button data-prompt="${esc(p.prompt_id)}" data-action="${esc(a)}">${esc(actionLabel(a))}</button>`).join('')}</div></div>`).join('');
}


// -- Schedule & Plans helpers (spec §16.3, §5.5, §5.7) ------------------------
// Heights are stored in mm; shown and typed in HA units (inches when imperial).

const fmtHeight = (mm, lengthUnit) =>
  (lengthUnit === 'mi' ? `${num(mm / 25.4)} in` : `${num(mm)} mm`);
const heightToMm = (v, lengthUnit) => (lengthUnit === 'mi' ? num(v * 25.4) : v);
const heightValue = (mm, lengthUnit) => (lengthUnit === 'mi' ? num(mm / 25.4) : num(mm));

const EDIT_WARNINGS = {
  height_increases: 'This makes the Run taller than the one before it, so the Plan no longer steps down.',
  step_above_max_cut: 'This step is more than the Plan\'s max cut.',
  angle_outside_range: 'This angle is outside the Zone\'s allowed angle range.',
};
const editWarningsText = (keys) => (keys || []).map((k) => EDIT_WARNINGS[k] || k);

// Position among the pending Runs for a one-step move, or null when the move
// is not allowed: a Run never passes an earlier or later Run of its own Plan
// (same rule as Up next, spec §5.7).
function moveTarget(pending, runId, dir) {
  const i = pending.findIndex((r) => r.id === runId);
  const j = i + dir;
  if (i < 0 || j < 0 || j >= pending.length) return null;
  return pending[j].plan_id === pending[i].plan_id ? null : j;
}

const pendingOf = (view) => (view.runs || []).filter((r) => r.status === 'pending');

function scheduleTableHtml(view, lengthUnit) {
  const pending = pendingOf(view);
  const btn = (act, r, label, dir) => {
    const ok = r.status === 'pending' && moveTarget(pending, r.id, dir) != null;
    return `<button data-act="${act}" data-run="${esc(r.id)}"${ok ? '' : ' disabled'} aria-label="${label}">${dir < 0 ? '&#9650;' : '&#9660;'}</button>`;
  };
  const rows = (view.runs || []).map((r, i) => `<tr data-run="${esc(r.id)}">
      <td>${i + 1}</td><td>${esc(r.zone_name)}</td><td>${esc(r.index)} of ${esc(r.of)}</td>
      <td>${fmtHeight(r.height_mm, lengthUnit)}</td><td>${fmtDeg(r.angle_deg)}</td>
      <td><span class="tag">${esc(r.status)}</span></td>
      <td>${r.status === 'pending' ? btn('move-up', r, 'Move up', -1) + btn('move-down', r, 'Move down', 1) : ''}</td></tr>`).join('');
  if (!rows) return '<p class="empty">The Schedule is empty.</p>';
  return `<table class="schedule"><tr><th>#</th><th>Zone</th><th>Run</th><th>Height</th><th>Angle</th><th>Status</th><th></th></tr>${rows}</table>`;
}

function planCardsHtml(view, lengthUnit) {
  const cards = (view.plans || []).map((p) => {
    const runs = (view.runs || []).filter((r) => r.plan_id === p.id);
    const started = runs.some((r) => r.status !== 'pending');
    const dis = started ? ' disabled' : '';
    const rows = runs.map((r) => {
      const pend = r.status === 'pending';
      const edit = pend ? `<label>Height <input type="number" step="any" class="edit-height" data-run="${esc(r.id)}" value="${heightValue(r.height_mm, lengthUnit)}"></label>
          <label>Angle <input type="number" step="any" class="edit-angle" data-run="${esc(r.id)}" value="${num(r.angle_deg)}"></label>
          <button data-act="edit-run" data-run="${esc(r.id)}">Save</button>
          <button data-act="delete-run" data-run="${esc(r.id)}">Delete</button>`
        : `${fmtHeight(r.height_mm, lengthUnit)} at ${fmtDeg(r.angle_deg)} <span class="tag">${esc(r.status)}</span>`;
      return `<li>Run ${esc(r.index)} of ${esc(r.of)}: ${edit}</li>`;
    }).join('');
    const warns = (p.angle_warnings || []).map((w) => `<p class="warn">${esc(w)}</p>`).join('');
    const note = p.note === 'angles_redrawn_for_range'
      ? '<p class="hint">Angles were redrawn when the Zone\'s allowed angle range arrived.</p>' : '';
    return `<section class="card plan" data-plan="${esc(p.id)}"><h3>${esc(p.zone_name)}: ${fmtHeight(p.start_height_mm, lengthUnit)} to ${fmtHeight(p.end_height_mm, lengthUnit)}
      <span class="tag">${esc(p.status)}</span></h3>${note}${warns}<ul class="plan-runs">${rows}</ul>
      <p><button data-act="reroll" data-plan="${esc(p.id)}"${dis}>Re-roll angles</button>
      <button data-act="rederive" data-plan="${esc(p.id)}"${dis}>Re-derive from defaults</button>
      <button data-act="remove-plan" data-plan="${esc(p.id)}">Remove Plan</button></p></section>`;
  }).join('');
  return cards || '<section class="card"><p class="empty">No Plans yet.</p></section>';
}

// Websocket fields for preview_plan / add_plan from the New Plan form, or { error }.
function newPlanPayload(d, lengthUnit) {
  if (!d.zone) return { error: 'Pick a Zone.' };
  const start = Number(d.start), end = Number(d.end);
  if (d.start === '' || d.end === '' || d.start == null || d.end == null || Number.isNaN(start) || Number.isNaN(end)) {
    return { error: 'Enter a start and an end height.' };
  }
  const payload = {
    zone: d.zone, start_height_mm: heightToMm(start, lengthUnit), end_height_mm: heightToMm(end, lengthUnit),
  };
  if (d.max_cut !== '' && d.max_cut != null) payload.max_cut_pct = Number(d.max_cut);
  if (d.base_angle !== '' && d.base_angle != null) payload.base_angle = Number(d.base_angle);
  if (d.finishing === 'on') payload.finishing = true;
  if (d.finishing === 'off') payload.finishing = false;
  return { payload };
}

function previewHtml(pv, lengthUnit) {
  const rows = pv.runs.map((r) => `<tr><td>${esc(r.n)} of ${esc(r.m)}</td><td>${fmtHeight(r.height_mm, lengthUnit)}</td>
      <td>${fmtDeg(r.angle_deg)}${r.provisional ? ' <span class="tag">provisional</span>' : ''}</td>
      <td>${r.estimated_time_s == null ? 'unknown' : fmtDuration(r.estimated_time_s)}</td></tr>`).join('');
  const prov = pv.provisional
    ? '<p class="warn" id="provisional-warning">Angles are provisional: the Zone\'s allowed angle range is not known yet. They are redrawn when it arrives.</p>' : '';
  const warns = (pv.warnings || []).filter((w) => !/provisional/i.test(w)).map((w) => `<p class="warn">${esc(w)}</p>`).join('');
  return `<table class="preview"><tr><th>Run</th><th>Height</th><th>Angle</th><th>Estimated time</th></tr>${rows}</table>${prov}${warns}`;
}

function newPlanFormHtml(d, zones, preview, lengthUnit) {
  const opts = zones.filter((z) => !z.missing && !z.hidden)
    .map((z) => `<option value="${esc(z.id)}"${z.id === d.zone ? ' selected' : ''}>${esc(z.name)}</option>`).join('');
  const hu = lengthUnit === 'mi' ? 'in' : 'mm';
  const fin = (v, l) => `<option value="${v}"${d.finishing === v ? ' selected' : ''}>${l}</option>`;
  return `<section class="card"><h2>New Plan</h2>
    <p><label>Zone <select id="np-zone"><option value="">Choose a Zone</option>${opts}</select></label></p>
    <p><label>Start height (${hu}) <input type="number" step="any" id="np-start" value="${esc(d.start)}"></label>
    <label>End height (${hu}) <input type="number" step="any" id="np-end" value="${esc(d.end)}"></label></p>
    <p><label>Max cut (%, optional) <input type="number" step="any" id="np-max-cut" value="${esc(d.max_cut)}"></label>
    <label>Base angle (optional) <input type="number" step="any" id="np-base-angle" value="${esc(d.base_angle)}"></label>
    <label>Finishing <select id="np-finishing">${fin('default', 'Default')}${fin('on', 'On')}${fin('off', 'Off')}</select></label></p>
    ${d.error ? `<p class="warn" id="np-error">${esc(d.error)}</p>` : ''}
    <p><button data-act="preview-plan">Preview</button> <button data-act="add-plan">Add Plan</button></p>
    ${preview ? previewHtml(preview, lengthUnit) : ''}</section>`;
}

// -- Checks tab (spec §16.3, §9, §11.4) ----------------------------------------

const WEEKDAYS = [['mon', 'Mon'], ['tue', 'Tue'], ['wed', 'Wed'], ['thu', 'Thu'], ['fri', 'Fri'], ['sat', 'Sat'], ['sun', 'Sun']];
const DETECTORS = [['charge_not_rising', 'Charge not rising while docked'], ['run_too_long', 'Run far longer than the Zone\'s history']];
const OPERATORS = [['is', 'is'], ['is_not', 'is not'], ['above', 'above'], ['below', 'below']];
const ALWAYS_ON_CHECKS = ['mower_ready', 'zone_ready'];

const hoursText = (s) => (s == null ? '' : String(num(s / 3600)));
const hoursToS = (t) => {
  const v = String(t == null ? '' : t).trim();
  if (v === '') return { value: null };
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? { value: Math.round(n * 3600) } : { error: 'Timeouts are a positive number of hours.' };
};
const entityList = (v) => (Array.isArray(v) ? v : String(v || '').split(',')).map((s) => s.trim()).filter(Boolean);

const optionsHtml = (pairs, current) =>
  pairs.map(([v, l]) => `<option value="${esc(v)}"${v === current ? ' selected' : ''}>${esc(l)}</option>`).join('');

const timeoutPolicyHtml = (c) => `
  <label>Timeout (hours, blank for none) <input type="number" min="0" step="any" data-f="timeout_h" value="${esc(hoursText(c.timeout_s))}"></label>
  <label>When it times out <select data-f="policy">${optionsHtml([['notify', 'Notify and keep waiting'], ['pause', 'Pause the Schedule']], c.policy)}</select></label>`;

function checkFieldsHtml(c, view) {
  const cfg = c.config || {};
  if (c.type === 'battery') {
    return `<label>Minimum battery (%) <input type="number" min="1" max="100" data-f="threshold_pct" value="${esc(cfg.threshold_pct)}"></label>`;
  }
  if (c.type === 'work_hours') {
    const rows = WEEKDAYS.map(([d, l]) => {
      const w = (cfg.days || {})[d] || ['08:00', '00:00'];
      return `<tr data-day="${d}"><td>${l}</td><td><input type="time" data-f="start" value="${esc(w[0])}"></td><td><input type="time" data-f="end" value="${esc(w[1])}"></td></tr>`;
    }).join('');
    return `<table class="hours"><tr><th></th><th>Start</th><th>End (00:00 = midnight)</th></tr>${rows}</table>`;
  }
  if (c.type === 'wet_grass') {
    const mower = view.mower_rain_detection
      ? ''
      : ' <span class="warn" id="mower-rain-off">off on the Mower</span>';
    return `<p><label><input type="checkbox" data-f="mower_rain"${cfg.mower_rain ? ' checked' : ''}> Mower rain flag</label>${mower}</p>
      <label>Weather entities (comma separated) <input type="text" list="weather-ids" data-f="weather" value="${esc((cfg.weather || []).join(', '))}"></label>
      <label>Rain sensors (comma separated) <input type="text" list="sensor-ids" data-f="sensors" value="${esc((cfg.sensors || []).join(', '))}"></label>
      <label>Dry-out (hours) <input type="number" min="0" step="any" data-f="dry_out_h" value="${esc(hoursText(cfg.dry_out_s))}"></label>
      <label><input type="checkbox" data-f="morning"${cfg.morning ? ' checked' : ''}> Morning dry-out</label>
      <label>Sunrise plus (hours) <input type="number" min="0" step="any" data-f="morning_offset_h" value="${esc(hoursText(cfg.morning_offset_s))}"></label>
      <label>Rain probability threshold (%) <input type="number" min="0" max="100" data-f="precip_pct" value="${esc(cfg.precip_pct)}"></label>`;
  }
  if (c.type === 'custom') {
    const cond = cfg.mode === 'template'
      ? `<code>${esc(cfg.template)}</code>`
      : `<code>${esc(cfg.entity)} ${esc(cfg.operator)} ${esc(cfg.value)}</code>`;
    return `<p>${cond}</p>`;
  }
  return '';
}

function checkCardHtml(c, view) {
  if (ALWAYS_ON_CHECKS.includes(c.type)) {
    return `<section class="card" data-check="${esc(c.id)}"><h3>${esc(c.name)}</h3><p>Always on.</p></section>`;
  }
  const del = c.type === 'custom' ? '<button data-act="delete-check">Delete</button>' : '';
  return `<section class="card" data-check="${esc(c.id)}"><h3>${esc(c.name)}</h3>
    <label><input type="checkbox" data-f="enabled"${c.enabled ? ' checked' : ''}> Enabled</label>
    ${checkFieldsHtml(c, view)}${timeoutPolicyHtml(c)}
    <p><button data-act="save-check">Save</button> ${del}</p></section>`;
}

const entityOptionsHtml = (ids) => (ids || []).map((e) => `<option value="${esc(e)}">`).join('');

const blankCustomDraft = () => ({ name: '', mode: 'entity', entity: '', operator: 'is', value: '', template: '', timeout_h: '', policy: 'notify' });

function customCheckFormHtml(d, allIds) {
  const entity = d.mode !== 'template';
  return `<section class="card" id="custom-check-form"><h3>New Custom Check</h3>
    <label>Name <input type="text" id="cc-name" value="${esc(d.name)}"></label>
    <label>Kind <select id="cc-mode">${optionsHtml([['entity', 'Entity condition'], ['template', 'Template']], d.mode)}</select></label>
    ${entity
      ? `<label>Entity <input type="text" id="cc-entity" list="custom-ids" value="${esc(d.entity)}"></label>
         <datalist id="custom-ids">${entityOptionsHtml(allIds)}</datalist>
         <label>Passes when it <select id="cc-operator">${optionsHtml(OPERATORS, d.operator)}</select></label>
         <label>Value <input type="text" id="cc-value" value="${esc(d.value)}"></label>`
      : `<label>Template (passes when it renders true) <textarea id="cc-template" rows="3">${esc(d.template)}</textarea></label>`}
    <label>Timeout (hours, blank for none) <input type="number" min="0" step="any" id="cc-timeout" value="${esc(d.timeout_h)}"></label>
    <label>When it times out <select id="cc-policy">${optionsHtml([['notify', 'Notify and keep waiting'], ['pause', 'Pause the Schedule']], d.policy)}</select></label>
    ${d.error ? `<p class="warn" id="cc-error">${esc(d.error)}</p>` : ''}
    <p><button data-act="add-custom-check">Add Check</button></p></section>`;
}

// `allIds` is the every-entity candidate list for the custom Check's Entity
// input; null until that input is first focused (see `_wireChecks`).
function checksTabHtml(view, entityIds, draft, allIds) {
  const ids = (id, re) => `<datalist id="${id}">${entityOptionsHtml(entityIds.filter((e) => re.test(e)))}</datalist>`;
  const anomalies = DETECTORS.map(([d, l]) => `<tr><td>${esc(l)}</td><td><select data-detector="${d}">${
    optionsHtml([['off', 'Off'], ['notify', 'Notify'], ['pause', 'Pause after the Run']], (view.anomaly_actions || {})[d] || 'notify')}</select></td></tr>`).join('');
  return `${view.checks.map((c) => checkCardHtml(c, view)).join('')}
    ${customCheckFormHtml(draft || blankCustomDraft(), allIds)}
    <section class="card"><h3>Anomaly detectors</h3><table>${anomalies}</table></section>
    ${ids('weather-ids', /^weather\./)}${ids('sensor-ids', /^binary_sensor\./)}`;
}

// `update_check` changes from a card's form values, or an error.
function checkChanges(rec, f) {
  const t = hoursToS(f.timeout_h);
  if (t.error) return { error: t.error };
  const changes = { enabled: !!f.enabled, timeout_s: t.value, policy: f.policy };
  if (rec.type === 'battery') {
    const pct = Number(f.threshold_pct);
    if (String(f.threshold_pct).trim() === '' || !(pct >= 1 && pct <= 100)) return { error: 'Battery threshold is 1 to 100 %.' };
    changes.config = { threshold_pct: pct };
  } else if (rec.type === 'work_hours') {
    const days = {};
    for (const [d] of WEEKDAYS) {
      const w = (f.days || {})[d];
      if (!w || !w.start || !w.end) return { error: 'Every weekday needs a start and an end.' };
      days[d] = [w.start, w.end];
    }
    changes.config = { days };
  } else if (rec.type === 'wet_grass') {
    const dry = Number(f.dry_out_h), off = Number(f.morning_offset_h), pct = Number(f.precip_pct);
    if (!(dry >= 0) || !(off >= 0)) return { error: 'Dry-out times are zero or more hours.' };
    if (!(pct >= 0 && pct <= 100)) return { error: 'Rain probability is 0 to 100 %.' };
    changes.config = {
      mower_rain: !!f.mower_rain, weather: entityList(f.weather), sensors: entityList(f.sensors),
      dry_out_s: Math.round(dry * 3600), morning: !!f.morning,
      morning_offset_s: Math.round(off * 3600), precip_pct: pct,
    };
  }
  return { changes };
}

// `create_check` payload from the New Custom Check form, or an error.
function customCheckPayload(d) {
  const name = (d.name || '').trim();
  if (!name) return { error: 'A Check needs a name.' };
  const t = hoursToS(d.timeout_h);
  if (t.error) return { error: t.error };
  let config;
  if (d.mode === 'template') {
    if (!(d.template || '').trim()) return { error: 'A template is required.' };
    config = { mode: 'template', template: d.template };
  } else {
    if (!(d.entity || '').trim()) return { error: 'Pick an entity.' };
    if (String(d.value == null ? '' : d.value).trim() === '') return { error: 'A value is required.' };
    config = { mode: 'entity', entity: d.entity.trim(), operator: d.operator, value: String(d.value).trim() };
  }
  return { payload: { name, config, timeout_s: t.value, policy: d.policy } };
}

// -- Settings tab: notify targets and categories (spec §16.3, §12.3, §4.2) ----------
// Section builders for the Settings tab. Later tickets fill #settings-tunables and
// #settings-storage; this ticket owns #settings-notifications.

const NOTIFY_CATEGORIES = [
  ['run_failed', 'Run failed (Retry / Skip Plan / Abort)', true],
  ['run_on_hold', 'Run on hold (Resume / End Run)', true],
  ['check_waits', 'Check waits (timeout, Mower ready, template error, start refused)', false],
  ['is_raining', '"Is it raining?" (off: no question, unconfirmed counts as rain)', false],
  ['run_started', 'Run started', false],
  ['run_done', 'Run done', false],
  ['schedule_completed', 'Schedule completed', false],
];

// Legacy `notify.mobile_app_*` services that exist, as stored service names.
function notifyTargetOptions(services) {
  const notify = (services && services.notify) || {};
  return Object.keys(notify).filter((n) => n.startsWith('mobile_app_')).sort();
}

function notifyTargetsPayload(selected) {
  return { targets: [...selected] };
}

function notifySettingsHtml(settings, available) {
  const stored = settings.targets || [];
  const rows = available.map((n) => `<li><label><input type="checkbox" data-target="${esc(n)}"${stored.includes(n) ? ' checked' : ''}> ${esc(n)}</label></li>`);
  for (const n of stored.filter((t) => !available.includes(t))) {
    rows.push(`<li><label><input type="checkbox" data-target="${esc(n)}" checked> ${esc(n)}</label> <span class="tag missing">no longer exists</span></li>`);
  }
  const phones = rows.length
    ? `<ul class="targets">${rows.join('')}</ul>`
    : '<p class="hint">No phones found. No phone, panel only: Prompts show here and failures raise a persistent notification.</p>';
  const cats = NOTIFY_CATEGORIES.map(([id, label, locked]) => {
    const on = locked || (settings.categories || {})[id];
    return `<li><label><input type="checkbox" data-category="${id}"${on ? ' checked' : ''}${locked ? ' disabled' : ''}> ${esc(label)}</label>${locked ? ' <span class="tag">always on</span>' : ''}</li>`;
  }).join('');
  return `<h2>Notifications</h2>
    <h3>Where should the Mower reach you?</h3>${phones}
    <h3>Categories</h3><ul class="categories">${cats}</ul>
    <p class="hint">Gate prompts follow each Gate's "Notify me before crossings" setting (Zones &amp; Gates). Anomaly notifications follow each detector's action (off, notify or pause).</p>`;
}

function settingsTabHtml(notifyHtml) {
  return `<section class="card" id="settings-notifications">${notifyHtml}</section>
    <section class="card" id="settings-tunables"></section>
    <section class="card" id="settings-storage"></section>`;
}

class GroundskeeperPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._hass = null;
    this._mowers = [];
    this._entryId = null;
    this._snapshot = null;
    this._tab = 'now';
    this._zoneView = { zones: [], channels: [] };
    this._zoneId = null;
    this._gateDraft = null; // Gate being created or edited, with its picked Channels
    this._scheduleView = { schedule: {}, plans: [], runs: [] };
    this._planDraft = { zone: '', start: '', end: '', max_cut: '', base_angle: '', finishing: 'default' };
    this._planPreview = null;
    this._checksView = null;
    this._customDraft = blankCustomDraft();
    this._allEntityIds = null; // every entity id, built on first focus of the custom Check Entity input
    this._unsub = null;
    this._started = false;
    // Now tab state (spec §16.2)
    this._now = null; // latest subscribe_now view
    this._prompts = []; // latest subscribe_prompts list
    this._unsubNow = null;
    this._unsubPrompts = null;
    this._runId = null; // tapped Run, highlighted on the map
    this._sheet = null; // { run_id, warnings, error } while the edit sheet is open
    this._nowZonesAt = 0;
    this._notify = null; // Settings tab: { targets, categories } from get_notify_settings
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
      const res = await this._hass.connection.sendMessagePromise({ type: 'groundskeeper/list_mowers' });
      this._mowers = res.mowers;
      if (!this._mowers.some((m) => m.entry_id === this._entryId)) {
        this._entryId = this._mowers.length ? this._mowers[0].entry_id : null;
      }
    } catch (err) {
      this._error = errMessage(err);
    }
    await this._subscribe();
    this._render();
  }

  async _subscribe() {
    this._unsubscribe();
    this._snapshot = null;
    this._notify = null;
    if (!this._entryId) return;
    this._unsub = await this._hass.connection.subscribeMessage(
      (snapshot) => { this._snapshot = snapshot; this._loadZones(); this._loadSchedule(); this._render(); },
      { type: 'groundskeeper/subscribe', entry_id: this._entryId },
    );
    await this._subscribeNow();
  }

  async _loadZones() {
    if (!this._entryId || (this._tab !== 'zones' && this._tab !== 'now' && this._tab !== 'schedule')) return;
    try {
      this._zoneView = await this._hass.connection.sendMessagePromise({
        type: 'groundskeeper/get_zones', entry_id: this._entryId });
      this._render();
    } catch (err) {
      this._error = errMessage(err);
    }
  }

  // One zone command; the backend pushes a fresh snapshot, which reloads Zones.
  async _zoneCmd(type, extra = {}) {
    try {
      await this._hass.connection.sendMessagePromise({
        type: `groundskeeper/${type}`, entry_id: this._entryId, ...extra });
    } catch (err) {
      this._error = errMessage(err);
    }
    await this._loadZones();
  }

  async _loadSchedule() {
    if (!this._entryId || this._tab !== 'schedule') return;
    try {
      this._scheduleView = await this._hass.connection.sendMessagePromise({
        type: 'groundskeeper/get_schedule', entry_id: this._entryId });
      this._render();
    } catch (err) {
      this._error = errMessage(err);
    }
  }

  async _loadChecks() {
    if (!this._entryId || this._tab !== 'checks') return;
    try {
      this._checksView = await this._hass.connection.sendMessagePromise({
        type: 'groundskeeper/list_checks', entry_id: this._entryId });
      this._render();
    } catch (err) {
      this._error = errMessage(err);
    }
  }

  async _checkCmd(type, extra) {
    this._checksNotice = null;
    try {
      await this._hass.connection.sendMessagePromise({
        type: `groundskeeper/${type}`, entry_id: this._entryId, ...extra });
      return true;
    } catch (err) {
      this._checksNotice = errMessage(err);
      return false;
    } finally {
      await this._loadChecks();
      this._render();
    }
  }

  _readCustomDraft(root) {
    const v = (id) => { const el = root.querySelector(id); return el ? el.value : undefined; };
    if (!root.querySelector('#cc-name')) return;
    const d = this._customDraft;
    Object.assign(d, { name: v('#cc-name'), mode: v('#cc-mode'), timeout_h: v('#cc-timeout'), policy: v('#cc-policy') });
    if (d.mode === 'template') d.template = v('#cc-template') || '';
    else Object.assign(d, { entity: v('#cc-entity'), operator: v('#cc-operator'), value: v('#cc-value') });
  }

  _checksTab() {
    if (!this._checksView) return '<section class="card"><p>Loading Checks.</p></section>';
    const ids = Object.keys((this._hass && this._hass.states) || {}).sort();
    const notice = this._checksNotice ? `<p class="warn" id="checks-notice">${esc(this._checksNotice)}</p>` : '';
    return notice + checksTabHtml(this._checksView, ids, this._customDraft, this._allEntityIds);
  }

  _readCheckCard(card) {
    const f = (name) => card.querySelector(`[data-f="${name}"]`);
    const val = (name) => (f(name) ? f(name).value : undefined);
    const form = { enabled: f('enabled') && f('enabled').checked, timeout_h: val('timeout_h'), policy: val('policy') };
    for (const k of ['threshold_pct', 'weather', 'sensors', 'dry_out_h', 'morning_offset_h', 'precip_pct']) form[k] = val(k);
    for (const k of ['mower_rain', 'morning']) form[k] = f(k) ? f(k).checked : undefined;
    const days = {};
    card.querySelectorAll('[data-day]').forEach((row) => {
      days[row.dataset.day] = {
        start: row.querySelector('[data-f="start"]').value, end: row.querySelector('[data-f="end"]').value };
    });
    if (Object.keys(days).length) form.days = days;
    return form;
  }

  _wireChecks(root) {
    root.querySelectorAll('#cc-entity').forEach((el) => {
      el.onfocus = () => {
        if (!this._allEntityIds) {
          this._allEntityIds = Object.keys((this._hass && this._hass.states) || {}).sort();
          const list = root.querySelector('#custom-ids');
          if (list) list.innerHTML = entityOptionsHtml(this._allEntityIds);
        }
      };
    });
    root.querySelectorAll('#cc-mode').forEach((el) => {
      el.onchange = () => { this._readCustomDraft(root); this._render(); };
    });
    root.querySelectorAll('[data-detector]').forEach((el) => {
      el.onchange = () => this._checkCmd('set_anomaly_action', { detector: el.dataset.detector, action: el.value });
    });
    root.querySelectorAll('[data-act]').forEach((el) => {
      el.onclick = async () => {
        const card = el.closest('[data-check]');
        const rec = card && this._checksView.checks.find((c) => c.id === card.dataset.check);
        if (el.dataset.act === 'save-check') {
          const { changes, error } = checkChanges(rec, this._readCheckCard(card));
          if (error) { this._checksNotice = error; this._render(); return; }
          this._checkCmd('update_check', { check_id: rec.id, changes });
        } else if (el.dataset.act === 'delete-check') {
          this._checkCmd('delete_check', { check_id: rec.id });
        } else if (el.dataset.act === 'add-custom-check') {
          this._readCustomDraft(root);
          const { payload, error } = customCheckPayload(this._customDraft);
          this._customDraft.error = error || null;
          if (error) { this._render(); return; }
          // The backend validates a template on save and answers with its error.
          if (await this._checkCmd('create_check', payload)) this._customDraft = blankCustomDraft();
          else { this._customDraft.error = this._checksNotice; this._checksNotice = null; this._render(); }
        }
      };
    });
  }

  // One Schedule command. Errors and edit warnings show in a notice; the
  // backend pushes a fresh snapshot, which reloads the Schedule.
  async _scheduleCmd(type, extra = {}) {
    let result = null;
    this._notice = null;
    try {
      result = await this._hass.connection.sendMessagePromise({
        type: `groundskeeper/${type}`, entry_id: this._entryId, ...extra });
      const warnings = editWarningsText(result && result.warnings);
      if (warnings.length) this._notice = warnings.join(' ');
    } catch (err) {
      this._notice = errMessage(err);
    }
    await this._loadSchedule();
    return result;
  }

  _readPlanDraft(root) {
    const el = (id) => root.querySelector(id);
    if (!el('#np-zone')) return;
    Object.assign(this._planDraft, {
      zone: el('#np-zone').value, start: el('#np-start').value, end: el('#np-end').value,
      max_cut: el('#np-max-cut').value, base_angle: el('#np-base-angle').value,
      finishing: el('#np-finishing').value,
    });
  }

  async _planCmd(kind) {
    const d = this._planDraft;
    const { payload, error } = newPlanPayload(d, this._lengthUnit());
    d.error = error || null;
    if (error) { this._render(); return; }
    try {
      if (kind === 'preview') {
        this._planPreview = await this._hass.connection.sendMessagePromise({
          type: 'groundskeeper/preview_plan', entry_id: this._entryId, ...payload });
      } else {
        await this._hass.connection.sendMessagePromise({
          type: 'groundskeeper/add_plan', entry_id: this._entryId, ...payload });
        this._planPreview = null;
      }
    } catch (err) {
      d.error = errMessage(err);
    }
    await this._loadSchedule();
    this._render();
  }

  _scheduleTab() {
    const unit = this._lengthUnit();
    const view = this._scheduleView;
    const notice = this._notice ? `<p class="warn" id="schedule-notice">${esc(this._notice)}</p>` : '';
    return `<section class="card"><h2>Schedule</h2>${notice}${scheduleTableHtml(view, unit)}</section>
      ${newPlanFormHtml(this._planDraft, this._zoneView.zones || [], this._planPreview, unit)}
      ${planCardsHtml(view, unit)}`;
  }

  _wireSchedule(root) {
    const unit = this._lengthUnit();
    root.querySelectorAll('#np-zone,#np-start,#np-end,#np-max-cut,#np-base-angle,#np-finishing').forEach((el) => {
      el.addEventListener('change', () => { this._readPlanDraft(root); this._planPreview = null; });
    });
    root.querySelectorAll('[data-act]').forEach((el) => {
      el.addEventListener('click', () => {
        const run_id = el.dataset.run, plan_id = el.dataset.plan;
        switch (el.dataset.act) {
          case 'move-up': case 'move-down': {
            const to = moveTarget(pendingOf(this._scheduleView), run_id, el.dataset.act === 'move-up' ? -1 : 1);
            if (to != null) this._scheduleCmd('move_run', { run_id, position: to });
            break;
          }
          case 'edit-run': {
            const q = (c) => root.querySelector(`.${c}[data-run="${run_id}"]`);
            const height = Number(q('edit-height').value), angle = Number(q('edit-angle').value);
            this._scheduleCmd('edit_run', { run_id, height_mm: heightToMm(height, unit), angle_deg: angle });
            break;
          }
          case 'delete-run':
            if (window.confirm('Delete this Run? The Plan will have one step fewer and its other Runs keep their heights.')) {
              this._scheduleCmd('delete_run', { run_id });
            }
            break;
          case 'reroll': this._scheduleCmd('reroll_angles', { plan_id }); break;
          case 'rederive':
            if (window.confirm('Re-derive this Plan from the current defaults? Edits you made to its Runs are replaced.')) {
              this._scheduleCmd('rederive_plan', { plan_id });
            }
            break;
          case 'remove-plan':
            if (window.confirm('Remove this Plan and its pending Runs?')) this._scheduleCmd('remove_plan', { plan_id });
            break;
          case 'preview-plan': this._readPlanDraft(root); this._planCmd('preview'); break;
          case 'add-plan': this._readPlanDraft(root); this._planCmd('add'); break;
        }
      });
    });
  }

  _zonesTab() {
    const { zones, channels } = this._zoneView;
    const gates = this._zoneView.gates || [];
    const draft = this._gateDraft;
    const unit = (this._hass.config && this._hass.config.unit_system && this._hass.config.unit_system.length) || 'km';
    const sel = zones.find((z) => z.id === this._zoneId) || null;
    const bearing = sel && sel.bearing && sel.bearing.value;
    const detail = sel
      ? `<section class="card"><h2>${esc(sel.name)}</h2>
          ${bearingHtml(sel)}${rangeHtml(sel, unit)}</section>`
      : '';
    const entityIds = this._hass && this._hass.states
      ? Object.keys(this._hass.states).filter((e) => /^(binary_sensor|cover|lock)\./.test(e)).sort() : [];
    const gateCard = `<section class="card"><h2>Gates</h2>
        ${draft ? gateEditorHtml(draft, this._zoneView, entityIds) : gateListHtml(this._zoneView, null)}</section>`;
    return `<section class="card"><h2>Zones &amp; Channels</h2>
        ${renderMap(zones, channels, {
          selectedZoneId: sel && sel.id, stripeZoneId: sel && sel.id, angle: bearing,
          gates, pickedChannelIds: draft ? draft.channels : undefined,
          editGate: draft ? { id: draft.id || 'new-gate', name: draft.name || 'New Gate', channels: draft.channels,
            positions: draft.positions, open: Boolean((gates.find((g) => g.id === draft.id) || {}).open) } : undefined })}
        ${draft ? '<p>Tap Channels on the map to put the Gate on them. Drag a Gate marker, or tap its Channel, to set where it sits.</p>' : ''}
        ${zoneListHtml(zones, sel && sel.id)}</section>${detail}${gateCard}`;
  }

  // Copy the editor's inputs into the draft before anything re-renders.
  _readGateDraft(root) {
    const d = this._gateDraft;
    const el = (id) => root.querySelector(id);
    if (!d || !el('#gate-name')) return;
    d.name = el('#gate-name').value;
    d.state_entity_id = el('#gate-state-entity').value;
    d.open_action = el('#gate-open-action').value;
    d.close_action = el('#gate-close-action').value;
    d.notify = el('#gate-notify').checked;
  }

  async _saveGate(root) {
    this._readGateDraft(root);
    const d = this._gateDraft;
    const { payload, error } = gatePayload(d);
    if (error) { d.error = error; this._render(); return; }
    try {
      await this._hass.connection.sendMessagePromise(d.id
        ? { type: 'groundskeeper/update_gate', entry_id: this._entryId, gate_id: d.id, changes: payload }
        : { type: 'groundskeeper/create_gate', entry_id: this._entryId, ...payload });
    } catch (err) {
      d.error = errMessage(err);
      this._render();
      return;
    }
    this._gateDraft = null;
    await this._loadZones();
    this._render();
  }

  _wireZones(root) {
    const num = (id) => {
      const el = root.querySelector(id);
      return el && el.value !== '' ? Number(el.value) : null;
    };
    root.querySelectorAll('[data-zone]').forEach((el) => {
      el.addEventListener('click', (e) => {
        if (e.target.dataset.act) return;
        this._zoneId = el.dataset.zone;
        this._render();
      });
    });
    // Placement: the pointer's SVG coordinates snap to the Channel's line.
    const place = (e, cid) => {
      const svg = root.querySelector('svg.map');
      const line = root.querySelector(`[data-place="${cid}"]`);
      const m = svg && svg.getScreenCTM && svg.getScreenCTM();
      if (!line || !m) return null;
      const q = new DOMPoint(e.clientX, e.clientY).matrixTransform(m.inverse());
      const route = line.dataset.route.split(' ').map((s) => s.split(',').map(Number));
      return { route, f: nearestFraction(route, [q.x, q.y]) };
    };
    // Save the snapped fraction for one Channel into the draft and redraw.
    const setPosition = (e, cid) => {
      const hit = this._gateDraft && place(e, cid);
      if (!hit) return;
      this._readGateDraft(root);
      this._gateDraft.positions[cid] = hit.f;
      this._render();
    };
    root.querySelectorAll('[data-place]').forEach((el) => {
      el.addEventListener('click', (e) => setPosition(e, el.dataset.place));
    });
    root.querySelectorAll('[data-gate-handle]').forEach((el) => {
      el.style.touchAction = 'none';
      el.addEventListener('pointerdown', (e) => { e.stopPropagation(); el.setPointerCapture(e.pointerId); el.dataset.drag = '1'; });
      el.addEventListener('pointermove', (e) => {
        if (!el.dataset.drag) return;
        const hit = place(e, el.dataset.channelId);
        if (!hit) return;
        const p = polylinePoint(hit.route, hit.f);
        const r = el.querySelector('rect');
        r.setAttribute('x', num(p[0] - 6)); r.setAttribute('y', num(p[1] - 6));
      });
      const end = (e) => {
        if (!el.dataset.drag) return;
        delete el.dataset.drag;
        setPosition(e, el.dataset.channelId);
      };
      // The click that follows a drag must not re-place the Gate via the line below.
      el.addEventListener('click', (e) => e.stopPropagation());
      el.addEventListener('pointerup', end);
      el.addEventListener('pointercancel', () => { delete el.dataset.drag; });
    });
    root.querySelectorAll('[data-pick]').forEach((el) => {
      el.addEventListener('click', () => {
        if (!this._gateDraft) return;
        this._readGateDraft(root);
        this._gateDraft.channels = toggleChannel(this._gateDraft.channels, el.dataset.pick);
        this._render();
      });
    });
    root.querySelectorAll('[data-act]').forEach((el) => {
      el.addEventListener('click', (e) => {
        e.stopPropagation();
        const zone_id = this._zoneId;
        switch (el.dataset.act) {
          case 'new-gate': this._gateDraft = gateDraft(null); this._render(); break;
          case 'edit-gate':
            this._gateDraft = gateDraft((this._zoneView.gates || []).find((g) => g.id === el.dataset.gate));
            this._render(); break;
          case 'cancel-gate': this._gateDraft = null; this._render(); break;
          case 'save-gate': this._saveGate(root); break;
          case 'delete-gate': {
            const gate_id = this._gateDraft && this._gateDraft.id;
            if (!gate_id) break;
            this._gateDraft = null;
            this._zoneCmd('delete_gate', { gate_id });
            break;
          }
          case 'refresh-zones': this._zoneCmd('refresh_zones'); break;
          case 'toggle-hidden':
            this._zoneCmd('set_zone_hidden', { zone_id: el.dataset.zone, hidden: el.checked }); break;
          case 'set-bearing': {
            const value = num('#bearing-input');
            if (value != null) this._zoneCmd('set_zone_bearing', { zone_id, value });
            break;
          }
          case 'reset-bearing': this._zoneCmd('reset_zone_bearing', { zone_id }); break;
          case 'set-range': {
            const min = num('#range-min'), max = num('#range-max');
            if (min != null && max != null) this._zoneCmd('set_zone_range', { zone_id, min, max });
            break;
          }
          case 'set-range-full': this._zoneCmd('set_zone_range', { zone_id, full: true }); break;
          case 'reset-range': this._zoneCmd('reset_zone_range', { zone_id }); break;
        }
      });
    });
  }

  _unsubscribe() {
    if (this._unsub) { this._unsub(); this._unsub = null; }
    if (this._unsubNow) { this._unsubNow(); this._unsubNow = null; }
    if (this._unsubPrompts) { this._unsubPrompts(); this._unsubPrompts = null; }
  }

  // ---- Now tab (spec §16.2) -------------------------------------------------

  async _subscribeNow() {
    this._now = null;
    this._prompts = [];
    const conn = this._hass.connection;
    this._unsubNow = await conn.subscribeMessage((view) => {
      this._now = view;
      // The map needs Gate states: refresh Zones at most every 10 s of pushes.
      if (this._tab === 'now' && Date.now() - this._nowZonesAt > 10000) {
        this._nowZonesAt = Date.now();
        this._loadZones();
      }
      this._render();
    }, { type: 'groundskeeper/subscribe_now', entry_id: this._entryId });
    this._unsubPrompts = await conn.subscribeMessage((ev) => {
      this._prompts = ev.prompts || [];
      this._render();
    }, { type: 'groundskeeper/subscribe_prompts', entry_id: this._entryId });
  }

  _lengthUnit() {
    const u = this._hass && this._hass.config && this._hass.config.unit_system;
    return (u && u.length) || 'km';
  }

  async _nowCmd(type, extra = {}) {
    try {
      await this._hass.connection.sendMessagePromise({
        type: `groundskeeper/${type}`, entry_id: this._entryId, ...extra });
      return null;
    } catch (err) {
      const message = errorText(err && err.code, errMessage(err));
      this._notice = message;
      this._render();
      return message;
    }
  }

  _nowTab() {
    const view = this._now;
    if (!view) return '<section class="card"><h2>Now</h2><p>Loading.</p></section>';
    const unit = this._lengthUnit();
    const { zones, channels } = this._zoneView;
    const sheetRun = this._sheet && (view.up_next || []).find((r) => r.run_id === this._sheet.run_id);
    const map = renderMap(zones, channels, { gates: this._zoneView.gates || [], ...nowMapOpts(view, this._runId) });
    return `<section class="card"><h2>Map</h2>${map}</section>
      <div><section class="card"><h2>Status</h2>${this._notice ? `<p class="warn" id="notice">${esc(this._notice)}</p>` : ''}
        ${statusCardHtml(view, unit)}</section>
      ${this._prompts.length ? `<section class="card"><h2>Needs your answer</h2>${promptCardsHtml(this._prompts)}</section>` : ''}
      <section class="card"><h2>Up next</h2>${upNextHtml(view, unit, this._runId)}</section>
      <section class="card"><h2>Checks</h2>${checkPillsHtml(view.checks)}</section></div>
      ${sheetRun ? runSheetHtml(sheetRun, unit, this._sheet) : ''}`;
  }

  _wireNow(root) {
    const confirmed = (action) => {
      const text = confirmText(action);
      return !text || (typeof confirm !== 'function') || confirm(text);
    };
    root.querySelectorAll('li.run').forEach((li) => {
      li.addEventListener('click', (e) => {
        if (e.target.closest('button')) return;
        this._runId = this._runId === li.dataset.run ? null : li.dataset.run;
        this._render();
      });
    });
    root.querySelectorAll('[data-ctl]').forEach((b) => {
      b.addEventListener('click', () => {
        this._notice = null;
        if (confirmed(b.dataset.ctl)) this._nowCmd('run_control', { action: b.dataset.ctl });
      });
    });
    root.querySelectorAll('[data-prompt]').forEach((b) => {
      b.addEventListener('click', () => {
        this._notice = null;
        if (confirmed(b.dataset.action)) {
          this._nowCmd('answer_prompt', { prompt_id: b.dataset.prompt, action: b.dataset.action });
        }
      });
    });
    root.querySelectorAll('[data-act]').forEach((b) => {
      b.addEventListener('click', async (e) => {
        e.stopPropagation();
        const run_id = b.dataset.run || (this._sheet && this._sheet.run_id);
        switch (b.dataset.act) {
          case 'add-plan': this._tab = 'schedule'; this._render(); break;
          case 'move-up': case 'move-down': {
            this._notice = null;
            const position = movePosition(this._now.up_next, run_id, b.dataset.act === 'move-up' ? 'up' : 'down');
            this._nowCmd('move_run', { run_id, position });
            break;
          }
          case 'run-menu': this._sheet = { run_id }; this._render(); break;
          case 'close-sheet': this._sheet = null; this._render(); break;
          case 'delete-run': {
            if (await this._nowCmd('delete_run', { run_id }) == null) this._sheet = null;
            this._render();
            break;
          }
          case 'save-run': {
            const { payload, error } = runEditPayload({
              height: root.querySelector('#run-height').value,
              angle: root.querySelector('#run-angle').value }, this._lengthUnit());
            if (error) { this._sheet.error = error; this._render(); break; }
            try {
              const res = await this._hass.connection.sendMessagePromise({
                type: 'groundskeeper/edit_run', entry_id: this._entryId, run_id, ...payload });
              const warnings = (res && res.warnings) || [];
              this._sheet = warnings.length ? { run_id, warnings } : null; // warnings show; edit already saved
            } catch (err) {
              this._sheet.error = errorText(err && err.code, errMessage(err));
              this._sheet.warnings = [];
            }
            this._render();
            break;
          }
        }
      });
    });
  }

  // -- Settings tab: notify targets and categories --
  async _loadNotify() {
    if (!this._entryId || this._tab !== 'settings') return;
    try {
      this._notify = await this._hass.connection.sendMessagePromise({
        type: 'groundskeeper/get_notify_settings', entry_id: this._entryId });
      this._render();
    } catch (err) {
      this._error = errMessage(err);
    }
  }

  async _setNotify(extra) {
    try {
      this._notify = await this._hass.connection.sendMessagePromise({
        type: 'groundskeeper/set_notify_settings', entry_id: this._entryId, ...extra });
    } catch (err) {
      this._notifyError = errMessage(err);
    }
    this._render();
  }

  _settingsTab() {
    const notify = this._notify
      ? notifySettingsHtml(this._notify, notifyTargetOptions(this._hass && this._hass.services))
      : '<h2>Notifications</h2><p class="hint">Loading...</p>';
    const err = this._notifyError ? `<p class="warn" id="notify-error">${esc(this._notifyError)}</p>` : '';
    return settingsTabHtml(notify + err);
  }

  _wireSettings(root) {
    root.querySelectorAll('[data-target]').forEach((box) => {
      box.addEventListener('change', () => {
        this._notifyError = null;
        const picked = [...root.querySelectorAll('[data-target]')].filter((b) => b.checked).map((b) => b.dataset.target);
        this._setNotify(notifyTargetsPayload(picked));
      });
    });
    root.querySelectorAll('[data-category]').forEach((box) => {
      box.addEventListener('change', () => {
        this._notifyError = null;
        this._setNotify({ categories: { [box.dataset.category]: box.checked } });
      });
    });
  }

  _render() {
    this._readGateDraft(this.shadowRoot); // keep typing across pushed re-renders
    const rh = this.shadowRoot.querySelector('#run-height');
    if (this._sheet && rh) {
      this._sheet.draft = { height: rh.value, angle: this.shadowRoot.querySelector('#run-angle').value };
    }
    if (this._tab === 'schedule') this._readPlanDraft(this.shadowRoot);
    if (this._tab === 'checks') this._readCustomDraft(this.shadowRoot);
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
    else if (this._tab === 'zones') body = `<main>${this._zonesTab()}</main>`;
    else if (this._tab === 'now') body = `<main>${this._nowTab()}</main>`;
    else if (this._tab === 'schedule') body = `<main>${this._scheduleTab()}</main>`;
    else if (this._tab === 'checks') body = `<main>${this._checksTab()}</main>`;
    else if (this._tab === 'settings') body = `<main>${this._settingsTab()}</main>`;
    else body = `<main><section class="card"><h2>${esc(label)}</h2><p>Coming soon.</p></section></main>`;
    this.shadowRoot.innerHTML = `<style>${STYLE}</style>
      <header><h1>Groundskeeper</h1><select id="mower" aria-label="Mower">${mowerOptions}</select></header>
      <nav role="tablist">${tabs}</nav>${banner}${body}`;
    this.shadowRoot.querySelector('#mower').onchange = (e) => {
      this._entryId = e.target.value;
      this._checksView = null; // another Mower's Checks; a snapshot push no longer refetches
      this._subscribe().then(() => { this._render(); this._loadChecks(); });
    };
    this.shadowRoot.querySelectorAll('nav button').forEach((b) => {
      b.onclick = () => { this._tab = b.dataset.tab; this._render(); this._loadZones(); this._loadSchedule(); this._loadChecks(); this._loadNotify(); };
    });
    if (this._tab === 'zones') this._wireZones(this.shadowRoot);
    if (this._tab === 'now' && this._now) this._wireNow(this.shadowRoot);
    if (this._tab === 'schedule') this._wireSchedule(this.shadowRoot);
    if (this._tab === 'checks') this._wireChecks(this.shadowRoot);
    if (this._tab === 'settings') this._wireSettings(this.shadowRoot);
  }
}

customElements.define('groundskeeper-panel', GroundskeeperPanel);
