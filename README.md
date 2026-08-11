# Svampskogen 🍄 (private)

Personal mushroom-habitat finder for the forests around **Åre**. A client-only
PWA: an offline-capable map of known finds plus your own reported finds, and a
habitat-suitability overlay.

> **Private by design.** This repo is private and the app ships `noindex` +
> `robots.txt` so it stays out of search engines and AI crawlers. If you deploy
> it, host it somewhere access-controlled — GitHub Pages is public.

## Run locally

```sh
cd svampskogen
python3 -m http.server 8137
# open http://127.0.0.1:8137
```

Geolocation and "install to home screen" need HTTPS in the field; `localhost`
counts as secure for development.

## What's here

- `index.html`, `styles.css`, `app.js` — the app (vanilla, no build step)
- `vendor/` — Leaflet, vendored so the shell works offline
- `data/occurrences.geojson` — known finds baked from GBIF / Artportalen (CC0)
- `data/suitability.json` — habitat-suitability grid (see below)
- `manifest.webmanifest`, `sw.js`, `icons/` — PWA plumbing
- `tools/build_suitability.py` — regenerates the suitability grid

## Habitat overlay

`data/suitability.json` is a coarse grid scoring each cell's mushroom-habitat
potential. **v0 is a transparent terrain heuristic** (elevation band, slope,
proximity to known finds) — a placeholder for the trained species-distribution
model described in the project build spec. Regenerate with:

```sh
python3 tools/build_suitability.py
```

## Data & licenses

Known finds: GBIF / Artportalen (CC0). Elevation: EU-DEM via opentopodata.
Basemap: OpenTopoMap (CC-BY-SA) / OpenStreetMap. The app maps *likelihood of
habitat* — it never identifies species or judges edibility.
