#!/usr/bin/env python3
"""Build daily mushroom fruiting-index series from SMHI weather, per region.

fruiting(t) = rain_factor x temp_factor, the temporal half of the model:
  * rain_factor — cumulative precipitation over the prior ~21 days (mushrooms
    fruit a 1-2 weeks after sustained rain); 0 when too dry, 1 when wet.
  * temp_factor — bell around ~11 C; 0 in frost or heat.
History from SMHI metobs (nearest active stations), future from the snow1g
10-day point forecast. Writes data/forecast.json for the app's date picker;
the map shows habitat(x) x fruiting(selected date).

The tool now covers TWO regions with meaningfully different weather:
  * "are"    — the Åre fjäll/valley in the WEST (nearest stations + a snow1g
               point at lon 13.1 / lat 63.4).
  * "krokom" — Krokom kommun ~60-100 km EAST, drier/warmer lowlands (its own
               nearest stations + a snow1g point near Krokom tätort).
The frontend picks, per map cell, the nearest region anchor so the overlay
shows each area's own conditions. forecast.json stays backward-safe: it keeps a
top-level `days` array (the Åre default) so older code paths still work, and
adds a `regions` map with a full per-region daily series.

All SMHI open-data APIs are key-free (CC BY 4.0).
"""
import json, os, urllib.request, datetime

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "data", "forecast.json")

METOBS = "https://opendata-download-metobs.smhi.se/api/version/1.0"
FCST = ("https://opendata-download-metfcst.smhi.se/api/category/snow1g/version/1"
        "/geotype/point/lon/{lon}/lat/{lat}/data.json")

# A metobs series is rejected if its most recent observation is older than this
# (fixes e.g. Mörsil temp, which lingers in the catalog but stops mid-June); the
# next station in the list is used instead. Stations are listed nearest-first.
MAX_STALE_DAYS = 21

# Per-region weather sources. Each region draws its history from its own nearest
# active stations (precip param 5 = daily precip sum; temp param 2 = daily mean
# temp) and its future from a snow1g point forecast at `anchor` [lat, lon].
REGIONS = {
    "are": {
        "anchor": [63.40, 13.10],
        # Järpströmmen(14km), Vallbo, Digernäset, Mörsil — all Åre valley/fjäll.
        "precip_stations": [133240, 133100, 132370, 133190],
        # Mörsil(28km, valley — preferred when fresh) then the fresh high-fjäll
        # autostations Storlien-Storvallen / Korsvattnet as fallbacks.
        "temp_stations": [133190, 132170, 133500],
    },
    "krokom": {
        "anchor": [63.55, 14.40],
        # Föllinge A(17km, active for both) then Kaxås-Åflo.
        "precip_stations": [134410, 133300],
        # Föllinge A(17km) then Östersund-Frösön flygplats / Norderön.
        "temp_stations": [134410, 134110, 134090],
    },
}
DEFAULT_REGION = "are"   # what the backward-compatible top-level `days` holds


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "svampskogen/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def metobs_daily(param, stations):
    """{date_str: value} for a daily metobs parameter.

    Tries the (nearest-first) station list and returns the first station whose
    most recent observation is fresh (<= MAX_STALE_DAYS old). Falls back to the
    freshest station that returned any data if none qualify. Returns (dict, id).
    """
    results = []   # (station, {day: value}, last_day)
    for st in stations:
        try:
            d = get(f"{METOBS}/parameter/{param}/station/{st}/period/latest-months/data.json")
            out = {}
            for v in d.get("value", []):
                ts = v.get("date") or v.get("from")
                if ts is None or v.get("value") is None:
                    continue
                day = datetime.datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%d")
                out[day] = float(v["value"])
            if out:
                last = max(out)
                results.append((st, out, last))
                print(f"    param {param}: station {st} -> {len(out)} days (..{last})")
        except Exception as e:
            print(f"    param {param}: station {st} failed ({e})")
    if not results:
        return {}, None
    today = datetime.date.today()
    for st, out, last in results:                      # nearest-first, fresh enough
        if (today - datetime.date.fromisoformat(last)).days <= MAX_STALE_DAYS:
            return out, st
    results.sort(key=lambda r: r[2], reverse=True)      # else the freshest available
    print(f"    param {param}: all stale, using freshest {results[0][0]} (..{results[0][2]})")
    return results[0][1], results[0][0]


def forecast_daily(anchor):
    """{date: (precip_sum, temp_mean)} from the snow1g point forecast at anchor."""
    lat, lon = anchor
    rain, temp = {}, {}
    try:
        d = get(FCST.format(lon=lon, lat=lat))
    except Exception as e:
        print(f"    forecast fetch failed: {e}"); return {}
    for step in d.get("timeSeries", []):
        day = step["time"][:10]
        data = step.get("data", step)
        def field(name):
            if isinstance(data, dict) and name in data:
                return data[name]
            return None
        t = field("air_temperature")
        p = field("precipitation_amount_mean")
        if p is None: p = field("precipitation_amount_mean_deterministic")
        if t is not None: temp.setdefault(day, []).append(float(t))
        if p is not None: rain[day] = rain.get(day, 0.0) + float(p)
    out = {}
    for day in set(list(rain) + list(temp)):
        tmean = sum(temp[day]) / len(temp[day]) if temp.get(day) else None
        out[day] = (round(rain.get(day, 0.0), 1), round(tmean, 1) if tmean is not None else None)
    print(f"    forecast: {len(out)} days")
    return out


def clamp(x, lo=0.0, hi=1.0): return max(lo, min(hi, x))


def gather_region(cfg):
    """Fetch a region's weather and return (rec, stations) where rec maps its
    available days -> {rain, temp, forecast} and stations records the ids used."""
    precip, pst = metobs_daily(5, cfg["precip_stations"])   # daily precip sum (mm)
    temps, tst = metobs_daily(2, cfg["temp_stations"])      # daily mean temp (C)
    fc = forecast_daily(cfg["anchor"])
    days = set(precip) | set(temps) | set(fc)
    if not days:
        return {}, {"precip": pst, "temp": tst}
    d0 = datetime.date.fromisoformat(min(days))
    d1 = datetime.date.fromisoformat(max(days))
    rec = {}
    last_t = None
    cur = d0
    while cur <= d1:
        s = cur.isoformat()
        is_fc = s in fc
        if is_fc:
            r, t = fc[s]
        else:
            r = precip.get(s, 0.0)
            t = temps.get(s)
        if t is None: t = last_t
        else: last_t = t
        rec[s] = {"rain": round(r, 1), "temp": t, "forecast": is_fc}
        cur += datetime.timedelta(days=1)
    return rec, {"precip": pst, "temp": tst}


def series_from_rec(rec, days_sorted):
    """Compute the daily fruiting series for one region over the shared date axis.

    `days_sorted` is the common axis across all regions so every region's series
    is index-aligned (a single date slider drives them all). Days missing from a
    region's `rec` get rain=0 and carry the last known temp — identical to the
    within-region gap handling."""
    idx = {d: i for i, d in enumerate(days_sorted)}
    # dense rain/temp over the shared axis (rain=0 / carry temp where absent)
    rain_by = []
    temp_by = []
    fcast_by = []
    last_t = None
    for d in days_sorted:
        r = rec.get(d)
        if r is not None:
            rain_by.append(r["rain"])
            if r["temp"] is not None: last_t = r["temp"]
            temp_by.append(last_t)
            fcast_by.append(r["forecast"])
        else:
            rain_by.append(0.0)
            temp_by.append(last_t)
            fcast_by.append(False)
    out_days = []
    for i, d in enumerate(days_sorted):
        rain21 = round(sum(rain_by[max(0, i - 20): i + 1]), 1)
        temp = temp_by[i]
        # 21-day rain: fruiting saturates at ~58 mm (was 70). Åre's wettest
        # spells only reach ~56-61 mm of 21-day rain, so a 70 mm bar meant Åre
        # could NEVER fully fruit while wetter Krokom saturated — hiding all of
        # Åre in strict mode. 58 mm lets both regions peak on their genuinely
        # wet periods; dry days (Åre median ~25 mm) still stay low so the map
        # still dims when it should.
        rain_f = clamp((rain21 - 15) / (58 - 15))
        if temp is None:
            temp_f = 0.4
        else:
            temp_f = clamp(1 - ((temp - 11) / 9) ** 2)
        fr = round(rain_f * temp_f, 3)
        if fr >= 0.65:   verdict = "Toppförhållanden"
        elif fr >= 0.4:  verdict = "Bra förhållanden"
        elif fr >= 0.18: verdict = "Kan börja komma"
        else:            verdict = "Dåliga förhållanden"
        reason = ""
        if fr < 0.4:
            reason = "för torrt" if rain_f <= temp_f else "för kallt"
        out_days.append({"date": d, "rain21": rain21, "temp": temp,
                         "fruiting": fr, "verdict": verdict, "reason": reason,
                         "forecast": fcast_by[i]})
    return out_days


def main():
    recs = {}
    stations = {}
    for name, cfg in REGIONS.items():
        print(f"region {name} anchor={cfg['anchor']}:")
        rec, st = gather_region(cfg)
        recs[name] = rec
        stations[name] = st

    all_days = set()
    for rec in recs.values():
        all_days |= set(rec)
    if not all_days:
        raise SystemExit("no SMHI data retrieved")
    d0 = datetime.date.fromisoformat(min(all_days))
    d1 = datetime.date.fromisoformat(max(all_days))
    days_sorted = []
    cur = d0
    while cur <= d1:                       # dense, gap-free shared axis
        days_sorted.append(cur.isoformat())
        cur += datetime.timedelta(days=1)

    regions_out = {}
    for name, cfg in REGIONS.items():
        regions_out[name] = {
            "anchor": cfg["anchor"],
            "stations": stations[name],
            "days": series_from_rec(recs[name], days_sorted),
        }

    default_days = regions_out[DEFAULT_REGION]["days"]
    out = {
        "meta": {
            "built": datetime.date.today().isoformat(),
            "default_region": DEFAULT_REGION,
            "regions": {n: {"anchor": r["anchor"], "stations": r["stations"]}
                        for n, r in regions_out.items()},
            "source": "SMHI metobs (history) + snow1g forecast (CC BY 4.0)",
            "formula": "fruiting = rain_factor(21-day precip) x temp_factor(bell ~11C)",
            "note": ("Per-region heuristic fruiting index (Åre west, Krokom east); "
                     "the map shows habitat x fruiting(date) using the nearest "
                     "region anchor per cell. Calibrate later against logged "
                     "finds-vs-weather."),
        },
        # Backward-compatible top-level default (Åre) so older code keeps working.
        "days": default_days,
        # New: per-region index-aligned series (same date axis as `days`).
        "regions": regions_out,
    }
    json.dump(out, open(OUT, "w"), separators=(",", ":"))

    print(f"wrote {OUT}: {len(days_sorted)} days, {days_sorted[0]}..{days_sorted[-1]}")
    for name, r in regions_out.items():
        fcn = sum(1 for x in r["days"] if x["forecast"])
        print(f"  region {name}: stations {r['stations']}  "
              f"({len(r['days'])-fcn} obs + {fcn} forecast)")
        for x in r["days"][-7:]:
            tag = "F" if x["forecast"] else " "
            t = x['temp'] if x['temp'] is not None else float('nan')
            print(f"    {tag} {x['date']}  rain21={x['rain21']:5.1f}  temp={t:5.1f}  "
                  f"fruiting={x['fruiting']:.2f}  {x['verdict']}")


if __name__ == "__main__":
    main()
