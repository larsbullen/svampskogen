#!/usr/bin/env python3
"""
pull_osm.py -- reusable OSM Overpass -> GeoJSON downloader.

Pure Python standard library only (urllib, json). No requests, no osmtogeojson,
no geopandas/shapely required.

Downloads themed feature categories (water, wetland, trails, huts, barriers)
for an arbitrary WGS84 bounding box and writes one GeoJSON FeatureCollection
per category.

Usage
-----
  # defaults: Vålådalen bbox -> data/morgonsol/
  python3 pull_osm.py

  # explicit bbox and output dir
  python3 pull_osm.py --south 62.957 --west 12.480 --north 63.165 --east 13.181 \
                      --out /path/to/outdir

  # only some categories
  python3 pull_osm.py --categories water wetland

  # as a library
  from pull_osm import fetch_category, BBox
  fc = fetch_category("water", BBox(62.957, 12.480, 63.165, 13.181))

Notes
-----
* Queries are issued ONE AT A TIME with a polite pause between them.
* HTTP 429 / 504 are retried with exponential backoff; after repeated
  failures the script falls back to the kumi.systems mirror.
* Overpass is queried with `out geom;` so ways and relations carry inline
  coordinates; GeoJSON geometry is assembled here (rings stitched for
  multipolygon relations).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import namedtuple

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

USER_AGENT = "svampfinder-morgonsol/1.0 (tent-site suitability analysis; stdlib urllib)"

DEFAULT_BBOX = (62.957, 12.480, 63.165, 13.181)  # south, west, north, east
DEFAULT_OUT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "morgonsol",
)

BBox = namedtuple("BBox", "south west north east")

# Overpass QL statement bodies. `{bbox}` is substituted with "s,w,n,e".
# Each body must leave results in the default set; the runner appends `out geom;`.
CATEGORIES = {
    # 1. Hydrography ------------------------------------------------------
    "water": """
      (
        node["natural"="water"]({bbox});
        way["natural"="water"]({bbox});
        relation["natural"="water"]({bbox});
        way["waterway"~"^(river|stream|brook)$"]({bbox});
        relation["waterway"~"^(river|stream|brook)$"]({bbox});
        node["natural"="spring"]({bbox});
        way["natural"="spring"]({bbox});
      );
    """,
    # 2. Wetland (hard-exclude) -------------------------------------------
    "wetland": """
      (
        node["natural"~"^(wetland|marsh|bog)$"]({bbox});
        way["natural"~"^(wetland|marsh|bog)$"]({bbox});
        relation["natural"~"^(wetland|marsh|bog)$"]({bbox});
      );
    """,
    # 3. Trails ------------------------------------------------------------
    "trails": """
      (
        way["highway"~"^(path|footway|track)$"]({bbox});
        relation["route"="hiking"]({bbox});
      );
    """,
    # 4. Huts / shelters ---------------------------------------------------
    "huts": """
      (
        nwr["tourism"="alpine_hut"]({bbox});
        nwr["tourism"="wilderness_hut"]({bbox});
        nwr["amenity"="shelter"]({bbox});
        nwr["tourism"="chalet"]({bbox});
        nwr["building"="cabin"]({bbox});
      );
    """,
    # 5. Barriers / reindeer fencing --------------------------------------
    "barriers": """
      (
        nwr["barrier"="fence"]({bbox});
        nwr["barrier"="wall"]["wall"~"reindeer",i]({bbox});
        nwr["fence_type"~"reindeer",i]({bbox});
        nwr["barrier"]["reindeer"]({bbox});
        nwr["barrier"]["name"~"ren(gärde|stängsel|hägn)",i]({bbox});
      );
    """,
}

# Tags that make a *closed* way an area rather than a ring-shaped line.
AREA_TAGS = {
    "natural": {"water", "wetland", "marsh", "bog", "spring", "scrub", "wood"},
    "landuse": None,   # any value
    "building": None,
    "leisure": None,
    "amenity": None,
    "tourism": None,
    "waterway": {"riverbank", "dock"},
}


# --------------------------------------------------------------------------
# Overpass transport
# --------------------------------------------------------------------------

def build_query(body, bbox, timeout=300):
    bbox_str = "{:.6f},{:.6f},{:.6f},{:.6f}".format(
        bbox.south, bbox.west, bbox.north, bbox.east
    )
    return (
        "[out:json][timeout:{t}];\n{body}\nout geom;\n".format(
            t=timeout, body=body.format(bbox=bbox_str).strip()
        )
    )


def overpass(query, max_attempts=5, base_delay=8.0, verbose=True):
    """POST a query to Overpass, retrying on 429/504 and falling back to mirrors."""
    last_err = None
    attempt = 0
    for endpoint in ENDPOINTS:
        for _ in range(max_attempts):
            attempt += 1
            req = urllib.request.Request(
                endpoint,
                data=query.encode("utf-8"),
                headers={
                    "User-Agent": USER_AGENT,
                    "Content-Type": "text/plain; charset=utf-8",
                    "Accept": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=360) as resp:
                    raw = resp.read()
                return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in (429, 502, 503, 504):
                    delay = base_delay * (2 ** min(attempt - 1, 4))
                    if verbose:
                        print(
                            "    HTTP {} from {} -- backing off {:.0f}s".format(
                                e.code, endpoint.split("/")[2], delay
                            ),
                            file=sys.stderr,
                        )
                    time.sleep(delay)
                    continue
                if verbose:
                    print("    HTTP {}: {}".format(e.code, e.reason), file=sys.stderr)
                break
            except (urllib.error.URLError, OSError, ValueError) as e:
                last_err = e
                delay = base_delay * (2 ** min(attempt - 1, 3))
                if verbose:
                    print("    {} -- retry in {:.0f}s".format(e, delay), file=sys.stderr)
                time.sleep(delay)
        if verbose:
            print("    switching endpoint...", file=sys.stderr)
    raise RuntimeError("Overpass failed on all endpoints: {}".format(last_err))


# --------------------------------------------------------------------------
# OSM -> GeoJSON geometry construction
# --------------------------------------------------------------------------

def _coords(geom):
    """Overpass `geometry` list -> [[lon, lat], ...], dropping unresolved nodes."""
    return [[p["lon"], p["lat"]] for p in geom or [] if p and "lon" in p and "lat" in p]


def _is_closed(ring):
    return len(ring) >= 4 and ring[0] == ring[-1]


def _looks_like_area(tags):
    if tags.get("area") == "yes":
        return True
    if tags.get("area") == "no":
        return False
    for key, values in AREA_TAGS.items():
        if key in tags:
            if values is None or tags[key] in values:
                return True
    return False


def _stitch(ways):
    """Join member way coordinate lists into closed rings where possible.

    Returns (closed_rings, dangling_lines).
    """
    segments = [list(w) for w in ways if len(w) >= 2]
    rings, dangling = [], []

    while segments:
        cur = segments.pop(0)
        if _is_closed(cur):
            rings.append(cur)
            continue
        progressed = True
        while progressed and not _is_closed(cur):
            progressed = False
            for i, seg in enumerate(segments):
                if cur[-1] == seg[0]:
                    cur = cur + seg[1:]
                elif cur[-1] == seg[-1]:
                    cur = cur + list(reversed(seg))[1:]
                elif cur[0] == seg[-1]:
                    cur = seg[:-1] + cur
                elif cur[0] == seg[0]:
                    cur = list(reversed(seg))[:-1] + cur
                else:
                    continue
                segments.pop(i)
                progressed = True
                break
        if _is_closed(cur):
            rings.append(cur)
        else:
            dangling.append(cur)
    return rings, dangling


def _ring_area(ring):
    """Signed shoelace area (planar, lon/lat) -- only used for ordering by size."""
    a = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        a += x1 * y2 - x2 * y1
    return a / 2.0


def _point_in_ring(pt, ring):
    x, y = pt
    inside = False
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        if (y1 > y) != (y2 > y):
            xint = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < xint:
                inside = not inside
    return inside


def _assemble_multipolygon(outers, inners):
    """Group inner rings into their containing outer ring."""
    if not outers:
        return None
    # Largest first so a hole lands in the smallest containing outer last-wins-free.
    ordered = sorted(outers, key=lambda r: abs(_ring_area(r)), reverse=True)
    polys = [[r] for r in ordered]
    for inner in inners:
        if len(inner) < 4:
            continue
        target = None
        target_area = None
        for idx, poly in enumerate(polys):
            if _point_in_ring(inner[0], poly[0]):
                a = abs(_ring_area(poly[0]))
                if target_area is None or a < target_area:
                    target, target_area = idx, a
        polys[target if target is not None else 0].append(inner)
    if len(polys) == 1:
        return {"type": "Polygon", "coordinates": polys[0]}
    return {"type": "MultiPolygon", "coordinates": polys}


def element_to_features(el):
    """Convert one Overpass element into zero or more GeoJSON features."""
    tags = el.get("tags") or {}
    etype = el.get("type")
    props = dict(tags)
    props["@id"] = "{}/{}".format(etype, el.get("id"))
    props["@type"] = etype
    props["osm_id"] = el.get("id")

    if etype == "node":
        if "lon" not in el or "lat" not in el:
            return []
        return [{
            "type": "Feature",
            "id": props["@id"],
            "properties": props,
            "geometry": {"type": "Point", "coordinates": [el["lon"], el["lat"]]},
        }]

    if etype == "way":
        coords = _coords(el.get("geometry"))
        if len(coords) < 2:
            return []
        if _is_closed(coords) and _looks_like_area(tags):
            geom = {"type": "Polygon", "coordinates": [coords]}
        else:
            geom = {"type": "LineString", "coordinates": coords}
        return [{
            "type": "Feature",
            "id": props["@id"],
            "properties": props,
            "geometry": geom,
        }]

    if etype == "relation":
        members = el.get("members") or []
        rel_type = tags.get("type")
        if rel_type in ("multipolygon", "boundary"):
            outers, inners = [], []
            for m in members:
                if m.get("type") != "way":
                    continue
                c = _coords(m.get("geometry"))
                if len(c) < 2:
                    continue
                (inners if m.get("role") == "inner" else outers).append(c)
            o_rings, o_dangle = _stitch(outers)
            i_rings, _ = _stitch(inners)
            geom = _assemble_multipolygon(o_rings, i_rings)
            if geom is None and o_dangle:
                geom = {"type": "MultiLineString", "coordinates": o_dangle}
            if geom is None:
                return []
            return [{
                "type": "Feature",
                "id": props["@id"],
                "properties": props,
                "geometry": geom,
            }]

        # Route / other relations -> MultiLineString of member way geometries.
        lines = []
        points = []
        for m in members:
            if m.get("type") == "way":
                c = _coords(m.get("geometry"))
                if len(c) >= 2:
                    lines.append(c)
            elif m.get("type") == "node" and "lon" in m and "lat" in m:
                points.append([m["lon"], m["lat"]])
        feats = []
        if lines:
            feats.append({
                "type": "Feature",
                "id": props["@id"],
                "properties": props,
                "geometry": {"type": "MultiLineString", "coordinates": lines},
            })
        elif points:
            feats.append({
                "type": "Feature",
                "id": props["@id"],
                "properties": props,
                "geometry": {"type": "MultiPoint", "coordinates": points},
            })
        return feats

    return []


def to_feature_collection(osm_json, bbox=None, name=None):
    features = []
    for el in osm_json.get("elements", []):
        features.extend(element_to_features(el))
    fc = {
        "type": "FeatureCollection",
        "name": name or "osm",
        "crs": {
            "type": "name",
            "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"},
        },
        "features": features,
    }
    if bbox:
        fc["bbox"] = [bbox.west, bbox.south, bbox.east, bbox.north]
    return fc


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def fetch_category(category, bbox, verbose=True, timeout=300):
    if category not in CATEGORIES:
        raise KeyError("unknown category {!r} (have: {})".format(
            category, ", ".join(sorted(CATEGORIES))))
    query = build_query(CATEGORIES[category], bbox, timeout=timeout)
    if verbose:
        print("  querying Overpass for '{}'...".format(category))
    data = overpass(query, verbose=verbose)
    return to_feature_collection(data, bbox=bbox, name=category)


def geometry_histogram(fc):
    hist = {}
    for f in fc["features"]:
        t = (f.get("geometry") or {}).get("type", "None")
        hist[t] = hist.get(t, 0) + 1
    return hist


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--south", type=float, default=DEFAULT_BBOX[0])
    p.add_argument("--west", type=float, default=DEFAULT_BBOX[1])
    p.add_argument("--north", type=float, default=DEFAULT_BBOX[2])
    p.add_argument("--east", type=float, default=DEFAULT_BBOX[3])
    p.add_argument("--out", default=DEFAULT_OUT, help="output directory")
    p.add_argument("--categories", nargs="+", default=sorted(CATEGORIES),
                   choices=sorted(CATEGORIES), metavar="CAT",
                   help="subset of: " + ", ".join(sorted(CATEGORIES)))
    p.add_argument("--pause", type=float, default=6.0,
                   help="polite seconds between queries (default 6)")
    p.add_argument("--timeout", type=int, default=300, help="Overpass server timeout")
    args = p.parse_args(argv)

    bbox = BBox(args.south, args.west, args.north, args.east)
    os.makedirs(args.out, exist_ok=True)

    print("bbox  S={} W={} N={} E={}".format(*bbox))
    print("out   {}".format(args.out))

    summary = {}
    for i, cat in enumerate(args.categories):
        if i:
            time.sleep(args.pause)  # one query at a time, be polite
        fc = fetch_category(cat, bbox, timeout=args.timeout)
        path = os.path.join(args.out, "{}.geojson".format(cat))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(fc, fh, ensure_ascii=False)
        n = len(fc["features"])
        summary[cat] = (n, path, geometry_histogram(fc))
        if n == 0:
            print("  {}: 0 features (EMPTY -- nothing in OSM for this bbox)".format(cat))
        else:
            print("  {}: {} features -> {}".format(cat, n, path))
            print("     geometry: {}".format(
                ", ".join("{}={}".format(k, v) for k, v in sorted(
                    summary[cat][2].items(), key=lambda kv: -kv[1]))))

    print("\nTotals")
    for cat, (n, path, _) in summary.items():
        print("  {:<10} {:>6}  {}".format(cat, n, os.path.basename(path)))
    return summary


if __name__ == "__main__":
    main()
