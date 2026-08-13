#!/usr/bin/env python3
"""Dump raw elevation + slope on the shared grid to data/layers/elevation.json.

Covers the whole Åre + Krokom kommun. Cells whose centre falls OUTSIDE both
municipality boundaries (data/kommuner.geojson) are set to None — this both masks
everything downstream to the real kommun outlines and avoids wasting elevation API
calls on Norway / neighbouring municipalities.

Same grid constants are the single source of truth; all other layers align to
elevation.json's meta.
"""
import json, math, time, urllib.request, urllib.parse, os
from datetime import date

# Union bbox of Åre + Krokom kommun admin boundaries, ~0.8 km cells.
NORTH, SOUTH = 64.41, 62.90
WEST,  EAST  = 11.97, 15.13
NROWS, NCOLS = 210, 196
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT  = os.path.join(HERE, "data", "layers", "elevation.json")
KOMMUN = os.path.join(HERE, "data", "kommuner.geojson")
API  = "https://api.opentopodata.org/v1/eudem25m"

dlat = (NORTH - SOUTH) / NROWS
dlon = (EAST - WEST) / NCOLS
def cell_center(i, j): return (NORTH - (i + 0.5) * dlat, WEST + (j + 0.5) * dlon)

# ---- point-in-(multi)polygon mask for the kommun boundaries ----
def load_features():
    # Each feature (Åre, Krokom) -> list of polygons; each polygon = [outer, holes...].
    feats = []
    for f in json.load(open(KOMMUN))["features"]:
        g = f["geometry"]
        if g["type"] == "Polygon": feats.append([g["coordinates"]])
        elif g["type"] == "MultiPolygon": feats.append(g["coordinates"])
    return feats

FEATURES = load_features()
def _in_feature(lon, lat, polys):
    # Even-odd ray casting across every ring (outer + holes) of one municipality.
    inside = False
    for poly in polys:
        for ring in poly:
            n = len(ring)
            j = n - 1
            for i in range(n):
                xi, yi = ring[i][0], ring[i][1]
                xj, yj = ring[j][0], ring[j][1]
                if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi):
                    inside = not inside
                j = i
    return inside

def in_kommun(lon, lat):
    # Keep the cell if its centre is inside Åre OR Krokom.
    return any(_in_feature(lon, lat, polys) for polys in FEATURES)

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
    N = NROWS * NCOLS
    inside_idx, inside_pts = [], []
    for i in range(NROWS):
        for j in range(NCOLS):
            lat, lon = cell_center(i, j)
            if in_kommun(lon, lat):
                inside_idx.append(i * NCOLS + j); inside_pts.append((lat, lon))
    print(f"Grid {NROWS}x{NCOLS}={N} cells; {len(inside_pts)} inside Åre + Krokom kommun; fetching elevation…")
    ev = fetch(inside_pts)
    elev = [None] * N
    for idx, e in zip(inside_idx, ev):
        elev[idx] = e
    E = [[elev[i * NCOLS + j] for j in range(NCOLS)] for i in range(NROWS)]

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
                        "region": "Åre + Krokom kommun (boundary-masked)",
                        "units": {"elevation": "m", "slope": "degrees"}, "built": date.today().isoformat()},
               "elevation": elevation, "slope": slope}, open(OUT, "w"), separators=(",", ":"))
    v = [x for x in elevation if x is not None]
    print(f"Wrote {OUT}: {len(v)} in-kommun cells, elev {min(v):.0f}–{max(v):.0f} m")

if __name__ == "__main__":
    main()
