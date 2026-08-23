/* Morgonsol — tent-ground map for a hiking route in Vålådalen.
 *
 * Three things share the screen and they mean different things:
 *   coloured ground  modelled quality (dry, level, even, a little raised)
 *   violet dashed    legal camping/access bans — deliberately OFF the warm
 *                    quality ramp, so 'best ground' can never be misread as
 *                    'forbidden'; the dash carries it for colour-blind eyes too
 *   coloured pins    the best single spot inside each good patch, coloured by
 *                    CHARACTER (skog / glest / kalfjäll) rather than by rank —
 *                    all 150 score 0.93-0.98, so ranking them 1..150 implied a
 *                    precision that isn't there. Rank stays in the popup.
 */
'use strict';

const BUILD = 41;   // bump on every deploy; busts the 10-min GitHub Pages cache
const DATA = 'data/morgonsol/';
const BAND_COLOR = { 1: '#f2d9a0', 2: '#e8a24a', 3: '#cf5b1c' };
const BAND_NAME = { 1: 'Bra', 2: 'Mycket bra', 3: 'Topp' };

// Pins are classified by CHARACTER, not by a rank number. Across the 150 sites
// the score spread is 0.93-0.98 — they are all top-tier ground, so numbering
// them 1..150 implied a precision the model doesn't have. What actually varies,
// and actually changes the decision, is shelter: 20 wooded sites at a median
// 636 m against 121 open ones at 901 m. Colour carries that. The numeric rank
// still lives in the popup as a tiebreaker.
const SHELTER = {
  skog:     { color: '#2f7d4f', label: 'Skog',              hint: 'Lä, ved, skymd — varmast' },
  glest:    { color: '#7d9c3f', label: 'Glest / skogsbryn', hint: 'Delvis lä, viss utsikt' },
  kalfjall: { color: '#2b8f96', label: 'Kalfjäll',          hint: 'Öppet, blåsigt, utsikt, mindre mygg' },
  okand:    { color: '#8a8a8a', label: 'Okänt',             hint: 'Trädtäcke okänt här' },
};
const CAP_RADIUS = { liten: 9, medel: 11, stor: 14 };
const CAP_LABEL = { liten: 'plats för 1–2 tält', medel: 'plats för flera tält',
                    stor: 'stor öppen yta' };

const map = L.map('map', {
  zoomControl: true,
  attributionControl: true,
  preferCanvas: true,          // thousands of polygons — SVG would crawl on a phone
}).setView([63.06, 12.83], 11);

const canvas = L.canvas({ padding: 0.3 });

const base = {
  'Terräng': L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}',
    { maxZoom: 19, attribution: 'Tiles &copy; Esri' }),
  'Satellit': L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    { maxZoom: 19, attribution: 'Tiles &copy; Esri' }),
  'OSM': L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    { maxZoom: 19, attribution: '&copy; OpenStreetMap' }),
};
base['Terräng'].addTo(map);
L.control.layers(base, null, { position: 'bottomleft' }).addTo(map);

// ------------------------------------------------------------------ layers
const layers = {
  areas1: L.layerGroup(),
  areas2: L.layerGroup(),
  areas3: L.layerGroup(),
  sites: L.layerGroup(),
  ban: L.layerGroup(),
  wetland: L.layerGroup(),
  contours: L.layerGroup(),
  route: L.layerGroup(),
  huts: L.layerGroup(),
};
const LAYER_ORDER = [
  ['contours', 'Höjdkurvor (20 m)', '#9c8161', true],
  ['wetland', 'Myr / våtmark', '#6f8fa6', false],
  ['areas1', 'Bra mark', BAND_COLOR[1], true],
  ['areas2', 'Mycket bra mark', BAND_COLOR[2], true],
  ['areas3', 'Topp-mark', BAND_COLOR[3], true],
  ['ban', 'Tält-/beträdnadsförbud', '#7b2fbe', true],
  ['route', 'Leden', '#2f4f8f', true],
  ['huts', 'Stugor', '#4c3b2a', true],
  ['sites', 'Tältplatser', '#2b8f96', true],
];
const counts = {};
let allSites = [];
let meta = null;

const $ = (id) => document.getElementById(id);
const jget = (f) => fetch(`${DATA}${f}?v=${BUILD}`)
  .then(r => (r.ok ? r.json() : null)).catch(() => null);

function fmtHour(h) {
  if (h === null || h === undefined || h >= 24) return '–';
  const hh = Math.floor(h), mm = Math.round((h - hh) * 60);
  return String(hh).padStart(2, '0') + ':' + String(mm).padStart(2, '0');
}

// ------------------------------------------------------------------- boot
(async function boot() {
  const [areas, sites, route, ban, wetland, huts, reserve, m, contours, weather] =
    await Promise.all([
      jget('areas.geojson'), jget('sites.geojson'), jget('route.geojson'),
      jget('zones.geojson'), jget('wetland.geojson'), jget('huts.geojson'),
      jget('reserve.geojson'), jget('meta.json'), jget('contours.geojson'),
      jget('weather.json'),
    ]);
  meta = m;
  if (weather) renderWeather(weather);

  if (contours) setupContours(contours);

  if (reserve) {
    L.geoJSON(reserve, {
      renderer: canvas,
      style: { color: '#7a6a52', weight: 1.5, opacity: .7, fill: false, dashArray: '6 5' },
      interactive: false,
    }).addTo(map);
  }

  if (wetland) {
    L.geoJSON(wetland, {
      renderer: canvas, interactive: false,
      style: { color: '#6f8fa6', weight: 0, fillColor: '#6f8fa6', fillOpacity: .30 },
    }).addTo(layers.wetland);
    counts.wetland = (wetland.features || []).length;
  }

  if (areas) {
    counts.areas1 = counts.areas2 = counts.areas3 = 0;
    L.geoJSON(areas, {
      renderer: canvas,
      style: (f) => {
        const b = f.properties.band;
        return { color: BAND_COLOR[b], weight: b === 3 ? 1 : 0, opacity: .9,
                 fillColor: BAND_COLOR[b], fillOpacity: b === 3 ? .62 : b === 2 ? .45 : .28 };
      },
      onEachFeature: (f, lyr) => {
        const b = f.properties.band;
        counts['areas' + b]++;
        lyr.addTo(layers['areas' + b]);
        lyr.bindPopup(`<div class="pop"><h3>${BAND_NAME[b]} tältmark</h3>
          <div class="why">${(f.properties.area_m2 / 10000).toFixed(1)} ha sammanhängande</div></div>`);
      },
    });
  }

  // Legal bans: only the restriction polygons, never the permissive zones.
  if (ban) {
    const restr = (ban.features || []).filter(f => (f.properties || {})._kind === 'restriction');
    counts.ban = restr.length;
    L.geoJSON({ type: 'FeatureCollection', features: restr }, {
      renderer: canvas,
      style: { color: '#7b2fbe', weight: 2.5, dashArray: '7 4',
               fillColor: '#7b2fbe', fillOpacity: .32 },
      onEachFeature: (f, lyr) => {
        const p = f.properties || {};
        const rule = p['slå_läger_övernatta'] || p['Slå_läger_Övernatta'] || '';
        const acc = p['beträda'] || '';
        lyr.bindPopup(`<div class="pop"><h3>⛔ ${p._name || 'Förbudsområde'}</h3>
          ${rule ? `<div class="why"><b>Övernattning:</b> ${rule}</div>` : ''}
          ${acc ? `<div class="why"><b>Beträda:</b> ${acc}</div>` : ''}</div>`);
      },
    }).addTo(layers.ban);
  }

  if (route) {
    const lines = (route.features || []).filter(f => f.geometry.type === 'LineString');
    L.geoJSON({ type: 'FeatureCollection', features: lines }, {
      renderer: canvas,
      // Planned loop solid; alternative trails dashed, so a bail-out option is
      // never mistaken for the route you meant to walk.
      style: (f) => ((f.properties || {}).kind === 'segment'
        ? { color: '#2f4f8f', weight: 2.5, opacity: .85, dashArray: '9 6' }
        : { color: '#2f4f8f', weight: 3.5, opacity: .95 }),
      onEachFeature: (f, lyr) => {
        const p = f.properties || {};
        if (p.kind === 'segment') {
          lyr.bindPopup(`<div class="pop"><h3>↔ ${p.name}</h3>
            <div class="why">Alternativ led, ${p.length_km} km — inte en del av
            planerade rutten. Med i modellen ifall planen ändras.</div></div>`);
        }
      },
    }).addTo(layers.route);
    const main = lines.find(f => (f.properties || {}).kind !== 'segment');
    const props = (main || {}).properties || {};
    $('routeSub').textContent =
      `${props.length_km ?? '?'} km · ${props.ele_min}–${props.ele_max} m`;
    counts.route = lines.length;
  }

  if (huts) {
    counts.huts = (huts.features || []).length;
    L.geoJSON(huts, {
      pointToLayer: (f, ll) => L.circleMarker(ll, {
        renderer: canvas, radius: 5, color: '#fff', weight: 2,
        fillColor: '#4c3b2a', fillOpacity: 1,
      }),
      onEachFeature: (f, lyr) => {
        const p = f.properties || {};
        lyr.bindPopup(`<div class="pop"><h3>🛖 ${p.name || 'Stuga'}</h3>
          <div class="why">${p.operator || ''}</div></div>`);
      },
    }).addTo(layers.huts);
  }

  if (sites) {
    allSites = (sites.features || []);
    counts.sites = allSites.length;
    renderSites(800);
    renderSiteList(800);
  }

  LAYER_ORDER.forEach(([key, , , on]) => { if (on) layers[key].addTo(map); });
  renderLegend();
  renderAbout();

  try {
    const bounds = L.geoJSON(route).getBounds();
    if (bounds.isValid()) map.fitBounds(bounds, { padding: [24, 24] });
  } catch (e) { /* keep the default view */ }
})();

// ----------------------------------------------------------------- weather
// The scores are structural — where water collects given terrain and soil, true
// in any weather. This is the other half: whether the ground is currently wetter
// or drier than it usually is here at this time of year, ranked against every
// year the nearest station has recorded.
function renderWeather(w) {
  const el = $('wx');
  if (!el) return;
  const pct = w.api_percentile;
  const cls = pct === null || pct === undefined ? 'norm'
    : pct >= 70 ? 'wet' : pct <= 30 ? 'dry' : 'norm';

  const days = (w.forecast || []).filter(f => f.available).map((f) => {
    const d = new Date(f.date + 'T12:00:00');
    const name = d.toLocaleDateString('sv-SE', { weekday: 'short' });
    const rain = f.precip_mm || 0;
    return `<div class="wx-day${rain >= 1 ? ' rain' : ''}">
      <div class="d">${name}</div>
      <div class="t">${Math.round(f.t_max)}°</div>
      <div class="r">${rain >= 0.1 ? rain.toFixed(1) + ' mm' : '–'}</div>
    </div>`;
  }).join('');

  const r = w.rain_mm || {};
  el.hidden = false;
  el.innerHTML = `
    <div class="wx-head">
      <span class="wx-chip ${cls}">${w.wetness_band}</span>
      <span class="wx-obs">obs t.o.m. ${w.observed_to}</span>
    </div>
    <p class="wx-advice">${w.advice || ''}</p>
    ${days ? `<div class="wx-days">${days}</div>` : ''}
    <p class="wx-src">Regn ${r.last_7d ?? '?'} mm senaste veckan,
      ${r.last_30d ?? '?'} mm senaste månaden — blötare än ${pct}% av samma
      period ${w.climatology_years} vid ${w.station_precip ? w.station_precip.name : '?'}
      (${w.station_precip ? w.station_precip.km : '?'} km). SMHI öppna data.</p>`;
}

// ---------------------------------------------------------------- contours
// Zoom-aware on purpose: 1004 lines at 20 m spacing is a legible map at z13 and
// a brown smear at z10, so the minor lines only appear once they can be read,
// and the index lines thin out rather than disappearing.
let contourMinor = null;
let contourIndex = null;
const contourLabels = L.layerGroup();
let contourIndexFeatures = [];

function setupContours(gj) {
  const feats = gj.features || [];
  const split = (wantIndex) => ({
    type: 'FeatureCollection',
    features: feats.filter(f => !!f.properties.index === wantIndex),
  });

  contourMinor = L.geoJSON(split(false), {
    renderer: canvas, interactive: false,
    style: { color: '#9c8161', weight: 0.5, opacity: 0.22 },
  });
  contourIndex = L.geoJSON(split(true), {
    renderer: canvas, interactive: false,
    style: { color: '#a08a6b', weight: 0.7, opacity: 0.26 },
  });
  contourIndexFeatures = feats.filter(f => f.properties.index);

  contourIndex.addTo(layers.contours);
  contourMinor.addTo(layers.contours);
  contourLabels.addTo(layers.contours);
  counts.contours = feats.length;

  map.on('zoomend moveend', refreshContours);
  refreshContours();
}

function refreshContours() {
  if (!contourMinor) return;
  const z = map.getZoom();

  // Minor lines are noise below z12.
  if (z >= 12) {
    if (!layers.contours.hasLayer(contourMinor)) contourMinor.addTo(layers.contours);
    contourMinor.setStyle({ weight: z >= 14 ? 0.6 : 0.5, opacity: z >= 13 ? 0.30 : 0.20 });
  } else if (layers.contours.hasLayer(contourMinor)) {
    layers.contours.removeLayer(contourMinor);
  }
  if (contourIndex) {
    contourIndex.setStyle({ weight: z >= 13 ? 0.85 : 0.7, opacity: z >= 11 ? 0.30 : 0.22 });
  }

  // Labels only where they fit, only in view, and capped so a pan never drops
  // a hundred divIcons on the map at once.
  contourLabels.clearLayers();
  if (z < 13) return;
  const bounds = map.getBounds();
  let placed = 0;
  for (const f of contourIndexFeatures) {
    if (placed >= 40) break;
    const cs = f.geometry.coordinates;
    const mid = cs[Math.floor(cs.length / 2)];
    const ll = L.latLng(mid[1], mid[0]);
    if (!bounds.contains(ll)) continue;
    L.marker(ll, {
      interactive: false,
      icon: L.divIcon({ className: '', html: `<span class="ctr-lbl">${f.properties.ele}</span>`,
                        iconSize: [30, 12], iconAnchor: [15, 6] }),
    }).addTo(contourLabels);
    placed++;
  }
}

// ------------------------------------------------------------------- sites
function pinIcon(p) {
  const sh = SHELTER[p.shelter] || SHELTER.okand;
  const r = CAP_RADIUS[p.capacity] || 11;
  const size = r * 2;
  return L.divIcon({
    className: '', iconSize: [size, size], iconAnchor: [r, r],
    html: `<div class="pin" title="${sh.label}"
      style="width:${size}px;height:${size}px;background:${sh.color}"></div>`,
  });
}

// A site sitting on an added connecting trail rather than the planned loop.
function isAlt(p) {
  return (p.route || '').includes('–') ? 1 : 0;
}

function siteBand(p) {
  return p.score >= 0.82 ? 3 : 2;
}

function renderSites(maxOff) {
  layers.sites.clearLayers();
  allSites
    .filter(f => (f.properties.off_route_m ?? 0) <= maxOff)
    .forEach((f) => {
      const p = f.properties;
      const [lon, lat] = f.geometry.coordinates;
      L.marker([lat, lon], { icon: pinIcon(p) })
        .bindPopup(sitePopup(p, lat, lon))
        .addTo(layers.sites);
    });
}

function sitePopup(p, lat, lon) {
  const row = (k, v) => (v === null || v === undefined ? '' : `<dt>${k}</dt><dd>${v}</dd>`);
  return `<div class="pop">
    <h3><span class="pop-dot" style="background:${(SHELTER[p.shelter] || SHELTER.okand).color}"></span>
      ${(SHELTER[p.shelter] || SHELTER.okand).label}</h3>
    <p class="why">${(SHELTER[p.shelter] || SHELTER.okand).hint} · ${CAP_LABEL[p.capacity] || ''}</p>
    <dl>
      ${row('Rangordning', '#' + p.rank + ' av 150')}
      ${row('Markpoäng', p.score)}
      ${row('Trädtäcke', p.tree_frac === null || p.tree_frac === undefined ? null
             : Math.round(p.tree_frac * 100) + '%')}
      ${row('Led', p.route || null)}
      ${row('Vid km', p.route_km !== null ? p.route_km.toFixed(1) : null)}
      ${row('Från leden', p.off_route_m !== null ? p.off_route_m + ' m' : null)}
      ${row('Höjd', p.elev_m + ' m')}
      ${row('Lutning', p.slope_deg + '°')}
      ${row('Yta', (p.patch_m2 / 10000).toFixed(1) + ' ha')}
      ${row('Vatten', p.water_m !== null ? p.water_m + ' m' : null)}
      ${row('Myr', p.wetland_m !== null ? p.wetland_m + ' m' : null)}
      ${row('Närmsta stuga', p.hut_m !== null ? (p.hut_m / 1000).toFixed(1) + ' km' : null)}
      ${row('Sol på tältet', fmtHour(p.first_light))}
    </dl>
    <p class="why">${p.why || ''}</p>
    <p class="why"><a href="https://www.google.com/maps?q=${lat},${lon}" target="_blank" rel="noopener">${lat.toFixed(5)}, ${lon.toFixed(5)}</a></p>
  </div>`;
}

function renderSiteList(maxOff) {
  const ul = $('siteList');
  ul.innerHTML = '';
  allSites
    .filter(f => (f.properties.off_route_m ?? 0) <= maxOff)
    .slice()
    .sort((a, b) => (isAlt(a.properties) - isAlt(b.properties))
      || ((a.properties.route_km ?? 0) - (b.properties.route_km ?? 0)))
    .forEach((f) => {
      const p = f.properties;
      const [lon, lat] = f.geometry.coordinates;
      const li = document.createElement('li');
      li.className = 'site b' + siteBand(p);
      const sh = SHELTER[p.shelter] || SHELTER.okand;
      li.innerHTML = `<span class="rank" style="background:${sh.color};color:#fff"
        title="${sh.label}">${sh.label.charAt(0)}</span>
        <span class="meta">
          <span class="hd">${sh.label} · km ${p.route_km !== null ? p.route_km.toFixed(1) : '?'} · ${p.elev_m} m</span>
          <span class="sm">${p.off_route_m} m från leden${p.water_m !== null ? ' · vatten ' + p.water_m + ' m' : ''} · sol ${fmtHour(p.first_light)}</span>
          ${isAlt(p) ? `<span class="sm alt">↔ ${p.route}</span>` : ''}
        </span>`;
      li.onclick = () => {
        map.setView([lat, lon], 15);
        L.popup().setLatLng([lat, lon]).setContent(sitePopup(p, lat, lon)).openOn(map);
        closePanel();
      };
      ul.appendChild(li);
    });
}

// ------------------------------------------------------------------ legend
function renderLegend() {
  const ul = $('legend');
  ul.innerHTML = '';
  LAYER_ORDER.forEach(([key, label, color, on]) => {
    const li = document.createElement('li');
    li.className = on ? '' : 'off';
    li.innerHTML = `<span class="sw" style="background:${color}"></span>
      <span class="lbl">${label}</span><span class="n">${counts[key] ?? 0}</span>`;
    li.onclick = () => {
      if (map.hasLayer(layers[key])) { map.removeLayer(layers[key]); li.classList.add('off'); }
      else { layers[key].addTo(map); li.classList.remove('off'); }
    };
    ul.appendChild(li);
  });

  // Pin colours need their own key: they encode character, not quality.
  const counts_by = {};
  allSites.forEach((f) => {
    const k = f.properties.shelter || 'okand';
    counts_by[k] = (counts_by[k] || 0) + 1;
  });
  const keyRows = Object.keys(SHELTER)
    .filter(k => counts_by[k])
    .map(k => `<li><span class="sw" style="background:${SHELTER[k].color};border-radius:50%;
      width:14px;height:14px"></span><span class="lbl">${SHELTER[k].label}
      <span class="pin-hint">${SHELTER[k].hint}</span></span>
      <span class="n">${counts_by[k]}</span></li>`).join('');
  const keyEl = $('pinKey');
  if (keyEl) {
    keyEl.innerHTML = `<div class="pin-key-head">Tältplatserna färgas efter
      <b>karaktär</b>, inte rang — alla 150 ligger på likvärdigt bra mark
      (poäng 0,93–0,98). Storleken visar hur stor ytan är. Rangordningen finns
      kvar i detaljrutan.</div><ul class="legend">${keyRows}</ul>`;
  }

  if (meta) {
    const km2 = (n) => (n * meta.resolution_m * meta.resolution_m / 1e6).toFixed(1);
    $('mapNote').innerHTML =
      `Modellen har gått igenom en korridor på <b>${km2(meta.counts.corridor_cells)} km²</b> runt leden
       och underkänt allt som är brantare än ${meta.hard_masks.slope_max_deg}°, ligger i myr eller vatten,
       eller ligger i ett förbudsområde. Kvar blev <b>${km2(meta.counts.passing_cells)} km²</b> tänkbar mark.`;
  }
}

function renderAbout() {
  if (!meta) return;
  const p = meta.provenance || {};
  const sm = p.sun_meta || null;
  const hhmm = (x) => (x === null || x === undefined ? '–'
    : String(Math.floor(x)).padStart(2, '0') + ':' + String(Math.round((x % 1) * 60)).padStart(2, '0'));
  const w = meta.weights || {};
  const pct = (x) => Math.round(x * 100) + '%';
  $('aboutBody').innerHTML = `
    <p class="note" style="border:0;padding-top:0;margin-top:0">
      Varje ruta på ${meta.resolution_m} m poängsätts på sex saker, som ett
      <b>geometriskt medelvärde</b> — en blöt men plan yta ska inte kunna
      kompensera sig till en bra poäng.
    </p>
    <ul class="legend">
      <li><span class="lbl">Torrt</span><span class="n">${pct(w.dry)}</span></li>
      <li><span class="lbl">Plant</span><span class="n">${pct(w.level)}</span></li>
      <li><span class="lbl">Läge (kalluft rinner undan)</span><span class="n">${pct(w.position)}</span></li>
      <li><span class="lbl">Jämnt (inte blockigt)</span><span class="n">${pct(w.smooth)}</span></li>
      <li><span class="lbl">Morgonsol</span><span class="n">${pct(w.sun)}</span></li>
      <li><span class="lbl">Vatten på lagom avstånd</span><span class="n">${pct(w.water)}</span></li>
    </ul>
    <div class="warn">
      ${/1 m/.test(p.dem || '')
        ? `<b>Höjddata: 1 m laserskannad markmodell.</b> Den är rensad från
           träd och byggnader, så lutning och jämnhet beskriver verklig mark —
           även under trädgränsen.`
        : `<b>Höjddatan är en ytmodell (30 m).</b> Den ser trädkronor som mark,
           så under trädgränsen blir lutning och jämnhet osäkra.`}
      Modellen hittar ändå bara <i>kandidater</i> — de sista 50 metrarna avgör
      du på plats.
    </div>
    <p class="note"><b>Förbudsområden</b> kommer från Länsstyrelsens zonkarta
      (${meta.hard_masks.ban_zones}). Kontrollera alltid skyltning på plats.</p>
    ${sm ? `<p class="note"><b>Solen är räknad för ${sm.date}.</b>
      Soluppgång ${hhmm(sm.sunrise_flat)}, solnedgång ${hhmm(sm.sunset_flat)}
      (${sm.daylight_h} h dagsljus vid fri horisont). Bakom verklig terräng
      kommer solen typiskt först ${hhmm(sm.first_light_median)} — det är den
      skillnaden kartan räknar ut, kurva för kurva.</p>` : ''}
    <p class="note"><b>Höjdkurvor</b> var 20:e meter (grövre linje var 100:e),
      genererade ur samma höjdmodell som poängen — de stämmer alltså med
      terrängen kartan räknat på. Minorkurvorna tänds först vid inzoomning.</p>
    <p class="note">Version <b>${BUILD}</b> · uppdaterad ${meta.built_for_route ? '' : ''}${new Date().toISOString().slice(0,10)}</p>
    <p class="note"><b>Källor:</b> ${p.dem}; myr/vatten/leder/stugor från OpenStreetMap;
      zoner och reservatsgräns från Länsstyrelsen Jämtland; markfuktighet: ${p.soil_moisture}.</p>
  `;
}

// ------------------------------------------------------------------- chrome
function openPanel() { $('panel').hidden = false; $('menuBtn').setAttribute('aria-expanded', 'true'); }
function closePanel() { $('panel').hidden = true; $('menuBtn').setAttribute('aria-expanded', 'false'); }
$('menuBtn').onclick = () => ($('panel').hidden ? openPanel() : closePanel());
$('panelClose').onclick = closePanel;

const panes = { tabMap: 'paneMap', tabSites: 'paneSites', tabAbout: 'paneAbout' };
Object.keys(panes).forEach((tab) => {
  $(tab).onclick = () => {
    Object.entries(panes).forEach(([t, pane]) => {
      const on = t === tab;
      $(t).setAttribute('aria-selected', String(on));
      $(pane).hidden = !on;
    });
  };
});

$('fOff').oninput = (e) => {
  const v = +e.target.value;
  $('fOffVal').textContent = v;
  renderSites(v);
  renderSiteList(v);
};

// --------------------------------------------------------------------- GPS
// iOS Safari specifics this is built around:
//  - The permission prompt is far more reliable when the FIRST request comes
//    from a real user gesture. A watchPosition fired at page load often gets
//    ignored or dismissed, and then nothing ever happens.
//  - enableHighAccuracy:true can take 20 s+ outdoors and simply time out. Ask
//    for a coarse fix first so something appears, then upgrade to precise.
//  - Errors must be shown. The previous version passed an empty error handler,
//    so every failure was silent and looked like a dead button.
let gpsMarker = null;
let gpsCircle = null;
let gpsWatchId = null;

function toast(msg, ms) {
  let el = $('toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    el.className = 'toast';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, ms || 4200);
}

function showGps(pos, recentre) {
  const ll = [pos.coords.latitude, pos.coords.longitude];
  const acc = pos.coords.accuracy || 0;
  if (!gpsMarker) {
    gpsMarker = L.marker(ll, {
      icon: L.divIcon({ className: '', html: '<div class="gps-dot"></div>',
                        iconSize: [14, 14], iconAnchor: [7, 7] }),
      interactive: false, zIndexOffset: 1000,
    }).addTo(map);
    gpsCircle = L.circle(ll, { radius: acc, interactive: false, weight: 1,
                               color: '#1e73e8', opacity: .5, fillOpacity: .10 }).addTo(map);
  } else {
    gpsMarker.setLatLng(ll);
    if (gpsCircle) { gpsCircle.setLatLng(ll); gpsCircle.setRadius(acc); }
  }
  if (recentre) map.setView(ll, Math.max(map.getZoom(), 15));
}

function gpsError(err, context) {
  // err.code: 1 PERMISSION_DENIED, 2 POSITION_UNAVAILABLE, 3 TIMEOUT
  if (err.code === 1) {
    toast('Platsåtkomst nekad. iPhone: Inställningar ▸ Integritet ▸ Platstjänster ▸ '
        + 'Safari-webbplatser ▸ Fråga/Tillåt, och Inställningar ▸ Safari ▸ Plats.', 9000);
  } else if (err.code === 3) {
    toast('Hittade ingen position i tid — försöker igen med grövre precision.', 4000);
  } else {
    toast('Positionen är inte tillgänglig just nu (' + (err.message || 'okänt fel') + ').', 6000);
  }
  console.warn('[morgonsol] geolocation ' + context + ':', err.code, err.message);
}

function startWatch() {
  if (gpsWatchId !== null || !navigator.geolocation) return;
  gpsWatchId = navigator.geolocation.watchPosition(
    (p) => showGps(p, false),
    (e) => gpsError(e, 'watch'),
    { enableHighAccuracy: true, maximumAge: 15000, timeout: 30000 },
  );
}

function locate() {
  if (!navigator.geolocation) {
    toast('Den här webbläsaren har ingen platstjänst.');
    return;
  }
  if (!window.isSecureContext) {
    toast('Platstjänst kräver https — öppna sidan via https-adressen.', 7000);
    return;
  }
  if (gpsMarker) {                       // already have a fix: just recentre
    map.setView(gpsMarker.getLatLng(), Math.max(map.getZoom(), 15));
    startWatch();
    return;
  }
  toast('Söker position…', 3000);
  // Coarse first — fast, and enough to put you on the map.
  navigator.geolocation.getCurrentPosition(
    (p) => { showGps(p, true); startWatch(); },
    (e) => {
      if (e.code === 3 || e.code === 2) {
        // Retry once, coarser and more patient, before giving up.
        navigator.geolocation.getCurrentPosition(
          (p) => { showGps(p, true); startWatch(); },
          (e2) => gpsError(e2, 'retry'),
          { enableHighAccuracy: false, maximumAge: 600000, timeout: 30000 },
        );
      } else {
        gpsError(e, 'initial');
      }
    },
    { enableHighAccuracy: false, maximumAge: 60000, timeout: 12000 },
  );
}

$('btnLocate').onclick = locate;

// If permission was already granted on a previous visit, start passively — no
// gesture needed and no prompt. Where the Permissions API is missing (older
// iOS), we simply wait for the button, which is the reliable path anyway.
if (navigator.permissions && navigator.permissions.query) {
  navigator.permissions.query({ name: 'geolocation' })
    .then((st) => { if (st.state === 'granted') startWatch(); })
    .catch(() => {});
}

// GitHub Pages serves everything with max-age=600, which also applies to sw.js —
// so without updateViaCache the browser can sit on a stale worker for ten
// minutes and never notice a deploy. Force a fresh check, and reload once when
// a new worker takes over so the page isn't left half-updated.
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('sw.js', { updateViaCache: 'none' })
      .then((reg) => { reg.update().catch(() => {}); })
      .catch(() => {});
  });
  let reloaded = false;
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (reloaded) return;
    reloaded = true;
    window.location.reload();
  });
}
