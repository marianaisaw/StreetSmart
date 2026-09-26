// StreetSmart app controller: vanilla DOM + Mapbox/MapLibre GL, mounted by StreetSmart.jsx.
import * as h3 from 'h3-js';
import { gl, isMapbox, styleFor, addBuildings, applyLook } from './engine.js';

let started = false;
export function start() {
  if (started) return;
  started = true;
/* ======================= settings ======================= */
const Q = new URLSearchParams(location.search);
const DEMO_O = { name: 'Mission St & 16th St', lat: 37.76506, lng: -122.41963 };
const DEMO_D = { name: '9th Ave & Irving St', lat: 37.76410, lng: -122.46652 };
const DEFAULTS = {
  origin: DEMO_O, destination: DEMO_D, depart: 'now', deadline: '', tiers: [3, 15, 50], budget: 3,
  priority: 30, max_walk_min: 15, modes: { walk: true, muni: true, bart: true, uber: true }, surge: 1.0, effort: 'medium',
  scoring: { weights: { datasf: 1.0, news: 0.8, x: 0.4 }, halflife_hours: 72, night: true, night_mult: 1.5, thresholds: [2, 6] },
  map: { heatmap: true, opacity: 1, showSafe: true, alternatives: true, legend: true, style: 'dark', tint: 'blue', tilt: true, look: 'night', frame: 'web' },
  bubbles: { on: true, news: true, x: true, sfpd: true, max: 12 },
  stops: { autoCaption: true, autoPhoto: false },
  share: { name: '', eta: true, simulate: 'auto' },
  advanced: { demo: false, trace: false, ai: 'opus' },
};
const TINTS = { blue: '#0a84ff', green: '#30d158', orange: '#ff9f0a', pink: '#ff375f', purple: '#bf5af2', teal: '#40c8e0' };
const clone = o => JSON.parse(JSON.stringify(o));
const merge = (a, b) => { for (const k in b) { if (b[k] && typeof b[k] === 'object' && !Array.isArray(b[k]) && a[k] && typeof a[k] === 'object') merge(a[k], b[k]); else if (b[k] !== undefined) a[k] = b[k]; } return a; };
let S = clone(DEFAULTS);
try { const saved = JSON.parse(localStorage.getItem('streetsmart.v2') || 'null'); if (saved) S = merge(S, saved); } catch {}
if (Q.get('demo') === '1') { S.advanced.demo = true; S.origin = DEMO_O; S.destination = DEMO_D; S.depart = '23:00'; }
const save = () => { try { localStorage.setItem('streetsmart.v2', JSON.stringify(S)); } catch {} };
let DRAFT = null, PRESETS = [];

/* ======================= map + theme ======================= */
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const post = (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
async function jget(res) { if (!res.ok) { const t = await res.text(); let d; try { d = JSON.parse(t).detail; } catch { d = t.slice(0, 160); } throw new Error(d || res.status); } return res.json(); }

let map = null;
let TRIP = null, STOPS = {}, CELLS = null, CURRENT = null, busy = false, planSeq = 0, pickMode = null, STATUS = null, CELLMAP = {};
const BANDC = () => ({ 'very safe': css('--safe'), 'kind of safe': css('--mid'), 'unsafe': css('--bad') });
const MODEC = () => ({ walk: css('--walk'), muni: css('--muni'), bart: css('--bart'), uber: css('--uber'), rail: css('--rail') });
const MODE_NAME = { walk: 'Walk', muni: 'Muni', bart: 'BART', uber: 'Uber', rail: 'Rail' };
const SRC = { x: 'X', news: 'News', datasf: 'SFPD data' };
const GEO = { cells: [], alts: [], rec: [], trail: [] };
const fc = f => ({ type: 'FeatureCollection', features: f });
const line = (coords, props) => ({ type: 'Feature', properties: props || {}, geometry: { type: 'LineString', coordinates: coords.map(([a, b]) => [b, a]) } });
function setSrc(name, feats) { GEO[name] = feats; const s = map && map.getSource(name); if (s) s.setData(fc(feats)); }
let styleKind = null, pinMarkers = [], stopMarkers = [], meMarker = null, popup = null;
function closePopup() { if (popup) { popup.remove(); popup = null; } }
function openPopup(lngLat, html) { closePopup(); popup = new gl.Popup({ offset: 8, closeButton: false, maxWidth: '260px' }).setLngLat(lngLat).setHTML(html).addTo(map); return popup; }
function addLayers() {
  const dark = document.documentElement.dataset.theme === 'dark';
  for (const n of Object.keys(GEO)) if (!map.getSource(n)) map.addSource(n, { type: 'geojson', data: fc(GEO[n]) });
  addBuildings(map, dark);
  // Mapbox Standard lights the scene (night preset); emissive layers stay bright like a car display.
  const glow = (o) => { if (!isMapbox) return o; const k = o.type === 'fill' ? 'fill-emissive-strength' : o.type === 'line' ? 'line-emissive-strength' : null;
    if (k) o.paint = { ...o.paint, [k]: 1 }; o.slot = 'top'; return o; };
  const L = (o) => { if (!map.getLayer(o.id)) map.addLayer(glow(o)); };
  L({ id: 'cells-fill', type: 'fill', source: 'cells', paint: { 'fill-color': ['get', 'color'], 'fill-opacity': ['get', 'fo'] } });
  L({ id: 'cells-line', type: 'line', source: 'cells', paint: { 'line-color': ['get', 'color'], 'line-opacity': ['get', 'lo'], 'line-width': ['get', 'w'] } });
  L({ id: 'alts', type: 'line', source: 'alts', layout: { 'line-cap': 'round' }, paint: { 'line-color': dark ? '#9aa3b5' : '#6b7280', 'line-opacity': .6, 'line-width': 3, 'line-dasharray': [1, 2.5] } });
  L({ id: 'rec-glow', type: 'line', source: 'rec', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': ['get', 'color'], 'line-width': 22, 'line-blur': 14, 'line-opacity': .55 } });
  L({ id: 'rec-case', type: 'line', source: 'rec', layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': dark ? '#000' : '#fff', 'line-width': 11, 'line-opacity': .75 } });
  L({ id: 'rec-line', type: 'line', source: 'rec', filter: ['!=', ['get', 'mode'], 'walk'], layout: { 'line-cap': 'round', 'line-join': 'round' }, paint: { 'line-color': ['get', 'color'], 'line-width': 6 } });
  L({ id: 'rec-walk', type: 'line', source: 'rec', filter: ['==', ['get', 'mode'], 'walk'], layout: { 'line-cap': 'round' }, paint: { 'line-color': ['get', 'color'], 'line-width': 6, 'line-dasharray': [0.1, 1.8] } });
  L({ id: 'trail', type: 'line', source: 'trail', paint: { 'line-color': css('--tint'), 'line-width': 3, 'line-opacity': .85 } });
}
function initMap() {
  styleKind = styleFor(S.map.look);
  map = new gl.Map({ container: 'map', style: styleKind, center: [-122.442, 37.77], zoom: 13, pitch: S.map.tilt ? 55 : 0,
    attributionControl: false, antialias: true });
  map.addControl(new gl.AttributionControl({ compact: true }), 'bottom-right');
  window.__ssMap = map;
  map.on('error', e => console.warn('map error', e && e.error && e.error.message));
  map.on('style.load', () => { applyLook(map, S.map.look); addLayers(); });
  map.on('click', onMapClick);
  map.on('moveend', () => { clearTimeout(bubbleTimer); bubbleTimer = setTimeout(() => drawBubbles(), 120); });
  map.on('mouseenter', 'cells-fill', () => map.getCanvas().style.cursor = 'pointer');
  map.on('mouseleave', 'cells-fill', () => map.getCanvas().style.cursor = '');
}
const mq = matchMedia('(prefers-color-scheme: dark)');
function themeName(st = S) { return st.map.style === 'auto' ? (mq.matches ? 'dark' : 'light') : st.map.style; }
function applyTheme(st = S) {
  const t = themeName(st);
  document.documentElement.dataset.theme = t;
  document.documentElement.style.setProperty('--tint', TINTS[st.map.tint] || TINTS.blue);
  const root = document.querySelector('.ss-root'), wasWeb = root?.classList.contains('web');
  root?.classList.toggle('web', st.map.frame === 'web');
  if (map && wasWeb !== (st.map.frame === 'web')) setTimeout(() => { map.resize(); fitView(); }, 50);
  const url = styleFor(st.map.look);
  if (map && url !== styleKind) { styleKind = url; map.setStyle(url); }
  else if (map && map.isStyleLoaded()) applyLook(map, st.map.look);
  if (map && !driving) map.easeTo({ pitch: st.map.tilt ? 55 : 0, duration: 400 });
  drawCells(st); if (TRIP) drawRoutes(CURRENT && CURRENT.recommended_route_id, st);
  layoutFloating(st);
}
mq.addEventListener?.('change', () => S.map.style === 'auto' && applyTheme());

function isWeb(st = S) { return st.map.frame === 'web' && innerWidth >= 720; }
function layoutFloating(st = S) {
  const top = $('search').offsetTop + $('search').offsetHeight;
  if (isWeb(st)) {
    $('sheet').style.top = (top + 12) + 'px'; $('fabs').style.top = '16px'; $('live').style.bottom = ''; $('legend').style.bottom = '';
    $('legend').style.opacity = st.map.heatmap && st.map.legend ? 1 : 0;
    return { top: 30, peek: 30, left: 440, web: true };
  }
  $('sheet').style.top = '';
  $('fabs').style.top = (top + 10) + 'px';
  const peek = parseFloat(getComputedStyle($('sheet')).getPropertyValue('--peek'));
  $('live').style.bottom = (peek + 10) + 'px';
  $('legend').style.bottom = (peek + 40) + 'px';
  $('legend').style.opacity = st.map.heatmap && st.map.legend ? 1 : 0;
  return { top, peek };
}
function fitView(animate) {
  const { top, peek } = layoutFloating();
  if (!map) return;
  let pts = [[S.origin.lat, S.origin.lng], [S.destination.lat, S.destination.lng]];
  if (TRIP) { const r = CURRENT && TRIP.routes.find(x => x.id === CURRENT.recommended_route_id); pts = pts.concat((r ? [r] : TRIP.routes).flatMap(r => r.legs.flatMap(l => l.coords))); }
  const lats = pts.map(p => p[0]), lngs = pts.map(p => p[1]);
  const H = $('screen').clientHeight;
  map.fitBounds([[Math.min(...lngs), Math.min(...lats)], [Math.max(...lngs), Math.max(...lats)]],
    { padding: { top: Math.min(top + 16, H * .45), bottom: Math.min(peek + 50, H * .45), left: arguments[1] || (isWeb() ? 440 : 30), right: 70 }, maxZoom: 16, duration: animate ? 900 : 0, pitch: S.map.tilt ? 55 : 0, bearing: 0 });
}

/* ---------- safety cells ---------- */
async function loadCells() {
  const p = encodeURIComponent(JSON.stringify(S.scoring));
  CELLS = await (await fetch(`/api/cells?p=${p}&depart=${encodeURIComponent(S.depart)}`)).json();
  drawCells();
}
function cellPopup(c, col) {
  return `<div class="pband" style="color:${col}">${c.band}</div><div>${esc(c.reason)}</div>` +
    (c.sources?.length ? `<div class="badges">${c.sources.map(s => `<span class="badge b-${s}">${SRC[s] || s}</span>`).join('')}</div>` : '') +
    ((c.top || []).length && c.band !== 'very safe' ? `<div class="tops">${c.top.slice(0, 2).map(t => `<div>· ${esc(t.summary)} <span class="muted">(${SRC[t.source] || t.source})</span></div>`).join('')}</div>` : '') +
    (c.incidents ? `<div class="muted tiny" style="margin-top:4px">${c.incidents} report${c.incidents > 1 ? 's' : ''} · risk ${c.risk}</div>` : '');
}
function drawCells(st = S) {
  const feats = []; CELLMAP = {};
  if (st.map.heatmap && CELLS) {
    const BC = BANDC(), k = st.map.opacity, onRoute = new Set(TRIP?.route_cells || []), seen = new Set();
    const add = c => { const col = BC[c.band]; CELLMAP[c.cell] = c;
      feats.push({ type: 'Feature', properties: { cell: c.cell, color: col, w: c.band === 'very safe' ? .6 : 1.2, lo: Math.min(1, .7 * k),
        fo: Math.min(.9, (c.band === 'very safe' ? .12 : c.band === 'kind of safe' ? .32 : .48) * k) },
        geometry: { type: 'Polygon', coordinates: [h3.cellToBoundary(c.cell, true)] } }); };
    for (const c of CELLS) { seen.add(c.cell); if (c.band === 'very safe' && (!st.map.showSafe || !onRoute.has(c.cell))) continue; add(c); }
    if (st.map.showSafe) for (const cell of onRoute) if (!seen.has(cell)) add({ cell, band: 'very safe', reason: 'No incidents reported in the last week', incidents: 0 });
  }
  setSrc('cells', feats);
}

/* ---------- pins ---------- */
function el(html) { const d = document.createElement('div'); d.innerHTML = html; return d.firstElementChild; }
function drawPins() {
  if (!map) return;
  pinMarkers.forEach(m => m.remove()); pinMarkers = [];
  for (const [key, color, label] of [['origin', css('--tint'), 'Start'], ['destination', css('--red'), 'End']]) {
    const p = S[key];
    const m = new gl.Marker({ element: el(`<div class="pin" style="background:${color}" title="${label} · ${esc(p.name)} · drag to move"></div>`), draggable: !isViewer() })
      .setLngLat([p.lng, p.lat]).addTo(map);
    m.on('dragend', async () => { const ll = m.getLngLat(); await setPlace(key, await reverseName(ll.lat, ll.lng)); });
    pinMarkers.push(m);
  }
}
function drawRoutes(recId, st = S) {
  drawPins();
  stopMarkers.forEach(m => m.remove()); stopMarkers = [];
  if (!TRIP) { setSrc('alts', []); setSrc('rec', []); return; }
  const MC = MODEC();
  setSrc('alts', TRIP.routes.filter(r => r.id !== recId && !(recId && !st.map.alternatives)).flatMap(r => r.legs.map(l => line(l.coords))));
  const rec = TRIP.routes.find(r => r.id === recId);
  setSrc('rec', rec ? rec.legs.map(l => line(l.coords, { mode: l.mode, color: MC[l.mode] || MC.muni, label: l.label })) : []);
  if (!rec) return;
  for (const sid of rec.stops) {
    const s = STOPS[sid]; if (!s) continue;
    const m = new gl.Marker({ element: el(`<div class="stopdot" title="${esc(s.name)} · tap for photo"></div>`) }).setLngLat([s.lng, s.lat]).addTo(map);
    m.getElement().addEventListener('click', e => { e.stopPropagation(); openStop(sid); });
    stopMarkers.push(m);
  }
}
/* ======================= places: search, current location, map picks ======================= */
let suggField = null, suggTimer = null, suggItems = [];
const ICON = {
  loc: '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M21 3 3 10.5l7.3 2.2L12.5 20z"/></svg>',
  map: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M12 21s-7-6.2-7-11a7 7 0 1 1 14 0c0 4.8-7 11-7 11Z"/><circle cx="12" cy="10" r="2.5"/></svg>',
  star: '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="m12 2 3 7 7 .6-5.3 4.7L18.3 22 12 18l-6.3 4 1.6-7.7L2 9.6 9 9z"/></svg>',
  pin: '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="5"/></svg>',
};
function showSugg(items) {
  suggItems = items;
  $('sugg').innerHTML = items.map((it, i) => `<div class="li" data-i="${i}"><div class="sico">${ICON[it.icon || 'pin']}</div><div class="grow"><div class="t">${esc(it.name)}</div>${it.sub ? `<div class="s">${esc(it.sub)}</div>` : ''}</div></div>`).join('');
  $('sugg').classList.toggle('on', items.length > 0);
  $('search-rest').style.display = items.length ? 'none' : '';
  $('sugg').querySelectorAll('.li').forEach(el => el.onmousedown = e => { e.preventDefault(); pickSugg(suggItems[+el.dataset.i]); });
  layoutFloating();
}
function baseSugg() {
  return [{ name: 'Current location', icon: 'loc', action: 'loc' }, { name: 'Choose on map', sub: 'Tap anywhere on the map', icon: 'map', action: 'map' }]
    .concat(PRESETS.map(p => ({ ...p, icon: 'star' })));
}
function onFieldFocus(field) { suggField = field; const el = $(field === 'origin' ? 'from' : 'to'); el.select(); showSugg(baseSugg()); }
function onFieldInput(field, q) {
  clearTimeout(suggTimer);
  if (q.trim().length < 2) return showSugg(baseSugg());
  suggTimer = setTimeout(async () => {
    try { const res = await (await fetch('/api/geocode?q=' + encodeURIComponent(q))).json();
      if (suggField === field) showSugg(res.length ? res.map(r => ({ ...r, icon: 'pin' })) : [{ name: 'No places found in San Francisco', icon: 'pin', action: 'none' }]);
    } catch { showSugg([{ name: 'Search is unavailable right now', icon: 'pin', action: 'none' }]); }
  }, 300);
}
function hideSugg() { showSugg([]); $('from').value = S.origin.name; $('to').value = S.destination.name; suggField = null; }
async function pickSugg(it) {
  const field = suggField; if (!field || it.action === 'none') return;
  if (it.action === 'loc') { hideSugg(); document.activeElement.blur(); return useMyLocation(field); }
  if (it.action === 'map') { hideSugg(); document.activeElement.blur(); pickMode = field; return toast(`Tap the map to set your ${field === 'origin' ? 'start' : 'destination'}`); }
  hideSugg(); document.activeElement.blur();
  await setPlace(field, { name: it.name, lat: it.lat, lng: it.lng });
}
async function reverseName(lat, lng) {
  try { const r = await (await fetch(`/api/reverse?lat=${lat}&lng=${lng}`)).json(); return { name: r.name || 'Dropped pin', lat, lng }; }
  catch { return { name: 'Dropped pin', lat, lng }; }
}
const inSF = (lat, lng) => lat > 37.70 && lat < 37.83 && lng > -122.53 && lng < -122.35;
async function setPlace(field, place) {
  if (!inSF(place.lat, place.lng)) return toast('StreetSmart covers San Francisco only');
  S[field] = place; save();
  $('from').value = S.origin.name; $('to').value = S.destination.name;
  if (S.advanced.demo && !isDemoTrip()) toast('Live mode: demo cache is only for the demo trip');
  drawPins(); plan();
}
function swapOD() { [S.origin, S.destination] = [S.destination, S.origin]; save(); $('from').value = S.origin.name; $('to').value = S.destination.name; plan(); }
function getPosition() {
  return new Promise((res, rej) => navigator.geolocation ? navigator.geolocation.getCurrentPosition(p => res(p.coords), rej, { enableHighAccuracy: true, timeout: 10000, maximumAge: 30000 }) : rej(new Error('no geolocation')));
}
async function useMyLocation(field) {
  toast('Finding you…');
  try {
    const c = await getPosition();
    if (!inSF(c.latitude, c.longitude)) return toast('Your location is outside San Francisco');
    showMe(c.latitude, c.longitude);
    await setPlace(field, { ...(await reverseName(c.latitude, c.longitude)), name: 'Current location' });
  } catch { toast('Location unavailable. Allow location access and try again'); }
}
function showMe(lat, lng) {
  if (!map) return;
  if (!meMarker) meMarker = new gl.Marker({ element: el('<div class="me"></div>') }).setLngLat([lng, lat]).addTo(map);
  else meMarker.setLngLat([lng, lat]);
}
async function locateMe() {
  try { const c = await getPosition(); showMe(c.latitude, c.longitude);
    if (inSF(c.latitude, c.longitude)) map.flyTo({ center: [c.longitude, c.latitude], zoom: 16 }); else toast('You appear to be outside San Francisco');
  } catch { toast('Location unavailable'); }
}
async function onMapClick(e) {
  if (isViewer()) return;
  const { lat, lng } = e.lngLat;
  if (pickMode) { const f = pickMode; pickMode = null; return setPlace(f, await reverseName(lat, lng)); }
  const hit = map.queryRenderedFeatures(e.point, { layers: ['cells-fill'] })[0];
  const c = hit && CELLMAP[hit.properties.cell];
  const head = c ? cellPopup(c, BANDC()[c.band]) + '<hr style="border:0;border-top:.5px solid var(--sep);margin:8px 0">' : '';
  const pop = openPopup(e.lngLat, head + '<div class="loading" style="padding:0"><span class="spin"></span>Looking up…</div>');
  const place = await reverseName(lat, lng);
  window._drop = place;
  if (popup === pop) pop.setHTML(head + `<b>${esc(place.name)}</b><div class="popbtns"><button onclick="closePopup();setPlace('origin',window._drop)">Start here</button><button class="go" onclick="closePopup();setPlace('destination',window._drop)">Go here</button></div>`);
}

/* ======================= when + budget ======================= */
function fmt12(hhmm) { const [h, m] = hhmm.split(':').map(Number); return `${(h % 12) || 12}:${String(m).padStart(2, '0')} ${h < 12 ? 'AM' : 'PM'}`; }
function renderWhen() {
  const t = S.depart;
  $('when').innerHTML = `<button class="pillbtn ${t === 'now' ? 'on' : ''}" data-d="now">Leave now</button>
    <button class="pillbtn ${t === '23:00' ? 'on' : ''}" data-d="23:00">Tonight 11 PM</button>
    <label class="pillbtn ${t !== 'now' && t !== '23:00' ? 'on' : ''}">At <input type="time" id="when-t" value="${t === 'now' ? '' : t}"></label>
    ${S.deadline ? `<span class="pillbtn">Arrive by ${fmt12(S.deadline)}</span>` : ''}`;
  $('when').querySelectorAll('button[data-d]').forEach(b => b.onclick = () => { S.depart = b.dataset.d; save(); renderWhen(); tick(); loadCells(); plan(); });
  $('when-t').onchange = e => { if (!e.target.value) return; S.depart = e.target.value; save(); renderWhen(); tick(); loadCells(); plan(); };
}
function renderTiers() {
  $('tiers').innerHTML = S.tiers.map(t => `<button data-b="${t}" class="${t === S.budget ? 'on' : ''}">${FMT.money(t)}</button>`).join('');
  $('tiers').querySelectorAll('button').forEach(b => b.onclick = () => { S.budget = +b.dataset.b; save(); plan(); });
}

/* ======================= plan ======================= */
const near = (a, b) => Math.abs(a.lat - b.lat) < .0012 && Math.abs(a.lng - b.lng) < .0012;
const isDemoTrip = () => near(S.origin, DEMO_O) && near(S.destination, DEMO_D);
const useDemo = () => S.advanced.demo && isDemoTrip() && S.depart === '23:00';
function planBody() {
  return { origin: S.origin, destination: S.destination, budget: S.budget, depart: S.depart, deadline: S.deadline,
    priority: S.priority, max_walk_min: S.max_walk_min, modes: Object.keys(S.modes).filter(m => S.modes[m]),
    surge: S.surge, effort: S.effort, scoring: S.scoring };
}
function loadingView(steps) {
  $('sheet-body').innerHTML = `<div class="loading"><div class="spin"></div><div><b>${esc(S.origin.name)} → ${esc(S.destination.name)}</b>
    <div class="tiny">Opus 5.5 is weighing cost, time and safety for $${S.budget}…</div></div></div><div class="steps">${steps.map(s => `<div>${esc(s)}</div>`).join('')}</div>`;
}
async function plan(opts = {}) {
  const seq = ++planSeq; busy = true;
  renderTiers();
  CURRENT = null; closeStop();
  const steps = [];
  loadingView(steps);
  try {
    const demo = useDemo();
    const mode = opts.mode || S.advanced.ai;
    const q = (extra) => { const u = new URLSearchParams(demo ? { demo: 1 } : {}); for (const k in extra) if (extra[k]) u.set(k, extra[k]); const s = u.toString(); return s ? '?' + s : ''; };
    const trip = await jget(await post('/api/trip' + q({ fresh: opts.fresh ? 1 : 0 }), { origin: S.origin, destination: S.destination, depart: S.depart }));
    if (seq !== planSeq) return;
    TRIP = trip; STOPS = { ...trip.stop_cards };
    const rt = trip.routes.some(r => r.realtime);
    steps.push(`${trip.routes.length} routes found${trip.live ? (rt ? ' with live departures' : '') : ' (demo trip)'}`);
    loadingView(steps); drawCells(); drawRoutes(null); fitView(true);
    steps.push(mode === 'local' ? 'Scoring each route for free (no AI tokens)' : 'Opus 5.5 is scoring each route against live SFPD, news and X incidents');
    loadingView(steps);
    const p = await jget(await post('/api/plan' + q({ mode, force: opts.force ? 1 : 0 }), planBody()));
    if (seq !== planSeq) return;
    TRIP = p.trip; STOPS = { ...p.trip.stop_cards, ...STOPS };
    CURRENT = { ...p, budget: S.budget };
    drawRoutes(p.recommended_route_id); renderPlan(); fitView(true);
    const rec = TRIP.routes.find(r => r.id === p.recommended_route_id);
    if (S.stops.autoCaption) prefetchCaptions(rec.stops);
    loadSpend();
  } catch (e) {
    if (seq !== planSeq) return;
    $('sheet-body').innerHTML = `<div class="rname">Couldn't plan this trip</div><div class="muted">${esc(e.message || e)}</div><button class="btn gray" onclick="plan()">Try again</button>`;
  } finally { if (seq === planSeq) busy = false; }
}
function renderPlan() {
  const p = CURRENT, rec = TRIP.routes.find(r => r.id === p.recommended_route_id), MC = MODEC();
  const modes = [...new Set(rec.legs.map(l => l.mode))];
  const legs = rec.legs.map(l => `<div class="li"><div class="modeico" style="background:${MC[l.mode] || MC.muni};color:${l.mode === 'walk' ? '#000' : '#fff'}">${esc(l.line || MODE_NAME[l.mode][0])}</div>
      <div class="grow"><div class="t">${esc(l.label)}</div><div class="s">${l.departs ? `${l.realtime ? '<span class="rt">Live</span> ' : ''}departs ${esc(l.departs)} · ` : ''}${l.minutes} min${l.miles ? ' · ' + l.miles + ' mi' : ''}${l.headsign ? ' · to ' + esc(l.headsign) : ''}</div></div></div>`).join('');
  const stops = rec.stops.map(sid => { const s = STOPS[sid]; if (!s) return ''; return `<div class="li tap" onclick="openStop('${esc(sid)}')">
      ${s.photo ? `<img class="thumb" src="${esc(s.photo)}" alt="" loading="lazy">` : '<div class="thumb"></div>'}
      <div class="grow"><div class="t">${esc(s.name)}</div><div class="s">${s.caption ? esc(s.caption) : s.photo ? 'Tap for photo + where to stand' : 'Tap to find a photo'}</div></div><span class="chev">›</span></div>`; }).join('');
  const alts = (p.alternatives || []).map(a => { const r = TRIP.routes.find(x => x.id === a.route_id);
    return `<div class="li"><div class="grow"><div class="t">${esc(r ? r.name : a.route_id)}</div><div class="s">${esc(a.why_not)}</div></div></div>`; }).join('');
  const trace = S.advanced.trace ? `<div class="section-h">Agent trace · ${(p.trace || []).length} tool calls</div><div class="list"><div class="li"><div class="trace">${esc((p.trace || []).map(t => `${t.tool}(${JSON.stringify(t.input)})`).join('\n'))}</div></div></div>` : '';
  const arrive = rec.arrive_at || '';
  $('sheet-body').innerHTML = `
    <div class="stats">
      <div><div class="big">${esc(p.cost_label || '$' + (+p.total_cost).toFixed(2))}</div><div class="sub">of $${p.budget} budget</div></div>
      <div><div class="big">${p.total_minutes} min</div><div class="sub">${arrive ? 'arrive ' + esc(arrive) : ''}</div></div>
    </div>
    <div class="rname">${esc(rec.name)}</div>
    <div class="chips">${modes.map(m => `<span class="chip"><i style="background:${MC[m] || MC.muni}"></i>${MODE_NAME[m] || m}</span>`).join('')}
      ${rec.realtime ? '<span class="chip live">Live departures</span>' : ''}${p.cached ? '<span class="chip">Cached · no tokens</span>' : ''}${p.source === 'local' ? '<span class="chip">Free mode</span>' : ''}</div>
    <div class="why"><div class="h"><i></i>${p.source === 'local' ? 'Free safety score · no AI tokens' : 'Opus 5.5 · why this route'}</div><p>${esc(p.why)}</p></div>
    <div class="muted" style="margin-top:8px;font-size:14px">${esc(p.safety_summary)}</div>
    <div class="row2"><button class="btn" onclick="drivePreview()">Preview route</button><button class="btn" onclick="walkWithMe()">Walk with me</button></div>
    <div class="row2">${p.source === 'local' ? '<button class="btn gray" onclick="plan({mode:\'opus\'})">Ask Opus 5.5 (~7¢)</button>' : `<button class="btn gray" onclick="plan({mode:'local'})">Free re-score</button>`}
      <button class="btn gray" onclick="scanArea(this)">Scan X + news here</button></div>
    <div class="section-h">Steps</div><div class="list">${legs}</div>
    ${stops ? `<div class="section-h">Stops</div><div class="list">${stops}</div>` : ''}
    ${(() => { const nn = newsNearRoute(rec); return nn.length ? `<div class="section-h">News near this route</div><div class="list">${nn.map(it => `<div class="li${it.url ? ' tap' : ''}" ${it.url ? `onclick="window.open('${esc(it.url)}','_blank')"` : ''}><span class="badge b-${it.source}">${SRC[it.source]}</span><div class="grow"><div class="t" style="font-size:14px">${esc(it.summary)}</div><div class="s">${esc(it.where || '')}${it.hours_ago != null ? ' · ' + agoH(it.hours_ago) : ''}</div></div></div>`).join('')}</div>` : ''; })()}
    ${alts ? `<div class="section-h">Also considered</div><div class="list">${alts}</div>` : ''}
    ${trace}
    <div class="foot" style="margin-top:14px">${p.source === 'local' ? 'Scored without AI (free)' : p.cached ? 'Cached Opus 5.5 plan (no new tokens)' : 'Planned live by Claude Opus 5.5'} · ${TRIP.live ? 'transit from Transitous (real-time where available)' : 'precomputed demo routes'} · fares from SFMTA and BART · rideshare prices are estimates</div>`;
}

/* ======================= stops ======================= */
async function captionFor(sid) {
  const s = STOPS[sid];
  if (!s || !s.photo || s.caption) return s && s.caption;
  try { const r = await jget(await post('/api/caption', { image_url: s.photo, stop_name: s.name })); s.caption = r.caption || null; } catch { s.caption = null; }
  return s.caption;
}
async function prefetchCaptions(ids) { for (const id of ids) await captionFor(id); if (CURRENT) renderPlan(); }
async function fetchPhoto(sid) {
  const s = STOPS[sid];
  drawStop(sid, null, 'photo');
  try { const r = await jget(await post('/api/stop_photo', { id: sid, name: s.name, lat: s.lat, lng: s.lng, kind: s.kind || 'stop' }));
    Object.assign(s, r); if (r.error) toast(r.error.slice(0, 80)); } catch (e) { toast('Photo lookup failed'); }
  loadSpend();
  if (!s.photo) return drawStop(sid, 'No photo found near this stop.');
  drawStop(sid, null, 'caption'); drawStop(sid, await captionFor(sid)); if (CURRENT) renderPlan();
}
function drawStop(sid, cap, loading) {
  const s = STOPS[sid];
  $('stop-body').innerHTML = `${s.photo ? `<img class="photo" id="stop-photo" src="${esc(s.photo)}" alt="Photo near ${esc(s.name)}">` :
      `<div class="photo" style="display:grid;place-items:center;text-align:center;padding:20px">${loading === 'photo' ? '<div class="loading"><span class="spin"></span>Apify is fetching Google Maps photos for this stop (about 30-60 s)…</div>' : `<div><div class="muted">No photo yet for this stop</div><button class="btn" style="width:auto;padding:10px 16px" onclick="fetchPhoto('${esc(sid)}')">Find a photo with Apify (~1¢)</button></div>`}</div>`}
    ${(s.photos || []).length > 1 ? `<div class="photos">${s.photos.map((u, i) => `<img src="${esc(u)}" class="${i === 0 ? 'on' : ''}" onclick="swapPhoto(this)">`).join('')}</div>` : ''}
    <div class="why"><div class="h"><i></i>Opus 5.5 · where to stand</div><p>${loading === 'caption' ? '<span class="loading" style="padding:0;color:#ebebf599"><span class="spin"></span>Looking at the photo…</span>' : esc(cap || (s.photo ? 'No caption available.' : 'A caption appears once there is a photo.'))}</p></div>
    <div class="section-h">Details</div><div class="list">
      <div class="li"><div class="grow">Type</div><span class="muted">${esc(s.kind || 'stop')}</span></div>
      ${s.place_title ? `<div class="li"><div class="grow">Photo from</div><span class="muted" style="text-align:right">${esc(s.place_title)}</span></div>` : ''}
    </div><div class="foot">Photos via Google Maps (Apify). Captions written by Claude Opus 5.5 from the photo.</div>`;
}
async function openStop(sid) {
  const s = STOPS[sid]; if (!s) return;
  $('stop-title').textContent = s.name;
  openModal('stop-modal');
  if (!s.photo) return S.stops.autoPhoto ? fetchPhoto(sid) : drawStop(sid);
  if (s.caption) return drawStop(sid, s.caption);
  drawStop(sid, null, 'caption'); drawStop(sid, await captionFor(sid));
}
function swapPhoto(img) { $('stop-photo').src = img.src; img.parentNode.querySelectorAll('img').forEach(i => i.classList.toggle('on', i === img)); }
function closeStop() { $('stop-modal').classList.remove('open'); if (!$('settings-modal').classList.contains('open')) $('scrim').classList.remove('on'); }

/* ======================= sheet drag ======================= */
(() => {
  const sheet = $('sheet'), grab = $('grab'); let y0 = null, base = 0, dy = 0;
  const closedY = () => sheet.offsetHeight - parseFloat(getComputedStyle(sheet).getPropertyValue('--peek'));
  grab.addEventListener('pointerdown', e => { y0 = e.clientY; base = sheet.classList.contains('open') ? 0 : closedY(); dy = 0; sheet.classList.add('dragging'); grab.setPointerCapture(e.pointerId); });
  grab.addEventListener('pointermove', e => { if (y0 === null) return; dy = e.clientY - y0; sheet.style.transform = `translateY(${Math.min(closedY(), Math.max(0, base + dy))}px)`; });
  grab.addEventListener('pointerup', () => { sheet.classList.remove('dragging'); sheet.style.transform = '';
    if (Math.abs(dy) < 6) sheet.classList.toggle('open'); else sheet.classList.toggle('open', dy < 0); y0 = null; });
})();

/* ======================= modals ======================= */
function openModal(id) { $('scrim').classList.add('on'); $(id).classList.add('open'); }
function closeModals(cancel) {
  $('scrim').classList.remove('on'); document.querySelectorAll('.modal').forEach(m => m.classList.remove('open'));
  if (cancel && DRAFT) { DRAFT = null; applyTheme(); drawBubbles(); }
}

/* ======================= settings UI ======================= */
const get = (o, path) => path.split('.').reduce((a, k) => a[k], o);
const set = (o, path, v) => { const ks = path.split('.'); const last = ks.pop(); ks.reduce((a, k) => a[k], o)[last] = v; };
const FMT = { money: v => '$' + (v % 1 ? (+v).toFixed(2) : v), min: v => v + ' min', w: v => (+v).toFixed(1), x: v => (+v).toFixed(1) + '×', h: v => v + ' h', pct: v => Math.round(v * 100) + '%', n: v => v,
  prio: v => v < 20 ? 'Safest' : v > 80 ? 'Fastest' : v < 45 ? 'Mostly safety' : v > 55 ? 'Mostly speed' : 'Balanced' };
const fmtFor = {};
function sw(path, label, sub) { return `<div class="li"><div class="grow"><div class="t">${label}</div>${sub ? `<div class="s">${sub}</div>` : ''}</div>
  <label class="switch"><input type="checkbox" data-p="${path}" ${get(DRAFT, path) ? 'checked' : ''}><span></span></label></div>`; }
function stepper(path, label, step, min, max, fmt, sub) { fmtFor[path] = fmt; return `<div class="li"><div class="grow"><div class="t">${label}</div>${sub ? `<div class="s">${sub}</div>` : ''}</div>
  <span class="val" id="v-${path}">${fmt(get(DRAFT, path))}</span><div class="stepper" data-p="${path}" data-step="${step}" data-min="${min}" data-max="${max}"><button data-d="-1">−</button><button data-d="1">+</button></div></div>`; }
function slider(path, label, min, max, step, fmt, ends) { fmtFor[path] = fmt; return `<div class="li slider-row"><div class="top"><span class="t">${label}</span><span class="val" id="v-${path}">${fmt(get(DRAFT, path))}</span></div>
  <input type="range" data-p="${path}" min="${min}" max="${max}" step="${step}" value="${get(DRAFT, path)}">${ends ? `<div class="ends"><span>${ends[0]}</span><span>${ends[1]}</span></div>` : ''}</div>`; }
function segc(path, label, opts) { return `<div class="li"><div class="grow t">${label}</div><div class="seg small" data-p="${path}">${opts.map(([v, l]) => `<button data-v="${v}" class="${get(DRAFT, path) === v ? 'on' : ''}">${l}</button>`).join('')}</div></div>`; }
function txt(path, label, ph = '') { return `<div class="li"><div class="t">${label}</div><input class="txt" type="text" data-p="${path}" value="${esc(get(DRAFT, path))}" placeholder="${esc(ph)}"></div>`; }

function renderSettings(section) {
  const MC = MODEC();
  const modeRow = (m, letter, sub) => `<div class="li"><div class="modeico" style="background:${MC[m]};color:${m === 'walk' ? '#000' : '#fff'}">${letter}</div><div class="grow"><div class="t">${MODE_NAME[m]}</div><div class="s">${sub}</div></div>
    <label class="switch"><input type="checkbox" data-p="modes.${m}" ${DRAFT.modes[m] ? 'checked' : ''}><span></span></label></div>`;
  const st = STATUS || {};
  $('settings-body').innerHTML = `
    <div class="section-h">Trip</div><div class="list">
      <div class="li"><div class="grow"><div class="t">From</div><div class="s">${esc(S.origin.name)}</div></div></div>
      <div class="li"><div class="grow"><div class="t">To</div><div class="s">${esc(S.destination.name)}</div></div></div>
      ${segc('depart', 'Leave', [['now', 'Now'], ['23:00', '11 PM']])}
      <div class="li"><div class="grow t">Or leave at</div><input type="time" data-p="depart" value="${DRAFT.depart === 'now' ? '' : DRAFT.depart}"></div>
      <div class="li"><div class="grow"><div class="t">Arrive by</div><div class="s">Optional deadline</div></div><input type="time" data-p="deadline" value="${DRAFT.deadline}"></div>
      <div class="li tap" onclick="loadDemoTrip()"><div class="grow t" style="color:var(--tint)">Load the demo trip</div><span class="chev">›</span></div>
    </div>
    <div class="foot">Change places with the search fields, the map (tap it or drag the pins), or your current location.</div>

    <div class="section-h">Budget buttons</div><div class="list">
      ${stepper('tiers.0', 'Low', .5, .5, 200, FMT.money)}${stepper('tiers.1', 'Medium', 1, 1, 200, FMT.money)}${stepper('tiers.2', 'High', 5, 1, 500, FMT.money)}
    </div>

    <div class="section-h">Route preferences</div><div class="list">
      ${slider('priority', 'Priority', 0, 100, 5, FMT.prio, ['Safety', 'Speed'])}
      ${stepper('max_walk_min', 'Max walking', 1, 0, 45, FMT.min)}
      ${stepper('surge', 'Rideshare surge', .1, 1, 3, FMT.x, 'Multiplies the Uber estimate')}
    </div>
    <div class="section-h">Allowed modes</div><div class="list">
      ${modeRow('walk', 'W', 'Walking legs')}${modeRow('muni', 'M', 'Buses, Muni Metro, streetcars · $2.85 Clipper')}
      ${modeRow('bart', 'B', 'BART · priced per station pair')}${modeRow('uber', 'U', 'Rideshare · estimated')}
    </div>

    <div class="section-h">Safety scoring</div><div class="list">
      ${slider('scoring.halflife_hours', 'Recency window', 12, 336, 12, FMT.h, ['Last few hours', '2 weeks'])}
      ${sw('scoring.night', 'Night adjustment', 'After 9pm: ×1.5, softened by each place open late nearby')}
      ${stepper('scoring.night_mult', 'Night multiplier', .1, 1, 3, FMT.x)}
      ${stepper('scoring.thresholds.0', '“Very safe” below', .5, .5, 20, FMT.w)}
      ${stepper('scoring.thresholds.1', '“Unsafe” above', .5, 1, 40, FMT.w)}
    </div>
    <div class="section-h">Source trust</div><div class="list">
      ${slider('scoring.weights.datasf', 'SFPD open data', 0, 1, .1, FMT.w)}
      ${slider('scoring.weights.news', 'Local news', 0, 1, .1, FMT.w)}
      ${slider('scoring.weights.x', 'X', 0, 1, .1, FMT.w)}
    </div>
    <div class="foot">0 hides a source. Cells re-score instantly with no model calls.</div>

    <div class="section-h">Map</div><div class="list">
      ${segc('map.look', 'Map', [['night', 'Night'], ['dusk', 'Dusk'], ['day', 'Day'], ['satellite', 'Sat']])}
      ${segc('map.style', 'Panels', [['dark', 'Dark'], ['light', 'Light'], ['auto', 'Auto']])}
      ${segc('map.frame', 'Layout', [['web', 'Full page'], ['phone', 'iPhone']])}
      ${sw('map.tilt', '3D view', 'Tilted map with 3D buildings')}
      <div class="li"><div class="grow t">Accent</div><div class="tints">${Object.entries(TINTS).map(([k, c]) => `<button data-tint="${k}" style="background:${c}" class="${DRAFT.map.tint === k ? 'on' : ''}" aria-label="${k}"></button>`).join('')}</div></div>
      ${sw('map.heatmap', 'Safety heatmap')}${sw('map.showSafe', 'Show “very safe” cells on the route')}
      ${slider('map.opacity', 'Heatmap strength', .2, 1.6, .1, FMT.pct)}
      ${sw('map.legend', 'Legend')}${sw('map.alternatives', 'Show other routes')}
    </div>

    <div class="section-h">News bubbles</div><div class="list">
      ${sw('bubbles.on', 'Show news bubbles on the map', 'Brief notes from local news, X posts and SFPD hotspots')}
      ${sw('bubbles.news', 'Local news')}${sw('bubbles.x', 'X posts')}${sw('bubbles.sfpd', 'SFPD hotspots')}
      ${stepper('bubbles.max', 'Max bubbles on screen', 2, 2, 30, FMT.n)}
    </div>
    <div class="foot">Tap a bubble to see where it happened and open the source.</div>

    <div class="section-h">Stops</div><div class="list">
      ${sw('stops.autoCaption', 'Auto-caption stop photos', 'Opus 5.5 vision, cached after the first look')}
      ${sw('stops.autoPhoto', 'Auto-fetch missing stop photos', 'Runs a small Apify Google Maps job (~1¢, ~30 s)')}
    </div>

    <div class="section-h">Walk with me</div><div class="list">${txt('share.name', 'Your name', 'Optional')}${sw('share.eta', 'Include arrival time')}
      ${segc('share.simulate', 'Position', [['auto', 'Auto'], ['gps', 'GPS'], ['sim', 'Simulate']])}</div>
    <div class="foot">Auto uses GPS in San Francisco and simulates walking the route anywhere else (handy for demos).</div>

    <div class="section-h" id="sec-agent">Agent and live data</div><div class="list">
      ${segc('advanced.ai', 'Route picker', [['opus', 'Opus 5.5'], ['local', 'Free']])}
      ${segc('effort', 'Opus effort', [['low', 'Low'], ['medium', 'Med'], ['high', 'High']])}
      ${sw('advanced.demo', 'Demo mode', 'Cached plans for the demo trip at 11 PM')}
      ${sw('advanced.trace', 'Show agent trace')}
      <div class="li"><div class="grow"><div class="t">SFPD reports</div><div class="s" id="st-sfpd">${st.datasf_updated ? `${st.datasf_rows} reports (7 days, citywide) · updated ${ago(st.datasf_updated)} · auto every 10 min` : 'Not loaded yet'}</div></div></div>
      <div class="li tap" onclick="refreshNow(this)"><div class="grow"><div class="t" style="color:var(--tint)">Refresh SFPD data now</div><div class="s">Free · DataSF open data</div></div><span class="chev">›</span></div>
      <div class="li tap" onclick="liveRefresh(this)"><div class="grow"><div class="t" style="color:var(--tint)">Check X for new reports</div><div class="s">Opus 5.5 runs Apify's X scraper via remote MCP · about $0.45</div></div><span class="chev">›</span></div>
      <div class="li"><div class="grow"><div class="t">Spent so far</div><div class="s" id="usage">Loading…</div></div></div>
    </div>

    <div class="section-h"></div><div class="list"><div class="li tap" onclick="resetSettings()"><div class="danger">Reset all settings</div></div></div>
    <div class="foot" style="text-align:center;margin-top:16px">StreetSmart · Claude Opus 5.5 · Apify · Transitous · DataSF · H3</div>`;
  bindSettings();
  fetch('/api/usage').then(r => r.json()).then(u => { $('usage').textContent = `Opus 5.5: ${u.calls} calls, ${(u.input + u.cache_read + u.cache_write).toLocaleString()} in / ${u.output.toLocaleString()} out tokens, $${u.opus_usd.toFixed(3)} · Apify: ${u.apify_runs} runs, $${u.apify_usd.toFixed(2)}`; })
    .catch(() => $('usage').textContent = '--');
  if (section === 'agent') setTimeout(() => $('sec-agent').scrollIntoView({ behavior: 'smooth' }), 350);
}
function preview() { applyTheme(DRAFT); }
function bindSettings() {
  const body = $('settings-body');
  const after = p => { if (p.startsWith('map.')) preview(); if (p.startsWith('bubbles.')) drawBubbles(DRAFT); };
  body.querySelectorAll('input[type=checkbox]').forEach(i => i.onchange = () => { set(DRAFT, i.dataset.p, i.checked); after(i.dataset.p); });
  body.querySelectorAll('input[type=range]').forEach(i => i.oninput = () => { set(DRAFT, i.dataset.p, +i.value); $('v-' + i.dataset.p).textContent = fmtFor[i.dataset.p](+i.value); after(i.dataset.p); });
  body.querySelectorAll('input.txt').forEach(i => i.oninput = () => set(DRAFT, i.dataset.p, i.value));
  body.querySelectorAll('input[type=time]').forEach(i => i.onchange = () => { set(DRAFT, i.dataset.p, i.value || (i.dataset.p === 'depart' ? 'now' : '')); if (i.dataset.p === 'depart') body.querySelectorAll('.seg.small[data-p=depart] button').forEach(x => x.classList.toggle('on', x.dataset.v === DRAFT.depart)); });
  body.querySelectorAll('.stepper').forEach(st => st.querySelectorAll('button').forEach(b => b.onclick = () => {
    const p = st.dataset.p; let v = +get(DRAFT, p) + +st.dataset.step * +b.dataset.d;
    v = Math.min(+st.dataset.max, Math.max(+st.dataset.min, Math.round(v * 10) / 10)); set(DRAFT, p, v); $('v-' + p).textContent = fmtFor[p](v); }));
  body.querySelectorAll('.seg.small').forEach(sg => sg.querySelectorAll('button').forEach(b => b.onclick = () => {
    set(DRAFT, sg.dataset.p, b.dataset.v); sg.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b)); after(sg.dataset.p); }));
  body.querySelectorAll('.tints button').forEach(b => b.onclick = () => {
    DRAFT.map.tint = b.dataset.tint; body.querySelectorAll('.tints button').forEach(x => x.classList.toggle('on', x === b)); after('map.tint'); });
}
function openSettings(section) { DRAFT = clone(S); renderSettings(section); $('settings-body').scrollTop = 0; openModal('settings-modal'); }
function resetSettings() { const keep = { origin: S.origin, destination: S.destination }; DRAFT = { ...clone(DEFAULTS), ...keep }; renderSettings(); preview(); }
async function applySettings() {
  if (!DRAFT) return closeModals();
  const before = JSON.stringify(planBody()), beforeScore = JSON.stringify([S.scoring, S.depart]), demoBefore = S.advanced.demo;
  DRAFT.tiers = [...new Set(DRAFT.tiers.map(Number))].sort((a, b) => a - b);
  while (DRAFT.tiers.length < 3) DRAFT.tiers.push(DRAFT.tiers[DRAFT.tiers.length - 1] + 5);
  if (!DRAFT.tiers.includes(DRAFT.budget)) DRAFT.budget = DRAFT.tiers[0];
  if (!Object.values(DRAFT.modes).some(Boolean)) DRAFT.modes.uber = true;
  DRAFT.origin = S.origin; DRAFT.destination = S.destination;
  S = DRAFT; DRAFT = null; save(); closeModals();
  renderTiers(); renderWhen(); applyTheme(); drawBubbles(); tick();
  if (JSON.stringify([S.scoring, S.depart]) !== beforeScore) await loadCells();
  if (JSON.stringify(planBody()) !== before || S.advanced.demo !== demoBefore) plan();
  else if (CURRENT) { drawRoutes(CURRENT.recommended_route_id); renderPlan(); }
}

/* ======================= live data ======================= */
function ago(iso) { const s = Math.max(0, (Date.now() - new Date(iso)) / 1000); return s < 90 ? 'just now' : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`; }
let lastSfpd = null, spend = '';
async function loadStatus() {
  try {
    STATUS = await (await fetch('/api/status')).json();
    const ok = !!STATUS.datasf_updated;
    $('live-dot').classList.toggle('off', !ok);
    $('live-text').textContent = ok ? `Live · SFPD ${ago(STATUS.datasf_updated)} · Muni & BART real-time${spend}` : (STATUS.refreshing ? 'Loading live SFPD data…' : `Live data offline${spend}`);
    if (ok && lastSfpd && STATUS.datasf_updated !== lastSfpd) loadCells();
    lastSfpd = STATUS.datasf_updated;
  } catch { $('live-dot').classList.add('off'); $('live-text').textContent = 'Server offline'; }
}
async function loadSpend() {
  try { const u = await (await fetch('/api/usage')).json(); spend = ` · Opus $${u.opus_usd.toFixed(2)} · Apify $${u.apify_usd.toFixed(2)}`; loadStatus(); } catch {}
}
async function refreshNow(row) {
  const s = row.querySelector('.s'); s.innerHTML = '<span class="spin" style="width:12px;height:12px;vertical-align:-2px"></span> Pulling the newest SFPD reports…';
  await post('/api/refresh');
  for (let i = 0; i < 40; i++) { await new Promise(r => setTimeout(r, 1500)); await loadStatus(); if (!STATUS.refreshing) break; }
  s.textContent = STATUS.error ? 'Refresh failed: ' + STATUS.error : `Updated · ${STATUS.datasf_rows} reports`;
  await loadCells();
}
async function liveRefresh(row) {
  const s = row.querySelector('.s'); s.innerHTML = '<span class="spin" style="width:12px;height:12px;vertical-align:-2px"></span> Scraping X through Apify MCP…';
  try { const r = await jget(await post('/api/live'));
    s.textContent = r.added ? `${r.added} new report${r.added > 1 ? 's' : ''} added to the map` : 'No new located reports in the last 24h';
    if (r.added) await loadCells(); loadSpend();
  } catch { s.textContent = 'Live check failed'; }
}

/* ======================= Walk with me (live) ======================= */
let shareId = null, watchId = null, simTimer = null;
const isViewer = () => !!Q.get('watch');
function hav(a, b) { const r = Math.PI / 180, x = Math.sin((b[0] - a[0]) * r / 2) ** 2 + Math.cos(a[0] * r) * Math.cos(b[0] * r) * Math.sin((b[1] - a[1]) * r / 2) ** 2; return 12742000 * Math.asin(Math.sqrt(x)); }
function pathOf(route) { return route.legs.flatMap(l => l.coords); }
function alongPath(path, frac) {
  const seg = []; let total = 0;
  for (let i = 1; i < path.length; i++) { const d = hav(path[i - 1], path[i]); seg.push(d); total += d; }
  let target = total * Math.min(1, Math.max(0, frac));
  for (let i = 1; i < path.length; i++) { if (target <= seg[i - 1]) { const t = seg[i - 1] ? target / seg[i - 1] : 0;
      return [path[i - 1][0] + (path[i][0] - path[i - 1][0]) * t, path[i - 1][1] + (path[i][1] - path[i - 1][1]) * t]; } target -= seg[i - 1]; }
  return path[path.length - 1];
}
async function walkWithMe() {
  if (!CURRENT) return;
  const rec = TRIP.routes.find(r => r.id === CURRENT.recommended_route_id);
  try {
    const r = await jget(await post('/api/share', { name: S.share.name, eta: S.share.eta ? (rec.arrive_at || '') : '',
      route: { name: rec.name, legs: rec.legs.map(l => ({ mode: l.mode, label: l.label, coords: l.coords })) }, origin: S.origin, destination: S.destination }));
    shareId = r.id;
  } catch { return toast('Could not start sharing'); }
  const link = `${location.origin}${location.pathname}?watch=${shareId}`;
  try { await navigator.clipboard.writeText(link); toast('Live link copied · send it to a friend'); } catch { prompt('Copy this live link:', link); }
  startTracking(rec);
}
function startTracking(rec) {
  stopTracking(true);
  const path = pathOf(rec), send = (lat, lng, simulated) => { showMe(lat, lng); if (S.map.tilt && !driving) map.easeTo({ center: [lng, lat], zoom: 16.5, pitch: 60, duration: 1800 }); post(`/api/share/${shareId}/pos`, { lat, lng, simulated }).catch(() => {}); };
  const simulate = () => { let f = 0; const total = CURRENT.total_minutes * 60; const step = 2, speed = 20;
    simTimer = setInterval(() => { f += (step * speed) / total; const [la, ln] = alongPath(path, f); send(la, ln, true); if (f >= 1) clearInterval(simTimer); }, step * 1000);
    renderSharing(true); };
  const mode = S.share.simulate;
  if (mode === 'sim' || !navigator.geolocation) return simulate();
  navigator.geolocation.getCurrentPosition(p => {
    if (mode === 'auto' && !inSF(p.coords.latitude, p.coords.longitude)) return simulate();
    watchId = navigator.geolocation.watchPosition(q => send(q.coords.latitude, q.coords.longitude, false), () => {}, { enableHighAccuracy: true });
    send(p.coords.latitude, p.coords.longitude, false); renderSharing(false);
  }, () => mode === 'gps' ? toast('Location unavailable') : simulate(), { timeout: 8000 });
}
function stopTracking(silent) {
  if (watchId !== null) navigator.geolocation.clearWatch(watchId); if (simTimer) clearInterval(simTimer);
  watchId = null; simTimer = null; if (meMarker) { meMarker.remove(); meMarker = null; }
  if (!silent) { $('share-banner').style.display = 'none'; layoutFloating(); toast('Stopped sharing'); }
}
function renderSharing(sim) {
  $('share-banner').style.display = 'block';
  $('share-banner').innerHTML = `Sharing live${sim ? ' (simulated walk)' : ''} · <u style="cursor:pointer" onclick="stopTracking()">Stop</u>`;
  layoutFloating();
}
async function viewer(id) {
  $('search-main').style.display = 'none'; $('fabs').style.display = 'none';
  let drawn = false;
  const tickV = async () => {
    try {
      const s = await jget(await fetch('/api/share/' + id));
      if (!drawn && map.isStyleLoaded()) {
        drawn = true; const MC = MODEC();
        setSrc('rec', s.route.legs.map(l => line(l.coords, { mode: l.mode, color: MC[l.mode] || MC.muni })));
        if (s.origin && s.destination) { S.origin = s.origin; S.destination = s.destination; drawPins(); }
        const pts = s.route.legs.flatMap(l => l.coords), la = pts.map(p => p[0]), ln = pts.map(p => p[1]);
        map.fitBounds([[Math.min(...ln), Math.min(...la)], [Math.max(...ln), Math.max(...la)]], { padding: { top: 120, bottom: 300, left: 30, right: 70 }, duration: 0 });
      }
      $('share-banner').style.display = 'block';
      $('share-banner').textContent = `Following ${s.name || 'a friend'} live` + (s.eta ? ` · ETA ${s.eta}` : '');
      if (s.pos) { showMe(s.pos.lat, s.pos.lng); setSrc('trail', [line(s.trail.length > 1 ? s.trail : [s.trail[0] || [s.pos.lat, s.pos.lng], [s.pos.lat, s.pos.lng]])]); }
      $('sheet-body').innerHTML = `<div class="rname">${esc(s.route.name)}</div>
        <div class="muted">${esc(s.origin?.name || '')} → ${esc(s.destination?.name || '')}</div>
        <div class="list" style="margin-top:12px"><div class="li"><div class="grow"><div class="t">Last update</div><div class="s">${s.age_s === null ? 'Waiting for their first position…' : s.age_s < 10 ? 'Just now' : s.age_s + ' s ago'}${s.pos?.simulated ? ' · simulated' : ''}</div></div><span class="rt">Live</span></div>
        ${s.eta ? `<div class="li"><div class="grow t">Expected arrival</div><span class="muted">${esc(s.eta)}</span></div>` : ''}</div>`;
    } catch { $('sheet-body').innerHTML = '<div class="rname">This live link has expired</div>'; }
  };
  await tickV(); setInterval(tickV, 3000);
}

/* ======================= news bubbles ======================= */
let NEWS = { items: [], hotspots: [] }, bubbleMarkers = [], bubbleTimer = null;
const TYPE_LABEL = { violent: 'Violence', theft: 'Theft', harassment: 'Harassment', hazard: 'Hazard', police_activity: 'Police', other: 'Report', hotspot: 'Hotspot' };
async function loadNews() {
  try { NEWS = await (await fetch('/api/news')).json(); } catch { NEWS = { items: [], hotspots: [] }; }
  drawBubbles(); if (CURRENT) renderPlan();
}
function agoH(h) { return h == null ? '' : h < 1 ? 'just now' : h < 24 ? `${Math.round(h)}h ago` : `${Math.round(h / 24)}d ago`; }
function bubbleSet(st = S) {
  const b = st.bubbles;
  return [...NEWS.items.filter(i => (i.source === 'news' && b.news) || (i.source === 'x' && b.x)), ...(b.sfpd ? NEWS.hotspots : [])];
}
function drawBubbles(st = S) {
  bubbleMarkers.forEach(m => m.remove()); bubbleMarkers = [];
  $('fab-news')?.classList.toggle('off', !st.bubbles.on);
  if (!map || !st.bubbles.on || driving || map.getZoom() < 11) return;
  const bounds = map.getBounds(), placed = [];
  const cand = bubbleSet(st).filter(i => bounds.contains([i.lng, i.lat]))
    .sort((a, b) => (b.source !== 'datasf') - (a.source !== 'datasf') || b.severity - a.severity || (a.hours_ago ?? 1e9) - (b.hours_ago ?? 1e9));
  for (const it of cand) {
    if (placed.length >= st.bubbles.max) break;
    const p = map.project([it.lng, it.lat]);
    if (placed.some(q => Math.abs(q.x - p.x) < 190 && Math.abs(q.y - p.y) < 64)) continue;  // declutter
    placed.push(p);
    const src = it.source === 'datasf' ? 'SFPD data' : SRC[it.source];
    const node = el(`<div class="nb nb-${it.source}"><div class="nb-h"><span class="badge b-${it.source}">${src}</span><span>${esc(TYPE_LABEL[it.type] || '')}</span><span class="nb-t">${agoH(it.hours_ago)}</span></div>
      <div class="nb-s">${esc(it.summary)}</div>
      <div class="nb-more">${it.where ? `<div>${esc(it.where)}</div>` : ''}${it.url ? `<a href="${esc(it.url)}" target="_blank" rel="noopener">Open source ↗</a>` : ''}</div></div>`);
    node.addEventListener('click', e => { e.stopPropagation(); node.classList.toggle('open'); });
    bubbleMarkers.push(new gl.Marker({ element: node, anchor: 'bottom', offset: [0, -6] }).setLngLat([it.lng, it.lat]).addTo(map));
  }
}
function toggleBubbles() { S.bubbles.on = !S.bubbles.on; save(); drawBubbles(); toast(S.bubbles.on ? 'News bubbles on' : 'News bubbles off'); }
function newsNearRoute(rec, meters = 450) {
  const pts = rec.legs.flatMap(l => l.coords).filter((_, i) => i % 3 === 0);
  return bubbleSet().filter(it => it.source !== 'datasf' && pts.some(p => hav(p, [it.lat, it.lng]) < meters)).slice(0, 5);
}

/* ======================= Tesla-style drive preview ======================= */
let driving = null, puck = null;
function bearingTo(a, b) { const r = Math.PI / 180, y = Math.sin((b[1] - a[1]) * r) * Math.cos(b[0] * r),
  x = Math.cos(a[0] * r) * Math.sin(b[0] * r) - Math.sin(a[0] * r) * Math.cos(b[0] * r) * Math.cos((b[1] - a[1]) * r);
  return (Math.atan2(y, x) / r + 360) % 360; }
function drivePreview() {
  if (!CURRENT || !map) return;
  if (driving) return stopDrive();
  const rec = TRIP.routes.find(r => r.id === CURRENT.recommended_route_id), path = pathOf(rec);
  let total = 0; for (let i = 1; i < path.length; i++) total += hav(path[i - 1], path[i]);
  const dur = Math.min(45000, Math.max(14000, total * 4)), t0 = performance.now();
  let bear = bearingTo(path[0], path[Math.min(5, path.length - 1)]);
  if (!puck) puck = new gl.Marker({ element: el('<div class="puck"><div></div></div>'), rotationAlignment: 'map', pitchAlignment: 'map' });
  puck.setLngLat([path[0][1], path[0][0]]).addTo(map);
  $('sheet').classList.remove('open'); document.querySelector('.ss-root').classList.add('driving');
  $('drive-hud').style.display = 'flex'; bubbleMarkers.forEach(m => m.remove()); bubbleMarkers = [];
  const frame = now => {
    const f = Math.min(1, (now - t0) / dur), p = alongPath(path, f), ahead = alongPath(path, Math.min(1, f + 0.012));
    const target = hav(p, ahead) > 3 ? bearingTo(p, ahead) : bear;
    let d = ((target - bear + 540) % 360) - 180; bear = (bear + d * 0.06 + 360) % 360;
    map.jumpTo({ center: [p[1], p[0]], zoom: 17.2, pitch: 68, bearing: bear, padding: { top: 0, bottom: 0, left: isWeb() ? 420 : 0, right: 0 } });
    puck.setLngLat([p[1], p[0]]).setRotation(bear);
    const leg = legAt(rec, f);
    $('drive-hud').innerHTML = `<div class="hud-main"><b>${esc(leg ? leg.label : rec.name)}</b><span>${Math.max(0, Math.round(CURRENT.total_minutes * (1 - f)))} min · ${Math.round(total * (1 - f) / 160.934) / 10} mi left</span></div><button onclick="drivePreview()">End</button>`;
    if (f < 1) driving = requestAnimationFrame(frame); else stopDrive();
  };
  driving = requestAnimationFrame(frame);
}
function legAt(rec, f) { let tot = 0; const lens = rec.legs.map(l => { let d = 0; for (let i = 1; i < l.coords.length; i++) d += hav(l.coords[i - 1], l.coords[i]); tot += d; return d; });
  let acc = 0; for (let i = 0; i < lens.length; i++) { acc += lens[i]; if (f * tot <= acc) return rec.legs[i]; } return rec.legs[rec.legs.length - 1]; }
function stopDrive() {
  if (driving) cancelAnimationFrame(driving); driving = null;
  if (puck) puck.remove(); $('drive-hud').style.display = 'none'; document.querySelector('.ss-root').classList.remove('driving');
  map.easeTo({ bearing: 0, duration: 600 }); setTimeout(() => fitView(true), 650);
}

/* ======================= refresh / scan ======================= */
async function refreshAll() {
  toast('Refreshing live data…');
  $('fab-ref').classList.add('spinning');
  try {
    await post('/api/refresh');
    for (let i = 0; i < 30; i++) { await new Promise(r => setTimeout(r, 1200)); await loadStatus(); if (!STATUS.refreshing) break; }
    await loadCells(); loadNews();
    await plan({ fresh: true, force: S.advanced.ai === 'opus' ? false : false });
    toast(`Updated · ${STATUS.datasf_rows || 0} SFPD reports · fresh departures`);
  } finally { $('fab-ref').classList.remove('spinning'); }
}
async function scanArea(btn) {
  btn.disabled = true; btn.innerHTML = '<span class="spin" style="width:14px;height:14px;vertical-align:-2px"></span> Scanning X + news…';
  try {
    const r = await jget(await post('/api/scan', { origin: S.origin, destination: S.destination, depart: S.depart }));
    toast(r.added ? `${r.added} new incident${r.added > 1 ? 's' : ''} from ${r.new_items} new posts` : `No new located incidents in ${r.items} posts`);
    if (r.added) { await loadCells(); await loadNews(); plan({ mode: 'local' }); }
    loadSpend();
  } catch (e) { toast('Scan failed: ' + e.message); }
  btn.disabled = false; btn.textContent = 'Scan X + news here';
}
function loadDemoTrip() { closeModals(true); S.origin = DEMO_O; S.destination = DEMO_D; S.depart = '23:00'; save(); $('from').value = S.origin.name; $('to').value = S.destination.name; renderWhen(); loadCells(); plan(); }

/* ======================= misc ======================= */
function toggleHeat() { S.map.heatmap = !S.map.heatmap; save(); $('fab-heat').classList.toggle('off', !S.map.heatmap); drawCells(); layoutFloating(); }
let toastT; function toast(t) { const el = $('toast'); el.textContent = t; el.classList.add('on'); clearTimeout(toastT); toastT = setTimeout(() => el.classList.remove('on'), 2600); }
function tick() {
  const d = new Date(); let h = d.getHours(), m = d.getMinutes();
  if (S.depart !== 'now' && !isViewer()) [h, m] = S.depart.split(':').map(Number);
  $('clock').textContent = `${(h % 12) || 12}:${String(m).padStart(2, '0')}`;
}

for (const [id, field] of [['from', 'origin'], ['to', 'destination']]) {
  const el = $(id);
  el.addEventListener('focus', () => onFieldFocus(field));
  el.addEventListener('input', e => onFieldInput(field, e.target.value));
  el.addEventListener('blur', () => setTimeout(() => { if (document.activeElement !== $('from') && document.activeElement !== $('to')) hideSugg(); }, 150));
  el.addEventListener('keydown', e => { if (e.key === 'Enter' && suggItems[0] && !suggItems[0].action) pickSugg(suggItems[0]); if (e.key === 'Escape') { hideSugg(); el.blur(); } });
}
$('fab-heat').classList.toggle('off', !S.map.heatmap);
addEventListener('resize', () => { map && map.resize(); layoutFloating(); });
addEventListener('keydown', e => { if (e.key === 'Escape') closeModals(true); });

(async () => {
  document.documentElement.dataset.theme = themeName(); initMap();
  applyTheme(); renderTiers(); renderWhen(); tick(); setInterval(tick, 15000);
  $('from').value = S.origin.name; $('to').value = S.destination.name; drawPins(); layoutFloating(); fitView();
  try { const cfg = await (await fetch('/api/config')).json(); PRESETS = cfg.presets || []; if (cfg.ai === false) { S.advanced.ai = 'local'; toast('No Anthropic key on this server: using the free route scorer'); } } catch {}
  loadSpend(); setInterval(loadStatus, 30000); loadNews(); setInterval(loadNews, 300000);
  await loadCells().catch(() => {});
  if (isViewer()) return viewer(Q.get('watch'));
  plan();
})();

Object.assign(window, { openStop, fetchPhoto, swapPhoto, stopTracking, openSettings, closeModals, applySettings, resetSettings,
  refreshNow, liveRefresh, setPlace, plan, swapOD, toggleHeat, locateMe, fitView, walkWithMe, closePopup, loadDemoTrip,
  refreshAll, scanArea, drivePreview, toggleBubbles });
}
