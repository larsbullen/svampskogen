#!/usr/bin/env python3
"""Morgonsol — add a trail segment to the scored corridor.

The corridor is whatever ground you could plausibly reach from a route. A GPX
loop is one route; a connecting trail you might take between two huts is
another. This finds the actual path through the OpenStreetMap trail network
(not a straight line) and widens the corridor to include it.

Cheap to run: the raster grid is a rectangle covering the whole area, so
terrain, sun and soil already exist everywhere. Only the corridor MASK changes,
so just re-run build_score.py afterwards.

Endpoints can be hut names from huts.geojson or raw lat,lon pairs.

Usage:
    python3 tools/morgonsol/add_segment.py --from Stensdalsstugorna --to Vålåstugorna
    python3 tools/morgonsol/add_segment.py --from 63.1,12.7 --to 63.0,12.8 --name "min genväg"
"""
import argparse
import heapq
import json
import math
import os

from pyproj import Transformer
from shapely.geometry import LineString, Point, mapping, shape
from shapely.ops import transform as shp_transform
from shapely.ops import unary_union

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.normpath(os.path.join(HERE, "..", "..", "data", "morgonsol"))

to_sweref = Transformer.from_crs("EPSG:4326", "EPSG:3006", always_xy=True).transform
to_wgs84 = Transformer.from_crs("EPSG:3006", "EPSG:4326", always_xy=True).transform

SNAP_M = 3.0  # OSM ways usually share exact junction nodes; this absorbs the rest


def key(x, y):
    return (round(x / SNAP_M), round(y / SNAP_M))


def build_graph(trail_geoms):
    """Undirected graph over trail vertices, snapped so junctions actually join."""
    graph = {}
    coords = {}

    def add(a, b):
        ka, kb = key(*a), key(*b)
        if ka == kb:
            return
        coords.setdefault(ka, a)
        coords.setdefault(kb, b)
        w = math.dist(a, b)
        graph.setdefault(ka, []).append((kb, w))
        graph.setdefault(kb, []).append((ka, w))

    for g in trail_geoms:
        lines = g.geoms if g.geom_type == "MultiLineString" else [g]
        for ln in lines:
            pts = list(ln.coords)
            for i in range(1, len(pts)):
                add(pts[i - 1][:2], pts[i][:2])
    return graph, coords


def nearest_node(coords, pt):
    best, bd = None, float("inf")
    for k, c in coords.items():
        d = math.dist(c, pt)
        if d < bd:
            best, bd = k, d
    return best, bd


def dijkstra(graph, start, goal):
    dist = {start: 0.0}
    prev = {}
    pq = [(0.0, start)]
    seen = set()
    while pq:
        d, u = heapq.heappop(pq)
        if u in seen:
            continue
        seen.add(u)
        if u == goal:
            break
        for v, w in graph.get(u, ()):
            nd = d + w
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    if goal not in dist:
        return None, None
    path, cur = [goal], goal
    while cur != start:
        cur = prev[cur]
        path.append(cur)
    return list(reversed(path)), dist[goal]


def resolve_point(text, huts):
    """A hut name (substring, case-insensitive) or 'lat,lon'."""
    if "," in text:
        lat, lon = (float(v) for v in text.split(",", 1))
        return (lon, lat), text
    low = text.lower()
    for name, lonlat in huts.items():
        if low in name.lower():
            return lonlat, name
    raise SystemExit("no hut matching %r. Known: %s" % (text, ", ".join(sorted(huts))))


def load_geoms(path, types=("LineString", "MultiLineString")):
    full = os.path.join(DATA, path)
    if not os.path.exists(full):
        return []
    with open(full) as f:
        gj = json.load(f)
    out = []
    for ft in gj.get("features", []):
        g = ft.get("geometry")
        if not g or g.get("type") not in types:
            continue
        try:
            out.append(shp_transform(to_sweref, shape(g)))
        except Exception:
            pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", required=True)
    ap.add_argument("--to", dest="dst", required=True)
    ap.add_argument("--name", default=None)
    ap.add_argument("--buffer-m", type=float, default=None,
                    help="corridor width for this segment (default: same as the route)")
    args = ap.parse_args()

    with open(os.path.join(DATA, "grid.json")) as f:
        grid = json.load(f)
    buf = args.buffer_m if args.buffer_m is not None else grid.get("buffer_m", 2000.0)

    huts = {}
    hp = os.path.join(DATA, "huts.geojson")
    if os.path.exists(hp):
        with open(hp) as f:
            for ft in json.load(f)["features"]:
                n = (ft.get("properties") or {}).get("name")
                if n and ft["geometry"]["type"] == "Point":
                    huts[n] = tuple(ft["geometry"]["coordinates"][:2])

    src_ll, src_name = resolve_point(args.src, huts)
    dst_ll, dst_name = resolve_point(args.dst, huts)
    seg_name = args.name or ("%s – %s" % (src_name, dst_name))
    print("routing: %s -> %s" % (src_name, dst_name))

    trails = load_geoms("trails.geojson")
    if not trails:
        raise SystemExit("no trails.geojson — run pull_osm.py first")
    graph, coords = build_graph(trails)
    print("trail network: %d nodes, %d edges"
          % (len(graph), sum(len(v) for v in graph.values()) // 2))

    a_xy = to_sweref(*src_ll)
    b_xy = to_sweref(*dst_ll)
    a_node, a_off = nearest_node(coords, a_xy)
    b_node, b_off = nearest_node(coords, b_xy)
    print("snapped endpoints to the network: %.0f m and %.0f m off" % (a_off, b_off))
    if max(a_off, b_off) > 500:
        print("  WARNING: an endpoint is far from any mapped trail")

    path, length = dijkstra(graph, a_node, b_node)
    if not path:
        raise SystemExit(
            "no connected trail route between those points in OpenStreetMap.\n"
            "The network may be broken there; pass --from/--to as lat,lon on a "
            "single continuous trail, or widen the segment manually.")
    pts = [coords[k] for k in path]
    line_sw = LineString([a_xy] + pts + [b_xy])
    print("path found: %.2f km along %d trail vertices" % (length / 1000.0, len(pts)))

    # Straight-line comparison, to catch a Dijkstra result that wandered.
    direct = math.dist(a_xy, b_xy)
    print("  straight line %.2f km, detour factor %.2f"
          % (direct / 1000.0, length / max(direct, 1.0)))

    # --- append to route.geojson
    rp = os.path.join(DATA, "route.geojson")
    with open(rp) as f:
        rgj = json.load(f)
    rgj["features"] = [ft for ft in rgj["features"]
                       if (ft.get("properties") or {}).get("name") != seg_name]
    rgj["features"].append({
        "type": "Feature",
        "properties": {"name": seg_name, "kind": "segment",
                       "length_km": round(length / 1000.0, 2), "buffer_m": buf},
        "geometry": mapping(shp_transform(to_wgs84, line_sw.simplify(10.0))),
    })
    with open(rp, "w") as f:
        json.dump(rgj, f)

    # --- corridor = union of every route line's buffer
    lines = [shp_transform(to_sweref, shape(ft["geometry"]))
             for ft in rgj["features"] if ft["geometry"]["type"] == "LineString"]
    corridor = unary_union([ln.buffer(buf, resolution=8) for ln in lines])
    with open(os.path.join(DATA, "corridor.geojson"), "w") as f:
        json.dump({"type": "FeatureCollection", "features": [{
            "type": "Feature",
            "properties": {"buffer_m": buf, "routes": len(lines)},
            "geometry": mapping(shp_transform(to_wgs84, corridor)),
        }]}, f)

    print("corridor now %.0f km2 across %d route line(s)"
          % (corridor.area / 1e6, len(lines)))
    print("\nNext: python3 tools/morgonsol/build_score.py")


if __name__ == "__main__":
    main()
