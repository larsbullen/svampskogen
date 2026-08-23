#!/usr/bin/env python3
"""Morgonsol — how wet is the ground RIGHT NOW, and what is coming during the trip.

The tent-ground score is structural: it says where water collects given terrain
and soil, which is true in any weather. This adds the missing half — whether the
ground is currently wetter or drier than it usually is at this time of year, and
what the forecast will do to it.

The measure is an Antecedent Precipitation Index: recent rain weighted by an
exponential decay, so yesterday's downpour counts far more than one three weeks
ago. Its absolute value means little on its own, so it is ranked against the
same calendar window in every year the station has recorded. "62 mm" tells you
nothing; "wetter than 85% of late Augusts here" tells you to favour raised ground.

Stations are discovered by distance rather than hardcoded, so this works for any
route. All SMHI open data, no key (CC BY 4.0).

Usage:
    python3 tools/morgonsol/build_weather.py [--days 5] [--start YYYY-MM-DD]
"""
import argparse
import csv
import datetime as dt
import io
import json
import math
import os
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.normpath(os.path.join(HERE, "..", "..", "data", "morgonsol"))

METOBS = "https://opendata-download-metobs.smhi.se/api/version/1.0"
FCST = ("https://opendata-download-metfcst.smhi.se/api/category/snow1g/version/1"
        "/geotype/point/lon/{lon}/lat/{lat}/data.json")

PARAM_PRECIP = 5   # daily precipitation sum
PARAM_TEMP = 2     # daily mean temperature

API_DECAY = 0.90   # per day; ~7 day half-life
API_WINDOW = 30    # days of rain that still count
DOY_WINDOW = 10    # +/- days around the calendar date for the climatology


def get_json(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "morgonsol/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def get_text(url, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": "morgonsol/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def haversine_km(a, b, c, d):
    R = 6371.0
    p1, p2 = math.radians(a), math.radians(c)
    x = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(d - b) / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(x))


def nearest_stations(param, lat, lon, n=6):
    doc = get_json("%s/parameter/%d.json" % (METOBS, param))
    st = [s for s in doc["station"] if s.get("active")]
    st.sort(key=lambda s: haversine_km(lat, lon, s["latitude"], s["longitude"]))
    return [
        {
            "id": s["id"], "name": s["name"],
            "km": round(haversine_km(lat, lon, s["latitude"], s["longitude"]), 1),
            "height_m": round(s.get("height") or 0),
        }
        for s in st[:n]
    ]


def daily_series(param, station_id):
    """{date: value}, freshest source winning, falling back to the long archive.

    Two quite different shapes come out of metobs:
      JSON  entries keyed by `ref` (the representative day) with `value` as a
            STRING — there is no `date` field.
      CSV   a multi-block preamble, then a header starting "Från Datum Tid",
            after which the representative day is column 2 and the value
            column 3. The archive reaches back to the 1940s for old stations,
            which is what makes a real climatology possible.
    """
    out = {}
    # Freshest first: later sources use setdefault so they never overwrite.
    for period, kind in (("latest-day", "json"),
                         ("latest-months", "json"),
                         ("corrected-archive", "csv")):
        url = "%s/parameter/%d/station/%d/period/%s/data.%s" % (
            METOBS, param, station_id, period, kind)
        got = 0
        try:
            if kind == "json":
                doc = get_json(url)
                for v in doc.get("value") or []:
                    raw = v.get("value")
                    if raw in (None, ""):
                        continue
                    ref = v.get("ref")
                    if ref:
                        d = dt.date.fromisoformat(str(ref)[:10])
                    elif v.get("to"):
                        d = dt.datetime.utcfromtimestamp(v["to"] / 1000).date()
                    else:
                        continue
                    if out.setdefault(d, float(raw)) is not None:
                        got += 1
            else:
                lines = get_text(url).splitlines()
                start = next((i for i, l in enumerate(lines)
                              if l.lower().startswith("från datum")), None)
                if start is None:
                    print("    %s: no data header found" % period)
                    continue
                rdr = csv.reader(io.StringIO("\n".join(lines[start:])), delimiter=";")
                next(rdr)  # header
                for row in rdr:
                    if len(row) < 4:
                        continue
                    try:
                        d = dt.date.fromisoformat(row[2][:10])
                        val = float(row[3].replace(",", "."))
                    except (ValueError, IndexError):
                        continue
                    out.setdefault(d, val)
                    got += 1
            print("    %-18s %6d values" % (period, got))
        except urllib.error.HTTPError as e:
            print("    %-18s HTTP %s" % (period, e.code))
        except Exception as e:
            print("    %-18s %s" % (period, e))
    return out


def api_at(series, day):
    """Antecedent Precipitation Index on `day`: rain weighted by exponential decay."""
    total, weight_used = 0.0, 0.0
    for i in range(API_WINDOW):
        d = day - dt.timedelta(days=i)
        w = API_DECAY ** i
        if d in series:
            total += series[d] * w
            weight_used += w
    coverage = weight_used / sum(API_DECAY ** i for i in range(API_WINDOW))
    return total, coverage


def climatology(series, day, years_back=40):
    """API values for the same calendar window in every year on record."""
    vals = []
    for yr in range(day.year - years_back, day.year):
        try:
            anchor = day.replace(year=yr)
        except ValueError:
            continue
        for off in range(-DOY_WINDOW, DOY_WINDOW + 1):
            d = anchor + dt.timedelta(days=off)
            v, cov = api_at(series, d)
            if cov > 0.85:
                vals.append(v)
    return sorted(vals)


def percentile_of(sorted_vals, x):
    if not sorted_vals:
        return None
    below = sum(1 for v in sorted_vals if v < x)
    return round(100.0 * below / len(sorted_vals))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None, help="trip start YYYY-MM-DD (default tomorrow)")
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--today", default=None, help="override 'today' for reproducibility")
    args = ap.parse_args()

    with open(os.path.join(DATA, "grid.json")) as f:
        grid = json.load(f)
    w, s, e, n = grid["bounds_wgs84"]
    lat, lon = (s + n) / 2.0, (w + e) / 2.0

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    start = dt.date.fromisoformat(args.start) if args.start else today + dt.timedelta(days=1)
    trip_days = [start + dt.timedelta(days=i) for i in range(args.days)]

    print("route centre %.3f, %.3f   trip %s .. %s" % (lat, lon, trip_days[0], trip_days[-1]))

    # ---------------------------------------------------------------- history
    precip_cands = nearest_stations(PARAM_PRECIP, lat, lon)
    temp_cands = nearest_stations(PARAM_TEMP, lat, lon)
    print("nearest precip stations: %s" % ", ".join(
        "%s (%s km)" % (c["name"], c["km"]) for c in precip_cands[:3]))
    print("nearest temp stations  : %s" % ", ".join(
        "%s (%s km)" % (c["name"], c["km"]) for c in temp_cands[:3]))

    precip_st, precip = None, {}
    for cand in precip_cands:
        print("  trying precip %s (%s)..." % (cand["name"], cand["id"]))
        ser = daily_series(PARAM_PRECIP, cand["id"])
        if ser and max(ser) >= today - dt.timedelta(days=5):
            precip_st, precip = cand, ser
            break
        print("    stale or empty (latest %s)" % (max(ser) if ser else "none"))
    if not precip_st:
        raise SystemExit("no usable precipitation station near the route")

    temp_st, temps = None, {}
    for cand in temp_cands:
        ser = daily_series(PARAM_TEMP, cand["id"])
        if ser and max(ser) >= today - dt.timedelta(days=5):
            temp_st, temps = cand, ser
            break

    print("using precip: %s (%s km, %s m), %d daily values %s..%s"
          % (precip_st["name"], precip_st["km"], precip_st["height_m"], len(precip),
             min(precip), max(precip)))
    if temp_st:
        print("using temp  : %s (%s km, %s m), %d values"
              % (temp_st["name"], temp_st["km"], temp_st["height_m"], len(temps)))

    # --------------------------------------------------------- wetness so far
    latest = max(precip)
    api_now, cov = api_at(precip, latest)
    clim = climatology(precip, latest)
    pct = percentile_of(clim, api_now)
    rain = {
        "last_3d": round(sum(precip.get(latest - dt.timedelta(days=i), 0.0) for i in range(3)), 1),
        "last_7d": round(sum(precip.get(latest - dt.timedelta(days=i), 0.0) for i in range(7)), 1),
        "last_14d": round(sum(precip.get(latest - dt.timedelta(days=i), 0.0) for i in range(14)), 1),
        "last_30d": round(sum(precip.get(latest - dt.timedelta(days=i), 0.0) for i in range(30)), 1),
    }
    print("\nrain to %s: 3d %.1f  7d %.1f  14d %.1f  30d %.1f mm"
          % (latest, rain["last_3d"], rain["last_7d"], rain["last_14d"], rain["last_30d"]))
    print("API %.1f (coverage %.0f%%), %d climatology samples -> %s percentile"
          % (api_now, 100 * cov, len(clim), pct))

    # ------------------------------------------------------------- forecast
    # snow1g v1: entries are {time, intervalParametersStartTime, data{...}} where
    # `data` is a flat dict — not the {validTime, parameters:[{name,values}]}
    # shape the older pmp3g used. Precipitation is an amount for the interval
    # ending at `time`, and the intervals lengthen further out (hourly, then
    # three- and six-hourly), so summing per entry is right but averaging
    # temperature per entry silently weights the near term. Good enough for
    # min/max; noted rather than corrected.
    forecast = []
    try:
        doc = get_json(FCST.format(lon=round(lon, 4), lat=round(lat, 4)))
        series = doc.get("timeSeries", [])
        by_day = {}
        for ts in series:
            t = dt.datetime.fromisoformat(ts["time"].replace("Z", "+00:00"))
            d = (t + dt.timedelta(hours=2)).date()  # CEST
            v = ts.get("data") or {}
            slot = by_day.setdefault(d, {
                "precip": [], "t": [], "ws": [], "gust": [], "pop": [], "frozen": [],
            })
            if "precipitation_amount_mean" in v:
                slot["precip"].append(v["precipitation_amount_mean"])
            for key, name in (("air_temperature", "t"), ("wind_speed", "ws"),
                              ("wind_speed_of_gust", "gust"),
                              ("probability_of_precipitation", "pop"),
                              ("probability_of_frozen_precipitation", "frozen")):
                if key in v:
                    slot[name].append(v[key])
        for d in trip_days:
            v = by_day.get(d)
            if not v or not v["t"]:
                forecast.append({"date": d.isoformat(), "available": False})
                continue
            forecast.append({
                "date": d.isoformat(),
                "available": True,
                "precip_mm": round(sum(v["precip"]), 1) if v["precip"] else None,
                "precip_prob": round(max(v["pop"])) if v["pop"] else None,
                "t_min": round(min(v["t"]), 1),
                "t_max": round(max(v["t"]), 1),
                "wind_ms": round(sum(v["ws"]) / len(v["ws"]), 1) if v["ws"] else None,
                "gust_ms": round(max(v["gust"]), 1) if v["gust"] else None,
                "frozen_risk": round(100 * max(v["frozen"])) if v["frozen"] else None,
            })
        got = sum(1 for f in forecast if f["available"])
        print("forecast: %d of %d trip days available (series %s..%s)"
              % (got, len(trip_days), series[0]["time"][:10], series[-1]["time"][:10]))
        for f in forecast:
            if f["available"]:
                print("  %s  %4.1f mm (%s%%)  %4.1f..%4.1f C  vind %.0f (byar %.0f) m/s%s"
                      % (f["date"], f["precip_mm"] or 0, f["precip_prob"],
                         f["t_min"], f["t_max"], f["wind_ms"] or 0, f["gust_ms"] or 0,
                         "  SNO-RISK %d%%" % f["frozen_risk"]
                         if (f["frozen_risk"] or 0) >= 20 else ""))
    except Exception as ex:
        print("forecast unavailable: %r" % ex)

    trip_rain = sum(f.get("precip_mm") or 0 for f in forecast if f.get("available"))

    # ------------------------------------------------------------- verdict
    # Two independent facts: how wet the ground is NOW (from what has already
    # fallen) and which way it is heading (from the forecast). They routinely
    # disagree — wet ground under a dry week is the common late-summer case —
    # and the useful advice comes from the combination, not either alone.
    if pct is None:
        band, advice = "okänt", "Ingen klimatologi tillgänglig."
    elif pct >= 80:
        band = "blötare än normalt"
        advice = ("Marken är blötare än den brukar vara så här års. Håll dig till "
                  "topp-marken och små höjdryggar — de gula, marginella ytorna är "
                  "sannolikt blötare än kartan antyder.")
    elif pct >= 55:
        band = "normalt fuktig"
        advice = "Marken är ungefär som vanligt. Kartans gradering gäller som den är."
    elif pct >= 25:
        band = "torrare än normalt"
        advice = ("Torrare än vanligt — även en del av den gula marken bör bära tält. "
                  "Räkna med att små bäckar kan vara svagare än kartan visar.")
    else:
        band = "ovanligt torrt"
        advice = ("Ovanligt torrt. Nästan all mark utanför myrarna bär tält, men "
                  "kontrollera vattentillgången — små bäckar kan vara uttorkade.")

    wet_now = pct is not None and pct >= 70
    if trip_rain >= 25:
        advice += (" Prognosen ger %.0f mm under turen, så marken blir blötare för "
                   "varje dag — välj hellre för högt än för lågt." % trip_rain)
    elif trip_rain >= 8:
        advice += " Prognosen ger %.0f mm under turen." % trip_rain
    elif wet_now:
        advice += (" Men prognosen är i stort sett torr (%.1f mm på %d dagar), så "
                   "marken torkar upp efter hand — det blir bättre mot slutet av "
                   "turen än i början." % (trip_rain, args.days))
    else:
        advice += " Prognosen är torr (%.1f mm på %d dagar)." % (trip_rain, args.days)

    # Cold, still, clear nights are exactly when cold air pools in hollows, which
    # is the term the map already scores as "läge". Worth connecting explicitly.
    cold = [f for f in forecast if f.get("available") and f.get("t_min") is not None]
    coldest = min((f["t_min"] for f in cold), default=None)
    calm = [f for f in cold if (f.get("wind_ms") or 99) <= 3]
    if coldest is not None and coldest <= 4:
        advice += (" Kallaste natten ligger runt %.0f°C%s — då rinner kalluften ner "
                   "i svackorna, så kartans läges-poäng (små höjdryggar) är värd "
                   "extra mycket." % (coldest, " och vinden är svag" if calm else ""))
    snowy = [f for f in forecast if (f.get("frozen_risk") or 0) >= 20]
    if snowy:
        advice += (" OBS: %d%% risk för snöblandad nederbörd %s — på högre delar av "
                   "leden." % (max(f["frozen_risk"] for f in snowy), snowy[0]["date"]))

    out = {
        "generated": today.isoformat(),
        "observed_to": latest.isoformat(),
        "station_precip": precip_st,
        "station_temp": temp_st,
        "rain_mm": rain,
        "api": round(api_now, 1),
        "api_percentile": pct,
        "climatology_samples": len(clim),
        "climatology_years": "%s-%s" % (min(precip).year, max(precip).year),
        "wetness_band": band,
        "advice": advice,
        "trip": {"start": trip_days[0].isoformat(), "days": args.days,
                 "forecast_rain_mm": round(trip_rain, 1)},
        "forecast": forecast,
    }
    dest = os.path.join(DATA, "weather.json")
    with open(dest, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print("\nverdict: %s (%s percentile)" % (band, pct))
    print("wrote %s" % dest)


if __name__ == "__main__":
    main()
