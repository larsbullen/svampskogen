#!/usr/bin/env python3
"""Morgonsol step 3 — how much MORNING SUN each patch of ground actually gets.

Not just "which way does the slope face". This ray-casts the real terrain, so a
south-east facing bench that sits behind a big ridge to the east is correctly
scored as staying in shadow until 09:00 — which is exactly the difference
between a tent you can dry out over breakfast and one you pack away wet.

For each timestep between sunrise and the end of the morning window it computes
the sun's position (NOAA solar position algorithm), then marches a ray toward
the sun from every cell to see whether terrain blocks it.

Outputs a GeoTIFF with bands:
    sun_morning   hours of direct sun in the morning window
    sun_day       hours of direct sun over the whole day
    first_light   local hour the cell first catches the sun (24 = never)

Usage:
    python3 tools/morgonsol/build_sun.py --date 2026-09-01 [--morning-end 10.0]
"""
import argparse
import json
import math
import os

import numpy as np
import rasterio
from scipy.ndimage import map_coordinates

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.normpath(os.path.join(HERE, "..", "..", "data", "morgonsol"))
TERRAIN = "/private/tmp/morgonsol_dem/terrain.tif"
DEFAULT_OUT = "/private/tmp/morgonsol_dem/sun.tif"

# Max distance a ridge can still shadow us from, and how many samples along the ray.
MAX_RAY_M = 15000.0
RAY_STEPS = 64


def solar_position(lat, lon, when_utc):
    """NOAA solar position. `when_utc` is a naive UTC datetime. Returns (az_deg, alt_deg)."""
    import datetime as dt

    # Julian day
    a = (14 - when_utc.month) // 12
    y = when_utc.year + 4800 - a
    m = when_utc.month + 12 * a - 3
    jdn = (
        when_utc.day
        + (153 * m + 2) // 5
        + 365 * y
        + y // 4
        - y // 100
        + y // 400
        - 32045
    )
    frac = (when_utc.hour - 12) / 24.0 + when_utc.minute / 1440.0 + when_utc.second / 86400.0
    jd = jdn + frac
    t = (jd - 2451545.0) / 36525.0

    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
    m_anom = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    mrad = math.radians(m_anom)
    c = (
        math.sin(mrad) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * mrad) * (0.019993 - 0.000101 * t)
        + math.sin(3 * mrad) * 0.000289
    )
    true_long = l0 + c
    omega = 125.04 - 1934.136 * t
    app_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))

    e0 = 23.0 + (26.0 + ((21.448 - t * (46.815 + t * (0.00059 - t * 0.001813)))) / 60.0) / 60.0
    e_corr = e0 + 0.00256 * math.cos(math.radians(omega))
    decl = math.degrees(
        math.asin(math.sin(math.radians(e_corr)) * math.sin(math.radians(app_long)))
    )

    vary = math.tan(math.radians(e_corr / 2)) ** 2
    eq_time = 4 * math.degrees(
        vary * math.sin(2 * math.radians(l0))
        - 2 * e * math.sin(mrad)
        + 4 * e * vary * math.sin(mrad) * math.cos(2 * math.radians(l0))
        - 0.5 * vary * vary * math.sin(4 * math.radians(l0))
        - 1.25 * e * e * math.sin(2 * mrad)
    )

    tst = (
        (when_utc.hour * 60 + when_utc.minute + when_utc.second / 60.0)
        + eq_time
        + 4 * lon
    ) % 1440.0
    ha = tst / 4.0 - 180.0 if tst / 4.0 >= 0 else tst / 4.0 + 180.0
    if tst / 4.0 < 0:
        ha = tst / 4.0 + 180.0
    else:
        ha = tst / 4.0 - 180.0

    latr = math.radians(lat)
    declr = math.radians(decl)
    har = math.radians(ha)
    zenith = math.degrees(
        math.acos(
            max(
                -1.0,
                min(1.0, math.sin(latr) * math.sin(declr)
                    + math.cos(latr) * math.cos(declr) * math.cos(har)),
            )
        )
    )
    alt = 90.0 - zenith
    # atmospheric refraction, matters at these low sun angles
    if alt > -0.575:
        te = math.tan(math.radians(alt))
        if alt > 5:
            r = 58.1 / te - 0.07 / te**3 + 0.000086 / te**5
        elif alt > -0.575:
            r = 1735 + alt * (-518.2 + alt * (103.4 + alt * (-12.79 + alt * 0.711)))
        else:
            r = -20.772 / te
        alt += r / 3600.0

    denom = math.cos(latr) * math.sin(math.radians(zenith))
    if abs(denom) < 1e-9:
        az = 180.0
    else:
        cos_az = (math.sin(latr) * math.cos(math.radians(zenith)) - math.sin(declr)) / denom
        cos_az = max(-1.0, min(1.0, cos_az))
        az = math.degrees(math.acos(cos_az))
        if ha > 0:
            az = 360.0 - az
    return az % 360.0, alt


def lit_mask(z, res, az_deg, alt_deg):
    """True where the cell sees the sun (no terrain between it and the sun)."""
    if alt_deg <= 0:
        return np.zeros(z.shape, dtype=bool)

    az = math.radians(az_deg)
    # Unit vector toward the sun in map space: east = sin(az), north = cos(az).
    # In array indices: +col is east, +row is south.
    dcol = math.sin(az)
    drow = -math.cos(az)
    tan_alt = math.tan(math.radians(alt_deg))

    h, w = z.shape
    rows, cols = np.mgrid[0:h, 0:w].astype("float32")
    blocked = np.zeros(z.shape, dtype=bool)

    # Geometric sampling: dense near the cell (small local bumps), sparse far
    # away (only big ridges matter at distance).
    for k in range(1, RAY_STEPS + 1):
        d = MAX_RAY_M * (k / RAY_STEPS) ** 2.2
        if d < res * 0.5:
            continue
        rr = rows + drow * (d / res)
        cc = cols + dcol * (d / res)
        sample = map_coordinates(
            z, [rr, cc], order=1, mode="constant", cval=-9999.0, prefilter=False
        )
        blocked |= sample > (z + d * tan_alt)

    return ~blocked


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-09-01", help="YYYY-MM-DD, local date")
    ap.add_argument("--morning-end", type=float, default=10.0, help="local hour")
    ap.add_argument("--step-min", type=float, default=30.0)
    ap.add_argument("--utc-offset", type=float, default=2.0, help="Europe/Stockholm summer = 2")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    import datetime as dt

    with open(os.path.join(DATA, "grid.json")) as f:
        grid = json.load(f)
    w, s, e, n = grid["bounds_wgs84"]
    lat_c, lon_c = (s + n) / 2.0, (w + e) / 2.0
    res = grid["res_m"]

    with rasterio.open(TERRAIN) as src:
        names = list(src.descriptions)
        z = src.read(names.index("elev") + 1)
        profile = src.profile
    nodata = ~np.isfinite(z)
    zf = np.where(nodata, np.nanmin(z[np.isfinite(z)]), z).astype("float32")

    date = dt.date.fromisoformat(args.date)
    step_h = args.step_min / 60.0

    sun_morning = np.zeros(z.shape, dtype="float32")
    sun_day = np.zeros(z.shape, dtype="float32")
    first_light = np.full(z.shape, 24.0, dtype="float32")

    t = 0.0
    steps_done = 0
    while t < 24.0:
        when_utc = dt.datetime.combine(date, dt.time(0, 0)) + dt.timedelta(
            hours=t - args.utc_offset
        )
        az, alt = solar_position(lat_c, lon_c, when_utc)
        if alt > 0.5:
            lit = lit_mask(zf, res, az, alt)
            sun_day += lit * step_h
            if t <= args.morning_end:
                sun_morning += lit * step_h
            newly = lit & (first_light > 23.9)
            first_light[newly] = t
            steps_done += 1
            print(
                "  %05.2f local  az %6.1f  alt %5.1f  lit %5.1f%%"
                % (t, az, alt, 100.0 * lit.mean())
            )
        t += step_h

    print("%d sunlit timesteps on %s" % (steps_done, args.date))

    for arr in (sun_morning, sun_day, first_light):
        arr[nodata] = np.nan

    profile.update(count=3)
    with rasterio.open(args.out, "w", **profile) as dst:
        for i, (name, arr) in enumerate(
            [("sun_morning", sun_morning), ("sun_day", sun_day), ("first_light", first_light)],
            start=1,
        ):
            dst.write(arr, i)
            dst.set_band_description(i, name)

    print("wrote %s" % args.out)
    for name, arr in [
        ("sun_morning", sun_morning),
        ("sun_day", sun_day),
        ("first_light", first_light),
    ]:
        v = arr[np.isfinite(arr)]
        print("  %-12s min %6.2f  median %6.2f  max %6.2f" % (name, v.min(), np.median(v), v.max()))


if __name__ == "__main__":
    main()
