'use strict';

/* ------------------------------------------------------------------ *
 * Svampfinder — Åre pilot
 * Client-only PWA. Historical finds come from a baked GBIF/Artportalen
 * GeoJSON; the user's own finds live in localStorage and export as
 * GeoJSON (future training data for the habitat model).
 * ------------------------------------------------------------------ */

const ARE = [63.40, 13.08];
const STORE_KEY = 'svampfinder.finds.v1';

const SPECIES = {
  'Kantarell':          { color: '#E0A100', sci: 'Cantharellus cibarius' },
  'Karljohan':          { color: '#8A5A2B', sci: 'Boletus edulis' },
  'Trattkantarell':     { color: '#E07B39', sci: 'Craterellus tubaeformis' },
  'Svart trumpetsvamp': { color: '#4A4A4A', sci: 'Craterellus cornucopioides' },
  'Annan / okänd':      { color: '#7A8A72', sci: '' },
};

/* ---------- Map + basemaps ---------- */
const map = L.map('map', { zoomControl: true, attributionControl: true }).setView(ARE, 11);
map.zoomControl.setPosition('topright');

// Esri tiles are reliable and key-free (OpenTopoMap rate-limits too hard for this).
const esriTopo = L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}',
  { maxZoom: 19, attribution: 'Tiles &copy; Esri' });
const esriImg = L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
  { maxZoom: 19, attribution: 'Tiles &copy; Esri' });
const osm = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19, attribution: '&copy; OpenStreetMap',
});
esriTopo.addTo(map);
const layersCtl = L.control.layers(
  { 'Terräng': esriTopo, 'Satellit': esriImg, 'Karta': osm }, {},
  { position: 'topright' }).addTo(map);

/* ---------- Layers ---------- */
const gbifLayer = L.layerGroup().addTo(map);
const mineLayer = L.layerGroup().addTo(map);

function speciesColor(sv) {
  return (SPECIES[sv] && SPECIES[sv].color) || '#7A8A72';
}

/* ---------- Habitat overlay × weather forecast ---------- */
// The map shows habitat(where) × fruiting(when). Habitat is the static SDM
// grid; fruiting is a daily 0..1 index (SMHI rain+temp). The date picker scales
// every cell's score by fruiting(date) so dry/cold days dim the whole map.
let suitGrid = null, forecastDays = null, suitOverlay = null, fcIndex = 0;
let zonesLayer = null, openZone = null;

Promise.all([
  fetch('data/suitability.json').then(r => r.json()),
  fetch('data/forecast.json').then(r => r.json()).catch(() => null),
  fetch('data/zones.geojson').then(r => r.json()).catch(() => null),
]).then(([grid, fc, zones]) => {
  suitGrid = grid;
  const m = grid.meta;
  const bounds = [[m.south, m.west], [m.north, m.east]];
  if (fc && fc.days && fc.days.length) {
    forecastDays = fc.days;
    // Window to ~2 weeks back + the 10-day outlook — drop the dead off-season.
    const ti = nearestDay(todayISO());
    forecastDays = forecastDays.slice(Math.max(0, ti - 14));
  }
  fcIndex = forecastDays ? nearestDay(todayISO()) : 0;

  // Zones = tappable best-habitat areas, recoloured by the forecast (default view).
  if (zones && zones.features) {
    zonesLayer = L.geoJSON(zones, { style: zoneStyle, onEachFeature: onZone }).addTo(map);
    layersCtl.addOverlay(zonesLayer, 'Zoner');
  }
  // Heatmap kept as an optional layer.
  suitOverlay = L.imageOverlay(renderSuit(grid, curMult()), bounds,
    { opacity: 1, interactive: false, className: 'suit-overlay', pane: 'overlayPane' });
  layersCtl.addOverlay(suitOverlay, 'Värmekarta');
  map.on('overlayadd', (e) => { if (e.layer === suitOverlay) scheduleRender(curMult()); });

  const leg = document.getElementById('suitLegend'); if (leg) leg.hidden = false;
  if (forecastDays) initForecast();
}).catch(() => {});

/* ---------- Zones ---------- */
function curMult() { return forecastDays ? forecastDays[fcIndex].fruiting : 1; }

function zoneFill(base) {
  const eff = base * curMult();
  const c = suitColor(Math.max(eff, 25));                   // keep a hue even when faint
  const op = Math.max(0.12, Math.min(0.62, 0.12 + eff / 100 * 0.55));
  return { fill: `rgb(${Math.round(c[0])},${Math.round(c[1])},${Math.round(c[2])})`, op };
}
function zoneStyle(f) {
  const z = zoneFill(f.properties.base);
  return { fillColor: z.fill, fillOpacity: z.op, color: '#3a5a44', weight: 1, opacity: 0.5 };
}
function onZone(feature, layer) {
  layer.bindPopup(() => zoneCard(feature), { maxWidth: 260 });
  layer.on('popupopen', () => { openZone = { feature, layer }; });
  layer.on('popupclose', () => { openZone = null; });
}
function effVerdict(eff) {
  const d = forecastDays ? forecastDays[fcIndex] : null;
  const why = d && d.reason ? ' · ' + d.reason : '';
  if (eff >= 55) return 'Gå nu 🍄';
  if (eff >= 35) return 'Kan vara värt';
  if (eff >= 18) return 'Tveksamt' + why;
  return 'Vänta' + why;
}
function zoneCard(feature) {
  const p = feature.properties;
  const eff = Math.round(p.base * curMult());
  const d = forecastDays ? forecastDays[fcIndex] : null;
  const when = d ? fmtDate(d.date) + (d.forecast ? ' · prognos' : '') : '';
  const canopy = p.canopy != null ? Math.round(p.canopy * 100) + '% krontäckning' : '';
  const bits = [p.area_ha + ' ha', p.elev != null ? p.elev + ' m' : '', canopy, p.wetness]
    .filter(Boolean).join(' · ');
  return `<div class="pop">
    <div class="pop-sv">${escapeHtml(p.type)}</div>
    <div class="pop-meta">${escapeHtml(bits)}</div>
    <div class="pop-meta">Habitat: <b>${escapeHtml(p.base_label)}</b></div>
    <div class="zone-now">${when}: <b>${escapeHtml(effVerdict(eff))}</b></div>
  </div>`;
}

// Green → gold → orange ramp; below 25 fully transparent to reduce clutter.
function suitColor(score) {
  const stops = [
    [25,  80, 120,  70, 0.12],
    [50, 150, 160,  60, 0.30],
    [70, 225, 166,  62, 0.46],
    [85, 224, 123,  57, 0.56],
    [100, 200, 80,  40, 0.64],
  ];
  if (score < stops[0][0]) return [0, 0, 0, 0];
  for (let k = 0; k < stops.length - 1; k++) {
    const a = stops[k], b = stops[k + 1];
    if (score <= b[0]) {
      const t = (score - a[0]) / (b[0] - a[0]);
      return [1, 2, 3, 4].map(m => a[m] + (b[m] - a[m]) * t);
    }
  }
  const last = stops[stops.length - 1];
  return [last[1], last[2], last[3], last[4]];
}

// Render the grid to a data-URL, scaling every score by the fruiting multiplier.
function renderSuit(grid, mult) {
  const { nrows, ncols } = grid.meta;
  const n = nrows * ncols;
  const cv = document.createElement('canvas');
  cv.width = ncols; cv.height = nrows;
  const ctx = cv.getContext('2d');
  const img = ctx.createImageData(ncols, nrows);
  for (let idx = 0; idx < n; idx++) {
    const s = grid.scores[idx];
    const p = idx * 4;
    if (s < 0) { img.data[p + 3] = 0; continue; }
    const c = suitColor(s * mult);
    img.data[p] = Math.round(c[0]);
    img.data[p + 1] = Math.round(c[1]);
    img.data[p + 2] = Math.round(c[2]);
    img.data[p + 3] = Math.round(c[3] * 255);
  }
  ctx.putImageData(img, 0, 0);
  return cv.toDataURL();
}

/* ---------- Forecast date picker ---------- */
function nearestDay(dateStr) {
  const target = Date.parse(dateStr + 'T00:00:00Z');
  let best = 0, bestD = Infinity;
  forecastDays.forEach((d, i) => {
    const dd = Math.abs(Date.parse(d.date + 'T00:00:00Z') - target);
    if (dd < bestD) { bestD = dd; best = i; }
  });
  return best;
}
function fmtDate(s) {
  const mn = ['jan', 'feb', 'mar', 'apr', 'maj', 'jun', 'jul', 'aug', 'sep', 'okt', 'nov', 'dec'];
  const [, m, d] = s.split('-');
  return (+d) + ' ' + mn[+m - 1];
}
function fruitingLevel(f) { return f >= 0.65 ? 3 : f >= 0.4 ? 2 : f >= 0.18 ? 1 : 0; }

let rafId = null, pendingMult = null;
function scheduleRender(mult) {
  pendingMult = mult;
  if (rafId) return;
  rafId = requestAnimationFrame(() => { rafId = null; suitOverlay.setUrl(renderSuit(suitGrid, pendingMult)); });
}

function initForecast() {
  document.getElementById('forecast').hidden = false;
  const slider = document.getElementById('fcSlider');
  slider.min = 0; slider.max = forecastDays.length - 1; slider.value = fcIndex;
  document.getElementById('fcStart').textContent = fmtDate(forecastDays[0].date);
  document.getElementById('fcEnd').textContent = fmtDate(forecastDays[forecastDays.length - 1].date);
  slider.addEventListener('input', () => setDay(+slider.value));
  document.getElementById('fcNow').addEventListener('click', () => {
    const i = nearestDay(todayISO());
    slider.value = i; setDay(i);
  });
  setDay(fcIndex);
}

function setDay(i) {
  fcIndex = i;
  const d = forecastDays[i];
  if (zonesLayer && map.hasLayer(zonesLayer)) zonesLayer.setStyle(zoneStyle);
  if (openZone) openZone.layer.setPopupContent(zoneCard(openZone.feature));
  if (map.hasLayer(suitOverlay)) scheduleRender(d.fruiting);
  const chip = document.getElementById('fcChip');
  chip.textContent = d.verdict + (d.reason ? ' · ' + d.reason : '');
  chip.className = 'fc-chip lvl' + fruitingLevel(d.fruiting);
  document.getElementById('fcDate').textContent = fmtDate(d.date) + (d.forecast ? ' · prognos' : '');
}

/* ---------- Historical (GBIF) finds ---------- */
let gbifCount = 0;
fetch('data/occurrences.geojson')
  .then(r => r.json())
  .then(fc => {
    (fc.features || []).forEach(f => {
      const [lon, lat] = f.geometry.coordinates;
      const p = f.properties || {};
      L.circleMarker([lat, lon], {
        radius: 6, weight: 1.5, color: '#ffffff', fillColor: speciesColor(p.sv),
        fillOpacity: 0.9, opacity: 0.9,
      }).bindPopup(gbifPopup(p)).addTo(gbifLayer);
    });
    gbifCount = (fc.features || []).length;
    document.getElementById('cGbif').textContent = gbifCount;
  })
  .catch(() => showToast('Kunde inte läsa kända fynd.'));

function gbifPopup(p) {
  const unc = p.uncertainty_m ? `±${p.uncertainty_m} m` : 'okänd noggrannhet';
  const when = p.date ? String(p.date).slice(0, 10) : (p.year || '');
  const by = p.recordedBy ? `<div class="pop-meta">Rapporterat av ${escapeHtml(p.recordedBy)}</div>` : '';
  return `<div class="pop">
    <div class="pop-sv">${escapeHtml(p.sv || 'Svamp')}</div>
    <div class="pop-sci">${escapeHtml(p.sci || '')}</div>
    <div class="pop-meta">${when} · ${unc}</div>
    ${by}
    <span class="pop-tag gbif">Känt fynd · GBIF</span>
  </div>`;
}

/* ---------- My finds (localStorage) ---------- */
function loadFinds() {
  try { return JSON.parse(localStorage.getItem(STORE_KEY)) || []; }
  catch { return []; }
}
function saveFinds(list) { localStorage.setItem(STORE_KEY, JSON.stringify(list)); }

function renderMine() {
  mineLayer.clearLayers();
  const finds = loadFinds();
  finds.forEach(f => {
    const [lon, lat] = f.geometry.coordinates;
    const p = f.properties;
    const icon = L.divIcon({
      className: 'my-find-icon',
      html: `<div class="star" style="--c:${speciesColor(p.sv)}">★</div>`,
      iconSize: [28, 28], iconAnchor: [14, 14],
    });
    L.marker([lat, lon], { icon }).bindPopup(minePopup(p)).addTo(mineLayer);
  });
  document.getElementById('cMine').textContent = finds.length;
}

function minePopup(p) {
  const extra = [p.count, p.notes].filter(Boolean).map(escapeHtml).join(' · ');
  return `<div class="pop">
    <div class="pop-sv">${escapeHtml(p.sv)}</div>
    <div class="pop-sci">${escapeHtml(SPECIES[p.sv] ? SPECIES[p.sv].sci : (p.sci || ''))}</div>
    <div class="pop-meta">${escapeHtml(p.date || '')}${extra ? ' · ' + extra : ''}</div>
    <span class="pop-tag mine">Mitt fynd</span>
    <button class="pop-del" data-id="${p.id}">Ta bort fynd</button>
  </div>`;
}

// delete via event delegation on popups
map.on('popupopen', (e) => {
  const del = e.popup.getElement().querySelector('.pop-del');
  if (!del) return;
  del.addEventListener('click', () => {
    const id = del.getAttribute('data-id');
    saveFinds(loadFinds().filter(f => f.properties.id !== id));
    map.closePopup();
    renderMine();
    showToast('Fynd borttaget.');
  });
});

/* ---------- Legend ---------- */
(function buildLegend() {
  const ul = document.getElementById('legend');
  Object.entries(SPECIES).forEach(([sv, meta]) => {
    if (sv === 'Annan / okänd') return;
    const li = document.createElement('li');
    li.innerHTML = `<span class="dot" style="background:${meta.color}"></span>
      <span class="swede">${sv}</span><span class="sci">${meta.sci}</span>`;
    ul.appendChild(li);
  });
})();

/* ---------- Species dropdown ---------- */
(function fillSpecies() {
  const sel = document.getElementById('fSpecies');
  Object.keys(SPECIES).forEach(sv => {
    const o = document.createElement('option');
    o.value = sv; o.textContent = sv;
    sel.appendChild(o);
  });
})();

/* ---------- Report flow ---------- */
let pending = null;        // {lat, lon}
let pendingMarker = null;
let pickMode = false;

const sheet = document.getElementById('sheet');
const backdrop = document.getElementById('sheetBackdrop');
const fab = document.getElementById('btnReport');
const locReadout = document.getElementById('locReadout');
const btnSave = document.getElementById('btnSave');

function openSheet() {
  document.getElementById('fDate').value = todayISO();
  sheet.hidden = false; backdrop.hidden = false;
}
function closeSheet() {
  sheet.hidden = true; backdrop.hidden = true;
  exitPickMode();
}
function resetPending() {
  pending = null;
  if (pendingMarker) { map.removeLayer(pendingMarker); pendingMarker = null; }
  locReadout.textContent = 'Ingen plats vald ännu.';
  locReadout.classList.remove('set');
  document.querySelectorAll('.loc-btn').forEach(b => b.classList.remove('chosen'));
  btnSave.disabled = true;
}
function setPending(lat, lon, sourceBtn) {
  pending = { lat, lon };
  if (pendingMarker) map.removeLayer(pendingMarker);
  const icon = L.divIcon({ className: 'pending-icon', html: '<div class="pin"></div>', iconSize: [24, 24], iconAnchor: [12, 22] });
  pendingMarker = L.marker([lat, lon], { icon, draggable: true }).addTo(map);
  pendingMarker.on('dragend', () => {
    const ll = pendingMarker.getLatLng();
    pending = { lat: ll.lat, lon: ll.lng };
    updateReadout();
  });
  updateReadout();
  document.querySelectorAll('.loc-btn').forEach(b => b.classList.remove('chosen'));
  if (sourceBtn) sourceBtn.classList.add('chosen');
  btnSave.disabled = false;
}
function updateReadout() {
  if (!pending) return;
  locReadout.textContent = `${pending.lat.toFixed(5)}, ${pending.lon.toFixed(5)}  (dra nålen för att justera)`;
  locReadout.classList.add('set');
}

fab.addEventListener('click', () => { resetPending(); openSheet(); });
document.getElementById('btnCancel').addEventListener('click', closeSheet);
backdrop.addEventListener('click', closeSheet);

document.getElementById('btnLocHere').addEventListener('click', (e) => {
  const b = e.currentTarget;
  b.textContent = '… hämtar plats';
  navigator.geolocation.getCurrentPosition(
    pos => { b.textContent = '📍 Min plats'; setPending(pos.coords.latitude, pos.coords.longitude, b); map.setView([pos.coords.latitude, pos.coords.longitude], 14); },
    () => { b.textContent = '📍 Min plats'; showToast('Kunde inte hämta din plats.'); },
    { enableHighAccuracy: true, timeout: 10000 }
  );
});

document.getElementById('btnLocPick').addEventListener('click', (e) => {
  enterPickMode(e.currentTarget);
});
function enterPickMode(btn) {
  pickMode = true;
  sheet.hidden = true; backdrop.hidden = true;  // reveal map to tap
  fab.classList.add('picking');
  fab.firstElementChild.textContent = '👆';
  fab.childNodes[fab.childNodes.length - 1].textContent = ' Peka på kartan…';
  showToast('Peka på platsen i kartan.');
  map._pickBtn = btn;
}
function exitPickMode() {
  pickMode = false;
  fab.classList.remove('picking');
  fab.firstElementChild.textContent = '＋';
  fab.childNodes[fab.childNodes.length - 1].textContent = ' Rapportera fynd';
}
map.on('click', (e) => {
  if (!pickMode) return;
  setPending(e.latlng.lat, e.latlng.lng, map._pickBtn);
  exitPickMode();
  sheet.hidden = false; backdrop.hidden = false;
});

/* ---------- Save ---------- */
sheet.addEventListener('submit', (ev) => {
  ev.preventDefault();
  if (!pending) return;
  const sv = document.getElementById('fSpecies').value;
  const feature = {
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [round6(pending.lon), round6(pending.lat)] },
    properties: {
      id: uid(),
      source: 'user',
      sv,
      sci: SPECIES[sv] ? SPECIES[sv].sci : '',
      date: document.getElementById('fDate').value || todayISO(),
      count: document.getElementById('fCount').value || '',
      notes: document.getElementById('fNotes').value.trim(),
      created: todayISO(),
    },
  };
  const finds = loadFinds();
  finds.push(feature);
  saveFinds(finds);
  if (pendingMarker) { map.removeLayer(pendingMarker); pendingMarker = null; }
  closeSheet();
  document.getElementById('fNotes').value = '';
  document.getElementById('fCount').value = '';
  renderMine();
  showToast(`${sv} sparad. Tack — det här blir träningsdata!`);
});

/* ---------- Locate control ---------- */
let locateMarker = null;
document.getElementById('btnLocate').addEventListener('click', (e) => {
  const b = e.currentTarget; b.classList.add('active');
  navigator.geolocation.getCurrentPosition(
    pos => {
      b.classList.remove('active');
      const ll = [pos.coords.latitude, pos.coords.longitude];
      map.setView(ll, 14);
      if (locateMarker) map.removeLayer(locateMarker);
      locateMarker = L.circleMarker(ll, { radius: 7, color: '#fff', weight: 2, fillColor: '#2b7fff', fillOpacity: 1 }).addTo(map);
    },
    () => { b.classList.remove('active'); showToast('Kunde inte hämta din plats.'); },
    { enableHighAccuracy: true, timeout: 10000 }
  );
});

/* ---------- Export ---------- */
document.getElementById('btnExport').addEventListener('click', () => {
  const finds = loadFinds();
  if (!finds.length) { showToast('Inga egna fynd att exportera ännu.'); return; }
  const fc = { type: 'FeatureCollection', features: finds, meta: { app: 'svampfinder', exported: todayISO() } };
  const blob = new Blob([JSON.stringify(fc, null, 1)], { type: 'application/geo+json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'mina-svampfynd.geojson';
  a.click();
  URL.revokeObjectURL(a.href);
});

/* ---------- Panel collapse ---------- */
document.getElementById('panelToggle').addEventListener('click', (e) => {
  const panel = document.getElementById('panel');
  const collapsed = panel.classList.toggle('collapsed');
  e.currentTarget.textContent = collapsed ? '+' : '–';
  e.currentTarget.setAttribute('aria-expanded', String(!collapsed));
});

/* ---------- Helpers ---------- */
function todayISO() { return new Date().toISOString().slice(0, 10); }
function round6(n) { return Math.round(n * 1e6) / 1e6; }
function uid() { return 'f_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7); }
function escapeHtml(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

let toastT;
function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.hidden = false;
  clearTimeout(toastT);
  toastT = setTimeout(() => { t.hidden = true; }, 3200);
}

/* ---------- Init ---------- */
renderMine();

/* ---------- Service worker ---------- */
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => navigator.serviceWorker.register('sw.js').catch(() => {}));
}
