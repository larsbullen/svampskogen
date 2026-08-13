'use strict';
/* God-mode overview: password-gated page showing ALL reported finds (every
   device) on the whole-kommun map, with the habitat overlay + weather forecast
   and a live GPS dot. Client-side gate = soft (the publishable key reads finds
   via the API regardless); real per-user security comes with login. */

const SB_URL = 'https://frivhxpuntqwzrkxdmrp.supabase.co/rest/v1';
const SB_KEY = 'sb_publishable_Fjd4npCW40Bz8-nAhhYkYQ_NH6THI_9';
const SB_HEAD = { apikey: SB_KEY, Authorization: 'Bearer ' + SB_KEY };
const PW_HASH = 'b5683d660cbac6aff6af03b6b23b7b4efc13adf8f7921560ddd8715c088e81ee';   // sha-256 of the password

const SPECIES = {
  'Kantarell': '#E0A100', 'Karljohan': '#8A5A2B', 'Trattkantarell': '#E07B39',
  'Svart trumpetsvamp': '#4A4A4A', 'Annan / okänd': '#7A8A72',
};
const speciesColor = sv => SPECIES[sv] || '#7A8A72';
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const todayISO = () => new Date().toISOString().slice(0, 10);

async function sha256hex(s) {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s));
  return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('');
}

const gate = document.getElementById('gate');
document.getElementById('gateForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const ok = (await sha256hex(document.getElementById('pw').value)) === PW_HASH;
  if (ok) { sessionStorage.setItem('overblick.ok', '1'); unlock(); }
  else { document.getElementById('pwErr').hidden = false; document.getElementById('pw').value = ''; }
});

let map, suitGrid = null, forecastDays = null, suitOverlay = null, fcIndex = 0;
if (sessionStorage.getItem('overblick.ok') === '1') unlock();

function unlock() {
  gate.style.display = 'none';
  document.getElementById('app').hidden = false;
  map = L.map('map', { zoomControl: true }).setView([63.5, 13.2], 8);
  map.zoomControl.setPosition('topright');
  L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}',
    { maxZoom: 19, attribution: 'Tiles &copy; Esri' }).addTo(map);
  startGps();
  loadOverlay();
  loadFinds();
}

/* ---------- Habitat overlay × forecast (ported from the main app) ---------- */
function suitColor(score) {
  const stops = [[25, 80, 120, 70, 0.12], [50, 150, 160, 60, 0.30], [70, 225, 166, 62, 0.46], [85, 224, 123, 57, 0.56], [100, 200, 80, 40, 0.64]];
  if (score < stops[0][0]) return [0, 0, 0, 0];
  for (let k = 0; k < stops.length - 1; k++) { const a = stops[k], b = stops[k + 1]; if (score <= b[0]) { const t = (score - a[0]) / (b[0] - a[0]); return [1, 2, 3, 4].map(m => a[m] + (b[m] - a[m]) * t); } }
  const last = stops[stops.length - 1]; return [last[1], last[2], last[3], last[4]];
}
function renderSuit(grid, mult) {
  const { nrows, ncols } = grid.meta, n = nrows * ncols;
  const cv = document.createElement('canvas'); cv.width = ncols; cv.height = nrows;
  const ctx = cv.getContext('2d'), img = ctx.createImageData(ncols, nrows);
  for (let idx = 0; idx < n; idx++) {
    const s = grid.scores[idx], p = idx * 4;
    if (s < 0) { img.data[p + 3] = 0; continue; }
    const c = suitColor(s * mult);
    img.data[p] = Math.round(c[0]); img.data[p + 1] = Math.round(c[1]); img.data[p + 2] = Math.round(c[2]); img.data[p + 3] = Math.round(c[3] * 255);
  }
  ctx.putImageData(img, 0, 0); return cv.toDataURL();
}
function nearestDay(dateStr) { const t = Date.parse(dateStr + 'T00:00:00Z'); let best = 0, bd = Infinity; forecastDays.forEach((d, i) => { const dd = Math.abs(Date.parse(d.date + 'T00:00:00Z') - t); if (dd < bd) { bd = dd; best = i; } }); return best; }
function fmtDate(s) { const mn = ['jan', 'feb', 'mar', 'apr', 'maj', 'jun', 'jul', 'aug', 'sep', 'okt', 'nov', 'dec']; const [, m, d] = s.split('-'); return (+d) + ' ' + mn[+m - 1]; }
function fruitingLevel(f) { return f >= 0.65 ? 3 : f >= 0.4 ? 2 : f >= 0.18 ? 1 : 0; }
function fcStepColor(f) { return ['#C9CBBE', '#A7C58A', '#E4B24A', '#D97A3C'][fruitingLevel(f)]; }
function buildTrackGradient() {
  const n = forecastDays.length; if (n < 2) return fcStepColor(forecastDays[0].fruiting);
  const st = []; forecastDays.forEach((d, i) => { const c = fcStepColor(d.fruiting); const a = Math.max(0, (i - 0.5) / (n - 1)) * 100, b = Math.min(1, (i + 0.5) / (n - 1)) * 100; st.push(`${c} ${a.toFixed(2)}%`, `${c} ${b.toFixed(2)}%`); });
  return `linear-gradient(90deg, ${st.join(', ')})`;
}
let rafId = null, pendingMult = null;
function scheduleRender(mult) { pendingMult = mult; if (rafId) return; rafId = requestAnimationFrame(() => { rafId = null; suitOverlay.setUrl(renderSuit(suitGrid, pendingMult)); }); }
function curMult() { return forecastDays ? forecastDays[fcIndex].fruiting : 1; }

async function loadOverlay() {
  try {
    const [grid, fc, kom] = await Promise.all([
      fetch('data/suitability.json').then(r => r.json()),
      fetch('data/forecast.json').then(r => r.json()).catch(() => null),
      fetch('data/kommun.geojson').then(r => r.json()).catch(() => null),
    ]);
    suitGrid = grid; const m = grid.meta; const bounds = [[m.south, m.west], [m.north, m.east]];
    if (kom) L.geoJSON(kom, { interactive: false, pane: 'overlayPane', style: { color: '#2A4634', weight: 1.5, opacity: 0.5, fill: false, dashArray: '5 4' } }).addTo(map);
    if (fc && fc.days && fc.days.length) { forecastDays = fc.days; const ti = nearestDay(todayISO()); forecastDays = forecastDays.slice(Math.max(0, ti - 14)); fcIndex = nearestDay(todayISO()); }
    suitOverlay = L.imageOverlay(renderSuit(grid, curMult()), bounds, { opacity: 1, interactive: false, className: 'suit-overlay', pane: 'overlayPane' }).addTo(map);
    map.fitBounds(bounds, { padding: [8, 8] });
    if (forecastDays) initForecast();
  } catch { /* overlay optional */ }
}
function initForecast() {
  document.getElementById('forecast').hidden = false;
  const slider = document.getElementById('fcSlider');
  slider.min = 0; slider.max = forecastDays.length - 1; slider.value = fcIndex;
  slider.style.setProperty('--fc-track', buildTrackGradient());
  document.getElementById('fcStart').textContent = fmtDate(forecastDays[0].date);
  document.getElementById('fcEnd').textContent = fmtDate(forecastDays[forecastDays.length - 1].date);
  slider.addEventListener('input', () => setDay(+slider.value));
  document.getElementById('fcNow').addEventListener('click', () => { const i = nearestDay(todayISO()); slider.value = i; setDay(i); });
  setDay(fcIndex);
}
function setDay(i) {
  fcIndex = i; const d = forecastDays[i];
  if (suitOverlay) scheduleRender(d.fruiting);
  const chip = document.getElementById('fcChip');
  chip.textContent = d.verdict + (d.reason ? ' · ' + d.reason : '');
  chip.className = 'fc-chip lvl' + fruitingLevel(d.fruiting);
  document.getElementById('fcDate').textContent = fmtDate(d.date) + (d.forecast ? ' · prognos' : '');
}

/* ---------- Finds ---------- */
function starIcon(color) {
  return L.divIcon({ className: 'my-find-icon', html: `<div class="star" style="--c:${color}">★</div>`, iconSize: [28, 28], iconAnchor: [14, 14] });
}
async function loadFinds() {
  // Known GBIF finds as faint grey reference dots.
  fetch('data/occurrences.geojson').then(r => r.json()).then(occ => {
    (occ.features || []).forEach(f => { const [lo, la] = f.geometry.coordinates; L.circleMarker([la, lo], { radius: 3, weight: 0, fillColor: '#9a9a9a', fillOpacity: 0.4, interactive: false, pane: 'overlayPane' }).addTo(map); });
  }).catch(() => {});
  // All reported finds (every device) as stars.
  let rows = [];
  try { rows = await fetch(SB_URL + '/finds?select=*&order=created.desc', { headers: SB_HEAD }).then(r => r.json()); }
  catch { document.getElementById('stat').textContent = 'Kunde inte hämta fynd.'; return; }
  rows = rows.filter(r => r.device_id !== 'setup-test' && r.lat && r.lon);
  const devices = new Set(), bySpecies = {};
  rows.forEach(r => {
    devices.add(r.device_id); bySpecies[r.species] = (bySpecies[r.species] || 0) + 1;
    L.marker([r.lat, r.lon], { icon: starIcon(speciesColor(r.species)) }).bindPopup(
      `<div class="pop"><div class="pop-sv">${esc(r.species) || 'Fynd'}</div>` +
      `<div class="pop-meta">${esc(r.date) || ''}${r.count ? ' · ' + esc(r.count) : ''}</div>` +
      (r.notes ? `<div class="pop-meta">${esc(r.notes)}</div>` : '') +
      `<div class="pop-meta" style="color:var(--muted)">enhet ${esc(String(r.device_id).slice(4, 12))}…</div></div>`
    ).addTo(map);
  });
  const spec = Object.entries(bySpecies).sort((a, b) => b[1] - a[1]).map(([k, v]) => `${esc(k)}: <b>${v}</b>`).join(' · ');
  document.getElementById('stat').innerHTML =
    `<b>${rows.length}</b> rapporterade fynd från <b>${devices.size}</b> enhet(er)` + (spec ? `<br>${spec}` : '') +
    `<br><span style="color:var(--muted)">★ rapporterade · grå = kända (GBIF)</span>`;
}

/* ---------- Live GPS dot ---------- */
let gpsMarker = null;
function startGps() {
  if (!navigator.geolocation) return;
  navigator.geolocation.watchPosition(pos => {
    const ll = [pos.coords.latitude, pos.coords.longitude];
    if (!gpsMarker) gpsMarker = L.circleMarker(ll, { radius: 7, color: '#fff', weight: 2, fillColor: '#2b7fff', fillOpacity: 1, pane: 'markerPane' }).addTo(map);
    else gpsMarker.setLatLng(ll);
  }, () => {}, { enableHighAccuracy: true, maximumAge: 10000, timeout: 15000 });
}
