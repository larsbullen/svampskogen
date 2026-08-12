#!/usr/bin/env python3
"""Dump raw elevation + slope on the shared grid to data/layers/elevation.json.

Covers the whole Åre kommun. Cells whose centre falls OUTSIDE the municipality
boundary (data/kommun.geojson) are set to None — this both masks everything
downstream to the real kommun outline and avoids wasting elevation API calls on
Norway / neighbouring municipalities.

Same grid constants are the single source of truth; all other layers align to
elevation.json's meta.
"""
import json, math, time, urllib.request, urllib.parse, os

# Whole Åre kommun (bbox of the admin boundary), ~0.8 km cells.
NORTH, SOUTH = 64.10, 62.90
WEST,  EAST  = 11.97, 14.41
NROWS, NCOLS = 167, 151
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT  = os.path.join(HERE, "data", "layers", "elevation.json")
KOMMUN = os.path.join(HERE, "data", "kommun.geojson")
API  = "https://api.opentopodata.org/v1/eudem25m"

dlat = (NORTH - SOUTH) / NROWS
dlon = (EAST - WEST) / NCOLS
def cell_center(i, j): return (NORTH - (i + 0.5) * dlat, WEST + (j + 0.5) * dlon)

# ---- point-in-(multi)polygon mask for the kommun boundary ----
def load_rings():
    g = json.load(open(KOMMUN))["features"][0]["geometry"]
    if g["type"] == "Polygon": polys = [g["coordinates"]]
    elif g["type"] == "MultiPolygon": polys = g["coordinates"]
    else: polys = []
    return polys  # list of polygons; each = [outer, holes...]

POLYS = load_rings()
def in_kommun(lon, lat):
    inside = False
    for poly in POLYS:
        for r, ring in enumerate(poly):
            c = False
            n = len(ring)
            j = n - 1
            for i in range(n):
                xi, yi = ring[i][0], ring[i][1]
                xj, yj = ring[j][0], ring[j][1]
                if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi):
                    c = not c
                j = i
            # ring 0 = outer (add), rings >0 = holes (subtract)
            if c:
                if r == 0: inside = not inside
                else: inside = not inside  # toggling handles holes within same polygon
    return inside

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
    print(f"Grid {NROWS}x{NCOLS}={N} cells; {len(inside_pts)} inside Åre kommun; fetching elevation…")
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
                        "region": "Åre kommun (boundary-masked)",
                        "units": {"elevation": "m", "slope": "degrees"}, "built": "2026-08-12"},
               "elevation": elevation, "slope": slope}, open(OUT, "w"), separators=(",", ":"))
    v = [x for x in elevation if x is not None]
    print(f"Wrote {OUT}: {len(v)} in-kommun cells, elev {min(v):.0f}–{max(v):.0f} m")

if __name__ == "__main__":
    main()
