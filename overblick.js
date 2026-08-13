'use strict';
/* God-mode overview: password-gated page showing ALL reported finds (every
   device) on the whole-kommun map, with the habitat overlay + weather forecast
   and a live GPS dot. Client-side gate = soft (the publishable key reads finds
   via the API regardless); real per-user security comes with login. */

const SB_URL = 'https://frivhxpuntqwzrkxdmrp.supabase.co/rest/v1';
const SB_KEY = 'sb_publishable_Fjd4npCW40Bz8-nAhhYkYQ_NH6THI_9';
const AUTH_URL = 'https://frivhxpuntqwzrkxdmrp.supabase.co/auth/v1';
let accessToken = null;
const authHead = () => ({ apikey: SB_KEY, Authorization: 'Bearer ' + accessToken });

const SPECIES = {
  'Kantarell': '#E0A100', 'Karljohan': '#8A5A2B', 'Trattkantarell': '#E07B39',
  'Svart trumpetsvamp': '#4A4A4A', 'Annan / okänd': '#7A8A72',
};
const speciesColor = sv => SPECIES[sv] || '#7A8A72';
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const todayISO = () => new Date().toISOString().slice(0, 10);

// ---- Supabase Auth (real security: reading finds requires a logged-in account) ----
async function login(email, password) {   // returns null on success, else an error message
  let res;
  try {
    res = await fetch(AUTH_URL + '/token?grant_type=password', {
      method: 'POST', headers: { apikey: SB_KEY, 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    });
  } catch { return 'Nätverksfel — försök igen.'; }
  if (res.ok) {
    const d = await res.json();
    accessToken = d.access_token;
    localStorage.setItem('overblick.token', accessToken);
    if (d.refresh_token) localStorage.setItem('overblick.refresh', d.refresh_token);
    return null;
  }
  let e = {}; try { e = await res.json(); } catch {}
  if (e.error_code === 'email_not_confirmed') return 'Kontot är inte bekräftat — bekräfta det i Supabase.';
  return e.msg || 'Fel e-post eller lösenord.';
}
async function refreshSession() {
  const rt = localStorage.getItem('overblick.refresh');
  if (!rt) return false;
  const res = await fetch(AUTH_URL + '/token?grant_type=refresh_token', {
    method: 'POST', headers: { apikey: SB_KEY, 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: rt }),
  });
  if (!res.ok) return false;
  const d = await res.json();
  accessToken = d.access_token;
  localStorage.setItem('overblick.token', accessToken);
  if (d.refresh_token) localStorage.setItem('overblick.refresh', d.refresh_token);
  return true;
}

const gate = document.getElementById('gate');
document.getElementById('gateForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = document.getElementById('pwBtn'); btn.disabled = true;
  const err = await login(document.getElementById('email').value.trim(), document.getElementById('pw').value);
  btn.disabled = false;
  if (!err) { unlock(); return; }
  const p = document.getElementById('pwErr'); p.textContent = err; p.hidden = false;
  document.getElementById('pw').value = '';
});

let map, suitGrid = null, forecastDays = null, suitOverlay = null, fcIndex = 0;
// Per-region fruiting series (Åre west, Krokom east); cellRegion[idx] = nearest
// region for that grid cell (built once). See app.js for the shared approach.
let fcRegions = null, cellRegion = null;
let strictMode = true;   // show only the best spots (default ON)
refreshSession().then(ok => { if (ok) unlock(); });   // resume a saved session

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
function cellLatLon(grid, idx) {
  const { nrows, ncols, north, south, west, east } = grid.meta;
  const row = Math.floor(idx / ncols), col = idx % ncols;
  return [north - (row + 0.5) * (north - south) / nrows, west + (col + 0.5) * (east - west) / ncols];
}
function buildCellRegion(grid, regions) {
  const { nrows, ncols } = grid.meta, n = nrows * ncols;
  const out = new Uint8Array(n);
  if (!regions.some(r => r.anchor)) return out;
  for (let idx = 0; idx < n; idx++) {
    const [lat, lon] = cellLatLon(grid, idx);
    let best = 0, bestD = Infinity;
    regions.forEach((r, ri) => { if (!r.anchor) return; const dLat = lat - r.anchor[0], dLon = (lon - r.anchor[1]) * Math.cos(lat * Math.PI / 180); const d = dLat * dLat + dLon * dLon; if (d < bestD) { bestD = d; best = ri; } });
    out[idx] = best;
  }
  return out;
}
function fruitAt(idx, i) {
  if (fcRegions && cellRegion) return fcRegions[cellRegion[idx]].days[i].fruiting;
  if (forecastDays) return forecastDays[i].fruiting;
  return 1;
}
function renderSuit(grid, i) {
  const { nrows, ncols } = grid.meta, n = nrows * ncols;
  const cut = strictMode ? 55 : 25;
  const cv = document.createElement('canvas'); cv.width = ncols; cv.height = nrows;
  const ctx = cv.getContext('2d'), img = ctx.createImageData(ncols, nrows);
  for (let idx = 0; idx < n; idx++) {
    const s = grid.scores[idx], p = idx * 4;
    if (s < 0) { img.data[p + 3] = 0; continue; }
    const eff = s * fruitAt(idx, i);
    if (eff < cut) { img.data[p + 3] = 0; continue; }
    const c = suitColor(eff);
    const a = strictMode ? Math.min(0.8, c[3] * 1.5) : c[3];
    img.data[p] = Math.round(c[0]); img.data[p + 1] = Math.round(c[1]); img.data[p + 2] = Math.round(c[2]); img.data[p + 3] = Math.round(a * 255);
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
let rafId = null, pendingIdx = 0;
function scheduleRender(i) { pendingIdx = i; if (rafId) return; rafId = requestAnimationFrame(() => { rafId = null; suitOverlay.setUrl(renderSuit(suitGrid, pendingIdx)); }); }

async function loadOverlay() {
  try {
    const [grid, fc, kom] = await Promise.all([
      fetch('data/suitability.json').then(r => r.json()),
      fetch('data/forecast.json').then(r => r.json()).catch(() => null),
      fetch('data/kommuner.geojson').then(r => r.json()).catch(() => null),
    ]);
    suitGrid = grid; const m = grid.meta; const bounds = [[m.south, m.west], [m.north, m.east]];
    if (kom) L.geoJSON(kom, { interactive: false, pane: 'overlayPane', style: { color: '#2A4634', weight: 2.5, opacity: 0.8, fill: false, dashArray: '6 5' } }).addTo(map);
    if (fc && fc.days && fc.days.length) {
      // Multi-region: each cell uses its nearest region anchor (Åre west,
      // Krokom east); fall back to the flat days[] as one anchorless region.
      const defName = (fc.meta && fc.meta.default_region) || 'are';
      const regs = fc.regions
        ? Object.keys(fc.regions).map(k => ({ anchor: fc.regions[k].anchor, days: fc.regions[k].days }))
        : [{ anchor: null, days: fc.days }];
      forecastDays = (fc.regions && fc.regions[defName]) ? fc.regions[defName].days : fc.days;
      const ti = nearestDay(todayISO());
      const from = Math.max(0, ti - 14);
      forecastDays = forecastDays.slice(from);
      fcRegions = regs.map(r => ({ anchor: r.anchor, days: r.days.slice(from) }));
      cellRegion = buildCellRegion(grid, fcRegions);
      fcIndex = nearestDay(todayISO());
    }
    suitOverlay = L.imageOverlay(renderSuit(grid, fcIndex), bounds, { opacity: 1, interactive: false, className: 'suit-overlay', pane: 'overlayPane' }).addTo(map);
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
  const strictCb = document.getElementById('fcStrict');
  strictCb.checked = strictMode;
  strictCb.addEventListener('change', () => { strictMode = strictCb.checked; scheduleRender(fcIndex); });
  setDay(fcIndex);
}
function setDay(i) {
  fcIndex = i; const d = forecastDays[i];
  if (suitOverlay) scheduleRender(i);
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
  try {
    const q = '/finds?select=*&order=created.desc';
    let res = await fetch(SB_URL + q, { headers: authHead() });
    if (res.status === 401 && await refreshSession()) res = await fetch(SB_URL + q, { headers: authHead() });
    if (!res.ok) throw 0;
    rows = await res.json();
  } catch { document.getElementById('stat').textContent = 'Kunde inte hämta fynd — logga in igen.'; return; }
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
  }, () => {}, { enableHighAccuracy: false, maximumAge: 60000, timeout: 20000 });
}
