#!/usr/bin/env python3
"""Morgonsol step 2 — reproject a DEM onto the corridor grid and derive terrain layers.

Outputs a multi-band GeoTIFF (kept out of the repo, it is large) holding:

    elev        m a.s.l.
    slope       degrees
    aspect      compass bearing of the downhill direction, 0=N 90=E
    northness   north component of downhill, +1 due N .. -1 due S
    eastness    east  component of downhill, +1 due E .. -1 due W
    rough       local relief (std of elevation in a 5x5 window) — bumpy vs even ground
    tpi         elevation minus the mean of a ~500 m neighbourhood
                (+ = local rise: dry, breezy;  - = hollow: cold air pools, damp)
    twi         ln(SCA / tan(beta)) — wetness proxy, higher = wetter
    flat        fraction of a 5x5 window within +/-0.5 m of the centre cell

Usage:
    python3 tools/morgonsol/build_terrain.py [--dem-dir DIR] [--out PATH]
"""
import argparse
import glob
import json
import os

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.merge import merge as rio_merge
from rasterio.transform import Affine
from rasterio.warp import Resampling as WarpResampling
from rasterio.warp import reproject
from scipy import ndimage

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.normpath(os.path.join(HERE, "..", "..", "data", "morgonsol"))
DEFAULT_DEM_DIR = "/private/tmp/morgonsol_dem"
DEFAULT_OUT = "/private/tmp/morgonsol_dem/terrain.tif"

BANDS = [
    "elev",
    "slope",
    "aspect",
    "northness",
    "eastness",
    "rough",
    "tpi",
    "twi",
    "flat",
]


def load_grid():
    with open(os.path.join(DATA, "grid.json")) as f:
        g = json.load(f)
    g["affine"] = Affine(*g["transform"])
    return g


def mosaic_to_grid(dem_dir, grid):
    """Warp every DEM tile in dem_dir onto the target grid, in one pass."""
    paths = sorted(glob.glob(os.path.join(dem_dir, "*.tif")))
    if not paths:
        raise SystemExit("no DEM tiles found in %s" % dem_dir)
    srcs = [rasterio.open(p) for p in paths]
    try:
        mos, mos_tf = rio_merge(srcs)
        src_crs = srcs[0].crs
        src_nodata = srcs[0].nodata
    finally:
        for s in srcs:
            s.close()

    dst = np.full((grid["height"], grid["width"]), np.nan, dtype="float32")
    reproject(
        source=mos[0],
        destination=dst,
        src_transform=mos_tf,
        src_crs=src_crs,
        src_nodata=src_nodata,
        dst_transform=grid["affine"],
        dst_crs=grid["crs"],
        dst_nodata=np.nan,
        resampling=WarpResampling.bilinear,
    )
    print("mosaicked %d tile(s) -> %dx%d grid" % (len(paths), grid["height"], grid["width"]))
    return dst


def fill_nan(z):
    """Nearest-neighbour fill so the focal maths does not smear NaNs everywhere."""
    bad = np.isnan(z)
    if not bad.any():
        return z, bad
    idx = ndimage.distance_transform_edt(bad, return_distances=False, return_indices=True)
    return z[tuple(idx)], bad


def d8_flow_accumulation(z, res):
    """Vectorised D8 accumulation: sort cells high->low, push each cell's load downslope.

    The per-cell loop is unavoidable (accumulation is sequential) but the
    neighbour search is done once, up front, for the whole array.
    """
    h, w = z.shape
    n = h * w
    flat = z.ravel()

    best_slope = np.full(n, 0.0, dtype="float32")
    down = np.full(n, -1, dtype="int64")
    idx = np.arange(n).reshape(h, w)

    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            if di == 0 and dj == 0:
                continue
            dist = res * np.hypot(di, dj)
            # Slice so that source and shifted-neighbour line up.
            si = slice(max(0, -di), h - max(0, di))
            sj = slice(max(0, -dj), w - max(0, dj))
            ni = slice(max(0, di), h - max(0, -di))
            nj = slice(max(0, dj), w - max(0, -dj))

            drop = (z[si, sj] - z[ni, nj]) / dist
            src = idx[si, sj].ravel()
            tgt = idx[ni, nj].ravel()
            dr = drop.ravel()
            better = dr > best_slope[src]
            sel = src[better]
            best_slope[sel] = dr[better]
            down[sel] = tgt[better]

    acc = np.ones(n, dtype="float64")
    order = np.argsort(flat)[::-1]  # highest first
    for i in order:
        d = down[i]
        if d >= 0:
            acc[d] += acc[i]
    return acc.reshape(h, w)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dem-dir", default=DEFAULT_DEM_DIR)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    grid = load_grid()
    res = grid["res_m"]

    z_raw = mosaic_to_grid(args.dem_dir, grid)
    z, nodata_mask = fill_nan(z_raw)
    z = z.astype("float32")

    # --- gradients. Row 0 is the NORTH edge, so +row = southward, +col = eastward.
    gy_south = np.gradient(z, res, axis=0)  # dz per metre going south
    gx_east = np.gradient(z, res, axis=1)  # dz per metre going east
    mag = np.hypot(gx_east, gy_south)

    slope = np.degrees(np.arctan(mag))

    # Downhill direction: east component -gx_east, north component +gy_south.
    east_c = -gx_east
    north_c = gy_south
    with np.errstate(invalid="ignore", divide="ignore"):
        northness = np.where(mag > 1e-9, north_c / np.maximum(mag, 1e-9), 0.0)
        eastness = np.where(mag > 1e-9, east_c / np.maximum(mag, 1e-9), 0.0)
    aspect = (np.degrees(np.arctan2(east_c, north_c)) + 360.0) % 360.0
    aspect = np.where(mag > 1e-9, aspect, -1.0)  # -1 = genuinely flat, no aspect

    # --- local relief in a 5x5 (100 m) window
    k = 5
    mean5 = ndimage.uniform_filter(z, size=k, mode="nearest")
    mean5_sq = ndimage.uniform_filter(z * z, size=k, mode="nearest")
    rough = np.sqrt(np.maximum(mean5_sq - mean5 * mean5, 0.0))

    # --- topographic position: how high this cell sits vs a ~500 m neighbourhood
    big = max(3, int(round(500.0 / res)) | 1)
    tpi = z - ndimage.uniform_filter(z, size=big, mode="nearest")

    # --- flatness: share of a 5x5 window within +/-0.5 m of the centre
    within = np.zeros_like(z, dtype="float32")
    for di in range(-(k // 2), k // 2 + 1):
        for dj in range(-(k // 2), k // 2 + 1):
            shifted = np.roll(np.roll(z, di, axis=0), dj, axis=1)
            within += (np.abs(shifted - z) <= 0.5).astype("float32")
    flat = within / float(k * k)

    # --- wetness
    print("computing D8 flow accumulation over %d cells..." % z.size)
    acc = d8_flow_accumulation(z, res)
    sca = acc * (res * res) / res  # specific catchment area, contour width = res
    beta = np.radians(np.maximum(slope, 0.05))
    twi = np.log(sca / np.maximum(np.tan(beta), 1e-3))

    stack = {
        "elev": z_raw,  # keep true nodata in the elevation band
        "slope": slope,
        "aspect": aspect,
        "northness": northness,
        "eastness": eastness,
        "rough": rough,
        "tpi": tpi,
        "twi": twi,
        "flat": flat,
    }
    for name in BANDS:
        stack[name] = np.where(nodata_mask, np.nan, stack[name]).astype("float32")

    profile = {
        "driver": "GTiff",
        "height": grid["height"],
        "width": grid["width"],
        "count": len(BANDS),
        "dtype": "float32",
        "crs": grid["crs"],
        "transform": grid["affine"],
        "nodata": np.nan,
        "compress": "deflate",
        "predictor": 2,
        "tiled": True,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with rasterio.open(args.out, "w", **profile) as dst:
        for i, name in enumerate(BANDS, start=1):
            dst.write(stack[name], i)
            dst.set_band_description(i, name)

    # Record what the terrain was actually derived from, so the map's "Om" tab
    # and meta.json can never claim 1 m data while showing 30 m results.
    names_seen = sorted(os.path.basename(p) for p in glob.glob(os.path.join(args.dem_dir, "*.tif")))
    src_label = ("Lantmateriet Markhojdmodell 1 m DTM (bare earth, CC BY 4.0)"
                 if any("dtm1m" in n for n in names_seen)
                 else "Copernicus DEM GLO-30 (30 m DSM, canopy included)")
    with open(os.path.join(DATA, "dem_source.json"), "w") as f:
        json.dump({"label": src_label, "dem_dir": args.dem_dir,
                   "files": names_seen, "grid_res_m": res}, f, indent=2)
    print("wrote %s" % args.out)
    print("  source: %s" % src_label)
    for name in BANDS:
        v = stack[name][np.isfinite(stack[name])]
        if v.size:
            print(
                "  %-10s min %8.2f  median %8.2f  max %8.2f"
                % (name, v.min(), np.median(v), v.max())
            )


if __name__ == "__main__":
    main()
