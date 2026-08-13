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

// Åre kommun outline.
fetch('data/kommun.geojson').then(r => r.json()).then(k => {
  L.geoJSON(k, { interactive: false, pane: 'overlayPane',
    style: { color: '#2A4634', weight: 1.5, opacity: 0.5, fill: false, dashArray: '5 4' } }).addTo(map);
}).catch(() => {});

function speciesColor(sv) {
  return (SPECIES[sv] && SPECIES[sv].color) || '#7A8A72';
}

/* ---------- Habitat overlay × weather forecast ---------- */
// The map shows habitat(where) × fruiting(when). Habitat is the static SDM
// grid; fruiting is a daily 0..1 index (SMHI rain+temp). The date picker scales
// every cell's score by fruiting(date) so dry/cold days dim the whole map.
let suitGrid = null, forecastDays = null, suitOverlay = null, fcIndex = 0;
let strictMode = localStorage.getItem('svampfinder.strict') === '1';   // show only the best spots

Promise.all([
  fetch('data/suitability.json').then(r => r.json()),
  fetch('data/forecast.json').then(r => r.json()).catch(() => null),
]).then(([grid, fc]) => {
  suitGrid = grid;
  const m = grid.meta;
  const bounds = [[m.south, m.west], [m.north, m.east]];
  if (fc && fc.days && fc.days.length) {
    forecastDays = fc.days;
    // Window to ~2 weeks back + the 10-day outlook — drop the dead off-season.
    const ti = nearestDay(todayISO());
    forecastDays = forecastDays.slice(Math.max(0, ti - 14));
  }
  const mult0 = forecastDays ? (forecastDays[fcIndex = nearestDay(todayISO())].fruiting) : 1;
  suitOverlay = L.imageOverlay(renderSuit(grid, mult0), bounds,
    { opacity: 1, interactive: false, className: 'suit-overlay', pane: 'overlayPane' }).addTo(map);
  layersCtl.addOverlay(suitOverlay, 'Habitatmodell v1');
  map.fitBounds(bounds, { padding: [8, 8] });   // frame the whole kommun
  const leg = document.getElementById('suitLegend'); if (leg) leg.hidden = false;
  if (forecastDays) initForecast();
}).catch(() => {});

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
  const cut = strictMode ? 55 : 25;    // strict: only the best spots light up, punchier
  const cv = document.createElement('canvas');
  cv.width = ncols; cv.height = nrows;
  const ctx = cv.getContext('2d');
  const img = ctx.createImageData(ncols, nrows);
  for (let idx = 0; idx < n; idx++) {
    const s = grid.scores[idx];
    const p = idx * 4;
    if (s < 0) { img.data[p + 3] = 0; continue; }
    const eff = s * mult;
    if (eff < cut) { img.data[p + 3] = 0; continue; }
    const c = suitColor(eff);
    const a = strictMode ? Math.min(0.8, c[3] * 1.5) : c[3];
    img.data[p] = Math.round(c[0]);
    img.data[p + 1] = Math.round(c[1]);
    img.data[p + 2] = Math.round(c[2]);
    img.data[p + 3] = Math.round(a * 255);
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

// Colour per fruiting level (dry → peak); used to paint each slider step.
function fcStepColor(f) { return ['#C9CBBE', '#A7C58A', '#E4B24A', '#D97A3C'][fruitingLevel(f)]; }
function buildTrackGradient() {
  const n = forecastDays.length;
  if (n < 2) return fcStepColor(forecastDays[0].fruiting);
  const stops = [];
  forecastDays.forEach((d, i) => {                 // each day centred on its thumb position
    const c = fcStepColor(d.fruiting);
    const a = Math.max(0, (i - 0.5) / (n - 1)) * 100;
    const b = Math.min(1, (i + 0.5) / (n - 1)) * 100;
    stops.push(`${c} ${a.toFixed(2)}%`, `${c} ${b.toFixed(2)}%`);
  });
  return `linear-gradient(90deg, ${stops.join(', ')})`;
}

let rafId = null, pendingMult = null;
function scheduleRender(mult) {
  pendingMult = mult;
  if (rafId) return;
  rafId = requestAnimationFrame(() => { rafId = null; suitOverlay.setUrl(renderSuit(suitGrid, pendingMult)); });
}
function curMult() { return forecastDays ? forecastDays[fcIndex].fruiting : 1; }

function initForecast() {
  document.getElementById('forecast').hidden = false;
  const slider = document.getElementById('fcSlider');
  slider.min = 0; slider.max = forecastDays.length - 1; slider.value = fcIndex;
  slider.style.setProperty('--fc-track', buildTrackGradient());
  document.getElementById('fcStart').textContent = fmtDate(forecastDays[0].date);
  document.getElementById('fcEnd').textContent = fmtDate(forecastDays[forecastDays.length - 1].date);
  slider.addEventListener('input', () => setDay(+slider.value));
  document.getElementById('fcNow').addEventListener('click', () => {
    const i = nearestDay(todayISO());
    slider.value = i; setDay(i);
  });
  const strictCb = document.getElementById('fcStrict');
  strictCb.checked = strictMode;
  strictCb.addEventListener('change', () => {
    strictMode = strictCb.checked;
    localStorage.setItem('svampfinder.strict', strictMode ? '1' : '0');
    scheduleRender(curMult());
  });
  setDay(fcIndex);
}

function setDay(i) {
  fcIndex = i;
  const d = forecastDays[i];
  scheduleRender(d.fruiting);
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
      const color = speciesColor(p.sv);
      // Search-area radius = the record's coordinate uncertainty (real metres).
      // Fade the coarse ones (>1000 m) so a vague record doesn't dominate.
      if (p.uncertainty_m && p.uncertainty_m > 0) {
        const big = p.uncertainty_m > 1000;
        L.circle([lat, lon], {
          radius: p.uncertainty_m, interactive: false, color,
          weight: 1, opacity: big ? 0.15 : 0.35,
          fillColor: color, fillOpacity: big ? 0.02 : 0.08,
          dashArray: big ? '3 5' : null,
        }).addTo(gbifLayer);
      }
      L.circleMarker([lat, lon], {
        radius: 6, weight: 1.5, color: '#ffffff', fillColor: color,
        fillOpacity: 0.9, opacity: 0.9,
      }).bindPopup(gbifPopup(p)).addTo(gbifLayer);
    });
    gbifCount = (fc.features || []).length;
    document.getElementById('cGbif').textContent = gbifCount;
  })
  .catch(() => showToast('Kunde inte läsa kända fynd.'));

function gbifPopup(p) {
  const unc = p.uncertainty_m ? `sökområde ±${p.uncertainty_m} m` : 'okänd noggrannhet';
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

/* ---------- Cloud sync (Supabase) ----------
   Finds are stored per-device in localStorage (the map only shows THIS device's
   finds). Each find is also pushed to a shared database so the model can train
   on everyone's finds. Publishable key = safe to ship in the browser. */
const SB_URL = 'https://frivhxpuntqwzrkxdmrp.supabase.co/rest/v1';
const SB_KEY = 'sb_publishable_Fjd4npCW40Bz8-nAhhYkYQ_NH6THI_9';
const SB_HEAD = { apikey: SB_KEY, Authorization: 'Bearer ' + SB_KEY };

function deviceId() {
  let d = localStorage.getItem('svampfinder.device');
  if (!d) {
    d = 'dev_' + ((self.crypto && crypto.randomUUID)
      ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2));
    localStorage.setItem('svampfinder.device', d);
  }
  return d;
}
function cloudPush(f) {
  const [lon, lat] = f.geometry.coordinates, p = f.properties;
  const row = { id: p.id, device_id: deviceId(), species: p.sv, lat, lon,
    date: p.date || null, count: p.count || null, notes: p.notes || null };
  return fetch(SB_URL + '/finds', {
    method: 'POST',
    headers: { ...SB_HEAD, 'Content-Type': 'application/json', Prefer: 'resolution=merge-duplicates' },
    body: JSON.stringify(row),
  }).then(r => r.ok).catch(() => false);
}
function markSynced(id) {
  const arr = loadFinds(); const t = arr.find(x => x.properties.id === id);
  if (t) { t.properties.synced = true; saveFinds(arr); }
}
function retryUnsynced() {   // push any finds saved while offline
  loadFinds().filter(f => !f.properties.synced).forEach(f =>
    cloudPush(f).then(ok => { if (ok) markSynced(f.properties.id); }));
}

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
    fetch(SB_URL + '/finds?id=eq.' + encodeURIComponent(id), { method: 'DELETE', headers: SB_HEAD }).catch(() => {});
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

fab.addEventListener('click', () => { setMenu(false); resetPending(); openSheet(); });
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
  showToast('Peka på platsen i kartan.');
  map._pickBtn = btn;
}
function exitPickMode() {
  pickMode = false;
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
      synced: false,
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
  cloudPush(feature).then(ok => { if (ok) markSynced(feature.properties.id); });
  showToast(`${sv} sparad. Tack — det här blir träningsdata!`);
});

/* ---------- Live GPS location ---------- */
let gpsMarker = null, gpsLatLng = null;
function startGps() {
  if (!navigator.geolocation) return;
  navigator.geolocation.watchPosition(pos => {
    gpsLatLng = [pos.coords.latitude, pos.coords.longitude];
    if (!gpsMarker) gpsMarker = L.circleMarker(gpsLatLng, { radius: 7, color: '#fff', weight: 2, fillColor: '#2b7fff', fillOpacity: 1, pane: 'markerPane' }).addTo(map);
    else gpsMarker.setLatLng(gpsLatLng);
  }, () => {}, { enableHighAccuracy: true, maximumAge: 10000, timeout: 15000 });
}
// The locate button re-centres on the live dot (or fetches a one-off fix).
document.getElementById('btnLocate').addEventListener('click', (e) => {
  const b = e.currentTarget;
  if (gpsLatLng) { map.setView(gpsLatLng, 14); return; }
  b.classList.add('active');
  navigator.geolocation.getCurrentPosition(
    pos => { b.classList.remove('active'); gpsLatLng = [pos.coords.latitude, pos.coords.longitude]; map.setView(gpsLatLng, 14); startGps(); },
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

/* ---------- Menu panel (hamburger) ---------- */
const menuBtn = document.getElementById('menuBtn');
function setMenu(open) {
  document.body.classList.toggle('menu-open', open);
  menuBtn.setAttribute('aria-expanded', String(open));
}
menuBtn.addEventListener('click', () => setMenu(true));
document.getElementById('panelToggle').addEventListener('click', () => setMenu(false));
// Open by default on wider screens; collapsed to the hamburger on mobile.
setMenu(window.matchMedia('(min-width: 760px)').matches);

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
retryUnsynced();
startGps();

/* ---------- Service worker ---------- */
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => navigator.serviceWorker.register('sw.js').catch(() => {}));
}
