'use strict';
/* God-mode overview: password-gated page showing ALL reported finds (every
   device) from Supabase. Client-side gate = soft (the publishable key can read
   finds via the API regardless); real per-user security comes with login. */

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
let map;
if (sessionStorage.getItem('overblick.ok') === '1') unlock();

function unlock() {
  gate.style.display = 'none';
  document.getElementById('app').hidden = false;
  map = L.map('map', { zoomControl: true }).setView([63.5, 13.2], 8);
  map.zoomControl.setPosition('topright');
  L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}',
    { maxZoom: 19, attribution: 'Tiles &copy; Esri' }).addTo(map);
  loadAll();
}

async function loadAll() {
  // Known GBIF finds as faint grey reference dots.
  fetch('data/occurrences.geojson').then(r => r.json()).then(occ => {
    (occ.features || []).forEach(f => {
      const [lo, la] = f.geometry.coordinates;
      L.circleMarker([la, lo], { radius: 3, weight: 0, fillColor: '#9a9a9a', fillOpacity: 0.4, interactive: false }).addTo(map);
    });
  }).catch(() => {});

  // All reported finds (every device).
  let rows = [];
  try {
    rows = await fetch(SB_URL + '/finds?select=*&order=created.desc', { headers: SB_HEAD }).then(r => r.json());
  } catch { document.getElementById('stat').textContent = 'Kunde inte hämta fynd.'; return; }
  rows = rows.filter(r => r.device_id !== 'setup-test' && r.lat && r.lon);

  const devices = new Set(), bySpecies = {};
  const pts = [];
  rows.forEach(r => {
    devices.add(r.device_id);
    bySpecies[r.species] = (bySpecies[r.species] || 0) + 1;
    pts.push([r.lat, r.lon]);
    L.circleMarker([r.lat, r.lon], {
      radius: 6, weight: 1.5, color: '#fff', fillColor: speciesColor(r.species), fillOpacity: 0.95,
    }).bindPopup(
      `<div class="pop"><div class="pop-sv">${esc(r.species) || 'Fynd'}</div>` +
      `<div class="pop-meta">${esc(r.date) || ''}${r.count ? ' · ' + esc(r.count) : ''}</div>` +
      (r.notes ? `<div class="pop-meta">${esc(r.notes)}</div>` : '') +
      `<div class="pop-meta" style="color:var(--muted)">enhet ${esc(String(r.device_id).slice(4, 12))}…</div></div>`
    ).addTo(map);
  });

  const spec = Object.entries(bySpecies).sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `${esc(k)}: <b>${v}</b>`).join(' · ');
  document.getElementById('stat').innerHTML =
    `<b>${rows.length}</b> rapporterade fynd från <b>${devices.size}</b> enhet(er)` +
    (spec ? `<br>${spec}` : '') +
    `<br><span style="color:var(--muted)">grå prickar = kända fynd (GBIF)</span>`;
  if (pts.length) map.fitBounds(pts, { padding: [40, 40], maxZoom: 12 });
}
