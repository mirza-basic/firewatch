"""Verify firedanger.py's Canadian FWI System port against the official reference.

Rows below are the exact published validation dataset for this algorithm (Van
Wagner & Pickett 1985), transcribed from NRCan's own reference implementation -
github.com/cffdrs/cffdrs_py, cffdrs/tests/data/fwi_test_data.csv - not generated
by this project. lat=40, starting from the standard startup codes
(FFMC=85, DMC=6, DC=15), replayed day by day with lat_adjust on (the >=30N band).
A transcription mistake in firedanger.py's constants fails here, rather than
silently mis-rating a real fire day.
"""
import sys
sys.path.insert(0, "/Users/mirza.basic/Projects/Personal/fire-detection")
from firewatch import firedanger as fd

LAT = 40.0

# Each row: (mon, temp, rh, ws, prec, FFMC, DMC, DC, ISI, BUI, FWI, DSR) - the
# published input columns plus the published output columns, LONG/LAT/YR/DAY
# dropped since they do not feed the calculation.
ROWS = [
    (4, 17, 42, 25, 0, 87.6, 8.5, 19, 10.8, 8.5, 10, 1.61),
    (4, 20, 21, 25, 2.4, 86.2, 10.4, 23.6, 8.8, 10.4, 9.2, 1.39),
    (4, 8.5, 40, 17, 0, 86.9, 11.8, 26.1, 6.5, 11.7, 7.5, 0.97),
    (4, 6.5, 25, 6, 0, 88.8, 13.2, 28.2, 4.9, 13.1, 6.1, 0.67),
    (4, 13, 34, 24, 0, 89, 15.4, 31.5, 12.5, 15.3, 14.7, 3.18),
    (4, 6, 40, 22, 0.4, 88.6, 16.5, 33.5, 10.6, 16.4, 13.4, 2.67),
    (4, 5.5, 52, 6, 0, 87.3, 17.2, 35.4, 3.9, 17.1, 5.8, 0.61),
    (4, 8.5, 46, 16, 0, 87.3, 18.5, 37.9, 6.5, 18.4, 9.5, 1.46),
    (4, 9.5, 54, 20, 0, 86.7, 19.7, 40.6, 7.3, 19.6, 10.9, 1.86),
    (4, 7, 93, 14, 9, 29.8, 10.1, 29.5, 0, 10.9, 0, 0),
    (4, 6.5, 71, 17, 1, 49.4, 10.7, 31.6, 0.4, 11.6, 0.2, 0),
    (4, 6, 59, 17, 0, 67.2, 11.4, 33.7, 1.3, 12.3, 0.9, 0.02),
    (4, 13, 52, 4, 0, 77.7, 13, 37, 1.1, 13.9, 0.8, 0.02),
    (4, 15.5, 40, 11, 0, 85.4, 15.4, 40.7, 3.9, 15.9, 5.5, 0.55),
    (4, 23, 25, 9, 0, 91.5, 19.8, 45.8, 8.3, 19.8, 12.1, 2.24),
    (4, 19, 46, 16, 0, 89.9, 22.5, 50.2, 9.4, 22.4, 14.2, 2.98),
    (4, 18, 41, 20, 0, 89.9, 25.2, 54.4, 11.5, 25.1, 17.5, 4.31),
    (4, 14.5, 51, 16, 0, 88.4, 27, 57.9, 7.6, 27, 13.2, 2.61),
    (5, 14.5, 69, 11, 0, 85.6, 28.3, 63, 4, 28.2, 7.9, 1.07),
    (5, 15.5, 42, 8, 0, 87.3, 30.8, 68.2, 4.4, 30.8, 9, 1.33),
    (5, 21, 37, 8, 0, 89.3, 34.5, 74.3, 5.8, 34.4, 12.2, 2.27),
    (5, 23, 32, 16, 0, 90.9, 38.8, 80.9, 11, 38.7, 20.9, 5.9),
    (5, 23, 32, 14, 0, 91.2, 43.1, 87.4, 10.2, 43, 20.9, 5.93),
    (5, 27, 33, 12, 0, 91.6, 48.1, 94.7, 9.9, 47.9, 21.6, 6.24),
    (5, 28, 17, 27, 0, 95.1, 54.5, 102.1, 34.3, 54.3, 52.3, 29.96),
    (5, 23.5, 54, 20, 0, 89.7, 57.4, 108.8, 11.2, 57.2, 25.7, 8.53),
    (5, 16, 50, 22, 12.2, 62.2, 29.9, 91.8, 1.4, 33, 3, 0.19),
    (5, 11, 58, 20, 0, 76.4, 31.3, 96.2, 2.3, 34.5, 5.3, 0.53),
    (5, 16, 54, 16, 0, 83.3, 33.3, 101.5, 3.8, 36.6, 8.8, 1.27),
    (5, 21.5, 37, 9, 0, 88.6, 37.1, 107.7, 5.5, 39.9, 12.6, 2.42),
    (5, 14, 61, 22, 0.2, 86.6, 38.6, 112.7, 8, 41.6, 17.2, 4.18),
    (5, 15, 30, 27, 0, 89.6, 41.6, 117.8, 15.7, 44.2, 28.7, 10.32),
    (5, 20, 23, 11, 0, 92.1, 45.9, 123.8, 10, 47.6, 21.7, 6.32),
    (5, 14, 95, 3, 16.4, 21.3, 20.1, 96.9, 0, 26.5, 0, 0),
    (5, 20, 53, 4, 2.8, 51, 18.2, 102.9, 0.2, 25.3, 0.2, 0),
    (5, 19.5, 30, 16, 0, 82.2, 22, 108.8, 3.3, 29.3, 6.8, 0.8),
    (5, 25.5, 51, 20, 6, 75.3, 16.4, 106.3, 2.1, 23.6, 3.8, 0.29),
    (5, 10, 38, 24, 0, 84.3, 18.2, 110.5, 6.4, 25.8, 11.2, 1.96),
    (5, 19, 27, 16, 0, 90.3, 22, 116.4, 9.9, 29.9, 17.1, 4.14),
    (5, 26, 46, 11, 4.2, 77.5, 18.7, 117.7, 1.6, 26.7, 2.9, 0.18),
    (5, 30, 38, 22, 0, 90.2, 23.7, 125.5, 13.3, 32.2, 21.9, 6.42),
    (5, 25.5, 67, 19, 12.6, 65.3, 13.1, 108.4, 1.4, 20.2, 1.9, 0.08),
    (5, 12, 53, 28, 11.8, 55.4, 7.7, 91.6, 1.2, 12.8, 0.8, 0.02),
    (5, 21, 38, 8, 0, 80.8, 11.3, 97.8, 1.9, 17.6, 2.5, 0.14),
    (5, 13, 70, 20, 3.8, 61.7, 8.4, 97.8, 1.2, 13.8, 0.9, 0.02),
    (5, 9, 78, 24, 1.4, 64.4, 9, 101.8, 1.7, 14.7, 1.9, 0.09),
    (5, 11, 54, 16, 0, 77.6, 10.5, 106.2, 2, 16.8, 2.8, 0.16),
    (5, 15.5, 39, 9, 0, 85.4, 13.1, 111.4, 3.5, 20.3, 5.7, 0.6),
]

passed = failed = 0


def check(name, got, want, tol):
    global passed, failed
    ok = abs(got - want) <= tol
    print(f"  {'PASS' if ok else 'FAIL'}  {name:22s} got={got:.3f} want={want} (tol {tol})")
    if ok:
        passed += 1
    else:
        failed += 1


codes = {"ffmc": fd.FFMC0, "dmc": fd.DMC0, "dc": fd.DC0}
# Published values are rounded to the source's own precision (mostly 1 decimal,
# occasionally 2-3 for DSR); 0.15 comfortably covers that rounding without
# masking a real transcription error, which would show up as a much larger diff.
TOL = 0.15
for day, row in enumerate(ROWS, start=1):
    mon, temp, rh, ws, prec, ffmc_w, dmc_w, dc_w, isi_w, bui_w, fwi_w, dsr_w = row
    codes = fd.step(codes, temp, rh, ws, prec, LAT, mon)
    check(f"day {day:>2} FFMC", codes["ffmc"], ffmc_w, TOL)
    check(f"day {day:>2} DMC", codes["dmc"], dmc_w, TOL)
    check(f"day {day:>2} DC", codes["dc"], dc_w, TOL)
    check(f"day {day:>2} ISI", codes["isi"], isi_w, TOL)
    check(f"day {day:>2} BUI", codes["bui"], bui_w, TOL)
    check(f"day {day:>2} FWI", codes["fwi"], fwi_w, TOL)

# Danger classification against the exact EFFIS legend thresholds (mf010.fwi,
# GetLegendGraphic) - a class boundary error would misreport a whole tier, e.g.
# tell a reader "high" when the official map would show "extreme".
ok = True
for fwi_val, want_class in ((5.0, "low"), (11.2, "moderate"), (21.29, "moderate"),
                            (21.3, "high"), (37.9, "high"), (38.0, "very_high"),
                            (49.9, "very_high"), (50.0, "extreme"),
                            (69.9, "extreme"), (70.0, "very_extreme"),
                            (200.0, "very_extreme")):
    key, _color = fd.classify(fwi_val)
    good = key == want_class
    ok &= good
    print(f"  {'PASS' if good else 'FAIL'}  {'classify(' + str(fwi_val) + ')':22s} "
          f"got={key} want={want_class}")
passed += ok
failed += not ok

# geo.forecast_point() must be inside the boundary it was derived from - this is
# the "moves with a forked boundary file" property, and a mean-of-vertices bug
# (e.g. reading (lat,lon) as (lon,lat)) would put it far outside instead.
from firewatch import geo  # noqa: E402
lat_p, lon_p = geo.forecast_point()
inside = geo.point_in_boundary(lat_p, lon_p)
print(f"  {'PASS' if inside else 'FAIL'}  {'forecast_point() inside boundary':34s} "
      f"({lat_p:.4f}, {lon_p:.4f})")
passed += inside
failed += not inside

print(f"\n  {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
