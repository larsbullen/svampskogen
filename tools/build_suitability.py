#!/usr/bin/env python3
"""Build a habitat-suitability grid for the Åre area.

v0 is a TRANSPARENT TERRAIN HEURISTIC — not a trained species-distribution
model. It scores each grid cell from:
  * elevation band  — favour the forested belt (~450–700 m), fall off toward
    valley bottoms and hard off above the Åre tree line (~800–900 m),
  * slope           — favour gentle-to-moderate slopes over flats and cliffs,
  * proximity to known finds (GBIF/Artportalen) — a light nudge, not a driver.

Elevation comes from EU-DEM (25 m) via the public opentopodata API. Output is
data/suitability.json, a coarse grid the web app renders as a heatmap overlay.
Swap this file's contents for real SDM output when the model exists; the app
only needs the same {meta, scores[]} shape.
"""
import json, math, time, urllib.request, urllib.parse, os

# Focused on the forested valley around Åre (skip the far high fjäll / open water edges).
NORTH, SOUTH = 63.52, 63.28
WEST,  EAST  = 12.80, 13.40
NROWS, NCOLS = 44, 60

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT  = os.path.join(HERE, "data", "suitability.json")
FINDS = os.path.join(HERE, "data", "occurrences.geojson")
API  = "https://api.opentopodata.org/v1/eudem25m"

dlat = (NORTH - SOUTH) / NROWS
dlon = (EAST - WEST) / NCOLS

def cell_center(i, j):
    return (NORTH - (i + 0.5) * dlat, WEST + (j + 0.5) * dlon)  # row 0 = north

def fetch_elevations(points):
    """points: list of (lat, lon). Returns list of elevation (float|None)."""
    out = []
    for k in range(0, len(points), 100):
        batch = points[k:k + 100]
        locs = "|".join(f"{lat:.6f},{lon:.6f}" for lat, lon in batch)
        url = API + "?" + urllib.parse.urlencode({"locations": locs})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    data = json.load(r)
                if data.get("status") == "OK":
                    out.extend(res.get("elevation") for res in data["results"])
                    break
                raise RuntimeError(data.get("error", "non-OK"))
            except Exception as e:
                if attempt == 3:
                    print(f"  batch {k}: giving up ({e}); masking")
                    out.extend([None] * len(batch))
                else:
                    time.sleep(2 * (attempt + 1))
        print(f"  fetched {min(k + 100, len(points))}/{len(points)}")
        time.sleep(1.1)  # respect ~1 req/sec public limit
    return out

def load_finds():
    try:
        fc = json.load(open(FINDS))
        return [(f["geometry"]["coordinates"][1], f["geometry"]["coordinates"][0])
                for f in fc.get("features", [])]
    except Exception:
        return []

def e_score(e):
    if e is None: return None
    if e < 380:  return 0.15
    if e < 450:  return 0.15 + (e - 380) / 70 * 0.85
    if e <= 700: return 1.0
    if e <= 850: return 1.0 - (e - 700) / 150 * 0.6
    if e <= 950: return 0.4 - (e - 850) / 100 * 0.4
    return 0.0

def slope_score(s):
    if s < 2:   return 0.70
    if s <= 12: return 1.0
    if s <= 25: return 1.0 - (s - 12) / 13 * 0.6
    return 0.30

def main():
    pts = [cell_center(i, j) for i in range(NROWS) for j in range(NCOLS)]
    print(f"Grid {NROWS}x{NCOLS} = {len(pts)} cells; fetching EU-DEM elevation…")
    elev_flat = fetch_elevations(pts)
    E = [[elev_flat[i * NCOLS + j] for j in range(NCOLS)] for i in range(NROWS)]

    finds = load_finds()
    print(f"{len(finds)} known finds for proximity term")

    # metres per degree, mid-latitude
    mlat = 111320.0 * dlat
    def mlon_at(lat): return 111320.0 * math.cos(math.radians(lat)) * dlon

    scores = []
    for i in range(NROWS):
        for j in range(NCOLS):
            e = E[i][j]
            if e is None:
                scores.append(-1); continue
            # slope from neighbours (central difference where possible)
            iN, iS = max(0, i - 1), min(NROWS - 1, i + 1)
            jW, jE = max(0, j - 1), min(NCOLS - 1, j + 1)
            eN, eS, eW, eE_ = E[iN][j], E[iS][j], E[i][jW], E[i][jE]
            lat, lon = cell_center(i, j)
            mlon = mlon_at(lat)
            dz_dy = ((eN if eN is not None else e) - (eS if eS is not None else e)) / (mlat * (iS - iN) or 1)
            dz_dx = ((eE_ if eE_ is not None else e) - (eW if eW is not None else e)) / (mlon * (jE - jW) or 1)
            slope_deg = math.degrees(math.atan(math.hypot(dz_dx, dz_dy)))

            es = e_score(e)
            ss = slope_score(slope_deg)

            # proximity to known finds (Gaussian, sigma ~1.6 km)
            boost = 0.0
            for (flat, flon) in finds:
                dx = (flon - lon) * mlon_at(lat) / dlon
                dy = (flat - lat) * mlat / dlat
                dkm = math.hypot(dx, dy) / 1000.0
                boost += math.exp(-(dkm / 1.6) ** 2)
            prox = 1.0 - math.exp(-boost)  # 0..1

            final = es * ss * (0.75 + 0.25 * prox)
            scores.append(round(final * 100))

    out = {
        "meta": {
            "north": NORTH, "south": SOUTH, "west": WEST, "east": EAST,
            "nrows": NROWS, "ncols": NCOLS,
            "model": "v0-terrain-heuristic",
            "built": "2026-08-11",
            "elevation": "EU-DEM 25 m (opentopodata)",
            "note": ("Transparent terrain heuristic: elevation band + slope + "
                     "proximity to known finds. Placeholder for the trained SDM."),
        },
        "scores": scores,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(out, open(OUT, "w"), separators=(",", ":"))
    valid = [s for s in scores if s >= 0]
    print(f"Wrote {OUT}: {len(valid)} scored cells, "
          f"{len(scores) - len(valid)} masked; max={max(valid) if valid else 0}")

if __name__ == "__main__":
    main()
