#!/usr/bin/env python3
"""Morgonsol step 1 — turn a GPX route into a route line, a corridor polygon and a grid definition.

The corridor is what bounds every later step: we only score ground you could
plausibly walk to from the route, not the whole reserve.

Usage:
    python3 tools/morgonsol/prep_route.py <route.gpx> [--buffer-m 2000] [--res 20]
"""
import argparse
import json
import math
import os
import xml.etree.ElementTree as ET

from pyproj import Transformer
from shapely.geometry import LineString, mapping, shape
from shapely.ops import transform as shp_transform

GPX_NS = {"g": "http://www.topografix.com/GPX/1/1"}
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.normpath(os.path.join(HERE, "..", "..", "data", "morgonsol"))

WGS84 = "EPSG:4326"
SWEREF = "EPSG:3006"  # SWEREF99 TM, metres — everything downstream is in this
to_sweref = Transformer.from_crs(WGS84, SWEREF, always_xy=True).transform
to_wgs84 = Transformer.from_crs(SWEREF, WGS84, always_xy=True).transform


def read_gpx(path):
    """Return (track points [(lon, lat, ele)], waypoints [(lon, lat, name)])."""
    root = ET.parse(path).getroot()
    pts = []
    for trk in root.findall("g:trk", GPX_NS):
        for seg in trk.findall("g:trkseg", GPX_NS):
            for tp in seg.findall("g:trkpt", GPX_NS):
                ele = tp.findtext("g:ele", default=None, namespaces=GPX_NS)
                pts.append(
                    (
                        float(tp.get("lon")),
                        float(tp.get("lat")),
                        float(ele) if ele else None,
                    )
                )
    wpts = [
        (
            float(w.get("lon")),
            float(w.get("lat")),
            w.findtext("g:name", default="", namespaces=GPX_NS),
        )
        for w in root.findall("g:wpt", GPX_NS)
    ]
    name = root.findtext("g:metadata/g:name", default="", namespaces=GPX_NS)
    return pts, wpts, name


def haversine(a, b):
    lon1, lat1 = a[0], a[1]
    lon2, lat2 = b[0], b[1]
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def cumulative_km(pts):
    """Distance along route in km at each vertex — used to label sites by trail position."""
    out = [0.0]
    for i in range(1, len(pts)):
        out.append(out[-1] + haversine(pts[i - 1], pts[i]))
    return [d / 1000.0 for d in out]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gpx")
    ap.add_argument("--buffer-m", type=float, default=2000.0)
    ap.add_argument("--res", type=float, default=20.0, help="grid cell size in metres")
    args = ap.parse_args()

    pts, wpts, name = read_gpx(args.gpx)
    if len(pts) < 2:
        raise SystemExit("no track points in %s" % args.gpx)

    km = cumulative_km(pts)
    total_km = km[-1]
    eles = [p[2] for p in pts if p[2] is not None]
    gain = sum(
        max(0.0, pts[i][2] - pts[i - 1][2])
        for i in range(1, len(pts))
        if pts[i][2] is not None and pts[i - 1][2] is not None
    )

    line_wgs = LineString([(p[0], p[1]) for p in pts])
    # Simplify in projected space so the tolerance is in real metres.
    line_sw = shp_transform(to_sweref, line_wgs)
    line_sw_simple = line_sw.simplify(15.0, preserve_topology=False)
    corridor_sw = line_sw.buffer(args.buffer_m, resolution=8)

    # Grid definition, snapped outward to whole cells.
    minx, miny, maxx, maxy = corridor_sw.bounds
    res = args.res
    minx = math.floor(minx / res) * res
    miny = math.floor(miny / res) * res
    maxx = math.ceil(maxx / res) * res
    maxy = math.ceil(maxy / res) * res
    width = int(round((maxx - minx) / res))
    height = int(round((maxy - miny) / res))

    os.makedirs(DATA, exist_ok=True)

    route_fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "name": name or "route",
                    "length_km": round(total_km, 2),
                    "ascent_m": round(gain),
                    "ele_min": round(min(eles)) if eles else None,
                    "ele_max": round(max(eles)) if eles else None,
                },
                "geometry": mapping(shp_transform(to_wgs84, line_sw_simple)),
            }
        ]
        + [
            {
                "type": "Feature",
                "properties": {"name": w[2], "kind": "waypoint"},
                "geometry": {"type": "Point", "coordinates": [w[0], w[1]]},
            }
            for w in wpts
        ],
    }
    with open(os.path.join(DATA, "route.geojson"), "w") as f:
        json.dump(route_fc, f)

    corridor_fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"buffer_m": args.buffer_m},
                "geometry": mapping(shp_transform(to_wgs84, corridor_sw)),
            }
        ],
    }
    with open(os.path.join(DATA, "corridor.geojson"), "w") as f:
        json.dump(corridor_fc, f)

    w, s = to_wgs84(minx, miny)
    e, n = to_wgs84(maxx, maxy)
    grid = {
        "crs": SWEREF,
        "res_m": res,
        "transform": [res, 0.0, minx, 0.0, -res, maxy],
        "width": width,
        "height": height,
        "bounds_sweref": [minx, miny, maxx, maxy],
        "bounds_wgs84": [w, s, e, n],
        "buffer_m": args.buffer_m,
        "route": {
            "name": name,
            "length_km": round(total_km, 2),
            "ascent_m": round(gain),
            "ele_min": round(min(eles)) if eles else None,
            "ele_max": round(max(eles)) if eles else None,
            "n_points": len(pts),
        },
    }
    with open(os.path.join(DATA, "grid.json"), "w") as f:
        json.dump(grid, f, indent=2)

    print("route      : %s" % (name or "(unnamed)"))
    print("length     : %.1f km, +%d m, %d-%d m a.s.l." % (total_km, gain, min(eles), max(eles)))
    print("corridor   : %.0f m buffer, %.0f km2" % (args.buffer_m, corridor_sw.area / 1e6))
    print("grid       : %d x %d cells @ %.0f m (%s)" % (width, height, res, SWEREF))
    print("bounds wgs : %.4f %.4f %.4f %.4f" % (w, s, e, n))
    print("wrote      : route.geojson corridor.geojson grid.json -> %s" % DATA)


if __name__ == "__main__":
    main()
