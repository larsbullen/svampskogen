#!/usr/bin/env python3
"""Compute a Topographic Wetness Index (TWI) layer from the elevation grid.

TWI = ln( SCA / tan(beta) ), the standard DEM-derived soil-wetness proxy:
high where a lot of upslope area drains through a gentle slope (valley bottoms,
hollows), low on steep ridges. Self-contained (no external service) and cheap —
D8 flow accumulation over the grid — so it scales to any extent. Replaces the
flaky auth-gated SLU DTW / flow-channel approach with something reproducible.

Writes data/layers/soilmoisture.json (same filename the model already reads).
Values = TWI; HIGHER = wetter (opposite sign to the old distance proxy — the
model learns the sign either way).
"""
import json, math, os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ELEV = os.path.join(HERE, "data", "layers", "elevation.json")
OUT = os.path.join(HERE, "data", "layers", "soilmoisture.json")

e = json.load(open(ELEV))
M = e["meta"]
NROWS, NCOLS = M["nrows"], M["ncols"]
NORTH, SOUTH, WEST, EAST = M["north"], M["south"], M["west"], M["east"]
N = NROWS * NCOLS
dlat = (NORTH - SOUTH) / NROWS
dlon = (EAST - WEST) / NCOLS
midlat = (NORTH + SOUTH) / 2
my = 111320.0 * dlat                                   # metres per row (N-S)
mx = 111320.0 * math.cos(math.radians(midlat)) * dlon  # metres per col (E-W)
cell_area = mx * my
Lc = (mx + my) / 2.0                                    # effective contour width

E = e["elevation"]           # flat, row-major; may contain None
def at(i, j): return E[i * NCOLS + j]

# 8-neighbour offsets with planar distances
NB = []
for di in (-1, 0, 1):
    for dj in (-1, 0, 1):
        if di == 0 and dj == 0: continue
        NB.append((di, dj, math.hypot(di * my, dj * mx)))

# D8 steepest-descent downstream neighbour for each cell
down = [None] * N
order = []  # (elev, idx) for processing high -> low
for i in range(NROWS):
    for j in range(NCOLS):
        z = at(i, j)
        idx = i * NCOLS + j
        if z is None: continue
        order.append((z, idx))
        best, bslope = None, 0.0
        for di, dj, dist in NB:
            ni, nj = i + di, j + dj
            if not (0 <= ni < NROWS and 0 <= nj < NCOLS): continue
            nz = at(ni, nj)
            if nz is None: continue
            s = (z - nz) / dist
            if s > bslope:
                bslope, best = s, ni * NCOLS + nj
        down[idx] = best  # None => pit/sink (keeps its own accumulation)

# flow accumulation: each cell starts with 1, push downslope from high to low
acc = [1.0] * N
order.sort(reverse=True)  # highest elevation first
for _, idx in order:
    d = down[idx]
    if d is not None:
        acc[d] += acc[idx]

# TWI per cell
slope = e.get("slope") or [None] * N
values = []
for i in range(NROWS):
    for j in range(NCOLS):
        idx = i * NCOLS + j
        z = at(i, j)
        if z is None:
            values.append(None); continue
        sca = acc[idx] * cell_area / Lc            # specific catchment area
        beta = math.radians(slope[idx] if slope[idx] is not None else 0.5)
        twi = math.log(sca / max(math.tan(beta), 0.001))
        values.append(round(twi, 3))

out = {
    "meta": {
        "source": "DEM-derived (EU-DEM 25 m via elevation.json)",
        "layer": "Topographic Wetness Index  TWI = ln(SCA / tan(beta))",
        "units": "TWI (dimensionless)", "interpretation": "higher = wetter",
        "nrows": NROWS, "ncols": NCOLS,
        "north": NORTH, "south": SOUTH, "west": WEST, "east": EAST,
        "built": "2026-08-11",
        "notes": ("Self-contained D8 flow-accumulation TWI; replaces the "
                  "auth-gated SLU DTW and the flaky flow-channel proxy. Coarse "
                  "at this grid resolution but scale-free."),
    },
    "values": values,
}
json.dump(out, open(OUT, "w"), separators=(",", ":"))
v = [x for x in values if x is not None]
print(f"wrote {OUT}: {len(v)} cells, TWI {min(v):.2f}..{max(v):.2f} (median {sorted(v)[len(v)//2]:.2f})")
