#!/usr/bin/env python3
"""Morgonsol — derive tree cover by subtracting bare earth from the surface model.

We hold two elevation models of the same ground: Copernicus GLO-30, a SURFACE
model that measures treetops, and Lantmäteriet's 1 m lidar DTM, which is bare
earth. Their difference is canopy. Nothing else in the pipeline knows where the
trees are, and for a campsite that matters as much as the ground does — shelter
from wind, a screen from the trail, and the difference between a cold night in
the open and a still one under birch.

Per pixel the difference is noisy: the two models are from different epochs and
30 m vs 1 m, so it produces impossible negative canopy about a quarter of the
time. Aggregated it is solid — the treeline shows up exactly where it should.
So this reports a local FRACTION of tree-covered ground, not a height, and that
fraction is the thing worth trusting.

Outputs canopy.tif with:
    canopy_m    raw DSM - DTM, for inspection only
    tree_frac   share of a ~150 m neighbourhood with canopy over TREE_M

Usage:
    python3 tools/morgonsol/build_canopy.py
"""
import glob
import json
import os

import numpy as np
import rasterio
from rasterio.merge import merge as rio_merge
from rasterio.transform import Affine
from rasterio.warp import Resampling as WarpResampling
from rasterio.warp import reproject
from scipy import ndimage

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.normpath(os.path.join(HERE, "..", "..", "data", "morgonsol"))
DTM = "/private/tmp/morgonsol_dem_1m/dtm1m_corridor.tif"
DSM_GLOB = "/private/tmp/morgonsol_dem/Copernicus_DSM_*.tif"
OUT = "/private/tmp/morgonsol_dem/canopy.tif"

TREE_M = 3.0        # canopy height that counts as "trees"
NEIGHBOURHOOD_M = 150.0


def main():
    with open(os.path.join(DATA, "grid.json")) as f:
        grid = json.load(f)
    H, W = grid["height"], grid["width"]
    res = grid["res_m"]
    tf = Affine(*grid["transform"])

    with rasterio.open(DTM) as s:
        dtm = s.read(1)

    paths = sorted(glob.glob(DSM_GLOB))
    if not paths:
        raise SystemExit("no Copernicus DSM tiles in %s" % DSM_GLOB)
    srcs = [rasterio.open(p) for p in paths]
    try:
        mos, mtf = rio_merge(srcs)
        crs, nod = srcs[0].crs, srcs[0].nodata
    finally:
        for s in srcs:
            s.close()

    dsm = np.full((H, W), np.nan, dtype="float32")
    reproject(source=mos[0], destination=dsm, src_transform=mtf, src_crs=crs,
              src_nodata=nod, dst_transform=tf, dst_crs=grid["crs"],
              dst_nodata=np.nan, resampling=WarpResampling.bilinear)

    canopy = (dsm - dtm).astype("float32")
    valid = np.isfinite(canopy)

    is_tree = np.where(valid, (canopy > TREE_M).astype("float32"), np.nan)
    k = max(3, int(round(NEIGHBOURHOOD_M / res)) | 1)
    tree_frac = ndimage.uniform_filter(np.nan_to_num(is_tree, nan=0.0), size=k,
                                       mode="nearest")
    tree_frac = np.where(valid, tree_frac, np.nan).astype("float32")

    print("canopy from %d DSM tiles, %d m neighbourhood" % (len(paths), NEIGHBOURHOOD_M))
    for lo, hi in ((500, 700), (700, 800), (800, 900), (900, 1100), (1100, 1700)):
        m = valid & (dtm >= lo) & (dtm < hi)
        if m.sum() > 1000:
            print("  %4d-%4d m : tree_frac median %.2f, %5.1f%% of cells over 0.25"
                  % (lo, hi, np.median(tree_frac[m]), 100 * (tree_frac[m] > 0.25).mean()))

    profile = {"driver": "GTiff", "height": H, "width": W, "count": 2,
               "dtype": "float32", "crs": grid["crs"], "transform": tf,
               "nodata": np.nan, "compress": "deflate", "predictor": 2, "tiled": True}
    with rasterio.open(OUT, "w", **profile) as d:
        d.write(canopy, 1)
        d.set_band_description(1, "canopy_m")
        d.write(tree_frac, 2)
        d.set_band_description(2, "tree_frac")
    print("wrote %s" % OUT)


if __name__ == "__main__":
    main()
