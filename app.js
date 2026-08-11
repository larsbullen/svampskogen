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

const topo = L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png', {
  maxZoom: 17,
  attribution: '© <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA) · © OpenStreetMap',
});
const osm = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 19, attribution: '© OpenStreetMap',
});
topo.addTo(map);
const layersCtl = L.control.layers({ 'Terräng': topo, 'Karta': osm }, {}, { position: 'topright' }).addTo(map);

/* ---------- Layers ---------- */
const gbifLayer = L.layerGroup().addTo(map);
const mineLayer = L.layerGroup().addTo(map);

function speciesColor(sv) {
  return (SPECIES[sv] && SPECIES[sv].color) || '#7A8A72';
}

/* ---------- Habitat suitability overlay (v0 terrain heuristic) ---------- */
fetch('data/suitability.json')
  .then(r => r.json())
  .then(grid => {
    const layer = buildSuitabilityOverlay(grid);
    layer.addTo(map);
    layersCtl.addOverlay(layer, 'Habitatmodell v0');
    const leg = document.getElementById('suitLegend');
    if (leg) leg.hidden = false;
  })
  .catch(() => {});

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

function buildSuitabilityOverlay(grid) {
  const { nrows, ncols, north, south, west, east } = grid.meta;
  const cv = document.createElement('canvas');
  cv.width = ncols; cv.height = nrows;
  const ctx = cv.getContext('2d');
  const img = ctx.createImageData(ncols, nrows);
  for (let i = 0; i < nrows; i++) {
    for (let j = 0; j < ncols; j++) {
      const s = grid.scores[i * ncols + j];
      const p = (i * ncols + j) * 4;
      if (s < 0) { img.data[p + 3] = 0; continue; }
      const c = suitColor(s);
      img.data[p] = Math.round(c[0]);
      img.data[p + 1] = Math.round(c[1]);
      img.data[p + 2] = Math.round(c[2]);
      img.data[p + 3] = Math.round(c[3] * 255);
    }
  }
  ctx.putImageData(img, 0, 0);
  return L.imageOverlay(cv.toDataURL(), [[south, west], [north, east]],
    { opacity: 1, interactive: false, className: 'suit-overlay', pane: 'overlayPane' });
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
