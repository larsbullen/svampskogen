#!/usr/bin/env python3
"""Morgonsol step 4 — combine every layer into a tent-site score, then extract candidates.

Two kinds of rule are kept deliberately separate:

  HARD MASKS   things that make a spot unusable or not allowed — too steep, in a
               mire, in open water, inside a camping-ban zone. These zero the cell.
  SOFT SCORES  things that make a spot nicer — level, dry, early sun, a slight
               rise so cold air drains away, water a short walk off.

Keeping them apart matters: a beautiful flat dry bench inside a ban zone must
never surface as "almost fine", it has to disappear.

Outputs:
    data/morgonsol/areas.geojson   good-ground polygons, banded by quality
    data/morgonsol/sites.geojson   ranked point candidates with a "why" per site
    data/morgonsol/meta.json       weights, thresholds, layer provenance

Usage:
    python3 tools/morgonsol/build_score.py
"""
import json
import math
import os
from collections import OrderedDict

import numpy as np
import rasterio
from rasterio import features
from pyproj import Transformer
from scipy import ndimage
from shapely.geometry import LineString, Point, mapping, shape
from shapely.ops import transform as shp_transform

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.normpath(os.path.join(HERE, "..", "..", "data", "morgonsol"))
TERRAIN = "/private/tmp/morgonsol_dem/terrain.tif"
SUN = "/private/tmp/morgonsol_dem/sun.tif"
SOIL = "/private/tmp/morgonsol_dem/soil.tif"  # optional
CANOPY = "/private/tmp/morgonsol_dem/canopy.tif"  # optional

to_sweref = Transformer.from_crs("EPSG:4326", "EPSG:3006", always_xy=True).transform
to_wgs84 = Transformer.from_crs("EPSG:3006", "EPSG:4326", always_xy=True).transform

# ---------------------------------------------------------------- tuning knobs
SLOPE_IDEAL = 2.0       # deg — full marks at or below
SLOPE_MAX = 6.0         # deg — hard reject above; you notice 5 deg all night
ROUGH_IDEAL = 1.0       # m local relief in a 100 m window
ROUGH_MAX = 6.0
WATER_NEAR = 15.0       # m — too close to a stream: damp, and bad practice
WATER_MIN = 50.0        # m — start of the comfortable band
WATER_MAX = 400.0       # m — beyond this, fetching water is a chore
HUT_CLEAR = 150.0       # m — keep off the doorstep of a cabin
TRAIL_CLEAR = 30.0      # m — don't pitch on the path itself
WETLAND_BUFFER = 25.0   # m — mire edges are wetter than the polygon says
TPI_BEST = 4.0          # m above the local mean: drains cold air, catches a breeze
TPI_SPREAD = 12.0
MIN_SITE_CELLS = 4      # ~1600 m2 at 20 m — room for a couple of tents
SITE_SPACING_M = 400.0  # don't list two candidates that are the same spot
MAX_SITES = 150

# Absolute quality gates, not percentiles. A percentile band would always call
# the best 3% of the corridor "topp" even if the whole corridor were a swamp.
# Each band is a conjunction so the label means something you can state out
# loud: "topp" is ground under 2.5 degrees that is genuinely dry and even.
BAND_RULES = [
    # (label, min_score, max_slope_deg, min_dry, max_rough_m)
    ("bra", 0.65, 4.5, 0.50, 3.5),
    ("mycket bra", 0.75, 3.0, 0.65, 2.2),
    ("topp", 0.82, 2.0, 0.75, 1.6),
]
MIN_POLY_M2 = 4000.0    # ~63x63 m; smaller than this is a sliver, not a campsite

# Morning sun is a tiebreaker, never a gate — dry and level ground win every
# time. --sun-weight re-dials it and the rest are renormalised around it.
WEIGHTS = OrderedDict(
    [
        ("level", 0.30),
        ("dry", 0.32),
        ("sun", 0.08),
        ("position", 0.13),
        ("smooth", 0.11),
        ("water", 0.06),
    ]
)


def load_grid():
    with open(os.path.join(DATA, "grid.json")) as f:
        return json.load(f)


def read_bands(path):
    if not os.path.exists(path):
        return None, None
    with rasterio.open(path) as src:
        names = list(src.descriptions)
        data = {n: src.read(i + 1) for i, n in enumerate(names)}
        return data, src.profile


def geoms_from(path, types=None):
    """Read a GeoJSON file and return shapely geometries reprojected to EPSG:3006."""
    full = os.path.join(DATA, path)
    if not os.path.exists(full):
        return []
    with open(full) as f:
        gj = json.load(f)
    feats = gj.get("features", []) if gj.get("type") == "FeatureCollection" else [gj]
    out = []
    for ft in feats:
        g = ft.get("geometry")
        if not g:
            continue
        if types and g.get("type") not in types:
            continue
        try:
            geom = shp_transform(to_sweref, shape(g))
            if geom.is_valid and not geom.is_empty:
                out.append(geom)
        except Exception:
            continue
    return out


def rasterize(geoms, grid, all_touched=True):
    if not geoms:
        return np.zeros((grid["height"], grid["width"]), dtype=bool)
    tf = rasterio.transform.Affine(*grid["transform"])
    arr = features.rasterize(
        ((g, 1) for g in geoms),
        out_shape=(grid["height"], grid["width"]),
        transform=tf,
        fill=0,
        all_touched=all_touched,
        dtype="uint8",
    )
    return arr.astype(bool)


def distance_to(mask, res):
    """Metres from every cell to the nearest True cell (inf if mask is empty)."""
    if not mask.any():
        return np.full(mask.shape, 1e9, dtype="float32")
    return (ndimage.distance_transform_edt(~mask) * res).astype("float32")


def round_coords(geom, nd=5):
    """Round coordinates in place. 5 dp is ~1 m here and roughly halves the file."""

    def walk(c):
        if isinstance(c, (list, tuple)):
            if c and isinstance(c[0], (int, float)):
                return [round(float(v), nd) for v in c]
            return [walk(x) for x in c]
        return c

    geom = dict(geom)
    geom["coordinates"] = walk(geom["coordinates"])
    return geom


def ramp(x, lo, hi):
    """0 below lo, 1 above hi, linear between. Handles lo > hi (descending)."""
    if hi == lo:
        return (x >= hi).astype("float32")
    return np.clip((x - lo) / (hi - lo), 0.0, 1.0).astype("float32")


def main():
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--sun-weight",
        type=float,
        default=WEIGHTS["sun"],
        help="how much morning sun counts (0 = ignore it entirely)",
    )
    args = ap.parse_args()
    if abs(args.sun_weight - WEIGHTS["sun"]) > 1e-9:
        others = {k: v for k, v in WEIGHTS.items() if k != "sun"}
        scale = (1.0 - args.sun_weight) / sum(others.values())
        for k in others:
            WEIGHTS[k] = others[k] * scale
        WEIGHTS["sun"] = args.sun_weight
        print("sun weight set to %.2f, others renormalised" % args.sun_weight)

    grid = load_grid()
    res = grid["res_m"]
    H, W = grid["height"], grid["width"]

    terrain, profile = read_bands(TERRAIN)
    if terrain is None:
        raise SystemExit("missing %s — run build_terrain.py first" % TERRAIN)
    sun, _ = read_bands(SUN)
    soil, _ = read_bands(SOIL)
    slu_water = soil.get("water") if soil else None
    canopy, _ = read_bands(CANOPY)
    tree_frac = canopy.get("tree_frac") if canopy else None

    slope = terrain["slope"]
    rough = terrain["rough"]
    tpi = terrain["tpi"]
    twi = terrain["twi"]
    flat = terrain["flat"]
    elev = terrain["elev"]
    valid = np.isfinite(elev)

    provenance = OrderedDict()
    dem_src = "unknown DEM"
    try:
        with open(os.path.join(DATA, "dem_source.json")) as f:
            dem_src = json.load(f)["label"]
    except Exception:
        pass
    provenance["dem"] = "%s, on a %g m grid" % (dem_src, res)
    if sun:
        try:
            with open(os.path.join(DATA, "sun_meta.json")) as f:
                sm = json.load(f)
            provenance["sun"] = (
                "ray-cast terrain shadowing (NOAA solar position) for %s" % sm["date"])
            provenance["sun_meta"] = sm
        except Exception:
            provenance["sun"] = "ray-cast terrain shadowing, NOAA solar position"
    else:
        provenance["sun"] = "absent"

    # ------------------------------------------------------------ hard masks
    corridor = rasterize(geoms_from("corridor.geojson"), grid)
    wetland_geoms = geoms_from("wetland.geojson")
    water_geoms = geoms_from("water.geojson")
    hut_geoms = geoms_from("huts.geojson")
    trail_geoms = geoms_from("trails.geojson")

    wetland = rasterize(wetland_geoms, grid)
    water = rasterize(water_geoms, grid)

    d_water = distance_to(water, res)
    d_wetland = distance_to(wetland, res)
    d_hut = distance_to(rasterize(hut_geoms, grid), res)
    d_trail = distance_to(rasterize(trail_geoms, grid), res)

    # Camping-ban zones, if the zone layer was obtainable.
    ban = np.zeros((H, W), dtype=bool)
    zones_path = os.path.join(DATA, "zones.geojson")
    ban_note = "zone layer not available — NOT applied"
    if os.path.exists(zones_path):
        with open(zones_path) as f:
            zg = json.load(f)
        # Two different things live in this file and they must not be confused:
        #   _kind == "zone"        the four big zones, ALL of which permit camping
        #   _kind == "restriction" the small named areas where it is forbidden
        # Never keyword-sniff the free text: the zone polygons say things like
        # "inga beträdnadsförbud" and a naive "förbud" match bans the whole reserve.
        ban_feats, ban_names = [], []
        n_zone = n_restriction = 0
        for ft in zg.get("features", []):
            props = ft.get("properties") or {}
            kind = str(props.get("_kind", "")).lower()
            if kind == "zone":
                n_zone += 1
                continue
            if kind != "restriction":
                continue
            n_restriction += 1
            # Conservative: every restriction polygon is treated as no-camping,
            # including the "partial" ones, because the polygon is the restricted
            # part and a legal mask should never err toward permitting.
            try:
                ban_feats.append(shp_transform(to_sweref, shape(ft["geometry"])))
                ban_names.append(str(props.get("_name") or props.get("namn") or "?"))
            except Exception:
                pass
        if n_zone == 0 and n_restriction == 0:
            raise SystemExit(
                "zones.geojson has no _kind field — refusing to guess which polygons "
                "ban camping. Fix the zone layer rather than shipping a wrong legal mask."
            )
        if ban_feats:
            ban = rasterize(ban_feats, grid)
            ban_note = "%d restriction polygons applied (%s); %d permissive zones ignored" % (
                len(ban_feats),
                ", ".join(sorted(set(ban_names))),
                n_zone,
            )
        else:
            ban_note = "no restriction polygons found in zones.geojson"
    provenance["ban_zones"] = ban_note

    # SLU sees open water the OSM polygons miss (small tarns, braided channels).
    if slu_water is not None:
        extra_water = np.nan_to_num(slu_water, nan=0.0) > 0.5
        water = water | extra_water
        d_water = distance_to(water, res)
        print("open water: +%d cells from SLU beyond the OSM polygons"
              % int((extra_water & ~rasterize(water_geoms, grid)).sum()))

    hard_ok = (
        valid
        & corridor
        & (slope <= SLOPE_MAX)
        & ~wetland
        & ~water
        & (d_water >= WATER_NEAR)
        & (d_wetland >= WETLAND_BUFFER)
        & (d_hut >= HUT_CLEAR)
        & (d_trail >= TRAIL_CLEAR)
        & ~ban
    )

    # ------------------------------------------------------------ soft scores
    s_level = 0.65 * (1.0 - ramp(slope, SLOPE_IDEAL, SLOPE_MAX)) + 0.35 * np.clip(flat * 2.0, 0, 1)

    # Dryness: TWI is the backbone; being far from mire and stream reinforces it.
    tw = twi[np.isfinite(twi)]
    twi_lo, twi_hi = np.percentile(tw, 15), np.percentile(tw, 85)
    s_twi = 1.0 - ramp(twi, twi_lo, twi_hi)
    s_dry = 0.60 * s_twi + 0.25 * ramp(d_wetland, 25.0, 250.0) + 0.15 * ramp(d_water, 15.0, 150.0)

    if soil and "wet01" in soil:
        # Real measured wetness beats the terrain proxy, so it carries most of
        # the weight; TWI stays in to cover the ~2% of cells SLU doesn't reach.
        wet01 = soil["wet01"]
        s_soil = 1.0 - np.clip(wet01, 0.0, 1.0)
        have = np.isfinite(wet01)
        s_dry = np.where(have, 0.65 * s_soil + 0.35 * s_dry, s_dry)
        provenance["soil_moisture"] = (
            "SLU Markfuktighetskarta (2 m, classified), %.1f%% grid coverage"
            % (100.0 * np.isfinite(wet01).mean())
        )
    else:
        provenance["soil_moisture"] = "not available — dryness is DEM-derived (TWI) only"

    if sun:
        first = sun["first_light"]
        # 06:00 or earlier is perfect; 10:00 or later scores nothing.
        s_sun = 1.0 - ramp(np.nan_to_num(first, nan=24.0), 6.0, 10.0)
    else:
        s_sun = np.full((H, W), 0.5, dtype="float32")

    # A slight rise: cold air drains off it and midges get a breeze, but a summit
    # is too exposed. Bell curve centred just above the local mean.
    s_pos = np.exp(-0.5 * ((tpi - TPI_BEST) / TPI_SPREAD) ** 2).astype("float32")

    s_smooth = 1.0 - ramp(rough, ROUGH_IDEAL, ROUGH_MAX)

    # Water: a band, not a gradient — too close is damp, too far is a chore.
    s_water = np.minimum(ramp(d_water, WATER_NEAR, WATER_MIN), 1.0 - ramp(d_water, WATER_MAX, WATER_MAX * 2.5))
    s_water = np.clip(s_water, 0.0, 1.0)

    parts = {
        "level": s_level,
        "dry": s_dry,
        "sun": s_sun,
        "position": s_pos,
        "smooth": s_smooth,
        "water": s_water,
    }
    # Weighted GEOMETRIC mean. A tent site has to be dry AND level AND smooth;
    # a sum would let an excellent level score paper over a soaking wet one.
    log_sum = np.zeros((H, W), dtype="float32")
    for k, w in WEIGHTS.items():
        p = np.clip(np.nan_to_num(parts[k], nan=0.0), 0.02, 1.0)
        log_sum += w * np.log(p)
    score = np.exp(log_sum).astype("float32")
    score = np.where(hard_ok, score, 0.0)
    score = np.clip(score, 0.0, 1.0)

    print("cells in corridor      : %d" % corridor.sum())
    print("cells passing hard mask: %d (%.1f%% of corridor)" % (hard_ok.sum(), 100.0 * hard_ok.sum() / max(1, corridor.sum())))
    for k in WEIGHTS:
        v = parts[k][hard_ok]
        if v.size:
            print("  %-9s mean %.3f" % (k, float(np.mean(v))))
    good = score[hard_ok]
    if good.size:
        print("score on passing cells : median %.3f  p90 %.3f  max %.3f" % (np.median(good), np.percentile(good, 90), good.max()))

    # ------------------------------------------------------- quality banding
    if good.size < 50:
        raise SystemExit("almost nothing passed the hard mask — check the input layers")

    banded = np.zeros((H, W), dtype="uint8")
    labels = {}
    band_defs = {}
    for level, (name, min_s, max_sl, min_dry, max_rg) in enumerate(BAND_RULES, start=1):
        m = (
            hard_ok
            & (score >= min_s)
            & (slope <= max_sl)
            & (s_dry >= min_dry)
            & (rough <= max_rg)
        )
        # Speckle removal. Drop whole connected components smaller than a real
        # pitch rather than eroding with an opening — an opening also eats the
        # edges of genuine patches, and at this grid size that deleted the top
        # tier outright. No closing either: it inflated "bra" by a third by
        # bridging across ground that had genuinely failed.
        lab_b, n_b = ndimage.label(m, structure=np.ones((3, 3)))
        if n_b:
            comp_sizes = np.bincount(lab_b.ravel())
            too_small = comp_sizes < (MIN_POLY_M2 / (res * res))
            too_small[0] = True
            m = ~too_small[lab_b]

        banded[m] = level
        labels[level] = name
        band_defs[name] = {
            "min_score": min_s,
            "max_slope_deg": max_sl,
            "min_dry": min_dry,
            "max_rough_m": max_rg,
        }
        print(
            "  %-11s : %7d cells (%.2f km2)  [score>=%.2f, slope<=%.1f, dry>=%.2f, rough<=%.1f]"
            % (name, int(m.sum()), m.sum() * res * res / 1e6, min_s, max_sl, min_dry, max_rg)
        )
    b2 = BAND_RULES[1][0]  # name of the middle band, used for candidate extraction

    tf = rasterio.transform.Affine(*grid["transform"])
    area_feats = []
    for geom, val in features.shapes(banded, mask=banded > 0, transform=tf, connectivity=8):
        g = shape(geom)
        if g.area < MIN_POLY_M2:
            continue
        g = g.simplify(res * 2.0, preserve_topology=True)
        if g.is_empty or g.area < MIN_POLY_M2:
            continue
        area_feats.append(
            {
                "type": "Feature",
                "properties": {"band": int(val), "label": labels[int(val)], "area_m2": round(g.area)},
                "geometry": round_coords(mapping(shp_transform(to_wgs84, g)), 5),
            }
        )
    area_feats.sort(key=lambda f: (-f["properties"]["band"], -f["properties"]["area_m2"]))
    with open(os.path.join(DATA, "areas.geojson"), "w") as f:
        json.dump({"type": "FeatureCollection", "features": area_feats}, f)
    print("areas.geojson          : %d polygons" % len(area_feats))

    # -------------------------------------------------- ranked point candidates
    # One candidate per contiguous patch that reaches at least "mycket bra".
    top = banded >= 2
    lab, n = ndimage.label(top, structure=np.ones((3, 3)))
    print("candidate patches      : %d" % n)

    # Every LineString in route.geojson is a route you might walk — the GPX loop
    # plus any connecting trail added by add_segment.py. A site is measured
    # against whichever one is actually nearest, and says which.
    routes = []
    with open(os.path.join(DATA, "route.geojson")) as f:
        for ft in json.load(f)["features"]:
            if ft["geometry"]["type"] == "LineString":
                routes.append((
                    (ft.get("properties") or {}).get("name") or "rutt",
                    shp_transform(to_sweref, shape(ft["geometry"])),
                ))
    print("routes: %s" % ", ".join("%s (%.1f km)" % (n, g.length / 1000) for n, g in routes))
    hut_pts = [g.centroid for g in hut_geoms] if hut_geoms else []

    sizes = ndimage.sum(np.ones_like(score), lab, index=np.arange(1, n + 1))
    maxpos = ndimage.maximum_position(score, lab, index=np.arange(1, n + 1))
    maxval = ndimage.maximum(score, lab, index=np.arange(1, n + 1))

    cands = []
    for i in range(n):
        if sizes[i] < MIN_SITE_CELLS:
            continue
        r, c = int(maxpos[i][0]), int(maxpos[i][1])
        x, y = tf * (c + 0.5, r + 0.5)
        lon, lat = to_wgs84(x, y)
        p = Point(x, y)
        if routes:
            r_name, r_geom = min(routes, key=lambda kv: kv[1].distance(p))
            along = r_geom.project(p) / 1000.0
            off = r_geom.distance(p)
        else:
            r_name, along, off = None, None, None
        nearest_hut = min((p.distance(h) for h in hut_pts), default=None)

        why = []
        if slope[r, c] <= 2.0:
            why.append("mycket flackt (%.1f°)" % slope[r, c])
        elif slope[r, c] <= 4.0:
            why.append("flackt (%.1f°)" % slope[r, c])
        if s_twi[r, c] > 0.7:
            why.append("torr mark")
        if sun and np.isfinite(sun["first_light"][r, c]) and sun["first_light"][r, c] < 24:
            fl = float(sun["first_light"][r, c])
            why.append("morgonsol %02d:%02d" % (int(fl), int(round((fl % 1) * 60))))
        if tpi[r, c] > 1.0:
            why.append("liten hojd, kalluft rinner undan")
        if float(d_water[r, c]) < 1e8:
            why.append("vatten %d m" % int(round(d_water[r, c] / 10.0) * 10))

        tf_here = (float(tree_frac[r, c])
                   if tree_frac is not None and np.isfinite(tree_frac[r, c]) else None)
        if tf_here is None:
            shelter, shelter_label = "okand", "Okänt"
        elif tf_here >= 0.30:
            shelter, shelter_label = "skog", "Skog"
        elif tf_here >= 0.10:
            shelter, shelter_label = "glest", "Glest / skogsbryn"
        else:
            shelter, shelter_label = "kalfjall", "Kalfjäll"

        area = float(sizes[i]) * res * res
        if area >= 20000:
            capacity = "stor"
        elif area >= 5000:
            capacity = "medel"
        else:
            capacity = "liten"

        cands.append(
            {
                "type": "Feature",
                "properties": {
                    "score": round(float(maxval[i]), 3),
                    "tree_frac": round(tf_here, 2) if tf_here is not None else None,
                    "shelter": shelter,
                    "shelter_label": shelter_label,
                    "capacity": capacity,
                    "elev_m": int(round(float(elev[r, c]))),
                    "slope_deg": round(float(slope[r, c]), 1),
                    "first_light": (
                        round(float(sun["first_light"][r, c]), 2)
                        if sun and np.isfinite(sun["first_light"][r, c])
                        else None
                    ),
                    "sun_morning_h": (
                        round(float(sun["sun_morning"][r, c]), 1) if sun else None
                    ),
                    "twi": round(float(twi[r, c]), 2),
                    "tpi_m": round(float(tpi[r, c]), 1),
                    "rough_m": round(float(rough[r, c]), 2),
                    "water_m": int(d_water[r, c]) if d_water[r, c] < 1e8 else None,
                    "wetland_m": int(d_wetland[r, c]) if d_wetland[r, c] < 1e8 else None,
                    "hut_m": int(nearest_hut) if nearest_hut is not None else None,
                    "trail_m": int(d_trail[r, c]) if d_trail[r, c] < 1e8 else None,
                    "route": r_name,
                    "route_km": round(along, 1) if along is not None else None,
                    "off_route_m": int(off) if off is not None else None,
                    "patch_m2": int(sizes[i] * res * res),
                    "why": ", ".join(why),
                },
                "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
            }
        )

    # `score` stays a pure statement about the GROUND. Ranking is a different
    # question — a perfect bench 1.9 km off the line is worth less to someone
    # walking 86 km than a very good one 200 m off it — so rank on both.
    for ft in cands:
        off = ft["properties"].get("off_route_m")
        access = 1.0 if off is None else float(np.clip(1.0 - (off - 300.0) / 1700.0, 0.0, 1.0))
        ft["properties"]["access"] = round(access, 3)
        ft["properties"]["usefulness"] = round(
            ft["properties"]["score"] * (0.6 + 0.4 * access), 4
        )
    cands.sort(key=lambda f: -f["properties"]["usefulness"])

    # Thin greedily: keep the best, drop anything within SITE_SPACING_M of an
    # already-kept site. Fifty separate pins on one flat terrace is not fifty
    # options, it is one option and a cluttered map.
    kept, kept_xy = [], []
    for ft in cands:
        lon, lat = ft["geometry"]["coordinates"]
        x, y = to_sweref(lon, lat)
        if any(math.hypot(x - kx, y - ky) < SITE_SPACING_M for kx, ky in kept_xy):
            continue
        kept.append(ft)
        kept_xy.append((x, y))
        if len(kept) >= MAX_SITES:
            break
    for rank, ft in enumerate(kept, start=1):
        ft["properties"]["rank"] = rank

    with open(os.path.join(DATA, "sites.geojson"), "w") as f:
        json.dump({"type": "FeatureCollection", "features": kept}, f)
    print(
        "sites.geojson          : %d kept (from %d patches, >=%.0f m apart)"
        % (len(kept), len(cands), SITE_SPACING_M)
    )

    meta = {
        "built_for_route": grid["route"],
        "resolution_m": res,
        "weights": dict(WEIGHTS),
        "hard_masks": {
            "slope_max_deg": SLOPE_MAX,
            "wetland_buffer_m": WETLAND_BUFFER,
            "water_min_m": WATER_NEAR,
            "hut_clear_m": HUT_CLEAR,
            "trail_clear_m": TRAIL_CLEAR,
            "ban_zones": ban_note,
        },
        "bands": band_defs,
        "provenance": provenance,
        "counts": {
            "corridor_cells": int(corridor.sum()),
            "passing_cells": int(hard_ok.sum()),
            "areas": len(area_feats),
            "sites": len(kept),
            "sites_before_thinning": len(cands),
            "wetland_polygons": len(wetland_geoms),
            "water_features": len(water_geoms),
            "huts": len(hut_geoms),
        },
    }
    with open(os.path.join(DATA, "meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
    print("meta.json              : written")


if __name__ == "__main__":
    main()
