import sys, os, urllib.request
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")); os.makedirs("snow", exist_ok=True)
spans = [("20240114", "20240119"), ("20221102", "20230315"), ("20241116", "20250228"), ("20141118", "20141121"), ("20171225", "20171228"),
         ("20200228", "20200301"), ("20161120", "20161122")]
days = []
for a, b in spans:
    t, e = datetime.strptime(a, "%Y%m%d"), datetime.strptime(b, "%Y%m%d")
    while t <= e:
        days.append(t); t += timedelta(days=1)
def get(d):
    p = "snow/%s.tif" % d.strftime("%Y%m%d")
    if os.path.exists(p): return None
    u = "https://www.nohrsc.noaa.gov/snowfall_v2/data/%s/sfav2_CONUS_24h_%s12.tif" % (d.strftime("%Y%m"), d.strftime("%Y%m%d"))
    try:
        raw = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=60).read()
        open(p, "wb").write(raw); return None
    except Exception as e:
        return "%s %r" % (d.strftime("%Y%m%d"), e)
with ThreadPoolExecutor(4) as ex:
    bad = [r for r in ex.map(get, days) if r]
print(len(days), "days", len(bad), "missing", bad[:20])
