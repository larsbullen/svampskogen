#!/usr/bin/env python3
"""Morgonsol — contour lines (höjdkurvor) straight from the 1 m lidar DTM.

Generated rather than borrowed from a tile basemap for three reasons: it works
offline like the rest of the page, it comes off the same elevation model as the
scores so the lines agree with the shading, and it can be styled to sit quietly
under the tent-ground colours instead of fighting them.

The DEM is smoothed slightly before contouring. Raw 1 m lidar is faithful enough
to pick up boulders and hummocks, which at a 20 m contour interval turns into
hairy, illegible lines — the smoothing buys legibility, not accuracy, and the
scoring layers still use the unsmoothed data.

Outputs data/morgonsol/contours.geojson with, per line:
    ele    elevation in metres
    index  true every INDEX_EVERY-th line (drawn bolder and labelled)

Usage:
    python3 tools/morgonsol/build_contours.py [--interval 20] [--index-every 5]
"""
import argparse
import json
import os

import numpy as np
import rasterio
from pyproj import Transformer
from scipy import ndimage
from skimage import measure

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.normpath(os.path.join(HERE, "..", "..", "data", "morgonsol"))
TERRAIN = "/private/tmp/morgonsol_dem/terrain.tif"

to_wgs84 = Transformer.from_crs("EPSG:3006", "EPSG:4326", always_xy=True).transform


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=20.0, help="metres between lines")
    ap.add_argument("--index-every", type=int, default=5, help="every Nth line is an index line")
    ap.add_argument("--smooth", type=float, default=2.0, help="gaussian sigma in cells")
    ap.add_argument("--min-points", type=int, default=8, help="drop shorter squiggles")
    args = ap.parse_args()

    with open(os.path.join(DATA, "grid.json")) as f:
        grid = json.load(f)
    tf = rasterio.transform.Affine(*grid["transform"])
    res = grid["res_m"]

    with rasterio.open(TERRAIN) as src:
        names = list(src.descriptions)
        z = src.read(names.index("elev") + 1)

    valid = np.isfinite(z)
    if not valid.any():
        raise SystemExit("no elevation data")
    zf = np.where(valid, z, np.nanmedian(z[valid])).astype("float32")
    if args.smooth > 0:
        zf = ndimage.gaussian_filter(zf, sigma=args.smooth)

    lo = float(np.floor(np.nanmin(z[valid]) / args.interval) * args.interval)
    hi = float(np.ceil(np.nanmax(z[valid]) / args.interval) * args.interval)
    levels = np.arange(lo, hi + args.interval, args.interval)
    print("elevation %.0f-%.0f m -> %d levels at %g m"
          % (np.nanmin(z[valid]), np.nanmax(z[valid]), len(levels), args.interval))

    # Simplify in grid cells; ~0.6 cell keeps the shape and halves the vertices.
    tol_cells = 0.6
    feats = []
    for lev in levels:
        is_index = int(round(lev / args.interval)) % args.index_every == 0
        for cont in measure.find_contours(zf, float(lev)):
            if len(cont) < args.min_points:
                continue
            # Douglas-Peucker in array space, then project to lon/lat.
            approx = measure.approximate_polygon(cont, tolerance=tol_cells)
            if len(approx) < 2:
                continue
            coords = []
            for r, c in approx:
                x, y = tf * (c + 0.5, r + 0.5)
                lon, lat = to_wgs84(x, y)
                coords.append([round(lon, 5), round(lat, 5)])
            # Drop consecutive duplicates left by rounding.
            dedup = [coords[0]]
            for p in coords[1:]:
                if p != dedup[-1]:
                    dedup.append(p)
            if len(dedup) < 2:
                continue
            feats.append({
                "type": "Feature",
                "properties": {"ele": int(round(lev)), "index": is_index},
                "geometry": {"type": "LineString", "coordinates": dedup},
            })

    out = os.path.join(DATA, "contours.geojson")
    with open(out, "w") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f, separators=(",", ":"))

    n_index = sum(1 for ft in feats if ft["properties"]["index"])
    n_vert = sum(len(ft["geometry"]["coordinates"]) for ft in feats)
    print("wrote %s" % out)
    print("  %d lines (%d index), %d vertices, %.1f MB"
          % (len(feats), n_index, n_vert, os.path.getsize(out) / 1e6))


if __name__ == "__main__":
    main()
