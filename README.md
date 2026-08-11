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

## Habitat overlay (v1 SDM)

`data/suitability.json` is a coarse grid scoring each cell's mushroom-habitat
potential, rendered as a heatmap overlay. **v1 is a presence-background logistic
species-distribution model** (pure stdlib) trained on the known finds vs. a
random background, over stacked open-data predictors:

- **elevation + slope** — EU-DEM 25 m (`tools/dump_elevation.py` → `data/layers/elevation.json`)
- **soil moisture (proxy)** — distance-to-flow-channel from Skogsstyrelsen
  Flödesackumulation; the real SLU DTW raster is auth-gated
  (`data/layers/soilmoisture.json`)
- **forest species + mask** — Naturvårdsverket NMD 2018 land cover; non-forest
  cells are masked out (`data/layers/forest.json`)

Pipeline: build the three layers, then train:

```sh
python3 tools/dump_elevation.py      # elevation + slope
# soil + forest layers are sampled from web services (see tools/ notes)
python3 tools/build_model.py         # → data/suitability.json
```

**Caveats:** few known finds (n≈9 unique cells in-grid) so the fit is
experimental and AUC is optimistic; the soil layer is a proxy, not calibrated
DTW; tree height/volume were unavailable (Skogsstyrelsen services now need
Geodatasamverkan credentials). Next steps: widen the grid to capture all finds,
add target-group background, add real DTW + stand height. `tools/build_suitability.py`
keeps the older v0 terrain heuristic for reference.

## Data & licenses

Known finds: GBIF / Artportalen (CC0). Elevation: EU-DEM via opentopodata.
Land cover: NMD, Naturvårdsverket (CC0). Wetness proxy: Skogsstyrelsen. Basemap:
OpenTopoMap (CC-BY-SA) / OpenStreetMap. The app maps *likelihood of habitat* —
it never identifies species or judges edibility.
