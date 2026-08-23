#!/usr/bin/env python3
"""Morgonsol — put the SLU Markfuktighetskarta onto the corridor grid.

This is the layer that actually distinguishes wet ground from dry: a machine
learning model trained on 20 000 Riksskogstaxeringen plots, at 2 m, from the
national laser scan. Nothing derived from terrain alone comes close.

Two products exist and this handles both:

  classified (1-4)   1 torr-frisk, 2 frisk-fuktig, 3 fuktig-blöt, 4 öppet vatten
  continuous (0-100) 0 = dry .. 100 = wet, and 101 = open water

That 101 is an undocumented sentinel. Left unmasked, lakes read as "wetter than
the wettest land" and quietly drag their surroundings down when aggregated.

Output bands on the corridor grid:
    wet01   0 = dry .. 1 = wet   (water excluded from the average, not averaged in)
    water   fraction of the cell that is open water

Source is Skogsstyrelsen, CC0. FTP credentials are published on their own site:
  https://www.skogsstyrelsen.se/e-tjanster-och-kartor/karttjanster/geodatatjanster/ftp/

Usage:
    python3 tools/morgonsol/build_soilmoisture.py --src <local .tif>       # classified
    python3 tools/morgonsol/build_soilmoisture.py --remote-classified     # over FTP
"""
import argparse
import json
import os

# Must NOT be EMPTY_DIR for remote reads: that hides the .ovr sidecar overviews.
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "FALSE")
os.environ.setdefault("GDAL_HTTP_TIMEOUT", "300")
os.environ.setdefault("VSI_CACHE", "TRUE")
os.environ.setdefault("GDAL_CACHEMAX", "512")

import numpy as np
import rasterio
from rasterio.transform import Affine
from rasterio.warp import Resampling as WarpResampling
from rasterio.warp import reproject

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.normpath(os.path.join(HERE, "..", "..", "data", "morgonsol"))
OUT = "/private/tmp/morgonsol_dem/soil.tif"

FTP = "/vsicurl/ftp://SGD:0N%21nd%3DI9EJ@ftpsks.skogsstyrelsen.se/SLUMarkfuktighet/"
REMOTE_CLASSIFIED = FTP + "SLUMarkfuktighetskartaKlassad/SLUMarkfuktighetKlassad.tif"
REMOTE_CONTINUOUS = FTP + "SLUMarkfuktighetskarta/SLUMarkfuktighetskarta.tif"


def to_wet_and_water(arr, classified):
    """Map raw values to (wetness 0..1, is_water, is_valid), all at source resolution."""
    a = arr.astype("float32")
    if classified:
        water = a == 4
        valid = np.isin(arr, [1, 2, 3])
        wet = np.select([a == 1, a == 2, a == 3], [0.0, 0.5, 1.0], default=np.nan)
    else:
        water = a == 101
        valid = (a >= 0) & (a <= 100)
        wet = np.where(valid, a / 100.0, np.nan)
    return wet, water, valid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", help="local .tif extract of the SLU raster")
    ap.add_argument("--remote-classified", action="store_true")
    ap.add_argument("--remote-continuous", action="store_true")
    ap.add_argument("--continuous", action="store_true",
                    help="treat --src as the continuous 0-100 product")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    if args.remote_classified:
        src_path, classified = REMOTE_CLASSIFIED, True
    elif args.remote_continuous:
        src_path, classified = REMOTE_CONTINUOUS, False
    elif args.src:
        src_path, classified = args.src, not args.continuous
    else:
        raise SystemExit("give --src PATH, --remote-classified or --remote-continuous")

    with open(os.path.join(DATA, "grid.json")) as f:
        grid = json.load(f)
    H, W = grid["height"], grid["width"]
    dst_tf = Affine(*grid["transform"])

    print("source: %s" % src_path)
    print("mode  : %s" % ("classified 1-4" if classified else "continuous 0-100"))

    with rasterio.open(src_path) as src:
        print("  %d x %d @ %.1f m, overviews %s"
              % (src.width, src.height, src.transform.a, src.overviews(1) or "none"))
        raw = src.read(1)
        wet, water, valid = to_wet_and_water(raw, classified)

        # Warp each component separately. Averaging the wetness only over VALID
        # pixels — with water carried as its own fraction — stops a lake from
        # being blended into the wetness of the land around it.
        def warp(band, nodata=np.nan, resamp=WarpResampling.average):
            out = np.full((H, W), np.nan, dtype="float32")
            reproject(
                source=band.astype("float32"), destination=out,
                src_transform=src.transform, src_crs=src.crs, src_nodata=nodata,
                dst_transform=dst_tf, dst_crs=grid["crs"], dst_nodata=np.nan,
                resampling=resamp,
            )
            return out

        wet01 = warp(wet)
        water_frac = warp(np.where(water, 1.0, 0.0), nodata=None)

    cov = np.isfinite(wet01)
    print("coverage: %.2f%% of grid cells" % (100.0 * cov.mean()))
    if cov.any():
        v = wet01[cov]
        print("  wetness 0..1  median %.2f  mean %.2f" % (np.median(v), v.mean()))
        for lo, hi, lbl in [(0.0, 0.2, "torr"), (0.2, 0.6, "frisk"), (0.6, 1.01, "fuktig-blöt")]:
            share = ((v >= lo) & (v < hi)).mean() * 100
            print("    %-12s %5.1f%%" % (lbl, share))
    wf = water_frac[np.isfinite(water_frac)]
    if wf.size:
        print("  open water: %.1f%% of area" % (100.0 * wf.mean()))

    profile = {
        "driver": "GTiff", "height": H, "width": W, "count": 2, "dtype": "float32",
        "crs": grid["crs"], "transform": dst_tf, "nodata": np.nan,
        "compress": "deflate", "predictor": 2, "tiled": True,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with rasterio.open(args.out, "w", **profile) as dst:
        dst.write(np.nan_to_num(wet01, nan=np.nan), 1)
        dst.set_band_description(1, "wet01")
        dst.write(water_frac, 2)
        dst.set_band_description(2, "water")
    print("wrote %s" % args.out)


if __name__ == "__main__":
    main()
