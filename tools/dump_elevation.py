#!/usr/bin/env python3
"""Dump raw elevation + slope on the shared grid to data/layers/elevation.json.

Same grid as build_suitability.py so all predictor layers align cell-for-cell.
The v1 model consumes these as raw predictors (v0 only kept derived scores).
"""
import json, math, time, urllib.request, urllib.parse, os

NORTH, SOUTH = 63.62, 63.18
WEST,  EAST  = 12.80, 13.45
NROWS, NCOLS = 76, 60
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT  = os.path.join(HERE, "data", "layers", "elevation.json")
API  = "https://api.opentopodata.org/v1/eudem25m"

dlat = (NORTH - SOUTH) / NROWS
dlon = (EAST - WEST) / NCOLS
def cell_center(i, j): return (NORTH - (i + 0.5) * dlat, WEST + (j + 0.5) * dlon)

def fetch(points):
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
                    out.extend(res.get("elevation") for res in data["results"]); break
                raise RuntimeError(data.get("error"))
            except Exception as e:
                if attempt == 3: out.extend([None] * len(batch))
                else: time.sleep(2 * (attempt + 1))
        print(f"  {min(k+100,len(points))}/{len(points)}")
        time.sleep(1.1)
    return out

def main():
    pts = [cell_center(i, j) for i in range(NROWS) for j in range(NCOLS)]
    ef = fetch(pts)
    E = [[ef[i*NCOLS+j] for j in range(NCOLS)] for i in range(NROWS)]
    mlat = 111320.0 * dlat
    elevation, slope = [], []
    for i in range(NROWS):
        for j in range(NCOLS):
            e = E[i][j]
            elevation.append(e)
            if e is None: slope.append(None); continue
            iN, iS = max(0, i-1), min(NROWS-1, i+1)
            jW, jE = max(0, j-1), min(NCOLS-1, j+1)
            lat, _ = cell_center(i, j)
            mlon = 111320.0 * math.cos(math.radians(lat)) * dlon
            eN = E[iN][j] if E[iN][j] is not None else e
            eS = E[iS][j] if E[iS][j] is not None else e
            eW = E[i][jW] if E[i][jW] is not None else e
            eE = E[i][jE] if E[i][jE] is not None else e
            dz_dy = (eN - eS) / (mlat * (iS - iN) or 1)
            dz_dx = (eE - eW) / (mlon * (jE - jW) or 1)
            slope.append(round(math.degrees(math.atan(math.hypot(dz_dx, dz_dy))), 2))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({"meta": {"north": NORTH, "south": SOUTH, "west": WEST, "east": EAST,
                        "nrows": NROWS, "ncols": NCOLS, "source": "EU-DEM 25 m (opentopodata)",
                        "units": {"elevation": "m", "slope": "degrees"}, "built": "2026-08-11"},
               "elevation": elevation, "slope": slope}, open(OUT, "w"), separators=(",", ":"))
    v = [x for x in elevation if x is not None]
    print(f"Wrote {OUT}: {len(v)} cells, elev {min(v):.0f}–{max(v):.0f} m")

if __name__ == "__main__":
    main()
