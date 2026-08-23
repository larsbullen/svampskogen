#!/usr/bin/env python3
"""Morgonsol — fetch Lantmäteriet's 1 m lidar DTM for the corridor.

Why this matters more than any other upgrade: the fallback DEM is Copernicus
GLO-30, a 30 m *surface* model. One pixel is bigger than a campsite, and it
measures treetops, so below the treeline "slope" and "evenness" describe the
canopy rather than the ground. This is 1 m *bare earth* — the difference
between "this area is roughly flat" and "you can pitch here".

Data: Markhöjdmodell, CC BY 4.0, free (EU High-Value Dataset). The STAC catalogue
is open; only the raster bytes need HTTP Basic credentials from a free Geotorget
account. Credentials live in the macOS keychain, never in this repo:

    security add-generic-password -U -a "<your-geotorget-email>" \
        -s lantmateriet-geotorget -w "<password>"

Usage:
    python3 tools/morgonsol/pull_lantmateriet_dem.py [--out-dir DIR] [--collection dtm-cog]
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.normpath(os.path.join(HERE, "..", "..", "data", "morgonsol"))
STAC = "https://api.lantmateriet.se/stac-hojd/v1"
KEYCHAIN_SERVICE = "lantmateriet-geotorget"
DEFAULT_OUT = "/private/tmp/morgonsol_dem_1m"


def keychain_creds():
    """Return (username, password) from the keychain, or exit with instructions."""
    try:
        meta = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE],
            capture_output=True, text=True, check=True,
        ).stderr + subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE],
            capture_output=True, text=True, check=True,
        ).stdout
        user = None
        for line in meta.splitlines():
            if '"acct"<blob>=' in line:
                user = line.split('="', 1)[1].rstrip('"')
        pw = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if not user or not pw:
            raise ValueError("incomplete keychain entry")
        return user, pw
    except Exception:
        sys.exit(
            "No Geotorget credentials in the keychain. Add them with:\n"
            '  security add-generic-password -U -a "<email>" -s %s -w "<password>"'
            % KEYCHAIN_SERVICE
        )


def stac_search(bbox, collection, limit=500):
    """Page through the open STAC search and collect .tif asset hrefs."""
    hrefs, page, seen = [], 0, set()
    body = {"bbox": bbox, "limit": 100}
    if collection:
        body["collections"] = [collection]
    url = STAC + "/search"
    while url and page < 20:
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            doc = json.load(r)
        for ft in doc.get("features", []):
            for asset in (ft.get("assets") or {}).values():
                href = asset.get("href", "")
                if href.endswith(".tif") and href not in seen:
                    seen.add(href)
                    hrefs.append(href)
        nxt = None
        for link in doc.get("links", []):
            if link.get("rel") == "next":
                nxt = link
        if not nxt or len(hrefs) >= limit:
            break
        url = nxt.get("href", url)
        body = nxt.get("body", body)
        page += 1
    return hrefs


def window_warp(hrefs, user, pw, out_dir):
    """Read only the corridor out of each remote COG and warp it onto the grid.

    The 12 tiles total ~15 GB, but the corridor is a thin loop through them and
    the COGs are internally tiled with overviews — so /vsicurl/ fetches just the
    blocks we touch. Downloading whole tiles would move a thousand times more
    bytes than we use.
    """
    import numpy as np
    import rasterio
    from rasterio.transform import Affine
    from rasterio.warp import Resampling as WarpResampling
    from rasterio.warp import reproject

    os.environ["GDAL_HTTP_USERPWD"] = "%s:%s" % (user, pw)
    os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif")
    os.environ.setdefault("GDAL_HTTP_TIMEOUT", "300")
    os.environ.setdefault("VSI_CACHE", "TRUE")
    os.environ.setdefault("GDAL_CACHEMAX", "512")

    with open(os.path.join(DATA, "grid.json")) as f:
        grid = json.load(f)
    H, W = grid["height"], grid["width"]
    dst_tf = Affine(*grid["transform"])
    out = np.full((H, W), np.nan, dtype="float32")

    for i, href in enumerate(hrefs, start=1):
        url = "/vsicurl/" + href
        try:
            with rasterio.open(url) as src:
                tmp = np.full((H, W), np.nan, dtype="float32")
                reproject(
                    source=rasterio.band(src, 1),
                    destination=tmp,
                    src_nodata=src.nodata,
                    dst_transform=dst_tf,
                    dst_crs=grid["crs"],
                    dst_nodata=np.nan,
                    resampling=WarpResampling.average,
                    num_threads=4,
                )
            fresh = np.isfinite(tmp) & ~np.isfinite(out)
            out[fresh] = tmp[fresh]
            print("  [%2d/%d] %-22s  +%d cells  (%.1f%% filled)"
                  % (i, len(hrefs), os.path.basename(href), int(fresh.sum()),
                     100.0 * np.isfinite(out).mean()), flush=True)
        except Exception as e:
            print("  [%2d/%d] FAILED %s: %s" % (i, len(hrefs), os.path.basename(href), e))

    cov = np.isfinite(out)
    print("coverage %.2f%% of grid" % (100.0 * cov.mean()))
    if cov.any():
        v = out[cov]
        print("  elevation %.0f .. %.0f m" % (v.min(), v.max()))

    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, "dtm1m_corridor.tif")
    profile = {
        "driver": "GTiff", "height": H, "width": W, "count": 1, "dtype": "float32",
        "crs": grid["crs"], "transform": dst_tf, "nodata": np.nan,
        "compress": "deflate", "predictor": 2, "tiled": True,
    }
    with rasterio.open(dest, "w", **profile) as d:
        d.write(out, 1)
        d.set_band_description(1, "elev")
    print("wrote %s" % dest)
    print("\nNext:")
    print("  python3 tools/morgonsol/build_terrain.py --dem-dir %s" % out_dir)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=DEFAULT_OUT)
    ap.add_argument("--full-tiles", action="store_true",
                    help="download whole 1 GB tiles instead of window-reading the corridor")
    ap.add_argument("--collection", default="dtm-cog",
                    help="dtm-cog = large COGs; or an mhm-XX_Y collection of 2.5 km tiles")
    ap.add_argument("--list-only", action="store_true")
    args = ap.parse_args()

    with open(os.path.join(DATA, "grid.json")) as f:
        bbox = json.load(f)["bounds_wgs84"]

    hrefs = stac_search(bbox, args.collection)
    print("STAC: %d tif assets in %s covering the corridor" % (len(hrefs), args.collection))
    if not hrefs:
        sys.exit("no tiles found — check the collection name")
    if args.list_only:
        for h in hrefs:
            print("  " + h)
        return

    user, pw = keychain_creds()

    if not args.full_tiles:
        window_warp(hrefs, user, pw, args.out_dir)
        return

    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, "https://dl1.lantmateriet.se/", user, pw)
    opener = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(mgr))

    os.makedirs(args.out_dir, exist_ok=True)
    ok = failed = skipped = 0
    for i, href in enumerate(hrefs, start=1):
        dest = os.path.join(args.out_dir, os.path.basename(href))
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            skipped += 1
            continue
        try:
            with opener.open(href, timeout=600) as r, open(dest, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            ok += 1
            print("  [%d/%d] %s  %.1f MB" % (i, len(hrefs), os.path.basename(href),
                                             os.path.getsize(dest) / 1e6), flush=True)
        except urllib.error.HTTPError as e:
            if os.path.exists(dest):
                os.remove(dest)
            failed += 1
            if e.code == 403:
                sys.exit(
                    "\n403 Forbidden — the credentials authenticate (a wrong password gives 401),\n"
                    "but this account has no entitlement to Markhöjdmodell yet.\n"
                    "Log in at https://geotorget.lantmateriet.se and order/request access to\n"
                    "'Markhöjdmodell nedladdning', then re-run this script."
                )
            if e.code == 401:
                sys.exit("401 Unauthorized — the keychain credentials are wrong.")
            print("  [%d/%d] HTTP %s on %s" % (i, len(hrefs), e.code, href))
        except Exception as e:
            failed += 1
            print("  [%d/%d] %s on %s" % (i, len(hrefs), e, href))

    print("downloaded %d, already had %d, failed %d -> %s" % (ok, skipped, failed, args.out_dir))
    if ok or skipped:
        print("\nNext: rebuild the terrain layers off the 1 m data:")
        print("  python3 tools/morgonsol/build_terrain.py --dem-dir %s" % args.out_dir)
        print("  python3 tools/morgonsol/build_sun.py --date <trip date>")
        print("  python3 tools/morgonsol/build_score.py")


if __name__ == "__main__":
    main()
