#!/usr/bin/env python3
"""Pull all user-reported finds from Supabase into data/cloudfinds.geojson.

Everyone's finds (all devices) train the model; the app itself only shows each
device's own finds. Run this before build_model.py to fold the latest reported
finds into training. Output is gitignored (contains exact user spots); only the
aggregate suitability.json is committed.
"""
import json, os, urllib.request

SB = "https://frivhxpuntqwzrkxdmrp.supabase.co/rest/v1"
KEY = "sb_publishable_Fjd4npCW40Bz8-nAhhYkYQ_NH6THI_9"   # publishable key = safe
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "data", "cloudfinds.geojson")

req = urllib.request.Request(SB + "/finds?select=*", headers={"apikey": KEY, "Authorization": "Bearer " + KEY})
rows = json.load(urllib.request.urlopen(req, timeout=30))
rows = [r for r in rows if r.get("device_id") != "setup-test" and r.get("lat") and r.get("lon")]

feats = [{"type": "Feature",
          "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
          "properties": {"id": r["id"], "sv": r.get("species"), "date": r.get("date"),
                         "device_id": r.get("device_id")}} for r in rows]
json.dump({"type": "FeatureCollection", "features": feats,
           "meta": {"source": "supabase finds table", "count": len(feats)}},
          open(OUT, "w"), ensure_ascii=False)
from collections import Counter
print(f"pulled {len(feats)} reported finds from {len(set(r.get('device_id') for r in rows))} device(s)")
print("by species:", dict(Counter(f['properties']['sv'] for f in feats)))
