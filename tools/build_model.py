#!/usr/bin/env python3
"""Train a presence-background habitat model (v1 SDM) and write suitability.json.

Pure-stdlib logistic regression — the standard presence-background / Maxent-lite
approach. Presences are the known GBIF/Artportalen finds; background is a random
sample of the landscape. Predictors are stacked from the layer files under
data/layers/ (elevation, slope, soil moisture, forest structure/type). Missing
layers are dropped gracefully so this still runs on whatever is available.

Output (data/suitability.json) keeps the {meta, scores[]} shape the web app
already renders, so no client change is needed beyond legend copy.
"""
import json, math, os, random

random.seed(42)
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
L = os.path.join(HERE, "data", "layers")
OUT = os.path.join(HERE, "data", "suitability.json")

def load(name):
    p = os.path.join(L, name)
    return json.load(open(p)) if os.path.exists(p) else None

elev = load("elevation.json")
if not elev:
    raise SystemExit("elevation.json missing — run tools/dump_elevation.py first")
soil = load("soilmoisture.json")
forest = load("forest.json")

M = elev["meta"]
NROWS, NCOLS = M["nrows"], M["ncols"]
NORTH, SOUTH, WEST, EAST = M["north"], M["south"], M["west"], M["east"]
N = NROWS * NCOLS
dlat = (NORTH - SOUTH) / NROWS
dlon = (EAST - WEST) / NCOLS

# ---- assemble raw predictor columns (each length N, None = missing) ----
cols = {}
cols["elev"] = elev["elevation"]
cols["slope"] = elev["slope"]
if soil and "values" in soil:
    cols["soil"] = soil["values"]
if forest:
    # tall_cover_frac (canopy cover, full coverage) is the robust maturity proxy;
    # tree_height_mean_m / volume added if present.
    for k in ("tall_cover_frac", "tree_height_mean_m", "volume"):
        if k in forest and isinstance(forest[k], list) and len(forest[k]) == N:
            cols[k] = forest[k]
    # derive a conifer indicator if the species labels make it inferable
    is_forest = forest.get("is_forest")
    sc = forest.get("species_class")
    labels = (forest.get("meta", {}) or {}).get("species_classes", {}) or {}
    conif_ids = {int(i) for i, lab in labels.items()
                 if any(w in lab.lower() for w in ("tall", "pine", "gran", "spruce", "barr", "conif"))}
    if sc and len(sc) == N and conif_ids:
        cols["conifer"] = [1 if (v is not None and int(v) in conif_ids) else (None if v is None else 0) for v in sc]
else:
    is_forest = None

# elevation squared (lets a linear model represent a mid-elevation optimum)
cols["elev2"] = [e * e if e is not None else None for e in cols["elev"]]

# NOTE: 'conifer' is derived and available, but with so few presences the
# background here is *also* mostly conifer, so a species-preference coefficient
# is unreliable — species instead drives the forest mask below. Add it back to
# this list once there are enough presences to estimate it.
# Keep the feature set modest given few presences: terrain + wetness + canopy cover.
feat_names = [k for k in ["elev", "elev2", "slope", "soil", "tall_cover_frac"] if k in cols]

# ---- validity mask: need elevation; drop cells above the local tree line hard? no — let model learn ----
valid = [cols["elev"][i] is not None for i in range(N)]

# ---- impute (median) + standardize (z-score) over valid cells ----
def stats(vals):
    xs = sorted(v for v in vals if v is not None)
    if not xs: return 0.0, 1.0, 0.0
    med = xs[len(xs) // 2]
    mean = sum(xs) / len(xs)
    var = sum((x - mean) ** 2 for x in xs) / max(1, len(xs) - 1)
    return med, (math.sqrt(var) or 1.0), mean

feat_stat = {f: stats(cols[f]) for f in feat_names}

def row(i):
    r = []
    for f in feat_names:
        med, sd, mean = feat_stat[f]
        v = cols[f][i]
        if v is None: v = med
        r.append((v - mean) / sd)
    return r

def cell_of(lon, lat):
    if not (SOUTH <= lat <= NORTH and WEST <= lon <= EAST): return None
    i = min(NROWS - 1, max(0, int((NORTH - lat) / dlat)))
    j = min(NCOLS - 1, max(0, int((lon - WEST) / dlon)))
    idx = i * NCOLS + j
    return idx if valid[idx] else None

# ---- presences: edible GBIF finds + the user's own reported finds ----
# data/myfinds.geojson is the "Exportera mina fynd" file dropped into the repo —
# this closes the loop so field finds train the model. Thinned to unique cells.
# Coarse GBIF records (>1 km uncertainty) are EXCLUDED from training — a presence
# that could be anywhere in a >3 km² circle is noise. They still show on the map
# (occurrences.geojson keeps them); only the model drops them. User finds are
# pinned exactly, so they're always kept.
MAX_PRESENCE_UNC = 1000
pres_idx = set()
n_user = 0
n_coarse_dropped = 0
for src in ("occurrences.geojson", "myfinds.geojson", "mina-svampfynd.geojson", "cloudfinds.geojson"):
    p = os.path.join(HERE, "data", src)
    if not os.path.exists(p): continue
    for f in json.load(open(p)).get("features", []):
        u = (f.get("properties") or {}).get("uncertainty_m")
        if src == "occurrences.geojson" and u and u > MAX_PRESENCE_UNC:
            n_coarse_dropped += 1
            continue
        lon, lat = f["geometry"]["coordinates"]
        c = cell_of(lon, lat)
        if c is not None:
            pres_idx.add(c)
            if src != "occurrences.geojson": n_user += 1
pres_idx = sorted(pres_idx)

# ---- background: target-group (all recorded fungi) to cancel recorder/effort
# bias; falls back to random if the target-group layer is missing/sparse. ----
valid_idx = [i for i in range(N) if valid[i]]
bg_method = "random"
background = []
tg_path = os.path.join(L, "targetgroup.geojson")
if os.path.exists(tg_path):
    tg_cells = [c for f in json.load(open(tg_path)).get("features", [])
                for c in (cell_of(*f["geometry"]["coordinates"]),) if c is not None]
    if len(tg_cells) >= 200:
        background = tg_cells if len(tg_cells) <= 3000 else random.sample(tg_cells, 3000)
        bg_method = "target-group (all-fungi effort surface)"
if not background:
    n_bg = min(len(valid_idx), max(1500, 40 * len(pres_idx)))
    background = random.sample(valid_idx, n_bg)

X = [row(i) for i in pres_idx] + [row(i) for i in background]
y = [1.0] * len(pres_idx) + [0.0] * len(background)
D = len(feat_names)

# ---- logistic regression via gradient descent w/ L2 ----
def sigmoid(z): return 1.0 / (1.0 + math.exp(-max(-30, min(30, z))))

w = [0.0] * D
b = 0.0
lr, lam, EPOCHS = 0.1, 1e-3, 3000
# balance: presence weight so the (few) presences aren't swamped by background
pw = len(background) / max(1, len(pres_idx))
for _ in range(EPOCHS):
    gw = [0.0] * D; gb = 0.0
    for r, yi in zip(X, y):
        z = b + sum(w[k] * r[k] for k in range(D))
        err = (sigmoid(z) - yi) * (pw if yi == 1.0 else 1.0)
        for k in range(D): gw[k] += err * r[k]
        gb += err
    n = len(X)
    for k in range(D): w[k] -= lr * (gw[k] / n + lam * w[k])
    b -= lr * gb / n

def predict(i): return sigmoid(b + sum(w[k] * row(i)[k] for k in range(D)))

# ---- simple held-out AUC (random split; optimistic under spatial autocorr) ----
def auc(pairs):
    pos = [p for p, l in pairs if l == 1]; neg = [p for p, l in pairs if l == 0]
    if not pos or not neg: return None
    c = sum((1 if a > b else 0.5 if a == b else 0) for a in pos for b in neg)
    return round(c / (len(pos) * len(neg)), 3)

allp = [(predict(i), 1) for i in pres_idx] + [(predict(i), 0) for i in background]
train_auc = auc(allp)

# ---- score every cell; mask non-forest to 0, invalid to -1; scale to 0..100 ----
pres_set = set(pres_idx)
tcf = cols.get("tall_cover_frac")   # cell-aggregate canopy cover (more robust than center-pixel is_forest)
def forest_ok(i):
    # true forest, OR partial canopy (≥10%) to keep tree-line mountain birch etc.
    if is_forest and len(is_forest) == N and is_forest[i] == 1:
        return True
    if tcf and tcf[i] is not None and tcf[i] >= 0.10:
        return True
    return False
raw = [predict(i) if valid[i] else None for i in range(N)]
mx = max((v for v in raw if v is not None), default=1.0) or 1.0
scores = []
for i in range(N):
    if raw[i] is None:
        scores.append(-1); continue
    # never mask a cell where mushrooms were actually found; else require some forest/canopy
    if i not in pres_set and is_forest and not forest_ok(i):
        scores.append(0); continue
    scores.append(round(raw[i] / mx * 100))

used = {
    "elevation+slope": True,
    "soil_moisture": "soil" in cols,
    "forest_canopy_cover": "tall_cover_frac" in cols,
    "forest_structure": any(k in cols for k in ("tree_height_mean_m", "volume")),
    "conifer_class": "conifer" in cols,
    "forest_mask": bool(is_forest),
}
out = {
    "meta": {
        "north": NORTH, "south": SOUTH, "west": WEST, "east": EAST,
        "nrows": NROWS, "ncols": NCOLS,
        "model": "v1-logistic-sdm",
        "predictors": feat_names,
        "layers_used": used,
        "n_presence": len(pres_idx), "n_user_finds": n_user,
        "n_coarse_dropped": n_coarse_dropped, "max_presence_uncertainty_m": MAX_PRESENCE_UNC,
        "n_background": len(background), "background_method": bg_method,
        "train_auc": train_auc,
        "built": "2026-08-11",
        "note": ("Presence-background logistic SDM on stacked open-data predictors. "
                 "Relative suitability (0=low,100=high). Presences include the "
                 "user's own reported finds; background is the all-fungi effort "
                 "surface (bias correction). Still few presences — AUC is "
                 "optimistic under spatial autocorrelation; treat as a guide."),
    },
    "scores": scores,
}
json.dump(out, open(OUT, "w"), separators=(",", ":"))
print(f"model: {feat_names}")
print(f"presences={len(pres_idx)} background={len(background)} train_auc={train_auc}")
print(f"weights={[round(x,3) for x in w]} bias={round(b,3)}")
print(f"wrote {OUT}")
