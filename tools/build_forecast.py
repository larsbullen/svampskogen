#!/usr/bin/env python3
"""Build a daily mushroom fruiting-index series from SMHI weather.

fruiting(t) = rain_factor x temp_factor, the temporal half of the model:
  * rain_factor — cumulative precipitation over the prior ~21 days (mushrooms
    fruit a 1-2 weeks after sustained rain); 0 when too dry, 1 when wet.
  * temp_factor — bell around ~11 C; 0 in frost or heat.
History from SMHI metobs (nearest active stations), future from the snow1g
10-day point forecast. Writes data/forecast.json for the app's date picker;
the map shows habitat(x) x fruiting(selected date).

All SMHI open-data APIs are key-free (CC BY 4.0).
"""
import json, os, urllib.request, datetime

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "data", "forecast.json")

METOBS = "https://opendata-download-metobs.smhi.se/api/version/1.0"
FCST = ("https://opendata-download-metfcst.smhi.se/api/category/snow1g/version/1"
        "/geotype/point/lon/13.1/lat/63.4/data.json")
PRECIP_STATIONS = [133240, 133100, 132370]   # Järpströmmen, Vallbo, Digernäset
TEMP_STATIONS = [133190, 132170]             # Mörsil, Storlien-Storvallen

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "svampskogen/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

def metobs_daily(param, stations):
    """Return {date_str: value} for a daily metobs parameter, first station that works."""
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
                print(f"  param {param}: station {st} -> {len(out)} days")
                return out, st
        except Exception as e:
            print(f"  param {param}: station {st} failed ({e})")
    return {}, None

def forecast_daily():
    """Return {date: (precip_sum, temp_mean)} from the snow1g forecast."""
    rain, temp = {}, {}
    try:
        d = get(FCST)
    except Exception as e:
        print(f"  forecast fetch failed: {e}"); return {}, {}
    for step in d.get("timeSeries", []):
        day = step["time"][:10]
        data = step.get("data", step)
        # data may be flat (snow1g) with named fields
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
    print(f"  forecast: {len(out)} days")
    return out, d

def clamp(x, lo=0.0, hi=1.0): return max(lo, min(hi, x))

def main():
    precip, pst = metobs_daily(5, PRECIP_STATIONS)   # daily precip sum (mm)
    temps, tst = metobs_daily(2, TEMP_STATIONS)      # daily mean temp (C)
    fc, _ = forecast_daily()

    # assemble a continuous daily record
    all_days = set(precip) | set(temps) | set(fc)
    if not all_days:
        raise SystemExit("no SMHI data retrieved")
    d0 = datetime.date.fromisoformat(min(all_days))
    d1 = datetime.date.fromisoformat(max(all_days))
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

    days_sorted = sorted(rec)
    idx = {d: i for i, d in enumerate(days_sorted)}
    out_days = []
    for d in days_sorted:
        i = idx[d]
        window = [rec[days_sorted[j]]["rain"] for j in range(max(0, i - 20), i + 1)]
        rain21 = round(sum(window), 1)
        temp = rec[d]["temp"]
        rain_f = clamp((rain21 - 15) / (70 - 15))
        if temp is None:
            temp_f = 0.4
        else:
            temp_f = clamp(1 - ((temp - 11) / 9) ** 2)
        fr = round(rain_f * temp_f, 3)
        # verdict + limiting reason
        if fr >= 0.65:   verdict = "Toppförhållanden"
        elif fr >= 0.4:  verdict = "Bra förhållanden"
        elif fr >= 0.18: verdict = "Kan börja komma"
        else:            verdict = "Dåliga förhållanden"
        reason = ""
        if fr < 0.4:
            reason = "för torrt" if rain_f <= temp_f else "för kallt"
        out_days.append({"date": d, "rain21": rain21, "temp": temp,
                         "fruiting": fr, "verdict": verdict, "reason": reason,
                         "forecast": rec[d]["forecast"]})

    out = {
        "meta": {
            "built": datetime.date.today().isoformat(),
            "precip_station": pst, "temp_station": tst,
            "source": "SMHI metobs (history) + snow1g forecast (CC BY 4.0)",
            "formula": "fruiting = rain_factor(21-day precip) x temp_factor(bell ~11C)",
            "note": ("Heuristic fruiting index for the Åre region; the map shows "
                     "habitat x fruiting(date). Calibrate later against logged "
                     "finds-vs-weather."),
        },
        "days": out_days,
    }
    json.dump(out, open(OUT, "w"), separators=(",", ":"))
    fcn = sum(1 for x in out_days if x["forecast"])
    print(f"wrote {OUT}: {len(out_days)} days ({len(out_days)-fcn} obs + {fcn} forecast), "
          f"{days_sorted[0]}..{days_sorted[-1]}")
    # peek recent
    for x in out_days[-14:]:
        tag = "F" if x["forecast"] else " "
        print(f"  {tag} {x['date']}  rain21={x['rain21']:5.1f}  temp={x['temp']}  fruiting={x['fruiting']:.2f}  {x['verdict']}")

if __name__ == "__main__":
    main()
