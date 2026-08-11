#!/usr/bin/env python3
"""Turn the habitat grid into discrete, tappable suitability ZONES.

Cells whose base suitability clears a threshold are grouped into 4-connected
components; each component becomes one polygon (with holes) plus aggregate
stats (dominant forest type, elevation, canopy, wetness, mean suitability, area).
Shapes are fixed (base habitat); the app recolours them by the weather forecast
for the selected date. Writes data/zones.geojson.
"""
import json, math, os
from collections import deque, Counter

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
L = os.path.join(HERE, "data", "layers")

def load(p):
    return json.load(open(os.path.join(HERE, "data", p)))

suit = load("suitability.json")
elev = load(os.path.join("layers", "elevation.json"))
forest = load(os.path.join("layers", "forest.json"))
soil = load(os.path.join("layers", "soilmoisture.json"))

M = suit["meta"]
NROWS, NCOLS = M["nrows"], M["ncols"]
NORTH, SOUTH, WEST, EAST = M["north"], M["south"], M["west"], M["east"]
N = NROWS * NCOLS
dlat = (NORTH - SOUTH) / NROWS
dlon = (EAST - WEST) / NCOLS
midlat = (NORTH + SOUTH) / 2
mlat = 111320.0 * dlat
mlon = 111320.0 * math.cos(math.radians(midlat)) * dlon
cell_ha = (mlat * mlon) / 10000.0

scores = suit["scores"]
sp_labels = forest["meta"]["species_classes"]
species_class = forest.get("species_class")
tcf = forest.get("tall_cover_frac")
twi = soil.get("values")
elv = elev["elevation"]

THRESH = 70       # base suitability to count as a zone cell (top ~20% of habitat)
MIN_CELLS = 3     # drop tiny specks

member = [scores[k] is not None and scores[k] >= THRESH for k in range(N)]

# ---- 4-connected components ----
comp = [-1] * N
comps = []
for s in range(N):
    if not member[s] or comp[s] != -1:
        continue
    q = deque([s]); comp[s] = len(comps); cells = []
    while q:
        k = q.popleft(); cells.append(k)
        i, j = divmod(k, NCOLS)
        for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ni, nj = i + di, j + dj
            if 0 <= ni < NROWS and 0 <= nj < NCOLS:
                nk = ni * NCOLS + nj
                if member[nk] and comp[nk] == -1:
                    comp[nk] = len(comps); q.append(nk)
    comps.append(cells)
comps = [c for c in comps if len(c) >= MIN_CELLS]

# ---- boundary rings for a set of cells (directed edges, interior on right) ----
def corner(ci, cj):
    return [round(WEST + cj * dlon, 6), round(NORTH - ci * dlat, 6)]

def rings_for(cells):
    cs = set(cells)
    def inset(i, j):
        return 0 <= i < NROWS and 0 <= j < NCOLS and (i * NCOLS + j) in cs
    edges = {}
    def add(a, b):
        edges.setdefault(a, []).append(b)
    for k in cells:
        i, j = divmod(k, NCOLS)
        if not inset(i - 1, j): add((i, j), (i, j + 1))          # top
        if not inset(i, j + 1): add((i, j + 1), (i + 1, j + 1))  # right
        if not inset(i + 1, j): add((i + 1, j + 1), (i + 1, j))  # bottom
        if not inset(i, j - 1): add((i + 1, j), (i, j))          # left
    rings = []
    while edges:
        start = next(iter(edges))
        ring = [start]; cur = start
        while True:
            nxts = edges.get(cur)
            nxt = nxts.pop()
            if not nxts: del edges[cur]
            ring.append(nxt); cur = nxt
            if cur == start: break
        rings.append(ring)
    # to lon/lat + signed area; largest = outer, rest = holes
    def to_ll(r): return [corner(ci, cj) for (ci, cj) in r]
    def area(r):
        a = 0.0
        for m in range(len(r) - 1):
            x1, y1 = r[m]; x2, y2 = r[m + 1]
            a += x1 * y2 - x2 * y1
        return a / 2.0
    llrings = [to_ll(r) for r in rings]
    llrings.sort(key=lambda r: abs(area(r)), reverse=True)
    return llrings  # [outer, holes...]

def mean(vals):
    v = [x for x in vals if x is not None]
    return sum(v) / len(v) if v else None

def wet_label(t):
    if t is None: return "okänt"
    return "torr mark" if t < 8.5 else "frisk mark" if t <= 11 else "fuktig mark"

def base_label(b):
    return "utmärkt" if b >= 78 else "mycket bra" if b >= 66 else "bra" if b >= 58 else "medel"

# Short Swedish forest types; NMD center-pixel calls tree-line canopy "non-forest"
# (class 0) or clearcut (5) — within a zone those are really sparse mountain forest.
SHORT = {1: "Tallskog", 2: "Granskog", 3: "Barrblandskog", 4: "Lövskog", 6: "Skog"}
def cell_type(k):
    sc = species_class[k] if species_class and species_class[k] is not None else 0
    return SHORT.get(sc, "Fjällnära skog")

features = []
for cid, cells in enumerate(sorted(comps, key=len, reverse=True)):
    rings = rings_for(cells)
    base = round(mean(scores[k] for k in cells))
    e = mean(elv[k] for k in cells)
    can = mean((tcf[k] if tcf else None) for k in cells)
    tw = mean((twi[k] if twi else None) for k in cells)
    dom_type = Counter(cell_type(k) for k in cells).most_common(1)[0][0]
    features.append({
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": rings},
        "properties": {
            "id": cid,
            "base": base,
            "n": len(cells),
            "area_ha": round(len(cells) * cell_ha, 1),
            "elev": round(e) if e is not None else None,
            "canopy": round(can, 2) if can is not None else None,
            "twi": round(tw, 1) if tw is not None else None,
            "wetness": wet_label(tw),
            "type": dom_type,
            "base_label": base_label(base),
        },
    })

out = {
    "type": "FeatureCollection",
    "meta": {"built": M.get("built"), "threshold": THRESH, "n_zones": len(features),
             "note": "Fixed base-habitat zones; the app recolours them by forecast(date)."},
    "features": features,
}
json.dump(out, open(os.path.join(HERE, "data", "zones.geojson"), "w"), separators=(",", ":"))
areas = sorted(f["properties"]["area_ha"] for f in features)
print(f"wrote data/zones.geojson: {len(features)} zones, "
      f"area ha min/median/max = {areas[0]}/{areas[len(areas)//2]}/{areas[-1]}")
from collections import Counter as C
print("dominant types:", dict(C(f["properties"]["type"] for f in features)))
