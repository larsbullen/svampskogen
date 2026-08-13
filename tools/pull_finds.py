#!/usr/bin/env python3
"""Pull all user-reported finds from Supabase into data/cloudfinds.geojson.

Reading is now locked to logged-in accounts, so this uses the Supabase SECRET
key (service role, bypasses RLS) — kept in the macOS keychain, never in the repo
or in chat. Add it once (in your own Terminal):

  read -rs "K?Paste Supabase secret key (sb_secret_...): " && \
  security add-generic-password -a svampskogen -s supabase-secret -w "$K" && unset K

Everyone's finds train the model; the app itself only shows each device's own.
Output is gitignored (exact user spots); only aggregate suitability.json ships.
"""
import json, os, subprocess, urllib.request

SB = "https://frivhxpuntqwzrkxdmrp.supabase.co/rest/v1"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "data", "cloudfinds.geojson")

def secret_key():
    try:
        r = subprocess.run(["security", "find-generic-password", "-a", "svampskogen",
                            "-s", "supabase-secret", "-w"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip()
    except Exception:
        return ""

KEY = secret_key()
if not KEY:
    raise SystemExit(
        "No Supabase secret key in keychain. Add it once in your Terminal:\n"
        '  read -rs "K?Paste Supabase secret key (sb_secret_...): " && '
        'security add-generic-password -a svampskogen -s supabase-secret -w "$K" && unset K')

req = urllib.request.Request(SB + "/finds?select=*", headers={"apikey": KEY, "Authorization": "Bearer " + KEY})
rows = json.load(urllib.request.urlopen(req, timeout=30))
rows = [r for r in rows if r.get("device_id") != "setup-test" and r.get("lat") and r.get("lon")]

feats = [{"type": "Feature",
          "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
          "properties": {"id": r["id"], "sv": r.get("species"), "date": r.get("date"),
                         "device_id": r.get("device_id")}} for r in rows]
json.dump({"type": "FeatureCollection", "features": feats,
           "meta": {"source": "supabase finds table (secret key)", "count": len(feats)}},
          open(OUT, "w"), ensure_ascii=False)
from collections import Counter
print(f"pulled {len(feats)} reported finds from {len(set(r.get('device_id') for r in rows))} device(s)")
print("by species:", dict(Counter(f['properties']['sv'] for f in feats)))
