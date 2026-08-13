#!/usr/bin/env python3
"""Rebuild the forest / canopy predictor layer -> data/layers/forest.json.

RE-RUNNABLE reconstruction of the original bulk-raster sampler. Reads the grid
(nrows/ncols + bbox) from data/layers/elevation.json's meta as the single source
of truth, so it AUTO-ADAPTS when the grid is expanded (e.g. Åre -> Åre+Krokom):
just re-run after elevation.json has been rebuilt on the new grid.

Two national NMD 2018 rasters (EPSG:3006, 10 m) are downloaded once, unzipped to
a cache dir, and read locally with rasterio (no per-cell WMS):

  * basskikt (Jämtlands län, ogeneraliserad GeoTIFF, uint8 class codes) ->
    species_class / is_forest / nmd_code, sampled at each cell CENTRE (one pixel).
  * Objekthöjd 5-45 m (national ERDAS .img/.ige, uint8 = 5 m-quantised upper-edge
    height in metres, nodata 255) -> tall_cover_frac / tree_height_mean_m,
    aggregated over ALL 10 m pixels inside each cell's footprint.

Cells whose centre falls outside basskikt coverage (Norway to the west, tile
nodata) are null across all fields (masked downstream by the kommun boundary).

Output schema matches the existing forest.json exactly:
  meta, species_class, is_forest, nmd_code, tall_cover_frac, tree_height_mean_m

The big rasters (~13 GB unzipped) are cached, NOT deleted, so a re-run on the
expanded grid is fast; the cache path is printed at the end. Set env
SVAMP_NMD_DIR to relocate the cache; set SVAMP_KEEP_RASTERS=0 to delete it.
"""
import json, math, os, sys, shutil, zipfile, urllib.request
from datetime import date

import numpy as np
import rasterio
from rasterio.warp import transform as warp_transform

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ELEV = os.path.join(HERE, "data", "layers", "elevation.json")
OUT = os.path.join(HERE, "data", "layers", "forest.json")

WORKDIR = os.environ.get("SVAMP_NMD_DIR", "/private/tmp/svampskogen_nmd")
KEEP_RASTERS = os.environ.get("SVAMP_KEEP_RASTERS", "1") != "0"

BAS_URL = ("https://geodata.naturvardsverket.se/nedladdning/marktacke/NMD2018/"
           "bas_lan_ogen/Z_lan_nmd2018bas_ogeneraliserad_v1_1.zip")
OBJ_URL = ("https://geodata.naturvardsverket.se/nedladdning/marktacke/NMD2018/"
           "Objekt_hojd_intervall_5_till_45_v1_3.zip")

BAS_TIF = "Z_lan_nmd2018bas_ogeneraliserad_v1_1.tif"
OBJ_IMG = "objekt_hojd_intervall_5_till_45_v1_3.img"
OBJ_IGE = "objekt_hojd_intervall_5_till_45_v1_3.ige"

EPSG_WGS84 = "EPSG:4326"
EPSG_SWEREF = "EPSG:3006"   # objekthöjd .img has no embedded CRS; it IS SWEREF99 TM

# ---- basskikt class code -> species_class (0..6) ---------------------------
# 111/121 pine; 112/122 spruce; 113/114/123/124 mixed conifer;
# 115/116/117/125/126/127 deciduous; 118/128 temp-non-forest (clearcut);
# any other forest code (>=111) -> 6; listed non-forest codes -> 0; else null.
NONFOREST = {2, 3, 41, 42, 51, 52, 53, 61, 62}


def species_from_nmd(code):
    if code in (111, 121):
        return 1
    if code in (112, 122):
        return 2
    if code in (113, 114, 123, 124):
        return 3
    if code in (115, 116, 117, 125, 126, 127):
        return 4
    if code in (118, 128):
        return 5
    if code >= 111:
        return 6                       # other/unspecified forest
    if code in NONFOREST:
        return 0
    return None                        # 0/nodata or anything outside coverage


# ---- download / unzip helpers ----------------------------------------------

def _download(url, dest):
    if os.path.exists(dest):
        print(f"  cached: {dest} ({os.path.getsize(dest)/1e6:.0f} MB)")
        return
    print(f"  downloading {url} -> {dest}")
    tmp = dest + ".part"
    with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, length=1 << 20)
    os.replace(tmp, dest)
    print(f"    done ({os.path.getsize(dest)/1e6:.0f} MB)")


def _extract(zip_path, members_endswith, out_dir):
    """Extract just the members whose name ends with one of the given suffixes,
    flattened into out_dir. Skips ones already present."""
    os.makedirs(out_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            base = os.path.basename(info.filename)
            if not base:
                continue
            if not any(base.endswith(s) for s in members_endswith):
                continue
            target = os.path.join(out_dir, base)
            if os.path.exists(target) and os.path.getsize(target) == info.file_size:
                continue
            print(f"    extracting {base} ({info.file_size/1e6:.0f} MB)")
            with z.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1 << 20)


def ensure_rasters():
    os.makedirs(WORKDIR, exist_ok=True)
    bas_dir = os.path.join(WORKDIR, "bas")
    obj_dir = os.path.join(WORKDIR, "obj")
    bas_tif = os.path.join(bas_dir, BAS_TIF)
    obj_img = os.path.join(obj_dir, OBJ_IMG)
    obj_ige = os.path.join(obj_dir, OBJ_IGE)

    if not os.path.exists(bas_tif):
        bas_zip = os.path.join(WORKDIR, "bas.zip")
        _download(BAS_URL, bas_zip)
        _extract(bas_zip, (BAS_TIF, BAS_TIF + "w", ".tfw"), bas_dir)
    if not (os.path.exists(obj_img) and os.path.exists(obj_ige)):
        obj_zip = os.path.join(WORKDIR, "obj.zip")
        _download(OBJ_URL, obj_zip)
        # ERDAS IMAGINE: .img header references the .ige raster blob.
        _extract(obj_zip, (OBJ_IMG, OBJ_IGE), obj_dir)

    return bas_tif, obj_img


# ---- windowed read of a raster over the whole grid bbox (into memory) ------

def read_bbox_window(ds, minx, miny, maxx, maxy, margin_px, fill):
    """Read one window covering [minx,maxx]x[miny,maxy] (dst/raster CRS coords)
    plus a margin, boundless (fill where the window runs off the raster).
    Returns (array, window_transform)."""
    T = ds.transform                      # a=+res, e=-res (north-up)
    col0 = math.floor((minx - T.c) / T.a) - margin_px
    col1 = math.floor((maxx - T.c) / T.a) + margin_px
    row0 = math.floor((maxy - T.f) / T.e) - margin_px   # top (T.e<0)
    row1 = math.floor((miny - T.f) / T.e) + margin_px   # bottom
    win = rasterio.windows.Window(col0, row0, col1 - col0 + 1, row1 - row0 + 1)
    arr = ds.read(1, window=win, boundless=True, fill_value=fill)
    wt = rasterio.windows.transform(win, T)
    return arr, wt


def col_of(x, wt):
    return np.floor((x - wt.c) / wt.a).astype(np.int64)


def row_of(y, wt):
    return np.floor((y - wt.f) / wt.e).astype(np.int64)


def main():
    e = json.load(open(ELEV))
    M = e["meta"]
    NROWS, NCOLS = M["nrows"], M["ncols"]
    NORTH, SOUTH = M["north"], M["south"]
    WEST, EAST = M["west"], M["east"]
    N = NROWS * NCOLS
    dlat = (NORTH - SOUTH) / NROWS
    dlon = (EAST - WEST) / NCOLS
    print(f"Grid {NROWS}x{NCOLS}={N} cells  bbox N{NORTH} S{SOUTH} W{WEST} E{EAST}")

    print("Ensuring NMD rasters (download+unzip if missing)…")
    bas_tif, obj_img = ensure_rasters()

    # --- reproject cell CENTRES and cell-VERTEX grid to SWEREF99 TM ----------
    # centres, row-major flat (matches elevation.json ordering)
    ci, cj = np.meshgrid(np.arange(NROWS), np.arange(NCOLS), indexing="ij")
    clat = (NORTH - (ci + 0.5) * dlat).ravel()
    clon = (WEST + (cj + 0.5) * dlon).ravel()
    cx, cy = warp_transform(EPSG_WGS84, EPSG_SWEREF, list(clon), list(clat))
    cx = np.asarray(cx); cy = np.asarray(cy)

    # vertices (NROWS+1)x(NCOLS+1): vertex (a,b) at lat=N-a*dlat, lon=W+b*dlon
    va, vb = np.meshgrid(np.arange(NROWS + 1), np.arange(NCOLS + 1), indexing="ij")
    vlat = (NORTH - va * dlat).ravel()
    vlon = (WEST + vb * dlon).ravel()
    vx, vy = warp_transform(EPSG_WGS84, EPSG_SWEREF, list(vlon), list(vlat))
    vx = np.asarray(vx).reshape(NROWS + 1, NCOLS + 1)
    vy = np.asarray(vy).reshape(NROWS + 1, NCOLS + 1)

    # per-cell footprint bbox in SWEREF (min/max over the 4 corners)
    x00, x01 = vx[:-1, :-1], vx[:-1, 1:]
    x10, x11 = vx[1:, :-1], vx[1:, 1:]
    y00, y01 = vy[:-1, :-1], vy[:-1, 1:]
    y10, y11 = vy[1:, :-1], vy[1:, 1:]
    fminx = np.minimum(np.minimum(x00, x01), np.minimum(x10, x11)).ravel()
    fmaxx = np.maximum(np.maximum(x00, x01), np.maximum(x10, x11)).ravel()
    fminy = np.minimum(np.minimum(y00, y01), np.minimum(y10, y11)).ravel()
    fmaxy = np.maximum(np.maximum(y00, y01), np.maximum(y10, y11)).ravel()

    gminx = float(vx.min()); gmaxx = float(vx.max())
    gminy = float(vy.min()); gmaxy = float(vy.max())

    species_class = [None] * N
    is_forest = [None] * N
    nmd_code = [None] * N
    tall_cover_frac = [None] * N
    tree_height_mean_m = [None] * N

    # --- basskikt: sample class code at each cell CENTRE --------------------
    print("Reading basskikt window + centre-sampling…")
    with rasterio.open(bas_tif) as bas:
        bnodata = int(bas.nodata) if bas.nodata is not None else 0
        barr, bwt = read_bbox_window(bas, gminx, gminy, gmaxx, gmaxy, 2, bnodata)
        bh, bw = barr.shape
        bc = col_of(cx, bwt); br = row_of(cy, bwt)
        inb = (br >= 0) & (br < bh) & (bc >= 0) & (bc < bw)
        codes = np.full(N, bnodata, dtype=np.int64)
        codes[inb] = barr[br[inb], bc[inb]]

    for k in range(N):
        code = int(codes[k])
        if code == bnodata:
            continue                       # outside coverage -> stays null
        sp = species_from_nmd(code)
        if sp is None:
            continue
        species_class[k] = sp
        is_forest[k] = 1 if sp in (1, 2, 3, 4, 6) else 0
        nmd_code[k] = code

    valid = [k for k in range(N) if species_class[k] is not None]
    print(f"  basskikt in-coverage cells: {len(valid)}")

    # --- objekthöjd: aggregate over each cell footprint ---------------------
    print("Reading objekthöjd window + footprint aggregation…")
    with rasterio.open(obj_img) as obj:
        onodata = int(obj.nodata) if obj.nodata is not None else 255
        oarr, owt = read_bbox_window(obj, gminx, gminy, gmaxx, gmaxy, 4, onodata)
        oh, ow = oarr.shape
        # footprint bbox -> array index ranges (inclusive)
        c0 = np.clip(col_of(fminx, owt), 0, ow - 1)
        c1 = np.clip(col_of(fmaxx, owt), 0, ow - 1)
        r0 = np.clip(row_of(fmaxy, owt), 0, oh - 1)   # top row
        r1 = np.clip(row_of(fminy, owt), 0, oh - 1)   # bottom row

        for k in valid:
            sub = oarr[r0[k]:r1[k] + 1, c0[k]:c1[k] + 1]
            if sub.size == 0:
                tall_cover_frac[k] = 0.0
                continue
            tall = (sub != 0) & (sub != onodata)
            ntall = int(tall.sum())
            tall_cover_frac[k] = round(ntall / sub.size, 4)
            if ntall:
                tree_height_mean_m[k] = round(float(sub[tall].mean()), 2)

    # --- meta: carry the rich existing block forward, update grid fields ----
    region = M.get("region", "").replace(" (boundary-masked)", "").strip()
    old = json.load(open(OUT))["meta"] if os.path.exists(OUT) else {}
    meta = dict(old)
    meta.update({
        "nrows": NROWS, "ncols": NCOLS,
        "north": NORTH, "south": SOUTH, "west": WEST, "east": EAST,
        "built": date.today().isoformat(),
        "region": region or old.get("region", "Åre kommun"),
    })
    # ensure the rich descriptive fields exist even on a first-ever build
    meta.setdefault("sources", {
        "landcover": ("Naturvårdsverket NMD 2018 basskikt (ogeneraliserad), "
                      "Jämtlands län (Z): " + BAS_URL + " -- GeoTIFF, EPSG:3006, "
                      "10 m single-band class codes. Sampled at each grid-cell "
                      "CENTRE (one 10 m pixel per cell)."),
        "canopy": ("Naturvårdsverket NMD 2018 tilläggsskikt Objekthöjd 5-45 m "
                   "(national): " + OBJ_URL + " -- ERDAS IMAGINE .img/.ige, "
                   "EPSG:3006, 10 m, uint8 = upper edge of a 5 m tall-object "
                   "height bin in metres, nodata 255. Aggregated over ALL pixels "
                   "inside each cell footprint."),
    })

    out = {
        "meta": meta,
        "species_class": species_class,
        "is_forest": is_forest,
        "nmd_code": nmd_code,
        "tall_cover_frac": tall_cover_frac,
        "tree_height_mean_m": tree_height_mean_m,
    }
    json.dump(out, open(OUT, "w"), separators=(",", ":"))
    nf = sum(1 for v in is_forest if v == 1)
    print(f"Wrote {OUT}: {len(valid)} in-coverage cells, {nf} forest.")
    print(f"NMD raster cache: {WORKDIR}"
          + ("" if KEEP_RASTERS else " (deleting…)"))
    if not KEEP_RASTERS:
        shutil.rmtree(WORKDIR, ignore_errors=True)


if __name__ == "__main__":
    main()
