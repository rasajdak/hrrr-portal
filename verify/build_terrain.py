"""
lake_terrain.npz: ground height round the Great Lakes on a 0.05 degree grid,
for the terrain term in lake_effect.py. Taken from HRRR's own orography (one
GRIB message, 3 km, no voids), so it is built once and kept in the repo.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timezone
import numpy as np
from scipy.spatial import cKDTree
import hrrr_render as hr, lake_effect

W, E, S, N = lake_effect.BOX
STEP, PAD = 0.05, 0.5
run = datetime(2022, 11, 18, 12, tzinfo=timezone.utc)
arrs, lat, lon = hr._one("hrrr", hr._grib_url("hrrr", "sfc", run, 0), ":HGT:surface:")
glon = np.arange(W - PAD, E + PAD + 1e-6, STEP)
glat = np.arange(S - PAD, N + PAD + 1e-6, STEP)
LON, LAT = np.meshgrid(glon, glat)
k = np.cos(np.radians(45.5))
near = (lon > W - PAD - 1) & (lon < E + PAD + 1) & (lat > S - PAD - 1) & (lat < N + PAD + 1)
d, i = cKDTree(np.c_[lon[near] * k, lat[near]]).query(np.c_[LON.ravel() * k, LAT.ravel()], k=4)
wgt = 1.0 / np.maximum(d, 1e-6) ** 2
z = (arrs[0][near][i] * wgt).sum(axis=1) / wgt.sum(axis=1)
z[d[:, 0] > 0.1] = 0                                   # off the edge of HRRR's domain
out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lake_terrain.npz")
np.savez_compressed(out, lon0=glon[0], lat0=glat[0], step=STEP, z=np.rint(z).reshape(LAT.shape).astype("int16"))
print(out, z.reshape(LAT.shape).shape, os.path.getsize(out), "bytes; off-domain cells", int((d[:, 0] > 0.1).sum()))
at = lambda la, lo: z.reshape(LAT.shape)[int(round((la - glat[0]) / STEP)), int(round((lo - glon[0]) / STEP))]
for nm, la, lo in (("Lake Ontario", 43.6, -77.5), ("Oswego", 43.45, -76.51), ("Redfield", 43.54, -75.81), ("Tug Hill top", 43.75, -75.6),
                   ("Watertown", 43.97, -75.91), ("Buffalo", 42.94, -78.73), ("Orchard Park", 42.77, -78.74), ("Chautauqua ridge", 42.3, -79.3)):
    print("  %-18s %4.0f m" % (nm, at(la, lo)))
