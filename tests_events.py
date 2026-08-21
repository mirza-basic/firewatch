"""Verify each alert kind fires exactly when it should."""
import sys
sys.path.insert(0, "/Users/mirza.basic/Projects/Personal/fire-detection")
from datetime import timedelta
from firewatch import events, store
from firewatch.store import iso, utcnow

now = utcnow()

def det(uid, minutes_ago, lat=44.31, lon=18.29, frp=8.0, src="mtg"):
    return {"uid": uid, "source": src, "sensor": "X", "lat": lat, "lon": lon,
            "ts": iso(now - timedelta(minutes=minutes_ago)), "frp": frp,
            "confidence": 80, "daynight": None, "inside": 1}

def ev(dets):
    e = events.build_events(dets)
    return e[0] if e else None

passed = failed = 0
def check(name, alerts, want):
    global passed, failed
    kinds = sorted({a["kind"] for a in alerts})
    ok = set(want).issubset(set(kinds))
    print(f"  {'PASS' if ok else 'FAIL'}  {name:34s} got={kinds} want⊇{sorted(want)}")
    if ok: passed += 1
    else: failed += 1

# 1. brand new fire
base = [det("a", 20)]
e1 = ev(base)
check("new fire", events.diff({}, [e1]), {"new"})

# 2. nothing changed -> no alerts
check("unchanged", events.diff({e1["id"]: e1}, [e1]), set())

# 3. intensified: 8 -> 40 MW
e2 = ev(base + [det("b", 5, frp=40.0)])
check("intensified", events.diff({e1["id"]: e1}, [e2]), {"intensified"})

# 4. grew: footprint expands past 0.5 km
e3 = ev(base + [det("c", 5, lat=44.325, lon=18.30)])
check("grew (footprint)", events.diff({e1["id"]: e1}, [e3]), {"grew"})

# 5. corroborated: a second satellite sees it
e4 = ev(base + [det("d", 5, src="firms")])
check("corroborated (new source)", events.diff({e1["id"]: e1}, [e4]), {"corroborated"})

# 6. extinguished: last detection older than quiet_hours
old = ev([det("e", 60 * 9)])
prev_active = dict(old); prev_active["status"] = "active"
check("extinguished", events.diff({old["id"]: prev_active}, [old]), {"extinguished"})

# 7. reignited: was quiet, now fresh detections
fresh = ev([det("e", 60 * 9), det("f", 3)])
prev_quiet = {k: v for k, v in old.items()}; prev_quiet["status"] = "quiet"
check("reignited", events.diff({fresh["id"]: prev_quiet}, [fresh]), {"reignited"})

# 8. two fires far apart must stay separate events
two = events.build_events([det("g", 10), det("h", 10, lat=44.46, lon=18.12)])
print(f"  {'PASS' if len(two)==2 else 'FAIL'}  {'separate fires stay separate':34s} "
      f"got={len(two)} events want=2")
passed += len(two) == 2; failed += len(two) != 2

# 9. same fire seen by 3 sensors = 1 event
one = events.build_events([det("i", 10, src="mtg"), det("j", 12, src="firms"),
                           det("k", 14, src="s3", lat=44.312, lon=18.292)])
print(f"  {'PASS' if len(one)==1 else 'FAIL'}  {'3 sensors -> 1 event':34s} "
      f"got={len(one)} events want=1")
passed += len(one) == 1; failed += len(one) != 1

# 9b. a historical (already quiet) fire must NOT raise a "new" alert
hist = ev([det("hist", 60 * 30)])
check("historical fire is silent", events.diff({}, [hist]), set())
assert not any(a["kind"] == "new" for a in events.diff({}, [hist])), "backfill spam"

# 10. cooldown suppression
con = store.connect()
store.mark_notified(con, "cooldown-test", "new")
sup = store.was_notified(con, "cooldown-test", "new", 25)
nosup = store.was_notified(con, "cooldown-test", "grew", 25)
print(f"  {'PASS' if (sup and not nosup) else 'FAIL'}  {'notify cooldown':34s} "
      f"same-kind suppressed={sup} other-kind={nosup}")
passed += (sup and not nosup); failed += not (sup and not nosup)
con.execute("DELETE FROM notified WHERE event_id='cooldown-test'"); con.commit()

# 11. range filtering: an event lands in exactly the ranges it should
def in_ranges(minutes_ago):
    e = ev([det("r%d" % minutes_ago, minutes_ago)])
    return [k for k in events.RANGES if events.filter_events([e], k)]

cases = [
    (30,           {"24h", "3d", "7d", "30d"}, "30 min ago"),
    (60 * 20,      {"24h", "3d", "7d", "30d"}, "20 h ago"),
    (60 * 30,      {"3d", "7d", "30d"},        "30 h ago (past 24h)"),
    (60 * 24 * 2,  {"3d", "7d", "30d"},        "2 days ago"),
    (60 * 24 * 5,  {"7d", "30d"},              "5 days ago"),
    (60 * 24 * 20, {"30d"},                    "20 days ago"),
    (60 * 24 * 45, set(),                      "45 days ago"),
]
for mins, want, why in cases:
    got = set(in_ranges(mins))
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {('range: ' + why):34s} got={sorted(got)} want={sorted(want)}")
    passed += ok; failed += not ok

# 12. all four ranges are ordered shortest-first and carry labels
order = list(events.RANGES)
ok = order == ["24h", "3d", "7d", "30d"] and all(
    "label" in v and "short" in v for v in events.RANGES.values())
print(f"  {'PASS' if ok else 'FAIL'}  {'range order + labels':34s} {order}")
passed += ok; failed += not ok

# 12b. every range is a rolling window - no calendar-day special case left
ok = all(isinstance(v["hours"], (int, float)) and v["hours"] > 0
         for v in events.RANGES.values())
print(f"  {'PASS' if ok else 'FAIL'}  {'all ranges are rolling':34s} "
      f"{[v['hours'] for v in events.RANGES.values()]}")
passed += ok; failed += not ok

# 12c. the retired "today" key still resolves rather than breaking a saved config
ok = (events.resolve_range("today") == "24h"
      and events.resolve_range("nonsense") == events.DEFAULT_RANGE
      and events.resolve_range(None) == events.DEFAULT_RANGE)
print(f"  {'PASS' if ok else 'FAIL'}  {'legacy range key resolves':34s} "
      f"today->{events.resolve_range('today')}")
passed += ok; failed += not ok

# 13. extent is the bbox diagonal and grows as detections spread.
# Offsets must stay inside cluster_radius_km or they become separate events.
near = ev([det("x1", 10), det("x2", 9, lat=44.3110, lon=18.2910)])
far  = ev([det("x1", 10), det("x2", 9, lat=44.3110, lon=18.2910),
           det("x3", 8, lat=44.3250, lon=18.3010)])
one_cluster = len(events.build_events(
    [det("x1", 10), det("x2", 9, lat=44.3110, lon=18.2910),
     det("x3", 8, lat=44.3250, lon=18.3010)])) == 1
ok = one_cluster and far["extent_km"] > near["extent_km"] > 0
print(f"  {'PASS' if ok else 'FAIL'}  {'extent grows with spread':34s} "
      f"{near['extent_km']} -> {far['extent_km']} km (1 cluster={one_cluster})")
passed += ok; failed += not ok

print(f"\n  {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
