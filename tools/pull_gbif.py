#!/usr/bin/env python3
"""Pull mushroom occurrence data from GBIF (mirrors Swedish Artportalen, CC0).

Two outputs, both parameterised by a WGS84 bbox:

  1. data/occurrences.geojson      EDIBLE target species (the map "presences"
                                   + model training). Kept records only, i.e.
                                   coordinateUncertaintyInMeters <= 1000 m.
  2. data/layers/targetgroup.geojson  ALL fungi (kingdom Fungi, taxonKey=5),
                                   the recorder-effort / bias-correction surface
                                   for the species distribution model.

GBIF occurrence search caps offset+limit at 100 000, so any query whose total
exceeds that is recursively split into bbox quadrants and merged (deduped by
gbifID). No API key needed; we just send a polite User-Agent.

Usage:
  python3 tools/pull_gbif.py                 # default union bbox (Åre+Krokom)
  python3 tools/pull_gbif.py S W N E         # custom bbox, decimal degrees
  python3 tools/pull_gbif.py --edible-only
  python3 tools/pull_gbif.py --targetgroup-only
"""
import json, os, sys, time, urllib.parse, urllib.request, urllib.error
from collections import Counter

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OCC_OUT = os.path.join(HERE, "data", "occurrences.geojson")
TG_OUT = os.path.join(HERE, "data", "layers", "targetgroup.geojson")

API = "https://api.gbif.org/v1/occurrence/search"
UA = "svampskogen/1.0 (olle.larsson@framna.com; personal mushroom-habitat tool)"

# Default bbox: union of Åre + Krokom kommun (Jämtlands län), padded. (S, W, N, E)
DEFAULT_BBOX = (62.85, 11.95, 64.55, 15.35)

# Edible target species: taxonKey -> (Swedish label, scientific label).
# taxonKey queries include synonyms (e.g. Cantharellus tubaeformis under
# Craterellus tubaeformis), so we normalise to these fixed labels.
EDIBLE = {
    5249504: ("Kantarell", "Cantharellus cibarius"),
    5954958: ("Karljohan", "Boletus edulis"),
    2554536: ("Trattkantarell", "Craterellus tubaeformis"),
}
FUNGI_KINGDOM_KEY = 5

MAX_UNCERTAINTY_M = 1000        # drop coarse edible records (noise)
PAGE = 300                      # GBIF max page size
OFFSET_CAP = 100000             # GBIF hard cap on offset+limit
SPLIT_THRESHOLD = 8000          # tile a bbox whose count exceeds this. Kept small
                                # ON PURPOSE: GBIF deep pagination is ~50x slower
                                # at offset 50k than at 0, so we keep every tile's
                                # offsets shallow (<8k, measured fast) by splitting
                                # into more, smaller bboxes rather than paging deep.
THROTTLE = 0.1                  # small politeness gap between pages.

# Keep only the fields we actually use, so a 180k-record pull doesn't hold ~1 GB
# of full GBIF records in memory. gbifID/key retained for dedup across tiles.
_KEEP = ("decimalLatitude", "decimalLongitude", "species", "scientificName",
         "year", "month", "day", "eventDate", "coordinateUncertaintyInMeters",
         "recordedBy", "gbifID", "key")


def _get(params, retries=6):
    url = API + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # transient network / 429 / 5xx
            last = e
            retry_after = 0
            if isinstance(e, urllib.error.HTTPError):
                try:
                    retry_after = int(e.headers.get("Retry-After", 0))
                except (TypeError, ValueError):
                    retry_after = 0
            time.sleep(max(retry_after, 2.0 * (attempt + 1)))
    raise RuntimeError(f"GBIF request failed after {retries} tries: {last}\n{url}")


def _base_params(bbox, taxon_key):
    s, w, n, e = bbox
    return {
        "country": "SE",
        "hasCoordinate": "true",
        "decimalLatitude": f"{s},{n}",
        "decimalLongitude": f"{w},{e}",
        "taxonKey": taxon_key,
    }


def count(bbox, taxon_key):
    p = _base_params(bbox, taxon_key)
    p["limit"] = 0
    return _get(p)["count"]


def _page_bbox(bbox, taxon_key):
    """Fetch every result for one bbox that is known to fit under the cap."""
    out = []
    offset = 0
    while True:
        p = _base_params(bbox, taxon_key)
        p["limit"] = PAGE
        p["offset"] = offset
        data = _get(p)
        out.extend({k: r.get(k) for k in _KEEP} for r in data["results"])
        if data.get("endOfRecords") or offset + PAGE >= OFFSET_CAP:
            break
        offset += PAGE
        time.sleep(THROTTLE)   # stay under GBIF's rate limit
    return out


def fetch_all(bbox, taxon_key, depth=0):
    """Fetch all records for bbox, recursively tiling if over the offset cap."""
    total = count(bbox, taxon_key)
    indent = "  " * depth
    if total <= SPLIT_THRESHOLD:
        print(f"{indent}bbox {tuple(round(x,3) for x in bbox)} -> {total} records, paging")
        return _page_bbox(bbox, taxon_key)
    # too big: split into 4 quadrants and merge (dedup by gbifID)
    s, w, n, e = bbox
    mlat, mlon = (s + n) / 2, (w + e) / 2
    print(f"{indent}bbox {tuple(round(x,3) for x in bbox)} -> {total} > {SPLIT_THRESHOLD}, splitting into quadrants")
    quads = [
        (s, w, mlat, mlon),
        (s, mlon, mlat, e),
        (mlat, w, n, mlon),
        (mlat, mlon, n, e),
    ]
    seen, merged = set(), []
    for q in quads:
        for rec in fetch_all(q, taxon_key, depth + 1):
            gid = rec.get("gbifID") or rec.get("key")
            if gid in seen:
                continue
            seen.add(gid)
            merged.append(rec)
    return merged


def _clean_date(rec):
    ev = rec.get("eventDate")
    if ev:
        return str(ev).split("/")[0][:10]  # handle ISO + date ranges
    y, m, d = rec.get("year"), rec.get("month"), rec.get("day")
    if y and m and d:
        return f"{y:04d}-{m:02d}-{d:02d}"
    return str(y) if y else None


def in_bbox(lon, lat, bbox):
    s, w, n, e = bbox
    return w <= lon <= e and s <= lat <= n


def pull_edible(bbox):
    feats = []
    per_species = Counter()
    totals = {}
    for key, (sv, sci) in EDIBLE.items():
        recs = fetch_all(bbox, key)
        totals[sci] = len(recs)
        for r in recs:
            lat, lon = r.get("decimalLatitude"), r.get("decimalLongitude")
            if lat is None or lon is None or not in_bbox(lon, lat, bbox):
                continue
            unc = r.get("coordinateUncertaintyInMeters")
            if unc is not None and unc > MAX_UNCERTAINTY_M:
                continue
            feats.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(lon, 5), round(lat, 5)]},
                "properties": {
                    "source": "gbif",
                    "sv": sv,
                    "sci": sci,
                    "date": _clean_date(r),
                    "year": r.get("year"),
                    "uncertainty_m": unc,
                    "recordedBy": r.get("recordedBy"),
                    "gbifID": str(r.get("gbifID") or r.get("key")),
                },
            })
            per_species[sv] += 1
    total_raw = sum(totals.values())
    fc = {
        "type": "FeatureCollection",
        "features": feats,
        "meta": {
            "bbox": [bbox[1], bbox[0], bbox[3], bbox[2]],  # [W,S,E,N]
            "source": "GBIF / Artportalen (CC0)",
            "region": "Åre + Krokom kommun",
            "pulled": time.strftime("%Y-%m-%d"),
            "total": total_raw,
            "count": len(feats),
            "filter": f"dropped uncertainty>{MAX_UNCERTAINTY_M}m",
        },
    }
    json.dump(fc, open(OCC_OUT, "w"), ensure_ascii=False)
    return len(feats), per_species, totals


def pull_targetgroup(bbox):
    recs = fetch_all(bbox, FUNGI_KINGDOM_KEY)
    feats = []
    for r in recs:
        lat, lon = r.get("decimalLatitude"), r.get("decimalLongitude")
        if lat is None or lon is None or not in_bbox(lon, lat, bbox):
            continue
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(lon, 5), round(lat, 5)]},
            "properties": {
                "species": r.get("species") or r.get("scientificName"),
                "year": r.get("year"),
            },
        })
    fc = {
        "type": "FeatureCollection",
        "features": feats,
        "meta": {
            "source": "GBIF occurrence search, kingdomKey=5",
            "bbox": [bbox[1], bbox[0], bbox[3], bbox[2]],  # [W,S,E,N]
            "purpose": "target-group background for SDM bias correction",
            "region": "Åre + Krokom kommun",
            "built": time.strftime("%Y-%m-%d"),
            "count": len(feats),
        },
    }
    json.dump(fc, open(TG_OUT, "w"), ensure_ascii=False)
    return len(feats)


def main(argv):
    bbox = DEFAULT_BBOX
    do_edible = do_tg = True
    coords = []
    for a in argv:
        if a == "--edible-only":
            do_tg = False
        elif a == "--targetgroup-only":
            do_edible = False
        else:
            coords.append(float(a))
    if len(coords) == 4:
        bbox = tuple(coords)  # S W N E
    print(f"bbox (S,W,N,E) = {bbox}\n")

    if do_edible:
        print("=== EDIBLE target species -> occurrences.geojson ===")
        n, per, totals = pull_edible(bbox)
        print("\ntaxon totals (raw, pre-uncertainty-filter):")
        for sci, t in totals.items():
            print(f"  {sci}: {t}")
        print(f"kept (<= {MAX_UNCERTAINTY_M}m): {n}")
        print("per species (kept):", dict(per))
        print()

    if do_tg:
        print("=== ALL fungi -> targetgroup.geojson ===")
        n = pull_targetgroup(bbox)
        print(f"targetgroup features: {n}\n")


if __name__ == "__main__":
    main(sys.argv[1:])
